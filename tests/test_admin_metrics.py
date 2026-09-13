"""The admin dashboard's gate and payload, through the ASGI app against the real DB.

The gate is the point of this suite: metrics are a whole-tenant view, so anything short of
an allow-listed admin must not see them - and must not learn the surface exists either.

    ./venv/bin/python -m pytest tests/test_admin_metrics.py -v
"""
import asyncio
import dataclasses
import uuid

import httpx
import pytest

import app.config as config
from app.constants import METRIC_WINDOWS
from app.main import app
from core import rate_limit
from db import session as db

_LOOP = asyncio.get_event_loop()
_ready = False
ADMIN_EMAIL = "admin_probe@example.com"


@pytest.fixture(autouse=True)
def cleanup_probe_users():
    yield
    async def _clean():
        if _ready:
            await db.execute("delete from refresh_tokens where user_id in (select user_id from users where name = 'Admin Probe')")
            await db.execute("delete from users where name = 'Admin Probe'")
    _LOOP.run_until_complete(_clean())


def run(coro):
    async def w():
        global _ready
        if not _ready:
            await db.connect(); _ready = True
        rate_limit._WINDOWS.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await coro(c)
    return _LOOP.run_until_complete(w())


async def register(c, email=None):
    email = email or f"adm_{uuid.uuid4().hex[:8]}@example.com"
    r = await c.post("/api/auth/register", json={
        "email": email, "username": f"adm{uuid.uuid4().hex[:6]}",
        "password": "test-pass-123", "name": "Admin Probe", "consent": True})
    assert r.status_code == 200, r.text
    return r.json()["access_token"], email


def as_admin(monkeypatch, *emails):
    """Settings is frozen, so swap in a replaced copy rather than mutating it."""
    monkeypatch.setattr(config, "settings",
                        dataclasses.replace(config.settings, admin_emails=[e.lower() for e in emails]))
    monkeypatch.setattr("core.security.settings", config.settings)


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------- the gate

def test_metrics_require_authentication():
    r = run(lambda c: c.get("/api/admin/metrics"))
    assert r.status_code == 401


def test_signed_in_non_admin_gets_404_not_403(monkeypatch):
    """404 keeps the dashboard's existence unconfirmable from an ordinary account."""
    tok, _ = run(register)
    as_admin(monkeypatch, "someone-else@folin.app")
    r = run(lambda c: c.get("/api/admin/metrics", headers=bearer(tok)))
    assert r.status_code == 404
    assert "admin" not in r.text.lower()


def test_admin_allowlist_is_case_insensitive(monkeypatch):
    tok, email = run(lambda c: register(c, f"Mixed_{uuid.uuid4().hex[:6]}@Example.COM"))
    as_admin(monkeypatch, email.upper())
    r = run(lambda c: c.get("/api/admin/metrics", headers=bearer(tok)))
    assert r.status_code == 200


def test_empty_allowlist_locks_everyone_out(monkeypatch):
    """An unset ADMIN_EMAILS must fail closed, never open."""
    tok, email = run(register)
    as_admin(monkeypatch)
    r = run(lambda c: c.get("/api/admin/metrics", headers=bearer(tok)))
    assert r.status_code == 404


def test_garbage_token_is_rejected():
    r = run(lambda c: c.get("/api/admin/metrics", headers=bearer("not-a-jwt")))
    assert r.status_code == 401


# ---------------------------------------------------------------------------- the payload

def test_snapshot_has_every_panel(monkeypatch):
    tok, email = run(register)
    as_admin(monkeypatch, email)
    r = run(lambda c: c.get("/api/admin/metrics", headers=bearer(tok)))
    assert r.status_code == 200
    d = r.json()
    for key in ("generated_at", "windows", "overview", "totals", "jobs", "models",
                "users", "reports", "payments"):
        assert key in d, key
    assert list(d["overview"]) == list(METRIC_WINDOWS)
    assert d["totals"]["users"] >= 1


@pytest.mark.parametrize("window", list(METRIC_WINDOWS))
def test_every_advertised_window_works(monkeypatch, window):
    tok, email = run(register)
    as_admin(monkeypatch, email)
    r = run(lambda c: c.get(f"/api/admin/metrics?window={window}", headers=bearer(tok)))
    assert r.status_code == 200
    assert r.json()["window_days"] == METRIC_WINDOWS[window]


def test_unknown_window_is_422_not_a_silent_default(monkeypatch):
    tok, email = run(register)
    as_admin(monkeypatch, email)
    r = run(lambda c: c.get("/api/admin/metrics?window=all-time", headers=bearer(tok)))
    assert r.status_code == 422


def test_rates_are_percentages_not_fractions(monkeypatch):
    tok, email = run(register)
    as_admin(monkeypatch, email)
    d = run(lambda c: c.get("/api/admin/metrics", headers=bearer(tok))).json()
    for w in d["overview"].values():
        assert 0 <= w["success_rate"] <= 100
    for m in d["models"]["by_model"]:
        assert 0 <= m["ok_rate"] <= 100
        assert m["ok"] + m["bad_reply"] + m["errors"] <= m["calls"]
    assert 0 <= d["payments"]["conversion"] <= 100


def test_metrics_leak_no_report_content(monkeypatch):
    """Operational counters only - the dashboard must never carry biomarker values."""
    tok, email = run(register)
    as_admin(monkeypatch, email)
    body = run(lambda c: c.get("/api/admin/metrics", headers=bearer(tok))).text.lower()
    for banned in ("biomarker", "haemoglobin", "hemoglobin", "patient_name", "file_base64"):
        assert banned not in body, banned


# ---------------------------------------------------------------------------- the page

def test_dashboard_page_is_an_empty_shell():
    """Served unauthenticated, so it must contain markup only - never data."""
    r = run(lambda c: c.get("/api/admin"))
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "folin" in r.text.lower()
    assert "revenue_inr\":" not in r.text


def test_page_fetch_urls_resolve_to_real_routes():
    """The page builds its URLs from location.pathname; a bare relative fetch from
    /api/admin resolves against /api/ and silently misses the admin segment, which
    surfaced to the user as 'not an admin'. Every URL the page builds must route."""
    import re
    from pathlib import Path
    from urllib.parse import urljoin

    from routers.admin import DASHBOARD

    page = DASHBOARD.read_text()
    served_at = "/api/admin"
    base = re.sub(r"/admin/?$", "", served_at)
    built = [u.replace("${BASE}", base) for u in re.findall(r'fetch\(`([^`]+)`', page)]
    assert built, "page makes no templated fetch calls - did the URL construction change?"

    routes = {r.path for r in app.routes if hasattr(r, "path")}
    for url in built:
        path = urljoin(served_at, url.split("?")[0])
        assert path in routes, f"page fetches {path!r}, which is not a route"
