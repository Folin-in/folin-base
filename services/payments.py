"""Google Play subscription verification and entitlement reconciliation.

The app may start the Play purchase UI, but it can never grant itself Pro. Every
purchase token is checked with the Google Play Developer API, bound to the Folin
account that started checkout, acknowledged, and then persisted. Real-time Developer
Notifications run through the same reconciliation path for renewals, cancellations,
payment failures, expiries, refunds, and revocations.
"""

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token, service_account

from app.config import settings
from app.enums import Plan, PlanAction
from app.models import GooglePlayRtdnEnvelope, GooglePlayVerifyRequest
from core import cache
from db import session as db

ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
ANDROID_PUBLISHER_API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
GOOGLE_PLAY_TIMEOUT_SECS = 20

ACCESS_STATES = {
    "SUBSCRIPTION_STATE_ACTIVE",
    "SUBSCRIPTION_STATE_CANCELED",  # paid access lasts until expires_at
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",  # Google says entitlement remains active
}
NO_ACCESS_STATES = {
    "SUBSCRIPTION_STATE_EXPIRED",
    "SUBSCRIPTION_STATE_ON_HOLD",
    "SUBSCRIPTION_STATE_PAUSED",
    "SUBSCRIPTION_STATE_PENDING",
    "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED",
}

MSG_NOT_CONFIGURED = "Google Play subscriptions are not configured yet."
MSG_PROVIDER_DOWN = (
    "Couldn't verify the subscription with Google Play. Please try again."
)
MSG_INVALID_PURCHASE = "Google Play could not verify this subscription."
MSG_WRONG_ACCOUNT = "This subscription belongs to a different Folin account."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _now().isoformat()


def account_hash(user_id: str) -> str:
    """Stable, non-PII Play obfuscatedAccountId (SHA-256 is 64 characters)."""
    return hashlib.sha256(f"folin:google-play:{user_id}".encode()).hexdigest()


