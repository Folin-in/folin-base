"""User routes. Thin: parse, call UserService, respond."""
from fastapi import APIRouter, Depends

from app.models import (ChangeEmailRequest, ChangePasswordRequest, HealthProfileUpdate, PlanChangeRequest,
                        PreferencesUpdate, PushTokenDeleteRequest, PushTokenRequest)
from core.security import get_current_user
from services.users import users
from services.push_notifications import push_notifications

router = APIRouter(tags=["users"])


@router.put("/user/health-profile")
async def update_health_profile(req: HealthProfileUpdate, current_user: dict = Depends(get_current_user)):
    return await users.update_health_profile(req, current_user)


@router.post("/user/events")
async def record_engagement_event(payload: dict, current_user: dict = Depends(get_current_user)):
    await users.record_event(payload, current_user["user_id"])
    return {"ok": True}


@router.get("/profile")
async def get_gamification_profile(member_id: str | None = None, current_user: dict = Depends(get_current_user)):
    return await users.gamification_profile(current_user["user_id"], member_id)


@router.post("/user/consent")
async def give_consent(current_user: dict = Depends(get_current_user)):
    return await users.give_consent(current_user["user_id"])


@router.get("/user/preferences")
async def get_preferences(current_user: dict = Depends(get_current_user)):
    return users.preferences(current_user)


@router.put("/user/preferences")
async def update_preferences(req: PreferencesUpdate, current_user: dict = Depends(get_current_user)):
    return await users.update_preferences(req, current_user)


@router.post("/user/push-token")
async def register_push_token(req: PushTokenRequest, current_user: dict = Depends(get_current_user)):
    await push_notifications.register(req, current_user["user_id"])
    return {"registered": True}


@router.delete("/user/push-token")
async def unregister_push_token(req: PushTokenDeleteRequest, current_user: dict = Depends(get_current_user)):
    await push_notifications.unregister(req.token, current_user["user_id"])
    return {"registered": False}


@router.post("/user/change-password")
async def change_password(req: ChangePasswordRequest, current_user: dict = Depends(get_current_user)):
    return await users.change_password(req, current_user)


@router.post("/user/change-email")
async def change_email(req: ChangeEmailRequest, current_user: dict = Depends(get_current_user)):
    return await users.change_email(req, current_user)


@router.post("/user/plan")
async def change_plan(req: PlanChangeRequest, current_user: dict = Depends(get_current_user)):
    return await users.change_plan(req, current_user)


@router.get("/user/plan/history")
async def plan_history(current_user: dict = Depends(get_current_user)):
    return await users.plan_history(current_user["user_id"])


@router.get("/user/export")
async def export_user_data(current_user: dict = Depends(get_current_user)):
    return await users.export(current_user)


@router.delete("/user/account")
async def delete_account(current_user: dict = Depends(get_current_user)):
    await users.delete_account(current_user["user_id"])
    return {"deleted": True}
