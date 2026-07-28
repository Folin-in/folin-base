"""IT19 security audit verification tests.

Covers 6 fixes:
  1. SEC-001       - HTML escape in family-invite emails
  2. SEC-002       - Invites bound to user_id; unverified email change can't steal invites
  3. HARDENING-1   - X-Forwarded-For only honored from trusted proxy peer
  4. HARDENING-2   - CORS allow-list parsing (env-driven)
  5. HARDENING-3   - 20/day per-user invite cap
  6. HARDENING-4   - Base64 payload pre-check (413) on /reports/analyze

The email test monkey-patches `resend.Emails.send` so no real network call is made.
Everything else hits the live backend via EXPO_PUBLIC_BACKEND_URL.
"""
import asyncio
import os
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

import pytest
import requests

# ---- Path setup so we can import backend modules directly ----
sys.path.insert(0, "/app/backend")

BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"

KETAN_EMAIL = "ketan@test.com"
KETAN_PW = "Test@1234"
PW = "Test@1234"

RUN = uuid.uuid4().hex[:6]


def _ip():
    return f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"


def _register(email, username, name, ip=None):
    headers = {"Content-Type": "application/json"}
    if ip:
        headers["X-Forwarded-For"] = ip
    return requests.post(
        f"{API}/auth/register",
        json={"email": email, "username": username, "name": name, "password": PW},
        headers=headers,
        timeout=30,
    )


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


@pytest.fixture(scope="module")
def state():
    return {"created_user_ids": [], "created_emails": []}


# ============================================================
# 1. SEC-001 - HTML escape in invite emails
# ============================================================
class TestSEC001EmailEscape:
    def _invoke(self, monkeypatch, inviter_name, invitee_email="target@test.com", relationship="family"):
        import config as _config
        import email_service as _es
        # Force the module to think Resend is configured.
        monkeypatch.setattr(_config, "RESEND_API_KEY", "test_key_123", raising=False)
        monkeypatch.setattr(_es, "_RESEND_AVAILABLE", True, raising=False)

        captured = {}

        class _MockEmails:
            @staticmethod
            def send(payload):
                captured["payload"] = payload
                return {"id": "mock_" + uuid.uuid4().hex[:8]}

        # Patch the module's `resend` reference so send_family_invite hits the mock.
        mock_resend = SimpleNamespace(Emails=_MockEmails, api_key=None)
        monkeypatch.setattr(_es, "resend", mock_resend, raising=False)

        email_id = _es.send_family_invite(inviter_name, invitee_email, relationship)
        return email_id, captured.get("payload")

    def test_01_script_tag_is_escaped(self, monkeypatch):
        email_id, payload = self._invoke(monkeypatch, "<script>alert(1)</script>")
        assert email_id, "mock send returned None - send_family_invite short-circuited"
        html = payload["html"]
        assert "<script>" not in html, "Raw <script> tag leaked into HTML body"
        assert "alert(1)" not in html or "&lt;script&gt;" in html, "script payload not escaped"
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html, "expected escaped <script>"

    def test_02_img_onerror_is_escaped(self, monkeypatch):
        _, payload = self._invoke(monkeypatch, '<img src=x onerror=alert(1)>')
        html = payload["html"]
        # The critical property: no raw <img> tag survives (browser can't execute the
        # onerror handler when the surrounding `<` is escaped to `&lt;`). The literal
        # substring "onerror=" as escaped text content is harmless.
        assert "<img" not in html.lower(), "Raw <img> tag leaked into HTML body"
        assert "&lt;img src=x onerror=alert(1)&gt;" in html, "expected escaped <img> payload"

    def test_03_email_field_also_escaped(self, monkeypatch):
        # invitee_email is echoed into the body; ensure it's escaped too.
        _, payload = self._invoke(monkeypatch, "Alice", invitee_email='"><script>x</script>@evil.com')
        html = payload["html"]
        assert "<script>x</script>" not in html
        assert "&lt;script&gt;x&lt;/script&gt;" in html

    def test_04_relationship_field_escaped(self, monkeypatch):
        _, payload = self._invoke(monkeypatch, "Alice", relationship="<b>friend</b>")
        html = payload["html"]
        assert "<b>friend</b>" not in html
        # capitalized + escaped -> "&lt;b&gt;friend&lt;/b&gt;"
        assert "&lt;b&gt;friend&lt;/b&gt;" in html

    def test_05_subject_strips_crlf(self, monkeypatch):
        _, payload = self._invoke(monkeypatch, "hax\r\ninject: header")
        subject = payload["subject"]
        assert "\r" not in subject and "\n" not in subject, f"Subject contains raw CRLF: {subject!r}"
        assert len(subject) <= 100, "subject unbounded"

    def test_06_subject_capped_at_reasonable_length(self, monkeypatch):
        very_long_name = "A" * 500
        _, payload = self._invoke(monkeypatch, very_long_name)
        subject = payload["subject"]
        # name is capped at 60 + " invited you to VitalQuest" suffix
        assert len(subject) <= 120


