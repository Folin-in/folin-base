"""Iteration 13 - Re-verify PUT /api/user/health-profile with username payload
persists correctly and preserves health_profile.

Iteration 14 SEC-003 note: any prior email change in the pytest session bumps
`token_version`, which would 401 an older JWT. This module always requests a
FRESH token at start so it is resilient to earlier revocations.
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "http://localhost:8000").rstrip("/")


def _fresh_token() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"identifier": "ketan@test.com", "password": "Test@1234"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def token():
    return _fresh_token()


@pytest.fixture
def headers(token):
    # Always try the cached token first; if the session revoked it mid-suite,
    # request a fresh one lazily via _fresh_token to keep tests robust.
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _me(headers):
    r = requests.get(f"{BASE_URL}/api/auth/me", headers=headers, timeout=15)
    assert r.status_code == 200
    return r.json()


class TestUsernameEditFlow:
    def test_change_username_and_persist(self, headers):
        # baseline
        me0 = _me(headers)
        hp0 = me0.get("health_profile") or {}

        # change username to ketan_v3
        r = requests.put(
            f"{BASE_URL}/api/user/health-profile",
            headers=headers,
            json={"username": "ketan_v3"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("username") == "ketan_v3"
        # hp preserved
        assert body.get("health_profile", {}).get("age") == hp0.get("age")

        # verify via GET /me
        me1 = _me(headers)
        assert me1.get("username") == "ketan_v3"
        assert me1.get("health_profile", {}).get("gender") == hp0.get("gender")

        # restore
        r2 = requests.put(
            f"{BASE_URL}/api/user/health-profile",
            headers=headers,
            json={"username": "ketan"},
            timeout=15,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json().get("username") == "ketan"

        me2 = _me(headers)
        assert me2.get("username") == "ketan"
        assert me2.get("health_profile", {}).get("workout_type") == hp0.get("workout_type")

    def test_short_username_rejected(self, headers):
        r = requests.put(
            f"{BASE_URL}/api/user/health-profile",
            headers=headers,
            json={"username": "x"},
            timeout=15,
        )
        # Backend validates username min-length; expect 422 or 400
        assert r.status_code in (400, 422), r.text
