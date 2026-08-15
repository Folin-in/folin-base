"""Behaviour events the gamification engine scores. XP rewards what the user DOES, never
what their biology does - see gamification.py."""
from db import session as db
from services.gamification import EVENT_XP

EVENT_XP_TYPES = frozenset(EVENT_XP)


async def fetch_events(user_id: str) -> list[dict]:
    return await db.fetch("select type, report_id, ref, at from engagement_events where user_id = $1", user_id)


async def record_event_once(user_id: str, event_type: str, at: str) -> None:
    exists = await db.fetchval(
        "select 1 from engagement_events where user_id = $1 and type = $2 limit 1", user_id, event_type)
    if not exists:
        await db.execute("insert into engagement_events (user_id, type, at) values ($1, $2, $3)",
                         user_id, event_type, at)
