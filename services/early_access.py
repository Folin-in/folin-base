"""Public website early-access signup with double opt-in and durable abuse limits."""
import hashlib
import hmac
import ipaddress
import logging
import math
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request

from app.config import settings
from app.models import EarlyAccessRequest
from core.email import send_early_access_confirmation, verify_resend_webhook
from core.disposable_email import is_disposable_email
from core.rate_limit import _client_ip
from db import session as db

logger = logging.getLogger(__name__)

TURNSTILE_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
GENERIC_RESPONSE = {"status": "check_your_email"}
CAPTCHA_ERROR = "We could not verify this request. Please refresh and try again."
UNAVAILABLE_ERROR = "Early access signup is temporarily unavailable. Please try again later."


def _require_signup_config() -> None:
    if not all((settings.resend_api_key, settings.early_access_from,
                settings.early_access_confirm_url, settings.early_access_web_url,
                settings.turnstile_secret_key)):
        raise HTTPException(status_code=503, detail=UNAVAILABLE_ERROR)


def _ip_key(request: Request, now: datetime) -> str:
    client = _client_ip(request)
    try:
        address = ipaddress.ip_address(client)
        if isinstance(address, ipaddress.IPv6Address):
            # IPv6 privacy addresses within one /64 must share the same budget.
            client = str(address.ipv4_mapped or ipaddress.ip_network(f"{address}/64", strict=False))
        else:
            client = str(address)
    except ValueError:
        client = "unknown"
    digest = hmac.new(
        settings.jwt_secret.encode(), client.encode(), hashlib.sha256,
    ).hexdigest()
    return f"ip-hour:{now.strftime('%Y-%m-%dT%H')}:{digest}"


def _burst_key(request: Request, now: datetime) -> str:
    return f"ip-minute:{now.minute}:{_ip_key(request, now)}"


def _limit_error(detail: str, expires_at: datetime) -> HTTPException:
    seconds = max(1, math.ceil((expires_at - datetime.now(timezone.utc)).total_seconds()))
    return HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(seconds)})


def _global_quota_key(now: datetime) -> str:
    return f"global-day:{now.strftime('%Y-%m-%d')}"


async def _consume_limit(key: str, limit: int, expires_at: datetime) -> None:
    count = await db.fetchval(
        """insert into early_access_limits (key, count, expires_at) values ($1, 1, $2)
           on conflict (key) do update set
             count = case when early_access_limits.expires_at <= now() then 1
                          else least(early_access_limits.count + 1, $3) end,
             expires_at = case when early_access_limits.expires_at <= now() then excluded.expires_at
                               else early_access_limits.expires_at end
           returning count""",
        key, expires_at, limit + 1,
    )
    if count > limit:
        raise _limit_error("Too many attempts. Please wait and try again.", expires_at)


