"""Auth routes. Thin: parse, call AuthService, respond. All logic lives in services/auth.py."""
from fastapi import APIRouter, Depends, Request

from app.models import (AppleLoginRequest, ForgotPasswordRequest, GoogleLoginRequest, LoginRequest,
                        RefreshRequest, RegisterRequest, ResetPasswordRequest, SupabaseSyncRequest,
                        public_user)
from core.security import get_current_user
from services.auth import AuthService, auth

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register")
async def register(req: RegisterRequest, request: Request):
    return await auth.register(req, request)


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    return await auth.login(req, request)


@router.post("/refresh")
async def refresh_tokens(req: RefreshRequest, request: Request):
    return await auth.refresh(req, request)


@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    return public_user(current_user)


@router.post("/logout")
async def logout(current_user: dict = Depends(get_current_user)):
    await auth.logout(current_user)
    return {"ok": True}


@router.post("/google")
async def google_login(req: GoogleLoginRequest, request: Request):
    return await auth.google(req, request)


@router.post("/apple")
async def apple_login(req: AppleLoginRequest, request: Request):
    return await auth.apple(req, request)


@router.post("/supabase/sync")
async def supabase_sync(req: SupabaseSyncRequest, request: Request):
    return await auth.supabase_sync(req, request)


@router.post("/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    """Always 200 whether or not the address exists - anything else is an enumeration oracle."""
    await auth.forgot_password(req, request)
    return {"ok": True}


@router.post("/reset-password")
async def reset_password(req: ResetPasswordRequest, request: Request):
    await auth.reset_password(req, request)
    return {"ok": True}
