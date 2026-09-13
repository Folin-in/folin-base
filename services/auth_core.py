"""Authentication: email/password, rotating refresh tokens, Google / Apple / Supabase
identities, and password reset.

Invariants worth knowing before touching this:
- A refresh token is single-use. Reuse of a consumed or revoked one revokes its whole
  family - that is the stolen-token defence, and the consume is an atomic UPDATE so a
  concurrent double-spend loses the race and trips it.
- Social sign-in binds by the provider's `sub` first, email second. Both can match
  different rows at once (the sub's owner changed their email; a stranger later
  registered with the freed address), so the sub wins the tie-break and an existing
  link is never repointed at a new sub.
- Forgot-password answers 200 whether or not the address exists, including when the
  per-user cap is hit. Anything else is an enumeration oracle.
- Every credential change (reset, password change, email change) bumps token_version
  AND revokes refresh tokens; a stolen refresh token must not survive it.
"""
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from fastapi import HTTPException, Request

from app.config import settings
from app.constants import (APPLE_ISSUER, APPLE_JWKS_URL, GOOGLE_ISSUERS, GOOGLE_JWKS_URL, RESET_CODE_DIGITS,
                           RESET_CODE_MAX, RESET_CODE_TTL_MIN, SOCIAL_TOKEN_ALGS, USER_ID_HEX, USERNAME_FALLBACK,
                           USERNAME_MAX_CHARS, USERNAME_PATTERN, RateLimit)
from app.enums import AuthProvider
from app.models import (AppleLoginRequest, ForgotPasswordRequest, GoogleLoginRequest, LoginRequest,
                        RefreshRequest, RegisterRequest, ResetPasswordRequest, SupabaseSyncRequest,
                        default_preferences, public_user)
from core import cache
from core.email import send_password_reset, smtp_enabled
from core.rate_limit import check_and_increment_daily_quota, check_ip_rate_limit
from core.security import (create_access_token, decode_supabase_token, hash_password, hash_refresh,
                           issue_refresh_token, revoke_all_refresh_tokens, revoke_refresh_family,
                           verify_password)
from db import session as db
from db.sql import SOCIAL_ID_COLUMNS, USERS_UPDATABLE, assert_columns, set_clause

logger = logging.getLogger(__name__)

_USERNAME_STRIP = re.compile(USERNAME_PATTERN)
_INSERT_COLS = USERS_UPDATABLE | {"auth_provider", "token_version", "created_at"}
PASSWORD_RESET_BUCKET = "password_reset_email"

MSG_CONSENT = "Please confirm you're 18+ and consent to health-data processing to create an account."
MSG_EMAIL_TAKEN = "Email already registered"
MSG_USERNAME_TAKEN = "Username already taken"
MSG_BAD_CREDS = "Incorrect email/username or password"
MSG_USE_SOCIAL = "This account uses {provider} login. Use that button instead."
MSG_BAD_REFRESH = "Invalid refresh token"
MSG_REFRESH_REUSED = "Refresh token expired or reused. Please sign in again."
MSG_RESET_OFF = "Password reset is not available right now."
MSG_BAD_CODE = "Invalid or expired code."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _limit(request: Request, bucket: tuple[str, int, int]) -> None:
    name, hits, window = bucket
    check_ip_rate_limit(request, bucket=name, max_hits=hits, window_seconds=window)


async def unique_username(base: str) -> str:
    base = _USERNAME_STRIP.sub("", base)[:USERNAME_MAX_CHARS] or USERNAME_FALLBACK
    candidate, i = base, 0
    while await db.fetchval("select 1 from users where username = $1", candidate):
        i += 1
        candidate = f"{base}{i}"
    return candidate

async def insert_user(user: dict) -> None:
    cols = list(user.keys())
    assert_columns([c for c in cols if c != "user_id"], _INSERT_COLS, "users")
    await db.execute(
        f"insert into users ({', '.join(cols)}) values ({', '.join(f'${i + 1}' for i in range(len(cols)))})",
        *user.values())


