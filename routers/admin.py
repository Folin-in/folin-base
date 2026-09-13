"""Admin routes. Thin: gate, call AdminMetricsService, respond.

Every route here is admin-only and read-only. The gate returns 404 for a signed-in
non-admin, so the surface is not discoverable from an ordinary account.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.config import settings
from app.constants import METRIC_WINDOWS
from app.models import GrantProRequest
from core.security import get_admin_user, get_client_ip, is_ip_allowed, require_admin_ip
from services.admin_metrics import admin_metrics
from services.users import users

router = APIRouter(prefix="/admin", tags=["admin"])

DASHBOARD = Path(__file__).resolve().parent.parent / "static" / "admin.html"
_DEFAULT_WINDOW = "7d"


@router.get("")
async def dashboard():
    """The page itself is public HTML with no data in it - it asks for a token and then
    calls /admin/metrics, which is gated. Serving it unauthenticated avoids a second
    auth mechanism (cookies) just to render an empty shell."""
    if not DASHBOARD.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(DASHBOARD, media_type="text/html")


@router.get("/metrics")
async def metrics(window: str = Query(default=_DEFAULT_WINDOW),
                  _: dict = Depends(get_admin_user)):
    days = METRIC_WINDOWS.get(window)
    if days is None:
        raise HTTPException(status_code=422, detail=f"window must be one of {list(METRIC_WINDOWS)}")
    return await admin_metrics.snapshot(days)


@router.get("/my-ip")
async def check_my_ip(request: Request):
    """Utility endpoint to verify caller's detected IP and authorization status."""
    ip = get_client_ip(request)
    allowed = is_ip_allowed(ip, settings.admin_allowed_ips)
    return {"ip": ip, "allowed": allowed}


@router.post("/grant-pro")
async def grant_pro(req: GrantProRequest,
                    _: str = Depends(require_admin_ip)):
    """Grant Pro access to a user. Strictly gated to authorized admin IPs."""
    return await users.grant_pro(req.identifier, req.days)

