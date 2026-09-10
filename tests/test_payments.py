"""Google Play subscription ownership and entitlement tests."""

import asyncio
import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from app.config import settings as app_settings  # noqa: E402
from app.main import app  # noqa: E402
from core import rate_limit  # noqa: E402
from db import session as db  # noqa: E402
from services import payments as payment_module  # noqa: E402

_LOOP = asyncio.get_event_loop()
_ready = False


def run(coro):
    async def _wrapped():
        global _ready
        if not _ready:
            await db.connect()
            _ready = True
        rate_limit._WINDOWS.clear()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            return await coro(client)

    return _LOOP.run_until_complete(_wrapped())


def uniq(prefix="play"):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def make_user(client):
    email = f"{uniq()}@example.com"
    response = await client.post(
        "/api/auth/register",
        json={
            "email": email,
            "username": uniq("u"),
            "password": "Str0ng!Passw0rd",
            "name": "Subscriber",
            "consent": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return (
        body["user"]["user_id"],
        email,
        {"Authorization": f"Bearer {body['access_token']}"},
    )


def play_payload(
    user_id: str, *, state="SUBSCRIPTION_STATE_ACTIVE", auto_renew=True, days=30
):
    expiry = (
        (datetime.now(timezone.utc) + timedelta(days=days))
        .isoformat()
        .replace("+00:00", "Z")
    )
    return {
        "subscriptionState": state,
        "acknowledgementState": "ACKNOWLEDGEMENT_STATE_PENDING",
        "latestOrderId": uniq("GPA"),
        "externalAccountIdentifiers": {
            "obfuscatedExternalAccountId": payment_module.account_hash(user_id),
        },
        "lineItems": [
            {
                "productId": "folin_pro",
                "expiryTime": expiry,
                "autoRenewingPlan": {"autoRenewEnabled": auto_renew},
                "offerDetails": {"basePlanId": "monthly"},
            }
        ],
    }


@pytest.fixture(autouse=True)
def configured_play(monkeypatch):
    configured = replace(
        app_settings,
        google_play_package_name="com.folin.app",
        google_play_pro_product_id="folin_pro",
        google_play_pro_base_plan_id="monthly",
        google_play_service_account_json='{"configured":"for mocked tests"}',
    )
    monkeypatch.setattr(payment_module, "settings", configured)


def test_account_hash_is_stable_non_pii_play_identifier():
    user_id = "user-123"
    got = payment_module.account_hash(user_id)
    assert got == hashlib.sha256(b"folin:google-play:user-123").hexdigest()
    assert len(got) == 64 and user_id not in got


def test_config_returns_product_and_account_binding(monkeypatch):
    async def scenario(client):
        uid, email, headers = await make_user(client)
        response = await client.get("/api/payments/google-play/config", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["configured"] is True
        assert body["product_id"] == "folin_pro"
        assert body["base_plan_id"] == "monthly"
        assert body["obfuscated_account_id"] == payment_module.account_hash(uid)
        await db.execute("delete from users where email = $1", email)

    run(scenario)


def test_verified_active_purchase_grants_pro_and_acknowledges(monkeypatch):
    async def scenario(client):
        uid, email, headers = await make_user(client)
        payload = play_payload(uid)
        acknowledged = []

        async def fake_get(_token):
            return payload

        async def fake_ack(token, product):
            acknowledged.append((token, product))

        monkeypatch.setattr(payment_module.payments, "_get_subscription", fake_get)
        monkeypatch.setattr(payment_module.payments, "_acknowledge", fake_ack)
        token = uniq("token")
        response = await client.post(
            "/api/payments/google-play/verify",
            headers=headers,
            json={"purchase_token": token},
        )
        assert response.status_code == 200, response.text
        assert response.json()["has_access"] is True
        assert response.json()["preferences"]["plan"] == "pro"
        assert acknowledged == [(token, "folin_pro")]
        stored = await db.fetchrow(
            "select * from google_play_subscriptions where purchase_token = $1", token
        )
        assert stored["user_id"] == uid and stored["acknowledged"] is True
        await db.execute("delete from users where email = $1", email)

    run(scenario)


def test_cancelled_purchase_keeps_access_until_expiry(monkeypatch):
    async def scenario(client):
        uid, email, headers = await make_user(client)
        payload = play_payload(
            uid, state="SUBSCRIPTION_STATE_CANCELED", auto_renew=False
        )

        async def fake_get(_token):
            return payload

        async def fake_ack(_token, _product):
            return None

        monkeypatch.setattr(payment_module.payments, "_get_subscription", fake_get)
        monkeypatch.setattr(payment_module.payments, "_acknowledge", fake_ack)
        response = await client.post(
            "/api/payments/google-play/verify",
            headers=headers,
            json={"purchase_token": uniq("token")},
        )
        body = response.json()
        assert response.status_code == 200
        assert body["has_access"] is True
        assert body["auto_renewing"] is False
        assert body["preferences"]["plan"] == "pro"
        await db.execute("delete from users where email = $1", email)

    run(scenario)


def test_purchase_cannot_be_claimed_by_another_folin_account(monkeypatch):
    async def scenario(client):
        uid_a, email_a, headers_a = await make_user(client)
        _, email_b, headers_b = await make_user(client)
        payload = play_payload(uid_a)

        async def fake_get(_token):
            return payload

        async def fake_ack(_token, _product):
            return None

        monkeypatch.setattr(payment_module.payments, "_get_subscription", fake_get)
        monkeypatch.setattr(payment_module.payments, "_acknowledge", fake_ack)
        token = uniq("token")
        assert (
            await client.post(
                "/api/payments/google-play/verify",
                headers=headers_a,
                json={"purchase_token": token},
            )
        ).status_code == 200
        denied = await client.post(
            "/api/payments/google-play/verify",
            headers=headers_b,
            json={"purchase_token": token},
        )
        assert denied.status_code == 409
        for email in (email_a, email_b):
            await db.execute("delete from users where email = $1", email)

    run(scenario)


def test_expired_purchase_removes_pro(monkeypatch):
    async def scenario(client):
        uid, email, headers = await make_user(client)
        active = play_payload(uid)
        expired = play_payload(
            uid, state="SUBSCRIPTION_STATE_EXPIRED", auto_renew=False, days=-1
        )

        async def fake_ack(_token, _product):
            return None

        monkeypatch.setattr(payment_module.payments, "_acknowledge", fake_ack)
        token = uniq("token")

        async def active_get(_token):
            return active

        monkeypatch.setattr(payment_module.payments, "_get_subscription", active_get)
        first = await client.post(
            "/api/payments/google-play/verify",
            headers=headers,
            json={"purchase_token": token},
        )
        assert first.json()["preferences"]["plan"] == "pro"

        async def expired_get(_token):
            return expired

        monkeypatch.setattr(payment_module.payments, "_get_subscription", expired_get)
        second = await client.post(
            "/api/payments/google-play/verify",
            headers=headers,
            json={"purchase_token": token},
        )
        assert second.json()["has_access"] is False
        assert second.json()["preferences"]["plan"] == "free"
        await db.execute("delete from users where email = $1", email)

    run(scenario)


def test_endpoints_require_auth():
    async def scenario(client):
        assert (await client.get("/api/payments/google-play/config")).status_code in (
            401,
            403,
        )
        assert (
            await client.post(
                "/api/payments/google-play/verify",
                json={"purchase_token": "token_long_enough"},
            )
        ).status_code in (401, 403)

    run(scenario)
