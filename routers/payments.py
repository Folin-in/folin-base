"""Google Play subscription routes."""

from fastapi import APIRouter, Depends, Header, Response, status

from app.models import GooglePlayRtdnEnvelope, GooglePlayVerifyRequest
from core.security import get_current_user
from services.payments import payments

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("/google-play/config")
async def google_play_config(current_user: dict = Depends(get_current_user)):
    return await payments.config_for_user(current_user)


@router.post("/google-play/verify")
async def verify_google_play_purchase(
    req: GooglePlayVerifyRequest, current_user: dict = Depends(get_current_user)
):
    return await payments.verify(req, current_user)


@router.post("/google-play/sync")
async def sync_google_play_subscription(current_user: dict = Depends(get_current_user)):
    return await payments.sync(current_user)


@router.post("/google-play/rtdn", status_code=status.HTTP_204_NO_CONTENT)
async def google_play_rtdn(
    envelope: GooglePlayRtdnEnvelope,
    authorization: str | None = Header(default=None),
):
    await payments.process_rtdn(envelope, authorization)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
