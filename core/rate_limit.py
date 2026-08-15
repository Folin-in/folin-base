"""Lightweight rate limiting utilities.
- In-memory sliding-window limiter for per-IP anonymous endpoints (login/register).
- Postgres-backed daily quota counter for per-user paid AI endpoints (reports/analyze).
"""
from collections import defaultdict, deque
from datetime import datetime, timezone
from ipaddress import ip_address, ip_network
from time import monotonic
from typing import Deque, Dict, Tuple

from fastapi import HTTPException, Request

from app.config import settings
from db import session as db
from app.enums import Plan
from app.constants import DAILY_QUOTA_FREE_GENERIC, DAILY_QUOTA_OVERRIDES


# ---------- In-memory per-IP sliding window ----------
_WINDOWS: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)


def _peer_is_trusted(peer: str) -> bool:
    """Return True when the immediate connecting peer is on the trusted-proxy allow-list.
    Supports individual IPs and CIDR blocks."""
    if not peer or peer == "unknown":
        return False
    try:
        peer_addr = ip_address(peer)
    except ValueError:
        return False
    for entry in settings.trusted_proxy_ips:
        try:
            if "/" in entry:
                if peer_addr in ip_network(entry, strict=False):
                    return True
            elif peer_addr == ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def _client_ip(request: Request) -> str:
    """HARDENING-1: only honor proxy headers when the direct peer is a trusted proxy.
    Otherwise use the socket peer. Prevents X-Forwarded-For header rotation from defeating
    per-IP throttles on /auth/login and /auth/register."""
    peer = request.client.host if request.client else "unknown"
    if _peer_is_trusted(peer):
        fwd = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
        if fwd:
            # P3 hardening: walk the chain RIGHT-to-LEFT skipping trusted proxies; the first
            # untrusted hop is the real client. (Left-most is attacker-controllable padding.)
            hops = [h.strip() for h in fwd.split(",") if h.strip()]
            for hop in reversed(hops):
                if not _peer_is_trusted(hop):
                    return hop
            # Whole chain trusted - fall back to the left-most entry.
            if hops:
                return hops[0]
        # Note: X-Real-IP is intentionally NOT trusted as a fallback - a single upstream
        # proxy typically forwards it verbatim from the client, making it spoofable.
        # We rely solely on the appended X-Forwarded-For chain (or the socket peer).
    return peer


def check_ip_rate_limit(request: Request, bucket: str, max_hits: int, window_seconds: int) -> None:
    """Raise 429 if the caller's IP has exceeded max_hits within window_seconds for a named bucket."""
    ip = _client_ip(request)
    key = (bucket, ip)
    dq = _WINDOWS[key]
    now = monotonic()
    # Trim old entries
    cutoff = now - window_seconds
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) >= max_hits:
        raise HTTPException(status_code=429, detail="Too many attempts. Please wait a bit and try again.")
    dq.append(now)


# ---------- Postgres-backed daily quota per user ----------
# Analyze quota for FREE users is a lifetime total checked in reports.py, not here.
DAILY_QUOTA_PRO = settings.pro_daily_analyses

# Named per-bucket overrides. Buckets that don't appear here use the plan defaults above.


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def check_and_increment_daily_quota(user_id: str, plan: str, quota_bucket: str = "reports_analyze") -> None:
    """Atomically increment a per-user daily counter and reject if it exceeds the plan quota."""
    limit = DAILY_QUOTA_OVERRIDES.get(quota_bucket)
    if limit is None:
        limit = DAILY_QUOTA_PRO if plan == Plan.PRO else DAILY_QUOTA_FREE_GENERIC
    day_key = _today_key()
    doc_id = f"{quota_bucket}:{user_id}:{day_key}"

    count = await db.fetchval(
        """insert into quota_counters (id, user_id, day, bucket, count) values ($1, $2, $3, $4, 1)
           on conflict (id) do update set count = quota_counters.count + 1
           returning count""",
        doc_id, user_id, day_key, quota_bucket,
    )
    if count > limit:
        # Roll back the increment to avoid stuck counters after a spike
        await db.execute("update quota_counters set count = count - 1 where id = $1", doc_id)
        if quota_bucket == "family_invite_daily":
            raise HTTPException(
                status_code=429,
                detail=f"You've sent {limit} invites today. Try again tomorrow.",
            )
        raise HTTPException(
            status_code=429,
            detail=f"Daily analysis quota reached ({limit} per day on the {plan} plan). Try again tomorrow.",
        )


async def refund_daily_quota(user_id: str, quota_bucket: str = "reports_analyze") -> None:
    """Refund one slot from today's counter when a call didn't actually consume value
    (e.g. AI classified the upload as a non-medical document)."""
    day_key = _today_key()
    doc_id = f"{quota_bucket}:{user_id}:{day_key}"
    await db.execute("update quota_counters set count = count - 1 where id = $1 and count > 0", doc_id)
