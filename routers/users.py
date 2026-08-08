"""User routes: health profile, preferences, gamification profile, plan, export & delete."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

import cache
import config
import database as db
import storage
from gamification import EVENT_XP, compute_profile, sort_by_effective_date
from models import (DIETS, GENDERS, LANGUAGES, PLANS, WORKOUT_TYPES, ChangeEmailRequest,
                    ChangePasswordRequest, HealthProfileUpdate,
                    PlanChangeRequest, PreferencesUpdate, default_preferences, public_user)
from security import (create_access_token, get_current_user, hash_password, issue_refresh_token,
                      revoke_all_refresh_tokens, verify_password)

router = APIRouter(tags=["users"])

VALID_APPEARANCE = {"light", "dark", "system"}
VALID_NOTIFICATIONS = {"off", "weekly", "daily"}
VALID_UNITS = {"us", "si"}
VALID_REGIONS = {"india", "other"}


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
        # Profile change history: advice correctness depends on profile state, so
        # every changed field gets an append-only audit row.
        _now_iso = datetime.now(timezone.utc).isoformat()
        for k in hp_keys:
            if existing_hp.get(k) != hp.get(k):
                await db.execute(
                    """insert into profile_edits (user_id, field, old_value, new_value, changed_at)
                       values ($1, $2, $3, $4, $5)""",
                    current_user["user_id"], k, {"v": existing_hp.get(k)}, {"v": hp.get(k)}, _now_iso,
                )

    if req.name is not None and req.name.strip():
        updates["name"] = req.name.strip()

    # Email moved to POST /user/change-email (password-confirmed). Kept here only so
    # older clients that still send it don't break; ignored unless it actually differs.
    # Email is deliberately NOT changeable here. /user/change-email confirms the current
    # password first because changing the sign-in address is an account-takeover vector,
    # and it refuses social accounts outright (Google/Apple owns that address). Allowing a
    # silent change through the profile endpoint would make both of those guards pointless.
    email_changed = False
    if req.email is not None and str(req.email).lower().strip() != (current_user.get("email") or "").lower():
        raise HTTPException(
            status_code=400,
            detail="Change your email from Settings → Account so we can confirm it's you.",
        )

    # Username update - must be unique
    if req.username is not None:
        new_username = req.username.strip().lower()
        if new_username and new_username != (current_user.get("username") or "").lower():
            if await db.fetchval("select 1 from users where username = $1 and user_id <> $2",
                                 new_username, current_user["user_id"]):
                raise HTTPException(status_code=409, detail="That username is already taken.")
            updates["username"] = new_username

    if "health_profile" in updates:
        hp_new = updates["health_profile"]
        now = datetime.now(timezone.utc).isoformat()
        # Auto-events (once each, enforced here AND at scoring time): completing the
        # profile / declaring allergies is behaviour the gamification engine rewards.
        if all(hp_new.get(k) for k in ("age", "weight_kg", "height_cm", "diet")) and hp_new.get("workout") is not None:
            await _record_event_once(current_user["user_id"], "profile_completed", now)
        if hp_new.get("allergies"):
            await _record_event_once(current_user["user_id"], "allergies_declared", now)

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


async def _record_event_once(user_id: str, etype: str, at: str) -> None:
    """Insert a once-only engagement event unless one already exists."""
    exists = await db.fetchval(
        "select 1 from engagement_events where user_id = $1 and type = $2 limit 1", user_id, etype
    )
    if not exists:
        await db.execute(
            "insert into engagement_events (user_id, type, at) values ($1, $2, $3)", user_id, etype, at
        )


async def fetch_events(user_id: str) -> list[dict]:
    return await db.fetch(
        "select type, report_id, ref, at from engagement_events where user_id = $1", user_id
    )


@router.post("/user/events")
async def record_engagement_event(payload: dict, current_user: dict = Depends(get_current_user)):
    """Record a behaviour signal from the app (e.g. explanation_read, followup_logged).
    XP farming is bounded at scoring time (EVENT_ONCE / EVENT_DAILY_CAP)."""
    etype = str(payload.get("type") or "")
    if etype not in EVENT_XP:
        raise HTTPException(status_code=400, detail=f"type must be one of {sorted(EVENT_XP)}")
    report_id = (str(payload.get("report_id")) if payload.get("report_id") else None)
    ref = (str(payload.get("ref"))[:80] if payload.get("ref") else None)
    await db.execute(
        "insert into engagement_events (user_id, type, report_id, ref, at) values ($1, $2, $3, $4, $5)",
        current_user["user_id"], etype, report_id, ref, datetime.now(timezone.utc).isoformat(),
    )
    return {"ok": True}


@router.get("/profile")
async def get_gamification_profile(
    member_id: str | None = None,
    current_user: dict = Depends(get_current_user),
):
    mid = (member_id or "self").strip() or "self"
    rows = await cache.get_reports(current_user["user_id"])
    docs = [r for r in rows if r.get("member_id", "self") == mid]
    # Engagement events belong to the ACCOUNT, not to a sub-profile - a family
    # member with no reports was showing the owner's XP and level.
    events = await fetch_events(current_user["user_id"]) if mid == "self" else []
    return compute_profile(sort_by_effective_date(docs), events=events)


# ---------- Consent (social-login users consent post-login; email signups at register) ----------
@router.post("/user/consent")
async def give_consent(current_user: dict = Depends(get_current_user)):
    await db.execute(
        "update users set consented_at = $1, consent_version = $2 where user_id = $3",
        datetime.now(timezone.utc).isoformat(), config.CONSENT_VERSION, current_user["user_id"],
    )
    cache.invalidate(current_user["user_id"])
    user = await db.fetchrow("select * from users where user_id = $1", current_user["user_id"])
    return public_user(user)


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
    if req.region is not None:
        if req.region not in VALID_REGIONS:
            raise HTTPException(status_code=400, detail=f"region must be one of {sorted(VALID_REGIONS)}")
        updates["preferences.region"] = req.region

    if updates:
        merged = {**(current_user.get("preferences") or {}), **{k.split(".")[1]: v for k, v in updates.items()}}
        await db.execute("update users set preferences = $1 where user_id = $2", merged, current_user["user_id"])
        cache.invalidate(current_user["user_id"])
    user = await db.fetchrow("select * from users where user_id = $1", current_user["user_id"])
    prefs = user.get("preferences") or {}
    return {**default_preferences(), **prefs}


# ---------- Account security (change password / email) ----------
async def _rotate_session(user_id: str) -> dict:
    """Bump token_version (kills every existing token everywhere), then mint a fresh
    pair for THIS device so the user who just made the change stays signed in."""
    new_tv = await db.fetchval(
        "update users set token_version = token_version + 1 where user_id = $1 returning token_version",
        user_id,
    )
    await revoke_all_refresh_tokens(user_id)
    cache.invalidate(user_id)
    return {
        "access_token": create_access_token(user_id, new_tv),
        "refresh_token": await issue_refresh_token(user_id),
        "token_type": "bearer",
    }


@router.post("/user/change-password")
async def change_password(req: ChangePasswordRequest, current_user: dict = Depends(get_current_user)):
    """Change the password while signed in. Requires the current password, signs every
    OTHER device out, and returns fresh tokens so this device stays signed in."""
    if not current_user.get("password_hash"):
        raise HTTPException(
            status_code=400,
            detail="This account signs in with Google or Apple, so it has no password to change.",
        )
    if not verify_password(req.current_password, current_user["password_hash"]):
        raise HTTPException(status_code=401, detail="That current password isn't right.")
    if req.new_password == req.current_password:
        raise HTTPException(status_code=400, detail="Your new password must be different from the current one.")
    await db.execute("update users set password_hash = $1 where user_id = $2",
                     hash_password(req.new_password), current_user["user_id"])
    tokens = await _rotate_session(current_user["user_id"])
    return {"ok": True, **tokens}


@router.post("/user/change-email")
async def change_email(req: ChangeEmailRequest, current_user: dict = Depends(get_current_user)):
    """Change the sign-in email. Password-confirmed (it is an account takeover vector),
    signs every other device out, and keeps THIS device signed in with fresh tokens."""
    if not current_user.get("password_hash"):
        raise HTTPException(
            status_code=400,
            detail="This account signs in with Google or Apple - change the email with that provider instead.",
        )
    if not verify_password(req.password, current_user["password_hash"]):
        raise HTTPException(status_code=401, detail="That password isn't right.")
    new_email = str(req.new_email).lower().strip()
    if new_email == (current_user.get("email") or "").lower():
        raise HTTPException(status_code=400, detail="That's already your email address.")
    if await db.fetchval("select 1 from users where email = $1 and user_id <> $2",
                         new_email, current_user["user_id"]):
        raise HTTPException(status_code=409, detail="That email is already used by another account.")
    await db.execute("update users set email = $1 where user_id = $2", new_email, current_user["user_id"])
    tokens = await _rotate_session(current_user["user_id"])
    user = await db.fetchrow("select * from users where user_id = $1", current_user["user_id"])
    return {"ok": True, "user": public_user(user), **tokens}


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
    reports = await db.fetch("select * from reports where user_id = $1 and deleted_at is null order by uploaded_at desc limit 1000", user_id)
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
    # storage_path is comma-joined for multi-page uploads - flatten to individual paths.
    paths = [p for r in await db.fetch(
        "select storage_path from reports where user_id = $1 and storage_path is not null", user_id)
        for p in r["storage_path"].split(",") if p]
    await storage.delete_files(paths)
    # Foreign-key cascades take out reports, members, tokens, resets, jobs, quotas and family rows.
    await db.execute("delete from users where user_id = $1", user_id)
    cache.invalidate(user_id)
    cache.invalidate_reports(user_id)
    return {"deleted": True}
