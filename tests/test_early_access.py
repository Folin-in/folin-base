"""Web early-access signup, confirmation, cooldown, and failure boundaries."""
import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException, Request

from app.main import app
from db import session as db
from services import early_access as early_access_module

_LOOP = asyncio.get_event_loop()
_ready = False


def run(coro):
    async def wrapped():
        global _ready
        if not _ready:
            await db.connect()
            _ready = True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await coro(client)
    return _LOOP.run_until_complete(wrapped())


@pytest.fixture
def email_sender(monkeypatch):
    sent = []
    namespace = uuid.uuid4().hex

    async def verify(*_):
        return None

    async def send(email, url):
        sent.append((email, url))
        return f"email_{uuid.uuid4().hex}"

    monkeypatch.setattr(early_access_module, "_require_signup_config", lambda: None)
    monkeypatch.setattr(early_access_module, "_verify_turnstile", verify)
    monkeypatch.setattr(early_access_module, "send_early_access_confirmation", send)
    monkeypatch.setattr(early_access_module, "_ip_key", lambda *_: f"test-ip:{namespace}")
    monkeypatch.setattr(early_access_module, "_burst_key", lambda *_: f"test-burst:{namespace}")
    monkeypatch.setattr(early_access_module, "_global_quota_key", lambda *_: f"test-global:{namespace}")
    yield sent
    if _ready:
        _LOOP.run_until_complete(db.execute(
            "delete from early_access_limits where key = any($1::text[])",
            [f"test-ip:{namespace}", f"test-burst:{namespace}", f"test-global:{namespace}"],
        ))


def test_signup_is_uniform_deduplicated_and_confirmable(email_sender):
    async def scenario(client):
        email = f"early_{uuid.uuid4().hex}@example.com"
        payload = {"email": email, "turnstile_token": "test-token", "website": ""}
        first = await client.post("/api/early-access", json=payload)
        second = await client.post("/api/early-access", json=payload)

        assert first.status_code == second.status_code == 202
        assert first.json() == second.json() == {"status": "check_your_email"}
        assert len(email_sender) == 1

        row = await db.fetchrow("select * from early_access_signups where email = $1", email)
        assert row["status"] == "pending"
        assert row["confirmation_token_hash"]

        token = parse_qs(urlparse(email_sender[0][1]).query)["token"][0]
        confirmed = await client.get("/api/early-access/confirm", params={"token": token})
        assert confirmed.status_code == 303
        assert "early_access=confirmed" in confirmed.headers["location"]
        assert await db.fetchval(
            "select status from early_access_signups where email = $1", email,
        ) == "confirmed"

        replay = await client.get("/api/early-access/confirm", params={"token": token})
        assert "early_access=invalid" in replay.headers["location"]
        await db.execute("delete from early_access_signups where email = $1", email)

    run(scenario)


def test_honeypot_submission_does_no_work(monkeypatch):
    monkeypatch.setattr(
        early_access_module, "_require_signup_config",
        lambda: (_ for _ in ()).throw(AssertionError("configuration should not be checked")),
    )

    async def scenario(client):
        email = f"bot_{uuid.uuid4().hex}@example.com"
        response = await client.post("/api/early-access", json={
            "email": email, "turnstile_token": "test-token", "website": "spam.example",
        })
        assert response.status_code == 202
        assert await db.fetchval(
            "select count(*) from early_access_signups where email = $1", email,
        ) == 0

    run(scenario)


def test_provider_failure_rolls_back_signup_and_daily_send(email_sender, monkeypatch):
    async def fail_send(*_):
        return None

    monkeypatch.setattr(early_access_module, "send_early_access_confirmation", fail_send)

    async def scenario(client):
        email = f"failure_{uuid.uuid4().hex}@example.com"
        response = await client.post("/api/early-access", json={
            "email": email, "turnstile_token": "test-token", "website": "",
        })
        assert response.status_code == 503
        assert await db.fetchval(
            "select count(*) from early_access_signups where email = $1", email,
        ) == 0
        # The fixture isolates this test's counter; unrelated real send counters
        # must not influence the rollback assertion.
        assert await db.fetchval(
            "select count from early_access_limits where key = $1",
            early_access_module._global_quota_key(datetime.now(timezone.utc)),
        ) == 0

    run(scenario)


def test_ip_limit_blocks_before_email_send(email_sender, monkeypatch):
    monkeypatch.setattr(early_access_module, "settings", replace(
        early_access_module.settings, early_access_ip_minute_limit=100,
    ))
    async def scenario(client):
        emails = [f"limit_{uuid.uuid4().hex}@example.com" for _ in range(6)]
        responses = []
        for email in emails:
            responses.append(await client.post("/api/early-access", json={
                "email": email, "turnstile_token": "test-token", "website": "",
            }))
        assert [response.status_code for response in responses] == [202, 202, 202, 202, 202, 429]
        assert len(email_sender) == 5
        assert int(responses[-1].headers["retry-after"]) > 0
        await db.execute("delete from early_access_signups where email = any($1::text[])", emails)

    run(scenario)


