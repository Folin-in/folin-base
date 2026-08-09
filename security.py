import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
import jwt as pyjwt
from fastapi import HTTPException, Request

import cache
import config
import database as db

logger = logging.getLogger(__name__)


# ---------- Password hashing ----------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


# ---------- JWT ----------
def create_access_token(user_id: str, token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "type": "access",
        "tv": int(token_version),  # bumped on logout / password change to revoke old tokens
        "iat": now,
        "exp": now + timedelta(minutes=config.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


# ---------- Refresh tokens (opaque, hashed at rest, single-use rotation) ----------
def new_refresh_token() -> str:
    import secrets as _secrets
    return _secrets.token_urlsafe(48)


def hash_refresh(raw: str) -> str:
    import hashlib as _hashlib
    return _hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def issue_refresh_token(user_id: str, family_id: Optional[str] = None) -> str:
    """Create + store a new refresh token; returns the RAW value (only shown once)."""
    import secrets as _secrets
    raw = new_refresh_token()
    now = datetime.now(timezone.utc)
    await db.execute(
        """insert into refresh_tokens (token_hash, user_id, family_id, used, revoked_at, created_at, expires_at)
           values ($1, $2, $3, false, null, $4, $5)""",
        hash_refresh(raw), user_id, family_id or _secrets.token_hex(16),
        now, now + timedelta(days=config.REFRESH_TOKEN_DAYS),
    )
    return raw


async def revoke_refresh_family(family_id: str) -> None:
    await db.execute(
        "update refresh_tokens set used = true, revoked_at = $1 where family_id = $2",
        datetime.now(timezone.utc), family_id,
    )


async def revoke_all_refresh_tokens(user_id: str) -> None:
    await db.execute(
        "update refresh_tokens set used = true, revoked_at = $1 where user_id = $2 and revoked_at is null",
        datetime.now(timezone.utc), user_id,
    )


# ---------- Pro-term expiry ----------
async def _expire_pro_if_due(user: dict) -> dict:
    """Pro runs 90 days. Enforced here because every request flows through this - no cron
    needed; the account downgrades on its first request after the term ends."""
    prefs = user.get("preferences") or {}
    expires = prefs.get("plan_expires_at")
    if prefs.get("plan") != "pro" or not expires:
        return user
    now_iso = datetime.now(timezone.utc).isoformat()
    if expires > now_iso:
        return user
    merged = {**prefs, "plan": "free", "plan_changed_at": now_iso, "plan_expires_at": None}
    await db.execute("update users set preferences = $1 where user_id = $2", merged, user["user_id"])
    await db.execute(
        """insert into plan_history (user_id, action, plan, amount_inr, starts_at, expires_at, created_at)
           values ($1, 'expired', 'free', null, $2, null, $2)""",
        user["user_id"], now_iso,
    )
    cache.invalidate(user["user_id"])
    return {**user, "preferences": merged}


# ---------- Auth dependency (JWT) ----------
# Supabase signs its access tokens with a rotating asymmetric key (ES256) published at
# the project's JWKS endpoint, so verification needs no shared secret. PyJWKClient caches
# the fetched keys, so this costs one HTTPS call per process.
_SUPABASE_JWKS = pyjwt.PyJWKClient(f"{config.SUPABASE_URL}/auth/v1/.well-known/jwks.json") \
    if config.SUPABASE_URL else None


def decode_supabase_token(token: str) -> Optional[dict]:
    """Verify a Supabase Auth access token. Returns claims, or None if it isn't one."""
    if not _SUPABASE_JWKS:
        return None
    try:
        key = _SUPABASE_JWKS.get_signing_key_from_jwt(token).key
        claims = pyjwt.decode(
            token, key, algorithms=["ES256", "RS256"], audience="authenticated",
            issuer=f"{config.SUPABASE_URL}/auth/v1",
        )
    except Exception:
        return None
    return claims if claims.get("sub") else None


async def _user_from_supabase(claims: dict) -> dict:
    """Resolve a Supabase identity to the Folin row that owns the person's data.

    Links by email on first sight so an account created before the migration keeps its
    reports, family and plan instead of silently starting over.
    """
    import uuid as _uuid
    try:
        uid = str(_uuid.UUID(str(claims["sub"])))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in. Please try again.")
    user = await db.fetchrow("select * from users where supabase_uid = $1", uid)
    if user:
        return user
    email = (claims.get("email") or "").lower().strip()
    if not email:
        raise HTTPException(status_code=401, detail="That sign-in has no email address.")
    user = await db.fetchrow("select * from users where lower(email) = $1", email)
    if not user:
        raise HTTPException(status_code=401, detail="No Folin account for that sign-in.")
    if user.get("supabase_uid"):
        # Already claimed by a different Supabase identity - refuse rather than repoint.
        raise HTTPException(status_code=409, detail="That email is already linked to another sign-in.")
    await db.execute("update users set supabase_uid = $1 where user_id = $2", uid, user["user_id"])
    cache.invalidate(user["user_id"])
    return await db.fetchrow("select * from users where user_id = $1", user["user_id"])


async def get_current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_token(token)
    if payload and payload.get("sub"):
        user = cache.get_user(payload["sub"])
        if user is None:
            user = await db.fetchrow("select * from users where user_id = $1", payload["sub"])
            if user:
                cache.set_user(payload["sub"], user)
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        # Reject tokens whose version is older than the user's current one (logout/password change)
        current_tv = int(user.get("token_version") or 0)
        token_tv = int(payload.get("tv") or 0)
        if token_tv < current_tv:
            raise HTTPException(status_code=401, detail="Token revoked. Please sign in again.")
        return await _expire_pro_if_due(user)

    # Not one of ours - try Supabase Auth. Both are accepted during the migration so
    # existing sessions keep working while clients move over.
    claims = decode_supabase_token(token)
    if claims:
        return await _expire_pro_if_due(await _user_from_supabase(claims))

    raise HTTPException(status_code=401, detail="Invalid or expired token")
