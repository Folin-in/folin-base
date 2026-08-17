"""Cross-tenant isolation: user A must not be able to read, modify, or even confirm the
existence of anything belonging to user B - by id, by header, or by body field.

This is the test that says "the APIs are user_id gated" as a fact rather than a design
intention. Every route that takes an id or acts on owned data is exercised from the
WRONG account. The only acceptable answers are 404 (not 403 - a 403 confirms the
resource exists) or, for list routes, an empty result.

    ./venv/bin/python -m pytest tests/test_tenant_isolation.py -v
"""
import asyncio
import base64
import uuid

import httpx
import pytest

import services.analysis as A
from app.main import app
from core import rate_limit
from db import session as db

_LOOP = asyncio.get_event_loop()
_ready = False
_PNG_RAW = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==") * 40

ACCEPT = {"verdict": "accept", "reason": None, "evidence": None, "document_kind": "lab"}
LAB = {"report_type": "lab", "patient_name": "Owner", "provider": "Lab", "report_date": "2026-03-02",
       "summary": "ok", "overall_assessment": "not a diagnosis - see a doctor", "positives": [], "concerns": [],
       "suggestions": [{"title": "t", "body": "b", "category": "nutrition", "linked_marker": "Haemoglobin"}],
       "biomarkers": [{"name": "Haemoglobin", "value": "11.4", "unit": "g/dL", "ref_range": "13-17", "status": "low",
                       "category": "Blood", "explanation": "x", "tip": "y"}],
       "pages_coherent": True, "pages_issue": None}


class FakeClient:
    def __init__(self): self.replies = []
    async def call(self, *, system_prompt, user_text, files=(), meta=None):
        r = self.replies.pop(0)
        if meta is not None: meta.trajectory_id, meta.model = None, "fake"
        return r


fake = FakeClient()
for agent in (A.pipeline.screener, A.pipeline.extractor, A.pipeline.verifier):
    agent._client = fake


def run(coro):
    async def w():
        global _ready
        if not _ready:
            await db.connect(); _ready = True
        rate_limit._WINDOWS.clear(); fake.replies.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await coro(c)
    return _LOOP.run_until_complete(w())


async def user(c, plan="pro"):
    email = f"iso_{uuid.uuid4().hex[:8]}@example.com"
    r = await c.post("/api/auth/register", json={"email": email, "username": f"i{uuid.uuid4().hex[:6]}",
                                                 "password": "Str0ngPass1", "name": "Iso", "consent": True})
    uid = r.json()["user"]["user_id"]
    if plan == "pro":
        await db.execute("update users set preferences = preferences || '{\"plan\": \"pro\"}' where user_id = $1", uid)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, uid, email


async def owned_report(c, h):
    fake.replies += [ACCEPT, LAB]
    b64 = base64.b64encode(_PNG_RAW + uuid.uuid4().bytes).decode()
    r = await c.post("/api/reports/analyze", headers=h, json={"files": [{"filename": "p.png", "mime_type": "image/png", "file_base64": b64}]})
    job = r.json()["job_id"]
    for _ in range(50):
        await asyncio.sleep(0.1)
        st = (await c.get(f"/api/reports/analyze/status/{job}", headers=h)).json()
        if st["status"] in ("success", "error"): break
    assert st["status"] == "success", st
    return st["report_id"], job


async def cleanup(*emails):
    for e in emails: await db.execute("delete from users where email = $1", e)


# ------------------------------------------------------------ reports & jobs

def test_cannot_read_modify_or_delete_another_users_report():
    async def t(c):
        ha, a_id, ea = await user(c); hb, b_id, eb = await user(c)
        rid, job = await owned_report(c, ha)
        # B attempts everything on A's report
        assert (await c.get(f"/api/reports/{rid}", headers=hb)).status_code == 404
        assert (await c.patch(f"/api/reports/{rid}", headers=hb, json={"title": "pwned"})).status_code == 404
        assert (await c.patch(f"/api/reports/{rid}/biomarkers", headers=hb, json={"index": 0, "value": "1"})).status_code == 404
        assert (await c.delete(f"/api/reports/{rid}", headers=hb)).status_code == 404
        assert (await c.post(f"/api/reports/{rid}/feedback", headers=hb, json={"correct": True, "issues": [], "note": ""})).status_code == 404
        assert (await c.get(f"/api/reports/{rid}/feedback", headers=hb)).json() == []
        assert (await c.get(f"/api/reports/analyze/status/{job}", headers=hb)).status_code == 404
        # nothing changed
        row = await db.fetchrow("select title, deleted_at, biomarkers from reports where id = $1::uuid", rid)
        assert row["title"] is None and row["deleted_at"] is None and row["biomarkers"][0]["value"] == "11.4"
        # A's own list is intact, B's list does not contain it
        assert [r["id"] for r in (await c.get("/api/reports", headers=ha)).json()] == [rid]
        assert (await c.get("/api/reports", headers=hb)).json() == []
        await cleanup(ea, eb)
    run(t)


def test_member_id_query_param_cannot_reach_another_users_member():
    """member_id is a query param on several routes - it must only ever select among the
    CALLER's own members, never index into someone else's."""
    async def t(c):
        ha, a_id, ea = await user(c); hb, b_id, eb = await user(c)
        m = (await c.post("/api/members", headers=ha, json={"name": "Mum", "relation": "parent"})).json()
        mid = m["member_id"]
        # B passes A's member_id everywhere it is accepted
        for path in (f"/api/reports?member_id={mid}", f"/api/reports/suggestions?member_id={mid}",
                     f"/api/reports/compare?member_id={mid}", f"/api/profile?member_id={mid}"):
            r = await c.get(path, headers=hb)
            assert r.status_code in (200, 404), f"{path}: {r.status_code}"
            if r.status_code == 200:
                body = r.json()
                assert body in ([], {"available": False, "reports_count": 0}) or "Mum" not in str(body), f"{path} leaked: {str(body)[:120]}"
        assert (await c.patch(f"/api/members/{mid}", headers=hb, json={"name": "pwned"})).status_code == 404
        assert (await c.delete(f"/api/members/{mid}", headers=hb)).status_code == 404
        assert (await db.fetchval("select name from members where member_id = $1", mid)) == "Mum"
        await cleanup(ea, eb)
    run(t)


