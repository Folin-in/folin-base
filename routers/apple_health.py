"""Apple Health integration - connect/disconnect + sync today's metrics.

On real iOS builds the client uses HealthKit to fetch the values and POSTs them here.
On web / Expo Go, the client submits a plausible sample day (or lets the user enter
values manually). The backend just persists whatever the client sends under
health_profile.apple_health so the home tab can render it.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import cache
import database as db
from models import public_user
from security import get_current_user

router = APIRouter(prefix="/user/apple-health", tags=["apple-health"])


class AppleHealthSyncRequest(BaseModel):
    # All optional - the client sends whatever HealthKit gave it.
    steps: Optional[int] = Field(default=None, ge=0, le=200_000)
    sleep_minutes: Optional[int] = Field(default=None, ge=0, le=24 * 60)
    resting_hr: Optional[int] = Field(default=None, ge=20, le=220)
    active_minutes: Optional[int] = Field(default=None, ge=0, le=24 * 60)
    # Optional day this data corresponds to (YYYY-MM-DD). Server stamps if omitted.
    day: Optional[str] = None
    # "healthkit" (real device data) or "sample" (demo fallback) - shown in the UI.
    source: Optional[str] = None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _save_apple_health(user_id: str, hp: dict, apple_health: dict) -> dict:
    await db.execute(
        "update users set health_profile = $1 where user_id = $2",
        {**hp, "apple_health": apple_health}, user_id,
    )
    cache.invalidate(user_id)
    return await db.fetchrow("select * from users where user_id = $1", user_id)


@router.post("/connect")
async def connect(current_user: dict = Depends(get_current_user)):
    """Idempotently mark Apple Health as connected. Client is expected to trigger a
    /sync right after so the home card has data."""
    now = _iso_now()
    hp = current_user.get("health_profile") or {}
    existing = hp.get("apple_health") or {}
    apple_health = {
        **existing,
        "connected": True,
        "connected_at": existing.get("connected_at") or now,
    }
    user = await _save_apple_health(current_user["user_id"], hp, apple_health)
    return public_user(user)


@router.post("/disconnect")
async def disconnect(current_user: dict = Depends(get_current_user)):
    hp = current_user.get("health_profile") or {}
    apple_health = {**(hp.get("apple_health") or {}), "connected": False, "disconnected_at": _iso_now()}
    user = await _save_apple_health(current_user["user_id"], hp, apple_health)
    return public_user(user)


@router.post("/sync")
async def sync(req: AppleHealthSyncRequest, current_user: dict = Depends(get_current_user)):
    """Store today's Apple Health metrics under health_profile.apple_health.metrics.
    Requires the user to have connected first (else 400).
    """
    hp = current_user.get("health_profile") or {}
    ah = hp.get("apple_health") or {}
    if not ah.get("connected"):
        raise HTTPException(status_code=400, detail="Apple Health is not connected. Connect first, then sync.")
    metrics = {
        "steps": req.steps,
        "sleep_minutes": req.sleep_minutes,
        "resting_hr": req.resting_hr,
        "active_minutes": req.active_minutes,
        "day": req.day or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "synced_at": _iso_now(),
        "source": req.source or "sample",
    }
    apple_health = {**ah, "metrics": metrics, "last_synced_at": _iso_now()}
    user = await _save_apple_health(current_user["user_id"], hp, apple_health)
    return public_user(user)
