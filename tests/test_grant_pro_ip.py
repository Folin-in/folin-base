"""Tests for the IP-restricted Grant Pro API and IP gating logic."""
import pytest
from types import SimpleNamespace
from fastapi import HTTPException

from core.security import get_client_ip, is_ip_allowed, require_admin_ip
from app.config import settings


class MockRequest:
    def __init__(self, host="127.0.0.1", headers=None):
        self.client = SimpleNamespace(host=host) if host else None
        self.headers = headers or {}


def test_client_ip_cf_connecting_ip():
    req = MockRequest("10.0.0.1", {"cf-connecting-ip": "223.235.96.51"})
    assert get_client_ip(req) == "223.235.96.51"


def test_client_ip_forwarded_for():
    req = MockRequest("127.0.0.1", {"x-forwarded-for": "223.235.96.51, 10.0.0.1"})
    assert get_client_ip(req) == "223.235.96.51"


def test_client_ip_socket_peer():
    req = MockRequest("223.235.96.51", {})
    assert get_client_ip(req) == "223.235.96.51"


def test_is_ip_allowed():
    allowed = ["223.235.96.51", "192.168.1.0/24"]
    assert is_ip_allowed("223.235.96.51", allowed) is True
    assert is_ip_allowed("127.0.0.1", allowed) is True
    assert is_ip_allowed("::1", allowed) is True
    assert is_ip_allowed("testclient", allowed) is True
    assert is_ip_allowed("192.168.1.50", allowed) is True
    assert is_ip_allowed("1.2.3.4", allowed) is False
    assert is_ip_allowed("198.51.100.22", allowed) is False
    assert is_ip_allowed("", allowed) is False


import asyncio

def test_require_admin_ip_permitted():
    req = MockRequest("127.0.0.1", {"cf-connecting-ip": "223.235.96.51"})
    ip = asyncio.run(require_admin_ip(req))
    assert ip == "223.235.96.51"


def test_require_admin_ip_rejected():
    req = MockRequest("198.51.100.22", {})
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(require_admin_ip(req))
    assert exc_info.value.status_code == 403
    assert "198.51.100.22" in exc_info.value.detail


def test_api_my_ip_endpoint():
    from starlette.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    res = client.get("/api/admin/my-ip")
    assert res.status_code == 200
    data = res.json()
    assert "ip" in data
    assert data["allowed"] is True


def test_api_grant_pro_ip_rejected():
    from starlette.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    res = client.post(
        "/api/admin/grant-pro",
        json={"identifier": "test@example.com", "days": 30},
        headers={"cf-connecting-ip": "198.51.100.99"},
    )
    assert res.status_code == 403
    assert "198.51.100.99" in res.json()["detail"]


def test_api_grant_pro_authorized_calls_service(monkeypatch):
    from unittest.mock import AsyncMock
    from starlette.testclient import TestClient
    from app.main import app
    import services.users as user_mod

    mock_grant = AsyncMock(return_value={
        "ok": True,
        "message": "Successfully granted Pro for 365 days",
        "user": {"user_id": "u123", "email": "test@example.com", "username": "testuser", "plan": "pro"},
    })
    monkeypatch.setattr(user_mod.users, "grant_pro", mock_grant)

    client = TestClient(app)
    res = client.post(
        "/api/admin/grant-pro",
        json={"identifier": "testuser", "days": 365},
        headers={"cf-connecting-ip": "223.235.96.51"},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    mock_grant.assert_awaited_once_with("testuser", 365)


def test_api_grant_pro_user_not_found(monkeypatch):
    from unittest.mock import AsyncMock
    from starlette.testclient import TestClient
    from app.main import app
    import services.users as user_mod

    mock_grant = AsyncMock(side_effect=HTTPException(status_code=404, detail="User 'ghost' not found"))
    monkeypatch.setattr(user_mod.users, "grant_pro", mock_grant)

    client = TestClient(app)
    res = client.post(
        "/api/admin/grant-pro",
        json={"identifier": "ghost", "days": 30},
        headers={"cf-connecting-ip": "223.235.96.51"},
    )
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_user_service_grant_pro_validation():
    import services.users as user_mod
    with pytest.raises(HTTPException) as exc:
        asyncio.run(user_mod.users.grant_pro("", 30))
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        asyncio.run(user_mod.users.grant_pro("user@example.com", 0))
    assert exc.value.status_code == 400


def test_api_users_list_endpoint(monkeypatch):
    from unittest.mock import AsyncMock
    from starlette.testclient import TestClient
    from app.main import app
    from core.security import get_admin_user
    import services.admin_metrics as metrics_mod

    mock_users = AsyncMock(return_value={
        "total": 1,
        "limit": 50,
        "offset": 0,
        "users": [{
            "user_id": "u1",
            "email": "test@example.com",
            "username": "test",
            "name": "Test",
            "auth_provider": "local",
            "plan": "free",
            "plan_expires_at": None,
            "created_at": "2026-09-01T00:00:00",
            "last_seen": "2026-09-13T00:00:00",
            "reports_count": 1,
            "api_calls_count": 2,
            "ai_model_calls": 5,
        }]
    })
    monkeypatch.setattr(metrics_mod.admin_metrics, "user_details_list", mock_users)

    app.dependency_overrides[get_admin_user] = lambda: {"email": "admin@example.com"}
    try:
        client = TestClient(app)
        res = client.get("/api/admin/users?search=test")
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 1
        assert data["users"][0]["email"] == "test@example.com"
        assert data["users"][0]["api_calls_count"] == 2
    finally:
        app.dependency_overrides.clear()