def _parse_google_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GooglePlayPaymentService:
    def __init__(self) -> None:
        self._credentials = None
        self._credentials_lock = asyncio.Lock()

    @staticmethod
    def configured() -> bool:
        return bool(
            settings.google_play_package_name
            and settings.google_play_pro_product_id
            and settings.google_play_service_account_json
        )

    async def config_for_user(self, user: dict) -> dict:
        hashed = account_hash(user["user_id"])
        now = _iso_now()
        await db.execute(
            """insert into google_play_account_links (account_hash, user_id, created_at, updated_at)
               values ($1, $2, $3, $3)
               on conflict (user_id) do update
               set account_hash = excluded.account_hash, updated_at = excluded.updated_at""",
            hashed,
            user["user_id"],
            now,
        )
        return {
            "configured": self.configured(),
            "package_name": settings.google_play_package_name,
            "product_id": settings.google_play_pro_product_id,
            "base_plan_id": settings.google_play_pro_base_plan_id or None,
            "obfuscated_account_id": hashed,
        }

    def _service_account_info(self) -> dict:
        raw = settings.google_play_service_account_json
        if not raw:
            raise HTTPException(status_code=503, detail=MSG_NOT_CONFIGURED)
        try:
            # One-line JSON is simplest. Base64 is accepted for hosts whose secret
            # editors make the private key's embedded newlines awkward.
            decoded = (
                raw if raw.lstrip().startswith("{") else base64.b64decode(raw).decode()
            )
            info = json.loads(decoded)
            if not isinstance(info, dict):
                raise ValueError
            return info
        except (ValueError, TypeError, json.JSONDecodeError):
            raise HTTPException(status_code=503, detail=MSG_NOT_CONFIGURED) from None

    async def _access_token(self) -> str:
        async with self._credentials_lock:
            if self._credentials is None:
                try:
                    self._credentials = (
                        service_account.Credentials.from_service_account_info(
                            self._service_account_info(),
                            scopes=[ANDROID_PUBLISHER_SCOPE],
                        )
                    )
                except (ValueError, KeyError):
                    raise HTTPException(
                        status_code=503, detail=MSG_NOT_CONFIGURED
                    ) from None
            if not self._credentials.valid:
                try:
                    await asyncio.to_thread(
                        self._credentials.refresh, GoogleAuthRequest()
                    )
                except Exception:
                    raise HTTPException(
                        status_code=503, detail=MSG_PROVIDER_DOWN
                    ) from None
            return self._credentials.token

    async def _google_request(self, method: str, url: str, **kwargs) -> dict:
        token = await self._access_token()
        try:
            async with httpx.AsyncClient(timeout=GOOGLE_PLAY_TIMEOUT_SECS) as client:
                response = await client.request(
                    method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
                )
        except httpx.HTTPError:
            raise HTTPException(status_code=503, detail=MSG_PROVIDER_DOWN) from None
        if response.status_code == 404:
            raise HTTPException(status_code=400, detail=MSG_INVALID_PURCHASE)
        if response.status_code >= 400:
            raise HTTPException(status_code=503, detail=MSG_PROVIDER_DOWN)
        return response.json() if response.content else {}

    async def _get_subscription(self, purchase_token: str) -> dict:
        if not self.configured():
            raise HTTPException(status_code=503, detail=MSG_NOT_CONFIGURED)
        package = quote(settings.google_play_package_name, safe="")
        token = quote(purchase_token, safe="")
        return await self._google_request(
            "GET",
            f"{ANDROID_PUBLISHER_API}/applications/{package}/purchases/subscriptionsv2/tokens/{token}",
        )

    async def _acknowledge(self, purchase_token: str, product_id: str) -> None:
        package = quote(settings.google_play_package_name, safe="")
        product = quote(product_id, safe="")
        token = quote(purchase_token, safe="")
        await self._google_request(
            "POST",
            f"{ANDROID_PUBLISHER_API}/applications/{package}/purchases/subscriptions/{product}/tokens/{token}:acknowledge",
            json={},
        )

    @staticmethod
    def _snapshot(subscription: dict) -> dict:
        state = (
            subscription.get("subscriptionState") or "SUBSCRIPTION_STATE_UNSPECIFIED"
        )
        expected_product = settings.google_play_pro_product_id
        expected_base_plan = settings.google_play_pro_base_plan_id
        candidates = []
        for item in subscription.get("lineItems") or []:
            if item.get("productId") != expected_product:
                continue
            base_plan = (item.get("offerDetails") or {}).get("basePlanId") or ""
            if expected_base_plan and base_plan != expected_base_plan:
                continue
            candidates.append(item)
        if not candidates:
            raise HTTPException(status_code=400, detail=MSG_INVALID_PURCHASE)
        item = max(
            candidates,
            key=lambda row: _parse_google_time(row.get("expiryTime"))
            or datetime.min.replace(tzinfo=timezone.utc),
        )
        expiry = _parse_google_time(item.get("expiryTime"))
        has_access = state in ACCESS_STATES and bool(expiry and expiry > _now())
        auto_renewing = bool(
            (item.get("autoRenewingPlan") or {}).get("autoRenewEnabled")
        )
        return {
            "state": state,
            "status": state.removeprefix("SUBSCRIPTION_STATE_").lower(),
            "has_access": has_access,
            "product_id": item["productId"],
            "base_plan_id": (
                (item.get("offerDetails") or {}).get("basePlanId") or None
            ),
            "expires_at": expiry.isoformat() if expiry else None,
            "auto_renewing": auto_renewing,
            "acknowledged": subscription.get("acknowledgementState")
            == "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
            "latest_order_id": subscription.get("latestOrderId"),
            "external_account_id": (
                (subscription.get("externalAccountIdentifiers") or {}).get(
                    "obfuscatedExternalAccountId"
                )
            ),
        }

    async def _apply_snapshot(
        self,
        purchase_token: str,
        user_id: str,
        snapshot: dict,
        notification_type: int | None = None,
    ) -> dict:
        existing = await db.fetchrow(
            "select * from google_play_subscriptions where purchase_token = $1",
            purchase_token,
        )
        if existing and existing["user_id"] != user_id:
            raise HTTPException(status_code=409, detail=MSG_WRONG_ACCOUNT)

        user = await db.fetchrow("select * from users where user_id = $1", user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Account not found.")

        now = _iso_now()
        prefs = user.get("preferences") or {}
        previous_plan = prefs.get("plan", str(Plan.FREE))
        next_plan = str(Plan.PRO if snapshot["has_access"] else Plan.FREE)
        next_expiry = snapshot["expires_at"] if snapshot["has_access"] else None
        merged = {
            **prefs,
            "plan": next_plan,
            "plan_changed_at": now,
            "plan_expires_at": next_expiry,
            "subscription_provider": "google_play",
            "subscription_status": snapshot["status"],
            "subscription_auto_renews": snapshot["auto_renewing"],
            "subscription_product_id": snapshot["product_id"],
            "subscription_order_id": snapshot["latest_order_id"],
        }

        await db.execute(
            """insert into google_play_subscriptions
                 (purchase_token, user_id, product_id, base_plan_id, latest_order_id, status,
                  auto_renewing, acknowledged, expires_at, created_at, updated_at)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10)
               on conflict (purchase_token) do update set
                 product_id = excluded.product_id,
                 base_plan_id = excluded.base_plan_id,
                 latest_order_id = excluded.latest_order_id,
                 status = excluded.status,
                 auto_renewing = excluded.auto_renewing,
                 acknowledged = excluded.acknowledged,
                 expires_at = excluded.expires_at,
                 updated_at = excluded.updated_at""",
            purchase_token,
            user_id,
            snapshot["product_id"],
            snapshot["base_plan_id"],
            snapshot["latest_order_id"],
            snapshot["status"],
            snapshot["auto_renewing"],
            snapshot["acknowledged"],
            snapshot["expires_at"],
            now,
        )
        await db.execute(
            "update users set preferences = $1 where user_id = $2", merged, user_id
        )

        action = None
        if previous_plan != str(Plan.PRO) and next_plan == str(Plan.PRO):
            action = PlanAction.UPGRADED
        elif previous_plan == str(Plan.PRO) and next_plan == str(Plan.FREE):
            action = (
                PlanAction.REVOKED if notification_type == 12 else PlanAction.EXPIRED
            )
        elif (
            existing
            and next_plan == str(Plan.PRO)
            and existing.get("latest_order_id") != snapshot["latest_order_id"]
        ):
            action = PlanAction.RENEWED
        if action:
            await db.execute(
                """insert into plan_history
                     (user_id, action, plan, amount_inr, starts_at, expires_at, created_at)
                   values ($1,$2,$3,null,$4,$5,$4)""",
                user_id,
                str(action),
                next_plan,
                now,
                next_expiry,
            )
        cache.invalidate(user_id)
        return {
            "ok": True,
            "status": snapshot["status"],
            "has_access": snapshot["has_access"],
            "auto_renewing": snapshot["auto_renewing"],
            "preferences": merged,
        }

    async def verify(self, req: GooglePlayVerifyRequest, user: dict) -> dict:
        existing = await db.fetchrow(
            "select user_id from google_play_subscriptions where purchase_token = $1",
            req.purchase_token,
        )
        if existing and existing["user_id"] != user["user_id"]:
            raise HTTPException(status_code=409, detail=MSG_WRONG_ACCOUNT)

        subscription = await self._get_subscription(req.purchase_token)
        snapshot = self._snapshot(subscription)
        if snapshot["external_account_id"] != account_hash(user["user_id"]):
            raise HTTPException(status_code=409, detail=MSG_WRONG_ACCOUNT)
        if snapshot["state"] in NO_ACCESS_STATES or not snapshot["has_access"]:
            return await self._apply_snapshot(
                req.purchase_token, user["user_id"], snapshot
            )
        if not snapshot["acknowledged"]:
            await self._acknowledge(req.purchase_token, snapshot["product_id"])
            snapshot["acknowledged"] = True
        return await self._apply_snapshot(req.purchase_token, user["user_id"], snapshot)

    async def sync(self, user: dict) -> dict:
        row = await db.fetchrow(
            """select purchase_token from google_play_subscriptions
               where user_id = $1 order by updated_at desc limit 1""",
            user["user_id"],
        )
        if not row:
            return {
                "ok": True,
                "has_purchase": False,
                "preferences": user.get("preferences") or {},
            }
        return await self.verify(
            GooglePlayVerifyRequest(purchase_token=row["purchase_token"]), user
        )

    async def cancel_for_account_deletion(self, user_id: str) -> None:
        """Stop the next Play renewal before permanently deleting the Folin account."""
        row = await db.fetchrow(
            """select purchase_token from google_play_subscriptions
               where user_id = $1 and auto_renewing = true
               order by updated_at desc limit 1""",
            user_id,
        )
        if not row:
            return
        try:
            snapshot = self._snapshot(
                await self._get_subscription(row["purchase_token"])
            )
        except HTTPException as exc:
            # A token Google no longer knows cannot renew, so it must not prevent
            # the user from deleting their Folin account.
            if exc.status_code == 400:
                return
            raise
        if not snapshot["auto_renewing"]:
            return
        package = quote(settings.google_play_package_name, safe="")
        token = quote(row["purchase_token"], safe="")
        await self._google_request(
            "POST",
            f"{ANDROID_PUBLISHER_API}/applications/{package}/purchases/subscriptionsv2/tokens/{token}:cancel",
            json={
                "cancellationContext": {
                    "cancellationType": "USER_REQUESTED_STOP_RENEWALS"
                }
            },
        )

    async def _verify_pubsub_auth(self, authorization: str | None) -> None:
        audience = settings.google_play_rtdn_audience
        expected_email = settings.google_play_pubsub_service_account_email
        if not audience or not expected_email:
            raise HTTPException(
                status_code=503, detail="Google Play notifications are not configured."
            )
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401, detail="Invalid notification authentication."
            )
        token = authorization.removeprefix("Bearer ").strip()
        try:
            claims = await asyncio.to_thread(
                id_token.verify_oauth2_token, token, GoogleAuthRequest(), audience
            )
        except Exception:
            raise HTTPException(
                status_code=401, detail="Invalid notification authentication."
            ) from None
        if (
            not claims.get("email_verified")
            or (claims.get("email") or "").lower() != expected_email
        ):
            raise HTTPException(
                status_code=401, detail="Invalid notification authentication."
            )

    async def process_rtdn(
        self, envelope: GooglePlayRtdnEnvelope, authorization: str | None
    ) -> None:
        await self._verify_pubsub_auth(authorization)
        try:
            payload = json.loads(base64.b64decode(envelope.message.data, validate=True))
        except (ValueError, TypeError, json.JSONDecodeError):
            raise HTTPException(
                status_code=400, detail="Invalid notification payload."
            ) from None
        if payload.get("packageName") != settings.google_play_package_name:
            raise HTTPException(status_code=400, detail="Unexpected package name.")

        notification = payload.get("subscriptionNotification")
        if not notification:  # Google sends a testNotification during setup.
            return
        purchase_token = notification.get("purchaseToken")
        notification_type = notification.get("notificationType")
        if not purchase_token:
            raise HTTPException(status_code=400, detail="Missing purchase token.")

        message_id = envelope.message.message_id
        if message_id and await db.fetchval(
            "select 1 from google_play_notifications where message_id = $1", message_id
        ):
            return

        subscription = await self._get_subscription(purchase_token)
        snapshot = self._snapshot(subscription)
        row = await db.fetchrow(
            "select user_id from google_play_subscriptions where purchase_token = $1",
            purchase_token,
        )
        if not row and snapshot["external_account_id"]:
            row = await db.fetchrow(
                "select user_id from google_play_account_links where account_hash = $1",
                snapshot["external_account_id"],
            )
        if row:
            if not snapshot["acknowledged"] and snapshot["state"] in ACCESS_STATES:
                await self._acknowledge(purchase_token, snapshot["product_id"])
                snapshot["acknowledged"] = True
            await self._apply_snapshot(
                purchase_token, row["user_id"], snapshot, notification_type
            )

        if message_id:
            await db.execute(
                """insert into google_play_notifications
                     (message_id, purchase_token, notification_type, processed_at)
                   values ($1,$2,$3,$4) on conflict (message_id) do nothing""",
                message_id,
                purchase_token,
                notification_type,
                _iso_now(),
            )


payments = GooglePlayPaymentService()
