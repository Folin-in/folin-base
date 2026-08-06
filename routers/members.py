"""Family-member (sub-profile) routes - spec item #11.

Members are sub-profiles under one user's account so a user can log parents' /
kids' reports without polluting their own gamification. "self" is a virtual
member_id that always exists and maps to the account holder.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

import cache
import database as db
from models import GENDERS, MEMBER_RELATIONS, MemberUpsertRequest
from security import get_current_user

router = APIRouter(prefix="/members", tags=["members"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_relation(rel: str) -> str:
    rel = (rel or "family").lower().strip()
    return rel if rel in MEMBER_RELATIONS else "family"


def _serialize(m: dict) -> dict:
    return {
        "member_id": m.get("member_id"),
        "user_id": m.get("user_id"),
        "name": m.get("name") or "",
        "relation": m.get("relation") or "family",
        "age": m.get("age"),
        "gender": m.get("gender"),
        "allergies": m.get("allergies") or [],
        "created_at": m.get("created_at"),
    }


def _self_member(user: dict) -> dict:
    hp = user.get("health_profile") or {}
    return {
        "member_id": "self",
        "user_id": user["user_id"],
        "name": user.get("name") or user.get("username") or "You",
        "relation": "self",
        "age": hp.get("age"),
        "gender": hp.get("gender"),
        "allergies": hp.get("allergies") or [],
        "created_at": user.get("created_at"),
    }


@router.get("")
async def list_members(current_user: dict = Depends(get_current_user)):
    docs = await db.fetch(
        "select * from members where user_id = $1 order by created_at asc limit 50", current_user["user_id"]
    )
    return [_self_member(current_user)] + [_serialize(m) for m in docs]


@router.post("")
async def create_member(req: MemberUpsertRequest, current_user: dict = Depends(get_current_user)):
    # PRO feature: family-member sub-profiles. Existing members keep working on free.
    plan = (current_user.get("preferences") or {}).get("plan") or "free"
    if plan != "pro":
        raise HTTPException(status_code=403, detail="Family member profiles are a Pro feature. Upgrade to unlock.")
    if req.gender is not None and req.gender not in GENDERS:
        raise HTTPException(status_code=400, detail="gender must be 'male' or 'female'")
    doc = {
        "member_id": uuid.uuid4().hex[:16],
        "user_id": current_user["user_id"],
        "name": req.name.strip()[:60],
        "relation": _validate_relation(req.relation),
        "age": req.age,
        "gender": req.gender,
        "allergies": [str(a).strip()[:40] for a in (req.allergies or []) if str(a).strip()],
        "created_at": _now(),
    }
    await db.execute(
        """insert into members (member_id, user_id, name, relation, age, gender, allergies, created_at)
           values ($1, $2, $3, $4, $5, $6, $7, $8)""",
        doc["member_id"], doc["user_id"], doc["name"], doc["relation"], doc["age"],
        doc["gender"], doc["allergies"], doc["created_at"],
    )
    return _serialize(doc)


@router.patch("/{member_id}")
async def update_member(member_id: str, req: MemberUpsertRequest, current_user: dict = Depends(get_current_user)):
    if member_id == "self":
        raise HTTPException(status_code=400, detail="Edit 'self' via the profile screen.")
    if req.gender is not None and req.gender not in GENDERS:
        raise HTTPException(status_code=400, detail="gender must be 'male' or 'female'")
    sets = "name = $3, relation = $4, age = $5, gender = $6"
    args = [member_id, current_user["user_id"], req.name.strip()[:60], _validate_relation(req.relation),
            req.age, req.gender]
    # If allergies wasn't sent, don't overwrite it
    if req.allergies is not None:
        sets += ", allergies = $7"
        args.append([str(a).strip()[:40] for a in req.allergies if str(a).strip()])
    updated = await db.fetchval(
        f"update members set {sets} where member_id = $1 and user_id = $2 returning 1", *args
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Member not found")
    doc = await db.fetchrow(
        "select * from members where member_id = $1 and user_id = $2", member_id, current_user["user_id"]
    )
    return _serialize(doc)


@router.delete("/{member_id}")
async def delete_member(member_id: str, current_user: dict = Depends(get_current_user)):
    if member_id == "self":
        raise HTTPException(status_code=400, detail="You can't delete yourself.")
    # Soft-delete the member's reports, matching DELETE /reports/{id}. Hard-deleting
    # them orphaned their files in Storage forever (account deletion reads paths from
    # this table), and made an accidental member delete unrecoverable.
    await db.execute(
        """update reports set deleted_at = $3
           where user_id = $1 and member_id = $2 and deleted_at is null""",
        current_user["user_id"], member_id, datetime.now(timezone.utc).isoformat(),
    )
    cache.invalidate_reports(current_user["user_id"])
    deleted = await db.fetchval(
        "delete from members where member_id = $1 and user_id = $2 returning 1",
        member_id, current_user["user_id"],
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Member not found")
    return {"deleted": True}


async def resolve_member_profile(current_user: dict, member_id: str) -> tuple[str, dict]:
    """Return (display_name, health_profile_like_dict) for the given member_id.
    For 'self' returns the user's own name + health_profile.
    Raises 404 if member is not owned by the user.
    """
    if not member_id or member_id == "self":
        return current_user.get("name") or current_user.get("username") or "", (current_user.get("health_profile") or {})
    doc = await db.fetchrow(
        "select * from members where member_id = $1 and user_id = $2", member_id, current_user["user_id"]
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Member not found")
    # Members only carry age + gender. Movement/diet/language default to the account holder's
    # habits so suggestions still make sense. Allergies are personal - we don't blast the
    # account holder's allergies onto a member's report (would produce wrong food subs).
    parent_hp = current_user.get("health_profile") or {}
    hp = {
        "age": doc.get("age"),
        "gender": doc.get("gender"),
        "workout": parent_hp.get("workout"),
        "workout_type": parent_hp.get("workout_type"),
        "diet": parent_hp.get("diet"),
        "language": parent_hp.get("language"),
        "allergies": doc.get("allergies") or [],
    }
    return doc.get("name") or "", hp
