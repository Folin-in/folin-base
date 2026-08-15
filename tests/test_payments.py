"""Razorpay verification: signature, amount authority, replay protection.

Order creation calls Razorpay's live test API; verification is pure local crypto, so the
security-critical half runs offline. In-process against the real DB.

    ./venv/bin/python -m pytest tests/test_payments.py -v
"""
import asyncio
import hashlib
import hmac
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from core import rate_limit  # noqa: E402
from db import session as db  # noqa: E402
from services.payments import expected_signature  # noqa: E402
from app.main import app  # noqa: E402

# One loop for the whole run, owned by conftest - see its docstring.
_LOOP = asyncio.get_event_loop()
_ready = False


def run(coro):
    async def _wrapped():
        global _ready
        if not _ready:
            await db.connect()
            _ready = True
        # Each test starts on a fresh throttle window: register is capped at 10 per 10
        # minutes per IP, which a combined run blows through and then fails in a way that
        # looks like a payments bug.
        rate_limit._WINDOWS.clear()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await coro(c)

    return _LOOP.run_until_complete(_wrapped())


def uniq(p="pay"):
    return f"{p}_{uuid.uuid4().hex[:10]}"


async def make_user(c):
    """Register a throwaway user and return (user_id, auth headers)."""
    email = f"{uniq()}@example.com"
    r = await c.post("/api/auth/register", json={
        "email": email, "username": uniq("u"), "password": "Str0ng!Passw0rd",
        "name": "Payer", "consent": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    return body["user"]["user_id"], email, {"Authorization": f"Bearer {body['access_token']}"}


async def seed_order(user_id, order_id, amount=None, status="created"):
    await db.execute(
        """insert into payments (order_id, user_id, amount_inr, currency, status, created_at)
           values ($1, $2, $3, 'INR', $4, '2026-08-08T00:00:00+00:00')""",
        order_id, user_id, amount if amount is not None else settings.pro_price_inr, status,
    )


def sign(order_id, payment_id, secret=None):
    secret = secret or settings.razorpay_key_secret
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


# ------------------------------------------------------------------- signature

def test_signature_matches_razorpay_scheme():
    """Razorpay signs `order_id|payment_id` with the key secret, hex HMAC-SHA256."""
    got = expected_signature("order_x", "pay_y", "secret_z")
    want = hmac.new(b"secret_z", b"order_x|pay_y", hashlib.sha256).hexdigest()
    assert got == want


def test_forged_signature_rejected_and_plan_untouched():
    async def t(c):
        uid, email, h = await make_user(c)
        oid = uniq("order")
        await seed_order(uid, oid)
        r = await c.post("/api/payments/razorpay/verify", headers=h, json={
            "razorpay_order_id": oid, "razorpay_payment_id": "pay_forged",
            "razorpay_signature": "deadbeef" * 8,
        })
        assert r.status_code == 400
        assert "not been charged" in r.json()["detail"]
        row = await db.fetchrow("select status from payments where order_id = $1", oid)
        assert row["status"] == "failed"
        me = (await c.get("/api/auth/me", headers=h)).json()
        assert (me.get("preferences") or {}).get("plan", "free") == "free", "forged signature bought Pro"
        await db.execute("delete from users where email = $1", email)

    run(t)


def test_signature_from_wrong_secret_rejected():
    """An attacker signing with a guessed/other secret must not pass."""
    async def t(c):
        uid, email, h = await make_user(c)
        oid = uniq("order")
        await seed_order(uid, oid)
        r = await c.post("/api/payments/razorpay/verify", headers=h, json={
            "razorpay_order_id": oid, "razorpay_payment_id": "pay_1",
            "razorpay_signature": sign(oid, "pay_1", secret="not-the-real-secret"),
        })
        assert r.status_code == 400
        await db.execute("delete from users where email = $1", email)

    run(t)


def test_valid_signature_activates_pro():
    async def t(c):
        uid, email, h = await make_user(c)
        oid, pid = uniq("order"), uniq("pay")
        await seed_order(uid, oid)
        r = await c.post("/api/payments/razorpay/verify", headers=h, json={
            "razorpay_order_id": oid, "razorpay_payment_id": pid,
            "razorpay_signature": sign(oid, pid),
        })
        assert r.status_code == 200, r.text
        assert r.json()["preferences"]["plan"] == "pro"

        me = (await c.get("/api/auth/me", headers=h)).json()
        assert me["preferences"]["plan"] == "pro"
        assert me["preferences"]["plan_expires_at"]

        hist = (await c.get("/api/user/plan/history", headers=h)).json()
        assert hist[0]["plan"] == "pro" and hist[0]["amount_inr"] == settings.pro_price_inr
        await db.execute("delete from users where email = $1", email)

    run(t)


# --------------------------------------------------------------------- replay

def test_replaying_same_payment_does_not_extend_term():
    async def t(c):
        uid, email, h = await make_user(c)
        oid, pid = uniq("order"), uniq("pay")
        await seed_order(uid, oid)
        body = {"razorpay_order_id": oid, "razorpay_payment_id": pid, "razorpay_signature": sign(oid, pid)}
        first = (await c.post("/api/payments/razorpay/verify", headers=h, json=body)).json()
        second = (await c.post("/api/payments/razorpay/verify", headers=h, json=body)).json()
        assert second["already_applied"] is True
        assert first["preferences"]["plan_expires_at"] == second["preferences"]["plan_expires_at"], \
            "replay extended the Pro term"
        rows = await db.fetch("select count(*) as n from plan_history where user_id = $1", uid)
        assert rows[0]["n"] == 1, "replay wrote a second plan_history row"
        await db.execute("delete from users where email = $1", email)

    run(t)


def test_same_payment_id_cannot_pay_a_second_order():
    """One captured payment must not activate Pro on another order."""
    async def t(c):
        uid, email, h = await make_user(c)
        o1, o2, pid = uniq("order"), uniq("order"), uniq("pay")
        await seed_order(uid, o1)
        await seed_order(uid, o2)
        ok = await c.post("/api/payments/razorpay/verify", headers=h, json={
            "razorpay_order_id": o1, "razorpay_payment_id": pid, "razorpay_signature": sign(o1, pid)})
        assert ok.status_code == 200
        again = await c.post("/api/payments/razorpay/verify", headers=h, json={
            "razorpay_order_id": o2, "razorpay_payment_id": pid, "razorpay_signature": sign(o2, pid)})
        assert again.status_code == 409, "same payment id activated a second order"
        # ...and it must be a clean refusal, not a leaked database error
        assert "duplicate key" not in again.text.lower()
        row2 = await db.fetchrow("select status from payments where order_id = $1", o2)
        assert row2["status"] == "created"
        await db.execute("delete from users where email = $1", email)

    run(t)


# ------------------------------------------------------------------ ownership

def test_cannot_verify_someone_elses_order():
    async def t(c):
        uid_a, email_a, _ = await make_user(c)
        _, email_b, hb = await make_user(c)
        oid, pid = uniq("order"), uniq("pay")
        await seed_order(uid_a, oid)  # belongs to A
        r = await c.post("/api/payments/razorpay/verify", headers=hb, json={
            "razorpay_order_id": oid, "razorpay_payment_id": pid, "razorpay_signature": sign(oid, pid),
        })
        assert r.status_code == 404, "B verified A's order"
        row = await db.fetchrow("select status from payments where order_id = $1", oid)
        assert row["status"] == "created"
        for e in (email_a, email_b):
            await db.execute("delete from users where email = $1", e)

    run(t)


def test_unknown_order_rejected():
    async def t(c):
        _, email, h = await make_user(c)
        r = await c.post("/api/payments/razorpay/verify", headers=h, json={
            "razorpay_order_id": "order_nope", "razorpay_payment_id": "p",
            "razorpay_signature": sign("order_nope", "p"),
        })
        assert r.status_code == 404
        await db.execute("delete from users where email = $1", email)

    run(t)


def test_endpoints_require_auth():
    async def t(c):
        assert (await c.post("/api/payments/razorpay/order")).status_code in (401, 403)
        assert (await c.post("/api/payments/razorpay/verify", json={
            "razorpay_order_id": "o", "razorpay_payment_id": "p", "razorpay_signature": "s",
        })).status_code in (401, 403)

    run(t)


# ---------------------------------------------------------------------- order

def test_order_uses_server_price_and_is_live():
    """Hits Razorpay's test API: the order must come back with OUR price, in paise."""
    async def t(c):
        _, email, h = await make_user(c)
        r = await c.post("/api/payments/razorpay/order", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["amount"] == settings.pro_price_inr * 100
        assert body["currency"] == "INR"
        assert body["key_id"] == settings.razorpay_key_id
        assert body["key_id"].startswith("rzp_test_"), "not a test key"
        assert "secret" not in str(body).lower()

        row = await db.fetchrow("select * from payments where order_id = $1", body["order_id"])
        assert row["amount_inr"] == settings.pro_price_inr and row["status"] == "created"
        await db.execute("delete from users where email = $1", email)

    run(t)


def test_pro_user_cannot_order_again():
    async def t(c):
        uid, email, h = await make_user(c)
        oid, pid = uniq("order"), uniq("pay")
        await seed_order(uid, oid)
        await c.post("/api/payments/razorpay/verify", headers=h, json={
            "razorpay_order_id": oid, "razorpay_payment_id": pid, "razorpay_signature": sign(oid, pid)})
        r = await c.post("/api/payments/razorpay/order", headers=h)
        assert r.status_code == 400 and "already on Pro" in r.json()["detail"]
        await db.execute("delete from users where email = $1", email)

    run(t)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
