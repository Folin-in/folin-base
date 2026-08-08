"""Authentication routes: JWT email/password with rotating refresh tokens, password reset,
and social login (Google / Apple) via provider ID-token verification against their JWKS."""
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import cache
import config
import database as db
from email_service import send_password_reset, smtp_enabled
from models import (ForgotPasswordRequest, LoginRequest, RegisterRequest,
                    ResetPasswordRequest, default_preferences, public_user)
from rate_limit import check_and_increment_daily_quota, check_ip_rate_limit
from security import (create_access_token, get_current_user, hash_password, hash_refresh,
                      issue_refresh_token, revoke_all_refresh_tokens, revoke_refresh_family,
                      verify_password)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/register")
async def register(req: RegisterRequest, request: Request):
    # SEC-004: block credential-spam
    check_ip_rate_limit(request, bucket="register", max_hits=10, window_seconds=600)
    if not req.consent:
        raise HTTPException(status_code=400, detail="Please confirm you're 18+ and consent to health-data processing to create an account.")
    email = req.email.lower().strip()
    username = req.username.lower().strip()
    if await db.fetchval("select 1 from users where email = $1", email):
        raise HTTPException(status_code=400, detail="Email already registered")
    if await db.fetchval("select 1 from users where username = $1", username):
        raise HTTPException(status_code=400, detail="Username already taken")

    user = {
        "user_id": f"user_{uuid.uuid4().hex[:12]}",
        "email": email,
        "username": username,
        "name": req.name.strip() or username,
        "password_hash": hash_password(req.password),
        "auth_provider": "local",
        "picture": None,
        "health_profile": {},
        "preferences": default_preferences(),
        "token_version": 0,
        "created_at": _now().isoformat(),
        "consented_at": _now().isoformat(),
        "consent_version": config.CONSENT_VERSION,
    }
    await db.execute(
        """insert into users (user_id, email, username, name, password_hash, auth_provider,
                              picture, health_profile, preferences, token_version, created_at,
                              consented_at, consent_version)
           values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
        user["user_id"], user["email"], user["username"], user["name"], user["password_hash"],
        user["auth_provider"], user["picture"], user["health_profile"], user["preferences"],
        user["token_version"], user["created_at"], user["consented_at"], user["consent_version"],
    )

    token = create_access_token(user["user_id"], 0)
    refresh = await issue_refresh_token(user["user_id"])
    return {"access_token": token, "refresh_token": refresh, "token_type": "bearer", "user": public_user(user)}


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    # SEC-004: brute-force guard - 15 attempts per 5 minutes per IP
    check_ip_rate_limit(request, bucket="login", max_hits=15, window_seconds=300)
    identifier = req.identifier.lower().strip()
    user = await db.fetchrow("select * from users where email = $1 or username = $1", identifier)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect email/username or password")
    if not user.get("password_hash"):
        provider = "Google" if user.get("google_id") else "Apple" if user.get("apple_id") else "social"
        raise HTTPException(status_code=400, detail=f"This account uses {provider} login. Use that button instead.")
    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect email/username or password")

    token = create_access_token(user["user_id"], user.get("token_version", 0))
    refresh = await issue_refresh_token(user["user_id"])
    return {"access_token": token, "refresh_token": refresh, "token_type": "bearer", "user": public_user(user)}


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh")
async def refresh_tokens(req: RefreshRequest, request: Request):
    """Rotate a refresh token → new access + refresh pair. Single-use; reuse of a
    consumed/revoked token revokes the whole family (stolen-token defense)."""
    check_ip_rate_limit(request, bucket="refresh", max_hits=30, window_seconds=300)
    now = _now()
    token_hash = hash_refresh(req.refresh_token.strip())
    doc = await db.fetchrow("select * from refresh_tokens where token_hash = $1", token_hash)
    if not doc:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if doc.get("used") or doc.get("revoked_at") or (doc["expires_at"] and doc["expires_at"] <= now):
        # Reuse detected - kill the whole family.
        await revoke_refresh_family(doc["family_id"])
        raise HTTPException(status_code=401, detail="Refresh token expired or reused. Please sign in again.")

    # Atomic single-use consume; a concurrent double-spend loses the race and triggers family revoke.
    consumed = await db.fetchval(
        """update refresh_tokens set used = true
           where token_hash = $1 and used = false and revoked_at is null
           returning 1""",
        token_hash,
    )
    if not consumed:
        await revoke_refresh_family(doc["family_id"])
        raise HTTPException(status_code=401, detail="Refresh token expired or reused. Please sign in again.")

    user = await db.fetchrow("select * from users where user_id = $1", doc["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    access = create_access_token(user["user_id"], user.get("token_version", 0))
    new_refresh = await issue_refresh_token(user["user_id"], family_id=doc["family_id"])
    return {"access_token": access, "refresh_token": new_refresh, "token_type": "bearer", "user": public_user(user)}


@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    return public_user(current_user)


@router.post("/logout")
async def logout(current_user: dict = Depends(get_current_user)):
    """Revoke the current token: bump token_version (rejects outstanding JWTs, SEC-003)
    and revoke every refresh token so no new access tokens can be minted."""
    await db.execute("update users set token_version = token_version + 1 where user_id = $1", current_user["user_id"])
    cache.invalidate(current_user["user_id"])
    await revoke_all_refresh_tokens(current_user["user_id"])
    return {"ok": True}


# ---------- Social login (Google / Apple) ----------
# PyJWKClient caches fetched keys; the first verification per process does one HTTPS fetch.
_GOOGLE_JWKS = pyjwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")
_APPLE_JWKS = pyjwt.PyJWKClient("https://appleid.apple.com/auth/keys")


class GoogleLoginRequest(BaseModel):
    id_token: str


class AppleLoginRequest(BaseModel):
    identity_token: str
    # Apple only provides the name on the FIRST sign-in, client passes it through when present.
    name: str | None = None


async def _unique_username(base: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_\.]", "", base)[:24] or "user"
    candidate = base
    i = 0
    while await db.fetchval("select 1 from users where username = $1", candidate):
        i += 1
        candidate = f"{base}{i}"
    return candidate


async def _social_login(provider: str, provider_sub: str, email: str, name: str | None, picture: str | None):
    """Find-or-create a user for a verified social identity, link by email, return a token pair."""
    id_col = f"{provider}_id"  # google_id | apple_id
    # Two rows can match at once: the account that owns this provider sub, and a different
    # account that happens to hold the same email (e.g. the sub's owner changed their Folin
    # email and a stranger later registered with the freed address). The sub is the identity
    # the provider actually vouched for, so it always wins - without the ORDER BY, Postgres
    # picks arbitrarily and the stranger's account can be handed out.
    user = await db.fetchrow(
        # `is not distinct from` (not `=`) so an unlinked row yields false, not NULL -
        # `desc` defaults to NULLS FIRST and would sort the unlinked row to the top.
        f"""select * from users where {id_col} = $1 or email = $2
            order by ({id_col} is not distinct from $1) desc limit 1""",
        provider_sub, email,
    )
    if user:
        # Never repoint an existing link at a new sub: the row already belongs to someone.
        # Matched-by-email is still a valid login (the provider verified the address), it
        # just doesn't get to claim the link.
        updates = {} if user.get(id_col) else {id_col: provider_sub}
        if not user.get("picture") and picture:
            updates["picture"] = picture
        if not user.get("name") and name:
            updates["name"] = name
        if updates:
            sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(updates))
            await db.execute(f"update users set {sets} where user_id = $1", user["user_id"], *updates.values())
            cache.invalidate(user["user_id"])
            user = await db.fetchrow("select * from users where user_id = $1", user["user_id"])
    else:
        username = await _unique_username(email.split("@")[0])
        user = {
            "user_id": f"user_{uuid.uuid4().hex[:12]}",
            "email": email,
            "username": username,
            "name": (name or "").strip() or username,
            "password_hash": None,
            "auth_provider": provider,
            id_col: provider_sub,
            "picture": picture,
            "health_profile": {},
            "preferences": default_preferences(),
            "token_version": 0,
            "created_at": _now().isoformat(),
        }
        cols = list(user.keys())
        await db.execute(
            f"insert into users ({', '.join(cols)}) values ({', '.join(f'${i + 1}' for i in range(len(cols)))})",
            *user.values(),
        )

    token = create_access_token(user["user_id"], user.get("token_version", 0))
    refresh = await issue_refresh_token(user["user_id"])
    return {"access_token": token, "refresh_token": refresh, "token_type": "bearer", "user": public_user(user)}


@router.post("/google")
async def google_login(req: GoogleLoginRequest, request: Request):
    """Verify a Google ID token (signature via Google JWKS, aud ∈ configured client IDs)."""
    if not config.GOOGLE_CLIENT_IDS:
        raise HTTPException(status_code=503, detail="Google login is not configured.")
    check_ip_rate_limit(request, bucket="social_login", max_hits=20, window_seconds=300)
    try:
        key = _GOOGLE_JWKS.get_signing_key_from_jwt(req.id_token).key
        claims = pyjwt.decode(req.id_token, key, algorithms=["RS256"], audience=config.GOOGLE_CLIENT_IDS)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Google token. Please try again.")
    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise HTTPException(status_code=401, detail="Invalid Google token. Please try again.")
    email = (claims.get("email") or "").lower().strip()
    if not email or not claims.get("email_verified", False):
        raise HTTPException(status_code=401, detail="Your Google account has no verified email.")
    return await _social_login("google", claims["sub"], email, claims.get("name"), claims.get("picture"))


@router.post("/apple")
async def apple_login(req: AppleLoginRequest, request: Request):
    """Verify an Apple identity token (signature via Apple JWKS, aud = app bundle id)."""
    check_ip_rate_limit(request, bucket="social_login", max_hits=20, window_seconds=300)
    try:
        key = _APPLE_JWKS.get_signing_key_from_jwt(req.identity_token).key
        claims = pyjwt.decode(
            req.identity_token, key, algorithms=["RS256"],
            audience=config.APPLE_BUNDLE_ID, issuer="https://appleid.apple.com",
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Apple token. Please try again.")
    email = (claims.get("email") or "").lower().strip()
    if not email:
        raise HTTPException(status_code=401, detail="Apple didn't share an email for this account.")
    return await _social_login("apple", claims["sub"], email, req.name, None)


# ---------- Password reset (spec item #7) ----------
RESET_CODE_TTL_MIN = 15


def _generate_reset_code() -> str:
    """6-digit numeric code, cryptographically random."""
    return f"{secrets.randbelow(1_000_000):06d}"


@router.post("/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    """Always return 200 regardless of whether the email exists (defeats user enumeration).
    If SMTP is not configured, endpoint returns 503 so the frontend can hide the link.
    Rate-limited: 3 requests / rolling-hour per IP + 3 / day per user.
    """
    if not smtp_enabled():
        raise HTTPException(status_code=503, detail="Password reset is not available right now.")
    check_ip_rate_limit(request, bucket="forgot_password", max_hits=3, window_seconds=3600)
    email = req.email.lower().strip()
    user = await db.fetchrow(
        "select user_id, email, password_hash, auth_provider from users where email = $1", email
    )
    # Silently succeed for missing accounts (no enumeration).
    if user and user.get("password_hash") and (user.get("auth_provider") or "local") == "local":
        # Per-user daily cap (3/day).
        try:
            await check_and_increment_daily_quota(user["user_id"], plan="free", quota_bucket="password_reset_email")
        except HTTPException as e:
            if e.status_code == 429:
                # Even on rate-limit hit, return a uniform 200 so callers can't infer email existence.
                return {"ok": True}
            raise
        code = _generate_reset_code()
        await db.execute(
            """insert into password_resets (user_id, email, code_hash, expires_at, consumed, created_at)
               values ($1, $2, $3, $4, false, $5)
               on conflict (user_id) do update set
                 email = $2, code_hash = $3, expires_at = $4, consumed = false, created_at = $5, consumed_at = null""",
            user["user_id"], email, hash_password(code),
            _now() + timedelta(minutes=RESET_CODE_TTL_MIN), _now(),
        )
        try:
            send_password_reset(email, code)
        except Exception as e:
            logger.warning(f"reset-email send failed: {e}")
    return {"ok": True}


@router.post("/reset-password")
async def reset_password(req: ResetPasswordRequest, request: Request):
    if not smtp_enabled():
        raise HTTPException(status_code=503, detail="Password reset is not available right now.")
    check_ip_rate_limit(request, bucket="reset_password", max_hits=10, window_seconds=600)
    email = req.email.lower().strip()
    user = await db.fetchrow("select * from users where email = $1", email)
    if not user or not user.get("password_hash"):
        # Uniform failure
        raise HTTPException(status_code=400, detail="Invalid or expired code.")
    entry = await db.fetchrow("select * from password_resets where user_id = $1", user["user_id"])
    if not entry or entry.get("consumed"):
        raise HTTPException(status_code=400, detail="Invalid or expired code.")
    if entry["expires_at"] and entry["expires_at"] < _now():
        raise HTTPException(status_code=400, detail="Invalid or expired code.")
    if not verify_password(req.code.strip(), entry.get("code_hash") or ""):
        raise HTTPException(status_code=400, detail="Invalid or expired code.")
    # Update password + revoke every existing token (SEC-003), including refresh tokens -
    # a stolen refresh token must not survive a password change.
    await db.execute(
        "update users set password_hash = $1, token_version = token_version + 1 where user_id = $2",
        hash_password(req.new_password), user["user_id"],
    )
    cache.invalidate(user["user_id"])
    await revoke_all_refresh_tokens(user["user_id"])
    await db.execute(
        "update password_resets set consumed = true, consumed_at = $1 where user_id = $2",
        _now(), user["user_id"],
    )
    return {"ok": True}
