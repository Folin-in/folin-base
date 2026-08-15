"""Family sharing: invite -> invitee accepts -> two-way link. Members see each other's
score, trend, level and report count - never raw markers.

Invite security invariants:
- Uniform response from /invite whatever the state of the target address (unknown,
  already linked, already invited) - anything else enumerates accounts.
- An invite is BOUND to the invitee's user_id at creation when that account already
  exists. Accept and decline then require an exact user_id match, so a later email
  change on a different account cannot steal it. Unbound invites (account did not exist
  yet) fall back to email match, and are only visible to a caller whose email matches.
- Sending invites is Pro; accepting one is free, else invites would only ever connect
  Pro users to Pro users.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.enums import Plan
from app.models import RELATIONSHIPS, FamilyInviteRequest
from core import cache
from core.email import send_family_invite
from core.rate_limit import check_and_increment_daily_quota
from db import session as db
from services.engagement import fetch_events
from services.gamification import compute_profile
from services.members import SELF

logger = logging.getLogger(__name__)

INVITE_LIST_LIMIT = 50
LINK_LIST_LIMIT = 200
TREND_REPORTS = 20
DEFAULT_RELATIONSHIP = "family"
INVITE_BUCKET = "family_invite_daily"
STATUS_PENDING, STATUS_ACCEPTED, STATUS_DECLINED = "pending", "accepted", "declined"

MSG_INVITE_PRO = "Inviting family members is a Pro feature. Upgrade to unlock."
MSG_SELF_INVITE = "You can't invite yourself."
MSG_INVITE_NOT_FOUND = "Invite not found"
MSG_OWN_INVITE = "You can't accept your own invite."
INVITE_SENT = {"status": "invite_sent"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display(u: dict) -> str:
    return u.get("name") or u.get("username") or (u.get("email") or "").split("@")[0]


class FamilyService:
    # ------------------------------------------------------------ summaries

    @staticmethod
    async def member_summary(user_id: str, user: dict | None = None) -> dict:
        """Share-safe view of one account: score, trend and metadata, never raw markers.
        Only the holder's OWN lab reports count - sub-profiles and non-lab documents used
        to leak into the score their family saw."""
        user = user or await db.fetchrow("select * from users where user_id = $1", user_id)
        if not user:
            return {}
        own = [r for r in await cache.get_reports(user_id) if r.get("member_id", SELF) == SELF]
        labs = sorted([r for r in own if (r.get("report_type") or "lab") == "lab"],
                      key=lambda r: r.get("uploaded_at") or "", reverse=True)[:TREND_REPORTS]
        profile = compute_profile(own, events=await fetch_events(user_id))
        latest = labs[0] if labs else None
        return {
            "user_id": user_id, "email": user["email"], "name": _display(user), "picture": user.get("picture"),
            "score": latest.get("health_score") if latest else None,
            "score_trend": [d.get("health_score", 0) for d in reversed(labs)],
            "level": profile["level"], "reports_count": profile["reports_count"],
            "updated_at": (latest.get("report_date") or (latest.get("uploaded_at") or "")[:10]) if latest else None,
        }

    async def members(self, user: dict) -> dict:
        """One query for every linked user row, then all summaries concurrently."""
        links = await db.fetch("select * from family_links where a = $1 limit $2", user["user_id"], LINK_LIST_LIMIT)
        ids = [l["b"] for l in links]
        rows = await db.fetch("select * from users where user_id = any($1::text[])", ids) if ids else []
        by_id = {r["user_id"]: r for r in rows}
        summaries = await asyncio.gather(*(self.member_summary(l["b"], by_id.get(l["b"])) for l in links))
        members = []
        for link, s in zip(links, summaries):
            if s:
                members.append({**s, "relationship": link.get("relationship", DEFAULT_RELATIONSHIP)})
        me = await self.member_summary(user["user_id"], user)
        return {"self": {**me, "relationship": "you"}, "members": members}

    @staticmethod
    async def remove_member(user_id: str, other_id: str) -> None:
        await db.execute("delete from family_links where (a = $1 and b = $2) or (a = $2 and b = $1)", user_id, other_id)

    # ------------------------------------------------------------ outgoing invites

    @staticmethod
    async def invite(req: FamilyInviteRequest, user: dict) -> dict:
        if ((user.get("preferences") or {}).get("plan") or Plan.FREE) != Plan.PRO:
            raise HTTPException(status_code=403, detail=MSG_INVITE_PRO)
        invitee_email = req.email.lower().strip()
        if invitee_email == user["email"].lower():
            raise HTTPException(status_code=400, detail=MSG_SELF_INVITE)
        relationship = (req.relationship or DEFAULT_RELATIONSHIP).lower().strip()
        if relationship not in RELATIONSHIPS:
            relationship = DEFAULT_RELATIONSHIP
        await check_and_increment_daily_quota(user["user_id"], plan="free", quota_bucket=INVITE_BUCKET)

        invitee_id = await db.fetchval("select user_id from users where email = $1", invitee_email)
        if invitee_id and await db.fetchval("select 1 from family_links where a = $1 and b = $2", user["user_id"], invitee_id):
            return INVITE_SENT
        if await db.fetchval("select 1 from family_invites where inviter_id = $1 and invitee_email = $2 and status = $3",
                             user["user_id"], invitee_email, STATUS_PENDING):
            return INVITE_SENT

        invite_id = uuid.uuid4().hex
        inviter_name = user.get("name") or user.get("username") or "Someone"
        await db.execute(
            """insert into family_invites (id, inviter_id, inviter_name, invitee_email, invitee_user_id,
                                           relationship, status, created_at)
               values ($1, $2, $3, $4, $5, $6, $7, $8)""",
            invite_id, user["user_id"], inviter_name, invitee_email, invitee_id, relationship, STATUS_PENDING, _now())
        try:
            if email_id := send_family_invite(inviter_name, invitee_email, relationship):
                await db.execute("update family_invites set resend_email_id = $1 where id = $2", email_id, invite_id)
        except Exception as e:
            logger.warning("invite email failed (invite still created): %s", e)
        return INVITE_SENT

    @staticmethod
    async def outgoing(user_id: str) -> list[dict]:
        docs = await db.fetch(
            "select * from family_invites where inviter_id = $1 and status = $2 order by created_at desc limit $3",
            user_id, STATUS_PENDING, INVITE_LIST_LIMIT)
        return [{"id": d["id"], "email": d["invitee_email"], "relationship": d.get("relationship", DEFAULT_RELATIONSHIP),
                 "created_at": d["created_at"]} for d in docs]

    @staticmethod
    async def cancel(invite_id: str, user_id: str) -> None:
        deleted = await db.fetchval(
            "delete from family_invites where id = $1 and inviter_id = $2 and status = $3 returning 1",
            invite_id, user_id, STATUS_PENDING)
        if not deleted:
            raise HTTPException(status_code=404, detail=MSG_INVITE_NOT_FOUND)

    # ------------------------------------------------------------ incoming invites

    @staticmethod
    async def incoming(user: dict) -> list[dict]:
        """One query for the invites, one for every inviter row - not one per invite."""
        docs = await db.fetch(
            """select * from family_invites where status = $1 and invitee_email = $2
               and (invitee_user_id = $3 or invitee_user_id is null) order by created_at desc limit $4""",
            STATUS_PENDING, user["email"].lower().strip(), user["user_id"], INVITE_LIST_LIMIT)
        ids = list({d["inviter_id"] for d in docs})
        rows = await db.fetch("select user_id, name, username, email, picture from users where user_id = any($1::text[])", ids) if ids else []
        inviters = {r["user_id"]: r for r in rows}
        out = []
        for d in docs:
            inv = inviters.get(d["inviter_id"])
            if inv:
                out.append({"id": d["id"], "inviter_name": _display(inv), "inviter_email": inv.get("email"),
                            "inviter_picture": inv.get("picture"),
                            "relationship": d.get("relationship", DEFAULT_RELATIONSHIP), "created_at": d["created_at"]})
        return out

    @staticmethod
    async def _mine(invite_id: str, user: dict) -> dict:
        """The pending invite if it is addressed to this caller, else 404. Bound invites need
        an exact user_id match; unbound ones fall back to email."""
        inv = await db.fetchrow("select * from family_invites where id = $1 and status = $2", invite_id, STATUS_PENDING)
        if not inv:
            raise HTTPException(status_code=404, detail=MSG_INVITE_NOT_FOUND)
        bound = inv.get("invitee_user_id")
        if bound is not None and bound != user["user_id"]:
            raise HTTPException(status_code=404, detail=MSG_INVITE_NOT_FOUND)
        if bound is None and (inv.get("invitee_email") or "").lower().strip() != user["email"].lower().strip():
            raise HTTPException(status_code=404, detail=MSG_INVITE_NOT_FOUND)
        return inv

    async def accept(self, invite_id: str, user: dict) -> dict:
        inv = await self._mine(invite_id, user)
        if inv["inviter_id"] == user["user_id"]:
            raise HTTPException(status_code=400, detail=MSG_OWN_INVITE)
        now, rel = _now(), inv.get("relationship", DEFAULT_RELATIONSHIP)
        await db.execute("update family_invites set status = $1, invitee_user_id = $2, accepted_at = $3 where id = $4",
                         STATUS_ACCEPTED, user["user_id"], now, inv["id"])
        upsert = """insert into family_links (a, b, relationship, created_at) values ($1, $2, $3, $4)
                    on conflict (a, b) do update set relationship = $3, created_at = $4"""
        await db.execute(upsert, inv["inviter_id"], user["user_id"], rel, now)
        await db.execute(upsert, user["user_id"], inv["inviter_id"], rel, now)
        return {"status": STATUS_ACCEPTED, "member": await self.member_summary(inv["inviter_id"])}

    async def decline(self, invite_id: str, user: dict) -> dict:
        inv = await self._mine(invite_id, user)
        await db.execute("update family_invites set status = $1, declined_at = $2 where id = $3", STATUS_DECLINED, _now(), inv["id"])
        return {"status": STATUS_DECLINED}


family = FamilyService()
