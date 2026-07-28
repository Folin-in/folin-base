"""VitalQuest backend tests - Iteration 3 (Auth + user-scoping + health profile).

- Uses public URL from /app/frontend/.env
- Registers a fresh throwaway user per session for auth-scoping tests
- Logs in as ketan/Test@1234 (seeded) to verify existing data + suggestions[]
- Does NOT call POST /api/reports/analyze (LLM-cost, already verified)
- Does NOT delete ketan's existing reports
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


# ---------- Shared fixtures ----------
@pytest.fixture(scope="session")
def s():
    return requests.Session()


@pytest.fixture(scope="session")
def ketan_token(s):
    r = s.post(f"{API}/auth/login", json={"identifier": "ketan", "password": "Test@1234"})
    assert r.status_code == 200, f"ketan login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def ketan_headers(ketan_token):
    return {"Authorization": f"Bearer {ketan_token}"}


@pytest.fixture(scope="session")
def user2(s):
    """Register a brand-new second user; return (token, user, headers)."""
    uid = uuid.uuid4().hex[:8]
    payload = {
        "email": f"TEST_u2_{uid}@vqtest.com",
        "username": f"TEST_u2_{uid}",
        "password": "Password@123",
        "name": "TEST U2",
    }
    r = s.post(f"{API}/auth/register", json=payload)
    assert r.status_code == 200, f"register user2 failed: {r.status_code} {r.text}"
    body = r.json()
    return {
        "token": body["access_token"],
        "user": body["user"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
        "email": payload["email"],
        "username": payload["username"],
        "password": payload["password"],
    }


# ---------- Health ----------
class TestHealth:
    def test_root(self, s):
        r = s.get(f"{API}/")
        assert r.status_code == 200
        assert r.json().get("status") == "ok"


# ---------- Auth: register / login / me ----------
class TestAuthRegisterLogin:
    def test_register_success_returns_token_and_user(self, user2):
        assert user2["token"]
        u = user2["user"]
        assert u["email"] == user2["email"].lower()
        assert u["username"] == user2["username"].lower()
        assert u["auth_provider"] == "local"
        assert "user_id" in u

    def test_register_duplicate_email_400(self, s, user2):
        r = s.post(f"{API}/auth/register", json={
            "email": user2["email"], "username": f"TEST_diff_{uuid.uuid4().hex[:6]}",
            "password": "Password@123", "name": "dup",
        })
        assert r.status_code == 400
        assert "Email" in r.json().get("detail", "")

    def test_register_duplicate_username_400(self, s, user2):
        r = s.post(f"{API}/auth/register", json={
            "email": f"TEST_diff_{uuid.uuid4().hex[:6]}@vqtest.com",
            "username": user2["username"], "password": "Password@123", "name": "dup",
        })
        assert r.status_code == 400
        assert "Username" in r.json().get("detail", "")

    def test_register_short_password_422(self, s):
        r = s.post(f"{API}/auth/register", json={
            "email": f"TEST_sp_{uuid.uuid4().hex[:6]}@vqtest.com",
            "username": f"TEST_sp_{uuid.uuid4().hex[:6]}", "password": "abc", "name": "x",
        })
        assert r.status_code == 422

    def test_login_by_email(self, s):
        r = s.post(f"{API}/auth/login", json={"identifier": "ketan@test.com", "password": "Test@1234"})
        assert r.status_code == 200
        assert r.json()["user"]["username"] == "ketan"

    def test_login_by_username(self, s):
        r = s.post(f"{API}/auth/login", json={"identifier": "ketan", "password": "Test@1234"})
        assert r.status_code == 200

    def test_login_wrong_password_401(self, s):
        r = s.post(f"{API}/auth/login", json={"identifier": "ketan", "password": "WRONG_pwd"})
        assert r.status_code == 401

    def test_me_with_valid_token(self, s, ketan_headers):
        r = s.get(f"{API}/auth/me", headers=ketan_headers)
        assert r.status_code == 200
        u = r.json()
        assert u["username"] == "ketan"
        assert u["email"] == "ketan@test.com"
        # health profile persisted from seed
        hp = u.get("health_profile") or {}
        assert hp.get("weight_kg") == 75
        assert hp.get("height_cm") == 175

    def test_me_without_token_401(self, s):
        r = s.get(f"{API}/auth/me")
        assert r.status_code == 401

    def test_me_garbage_token_401(self, s):
        r = s.get(f"{API}/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
        assert r.status_code == 401


# ---------- Auth gating on protected endpoints ----------
class TestAuthGating:
    ENDPOINTS = [
        ("GET", "/reports"),
        ("GET", "/profile"),
        ("GET", "/reports/compare"),
        ("PUT", "/user/health-profile"),
        ("POST", "/reports/analyze"),
    ]

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_endpoint_requires_auth(self, s, method, path):
        r = s.request(method, f"{API}{path}", json={} if method != "GET" else None)
        assert r.status_code == 401, f"{method} {path} expected 401 got {r.status_code}"


# ---------- User scoping ----------
class TestUserScoping:
    def test_user2_reports_empty(self, s, user2):
        r = s.get(f"{API}/reports", headers=user2["headers"])
        assert r.status_code == 200
        assert r.json() == []

    def test_user2_cannot_read_ketan_report(self, s, ketan_headers, user2):
        # Grab one of ketan's report ids
        r = s.get(f"{API}/reports", headers=ketan_headers)
        assert r.status_code == 200
        ketan_reports = r.json()
        assert len(ketan_reports) >= 2, f"expected ketan to have >=2 reports, got {len(ketan_reports)}"
        rid = ketan_reports[0]["id"]
        # user2 fetch that id
        r2 = s.get(f"{API}/reports/{rid}", headers=user2["headers"])
        assert r2.status_code == 404

    def test_user2_compare_unavailable(self, s, user2):
        r = s.get(f"{API}/reports/compare", headers=user2["headers"])
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body.get("reports_count", 0) == 0


# ---------- Ketan reports & suggestions[] ----------
class TestKetanReports:
    def test_ketan_has_four_reports(self, s, ketan_headers):
        r = s.get(f"{API}/reports", headers=ketan_headers)
        assert r.status_code == 200
        reports = r.json()
        assert len(reports) >= 4, f"ketan should have >=4 reports (found {len(reports)})"

    def test_newest_report_has_suggestions(self, s, ketan_headers):
        r = s.get(f"{API}/reports", headers=ketan_headers)
        reports = r.json()
        # /reports is sorted uploaded_at desc → newest is index 0
        newest_id = reports[0]["id"]
        rr = s.get(f"{API}/reports/{newest_id}", headers=ketan_headers)
        assert rr.status_code == 200
        rep = rr.json()
        suggestions = rep.get("suggestions") or []
        assert isinstance(suggestions, list) and len(suggestions) > 0, "newest report has empty suggestions[]"
        allowed = {"nutrition", "movement", "sleep", "supplements", "lifestyle"}
        for s_item in suggestions:
            assert s_item["category"] in allowed
            assert s_item["title"]
            assert s_item["body"]


# ---------- Health profile PUT ----------
class TestHealthProfile:
    """Uses user2 to avoid mutating ketan's profile."""

    def test_put_valid_updates_and_persists(self, s, user2):
        payload = {
            "name": "TEST U2 Updated",
            "age": 30, "gender": "female", "weight_kg": 60, "height_cm": 165,
            "workout": True, "workout_type": "yoga", "diet": "vegan",
        }
        r = s.put(f"{API}/user/health-profile", json=payload, headers=user2["headers"])
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "TEST U2 Updated"
        hp = body["health_profile"]
        assert hp["age"] == 30 and hp["gender"] == "female"
        assert hp["weight_kg"] == 60 and hp["height_cm"] == 165
        assert hp["workout"] is True and hp["workout_type"] == "yoga"
        assert hp["diet"] == "vegan"
        # GET /auth/me reflects it
        r2 = s.get(f"{API}/auth/me", headers=user2["headers"])
        assert r2.status_code == 200
        me = r2.json()
        assert me["health_profile"]["diet"] == "vegan"
        assert me["health_profile"]["weight_kg"] == 60

    def test_put_invalid_gender_400(self, s, user2):
        r = s.put(f"{API}/user/health-profile", json={"gender": "other"}, headers=user2["headers"])
        assert r.status_code == 400

    def test_put_invalid_diet_400(self, s, user2):
        r = s.put(f"{API}/user/health-profile", json={"diet": "keto"}, headers=user2["headers"])
        assert r.status_code == 400

    def test_put_invalid_workout_type_400(self, s, user2):
        r = s.put(f"{API}/user/health-profile", json={"workout": True, "workout_type": "crossfit"}, headers=user2["headers"])
        assert r.status_code == 400

    def test_ketan_health_profile_persists(self, s, ketan_headers):
        r = s.get(f"{API}/auth/me", headers=ketan_headers)
        hp = r.json()["health_profile"]
        assert hp["weight_kg"] == 75
        assert hp["height_cm"] == 175
        assert hp["gender"] == "male"
        assert hp["diet"] == "vegetarian"