async def _verify_turnstile(token: str, request: Request) -> None:
    payload = {
        "secret": settings.turnstile_secret_key,
        "response": token,
        "remoteip": _client_ip(request),
        "idempotency_key": str(uuid.uuid4()),
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(TURNSTILE_SITEVERIFY_URL, json=payload)
            response.raise_for_status()
            result = response.json()
    except Exception as exc:
        logger.warning("Turnstile verification request failed: %s", exc)
        raise HTTPException(status_code=503, detail=UNAVAILABLE_ERROR) from None

    valid = result.get("success") is True
    if settings.turnstile_expected_hostname:
        valid = valid and result.get("hostname") == settings.turnstile_expected_hostname
    if settings.turnstile_expected_action:
        valid = valid and result.get("action") == settings.turnstile_expected_action
    if not valid:
        logger.info("Turnstile rejected an early-access submission")
        raise HTTPException(status_code=400, detail=CAPTCHA_ERROR)


async def _claim_confirmation(email: str, token_hash: str, now: datetime) -> dict | None:
    cooldown_before = now - timedelta(minutes=settings.early_access_confirmation_cooldown_minutes)
    expires_at = now + timedelta(hours=settings.early_access_confirmation_expire_hours)
    quota_key = _global_quota_key(now)
    quota_expires = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    async with db.transaction() as conn:
        # Serialize concurrent submissions for the same normalized address.
        await conn.execute("select pg_advisory_xact_lock(hashtextextended($1, 0))", email)
        existing = await conn.fetchrow(
            """select id, status, confirmation_token_hash, confirmation_expires_at,
                      confirmation_sent_at, resend_email_id, last_delivery_status
               from early_access_signups where email = $1 for update""",
            email,
        )
        if existing and (
            existing["status"] != "pending"
            or (existing["confirmation_sent_at"] and existing["confirmation_sent_at"] > cooldown_before)
        ):
            return None

        count = await conn.fetchval(
            """insert into early_access_limits (key, count, expires_at) values ($1, 1, $2)
               on conflict (key) do update set
                 count = case when early_access_limits.expires_at <= now() then 1
                              else early_access_limits.count + 1 end,
                 expires_at = case when early_access_limits.expires_at <= now() then excluded.expires_at
                                   else early_access_limits.expires_at end
               returning count""",
            quota_key, quota_expires,
        )
        if count > settings.early_access_daily_limit:
            raise _limit_error("Early-access requests are full for today. Try again tomorrow.", quota_expires)

        if existing:
            await conn.execute(
                """update early_access_signups set confirmation_token_hash = $1,
                     confirmation_expires_at = $2, confirmation_sent_at = $3,
                     resend_email_id = null, last_delivery_status = null, updated_at = $3
                   where id = $4""",
                token_hash, expires_at, now, existing["id"],
            )
            return {
                "id": existing["id"], "created": False, "quota_key": quota_key,
                "old_token_hash": existing["confirmation_token_hash"],
                "old_expires_at": existing["confirmation_expires_at"],
                "old_sent_at": existing["confirmation_sent_at"],
                "old_resend_email_id": existing["resend_email_id"],
                "old_delivery_status": existing["last_delivery_status"],
            }

        signup_id = await conn.fetchval(
            """insert into early_access_signups
               (email, status, confirmation_token_hash, confirmation_expires_at,
                confirmation_sent_at, created_at, updated_at)
               values ($1, 'pending', $2, $3, $4, $4, $4) returning id""",
            email, token_hash, expires_at, now,
        )
        return {"id": signup_id, "created": True, "quota_key": quota_key}


async def _release_failed_claim(claim: dict) -> None:
    async with db.transaction() as conn:
        if claim["created"]:
            await conn.execute(
                "delete from early_access_signups where id = $1 and status = 'pending' and resend_email_id is null",
                claim["id"],
            )
        else:
            await conn.execute(
                """update early_access_signups set confirmation_token_hash = $1,
                     confirmation_expires_at = $2, confirmation_sent_at = $3,
                     resend_email_id = $4, last_delivery_status = $5, updated_at = now()
                   where id = $6 and status = 'pending' and resend_email_id is null""",
                claim["old_token_hash"], claim["old_expires_at"], claim["old_sent_at"],
                claim["old_resend_email_id"], claim["old_delivery_status"], claim["id"],
            )
        await conn.execute(
            "update early_access_limits set count = greatest(count - 1, 0) where key = $1",
            claim["quota_key"],
        )


class EarlyAccessService:
    async def signup(self, req: EarlyAccessRequest, request: Request) -> dict:
        # Honeypot submissions get the same response but consume no provider or DB work.
        if req.website:
            return GENERIC_RESPONSE
        _require_signup_config()
        now = datetime.now(timezone.utc)
        minute_end = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        hour_end = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        await _consume_limit(_burst_key(request, now), settings.early_access_ip_minute_limit, minute_end)
        await _consume_limit(_ip_key(request, now), settings.early_access_ip_hourly_limit, hour_end)

        email = str(req.email).lower().strip()
        if is_disposable_email(email):
            raise HTTPException(status_code=400, detail="Please use a permanent email address.")
        await _verify_turnstile(req.turnstile_token, request)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        claim = await _claim_confirmation(email, token_hash, now)
        if not claim:
            return GENERIC_RESPONSE

        separator = "&" if "?" in settings.early_access_confirm_url else "?"
        confirm_url = f"{settings.early_access_confirm_url}{separator}{urlencode({'token': token})}"
        email_id = await send_early_access_confirmation(email, confirm_url)
        if not email_id:
            await _release_failed_claim(claim)
            raise HTTPException(status_code=503, detail=UNAVAILABLE_ERROR)
        await db.execute(
            """update early_access_signups set resend_email_id = $1,
                 last_delivery_status = 'accepted', updated_at = now() where id = $2""",
            email_id, claim["id"],
        )
        return GENERIC_RESPONSE

    @staticmethod
    async def confirm(token: str) -> bool:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        signup_id = await db.fetchval(
            """update early_access_signups set status = 'confirmed', confirmed_at = now(),
                 confirmation_token_hash = null, confirmation_expires_at = null, updated_at = now()
               where confirmation_token_hash = $1 and status = 'pending'
                 and confirmation_expires_at > now() returning id""",
            token_hash,
        )
        return signup_id is not None

    @staticmethod
    async def webhook(payload: str, headers: dict[str, str]) -> None:
        try:
            event = verify_resend_webhook(payload, headers)
        except RuntimeError:
            raise HTTPException(status_code=503, detail="Webhook is not configured") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid webhook signature") from None

        event_type = event.get("type", "")
        data = event.get("data") or {}
        email_id = data.get("email_id")
        if not email_id or not event_type.startswith("email."):
            return
        delivery_status = event_type.removeprefix("email.")
        suppressed_status = {
            "email.bounced": "bounced",
            "email.complained": "complained",
            "email.suppressed": "bounced",
        }.get(event_type)
        if suppressed_status:
            await db.execute(
                """update early_access_signups set status = $1, last_delivery_status = $2,
                     updated_at = now() where resend_email_id = $3""",
                suppressed_status, delivery_status, email_id,
            )
        else:
            await db.execute(
                """update early_access_signups set last_delivery_status = $1,
                     updated_at = now() where resend_email_id = $2""",
                delivery_status, email_id,
            )


early_access = EarlyAccessService()