# ============================================================
# 3. HARDENING-1 - _client_ip trusts XFF only from trusted proxy
# ============================================================
class TestHardening1ClientIp:
    def _mock_req(self, peer, headers=None):
        return SimpleNamespace(
            client=SimpleNamespace(host=peer),
            headers=headers or {},
        )

    def test_01_trusted_peer_honors_xff(self):
        import rate_limit
        req = self._mock_req("127.0.0.1", {"x-forwarded-for": "1.2.3.4"})
        assert rate_limit._client_ip(req) == "1.2.3.4"

    def test_02_untrusted_peer_ignores_xff(self):
        import rate_limit
        req = self._mock_req("8.8.8.8", {"x-forwarded-for": "1.2.3.4"})
        assert rate_limit._client_ip(req) == "8.8.8.8"

    def test_03_trusted_cidr_honors_xff(self):
        import rate_limit
        req = self._mock_req("10.5.6.7", {"x-forwarded-for": "9.9.9.9"})
        assert rate_limit._client_ip(req) == "9.9.9.9"

    def test_04_trusted_peer_uses_leftmost_xff(self):
        import rate_limit
        req = self._mock_req("127.0.0.1", {"x-forwarded-for": "1.2.3.4, 5.6.7.8"})
        assert rate_limit._client_ip(req) == "1.2.3.4"

    def test_05_no_xff_returns_peer(self):
        import rate_limit
        req = self._mock_req("127.0.0.1", {})
        assert rate_limit._client_ip(req) == "127.0.0.1"

    def test_06_rate_limit_keys_on_xff_when_trusted(self):
        """End-to-end: 10 hits from same XFF should throttle even though peer is same."""
        import rate_limit
        from fastapi import HTTPException
        # Use a unique bucket name so this test is self-contained.
        bucket = f"test_bucket_{uuid.uuid4().hex[:8]}"
        req = self._mock_req("127.0.0.1", {"x-forwarded-for": "77.77.77.77"})
        for _ in range(3):
            rate_limit.check_ip_rate_limit(req, bucket, max_hits=3, window_seconds=60)
        with pytest.raises(HTTPException) as exc:
            rate_limit.check_ip_rate_limit(req, bucket, max_hits=3, window_seconds=60)
        assert exc.value.status_code == 429

        # Different XFF from trusted peer -> different key -> should pass.
        req2 = self._mock_req("127.0.0.1", {"x-forwarded-for": "88.88.88.88"})
        rate_limit.check_ip_rate_limit(req2, bucket, max_hits=3, window_seconds=60)


# ============================================================
# 4. HARDENING-2 - CORS allow-list parsing
# ============================================================
class TestHardening2Cors:
    def test_01_default_is_wildcard(self):
        import config
        assert config.CORS_ALLOWED_ORIGINS == ["*"], (
            f"Expected wildcard default in preview, got {config.CORS_ALLOWED_ORIGINS}"
        )

    def test_02_subprocess_parses_comma_list(self):
        # Fresh interpreter with the env override; ensure the module parses correctly.
        # Preserve all real env so config.py doesn't KeyError on MONGO_URL/DB_NAME/JWT_SECRET.
        env = os.environ.copy()
        env["CORS_ALLOWED_ORIGINS"] = "https://foo.com,https://bar.com"
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, '/app/backend'); "
                "import config; print(repr(config.CORS_ALLOWED_ORIGINS))",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert proc.returncode == 0, f"subprocess failed: {proc.stderr}"
        out = proc.stdout.strip()
        assert "['https://foo.com', 'https://bar.com']" in out, (
            f"CORS list not parsed correctly. stdout={out!r}"
        )

    def test_03_subprocess_ignores_blank_entries(self):
        env = os.environ.copy()
        env["CORS_ALLOWED_ORIGINS"] = "https://a.com, ,https://b.com,"
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, '/app/backend'); "
                "import config; print(repr(config.CORS_ALLOWED_ORIGINS))",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert proc.returncode == 0
        assert "['https://a.com', 'https://b.com']" in proc.stdout