# ---------- Compare endpoint (iteration 4) ----------
class TestKetanCompare:
    """Validate GET /api/reports/compare - default, ?against=<id>, invalid against fallback."""

    def test_compare_default(self, s, ketan_headers):
        r = s.get(f"{API}/reports/compare", headers=ketan_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["score_delta"] == 26, f"expected +26 score delta, got {body['score_delta']}"
        assert body["improved_count"] == 7
        assert body["worsened_count"] == 3
        assert body["same_count"] == 4
        older = body.get("older_reports") or []
        assert len(older) == 3, f"expected 3 older reports, got {len(older)}"
        # latest health_score 77, against health_score 51
        assert body["latest"]["health_score"] == 77
        assert body["against"]["health_score"] == 51
        # markers are sorted improved → worsened → same
        markers = body.get("markers") or []
        assert len(markers) == 7 + 3 + 4
        assert markers[0]["change"] == "improved"

    def test_compare_marker_examples(self, s, ketan_headers):
        """Validate specific expected marker transitions."""
        r = s.get(f"{API}/reports/compare", headers=ketan_headers)
        body = r.json()
        by_name = {m["name"].lower(): m for m in body["markers"]}

        def find(sub):
            for k, v in by_name.items():
                if sub.lower() in k:
                    return v
            return None

        b12 = find("vitamin b12") or find("b12")
        assert b12 is not None, f"no B12 marker in compare; markers={list(by_name)}"
        assert b12["change"] == "improved"
        assert b12["previous_status"] == "low"
        assert b12["current_status"] == "normal"

        ldl = find("ldl")
        assert ldl is not None
        assert ldl["change"] == "worsened"
        assert ldl["current_status"] == "high"

    def test_compare_against_each_older_id(self, s, ketan_headers):
        r = s.get(f"{API}/reports/compare", headers=ketan_headers)
        body = r.json()
        older_ids = [o["id"] for o in body["older_reports"]]
        assert len(older_ids) == 3
        for oid in older_ids:
            rr = s.get(f"{API}/reports/compare", params={"against": oid}, headers=ketan_headers)
            assert rr.status_code == 200
            b = rr.json()
            assert b["available"] is True
            assert b["against"]["id"] == oid, f"expected against={oid}, got {b['against']['id']}"
            # older_reports list should still contain 3 entries
            assert len(b["older_reports"]) == 3

    def test_compare_invalid_against_falls_back(self, s, ketan_headers):
        # completely unknown but valid-format ObjectId
        r = s.get(f"{API}/reports/compare", params={"against": "5f8d0e5f7b0d5a1a2b3c4d5e"}, headers=ketan_headers)
        assert r.status_code == 200
        b = r.json()
        assert b["available"] is True
        # falls back to the most recent older report (default)
        older_ids = [o["id"] for o in b["older_reports"]]
        assert b["against"]["id"] == older_ids[0]

    def test_compare_garbage_against_falls_back(self, s, ketan_headers):
        # not a valid ObjectId at all - should still fall back, not 500
        r = s.get(f"{API}/reports/compare", params={"against": "not-an-object-id"}, headers=ketan_headers)
        assert r.status_code == 200
        b = r.json()
        assert b["available"] is True
        older_ids = [o["id"] for o in b["older_reports"]]
        assert b["against"]["id"] == older_ids[0]

    def test_compare_requires_auth(self, s):
        r = s.get(f"{API}/reports/compare")
        assert r.status_code == 401