class AuthCore:
    # ------------------------------------------------------------ token pairs

    @staticmethod
    async def session(user: dict, family_id: str | None = None) -> dict:
        if user.get("is_blocked") or (user.get("preferences") or {}).get("is_blocked"):
            raise HTTPException(status_code=403, detail="Account suspended. Please contact support.")
        access = create_access_token(user["user_id"], user.get("token_version", 0))
        refresh = await issue_refresh_token(user["user_id"], family_id=family_id)
        return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "user": public_user(user)}

    # ------------------------------------------------------------ email/password

    async def register(self, req: RegisterRequest, request: Request) -> dict:
        _limit(request, RateLimit.REGISTER)
        if not req.consent:
            raise HTTPException(status_code=400, detail=MSG_CONSENT)
        email, username = req.email.lower().strip(), req.username.lower().strip()
        if await db.fetchval("select 1 from users where email = $1", email):
            raise HTTPException(status_code=400, detail=MSG_EMAIL_TAKEN)
        if await db.fetchval("select 1 from users where username = $1", username):
            raise HTTPException(status_code=400, detail=MSG_USERNAME_TAKEN)
        now = _now().isoformat()
        user = {
            "user_id": f"user_{uuid.uuid4().hex[:USER_ID_HEX]}", "email": email, "username": username,
            "name": req.name.strip() or username, "password_hash": hash_password(req.password),
            "auth_provider": str(AuthProvider.LOCAL), "picture": None, "health_profile": {},
            "preferences": default_preferences(), "token_version": 0, "created_at": now,
            "consented_at": now, "consent_version": settings.consent_version,
        }
        await insert_user(user)
        return await self.session(user)

    async def login(self, req: LoginRequest, request: Request) -> dict:
        _limit(request, RateLimit.LOGIN)
        identifier = req.identifier.lower().strip()
        user = await db.fetchrow("select * from users where email = $1 or username = $1", identifier)
        if not user:
            raise HTTPException(status_code=401, detail=MSG_BAD_CREDS)
        if not user.get("password_hash"):
            provider = "Google" if user.get("google_id") else "Apple" if user.get("apple_id") else "social"
            raise HTTPException(status_code=400, detail=MSG_USE_SOCIAL.format(provider=provider))
        if not verify_password(req.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail=MSG_BAD_CREDS)
        return await self.session(user)

    async def refresh(self, req: RefreshRequest, request: Request) -> dict:
        _limit(request, RateLimit.REFRESH)
        now = _now()
        token_hash = hash_refresh(req.refresh_token.strip())
        doc = await db.fetchrow("select * from refresh_tokens where token_hash = $1", token_hash)
        if not doc:
            raise HTTPException(status_code=401, detail=MSG_BAD_REFRESH)
        if doc.get("used") or doc.get("revoked_at") or (doc["expires_at"] and doc["expires_at"] <= now):
            await revoke_refresh_family(doc["family_id"])
            raise HTTPException(status_code=401, detail=MSG_REFRESH_REUSED)
        consumed = await db.fetchval(
            "update refresh_tokens set used = true where token_hash = $1 and used = false and revoked_at is null returning 1",
            token_hash)
        if not consumed:
            await revoke_refresh_family(doc["family_id"])
            raise HTTPException(status_code=401, detail=MSG_REFRESH_REUSED)
        user = await db.fetchrow("select * from users where user_id = $1", doc["user_id"])
        if not user:
            raise HTTPException(status_code=401, detail=MSG_BAD_REFRESH)
        return await self.session(user, family_id=doc["family_id"])

    @staticmethod
    async def logout(user: dict) -> None:
        await db.execute("update users set token_version = token_version + 1 where user_id = $1", user["user_id"])
        # Logout revokes every session for this account, so its device notification
        # registrations must also stop. This prevents reminders appearing on a shared
        # phone after its owner has signed out.
        await db.execute("update push_tokens set active = false, updated_at = now() where user_id = $1",
                         user["user_id"])
        cache.invalidate(user["user_id"])
        await revoke_all_refresh_tokens(user["user_id"])

    # ------------------------------------------------------------ password reset

    @staticmethod
    def reset_code() -> str:
        return f"{secrets.randbelow(RESET_CODE_MAX):0{RESET_CODE_DIGITS}d}"

    async def forgot_password(self, req: ForgotPasswordRequest, request: Request) -> None:
        if not smtp_enabled():
            raise HTTPException(status_code=503, detail=MSG_RESET_OFF)
        _limit(request, RateLimit.FORGOT_PASSWORD)
        email = req.email.lower().strip()
        user = await db.fetchrow("select user_id, email, password_hash, auth_provider from users where email = $1", email)
        if not (user and user.get("password_hash") and (user.get("auth_provider") or AuthProvider.LOCAL) == AuthProvider.LOCAL):
            return
        try:
            await check_and_increment_daily_quota(user["user_id"], plan="free", quota_bucket=PASSWORD_RESET_BUCKET)
        except HTTPException as e:
            if e.status_code == 429:
                return
            raise
        code = self.reset_code()
        await db.execute(
            """insert into password_resets (user_id, email, code_hash, expires_at, consumed, created_at)
               values ($1, $2, $3, $4, false, $5)
               on conflict (user_id) do update set email = $2, code_hash = $3, expires_at = $4,
                 consumed = false, created_at = $5, consumed_at = null""",
            user["user_id"], email, hash_password(code), _now() + timedelta(minutes=RESET_CODE_TTL_MIN), _now())
        try:
            send_password_reset(email, code)
        except Exception as e:
            logger.warning("reset-email send failed: %s", e)

    async def reset_password(self, req: ResetPasswordRequest, request: Request) -> None:
        if not smtp_enabled():
            raise HTTPException(status_code=503, detail=MSG_RESET_OFF)
        _limit(request, RateLimit.RESET_PASSWORD)
        user = await db.fetchrow("select * from users where email = $1", req.email.lower().strip())
        if not user or not user.get("password_hash"):
            raise HTTPException(status_code=400, detail=MSG_BAD_CODE)
        entry = await db.fetchrow("select * from password_resets where user_id = $1", user["user_id"])
        if (not entry or entry.get("consumed") or (entry["expires_at"] and entry["expires_at"] < _now())
                or not verify_password(req.code.strip(), entry.get("code_hash") or "")):
            raise HTTPException(status_code=400, detail=MSG_BAD_CODE)
        await db.execute("update users set password_hash = $1, token_version = token_version + 1 where user_id = $2",
                         hash_password(req.new_password), user["user_id"])
        cache.invalidate(user["user_id"])
        await revoke_all_refresh_tokens(user["user_id"])
        await db.execute("update password_resets set consumed = true, consumed_at = $1 where user_id = $2",
                         _now(), user["user_id"])


core = AuthCore()