# ------------------------------------------------------------ payments

def test_cannot_verify_or_learn_about_another_users_order():
    async def t(c):
        ha, a_id, ea = await user(c, plan="free"); hb, b_id, eb = await user(c, plan="free")
        oid = f"order_{uuid.uuid4().hex[:12]}"
        await db.execute("insert into payments (order_id, user_id, amount_inr, currency, status, created_at) values ($1,$2,299,'INR','created','2026-01-01')", oid, a_id)
        r = await c.post("/api/payments/razorpay/verify", headers=hb, json={"razorpay_order_id": oid, "razorpay_payment_id": "p", "razorpay_signature": "s"})
        # must be indistinguishable from a non-existent order
        r2 = await c.post("/api/payments/razorpay/verify", headers=hb, json={"razorpay_order_id": "order_doesnotexist", "razorpay_payment_id": "p", "razorpay_signature": "s"})
        assert r.status_code == r2.status_code == 404 and r.json() == r2.json()
        assert (await db.fetchval("select status from payments where order_id = $1", oid)) == "created"
        await cleanup(ea, eb)
    run(t)


# ------------------------------------------------------------ family

def test_cannot_act_on_another_users_family_links_or_invites():
    async def t(c):
        ha, a_id, ea = await user(c); hb, b_id, eb = await user(c); hc, c_id, ec = await user(c)
        # A invites C, C accepts -> A<->C linked. B is a stranger.
        await c.post("/api/family/invite", headers=ha, json={"email": ec})
        inv = (await c.get("/api/family/incoming-invites", headers=hc)).json()[0]["id"]
        await c.post(f"/api/family/incoming-invites/{inv}/accept", headers=hc)
        # B cannot see A's members, cannot remove A<->C, cannot cancel A's invites
        assert (await c.get("/api/family/members", headers=hb)).json()["members"] == []
        await c.delete(f"/api/family/members/{c_id}", headers=hb)  # returns removed:true but must be a no-op for A<->C
        assert (await db.fetchval("select count(*) from family_links where (a=$1 and b=$2) or (a=$2 and b=$1)", a_id, c_id)) == 2
        assert (await c.delete(f"/api/family/invites/{inv}", headers=hb)).status_code == 404
        await cleanup(ea, eb, ec)
    run(t)


# ------------------------------------------------------------ identity cannot be spoofed

def test_user_id_in_body_or_query_is_ignored():
    """The only identity is the verified token. A caller supplying someone else's user_id
    anywhere in a request must still act as themselves."""
    async def t(c):
        ha, a_id, ea = await user(c); hb, b_id, eb = await user(c)
        # try to write A's profile while authenticated as B, naming A in the body
        r = await c.put("/api/user/health-profile", headers=hb, json={"user_id": a_id, "name": "pwned", "age": 99})
        assert r.status_code in (200, 422)
        a_row = await db.fetchrow("select name, health_profile from users where user_id = $1", a_id)
        assert a_row["name"] == "Iso" and (a_row["health_profile"] or {}).get("age") is None, "A was modified by B"
        # try to record an engagement event against A
        await c.post("/api/user/events", headers=hb, json={"type": "explanation_read", "user_id": a_id})
        assert (await db.fetchval("select count(*) from engagement_events where user_id = $1", a_id)) == 0
        await cleanup(ea, eb)
    run(t)


def test_forged_and_tampered_tokens_are_rejected():
    async def t(c):
        import jwt, time
        ha, a_id, ea = await user(c)
        # token signed with the wrong secret, claiming to be A
        forged = jwt.encode({"sub": a_id, "type": "access", "tv": 0, "iat": int(time.time()), "exp": int(time.time()) + 600}, "not-the-secret", algorithm="HS256")
        assert (await c.get("/api/reports", headers={"Authorization": f"Bearer {forged}"})).status_code == 401
        # a real token whose payload is edited to another sub (signature no longer matches)
        real = ha["Authorization"].split()[1]
        h, p, s = real.split(".")
        import json
        payload = json.loads(base64.urlsafe_b64decode(p + "=="))
        payload["sub"] = "user_somebodyelse"
        p2 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        assert (await c.get("/api/reports", headers={"Authorization": f"Bearer {h}.{p2}.{s}"})).status_code == 401
        # alg=none
        none_tok = jwt.encode({"sub": a_id, "type": "access", "tv": 0}, "", algorithm="none")
        assert (await c.get("/api/reports", headers={"Authorization": f"Bearer {none_tok}"})).status_code == 401
        # no header at all
        assert (await c.get("/api/reports")).status_code in (401, 403)
        await cleanup(ea)
    run(t)


def test_revoked_token_is_dead_everywhere():
    async def t(c):
        ha, a_id, ea = await user(c)
        assert (await c.get("/api/reports", headers=ha)).status_code == 200
        await c.post("/api/auth/logout", headers=ha)
        for path in ("/api/reports", "/api/auth/me", "/api/user/preferences", "/api/family/members", "/api/members"):
            assert (await c.get(path, headers=ha)).status_code == 401, path
        await cleanup(ea)
    run(t)
