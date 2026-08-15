"""Sub-profiles ("family members" the account holder tracks: a parent, a child). They
carry only name / relation / age / gender / allergies - the account holder's own habits
(diet, workout, language) are inherited at analysis time so suggestions still make sense,
but their allergies are NOT copied over (that would produce wrong food swaps)."""
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.enums import Plan
from app.models import GENDERS, MEMBER_RELATIONS, MemberUpsertRequest
from core import cache
from db import session as db

SELF = "self"
MEMBER_ID_HEX = 16
MEMBER_LIST_LIMIT = 50
NAME_MAX = 60
ALLERGY_MAX = 40
DEFAULT_RELATION = "family"

MSG_MEMBERS_PRO = "Family member profiles are a Pro feature. Upgrade to unlock."
MSG_GENDER = "gender must be 'male' or 'female'"
MSG_EDIT_SELF = "Edit 'self' via the profile screen."
MSG_DELETE_SELF = "You can't delete yourself."


class MemberNotFound(HTTPException):
    def __init__(self):
        super().__init__(status_code=404, detail="Member not found")


def member_id_of(member_id: str | None) -> str:
    return (member_id or SELF).strip() or SELF


async def resolve_member_profile(current_user: dict, member_id: str) -> tuple[str, dict]:
    """(display_name, health-profile-like dict) for member_id; 'self' is the account holder.

    Members only carry age + gender. Movement/diet/language default to the account
    holder's habits so suggestions still make sense. Allergies are personal - the account
    holder's are NOT copied onto a member's report (that would produce wrong food swaps)."""
    if not member_id or member_id == SELF:
        return (current_user.get("name") or current_user.get("username") or "",
                current_user.get("health_profile") or {})
    doc = await db.fetchrow("select * from members where member_id = $1 and user_id = $2",
                            member_id, current_user["user_id"])
    if not doc:
        raise MemberNotFound()
    parent_hp = current_user.get("health_profile") or {}
    return doc.get("name") or "", {
        "age": doc.get("age"),
        "gender": doc.get("gender"),
        "workout": parent_hp.get("workout"),
        "workout_type": parent_hp.get("workout_type"),
        "diet": parent_hp.get("diet"),
        "language": parent_hp.get("language"),
        "allergies": doc.get("allergies") or [],
    }


# ---------------------------------------------------------------- CRUD

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relation(rel: str | None) -> str:
    rel = (rel or DEFAULT_RELATION).lower().strip()
    return rel if rel in MEMBER_RELATIONS else DEFAULT_RELATION


def _allergies(items) -> list[str]:
    return [str(a).strip()[:ALLERGY_MAX] for a in (items or []) if str(a).strip()]


def serialize(m: dict) -> dict:
    return {"member_id": m.get("member_id"), "user_id": m.get("user_id"), "name": m.get("name") or "",
            "relation": m.get("relation") or DEFAULT_RELATION, "age": m.get("age"), "gender": m.get("gender"),
            "allergies": m.get("allergies") or [], "created_at": m.get("created_at")}


def self_member(user: dict) -> dict:
    hp = user.get("health_profile") or {}
    return {"member_id": SELF, "user_id": user["user_id"], "name": user.get("name") or user.get("username") or "You",
            "relation": SELF, "age": hp.get("age"), "gender": hp.get("gender"), "allergies": hp.get("allergies") or [],
            "created_at": user.get("created_at")}


class MemberService:
    @staticmethod
    async def list_for(user: dict) -> list[dict]:
        docs = await db.fetch("select * from members where user_id = $1 order by created_at asc limit $2",
                              user["user_id"], MEMBER_LIST_LIMIT)
        return [self_member(user)] + [serialize(m) for m in docs]

    @staticmethod
    async def create(req: MemberUpsertRequest, user: dict) -> dict:
        if ((user.get("preferences") or {}).get("plan") or Plan.FREE) != Plan.PRO:
            raise HTTPException(status_code=403, detail=MSG_MEMBERS_PRO)
        if req.gender is not None and req.gender not in GENDERS:
            raise HTTPException(status_code=400, detail=MSG_GENDER)
        doc = {"member_id": uuid.uuid4().hex[:MEMBER_ID_HEX], "user_id": user["user_id"],
               "name": req.name.strip()[:NAME_MAX], "relation": _relation(req.relation), "age": req.age,
               "gender": req.gender, "allergies": _allergies(req.allergies), "created_at": _now()}
        await db.execute(
            """insert into members (member_id, user_id, name, relation, age, gender, allergies, created_at)
               values ($1, $2, $3, $4, $5, $6, $7, $8)""",
            *(doc[k] for k in ("member_id", "user_id", "name", "relation", "age", "gender", "allergies", "created_at")))
        return serialize(doc)

    @staticmethod
    async def update(member_id: str, req: MemberUpsertRequest, user: dict) -> dict:
        if member_id == SELF:
            raise HTTPException(status_code=400, detail=MSG_EDIT_SELF)
        if req.gender is not None and req.gender not in GENDERS:
            raise HTTPException(status_code=400, detail=MSG_GENDER)
        if req.allergies is not None:
            updated = await db.fetchval(
                """update members set name = $3, relation = $4, age = $5, gender = $6, allergies = $7
                   where member_id = $1 and user_id = $2 returning 1""",
                member_id, user["user_id"], req.name.strip()[:NAME_MAX], _relation(req.relation), req.age, req.gender,
                _allergies(req.allergies))
        else:
            updated = await db.fetchval(
                """update members set name = $3, relation = $4, age = $5, gender = $6
                   where member_id = $1 and user_id = $2 returning 1""",
                member_id, user["user_id"], req.name.strip()[:NAME_MAX], _relation(req.relation), req.age, req.gender)
        if not updated:
            raise MemberNotFound()
        return serialize(await db.fetchrow("select * from members where member_id = $1 and user_id = $2",
                                           member_id, user["user_id"]))

    @staticmethod
    async def delete(member_id: str, user_id: str) -> None:
        """Soft-deletes the member's reports first (matching DELETE /reports/{id}) - hard-deleting
        them orphaned their files in Storage and made an accidental delete unrecoverable."""
        if member_id == SELF:
            raise HTTPException(status_code=400, detail=MSG_DELETE_SELF)
        await db.execute(
            "update reports set deleted_at = $3 where user_id = $1 and member_id = $2 and deleted_at is null",
            user_id, member_id, _now())
        cache.invalidate_reports(user_id)
        deleted = await db.fetchval("delete from members where member_id = $1 and user_id = $2 returning 1",
                                    member_id, user_id)
        if not deleted:
            raise MemberNotFound()


members = MemberService()
