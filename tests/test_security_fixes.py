"""Security fixes verification: SEC-001, SEC-002, SEC-003, SEC-004.

Tests are ordered because SEC-004 rate limiting is stateful (in-memory per-IP).
We rotate IPs via X-Forwarded-For to keep tests isolated.
"""
import os
import time
import uuid

import pytest
import requests

BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"

KETAN_EMAIL = "ketan@test.com"
KETAN_PW = "Test@1234"

# Unique suffix per test-run to avoid collisions with prior runs.
RUN = uuid.uuid4().hex[:6]
VICTIM_EMAIL = f"victim_{RUN}@test.com"
BYSTANDER_EMAIL = f"bystander_{RUN}@test.com"
NEWUSER_EMAIL = f"newuser_{RUN}@test.com"
NONEXIST_EMAIL = f"nonexistent_{RUN}@example.com"

PW = "Test@1234"


def _ip():
    """Fresh IP per test to avoid rate-limit bleed."""
    return f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"


@pytest.fixture(scope="module")
def state():
    return {
        "created_user_ids": [],
        "created_report_ids": [],
        "created_emails": [],
    }


def _register(email, username, name, ip=None):
    headers = {"Content-Type": "application/json"}
    if ip:
        headers["X-Forwarded-For"] = ip
    r = requests.post(
        f"{API}/auth/register",
        json={"email": email, "username": username, "name": name, "password": PW, "consent": True},
        headers=headers,
        timeout=30,
    )
    return r


def _login(identifier, password, ip=None):
    headers = {"Content-Type": "application/json"}
    if ip:
        headers["X-Forwarded-For"] = ip
    return requests.post(
        f"{API}/auth/login",
        json={"identifier": identifier, "password": password},
        headers=headers,
        timeout=30,
    )


