"""Facade the auth router imports. Password/refresh/reset live in AuthCore; Google /
Apple / Supabase in SocialAuthService. Both share AuthCore.session and the user-insert
helpers so a session minted for a social login is identical to one from a password login."""
from services.auth_core import AuthCore, core
from services.auth_social import SocialAuthService


class AuthService:
    def __init__(self, core_: AuthCore):
        self._core = core_
        self._social = SocialAuthService(core_)
        # tests swap the JWKS on the class; expose the same attribute names here
        self.google_jwks = SocialAuthService.google_jwks
        self.apple_jwks = SocialAuthService.apple_jwks

    # password / session
    async def register(self, req, request): return await self._core.register(req, request)
    async def login(self, req, request): return await self._core.login(req, request)
    async def refresh(self, req, request): return await self._core.refresh(req, request)
    async def logout(self, user): return await self._core.logout(user)
    async def forgot_password(self, req, request): return await self._core.forgot_password(req, request)
    async def reset_password(self, req, request): return await self._core.reset_password(req, request)
    # social
    async def google(self, req, request): return await self._social.google(req, request)
    async def apple(self, req, request): return await self._social.apple(req, request)
    async def supabase_sync(self, req, request): return await self._social.supabase_sync(req, request)


auth = AuthService(core)
