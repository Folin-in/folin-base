"""Account and profile: health profile with per-field audit, preferences, consent,
credential changes (password / email) that rotate every session, plan changes,
export, and account deletion.

Credential changes bump token_version (kills every token everywhere) and then mint a
fresh pair for THIS device, so the person who made the change stays signed in while
every other device is out. Email is deliberately NOT changeable through the profile
endpoint - only through change_email, which confirms the password first.
"""
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.config import settings
from app.constants import (ALLERGY_MAX_CHARS, HEALTH_PROFILE_KEYS, VALID_APPEARANCE, VALID_NOTIFICATIONS,
                           VALID_REGIONS, VALID_UNITS)
from app.enums import Plan, PlanAction
from app.models import (DIETS, GENDERS, LANGUAGES, PLANS, WORKOUT_TYPES, ChangeEmailRequest,
                        ChangePasswordRequest, HealthProfileUpdate, PlanChangeRequest, PreferencesUpdate,
                        default_preferences, public_user)
from core import cache, storage
from core.security import (create_access_token, hash_password, issue_refresh_token, revoke_all_refresh_tokens,
                           verify_password)
from db import session as db
from db.sql import USERS_UPDATABLE, set_clause
from services.engagement import EVENT_XP_TYPES, fetch_events, record_event_once
from services.gamification import compute_profile, sort_by_effective_date
from services.members import SELF, member_id_of

PLAN_HISTORY_LIMIT = 100
EXPORT_REPORT_LIMIT = 1000
EVENT_REF_MAX = 80
DEFAULT_LANGUAGE = "english"

MSG_GENDER = "gender must be 'male' or 'female'"
MSG_EMAIL_HERE = "Change your email from Settings → Account so we can confirm it's you."
MSG_USERNAME_TAKEN = "That username is already taken."
MSG_NO_PASSWORD_CHANGE = "This account signs in with Google or Apple, so it has no password to change."
MSG_NO_PASSWORD_EMAIL = "This account signs in with Google or Apple - change the email with that provider instead."
MSG_WRONG_CURRENT = "That current password isn't right."
MSG_WRONG_PASSWORD = "That password isn't right."
MSG_SAME_PASSWORD = "Your new password must be different from the current one."
MSG_SAME_EMAIL = "That's already your email address."
MSG_EMAIL_USED = "That email is already used by another account."
MSG_UPGRADE_NEEDS_PAYMENT = "Upgrades require payment. Please use the checkout flow."
MSG_EXPORT_PRO = "Exporting your data is a Pro feature. Upgrade to unlock."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _prefs(user: dict) -> dict:
    return {**default_preferences(), **(user.get("preferences") or {})}


