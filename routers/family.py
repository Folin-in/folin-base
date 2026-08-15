"""Family sharing routes - invite → invitee accepts → two-way link (score-only, no raw markers)."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

import cache
from db import session as db
from email_service import send_family_invite
from gamification import compute_profile
from models import FamilyInviteRequest, RELATIONSHIPS
from rate_limit import check_and_increment_daily_quota
from routers.users import fetch_events
from security import get_current_user

router = APIRouter(prefix="/family", tags=["family"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _member_summary(user_id: str) -> dict:
    """Return a share-safe summary - only score, trend and metadata, never raw markers."""
    user = await db.fetchrow("select * from users where user_id = $1", user_id)
    if not user:
        return {}
    # ONLY the account holder's own reports. Including sub-profiles made a user's
    # shared score jump to whatever they last uploaded for a family member.
    rows = [r for r in await cache.get_reports(user_id) if r.get("member_id", "self") == "self"]
    # Score + trend come from LAB reports only, matching the profile tab. A
    # prescription photo scores ~65 by heuristic and used to become the score
    # your family sees (66 here vs 81 on your own profile).
    docs = sorted([r for r in rows if (r.get("report_type") or "lab") == "lab"],
                  key=lambda r: r.get("uploaded_at") or "", reverse=True)[:20]
    # Same inputs as the profile tab - omitting events made XP/badges diverge
    # between the family view and the member's own profile screen.
    profile = compute_profile(rows, events=await fetch_events(user_id))
    latest = docs[0] if docs else None
    return {
        "user_id": user_id,
        "email": user["email"],
        "name": user.get("name") or user.get("username") or user["email"].split("@")[0],
        "picture": user.get("picture"),
        "score": latest.get("health_score") if latest else None,
        "score_trend": [d.get("health_score", 0) for d in reversed(docs)],
        "level": profile["level"],
        "reports_count": profile["reports_count"],
        "updated_at": latest.get("report_date") or (latest.get("uploaded_at") or "")[:10] if latest else None,
    }


# NOTE: intentionally NO auto-accept-on-register hook - SEC-001/SEC-002 fix.
# Invitees must explicitly accept a pending invite in-app.


@router.post("/invite")
async def invite_family(req: FamilyInviteRequest, current_user: dict = Depends(get_current_user)):
    """Create a pending invite. Always uniform response - never leaks whether the target email
    has an existing account (defeats enumeration). Two-way link is only created after the
    invitee explicitly accepts via /family/incoming-invites/{id}/accept.
    """
    # PRO feature: sending invites requires Pro. Accepting a Pro user's invite stays free,
    # otherwise invites would only ever connect Pro users to Pro users.
    plan = (current_user.get("preferences") or {}).get("plan") or "free"
    if plan != "pro":
        raise HTTPException(status_code=403, detail="Inviting family members is a Pro feature. Upgrade to unlock.")
    invitee_email = req.email.lower().strip()
    if invitee_email == current_user["email"].lower():
        raise HTTPException(status_code=400, detail="You can't invite yourself.")
    relationship = req.relationship.lower().strip() if req.relationship else "family"
    if relationship not in RELATIONSHIPS:
        relationship = "family"

    # HARDENING-3: per-user daily invite cap. Prevents email/cost abuse of the Resend account
    # and reduces mass-phishing exposure via our domain. 20 successful sends per day.
    await check_and_increment_daily_quota(current_user["user_id"], plan="free", quota_bucket="family_invite_daily")

    # Uniform check: if already linked, silently succeed (don't leak that they exist and are linked)
    invitee_id = await db.fetchval("select user_id from users where email = $1", invitee_email)
    if invitee_id:
        already_linked = await db.fetchval(
            "select 1 from family_links where a = $1 and b = $2", current_user["user_id"], invitee_id
        )
        if already_linked:
            return {"status": "invite_sent"}

    # Already have a pending invite? Uniform response, no error leak.
    existing = await db.fetchval(
        "select 1 from family_invites where inviter_id = $1 and invitee_email = $2 and status = 'pending'",
        current_user["user_id"], invitee_email,
    )
    if existing:
        return {"status": "invite_sent"}

    invite_id = uuid.uuid4().hex
    inviter_name = current_user.get("name") or current_user.get("username") or "Someone"
    # SEC-002: bind to the invitee's user_id at invite time when the account already exists.
    # Accept-time will then require user_id match, so a later email change on a different
    # account cannot steal this invite.
    await db.execute(
        """insert into family_invites (id, inviter_id, inviter_name, invitee_email, invitee_user_id,
                                       relationship, status, created_at)
           values ($1, $2, $3, $4, $5, $6, 'pending', $7)""",
        invite_id, current_user["user_id"], inviter_name, invitee_email, invitee_id, relationship, _now(),
    )

    # Fire invite email (best-effort - dormant unless RESEND_API_KEY is set)
    try:
        email_id = send_family_invite(inviter_name, invitee_email, relationship)
        if email_id:
            await db.execute("update family_invites set resend_email_id = $1 where id = $2", email_id, invite_id)
    except Exception:
        pass

    return {"status": "invite_sent"}


# ---------- Outgoing (invites I sent) ----------
@router.get("/invites")
async def list_pending_invites(current_user: dict = Depends(get_current_user)):
    docs = await db.fetch(
        """select * from family_invites where inviter_id = $1 and status = 'pending'
           order by created_at desc limit 50""",
        current_user["user_id"],
    )
    return [
        {"id": d["id"], "email": d["invitee_email"], "relationship": d.get("relationship", "family"), "created_at": d["created_at"]}
        for d in docs
    ]


@router.delete("/invites/{invite_id}")
async def cancel_invite(invite_id: str, current_user: dict = Depends(get_current_user)):
    deleted = await db.fetchval(
        "delete from family_invites where id = $1 and inviter_id = $2 and status = 'pending' returning 1",
        invite_id, current_user["user_id"],
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Invite not found")
    return {"deleted": True}


# ---------- Incoming (invites addressed to me) ----------
@router.get("/incoming-invites")
async def list_incoming_invites(current_user: dict = Depends(get_current_user)):
    """List pending invites addressed to the current user.
    SEC-002: Only include invites whose `invitee_user_id` matches me OR that had no bound
    user id at invite time AND whose email matches mine - this prevents someone who later
    changes their email to a targeted address from seeing invites meant for that address."""
    email = current_user["email"].lower().strip()
    my_id = current_user["user_id"]
    docs = await db.fetch(
        """select * from family_invites
           where status = 'pending' and invitee_email = $1
             and (invitee_user_id = $2 or invitee_user_id is null)
           order by created_at desc limit 50""",
        email, my_id,
    )
    out = []
    for d in docs:
        inviter = await db.fetchrow(
            "select name, username, email, picture from users where user_id = $1", d["inviter_id"]
        )
        if not inviter:
            continue
        out.append({
            "id": d["id"],
            "inviter_name": inviter.get("name") or inviter.get("username") or inviter.get("email", "").split("@")[0],
            "inviter_email": inviter.get("email"),
            "inviter_picture": inviter.get("picture"),
            "relationship": d.get("relationship", "family"),
            "created_at": d["created_at"],
        })
    return out


@router.post("/incoming-invites/{invite_id}/accept")
async def accept_incoming_invite(invite_id: str, current_user: dict = Depends(get_current_user)):
    """SEC-002: When the invite was bound to a specific user_id at creation, require an exact
    match. Otherwise fall back to email match (invitee didn't exist at invite time)."""
    email = current_user["email"].lower().strip()
    my_id = current_user["user_id"]
    inv = await db.fetchrow("select * from family_invites where id = $1 and status = 'pending'", invite_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invite not found")
    bound_uid = inv.get("invitee_user_id")
    if bound_uid is not None:
        if bound_uid != my_id:
            raise HTTPException(status_code=404, detail="Invite not found")
    else:
        # Legacy / not-yet-bound invite - fall back to email match.
        if (inv.get("invitee_email") or "").lower().strip() != email:
            raise HTTPException(status_code=404, detail="Invite not found")
    if inv["inviter_id"] == my_id:
        raise HTTPException(status_code=400, detail="You can't accept your own invite.")

    now = _now()
    relationship = inv.get("relationship", "family")
    await db.execute(
        "update family_invites set status = 'accepted', invitee_user_id = $1, accepted_at = $2 where id = $3",
        my_id, now, inv["id"],
    )
    upsert = """insert into family_links (a, b, relationship, created_at) values ($1, $2, $3, $4)
                on conflict (a, b) do update set relationship = $3, created_at = $4"""
    await db.execute(upsert, inv["inviter_id"], my_id, relationship, now)
    await db.execute(upsert, my_id, inv["inviter_id"], relationship, now)
    return {"status": "accepted", "member": await _member_summary(inv["inviter_id"])}


@router.post("/incoming-invites/{invite_id}/decline")
async def decline_incoming_invite(invite_id: str, current_user: dict = Depends(get_current_user)):
    """SEC-002 mirror of accept: decline only if the invite belongs to me (by bound user_id or
    email fallback for legacy invites)."""
    email = current_user["email"].lower().strip()
    my_id = current_user["user_id"]
    inv = await db.fetchrow("select * from family_invites where id = $1 and status = 'pending'", invite_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invite not found")
    bound_uid = inv.get("invitee_user_id")
    if bound_uid is not None and bound_uid != my_id:
        raise HTTPException(status_code=404, detail="Invite not found")
    if bound_uid is None and (inv.get("invitee_email") or "").lower().strip() != email:
        raise HTTPException(status_code=404, detail="Invite not found")
    await db.execute(
        "update family_invites set status = 'declined', declined_at = $1 where id = $2", _now(), inv["id"]
    )
    return {"status": "declined"}


# ---------- Members ----------
@router.get("/members")
async def list_family_members(current_user: dict = Depends(get_current_user)):
    links = await db.fetch("select * from family_links where a = $1 limit 200", current_user["user_id"])
    members = []
    for link in links:
        s = await _member_summary(link["b"])
        if s:
            s["relationship"] = link.get("relationship", "family")
            members.append(s)
    me = await _member_summary(current_user["user_id"])
    me["relationship"] = "you"
    return {"self": me, "members": members}


@router.delete("/members/{user_id}")
async def remove_member(user_id: str, current_user: dict = Depends(get_current_user)):
    await db.execute(
        "delete from family_links where (a = $1 and b = $2) or (a = $2 and b = $1)",
        current_user["user_id"], user_id,
    )
    return {"removed": True}
