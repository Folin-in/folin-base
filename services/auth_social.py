"""Social identities: Google, Apple, and Supabase Auth. Each verifies a provider-signed
token against the provider's public keys, then finds-or-creates the Folin account.

Binding rule (the account-takeover guard): the provider's `sub` is what was actually
vouched for, so it wins the tie-break when sub and email match different rows, and an
existing link is never repointed at a new sub. Matched-by-email is still a valid login -
the provider verified the address - it just cannot claim the link.
"""
import uuid

import jwt as pyjwt
from fastapi import HTTPException, Request

from app.config import settings
from app.constants import (APPLE_ISSUER, APPLE_JWKS_URL, GOOGLE_ISSUERS, GOOGLE_JWKS_URL, SOCIAL_TOKEN_ALGS,
                           USER_ID_HEX, RateLimit)
from app.enums import AuthProvider
from app.models import AppleLoginRequest, GoogleLoginRequest, SupabaseSyncRequest, default_preferences, public_user
from core import cache
from core.security import decode_supabase_token
from db import session as db
from db.sql import SOCIAL_ID_COLUMNS, USERS_UPDATABLE, assert_columns, set_clause
from services.auth_core import (MSG_CONSENT, AuthCore, _limit, _now, insert_user, unique_username)

MSG_GOOGLE_OFF = "Google login is not configured."
MSG_BAD_GOOGLE = "Invalid Google token. Please try again."
MSG_GOOGLE_NO_EMAIL = "Your Google account has no verified email."
MSG_BAD_APPLE = "Invalid Apple token. Please try again."
MSG_APPLE_NO_EMAIL = "Apple didn't share an email for this account."
MSG_SUPABASE_OFF = "Supabase login is not configured."
MSG_BAD_SIGNIN = "Invalid or expired sign-in. Please try again."
MSG_SIGNIN_NO_EMAIL = "That sign-in has no email address."
MSG_EMAIL_LINKED = "That email is already linked to another sign-in."
MSG_CONFIRM_FIRST = "Please confirm your email first - it already belongs to a Folin account."