class UserService:
    # ------------------------------------------------------------ health profile

    async def update_health_profile(self, req: HealthProfileUpdate, user: dict) -> dict:
        for field, allowed, msg in ((req.gender, GENDERS, MSG_GENDER),
                                    (req.workout_type, WORKOUT_TYPES, f"workout_type must be one of {sorted(WORKOUT_TYPES)}"),
                                    (req.diet, DIETS, f"diet must be one of {sorted(DIETS)}"),
                                    (req.language, LANGUAGES, f"language must be one of {sorted(LANGUAGES)}")):
            if field is not None and field not in allowed:
                raise HTTPException(status_code=400, detail=msg)
        updates: dict = {}
        payload = req.model_dump(exclude_unset=True)
        if any(k in payload for k in HEALTH_PROFILE_KEYS):
            updates["health_profile"] = await self._merge_health_profile(req, payload, user)
        if req.name is not None and req.name.strip():
            updates["name"] = req.name.strip()
        if req.email is not None and str(req.email).lower().strip() != (user.get("email") or "").lower():
            raise HTTPException(status_code=400, detail=MSG_EMAIL_HERE)
        if req.username is not None:
            new_username = req.username.strip().lower()
            if new_username and new_username != (user.get("username") or "").lower():
                if await db.fetchval("select 1 from users where username = $1 and user_id <> $2", new_username, user["user_id"]):
                    raise HTTPException(status_code=409, detail=MSG_USERNAME_TAKEN)
                updates["username"] = new_username
        if "health_profile" in updates:
            await self._reward_profile_behaviour(updates["health_profile"], user["user_id"])
        if updates:
            await db.execute(f"update users set {set_clause(updates, USERS_UPDATABLE, 'users')} where user_id = $1",
                             user["user_id"], *updates.values())
            cache.invalidate(user["user_id"])
        return public_user(await db.fetchrow("select * from users where user_id = $1", user["user_id"]))

    @staticmethod
    async def _merge_health_profile(req: HealthProfileUpdate, payload: dict, user: dict) -> dict:
        """Merge only the fields that were SENT over the existing profile, and write an
        append-only profile_edits row for every field that changed - advice correctness
        depends on profile state, so its history must be reconstructible."""
        existing = user.get("health_profile") or {}
        allergies = existing.get("allergies")
        if "allergies" in payload:
            seen: set[str] = set()
            allergies = []
            for a in (req.allergies or []):
                s = (str(a) or "").strip()[:ALLERGY_MAX_CHARS]
                if s and s.lower() not in seen:
                    seen.add(s.lower()); allergies.append(s)
        pick = lambda k, default=None: getattr(req, k) if k in payload else existing.get(k, default)  # noqa: E731
        hp = {
            **existing,
            "age": pick("age"), "gender": pick("gender"), "weight_kg": pick("weight_kg"), "height_cm": pick("height_cm"),
            "workout": pick("workout"),
            "workout_type": (req.workout_type if req.workout else None) if ("workout_type" in payload or "workout" in payload) else existing.get("workout_type"),
            "diet": pick("diet"),
            "language": req.language if "language" in payload else (existing.get("language") or DEFAULT_LANGUAGE),
            "allergies": allergies,
            "allergies_asked": pick("allergies_asked", False),
        }
        now = _now().isoformat()
        for k in HEALTH_PROFILE_KEYS:
            if existing.get(k) != hp.get(k):
                await db.execute(
                    "insert into profile_edits (user_id, field, old_value, new_value, changed_at) values ($1, $2, $3, $4, $5)",
                    user["user_id"], k, {"v": existing.get(k)}, {"v": hp.get(k)}, now)
        return hp

    @staticmethod
    async def _reward_profile_behaviour(hp: dict, user_id: str) -> None:
        """Completing the profile / declaring allergies is behaviour the gamification engine
        rewards - once each, enforced here AND at scoring time."""
        now = _now().isoformat()
        if all(hp.get(k) for k in ("age", "weight_kg", "height_cm", "diet")) and hp.get("workout") is not None:
            await record_event_once(user_id, "profile_completed", now)
        if hp.get("allergies"):
            await record_event_once(user_id, "allergies_declared", now)

    # ------------------------------------------------------------ engagement / profile

    @staticmethod
    async def record_event(payload: dict, user_id: str) -> None:
        etype = str(payload.get("type") or "")
        if etype not in EVENT_XP_TYPES:
            raise HTTPException(status_code=400, detail=f"type must be one of {sorted(EVENT_XP_TYPES)}")
        report_id = str(payload["report_id"]) if payload.get("report_id") else None
        ref = str(payload["ref"])[:EVENT_REF_MAX] if payload.get("ref") else None
        await db.execute("insert into engagement_events (user_id, type, report_id, ref, at) values ($1, $2, $3, $4, $5)",
                         user_id, etype, report_id, ref, _now().isoformat())

    @staticmethod
    async def gamification_profile(user_id: str, member_id: str | None) -> dict:
        """Engagement events belong to the ACCOUNT, not a sub-profile: a family member with no
        reports must not show the owner's XP and level."""
        mid = member_id_of(member_id)
        rows = await cache.get_reports(user_id)
        docs = [r for r in rows if r.get("member_id", SELF) == mid]
        events = await fetch_events(user_id) if mid == SELF else []
        return compute_profile(sort_by_effective_date(docs), events=events)

    @staticmethod
    async def give_consent(user_id: str) -> dict:
        await db.execute("update users set consented_at = $1, consent_version = $2 where user_id = $3",
                         _now().isoformat(), settings.consent_version, user_id)
        cache.invalidate(user_id)
        return public_user(await db.fetchrow("select * from users where user_id = $1", user_id))

    # ------------------------------------------------------------ preferences

    @staticmethod
    def preferences(user: dict) -> dict:
        return _prefs(user)

    @staticmethod
    async def update_preferences(req: PreferencesUpdate, user: dict) -> dict:
        updates: dict = {}
        for name, value, allowed in (("appearance", req.appearance, VALID_APPEARANCE),
                                     ("notifications", req.notifications, VALID_NOTIFICATIONS),
                                     ("units", req.units, VALID_UNITS),
                                     ("region", req.region, VALID_REGIONS)):
            if value is None:
                continue
            if value not in allowed:
                raise HTTPException(status_code=400, detail=f"{name} must be one of {sorted(allowed)}")
            updates[name] = value
        if updates:
            merged = {**(user.get("preferences") or {}), **updates}
            await db.execute("update users set preferences = $1 where user_id = $2", merged, user["user_id"])
            cache.invalidate(user["user_id"])
        return _prefs(await db.fetchrow("select * from users where user_id = $1", user["user_id"]))

    # ------------------------------------------------------------ credentials

    @staticmethod
    async def _rotate_session(user_id: str) -> dict:
        new_tv = await db.fetchval(
            "update users set token_version = token_version + 1 where user_id = $1 returning token_version", user_id)
        await revoke_all_refresh_tokens(user_id)
        cache.invalidate(user_id)
        return {"access_token": create_access_token(user_id, new_tv),
                "refresh_token": await issue_refresh_token(user_id), "token_type": "bearer"}

    async def change_password(self, req: ChangePasswordRequest, user: dict) -> dict:
        if not user.get("password_hash"):
            raise HTTPException(status_code=400, detail=MSG_NO_PASSWORD_CHANGE)
        if not verify_password(req.current_password, user["password_hash"]):
            raise HTTPException(status_code=401, detail=MSG_WRONG_CURRENT)
        if req.new_password == req.current_password:
            raise HTTPException(status_code=400, detail=MSG_SAME_PASSWORD)
        await db.execute("update users set password_hash = $1 where user_id = $2", hash_password(req.new_password), user["user_id"])
        return {"ok": True, **await self._rotate_session(user["user_id"])}

    async def change_email(self, req: ChangeEmailRequest, user: dict) -> dict:
        if not user.get("password_hash"):
            raise HTTPException(status_code=400, detail=MSG_NO_PASSWORD_EMAIL)
        if not verify_password(req.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail=MSG_WRONG_PASSWORD)
        new_email = str(req.new_email).lower().strip()
        if new_email == (user.get("email") or "").lower():
            raise HTTPException(status_code=400, detail=MSG_SAME_EMAIL)
        if await db.fetchval("select 1 from users where email = $1 and user_id <> $2", new_email, user["user_id"]):
            raise HTTPException(status_code=409, detail=MSG_EMAIL_USED)
        await db.execute("update users set email = $1 where user_id = $2", new_email, user["user_id"])
        tokens = await self._rotate_session(user["user_id"])
        fresh = await db.fetchrow("select * from users where user_id = $1", user["user_id"])
        return {"ok": True, "user": public_user(fresh), **tokens}

    # ------------------------------------------------------------ plan

    @staticmethod
    async def change_plan(req: PlanChangeRequest, user: dict) -> dict:
        """Self-serve plan switch. Upgrades are only allowed with ALLOW_SELF_SERVE_PRO (preview /
        demo); production grants Pro exclusively through a verified payment."""
        new_plan = (req.plan or "").strip().lower()
        if new_plan not in PLANS:
            raise HTTPException(status_code=400, detail=f"plan must be one of {sorted(PLANS)}")
        if new_plan != Plan.FREE and not settings.allow_self_serve_pro:
            raise HTTPException(status_code=403, detail=MSG_UPGRADE_NEEDS_PAYMENT)
        now = _now()
        is_pro = new_plan == Plan.PRO
        expires_at = (now + timedelta(days=settings.pro_term_days)).isoformat() if is_pro else None
        merged = {**(user.get("preferences") or {}), "plan": new_plan,
                  "plan_changed_at": now.isoformat(), "plan_expires_at": expires_at}
        await db.execute("update users set preferences = $1 where user_id = $2", merged, user["user_id"])
        await db.execute(
            """insert into plan_history (user_id, action, plan, amount_inr, starts_at, expires_at, created_at)
               values ($1, $2, $3, $4, $5, $6, $5)""",
            user["user_id"], str(PlanAction.UPGRADED if is_pro else PlanAction.DOWNGRADED), new_plan,
            settings.pro_price_inr if is_pro else None, now.isoformat(), expires_at)
        cache.invalidate(user["user_id"])
        return {**default_preferences(), **merged}

    @staticmethod
    async def plan_history(user_id: str) -> list[dict]:
        return await db.fetch(
            "select action, plan, amount_inr, starts_at, expires_at, created_at from plan_history "
            "where user_id = $1 order by created_at desc limit $2", user_id, PLAN_HISTORY_LIMIT)

    async def grant_pro(self, identifier: str, days: int = 365) -> dict:
        clean_id = (identifier or "").strip()
        if not clean_id:
            raise HTTPException(status_code=400, detail="identifier is required (email, username, or user_id)")
        if days < 1:
            raise HTTPException(status_code=400, detail="days must be at least 1")

        user = await db.fetchrow(
            """
            select user_id, email, username, name, preferences
            from users
            where lower(email) = lower($1) or lower(username) = lower($1) or user_id = $1
            """,
            clean_id,
        )
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{clean_id}' not found")

        now = _now()
        now_iso = now.isoformat()
        expires_at = (now + timedelta(days=days)).isoformat()

        prefs = dict(user.get("preferences") or {})
        prefs.update({
            "plan": "pro",
            "plan_changed_at": now_iso,
            "plan_expires_at": expires_at,
        })

        await db.execute(
            "update users set preferences = $1 where user_id = $2",
            prefs,
            user["user_id"],
        )
        await db.execute(
            """
            insert into plan_history (user_id, action, plan, amount_inr, starts_at, expires_at, created_at)
            values ($1, 'upgraded', 'pro', null, $2, $3, $2)
            """,
            user["user_id"],
            now_iso,
            expires_at,
        )
        cache.invalidate(user["user_id"])

        return {
            "ok": True,
            "message": f"Successfully granted Pro for {days} days",
            "user": {
                "user_id": user["user_id"],
                "email": user["email"],
                "username": user.get("username"),
                "name": user.get("name") or "",
                "plan": "pro",
                "plan_expires_at": expires_at,
            },
        }

    async def block_user(self, identifier: str, reason: str = "") -> dict:
        clean_id = (identifier or "").strip()
        if not clean_id:
            raise HTTPException(status_code=400, detail="identifier is required (email, username, or user_id)")

        user = await db.fetchrow(
            """
            select user_id, email, username, name, preferences, is_blocked
            from users
            where lower(email) = lower($1) or lower(username) = lower($1) or user_id = $1
            """,
            clean_id,
        )
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{clean_id}' not found")

        if (user.get("email") or "").lower() in settings.admin_emails:
            raise HTTPException(status_code=400, detail="Cannot block an admin account")

        now = _now()
        now_iso = now.isoformat()
        reason = (reason or "").strip()

        prefs = dict(user.get("preferences") or {})
        prefs["is_blocked"] = True
        prefs["blocked_at"] = now_iso
        prefs["blocked_reason"] = reason

        await db.execute(
            """
            update users
            set is_blocked = true,
                blocked_at = $1,
                blocked_reason = $2,
                token_version = token_version + 1,
                preferences = $3
            where user_id = $4
            """,
            now_iso,
            reason,
            prefs,
            user["user_id"],
        )

        cache.invalidate(user["user_id"])
        await revoke_all_refresh_tokens(user["user_id"])

        return {
            "ok": True,
            "message": f"User '{user.get('email') or user['user_id']}' has been blocked",
            "user": {
                "user_id": user["user_id"],
                "email": user["email"],
                "username": user.get("username"),
                "name": user.get("name") or "",
                "is_blocked": True,
                "blocked_at": now_iso,
                "blocked_reason": reason,
            },
        }

    async def unblock_user(self, identifier: str) -> dict:
        clean_id = (identifier or "").strip()
        if not clean_id:
            raise HTTPException(status_code=400, detail="identifier is required (email, username, or user_id)")

        user = await db.fetchrow(
            """
            select user_id, email, username, name, preferences, is_blocked
            from users
            where lower(email) = lower($1) or lower(username) = lower($1) or user_id = $1
            """,
            clean_id,
        )
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{clean_id}' not found")

        prefs = dict(user.get("preferences") or {})
        prefs["is_blocked"] = False
        prefs.pop("blocked_at", None)
        prefs.pop("blocked_reason", None)

        await db.execute(
            """
            update users
            set is_blocked = false,
                blocked_at = null,
                blocked_reason = null,
                preferences = $1
            where user_id = $2
            """,
            prefs,
            user["user_id"],
        )

        cache.invalidate(user["user_id"])

        return {
            "ok": True,
            "message": f"User '{user.get('email') or user['user_id']}' has been unblocked",
            "user": {
                "user_id": user["user_id"],
                "email": user["email"],
                "username": user.get("username"),
                "name": user.get("name") or "",
                "is_blocked": False,
            },
        }


    # ------------------------------------------------------------ export / delete

    @staticmethod
    async def export(user: dict) -> dict:
        if ((user.get("preferences") or {}).get("plan") or Plan.FREE) != Plan.PRO:
            raise HTTPException(status_code=403, detail=MSG_EXPORT_PRO)
        reports = await db.fetch(
            "select * from reports where user_id = $1 and deleted_at is null order by uploaded_at desc limit $2",
            user["user_id"], EXPORT_REPORT_LIMIT)
        for r in reports:
            r["id"] = str(r["id"])
        return {"exported_at": _now().isoformat(), "user": public_user(user), "reports": reports}

    @staticmethod
    async def delete_account(user_id: str) -> None:
        """Hard delete. Stored files first (storage_path is comma-joined for multi-page uploads),
        then the users row - FK cascades take out reports, members, tokens, resets, jobs,
        quotas and family rows. This is the privacy-policy promise."""
        # Account deletion must not leave a paid renewal running with no Folin account
        # to receive it. This stops the next charge; it does not issue a refund.
        from services.payments import payments

        await payments.cancel_for_account_deletion(user_id)
        rows = await db.fetch("select storage_path from reports where user_id = $1 and storage_path is not null", user_id)
        await storage.delete_files([p for r in rows for p in r["storage_path"].split(",") if p])
        # Billing rows intentionally have no user FK because Play notifications may
        # arrive after deletion. Remove the account binding and purchase token here so
        # a deleted Folin identity cannot be reconstructed from subscription metadata.
        await db.execute("delete from google_play_account_links where user_id = $1", user_id)
        await db.execute("delete from google_play_subscriptions where user_id = $1", user_id)
        await db.execute("delete from users where user_id = $1", user_id)
        cache.invalidate(user_id)
        cache.invalidate_reports(user_id)


users = UserService()
