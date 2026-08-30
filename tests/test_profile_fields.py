"""Iteration 11 - email/username fields on Edit Profile (HealthProfileUpdate).

Verifies:
- GET /api/auth/me returns current email & username
- PUT /api/user/health-profile accepts email + username
- Uniqueness (409) when new value collides with another account
- 422 (Pydantic) for invalid regex/malformed values
- Cleanup: reset ketan back to canonical values, delete throwaway
"""
import os
import uuid
from pathlib import Path

import pytest
import requests
from dotenv import load_dotenv

load_dotenv(Path("/app/frontend/.env"))
BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set"
API = f"{BASE_URL}/api"

KETAN_EMAIL = "ketan@test.com"
KETAN_USERNAME = "ketan"
KETAN_PASSWORD = "Test@1234"


@pytest.fixture(scope="module")
def s():
    return requests.Session()


@pytest.fixture(scope="module")
def ketan(s):
    body = _login(s, KETAN_USERNAME, KETAN_PASSWORD)
    return {"headers": {"Authorization": f"Bearer {body['access_token']}"}, "user": body["user"]}


def _login(s, identifier: str, password: str) -> dict:
    r = s.post(f"{API}/auth/login", json={"identifier": identifier, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _refresh_token(s, ketan: dict, identifier: str = KETAN_USERNAME, password: str = KETAN_PASSWORD):
    """SEC-003: email change bumps token_version, revoking old JWTs.
    Callers must re-login and refresh the fixture's Authorization header."""
    body = _login(s, identifier, password)
    ketan["headers"]["Authorization"] = f"Bearer {body['access_token']}"
    ketan["user"] = body["user"]


@pytest.fixture(scope="module")
def throwaway(s):
    """Create a throwaway account for uniqueness collision tests. Cleaned up at teardown."""
    uid = uuid.uuid4().hex[:8]
    email = f"TEST_throw_{uid}@vqtest.com"
    username = f"TEST_throw_{uid}"
    r = s.post(
        f"{API}/auth/register",
        json={"email": email, "username": username, "password": "Password@123",
              "name": "Throwaway", "consent": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ctx = {
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
        "email": email.lower(),
        "username": username.lower(),
    }
    yield ctx
    # cleanup
    try:
        s.delete(f"{API}/user/account", headers=ctx["headers"])
    except Exception:
        pass


# ---------- GET /api/auth/me ----------
class TestGetMe:
    def test_me_returns_email_and_username(self, s, ketan):
        r = s.get(f"{API}/auth/me", headers=ketan["headers"])
        assert r.status_code == 200, r.text
        u = r.json()
        assert u["email"] == KETAN_EMAIL
        assert u["username"] == KETAN_USERNAME


# ---------- PUT /api/user/health-profile ----------
class TestUpdateProfileNoop:
    def test_put_same_values_no_conflict(self, s, ketan):
        r = s.put(
            f"{API}/user/health-profile",
            json={"email": KETAN_EMAIL, "username": KETAN_USERNAME},
            headers=ketan["headers"],
        )
        assert r.status_code == 200, r.text
        u = r.json()
        assert u["email"] == KETAN_EMAIL
        assert u["username"] == KETAN_USERNAME


class TestUpdateUsernameEmail:
    def test_update_username_then_email(self, s, ketan):
        new_username = "ketan_v2"
        new_email = "ketan+alt@test.com"

        # 1) change username
        r = s.put(f"{API}/user/health-profile", json={"username": new_username}, headers=ketan["headers"])
        assert r.status_code == 200, r.text
        assert r.json()["username"] == new_username
        # Verify persisted via GET
        me = s.get(f"{API}/auth/me", headers=ketan["headers"]).json()
        assert me["username"] == new_username

        # 2) change email - SEC-003 revokes old JWT after email change; refresh session.
        r = s.put(f"{API}/user/health-profile", json={"email": new_email}, headers=ketan["headers"])
        assert r.status_code == 200, r.text
        assert r.json()["email"] == new_email.lower()
        _refresh_token(s, ketan, identifier=new_email, password=KETAN_PASSWORD)
        me = s.get(f"{API}/auth/me", headers=ketan["headers"]).json()
        assert me["email"] == new_email.lower()

        # 3) restore
        r = s.put(
            f"{API}/user/health-profile",
            json={"username": KETAN_USERNAME, "email": KETAN_EMAIL},
            headers=ketan["headers"],
        )
        assert r.status_code == 200
        u = r.json()
        assert u["username"] == KETAN_USERNAME
        assert u["email"] == KETAN_EMAIL
        # Re-login one more time so subsequent tests in this module keep working.
        _refresh_token(s, ketan, identifier=KETAN_USERNAME, password=KETAN_PASSWORD)


class TestUniqueness:
    def test_email_collision_returns_409(self, s, ketan, throwaway):
        r = s.put(
            f"{API}/user/health-profile",
            json={"email": throwaway["email"]},
            headers=ketan["headers"],
        )
        assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
        assert "email" in r.json()["detail"].lower()
        # Ensure ketan's email did not change
        me = s.get(f"{API}/auth/me", headers=ketan["headers"]).json()
        assert me["email"] == KETAN_EMAIL

    def test_username_collision_returns_409(self, s, ketan, throwaway):
        r = s.put(
            f"{API}/user/health-profile",
            json={"username": throwaway["username"]},
            headers=ketan["headers"],
        )
        assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
        assert "username" in r.json()["detail"].lower()
        me = s.get(f"{API}/auth/me", headers=ketan["headers"]).json()
        assert me["username"] == KETAN_USERNAME


class TestServerSideValidation:
    def test_invalid_username_too_short_422(self, s, ketan):
        r = s.put(f"{API}/user/health-profile", json={"username": "hi"}, headers=ketan["headers"])
        assert r.status_code == 422, r.text

    def test_invalid_username_bad_chars_422(self, s, ketan):
        r = s.put(f"{API}/user/health-profile", json={"username": "bad name!"}, headers=ketan["headers"])
        assert r.status_code == 422, r.text

    def test_invalid_email_422(self, s, ketan):
        r = s.put(f"{API}/user/health-profile", json={"email": "not-an-email"}, headers=ketan["headers"])
        assert r.status_code == 422, r.text


class TestAuthGate:
    def test_put_requires_auth(self, s):
        r = s.put(f"{API}/user/health-profile", json={"email": "x@x.com"})
        assert r.status_code == 401
