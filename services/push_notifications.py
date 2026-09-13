"""Remote retest reminders through the Expo Push Service.

Devices register an Expo push token against the authenticated account. A durable worker
checks Pro users' latest lab reports and sends a privacy-safe reminder at most once per
day or ISO week, according to the user's notification preference. Delivery claims live
in Postgres, so multiple API workers cannot send the same reminder twice.
"""
import asyncio
import logging
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import HTTPException

from app.config import settings
from app.constants import (PUSH_RECEIPT_DELAY_SECS, PUSH_SEND_LOCAL_HOUR, PUSH_WORKER_POLL_SECS,
                           RETEST_AFTER_DAYS)
from app.enums import BiomarkerStatus, Plan, ReportType
from app.models import PushTokenRequest
from core import cache
from db import session as db
from services.gamification import effective_date, sort_by_effective_date
from services.fcm import fcm

logger = logging.getLogger(__name__)

EXPO_SEND_URL = "https://exp.host/--/api/v2/push/send"
EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"
RECEIPT_BATCH = 1000
REMINDER_KEY = "retest_due"
ANALYSIS_KEY = "analysis_complete"
COMPLETION_POLL_SECONDS = 5
MSG_TIMEZONE = "timezone must be a valid IANA timezone, such as Asia/Kolkata"
DEVICE_NOT_REGISTERED = "DeviceNotRegistered"


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if settings.expo_access_token:
        headers["Authorization"] = f"Bearer {settings.expo_access_token}"
    return headers


def _plan_is_active(prefs: dict, now: datetime) -> bool:
    if (prefs.get("plan") or Plan.FREE) != Plan.PRO:
        return False
    expires = prefs.get("plan_expires_at")
    if not expires:
        return True
    try:
        expires_at = datetime.fromisoformat(expires)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at.astimezone(timezone.utc) > now
    except (TypeError, ValueError):
        return False