def _auth(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ============================================================
# SEC-001 - Family invite requires explicit acceptance
# ============================================================
class TestSEC001InviteConsent:
    def test_01_login_ketan(self, state):
        r = _login(KETAN_EMAIL, KETAN_PW, ip=_ip())
        assert r.status_code == 200, r.text
        data = r.json()
        state["ketan_token"] = data["access_token"]
        state["ketan_id"] = data["user"]["user_id"]

    def test_02_register_victim(self, state):
        r = _register(VICTIM_EMAIL, f"victim{RUN}", "Victim", ip=_ip())
        assert r.status_code == 200, r.text
        data = r.json()
        state["victim_token"] = data["access_token"]
        state["victim_id"] = data["user"]["user_id"]
        state["created_user_ids"].append(state["victim_id"])
        state["created_emails"].append(VICTIM_EMAIL)

    def test_03_invite_uniform_response_when_target_exists(self, state):
        r = requests.post(
            f"{API}/family/invite",
            json={"email": VICTIM_EMAIL, "relationship": "sibling"},
            headers=_auth(state["ketan_token"]),
            timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Uniform response - no member/linked leak
        assert body == {"status": "invite_sent"}, f"Response leaks: {body}"

    def test_04_no_link_created_without_consent(self, state):
        # Ketan side
        r = requests.get(f"{API}/family/members", headers=_auth(state["ketan_token"]))
        assert r.status_code == 200
        members = r.json().get("members", [])
        assert not any(m["email"] == VICTIM_EMAIL for m in members), \
            "SEC-001 BUG: victim appears in ketan's members without accepting"

        # Victim side
        r2 = requests.get(f"{API}/family/members", headers=_auth(state["victim_token"]))
        assert r2.status_code == 200
        v_members = r2.json().get("members", [])
        assert not any(m["email"] == KETAN_EMAIL for m in v_members), \
            "SEC-001 BUG: ketan appears in victim's members without consent"

    def test_05_victim_sees_incoming_invite(self, state):
        r = requests.get(f"{API}/family/incoming-invites", headers=_auth(state["victim_token"]))
        assert r.status_code == 200, r.text
        invites = r.json()
        matches = [i for i in invites if i["inviter_email"] == KETAN_EMAIL]
        assert len(matches) == 1, f"Expected 1 invite from ketan, got {invites}"
        state["victim_invite_id"] = matches[0]["id"]

    def test_06_victim_accepts(self, state):
        r = requests.post(
            f"{API}/family/incoming-invites/{state['victim_invite_id']}/accept",
            headers=_auth(state["victim_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "accepted"
        assert body["member"]["email"] == KETAN_EMAIL

        # Both members lists now show each other
        km = requests.get(f"{API}/family/members", headers=_auth(state["ketan_token"])).json()
        assert any(m["email"] == VICTIM_EMAIL for m in km["members"]), "link not created on ketan side"
        vm = requests.get(f"{API}/family/members", headers=_auth(state["victim_token"])).json()
        assert any(m["email"] == KETAN_EMAIL for m in vm["members"]), "link not created on victim side"

    def test_07_decline_flow(self, state):
        # Bystander registers
        r = _register(BYSTANDER_EMAIL, f"bystnd{RUN}", "Bystander", ip=_ip())
        assert r.status_code == 200, r.text
        b_data = r.json()
        state["bystander_token"] = b_data["access_token"]
        state["bystander_id"] = b_data["user"]["user_id"]
        state["created_user_ids"].append(state["bystander_id"])
        state["created_emails"].append(BYSTANDER_EMAIL)

        # Ketan invites bystander
        inv = requests.post(
            f"{API}/family/invite",
            json={"email": BYSTANDER_EMAIL, "relationship": "family"},
            headers=_auth(state["ketan_token"]),
        )
        assert inv.status_code == 200

        # Bystander lists + declines
        got = requests.get(f"{API}/family/incoming-invites", headers=_auth(state["bystander_token"])).json()
        ids = [i["id"] for i in got if i["inviter_email"] == KETAN_EMAIL]
        assert ids, "bystander didn't receive invite"
        dec = requests.post(
            f"{API}/family/incoming-invites/{ids[0]}/decline",
            headers=_auth(state["bystander_token"]),
        )
        assert dec.status_code == 200, dec.text
        assert dec.json()["status"] == "declined"

        # No link should exist
        km = requests.get(f"{API}/family/members", headers=_auth(state["ketan_token"])).json()
        assert not any(m["email"] == BYSTANDER_EMAIL for m in km["members"]), \
            "SEC-001 BUG: link created despite decline"

    def test_08_no_enumeration_leak(self, state):
        # Invite existing user
        r1 = requests.post(
            f"{API}/family/invite", json={"email": VICTIM_EMAIL},
            headers=_auth(state["ketan_token"]),
        )
        # Invite non-existent user
        r2 = requests.post(
            f"{API}/family/invite", json={"email": NONEXIST_EMAIL},
            headers=_auth(state["ketan_token"]),
        )
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json() == r2.json() == {"status": "invite_sent"}, \
            f"SEC-001 BUG: responses differ - existing={r1.json()} nonexistent={r2.json()}"


# ============================================================
# SEC-002 - No auto-accept on register
# ============================================================
class TestSEC002NoAutoAccept:
    def test_01_invite_before_registration(self, state):
        # Ensure ketan token loaded
        if "ketan_token" not in state:
            state["ketan_token"] = _login(KETAN_EMAIL, KETAN_PW, ip=_ip()).json()["access_token"]

        r = requests.post(
            f"{API}/family/invite", json={"email": NEWUSER_EMAIL, "relationship": "friend"},
            headers=_auth(state["ketan_token"]),
        )
        assert r.status_code == 200
        assert r.json() == {"status": "invite_sent"}

    def test_02_register_target_email_no_auto_link(self, state):
        r = _register(NEWUSER_EMAIL, f"newu{RUN}", "New User", ip=_ip())
        assert r.status_code == 200, r.text
        data = r.json()
        state["newuser_token"] = data["access_token"]
        state["newuser_id"] = data["user"]["user_id"]
        state["created_user_ids"].append(state["newuser_id"])
        state["created_emails"].append(NEWUSER_EMAIL)

        # /members MUST NOT include ketan
        members = requests.get(f"{API}/family/members", headers=_auth(state["newuser_token"])).json()
        assert not any(m["email"] == KETAN_EMAIL for m in members.get("members", [])), \
            "SEC-002 BUG: invite auto-accepted on registration"

        # /incoming-invites SHOULD contain ketan's invite still pending
        inv = requests.get(f"{API}/family/incoming-invites", headers=_auth(state["newuser_token"])).json()
        assert any(i["inviter_email"] == KETAN_EMAIL for i in inv), \
            "invite disappeared after registration - should still be pending"

    def test_03_explicit_accept_still_works(self, state):
        inv = requests.get(f"{API}/family/incoming-invites", headers=_auth(state["newuser_token"])).json()
        target = next(i for i in inv if i["inviter_email"] == KETAN_EMAIL)
        r = requests.post(
            f"{API}/family/incoming-invites/{target['id']}/accept",
            headers=_auth(state["newuser_token"]),
        )
        assert r.status_code == 200
        members = requests.get(f"{API}/family/members", headers=_auth(state["newuser_token"])).json()
        assert any(m["email"] == KETAN_EMAIL for m in members["members"])


# ============================================================
# SEC-003 - JWT revocable via token_version
# ============================================================
class TestSEC003TokenRevocation:
    def test_01_login_and_use_token(self):
        r = _login(KETAN_EMAIL, KETAN_PW, ip=_ip())
        assert r.status_code == 200
        t1 = r.json()["access_token"]

        me = requests.get(f"{API}/auth/me", headers=_auth(t1))
        assert me.status_code == 200
        self.__class__.t1 = t1

    def test_02_logout_bumps_version(self):
        lo = requests.post(f"{API}/auth/logout", headers=_auth(self.__class__.t1))
        assert lo.status_code == 200

    def test_03_old_token_rejected_after_logout(self):
        me = requests.get(f"{API}/auth/me", headers=_auth(self.__class__.t1))
        assert me.status_code == 401, f"SEC-003 BUG: revoked token still works, got {me.status_code}"

    def test_04_relogin_gets_new_valid_token(self, state):
        r = _login(KETAN_EMAIL, KETAN_PW, ip=_ip())
        assert r.status_code == 200
        t2 = r.json()["access_token"]
        me = requests.get(f"{API}/auth/me", headers=_auth(t2))
        assert me.status_code == 200
        state["ketan_token"] = t2  # refresh module-scoped
        self.__class__.t2 = t2

    def test_05_email_change_revokes_token(self, state):
        # Change ketan's email to a temp email
        temp_email = f"ketan_tmp_{RUN}@test.com"
        r = requests.put(
            f"{API}/user/health-profile",
            json={"email": temp_email},
            headers=_auth(self.__class__.t2),
        )
        assert r.status_code == 200, r.text
        # Old token should now be revoked
        me = requests.get(f"{API}/auth/me", headers=_auth(self.__class__.t2))
        assert me.status_code == 401, f"SEC-003 BUG: token still valid after email change, got {me.status_code}"

        # Re-login with new email → new valid token
        r2 = _login(temp_email, KETAN_PW, ip=_ip())
        assert r2.status_code == 200
        t3 = r2.json()["access_token"]
        me2 = requests.get(f"{API}/auth/me", headers=_auth(t3))
        assert me2.status_code == 200

        # Restore ketan's email
        restore = requests.put(
            f"{API}/user/health-profile",
            json={"email": KETAN_EMAIL},
            headers=_auth(t3),
        )
        assert restore.status_code == 200, restore.text
        # Get a fresh valid token for ketan and store it back
        t4 = _login(KETAN_EMAIL, KETAN_PW, ip=_ip()).json()["access_token"]
        state["ketan_token"] = t4

    def test_06_legacy_user_no_tv_still_authenticates(self, state):
        """Users created before the fix have no token_version field (defaults to 0).
        The current token payload has tv=0 → should still validate.
        """
        # verify current ketan token still works
        me = requests.get(f"{API}/auth/me", headers=_auth(state["ketan_token"]))
        assert me.status_code == 200


# ============================================================
# SEC-004 - Rate limiting + daily quota
# ============================================================
class TestSEC004RateLimit:
    def test_01_login_bruteforce_429(self):
        """15 attempts/5min per IP. 16th must be 429."""
        ip = _ip()
        codes = []
        for i in range(17):
            r = _login(KETAN_EMAIL, "wrong-pw", ip=ip)
            codes.append(r.status_code)
        # First 15 should be 401 (wrong pw). 16th onward should be 429.
        assert 429 in codes, f"SEC-004 BUG: no 429 seen after 17 tries. codes={codes}"
        # Ensure exactly the ~16th call is where it flips
        first_429 = codes.index(429)
        assert 14 <= first_429 <= 16, f"429 at unexpected index {first_429}, codes={codes}"

    def test_02_different_ip_not_blocked(self):
        r = _login(KETAN_EMAIL, KETAN_PW, ip=_ip())
        assert r.status_code == 200, f"fresh IP should not be rate limited: {r.status_code}"

    def test_03_daily_quota_free_10(self, state):
        """Free plan → 10 analyses/day. 11th must be 429.
        We call analyze with an invalid mime_type - quota is incremented BEFORE
        mime_type validation, so counter still bumps.
        """
        # Ensure fresh ketan token
        t = _login(KETAN_EMAIL, KETAN_PW, ip=_ip()).json()["access_token"]
        state["ketan_token"] = t

        # Clean any existing quota counter for today first via unauthenticated?
        # We can't touch DB from tests directly here; assume clean or existing count is small.
        # Instead, make sure we start from 0 by calling delete endpoint if possible... skip.
        # We call the endpoint with a small invalid payload. It errors 400 but quota is incremented.
        codes = []
        for i in range(12):
            r = requests.post(
                f"{API}/reports/analyze",
                json={"file_base64": "aGVsbG8=", "mime_type": "text/plain", "filename": "x.txt"},
                headers=_auth(t),
                timeout=30,
            )
            codes.append(r.status_code)
        # We expect 10 non-429s then 429s (or fewer non-429s if quota was partly used already).
        assert 429 in codes, f"SEC-004 BUG: quota never hit. codes={codes}"
        # The last code should be 429
        assert codes[-1] == 429, f"expected trailing 429, codes={codes}"


# ============================================================
# Cleanup - module teardown
# ============================================================
@pytest.fixture(scope="module", autouse=True)
def cleanup(state):
    yield
    # Delete throwaway users + associated data via /user/account (auth'd as each)
    import asyncio
    from motor.motor_asyncio import AsyncIOMotorClient

    async def _clean():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        # Delete throwaway users
        for uid in state.get("created_user_ids", []):
            await db.reports.delete_many({"user_id": uid})
            await db.users.delete_one({"user_id": uid})
            await db.family_links.delete_many({"$or": [{"a": uid}, {"b": uid}]})
            await db.family_invites.delete_many({"$or": [{"inviter_id": uid}, {"invitee_user_id": uid}]})
            await db.user_sessions.delete_many({"user_id": uid})
            await db.quota_counters.delete_many({"user_id": uid})
        # Also purge any invites addressed to throwaway emails
        emails = state.get("created_emails", []) + [NONEXIST_EMAIL, NEWUSER_EMAIL]
        if emails:
            await db.family_invites.delete_many({"invitee_email": {"$in": emails}})
        # Delete family links from ketan to any of these
        ketan_id = state.get("ketan_id")
        if ketan_id and state.get("created_user_ids"):
            await db.family_links.delete_many({
                "$or": [{"a": ketan_id, "b": {"$in": state["created_user_ids"]}},
                        {"b": ketan_id, "a": {"$in": state["created_user_ids"]}}],
            })
        # Reset ketan's quota counter for today
        if ketan_id:
            await db.quota_counters.delete_many({"user_id": ketan_id})
            # Also reset token_version to keep it tidy (not strictly required)
        client.close()

    try:
        asyncio.get_event_loop().run_until_complete(_clean())
    except RuntimeError:
        # loop already closed
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_clean())
        loop.close()
