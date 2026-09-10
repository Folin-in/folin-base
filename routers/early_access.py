"""Public web early-access and Resend webhook routes."""
from fastapi import APIRouter, Query, Request, status
from fastapi.responses import RedirectResponse

from app.config import settings
from app.models import EarlyAccessRequest
from services.early_access import early_access

router = APIRouter(tags=["early-access"])


@router.post("/early-access", status_code=status.HTTP_202_ACCEPTED)
async def signup(req: EarlyAccessRequest, request: Request):
    return await early_access.signup(req, request)


@router.get("/early-access/confirm")
async def confirm(token: str = Query(min_length=20, max_length=512)):
    confirmed = await early_access.confirm(token)
    separator = "&" if "?" in settings.early_access_web_url else "?"
    result = "confirmed" if confirmed else "invalid"
    return RedirectResponse(f"{settings.early_access_web_url}{separator}early_access={result}", status_code=303)


@router.post("/webhooks/resend")
async def resend_webhook(request: Request):
    payload = (await request.body()).decode("utf-8")
    headers = {
        "id": request.headers.get("svix-id", ""),
        "timestamp": request.headers.get("svix-timestamp", ""),
        "signature": request.headers.get("svix-signature", ""),
    }
    await early_access.webhook(payload, headers)
    return {"received": True}