def _cadence_key(preference: str, local_now: datetime) -> str:
    if preference == "daily":
        return local_now.date().isoformat()
    iso = local_now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class PushNotificationService:
    def __init__(self):
        self._worker_task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._completion_task: asyncio.Task | None = None

    async def register(self, req: PushTokenRequest, user_id: str) -> None:
        try:
            ZoneInfo(req.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(status_code=400, detail=MSG_TIMEZONE) from None
        await db.execute(
            """insert into push_tokens (user_id, token, platform, timezone, active, updated_at, provider)
               values ($1, $2, $3, $4, true, now(), $5)
               on conflict (token) do update set user_id = excluded.user_id,
                 platform = excluded.platform, timezone = excluded.timezone, provider = excluded.provider,
                 active = true, updated_at = now()""",
            user_id, req.token, req.platform, req.timezone, req.provider,
        )
        if self._wake:
            self._wake.set()

    @staticmethod
    async def unregister(token: str, user_id: str) -> None:
        await db.execute(
            "update push_tokens set active = false, updated_at = now() where token = $1 and user_id = $2",
            token, user_id,
        )

    async def start(self) -> None:
        if self._worker_task and not self._worker_task.done():
            return
        self._wake = asyncio.Event()
        self._worker_task = asyncio.create_task(self._worker(), name="folin-push-worker")
        self._completion_task = asyncio.create_task(self._completion_worker(), name="folin-analysis-notifications")

    async def stop(self) -> None:
        if self._completion_task:
            self._completion_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._completion_task
            self._completion_task = None
        if not self._worker_task:
            return
        self._worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker_task
        self._worker_task = None
        self._wake = None

    async def _completion_worker(self) -> None:
        while True:
            try:
                await self.send_completed_analyses()
            except Exception:
                logger.exception("analysis completion notification scan failed")
            await asyncio.sleep(COMPLETION_POLL_SECONDS)

    async def send_completed_analyses(self) -> int:
        # Expire old events, and recover claims abandoned by a crashed sender.
        # No catch-up burst when someone installs the app on a new device.
        await db.execute("delete from analysis_notifications where created_at < now() - interval '24 hours'")
        await db.execute(
            """delete from push_deliveries where reminder_key = $1 and status = 'sending'
               and created_at < now() - interval '2 minutes'""", ANALYSIS_KEY)
        rows = await db.fetch(
            """select p.id, p.user_id, p.token, p.provider, n.report_id
               from analysis_notifications n
               join reports r on r.id = n.report_id and r.deleted_at is null
               join push_tokens p on p.user_id = n.user_id and p.active = true
                 and p.created_at <= n.created_at
               join users u on u.user_id = n.user_id
               where coalesce(u.preferences->>'notifications', 'weekly') in ('daily', 'weekly')
                 and n.next_attempt_at <= now()
                 and not exists (select 1 from push_deliveries d where d.push_token_id = p.id
                   and d.reminder_key = $1 and d.cadence_key = n.report_id::text)
               order by n.created_at limit 100""", ANALYSIS_KEY)
        sent = 0
        for token in rows:
            report_id = str(token["report_id"])
            delivery = await db.fetchrow(
                """insert into push_deliveries (push_token_id,user_id,reminder_key,cadence_key,status)
                   values ($1,$2,$3,$4,'sending')
                   on conflict (push_token_id,reminder_key,cadence_key) do nothing returning id""",
                token["id"], token["user_id"], ANALYSIS_KEY, report_id)
            if not delivery:
                continue
            if await self._send(token, delivery["id"], {
                "title": "Analysis complete",
                "body": "Your report is ready. Tap to view it in Folin.",
                "channelId": "analysis-results",
                "data": {"type": ANALYSIS_KEY, "report_id": report_id},
            }):
                sent += 1
            else:
                # Service/credential errors must not retry every five seconds.
                await db.execute(
                    "update analysis_notifications set next_attempt_at = now() + interval '5 minutes' where report_id = $1",
                    token["report_id"])
        return sent

    async def _worker(self) -> None:
        while True:
            try:
                await self.run_once()
                if self._wake:
                    self._wake.clear()
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=PUSH_WORKER_POLL_SECS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("push notification worker failed")
                await asyncio.sleep(PUSH_WORKER_POLL_SECS)

    async def run_once(self, now: datetime | None = None) -> int:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        # A process can die after claiming a delivery but before contacting Expo.
        # Release only claims far older than the 15s HTTP timeout.
        await db.execute(
            "delete from push_deliveries where status = 'sending' and created_at < $1",
            now - timedelta(seconds=PUSH_WORKER_POLL_SECS * 2),
        )
        await self._check_receipts(now)
        tokens = await db.fetch(
            """select p.id, p.user_id, p.token, p.platform, p.timezone, p.provider, u.preferences
               from push_tokens p join users u on u.user_id = p.user_id
               where p.active = true order by p.created_at asc limit 5000"""
        )
        due_by_user: dict[tuple[str, date], bool] = {}
        sent = 0
        for token in tokens:
            prefs = token.get("preferences") or {}
            preference = prefs.get("notifications") or "weekly"
            if preference not in {"daily", "weekly"} or not _plan_is_active(prefs, now):
                continue
            try:
                local_now = now.astimezone(ZoneInfo(token.get("timezone") or "UTC"))
            except ZoneInfoNotFoundError:
                local_now = now
            if local_now.hour < PUSH_SEND_LOCAL_HOUR:
                continue
            uid = token["user_id"]
            due_key = (uid, local_now.date())
            if due_key not in due_by_user:
                due_by_user[due_key] = await self._has_due_retest(uid, local_now.date())
            if not due_by_user[due_key]:
                continue
            delivery = await db.fetchrow(
                """insert into push_deliveries
                   (push_token_id, user_id, reminder_key, cadence_key, status)
                   values ($1, $2, $3, $4, 'sending')
                   on conflict (push_token_id, reminder_key, cadence_key) do nothing
                   returning id""",
                token["id"], uid, REMINDER_KEY, _cadence_key(preference, local_now),
            )
            if not delivery:
                continue
            if await self._send(token, delivery["id"]):
                sent += 1
        return sent

    @staticmethod
    async def _has_due_retest(user_id: str, today: date) -> bool:
        reports = await cache.get_reports(user_id)
        by_member: dict[str, list[dict]] = {}
        for report in reports:
            if report.get("report_type") == ReportType.LAB:
                by_member.setdefault(report.get("member_id") or "self", []).append(report)
        for docs in by_member.values():
            latest = sort_by_effective_date(docs, reverse=True)[0]
            raw_date = effective_date(latest)
            try:
                old_enough = bool(raw_date) and (today - date.fromisoformat(raw_date)).days >= RETEST_AFTER_DAYS
            except (TypeError, ValueError):
                old_enough = False
            if old_enough and any(
                biomarker.get("status") != BiomarkerStatus.NORMAL
                for biomarker in (latest.get("biomarkers") or [])
            ):
                return True
        return False

    async def _send(self, token: dict, delivery_id, content: dict | None = None) -> bool:
        message = {
            "to": token["token"],
            "title": "Time for a follow-up",
            "body": "A saved lab report is ready for a retest check-in. Open Folin to review it.",
            "sound": "default",
            "channelId": "retest-reminders",
            "data": {"type": "retest", "url": "/upload"},
        }
        if content:
            message.update(content)
        if token.get("provider") == "fcm":
            try:
                message_id, error = await fcm.send(token["token"], message)
                if message_id:
                    # FCM accepted the message; this is not proof of device delivery.
                    await db.execute(
                        "update push_deliveries set status = 'accepted', ticket_id = $1 where id = $2",
                        message_id, delivery_id)
                    return True
                if error == "UNREGISTERED":
                    await self._deactivate(token["id"])
                    await db.execute("update push_deliveries set status = 'error', error = $1 where id = $2", error, delivery_id)
                else:
                    await db.execute("delete from push_deliveries where id = $1", delivery_id)
                logger.warning("FCM rejected notification: %s", error)
            except Exception as exc:
                await db.execute("delete from push_deliveries where id = $1", delivery_id)
                # Never log credential contents, bearer tokens or device tokens.
                logger.warning("FCM send failed: %s", type(exc).__name__)
            return False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(EXPO_SEND_URL, headers=_headers(), json=message)
                response.raise_for_status()
                ticket = response.json().get("data") or {}
            if isinstance(ticket, list):
                ticket = ticket[0] if ticket else {}
            if ticket.get("status") == "ok" and ticket.get("id"):
                await db.execute(
                    "update push_deliveries set status = 'ticketed', ticket_id = $1 where id = $2",
                    ticket["id"], delivery_id,
                )
                return True
            error = (ticket.get("details") or {}).get("error") or ticket.get("message") or "Expo rejected push"
            if error == DEVICE_NOT_REGISTERED:
                await self._deactivate(token["id"])
                await db.execute(
                    "update push_deliveries set status = 'error', error = $1 where id = $2", error, delivery_id)
            else:
                # Credential/service failures may be temporary. Remove the claim so the
                # next worker pass retries rather than suppressing this cadence forever.
                await db.execute("delete from push_deliveries where id = $1", delivery_id)
            logger.warning("Expo push rejected for token %s: %s", token["id"], error)
            return False
        except Exception as exc:
            await db.execute("delete from push_deliveries where id = $1", delivery_id)
            logger.warning("Expo push request failed for token %s: %s", token["id"], exc)
            return False

    async def _check_receipts(self, now: datetime) -> None:
        await db.execute(
            """update push_deliveries set status = 'unknown', error = 'Expo receipt expired'
               where status = 'ticketed' and receipt_checked_at is null and created_at < $1""",
            now - timedelta(hours=24),
        )
        cutoff = now - timedelta(seconds=PUSH_RECEIPT_DELAY_SECS)
        rows = await db.fetch(
            """select d.id, d.ticket_id, d.push_token_id
               from push_deliveries d
               where d.status = 'ticketed' and d.receipt_checked_at is null
                 and d.created_at <= $1 order by d.created_at asc limit $2""",
            cutoff, RECEIPT_BATCH,
        )
        if not rows:
            return
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    EXPO_RECEIPTS_URL, headers=_headers(),
                    json={"ids": [row["ticket_id"] for row in rows]},
                )
                response.raise_for_status()
                receipts = response.json().get("data") or {}
        except Exception as exc:
            logger.warning("Expo receipt request failed: %s", exc)
            return
        for row in rows:
            receipt = receipts.get(row["ticket_id"])
            if not receipt:
                continue
            error = (receipt.get("details") or {}).get("error") or receipt.get("message")
            status = "delivered" if receipt.get("status") == "ok" else "error"
            await db.execute(
                """update push_deliveries set status = $1, error = $2,
                   receipt_checked_at = now() where id = $3""",
                status, error, row["id"],
            )
            if error == DEVICE_NOT_REGISTERED:
                await self._deactivate(row["push_token_id"])

    @staticmethod
    async def _deactivate(token_id) -> None:
        await db.execute("update push_tokens set active = false, updated_at = now() where id = $1", token_id)


push_notifications = PushNotificationService()