def test_disposable_rejections_consume_burst_budget_without_provider_work(email_sender, monkeypatch):
    async def unexpected(*_):
        raise AssertionError("Disposable email must not reach Turnstile or email provider")

    monkeypatch.setattr(early_access_module, "_verify_turnstile", unexpected)

    async def scenario(client):
        responses = [await client.post("/api/early-access", json={
            "email": "test@mailinator.com", "turnstile_token": "unused",
        }) for _ in range(4)]
        assert [r.status_code for r in responses] == [400, 400, 400, 429]
        assert "permanent email" in responses[0].json()["detail"]
        assert 1 <= int(responses[-1].headers["retry-after"]) <= 60
        assert not email_sender
        assert await db.fetchval(
            "select count(*) from early_access_signups where email = $1", "test@mailinator.com",
        ) == 0

    run(scenario)


def test_limit_is_atomic_and_expired_counter_resets():
    async def scenario(_):
        key = f"test-atomic:{uuid.uuid4().hex}"
        future = datetime.now(timezone.utc) + timedelta(minutes=1)
        try:
            results = await asyncio.gather(*[
                early_access_module._consume_limit(key, 3, future) for _ in range(20)
            ], return_exceptions=True)
            assert sum(result is None for result in results) == 3
            assert all(isinstance(r, HTTPException) and r.status_code == 429 for r in results if r)
            assert await db.fetchval("select count from early_access_limits where key = $1", key) == 4
            await db.execute("update early_access_limits set expires_at = now() - interval '1 second' where key = $1", key)
            await early_access_module._consume_limit(key, 3, future)
            assert await db.fetchval("select count from early_access_limits where key = $1", key) == 1
        finally:
            await db.execute("delete from early_access_limits where key = $1", key)

    run(scenario)


def test_global_limit_is_atomic_across_distinct_addresses(email_sender, monkeypatch):
    monkeypatch.setattr(early_access_module, "settings", replace(
        early_access_module.settings, early_access_daily_limit=2,
    ))

    async def scenario(_):
        emails = [f"global_{uuid.uuid4().hex}@example.com" for _ in range(10)]
        try:
            results = await asyncio.gather(*[
                early_access_module._claim_confirmation(email, uuid.uuid4().hex, datetime.now(timezone.utc))
                for email in emails
            ], return_exceptions=True)
            assert sum(isinstance(r, dict) for r in results) == 2
            rejected = [r for r in results if isinstance(r, HTTPException)]
            assert len(rejected) == 8
            assert all(r.status_code == 429 and int(r.headers["Retry-After"]) > 0 for r in rejected)
        finally:
            await db.execute("delete from early_access_signups where email = any($1::text[])", emails)

    run(scenario)


def test_ip_keys_resist_header_rotation_and_ipv6_address_rotation():
    now = datetime.now(timezone.utc)

    def key(ip, forwarded=""):
        return early_access_module._ip_key(Request({
            "type": "http", "client": (ip, 1234),
            "headers": [(b"x-forwarded-for", forwarded.encode())],
        }), now)

    assert key("203.0.113.20", "1.1.1.1") == key("203.0.113.20", "2.2.2.2")
    assert key("2001:db8:1::1") == key("2001:db8:1::abcd")
    assert key("2001:db8:1::1") != key("2001:db8:2::1")
    assert key("::ffff:203.0.113.20") == key("203.0.113.20")


def test_verified_complaint_webhook_suppresses_signup(email_sender, monkeypatch):
    async def scenario(client):
        email = f"complaint_{uuid.uuid4().hex}@example.com"
        response = await client.post("/api/early-access", json={
            "email": email, "turnstile_token": "test-token", "website": "",
        })
        assert response.status_code == 202
        email_id = await db.fetchval(
            "select resend_email_id from early_access_signups where email = $1", email,
        )
        monkeypatch.setattr(early_access_module, "verify_resend_webhook", lambda *_: {
            "type": "email.complained", "data": {"email_id": email_id},
        })
        await early_access_module.early_access.webhook("{}", {})
        row = await db.fetchrow(
            "select status, last_delivery_status from early_access_signups where email = $1", email,
        )
        assert row == {"status": "complained", "last_delivery_status": "complained"}
        await db.execute("delete from early_access_signups where email = $1", email)

    run(scenario)
