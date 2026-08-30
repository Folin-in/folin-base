"""Iteration 9 - backend tests for the newly added features:
Preferences, Family (invite/list/cancel/remove + auto-accept), Export, Delete account,
and Suggestion category normalization (nutrition/movement/sleep/supplements/lifestyle
+ legacy diet/exercise mapping).

CAREFUL: We NEVER delete ketan's account. Delete-account only runs on throwaway users.
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


def _register(s, email=None, username=None, password="Password@123", name="TEST"):
    uid = uuid.uuid4().hex[:8]
    email = email or f"TEST_{uid}@vqtest.com"
    username = username or f"TEST_{uid}"
    r = s.post(f"{API}/auth/register", json={"email": email, "username": username,
                                              "password": password, "name": name, "consent": True})
    assert r.status_code == 200, f"register failed: {r.status_code} {r.text}"
    body = r.json()
    return {
        "token": body["access_token"],
        "user": body["user"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
        "email": email,
        "username": username,
    }


@pytest.fixture(scope="session")
def s():
    return requests.Session()


@pytest.fixture(scope="session")
def ketan(s):
    r = s.post(f"{API}/auth/login", json={"identifier": "ketan", "password": "Test@1234"})
    assert r.status_code == 200
    body = r.json()
    return {"headers": {"Authorization": f"Bearer {body['access_token']}"}, "user": body["user"]}


# ============= PREFERENCES =============
class TestPreferences:
    """GET/PUT /api/user/preferences - appearance/notifications/units + validation."""

    def test_defaults_returned_on_new_user(self, s):
        u = _register(s)
        r = s.get(f"{API}/user/preferences", headers=u["headers"])
        assert r.status_code == 200
        p = r.json()
        assert p["appearance"] == "light"
        assert p["notifications"] == "weekly"
        assert p["units"] == "us"
        assert p["plan"] == "free"

    @pytest.mark.parametrize("appearance", ["light", "dark", "system"])
    def test_update_appearance_persists(self, s, appearance):
        u = _register(s)
        r = s.put(f"{API}/user/preferences", json={"appearance": appearance}, headers=u["headers"])
        assert r.status_code == 200, r.text
        assert r.json()["appearance"] == appearance
        # verify via GET
        r2 = s.get(f"{API}/user/preferences", headers=u["headers"])
        assert r2.json()["appearance"] == appearance

    @pytest.mark.parametrize("notif", ["off", "weekly", "daily"])
    def test_update_notifications_persists(self, s, notif):
        u = _register(s)
        r = s.put(f"{API}/user/preferences", json={"notifications": notif}, headers=u["headers"])
        assert r.status_code == 200
        assert r.json()["notifications"] == notif

    @pytest.mark.parametrize("units", ["us", "si"])
    def test_update_units_persists(self, s, units):
        u = _register(s)
        r = s.put(f"{API}/user/preferences", json={"units": units}, headers=u["headers"])
        assert r.status_code == 200
        assert r.json()["units"] == units

    def test_partial_update_preserves_other_fields(self, s):
        u = _register(s)
        s.put(f"{API}/user/preferences", json={"appearance": "dark", "units": "si"}, headers=u["headers"])
        r = s.put(f"{API}/user/preferences", json={"notifications": "daily"}, headers=u["headers"])
        p = r.json()
        assert p["appearance"] == "dark"
        assert p["units"] == "si"
        assert p["notifications"] == "daily"

    def test_invalid_appearance_400(self, s):
        u = _register(s)
        r = s.put(f"{API}/user/preferences", json={"appearance": "neon"}, headers=u["headers"])
        assert r.status_code == 400

    def test_invalid_notifications_400(self, s):
        u = _register(s)
        r = s.put(f"{API}/user/preferences", json={"notifications": "hourly"}, headers=u["headers"])
        assert r.status_code == 400

    def test_invalid_units_400(self, s):
        u = _register(s)
        r = s.put(f"{API}/user/preferences", json={"units": "metric"}, headers=u["headers"])
        assert r.status_code == 400

    def test_requires_auth(self, s):
        assert s.get(f"{API}/user/preferences").status_code == 401
        assert s.put(f"{API}/user/preferences", json={"units": "si"}).status_code == 401


# ============= FAMILY =============
class TestFamilyInviteExisting:
    """Invite between two ALREADY registered users → instant two-way link."""

    def test_invite_existing_user_creates_two_way_link(self, s):
        alice = _register(s, name="Alice")
        bob = _register(s, name="Bob")
        # Alice invites Bob
        r = s.post(f"{API}/family/invite", json={"email": bob["email"], "relationship": "sibling"}, headers=alice["headers"])
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "linked"

        # Alice sees Bob
        a = s.get(f"{API}/family/members", headers=alice["headers"]).json()
        assert any(m["email"].lower() == bob["email"].lower() for m in a["members"])
        assert a["self"]["email"].lower() == alice["email"].lower()
        assert a["self"]["relationship"] == "you"

        # Bob sees Alice too (two-way)
        b = s.get(f"{API}/family/members", headers=bob["headers"]).json()
        assert any(m["email"].lower() == alice["email"].lower() for m in b["members"])

    def test_cannot_invite_self(self, s):
        u = _register(s)
        r = s.post(f"{API}/family/invite", json={"email": u["email"], "relationship": "family"}, headers=u["headers"])
        assert r.status_code == 400
        assert "yourself" in r.json()["detail"].lower()

    def test_cannot_double_invite_existing_link(self, s):
        alice = _register(s)
        bob = _register(s)
        s.post(f"{API}/family/invite", json={"email": bob["email"]}, headers=alice["headers"])
        r = s.post(f"{API}/family/invite", json={"email": bob["email"]}, headers=alice["headers"])
        assert r.status_code == 400
        assert "already sharing" in r.json()["detail"].lower()

    def test_remove_member_unlinks_both_sides(self, s):
        alice = _register(s)
        bob = _register(s)
        s.post(f"{API}/family/invite", json={"email": bob["email"]}, headers=alice["headers"])
        # Alice removes Bob
        r = s.delete(f"{API}/family/members/{bob['user']['user_id']}", headers=alice["headers"])
        assert r.status_code == 200
        assert r.json() == {"removed": True}
        # Neither side should see the other
        a = s.get(f"{API}/family/members", headers=alice["headers"]).json()
        assert not any(m["email"].lower() == bob["email"].lower() for m in a["members"])
        b = s.get(f"{API}/family/members", headers=bob["headers"]).json()
        assert not any(m["email"].lower() == alice["email"].lower() for m in b["members"])


class TestFamilyInvitePending:
    """Invite a NOT-yet-registered email → pending invite; auto-accept on register."""

    def test_invite_non_existing_creates_pending(self, s):
        alice = _register(s)
        target_email = f"TEST_pending_{uuid.uuid4().hex[:8]}@vqtest.com"
        r = s.post(f"{API}/family/invite", json={"email": target_email, "relationship": "parent"}, headers=alice["headers"])
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "pending"
        assert body["invite"]["email"] == target_email.lower()

        # /family/invites lists it
        invites = s.get(f"{API}/family/invites", headers=alice["headers"]).json()
        assert any(i["email"] == target_email.lower() for i in invites)

    def test_double_invite_same_email_400(self, s):
        alice = _register(s)
        target = f"TEST_dupe_{uuid.uuid4().hex[:6]}@vqtest.com"
        s.post(f"{API}/family/invite", json={"email": target}, headers=alice["headers"])
        r = s.post(f"{API}/family/invite", json={"email": target}, headers=alice["headers"])
        assert r.status_code == 400

    def test_cancel_pending_invite(self, s):
        alice = _register(s)
        target = f"TEST_cancel_{uuid.uuid4().hex[:6]}@vqtest.com"
        inv = s.post(f"{API}/family/invite", json={"email": target}, headers=alice["headers"]).json()
        inv_id = inv["invite"]["id"]
        r = s.delete(f"{API}/family/invites/{inv_id}", headers=alice["headers"])
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        # No longer listed
        remaining = s.get(f"{API}/family/invites", headers=alice["headers"]).json()
        assert not any(i["id"] == inv_id for i in remaining)

    def test_cancel_missing_invite_404(self, s):
        alice = _register(s)
        r = s.delete(f"{API}/family/invites/nonexistent_id_xyz", headers=alice["headers"])
        assert r.status_code == 404

    def test_auto_accept_on_register(self, s):
        """(a) Alice invites new email; (b) that email registers → both see each other."""
        alice = _register(s)
        pending_email = f"TEST_autoacc_{uuid.uuid4().hex[:8]}@vqtest.com"
        pending_username = f"TEST_aa_{uuid.uuid4().hex[:6]}"
        r = s.post(f"{API}/family/invite", json={"email": pending_email, "relationship": "partner"}, headers=alice["headers"])
        assert r.json()["status"] == "pending"

        # Register the invitee
        bob = _register(s, email=pending_email, username=pending_username, name="Bob AA")

        # Bob's /family/members should include Alice immediately
        b = s.get(f"{API}/family/members", headers=bob["headers"]).json()
        alice_in_bob = [m for m in b["members"] if m["email"].lower() == alice["email"].lower()]
        assert alice_in_bob, f"Alice not auto-linked in Bob's members. Members={b['members']}"
        # And Alice sees Bob
        a = s.get(f"{API}/family/members", headers=alice["headers"]).json()
        assert any(m["email"].lower() == pending_email.lower() for m in a["members"])

        # Alice's pending invites should no longer contain that email
        pending_after = s.get(f"{API}/family/invites", headers=alice["headers"]).json()
        assert not any(i["email"] == pending_email.lower() for i in pending_after)


class TestFamilyMemberShape:
    """Ensure /family/members returns SAFE shape only (no biomarkers, no _id leakage)."""

    def test_members_response_shape(self, s):
        alice = _register(s)
        bob = _register(s)
        s.post(f"{API}/family/invite", json={"email": bob["email"]}, headers=alice["headers"])
        body = s.get(f"{API}/family/members", headers=alice["headers"]).json()
        assert "self" in body and "members" in body
        me = body["self"]
        for k in ("user_id", "email", "name", "score", "score_trend", "level", "reports_count", "relationship"):
            assert k in me, f"self missing key: {k}"
        # No raw biomarkers or _id
        assert "biomarkers" not in me
        assert "_id" not in me
        for m in body["members"]:
            assert "biomarkers" not in m
            assert "_id" not in m

    def test_family_requires_auth(self, s):
        assert s.get(f"{API}/family/members").status_code == 401
        assert s.get(f"{API}/family/invites").status_code == 401
        assert s.post(f"{API}/family/invite", json={"email": "x@x.com"}).status_code == 401


# ============= EXPORT =============
class TestExport:
    def test_export_returns_user_and_reports(self, s, ketan):
        r = s.get(f"{API}/user/export", headers=ketan["headers"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert "exported_at" in body
        assert "user" in body
        assert body["user"]["email"] == "ketan@test.com"
        assert isinstance(body.get("reports"), list)
        assert len(body["reports"]) >= 4, f"expected 4+ reports, got {len(body['reports'])}"
        # Reports should include biomarkers (raw for owner)
        assert "biomarkers" in body["reports"][0]

    def test_export_new_user_has_empty_reports(self, s):
        u = _register(s)
        r = s.get(f"{API}/user/export", headers=u["headers"])
        assert r.status_code == 200
        body = r.json()
        assert body["reports"] == []
        assert body["user"]["email"] == u["email"].lower()

    def test_export_requires_auth(self, s):
        assert s.get(f"{API}/user/export").status_code == 401


# ============= DELETE ACCOUNT =============
class TestDeleteAccount:
    """CAREFUL - only run on throwaway users, never ketan."""

    def test_delete_removes_user_reports_links_invites(self, s):
        alice = _register(s)
        bob = _register(s)
        # Alice invites Bob (link) and pending
        s.post(f"{API}/family/invite", json={"email": bob["email"]}, headers=alice["headers"])
        pending_email = f"TEST_del_pending_{uuid.uuid4().hex[:6]}@vqtest.com"
        s.post(f"{API}/family/invite", json={"email": pending_email}, headers=alice["headers"])

        # Delete Alice's account
        r = s.delete(f"{API}/user/account", headers=alice["headers"])
        assert r.status_code == 200
        assert r.json()["deleted"] is True

        # Alice's token no longer works
        me = s.get(f"{API}/auth/me", headers=alice["headers"])
        assert me.status_code == 401

        # Bob shouldn't see Alice anymore
        b = s.get(f"{API}/family/members", headers=bob["headers"]).json()
        assert not any(m["email"].lower() == alice["email"].lower() for m in b["members"])

        # Cannot log in as Alice anymore
        r = s.post(f"{API}/auth/login", json={"identifier": alice["email"], "password": "Password@123"})
        assert r.status_code == 401

    def test_delete_requires_auth(self, s):
        assert s.delete(f"{API}/user/account").status_code == 401


# ============= SUGGESTION CATEGORY NORMALIZATION =============
class TestSuggestionCategoryNormalization:
    """Legacy 'diet' → 'nutrition' and 'exercise' → 'movement' when read via GET /reports/{id}."""

    def test_ketan_reports_categories_are_in_new_set(self, s, ketan):
        r = s.get(f"{API}/reports", headers=ketan["headers"])
        reports = r.json()
        assert len(reports) >= 1
        allowed = {"nutrition", "movement", "sleep", "supplements", "lifestyle"}
        for rep in reports:
            rid = rep["id"]
            detail = s.get(f"{API}/reports/{rid}", headers=ketan["headers"]).json()
            for sug in detail.get("suggestions") or []:
                assert sug["category"] in allowed, (
                    f"report {rid} suggestion category '{sug['category']}' NOT in new taxonomy - "
                    f"legacy 'diet'/'exercise' should have been normalized"
                )
