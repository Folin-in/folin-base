"""Iteration 12 - Regression tests for PUT /api/user/health-profile
verifying health_profile is NOT wiped when payload only contains
email/username/name, and partial hp updates merge with existing values.

Fix under test: /app/backend/routers/users.py update_health_profile now uses
req.model_dump(exclude_unset=True) and only rebuilds health_profile if at
least one hp key is supplied; unset hp keys fall back to existing values.
"""
import os
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

CANONICAL_HP = {
    "age": 28,
    "gender": "male",
    "weight_kg": 75,
    "height_cm": 175,
    "workout": True,
    "workout_type": "strength",
    "diet": "vegetarian",
}


@pytest.fixture(scope="module")
def s():
    return requests.Session()


@pytest.fixture(scope="module")
def ketan(s):
    r = s.post(f"{API}/auth/login", json={"identifier": KETAN_USERNAME, "password": KETAN_PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    # Re-seed canonical hp (iteration 11 may have left him wiped)
    seed = s.put(
        f"{API}/user/health-profile",
        json={"email": KETAN_EMAIL, "username": KETAN_USERNAME, **CANONICAL_HP},
        headers=headers,
    )
    assert seed.status_code == 200, seed.text

    # Confirm
    me = s.get(f"{API}/auth/me", headers=headers).json()
    hp = me.get("health_profile") or {}
    for k, v in CANONICAL_HP.items():
        assert hp.get(k) == v, f"seed failed: hp.{k}={hp.get(k)} expected {v}"

    yield {"headers": headers, "user": body["user"]}

    # teardown: restore canonical again
    s.put(
        f"{API}/user/health-profile",
        json={"email": KETAN_EMAIL, "username": KETAN_USERNAME, **CANONICAL_HP, "name": "Ketan"},
        headers=headers,
    )


def _get_hp(s, headers):
    me = s.get(f"{API}/auth/me", headers=headers).json()
    return me.get("health_profile") or {}


class TestHealthProfilePreserve:
    """Regression: payloads without hp keys must NOT wipe health_profile."""

    def test_put_email_only_preserves_hp(self, s, ketan):
        r = s.put(
            f"{API}/user/health-profile",
            json={"email": KETAN_EMAIL},
            headers=ketan["headers"],
        )
        assert r.status_code == 200, r.text
        hp = _get_hp(s, ketan["headers"])
        for k, v in CANONICAL_HP.items():
            assert hp.get(k) == v, f"hp.{k} was mutated after email-only PUT: got {hp.get(k)} expected {v}"

    def test_put_username_only_preserves_hp(self, s, ketan):
        r = s.put(
            f"{API}/user/health-profile",
            json={"username": KETAN_USERNAME},
            headers=ketan["headers"],
        )
        assert r.status_code == 200, r.text
        hp = _get_hp(s, ketan["headers"])
        for k, v in CANONICAL_HP.items():
            assert hp.get(k) == v, f"hp.{k} was mutated after username-only PUT: got {hp.get(k)} expected {v}"

    def test_put_name_only_preserves_hp(self, s, ketan):
        r = s.put(
            f"{API}/user/health-profile",
            json={"name": "Ketan"},
            headers=ketan["headers"],
        )
        assert r.status_code == 200, r.text
        hp = _get_hp(s, ketan["headers"])
        for k, v in CANONICAL_HP.items():
            assert hp.get(k) == v

    def test_empty_put_no_changes(self, s, ketan):
        r = s.put(f"{API}/user/health-profile", json={}, headers=ketan["headers"])
        assert r.status_code == 200, r.text
        hp = _get_hp(s, ketan["headers"])
        for k, v in CANONICAL_HP.items():
            assert hp.get(k) == v


class TestHealthProfilePartial:
    """Regression: partial hp updates merge with existing hp."""

    def test_put_age_only_updates_only_age(self, s, ketan):
        r = s.put(f"{API}/user/health-profile", json={"age": 30}, headers=ketan["headers"])
        assert r.status_code == 200, r.text
        hp = _get_hp(s, ketan["headers"])
        assert hp.get("age") == 30
        # Other fields unchanged
        for k in ("gender", "weight_kg", "height_cm", "workout", "workout_type", "diet"):
            assert hp.get(k) == CANONICAL_HP[k], f"hp.{k} unexpectedly changed to {hp.get(k)}"
        # restore
        s.put(f"{API}/user/health-profile", json={"age": 28}, headers=ketan["headers"])

    def test_put_workout_false_nulls_workout_type_but_keeps_diet(self, s, ketan):
        r = s.put(f"{API}/user/health-profile", json={"workout": False}, headers=ketan["headers"])
        assert r.status_code == 200, r.text
        hp = _get_hp(s, ketan["headers"])
        assert hp.get("workout") is False
        assert hp.get("workout_type") is None
        # diet stays
        assert hp.get("diet") == "vegetarian"
        # age/gender/weight/height unchanged
        assert hp.get("age") == 28
        assert hp.get("gender") == "male"
        assert hp.get("weight_kg") == 75
        assert hp.get("height_cm") == 175
        # restore
        s.put(
            f"{API}/user/health-profile",
            json={"workout": True, "workout_type": "strength"},
            headers=ketan["headers"],
        )

    def test_full_payload_still_works(self, s, ketan):
        payload = {
            "email": KETAN_EMAIL,
            "username": KETAN_USERNAME,
            "name": "Ketan",
            **CANONICAL_HP,
        }
        r = s.put(f"{API}/user/health-profile", json=payload, headers=ketan["headers"])
        assert r.status_code == 200, r.text
        u = r.json()
        assert u["email"] == KETAN_EMAIL
        assert u["username"] == KETAN_USERNAME
        hp = u.get("health_profile") or {}
        for k, v in CANONICAL_HP.items():
            assert hp.get(k) == v
