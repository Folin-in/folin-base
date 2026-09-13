"""Tests for User Blocking & Unblocking functionality, endpoints, and enforcement."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException
from starlette.testclient import TestClient

from app.config import settings
from app.main import app
from core.security import _check_not_blocked, get_current_user
from services.auth_core import AuthCore
import services.users as user_mod


@pytest.fixture
def sample_user():
    return {
        "user_id": "user_block_test_123",
        "email": "badactor@example.com",
        "username": "badactor",
        "name": "Bad Actor",
        "token_version": 1,
        "is_blocked": False,
        "blocked_at": None,
        "blocked_reason": None,
        "preferences": {"plan": "free"},
    }


def test_check_not_blocked_passes_for_clean_user(sample_user):
    # Should not raise
    _check_not_blocked(sample_user)


def test_check_not_blocked_raises_for_blocked_flag():
    user = {"user_id": "u1", "is_blocked": True, "preferences": {}}
    with pytest.raises(HTTPException) as exc:
        _check_not_blocked(user)
    assert exc.value.status_code == 403
    assert "Account suspended" in exc.value.detail


def test_check_not_blocked_raises_for_blocked_in_preferences():
    user = {"user_id": "u1", "is_blocked": False, "preferences": {"is_blocked": True}}
    with pytest.raises(HTTPException) as exc:
        _check_not_blocked(user)
    assert exc.value.status_code == 403
    assert "Account suspended" in exc.value.detail


def test_auth_session_rejects_blocked_user():
    user = {"user_id": "u1", "is_blocked": True, "token_version": 0, "preferences": {}}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(AuthCore.session(user))
    assert exc.value.status_code == 403
    assert "Account suspended" in exc.value.detail


def test_block_user_service_success(sample_user, monkeypatch):
    mock_fetchrow = AsyncMock(return_value=sample_user)
    mock_execute = AsyncMock()
    mock_revoke_refresh = AsyncMock()
    mock_invalidate = MagicMock()

    monkeypatch.setattr(user_mod.db, "fetchrow", mock_fetchrow)
    monkeypatch.setattr(user_mod.db, "execute", mock_execute)
    monkeypatch.setattr(user_mod, "revoke_all_refresh_tokens", mock_revoke_refresh)
    monkeypatch.setattr(user_mod.cache, "invalidate", mock_invalidate)

    res = asyncio.run(user_mod.users.block_user("badactor@example.com", reason="Spam activity"))

    assert res["ok"] is True
    assert "blocked" in res["message"]
    assert res["user"]["is_blocked"] is True
    assert res["user"]["blocked_reason"] == "Spam activity"
    assert res["user"]["blocked_at"] is not None

    # DB update was executed
    assert mock_execute.called
    # Cache invalidated
    mock_invalidate.assert_called_with("user_block_test_123")
    # Refresh tokens revoked
    mock_revoke_refresh.assert_called_with("user_block_test_123")


def test_block_user_admin_protection(monkeypatch):
    admin_email = next(iter(settings.admin_emails)) if settings.admin_emails else "admin@folin.in"
    admin_user = {
        "user_id": "user_admin_1",
        "email": admin_email,
        "username": "admin",
        "preferences": {},
    }
    mock_fetchrow = AsyncMock(return_value=admin_user)
    monkeypatch.setattr(user_mod.db, "fetchrow", mock_fetchrow)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(user_mod.users.block_user(admin_email, reason="Testing"))
    assert exc.value.status_code == 400
    assert "Cannot block an admin account" in exc.value.detail


def test_block_user_not_found(monkeypatch):
    mock_fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr(user_mod.db, "fetchrow", mock_fetchrow)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(user_mod.users.block_user("nonexistent@example.com"))
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail.lower()


def test_unblock_user_service_success(sample_user, monkeypatch):
    sample_user["is_blocked"] = True
    sample_user["blocked_reason"] = "Terms violation"
    mock_fetchrow = AsyncMock(return_value=sample_user)
    mock_execute = AsyncMock()
    mock_invalidate = MagicMock()

    monkeypatch.setattr(user_mod.db, "fetchrow", mock_fetchrow)
    monkeypatch.setattr(user_mod.db, "execute", mock_execute)
    monkeypatch.setattr(user_mod.cache, "invalidate", mock_invalidate)

    res = asyncio.run(user_mod.users.unblock_user("badactor@example.com"))

    assert res["ok"] is True
    assert "unblocked" in res["message"]
    assert res["user"]["is_blocked"] is False
    assert mock_execute.called
    mock_invalidate.assert_called_with("user_block_test_123")


def test_api_block_user_ip_rejected():
    client = TestClient(app)
    res = client.post(
        "/api/admin/block-user",
        json={"identifier": "test@example.com", "reason": "Fraud"},
        headers={"cf-connecting-ip": "198.51.100.99"},
    )
    assert res.status_code == 403
    assert "198.51.100.99" in res.json()["detail"]


def test_api_unblock_user_ip_rejected():
    client = TestClient(app)
    res = client.post(
        "/api/admin/unblock-user",
        json={"identifier": "test@example.com"},
        headers={"cf-connecting-ip": "198.51.100.99"},
    )
    assert res.status_code == 403
    assert "198.51.100.99" in res.json()["detail"]


def test_api_block_user_authorized_calls_service(monkeypatch):
    mock_block = AsyncMock(return_value={
        "ok": True,
        "message": "User 'test@example.com' has been blocked",
        "user": {"user_id": "u1", "email": "test@example.com", "is_blocked": True},
    })
    monkeypatch.setattr(user_mod.users, "block_user", mock_block)

    client = TestClient(app)
    res = client.post(
        "/api/admin/block-user",
        json={"identifier": "test@example.com", "reason": "Abuse"},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    mock_block.assert_called_once_with("test@example.com", "Abuse")


def test_api_block_alias_authorized_calls_service(monkeypatch):
    mock_block = AsyncMock(return_value={
        "ok": True,
        "message": "User 'test@example.com' has been blocked",
        "user": {"user_id": "u1", "email": "test@example.com", "is_blocked": True},
    })
    monkeypatch.setattr(user_mod.users, "block_user", mock_block)

    client = TestClient(app)
    res = client.post(
        "/api/admin/block",
        json={"identifier": "test@example.com", "reason": "Abuse"},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    mock_block.assert_called_once_with("test@example.com", "Abuse")


def test_api_unblock_user_authorized_calls_service(monkeypatch):
    mock_unblock = AsyncMock(return_value={
        "ok": True,
        "message": "User 'test@example.com' has been unblocked",
        "user": {"user_id": "u1", "email": "test@example.com", "is_blocked": False},
    })
    monkeypatch.setattr(user_mod.users, "unblock_user", mock_unblock)

    client = TestClient(app)
    res = client.post(
        "/api/admin/unblock-user",
        json={"identifier": "test@example.com"},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    mock_unblock.assert_called_once_with("test@example.com")


def test_api_unblock_alias_authorized_calls_service(monkeypatch):
    mock_unblock = AsyncMock(return_value={
        "ok": True,
        "message": "User 'test@example.com' has been unblocked",
        "user": {"user_id": "u1", "email": "test@example.com", "is_blocked": False},
    })
    monkeypatch.setattr(user_mod.users, "unblock_user", mock_unblock)

    client = TestClient(app)
    res = client.post(
        "/api/admin/unblock",
        json={"identifier": "test@example.com"},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    mock_unblock.assert_called_once_with("test@example.com")


def test_end_to_end_blocked_user_api_denied(monkeypatch):
    from core.security import create_access_token
    import core.security as sec_mod

    # Token minted for user_blocked
    token = create_access_token("user_blocked", 0)

    # When DB is queried for this user, returns a blocked user
    blocked_user = {
        "user_id": "user_blocked",
        "email": "blocked@example.com",
        "username": "blocked",
        "token_version": 0,
        "is_blocked": True,
        "preferences": {"is_blocked": True, "plan": "free"},
    }

    monkeypatch.setattr(sec_mod.cache, "get_user", lambda uid: None)
    monkeypatch.setattr(sec_mod.db, "fetchrow", AsyncMock(return_value=blocked_user))

    client = TestClient(app)
    res = client.get(
        "/api/user/preferences",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403
    assert "Account suspended. Please contact support." in res.json()["detail"]


def test_end_to_end_blocked_user_login_denied(monkeypatch):
    from core.security import hash_password
    import services.auth_core as auth_mod

    blocked_user = {
        "user_id": "user_blocked",
        "email": "blocked@example.com",
        "username": "blocked",
        "password_hash": hash_password("ValidPassword123"),
        "token_version": 0,
        "is_blocked": True,
        "preferences": {"is_blocked": True},
    }

    monkeypatch.setattr(auth_mod.db, "fetchrow", AsyncMock(return_value=blocked_user))

    client = TestClient(app)
    res = client.post(
        "/api/auth/login",
        json={"identifier": "blocked@example.com", "password": "ValidPassword123"},
    )
    assert res.status_code == 403
    assert "Account suspended. Please contact support." in res.json()["detail"]

