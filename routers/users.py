"""User routes: health profile, preferences, gamification profile, plan, export & delete."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

import cache
import config
import database as db
import storage
from gamification import compute_profile, sort_by_effective_date
from models import (DIETS, GENDERS, LANGUAGES, PLANS, WORKOUT_TYPES, HealthProfileUpdate,
                    PlanChangeRequest, PreferencesUpdate, default_preferences, public_user)
from security import get_current_user, revoke_all_refresh_tokens

router = APIRouter(tags=["users"])

VALID_APPEARANCE = {"light", "dark", "system"}
VALID_NOTIFICATIONS = {"off", "weekly", "daily"}
VALID_UNITS = {"us", "si"}


@router.put("/user/health-profile")
async def update_health_profile(req: HealthProfileUpdate, current_user: dict = Depends(get_current_user)):
    if req.gender is not None and req.gender not in GENDERS:
        raise HTTPException(status_code=400, detail="gender must be 'male' or 'female'")
    if req.workout_type is not None and req.workout_type not in WORKOUT_TYPES:
        raise HTTPException(status_code=400, detail=f"workout_type must be one of {sorted(WORKOUT_TYPES)}")
    if req.diet is not None and req.diet not in DIETS:
        raise HTTPException(status_code=400, detail=f"diet must be one of {sorted(DIETS)}")
    if req.language is not None and req.language not in LANGUAGES:
        raise HTTPException(status_code=400, detail=f"language must be one of {sorted(LANGUAGES)}")

    updates: dict = {}

    # Only rebuild health_profile when at least one HP field was supplied. This
    # prevents wiping the profile when the client only sends email/username.
    payload_set = req.model_dump(exclude_unset=True)
    hp_keys = {"age", "gender", "weight_kg", "height_cm", "workout", "workout_type", "diet", "language", "allergies", "allergies_asked"}
    if any(k in payload_set for k in hp_keys):
        existing_hp = current_user.get("health_profile") or {}
        # Normalise + de-dupe allergies (case-insensitive, keep original casing of first occurrence)
        allergies_val = existing_hp.get("allergies")
        if "allergies" in payload_set:
            seen: set = set()
            allergies_val = []
            for a in (req.allergies or []):
                s = (str(a) or "").strip()[:40]
                if not s:
                    continue
                k = s.lower()
                if k in seen:
                    continue
                seen.add(k)
                allergies_val.append(s)
        hp = {
            **existing_hp,
            "age": req.age if "age" in payload_set else existing_hp.get("age"),
            "gender": req.gender if "gender" in payload_set else existing_hp.get("gender"),
            "weight_kg": req.weight_kg if "weight_kg" in payload_set else existing_hp.get("weight_kg"),
            "height_cm": req.height_cm if "height_cm" in payload_set else existing_hp.get("height_cm"),
            "workout": req.workout if "workout" in payload_set else existing_hp.get("workout"),
            "workout_type": (req.workout_type if req.workout else None) if "workout_type" in payload_set or "workout" in payload_set else existing_hp.get("workout_type"),
            "diet": req.diet if "diet" in payload_set else existing_hp.get("diet"),
            "language": req.language if "language" in payload_set else (existing_hp.get("language") or "english"),
            "allergies": allergies_val,
            "allergies_asked": req.allergies_asked if "allergies_asked" in payload_set else existing_hp.get("allergies_asked", False),
        }
        updates["health_profile"] = hp

    if req.name is not None and req.name.strip():
        updates["name"] = req.name.strip()

    # Email update - must be unique, cannot conflict with another account
    email_changed = False
    if req.email is not None:
        new_email = str(req.email).lower().strip()
        if new_email != current_user["email"].lower():
            if await db.fetchval("select 1 from users where email = $1 and user_id <> $2",
                                 new_email, current_user["user_id"]):
                raise HTTPException(status_code=409, detail="That email is already used by another account.")
            updates["email"] = new_email
            email_changed = True

    # Username update - must be unique
    if req.username is not None:
        new_username = req.username.strip().lower()
        if new_username and new_username != (current_user.get("username") or "").lower():
            if await db.fetchval("select 1 from users where username = $1 and user_id <> $2",
                                 new_username, current_user["user_id"]):
                raise HTTPException(status_code=409, detail="That username is already taken.")
            updates["username"] = new_username

    if updates:
        sets = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(updates))
        # If email was changed, revoke all previously issued tokens (SEC-003), including
        # refresh tokens so a "revoked" session can't re-mint access via refresh.
        if email_changed:
            sets += ", token_version = token_version + 1"
        await db.execute(f"update users set {sets} where user_id = $1", current_user["user_id"], *updates.values())
        cache.invalidate(current_user["user_id"])
        if email_changed:
            await revoke_all_refresh_tokens(current_user["user_id"])
    user = await db.fetchrow("select * from users where user_id = $1", current_user["user_id"])
    return public_user(user)


@router.get("/profile")
async def get_gamification_profile(
    member_id: str | None = None,
    current_user: dict = Depends(get_current_user),
):
    mid = (member_id or "self").strip() or "self"
    rows = await cache.get_reports(current_user["user_id"])
    docs = [r for r in rows if r.get("member_id", "self") == mid]
    return compute_profile(sort_by_effective_date(docs))


# ---------- Preferences ----------
@router.get("/user/preferences")
async def get_preferences(current_user: dict = Depends(get_current_user)):
    prefs = current_user.get("preferences") or {}
    return {**default_preferences(), **prefs}


@router.put("/user/preferences")
async def update_preferences(req: PreferencesUpdate, current_user: dict = Depends(get_current_user)):
    updates = {}
    if req.appearance is not None:
        if req.appearance not in VALID_APPEARANCE:
            raise HTTPException(status_code=400, detail=f"appearance must be one of {sorted(VALID_APPEARANCE)}")
        updates["preferences.appearance"] = req.appearance
    if req.notifications is not None:
        if req.notifications not in VALID_NOTIFICATIONS:
            raise HTTPException(status_code=400, detail=f"notifications must be one of {sorted(VALID_NOTIFICATIONS)}")
        updates["preferences.notifications"] = req.notifications
    if req.units is not None:
        if req.units not in VALID_UNITS:
            raise HTTPException(status_code=400, detail=f"units must be one of {sorted(VALID_UNITS)}")
        updates["preferences.units"] = req.units

    if updates:
        merged = {**(current_user.get("preferences") or {}), **{k.split(".")[1]: v for k, v in updates.items()}}
        await db.execute("update users set preferences = $1 where user_id = $2", merged, current_user["user_id"])
        cache.invalidate(current_user["user_id"])
    user = await db.fetchrow("select * from users where user_id = $1", current_user["user_id"])
    prefs = user.get("preferences") or {}
    return {**default_preferences(), **prefs}


# ---------- Plan (free / pro) ----------
@router.post("/user/plan")
async def change_plan(req: PlanChangeRequest, current_user: dict = Depends(get_current_user)):
    """Switch the user's plan. Pro runs config.PRO_TERM_DAYS days, then auto-expires (checked on every
    request in get_current_user) - the user re-purchases after that. Every change lands in
    plan_history. NOTE: self-serve until Stripe; ALLOW_SELF_SERVE_PRO=false blocks upgrades.
    """
    new_plan = (req.plan or "").strip().lower()
    if new_plan not in PLANS:
        raise HTTPException(status_code=400, detail=f"plan must be one of {sorted(PLANS)}")
    # SEC-002: upgrades to paid tiers are only allowed when self-serve mode is enabled
    # (preview/demo). Production must set ALLOW_SELF_SERVE_PRO=false and grant plans
    # via a verified payment webhook. Downgrades are always allowed.
    if new_plan != "free" and not config.ALLOW_SELF_SERVE_PRO:
        raise HTTPException(status_code=403, detail="Upgrades require payment. Please use the checkout flow.")

    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=config.PRO_TERM_DAYS)).isoformat() if new_plan == "pro" else None
    merged = {
        **(current_user.get("preferences") or {}),
        "plan": new_plan,
        "plan_changed_at": now.isoformat(),
        "plan_expires_at": expires_at,
    }
    await db.execute("update users set preferences = $1 where user_id = $2", merged, current_user["user_id"])
    await db.execute(
        """insert into plan_history (user_id, action, plan, amount_inr, starts_at, expires_at, created_at)
           values ($1, $2, $3, $4, $5, $6, $5)""",
        current_user["user_id"],
        "upgraded" if new_plan == "pro" else "downgraded",
        new_plan,
        config.PRO_PRICE_INR if new_plan == "pro" else None,
        now.isoformat(),
        expires_at,
    )
    cache.invalidate(current_user["user_id"])
    return {**default_preferences(), **merged}


@router.get("/user/plan/history")
async def plan_history(current_user: dict = Depends(get_current_user)):
    """Full payment / plan audit trail, newest first."""
    return await db.fetch(
        "select action, plan, amount_inr, starts_at, expires_at, created_at from plan_history where user_id = $1 order by created_at desc limit 100",
        current_user["user_id"],
    )


# ---------- Export ----------
@router.get("/user/export")
async def export_user_data(current_user: dict = Depends(get_current_user)):
    """Return the full user data blob for download - profile + all reports. PRO feature."""
    plan = (current_user.get("preferences") or {}).get("plan") or "free"
    if plan != "pro":
        raise HTTPException(status_code=403, detail="Exporting your data is a Pro feature. Upgrade to unlock.")
    user_id = current_user["user_id"]
    reports = await db.fetch("select * from reports where user_id = $1 order by uploaded_at desc limit 1000", user_id)
    for r in reports:
        r["id"] = str(r["id"])
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": public_user(current_user),
        "reports": reports,
    }


# ---------- Delete account ----------
@router.delete("/user/account")
async def delete_account(current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    paths = [r["storage_path"] for r in await db.fetch(
        "select storage_path from reports where user_id = $1 and storage_path is not null", user_id)]
    await storage.delete_files(paths)
    # Foreign-key cascades take out reports, members, tokens, resets, jobs, quotas and family rows.
    await db.execute("delete from users where user_id = $1", user_id)
    cache.invalidate(user_id)
    cache.invalidate_reports(user_id)
    return {"deleted": True}
