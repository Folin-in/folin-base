import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
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


# ---------- Auth dependency (JWT) ----------
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
        return user

    raise HTTPException(status_code=401, detail="Invalid or expired token")