# ============================================================
# 2. SEC-002 - Invite bound to invitee_user_id
# ============================================================
class TestSEC002InviteBinding:
    def test_01_setup(self, state):
        # Attacker (A) - logs into Ketan as convenience
        r = _login(KETAN_EMAIL, KETAN_PW, ip=_ip())
        assert r.status_code == 200, r.text
        state["A_token"] = r.json()["access_token"]
        state["A_id"] = r.json()["user"]["user_id"]

    def test_02_A_invites_nonexistent_email(self, state):
        target = f"sec002_target_{RUN}@example.com"
        state["target_email"] = target
        r = requests.post(
            f"{API}/family/invite",
            json={"email": target, "relationship": "family"},
            headers=_auth(state["A_token"]),
        )
        assert r.status_code == 200, r.text
        # At this point invitee_user_id must be None (target doesn't exist yet).
        # We confirm indirectly via the D-attack below.

    def test_03_legit_user_C_registers_with_target_email_and_can_accept(self, state):
        r = _register(state["target_email"], f"c{RUN}", "C legit", ip=_ip())
        assert r.status_code == 200, r.text
        c = r.json()
        state["C_token"] = c["access_token"]
        state["C_id"] = c["user"]["user_id"]
        state["created_user_ids"].append(state["C_id"])
        state["created_emails"].append(state["target_email"])

        got = requests.get(f"{API}/family/incoming-invites", headers=_auth(state["C_token"]))
        assert got.status_code == 200, got.text
        invites = got.json()
        # Legacy fallback: invitee_user_id None + email match -> visible.
        matches = [i for i in invites if i["inviter_email"] == KETAN_EMAIL]
        assert matches, f"Legit invitee C should see the invite, got {invites}"
        state["C_invite_id"] = matches[0]["id"]

    def test_04_D_email_change_cannot_steal_invite(self, state):
        # A creates a *second* invite while C already exists; this one binds invitee_user_id -> C.
        second_target = f"sec002_bound_{RUN}@example.com"
        # First: register a placeholder user with this email so binding happens
        r_placeholder = _register(second_target, f"ph{RUN}", "Placeholder", ip=_ip())
        assert r_placeholder.status_code == 200, r_placeholder.text
        ph_id = r_placeholder.json()["user"]["user_id"]
        state["created_user_ids"].append(ph_id)
        state["created_emails"].append(second_target)
        ph_token = r_placeholder.json()["access_token"]

        inv = requests.post(
            f"{API}/family/invite",
            json={"email": second_target, "relationship": "family"},
            headers=_auth(state["A_token"]),
        )
        assert inv.status_code == 200, inv.text

        # Now attacker D registers with a random email.
        d_email = f"d_attack_{RUN}@example.com"
        r_d = _register(d_email, f"d{RUN}", "D attacker", ip=_ip())
        assert r_d.status_code == 200, r_d.text
        d_data = r_d.json()
        d_token = d_data["access_token"]
        d_id = d_data["user"]["user_id"]
        state["created_user_ids"].append(d_id)
        state["created_emails"].append(d_email)

        # D changes email to target's email via PUT /api/user/health-profile
        # (endpoint may not exist; try both common paths and skip if it doesn't).
        change_ok = False
        for path in ["/user/health-profile", "/users/health-profile", "/user/profile", "/users/me"]:
            r_ch = requests.put(
                f"{API}{path}",
                json={"email": second_target},
                headers=_auth(d_token),
            )
            if r_ch.status_code in (200, 201):
                change_ok = True
                break

        # Regardless of whether the email change succeeded, D should NOT see the bound invite.
        got = requests.get(f"{API}/family/incoming-invites", headers=_auth(d_token))
        assert got.status_code == 200
        d_invites = got.json()
        bound_matches = [i for i in d_invites if i["inviter_email"] == KETAN_EMAIL]
        assert not bound_matches, (
            f"SEC-002 BUG: attacker D sees invite bound to another user. "
            f"change_ok={change_ok} invites={d_invites}"
        )

        # Also: D attempting to accept by known id from placeholder's view should 404.
        pg = requests.get(f"{API}/family/incoming-invites", headers=_auth(ph_token))
        assert pg.status_code == 200
        ph_invites = [i for i in pg.json() if i["inviter_email"] == KETAN_EMAIL]
        assert ph_invites, "placeholder should see its own bound invite"
        bound_invite_id = ph_invites[0]["id"]

        r_acc = requests.post(
            f"{API}/family/incoming-invites/{bound_invite_id}/accept",
            headers=_auth(d_token),
        )
        assert r_acc.status_code == 404, (
            f"SEC-002 BUG: attacker accept returned {r_acc.status_code}. body={r_acc.text}"
        )

    def test_05_legit_C_can_still_accept(self, state):
        r = requests.post(
            f"{API}/family/incoming-invites/{state['C_invite_id']}/accept",
            headers=_auth(state["C_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "accepted"


# ============================================================
# 5. HARDENING-3 - 20/day invite cap
# ============================================================
class TestHardening3InviteCap:
    def test_01_seed_user_and_send_20(self, state):
        # Register a fresh throwaway user
        email = f"h3_sender_{RUN}@example.com"
        r = _register(email, f"h3{RUN}", "H3 Sender", ip=_ip())
        assert r.status_code == 200, r.text
        data = r.json()
        state["h3_token"] = data["access_token"]
        state["h3_id"] = data["user"]["user_id"]
        state["created_user_ids"].append(state["h3_id"])
        state["created_emails"].append(email)

        for i in range(20):
            target = f"h3_target_{i}_{RUN}@example.com"
            state["created_emails"].append(target)
            r = requests.post(
                f"{API}/family/invite",
                json={"email": target, "relationship": "family"},
                headers=_auth(state["h3_token"]),
                timeout=15,
            )
            assert r.status_code == 200, (
                f"Invite #{i+1} should succeed but got {r.status_code}: {r.text}"
            )

    def test_02_21st_invite_returns_429(self, state):
        target = f"h3_target_over_{RUN}@example.com"
        state["created_emails"].append(target)
        r = requests.post(
            f"{API}/family/invite",
            json={"email": target, "relationship": "family"},
            headers=_auth(state["h3_token"]),
            timeout=15,
        )
        assert r.status_code == 429, f"expected 429, got {r.status_code}: {r.text}"
        detail = (r.json().get("detail") or "").lower()
        assert "20 invites" in detail or "sent 20" in detail, (
            f"expected 20-invite mention in detail, got: {detail!r}"
        )

    def test_03_analyze_quota_bucket_independent(self, state):
        """Sending invites should NOT have consumed the reports_analyze quota bucket."""
        from motor.motor_asyncio import AsyncIOMotorClient

        async def _peek():
            client = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = client[os.environ["DB_NAME"]]
            day = time.strftime("%Y-%m-%d", time.gmtime())
            invite_doc = await db.quota_counters.find_one(
                {"_id": f"family_invite_daily:{state['h3_id']}:{day}"}
            )
            analyze_doc = await db.quota_counters.find_one(
                {"_id": f"reports_analyze:{state['h3_id']}:{day}"}
            )
            client.close()
            return invite_doc, analyze_doc

        invite_doc, analyze_doc = asyncio.get_event_loop().run_until_complete(_peek())
        assert invite_doc is not None, "family_invite_daily counter missing"
        assert invite_doc.get("count", 0) >= 20, f"invite counter unexpectedly low: {invite_doc}"
        # analyze bucket must remain unaffected (either absent, or 0)
        analyze_count = (analyze_doc or {}).get("count", 0)
        assert analyze_count == 0, (
            f"reports_analyze bucket was incremented by invite flow: {analyze_doc}"
        )


# ============================================================
# 6. HARDENING-4 - Pre-check 30MB payload rejection
# ============================================================
class TestHardening4PayloadSize:
    def test_01_login_ketan(self, state):
        r = _login(KETAN_EMAIL, KETAN_PW, ip=_ip())
        assert r.status_code == 200, r.text
        state["ketan_token"] = r.json()["access_token"]
        state["ketan_id"] = r.json()["user"]["user_id"]

    def test_02_thirty_mb_rejected_with_413(self, state):
        big = "A" * (30 * 1024 * 1024)
        t0 = time.monotonic()
        r = requests.post(
            f"{API}/reports/analyze",
            json={
                "filename": "big.pdf",
                "mime_type": "application/pdf",
                "file_base64": big,
            },
            headers=_auth(state["ketan_token"]),
            timeout=120,
        )
        elapsed = time.monotonic() - t0
        assert r.status_code == 413, f"expected 413, got {r.status_code}: {r.text[:200]}"
        detail = (r.json().get("detail") or "").lower()
        assert "too large" in detail, f"detail should mention size, got: {detail!r}"
        # The pre-check runs before base64.b64decode, so response should be fast even for
        # a 30MB payload. Upload time dominates, but total should still be reasonable.
        print(f"[HARDENING-4] 30MB payload rejected in {elapsed:.2f}s")

    def test_03_analyze_quota_not_incremented(self, state):
        """The pre-check runs BEFORE check_and_increment_daily_quota, so ketan's counter
        must be unchanged by the rejected upload."""
        from motor.motor_asyncio import AsyncIOMotorClient

        async def _peek():
            client = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = client[os.environ["DB_NAME"]]
            day = time.strftime("%Y-%m-%d", time.gmtime())
            doc = await db.quota_counters.find_one(
                {"_id": f"reports_analyze:{state['ketan_id']}:{day}"}
            )
            client.close()
            return doc

        doc = asyncio.get_event_loop().run_until_complete(_peek())
        state["ketan_analyze_before"] = (doc or {}).get("count", 0)
        # Just record - we assert the fact that we didn't consume any *new* slot by
        # sending another oversized payload and re-checking.
        big = "B" * (29 * 1024 * 1024)
        r = requests.post(
            f"{API}/reports/analyze",
            json={"filename": "b.pdf", "mime_type": "application/pdf", "file_base64": big},
            headers=_auth(state["ketan_token"]),
            timeout=120,
        )
        assert r.status_code == 413
        doc2 = asyncio.get_event_loop().run_until_complete(_peek())
        count_after = (doc2 or {}).get("count", 0)
        assert count_after == state["ketan_analyze_before"], (
            f"HARDENING-4 BUG: quota moved from {state['ketan_analyze_before']} -> {count_after} "
            f"after oversized upload was rejected."
        )

    def test_04_bad_mime_type_still_400(self, state):
        r = requests.post(
            f"{API}/reports/analyze",
            json={"filename": "x.txt", "mime_type": "text/plain", "file_base64": "SGVsbG8="},
            headers=_auth(state["ketan_token"]),
            timeout=30,
        )
        assert r.status_code == 400, r.text


# ============================================================
# Cleanup
# ============================================================
@pytest.fixture(scope="module", autouse=True)
def cleanup(state):
    yield
    from motor.motor_asyncio import AsyncIOMotorClient

    async def _clean():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        for uid in state.get("created_user_ids", []):
            await db.reports.delete_many({"user_id": uid})
            await db.users.delete_one({"user_id": uid})
            await db.family_links.delete_many({"$or": [{"a": uid}, {"b": uid}]})
            await db.family_invites.delete_many(
                {"$or": [{"inviter_id": uid}, {"invitee_user_id": uid}]}
            )
            await db.user_sessions.delete_many({"user_id": uid})
            await db.quota_counters.delete_many({"user_id": uid})
        emails = state.get("created_emails", [])
        if emails:
            await db.family_invites.delete_many({"invitee_email": {"$in": emails}})
        # Also drop family links Ketan may have with our throwaway users.
        ketan_id = state.get("ketan_id") or state.get("A_id")
        if ketan_id:
            await db.family_links.delete_many({
                "$or": [
                    {"a": ketan_id, "b": {"$in": state["created_user_ids"]}},
                    {"b": ketan_id, "a": {"$in": state["created_user_ids"]}},
                ],
            })
            # Delete any test invites Ketan sent to our throwaway emails
            if emails:
                await db.family_invites.delete_many({
                    "inviter_id": ketan_id,
                    "invitee_email": {"$in": emails},
                })
            # Reset Ketan's daily invite counter so we don't burn his real quota.
            day = time.strftime("%Y-%m-%d", time.gmtime())
            await db.quota_counters.delete_one(
                {"_id": f"family_invite_daily:{ketan_id}:{day}"}
            )
        client.close()

    try:
        asyncio.get_event_loop().run_until_complete(_clean())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_clean())
        loop.close()
