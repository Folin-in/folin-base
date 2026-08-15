"""Sub-profile ("family member") helpers shared by several routers and the pipeline."""
from fastapi import HTTPException

from db import session as db

SELF = "self"


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