class SocialAuthService:
    # JWKS clients are class attributes so tests can swap in a local key.
    google_jwks = pyjwt.PyJWKClient(GOOGLE_JWKS_URL)
    apple_jwks = pyjwt.PyJWKClient(APPLE_JWKS_URL)

    def __init__(self, core: AuthCore):
        self._core = core

    async def google(self, req: GoogleLoginRequest, request: Request) -> dict:
        if not settings.google_login_enabled:
            raise HTTPException(status_code=503, detail=MSG_GOOGLE_OFF)
        _limit(request, RateLimit.SOCIAL_LOGIN)
        try:
            key = self.google_jwks.get_signing_key_from_jwt(req.id_token).key
            claims = pyjwt.decode(req.id_token, key, algorithms=list(SOCIAL_TOKEN_ALGS),
                                  audience=settings.google_client_ids)
        except Exception:
            raise HTTPException(status_code=401, detail=MSG_BAD_GOOGLE)
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise HTTPException(status_code=401, detail=MSG_BAD_GOOGLE)
        email = (claims.get("email") or "").lower().strip()
        if not email or not claims.get("email_verified", False):
            raise HTTPException(status_code=401, detail=MSG_GOOGLE_NO_EMAIL)
        return await self._social(AuthProvider.GOOGLE, claims["sub"], email, claims.get("name"), claims.get("picture"))

    async def apple(self, req: AppleLoginRequest, request: Request) -> dict:
        _limit(request, RateLimit.SOCIAL_LOGIN)
        try:
            key = self.apple_jwks.get_signing_key_from_jwt(req.identity_token).key
            claims = pyjwt.decode(req.identity_token, key, algorithms=list(SOCIAL_TOKEN_ALGS),
                                  audience=settings.apple_bundle_id, issuer=APPLE_ISSUER)
        except Exception:
            raise HTTPException(status_code=401, detail=MSG_BAD_APPLE)
        email = (claims.get("email") or "").lower().strip()
        if not email:
            raise HTTPException(status_code=401, detail=MSG_APPLE_NO_EMAIL)
        return await self._social(AuthProvider.APPLE, claims["sub"], email, req.name, None)

    async def _social(self, provider: AuthProvider, sub: str, email: str, name: str | None, picture: str | None) -> dict:
        id_col = f"{provider}_id"
        assert_columns([id_col], SOCIAL_ID_COLUMNS, "users")
        user = await db.fetchrow(
            f"""select * from users where {id_col} = $1 or email = $2
                order by ({id_col} is not distinct from $1) desc limit 1""",
            sub, email)
        if user:
            updates = {} if user.get(id_col) else {id_col: sub}
            if not user.get("picture") and picture:
                updates["picture"] = picture
            if not user.get("name") and name:
                updates["name"] = name
            if updates:
                await db.execute(f"update users set {set_clause(updates, USERS_UPDATABLE, 'users')} where user_id = $1",
                                 user["user_id"], *updates.values())
                cache.invalidate(user["user_id"])
                user = await db.fetchrow("select * from users where user_id = $1", user["user_id"])
            return await self._core.session(user)
        username = await unique_username(email.split("@")[0])
        user = {
            "user_id": f"user_{uuid.uuid4().hex[:USER_ID_HEX]}", "email": email, "username": username,
            "name": (name or "").strip() or username, "password_hash": None, "auth_provider": str(provider),
            id_col: sub, "picture": picture, "health_profile": {}, "preferences": default_preferences(),
            "token_version": 0, "created_at": _now().isoformat(),
        }
        await insert_user(user)
        return await self._core.session(user)

    async def supabase_sync(self, req: SupabaseSyncRequest, request: Request) -> dict:
        if not settings.supabase_auth_enabled:
            raise HTTPException(status_code=503, detail=MSG_SUPABASE_OFF)
        _limit(request, RateLimit.SOCIAL_LOGIN)
        header = request.headers.get("Authorization") or ""
        token = header.split(" ", 1)[1].strip() if header.startswith("Bearer ") else ""
        claims = decode_supabase_token(token) if token else None
        if not claims:
            raise HTTPException(status_code=401, detail=MSG_BAD_SIGNIN)
        try:
            uid = str(uuid.UUID(str(claims["sub"])))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(status_code=401, detail=MSG_BAD_SIGNIN)
        email = (claims.get("email") or "").lower().strip()
        if not email:
            raise HTTPException(status_code=401, detail=MSG_SIGNIN_NO_EMAIL)
        verified = bool(claims.get("email_verified") or (claims.get("user_metadata") or {}).get("email_verified"))

        user = await db.fetchrow("select * from users where supabase_uid = $1", uid)
        if user:
            return {"user": public_user(user)}
        existing = await db.fetchrow("select * from users where email = $1", email)
        if existing:
            if existing.get("supabase_uid"):
                raise HTTPException(status_code=409, detail=MSG_EMAIL_LINKED)
            if not verified:
                raise HTTPException(status_code=403, detail=MSG_CONFIRM_FIRST)
            await db.execute("update users set supabase_uid = $1 where user_id = $2", uid, existing["user_id"])
            cache.invalidate(existing["user_id"])
            return {"user": public_user(await db.fetchrow("select * from users where user_id = $1", existing["user_id"]))}
        if not req.consent:
            raise HTTPException(status_code=400, detail=MSG_CONSENT)
        meta = claims.get("user_metadata") or {}
        now = _now().isoformat()
        username = await unique_username((req.username or "").strip() or email.split("@")[0])
        user = {
            "user_id": f"user_{uuid.uuid4().hex[:USER_ID_HEX]}", "email": email, "username": username,
            "name": (req.name or meta.get("full_name") or meta.get("name") or "").strip() or username,
            "password_hash": None, "auth_provider": str(AuthProvider.SUPABASE), "supabase_uid": uid,
            "picture": meta.get("avatar_url") or meta.get("picture"), "health_profile": {},
            "preferences": default_preferences(), "token_version": 0, "created_at": now,
            "consented_at": now, "consent_version": settings.consent_version,
        }
        await insert_user(user)
        return {"user": public_user(await db.fetchrow("select * from users where user_id = $1", user["user_id"]))}



social = None  # bound in services.auth after AuthCore exists
