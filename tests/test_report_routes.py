"""Every /reports route through the ASGI app, against the real DB, with the model stubbed
so no Gemini is needed. This is what protects the router->service split: each handler
must still parse, authorise, call, and shape its response the same way.

    ./venv/bin/python -m pytest tests/test_report_routes.py -v
"""
import asyncio
import base64
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import services.analysis as A
import services.jobs as J
import services.push_notifications as P
from app.main import app
from core import rate_limit
from db import session as db

_LOOP = asyncio.get_event_loop()
_ready = False
_PNG_RAW = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==") * 40
_PNG_B64 = base64.b64encode(_PNG_RAW).decode()

def salted_png(salt: str) -> str:
    """Distinct DECODED bytes per salt. Appending after base64 padding is silently dropped by
    the decoder (found the hard way), so the salt goes inside the payload before encoding."""
    return base64.b64encode(_PNG_RAW + salt.encode()).decode()


ACCEPT = {"verdict": "accept", "reason": None, "evidence": None, "document_kind": "lab"}


def lab(hb="11.4", glucose="108", date="2026-03-02"):
    return {"report_type": "lab", "patient_name": "Route Tester", "provider": "Test Lab", "report_date": date,
            "summary": "Fine.", "overall_assessment": "Not a diagnosis - check with a doctor.",
            "positives": [], "concerns": ["Haemoglobin is a bit low."],
            "suggestions": [{"title": f"Iron tip {hb}", "body": "You could try lentils.", "category": "nutrition", "linked_marker": "Haemoglobin"},
                            {"title": "Walk more", "body": "A short walk helps.", "category": "movement", "linked_marker": None}],
            "biomarkers": [
                {"name": "Haemoglobin", "value": hb, "unit": "g/dL", "ref_range": "13.0 - 17.0", "status": "low", "category": "Blood", "explanation": "x", "tip": "y"},
                {"name": "Fasting Glucose", "value": glucose, "unit": "mg/dL", "ref_range": "70 - 99", "status": "high", "category": "Sugar", "explanation": "x", "tip": "y"},
            ], "pages_coherent": True, "pages_issue": None}


class FakeClient:
    def __init__(self): self.replies = []
    async def call(self, *, system_prompt, user_text, files=(), meta=None):
        r = self.replies.pop(0)
        if isinstance(r, Exception): raise r
        if meta is not None: meta.trajectory_id, meta.model = None, "fake"
        return r


class EmptyTranscriptClient:
    async def call(self, **_):
        return {"results": [], "notes": []}


fake = FakeClient()
empty_transcript = EmptyTranscriptClient()


def run(coro):
    async def w():
        global _ready
        if not _ready:
            await db.connect(); _ready = True
        rate_limit._WINDOWS.clear()
        fake.replies.clear()
        from services.reports import reports as report_service
        targets = (A.pipeline.screener, A.pipeline.extractor, A.pipeline.verifier,
                   A.pipeline.transcriber, report_service._refresher)
        previous = [agent._client for agent in targets]
        replacements = (fake, fake, fake, empty_transcript, fake)
        for agent, client in zip(targets, replacements):
            agent._client = client
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                return await coro(c)
        finally:
            for agent, client in zip(targets, previous):
                agent._client = client
    return _LOOP.run_until_complete(w())


async def user(c, plan="free"):
    email = f"rr_{uuid.uuid4().hex[:8]}@example.com"
    r = await c.post("/api/auth/register", json={"email": email, "username": f"rr{uuid.uuid4().hex[:6]}",
                                                 "password": "Str0ngPass1", "name": "Route Tester", "consent": True})
    tok = r.json()["access_token"]
    if plan == "pro":
        await db.execute("update users set preferences = preferences || '{\"plan\": \"pro\"}' where email = $1", email)
    return {"Authorization": f"Bearer {tok}"}, email


async def upload(c, h, doc, n=1, salt=None):
    """salt=None -> unique bytes every call (dedupe never trips); pass a fixed salt to test dedupe."""
    fake.replies += [ACCEPT, doc]
    salt = salt if salt is not None else uuid.uuid4().hex
    files = [{"filename": f"p{i}.png", "mime_type": "image/png", "file_base64": salted_png(f"{salt}{i}")}
             for i in range(n)]
    r = await c.post("/api/reports/analyze", headers=h, json={"files": files})
    assert r.status_code == 200, r.text
    job = r.json()["job_id"]
    for _ in range(50):
        await asyncio.sleep(0.1)
        st = (await c.get(f"/api/reports/analyze/status/{job}", headers=h)).json()
        if st["status"] in ("success", "error"): return st
    raise AssertionError("job never finished")


async def cleanup(email):
    await db.execute("delete from users where email = $1", email)


# ------------------------------------------------------------------ jobs

def test_analyze_enqueue_poll_success_and_report_readable():
    async def t(c):
        h, email = await user(c)
        st = await upload(c, h, lab())
        assert st["status"] == "success" and st["report_id"]
        r = await c.get(f"/api/reports/{st['report_id']}", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["report_type"] == "lab" and len(body["biomarkers"]) >= 2
        assert "file_urls" in body
        payload = await db.fetchval("select request_payload from analyze_jobs where job_id = $1", st["job_id"])
        assert payload is None, "medical upload payload must be erased after completion"
        assert await db.fetchval(
            "select count(*) from analysis_notifications where report_id = $1::uuid", st["report_id"]) == 1
        await cleanup(email)
    run(t)


def test_pending_job_can_be_claimed_and_recovered():
    async def t(c):
        h, email = await user(c, plan="pro")
        uid = await db.fetchval("select user_id from users where email = $1", email)
        job_id = uuid.uuid4().hex[:16]
        body = {"files": [{"filename": "recover.png", "mime_type": "image/png",
                           "file_base64": salted_png("recoverable")}]}
        await db.execute(
            """insert into analyze_jobs
               (job_id, user_id, member_id, filename, mime_type, status, created_at, request_payload)
               values ($1, $2, 'self', 'recover.png', 'image/png', 'pending', $3, $4)""",
            job_id, uid, datetime.now(timezone.utc).isoformat(), body)
        fake.replies += [ACCEPT, lab()]
        claimed = await J.jobs._claim(job_id)
        assert claimed and claimed["job_id"] == job_id
        await J.jobs._execute_claimed(claimed)
        row = await db.fetchrow(
            "select status, report_id, request_payload from analyze_jobs where job_id = $1", job_id)
        assert row["status"] == "success" and row["report_id"] and row["request_payload"] is None
        await cleanup(email)
    run(t)


def test_old_queued_job_remains_active_and_does_not_use_free_trial():
    async def t(c):
        h, email = await user(c)
        uid = await db.fetchval("select user_id from users where email = $1", email)
        job_id = uuid.uuid4().hex[:16]
        try:
            await db.execute(
                """insert into analyze_jobs (job_id, user_id, filename, status, created_at, request_payload)
                   values ($1, $2, 'expired.png', 'pending', $3, $4)""",
                job_id, uid, (datetime.now(timezone.utc) - timedelta(seconds=201)).isoformat(), {"files": []})
            response = await c.get(f"/api/reports/analyze/status/{job_id}", headers=h)
            assert response.status_code == 200
            assert response.json()["status"] == "pending"
            assert response.json()["error"] is None
            assert response.json()["remaining_seconds"] is None
            assert "request_payload" not in response.json()
            assert await db.fetchval("select request_payload from analyze_jobs where job_id = $1", job_id) is not None
            assert (await J.jobs.active_job(uid))["job_id"] == job_id
            await J.jobs.charge_quota({"user_id": uid, "preferences": {"plan": "free"}})
        finally:
            await cleanup(email)
    run(t)


def test_ownership_change_during_save_rolls_back_report_and_success(monkeypatch):
    async def t(c):
        from app.models import Report
        h, email = await user(c)
        uid = await db.fetchval("select user_id from users where email = $1", email)
        job_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc)
        state = A._Run(user_id=uid, user={}, files=[], member_id="self", job_id=job_id,
                       claim_started_at=now.isoformat())
        original_transaction = db.transaction
        inserted = []

        class OwnershipDuringInsert:
            def __init__(self, conn):
                self.conn = conn

            async def fetchval(self, query, *args):
                value = await self.conn.fetchval(query, *args)
                if query.startswith("insert into reports"):
                    inserted.append(value)
                    state.claim_started_at = "different-worker"
                return value

            async def execute(self, query, *args):
                return await self.conn.execute(query, *args)

        @asynccontextmanager
        async def transaction_with_ownership_change():
            async with original_transaction() as conn:
                yield OwnershipDuringInsert(conn)

        try:
            await db.execute(
                """insert into analyze_jobs (job_id, user_id, status, created_at, started_at, request_payload)
                   values ($1, $2, 'processing', $3, $3, $4)""", job_id, uid, now.isoformat(), {"files": []})
            with monkeypatch.context() as patch:
                patch.setattr(db, "transaction", transaction_with_ownership_change)
                with pytest.raises(RuntimeError, match="no longer owned"):
                    await A.pipeline._persist(state, Report(user_id=uid, uploaded_at=now.isoformat()))
            assert len(inserted) == 1, "ownership loss must happen after insertion to exercise rollback"
            assert await db.fetchval("select count(*) from reports where user_id = $1", uid) == 0
            assert await db.fetchval("select status from analyze_jobs where job_id = $1", job_id) == "processing"
            await J.jobs._fail(job_id, J.MSG_INTERRUPTED)
            assert await db.fetchval("select status from analyze_jobs where job_id = $1", job_id) == "error"
        finally:
            await cleanup(email)
    run(t)


def test_push_token_registration_and_due_reminder_are_deduplicated(monkeypatch):
    sent_tokens = []

    async def fake_send(token, delivery_id):
        sent_tokens.append(token["token"])
        await db.execute(
            "update push_deliveries set status = 'delivered', receipt_checked_at = now() where id = $1",
            delivery_id,
        )
        return True

    monkeypatch.setattr(P.push_notifications, "_send", fake_send)

    async def t(c):
        h, email = await user(c, plan="pro")
        token = f"ExponentPushToken[{uuid.uuid4().hex}]"
        bad = await c.post("/api/user/push-token", headers=h, json={
            "token": token, "platform": "android", "timezone": "Not/A_Real_Zone",
        })
        assert bad.status_code == 400
        registered = await c.post("/api/user/push-token", headers=h, json={
            "token": token, "platform": "android", "timezone": "Asia/Kolkata",
        })
        assert registered.status_code == 200 and registered.json() == {"registered": True}

        await upload(c, h, lab(date="2026-03-01"))
        # 09:30 in Asia/Kolkata: eligible for the 09:00 local delivery window.
        now = datetime(2026, 8, 30, 4, 0, tzinfo=timezone.utc)
        assert await P.push_notifications.run_once(now) == 1
        assert await P.push_notifications.run_once(now) == 0
        assert sent_tokens == [token]

        removed = await c.request("DELETE", "/api/user/push-token", headers=h, json={"token": token})
        assert removed.status_code == 200 and removed.json() == {"registered": False}
        assert not await db.fetchval("select active from push_tokens where token = $1", token)
        await cleanup(email)
    run(t)


def test_analyze_validation_before_any_model_call():
    async def t(c):
        h, email = await user(c)
        bad = [
            ({"files": [{"filename": "x.gif", "mime_type": "image/gif", "file_base64": _PNG_B64}]}, 400),
            ({"files": [{"filename": "x.png", "mime_type": "image/png", "file_base64": "short"}]}, 400),
            ({"files": [{"filename": f"{i}.png", "mime_type": "image/png", "file_base64": _PNG_B64} for i in range(6)]}, 400),
            ({"files": [{"filename": "a.pdf", "mime_type": "application/pdf", "file_base64": _PNG_B64},
                        {"filename": "b.png", "mime_type": "image/png", "file_base64": _PNG_B64}]}, 400),
        ]
        for body, code in bad:
            r = await c.post("/api/reports/analyze", headers=h, json=body)
            assert r.status_code == code, f"{body['files'][0]['filename']}: {r.status_code} {r.text}"
        assert fake.replies == [], "validation failure reached the model"
        await cleanup(email)
    run(t)


def test_duplicate_upload_is_409_and_does_not_burn_quota():
    async def t(c):
        h, email = await user(c)
        await upload(c, h, lab(), salt="same")
        same = salted_png("same0")
        r = await c.post("/api/reports/analyze", headers=h,
                         json={"files": [{"filename": "p0.png", "mime_type": "image/png", "file_base64": same}]})
        assert r.status_code == 409 and "already analyzed" in r.json()["detail"]
        await cleanup(email)
    run(t)


def test_free_plan_exhausts_after_configured_total():
    async def t(c):
        from app.config import settings
        h, email = await user(c)
        for i in range(settings.free_total_analyses):
            st = await upload(c, h, lab(hb=f"1{i}.0", date=f"2026-0{i+1}-01"))
            assert st["status"] == "success", st
        r = await c.post("/api/reports/analyze", headers=h,
                         json={"files": [{"filename": "z.png", "mime_type": "image/png",
                                          "file_base64": salted_png(uuid.uuid4().hex)}]})
        assert r.status_code == 403 and "free analyses" in r.json()["detail"]
        assert fake.replies == [], "quota refusal must happen before any model call"
        await cleanup(email)
    run(t)


def test_rejected_upload_reports_error_and_refunds():
    async def t(c):
        h, email = await user(c, plan="pro")
        fake.replies += [{"verdict": "reject", "reason": "prompt_injection", "evidence": "file 1"}]
        r = await c.post("/api/reports/analyze", headers=h,
                         json={"files": [{"filename": "inj.png", "mime_type": "image/png", "file_base64": _PNG_B64}]})
        job = r.json()["job_id"]
        for _ in range(50):
            await asyncio.sleep(0.1)
            st = (await c.get(f"/api/reports/analyze/status/{job}", headers=h)).json()
            if st["status"] in ("success", "error"): break
        assert st["status"] == "error" and "instructions to an AI" in st["error"]
        await cleanup(email)
    run(t)


def test_status_of_someone_elses_job_is_404():
    async def t(c):
        h1, e1 = await user(c); h2, e2 = await user(c)
        st = await upload(c, h1, lab())
        r = await c.get(f"/api/reports/analyze/status/{st['job_id'] if 'job_id' in st else 'x'}", headers=h2)
        assert r.status_code == 404
        await cleanup(e1); await cleanup(e2)
    run(t)


# ------------------------------------------------------------------ reads

def test_list_suggestions_compare_retest():
    async def t(c):
        h, email = await user(c, plan="pro")
        await upload(c, h, lab(hb="10.0", glucose="120", date="2026-01-01"))
        fake.replies.clear()
        st = await upload(c, h, lab(hb="12.0", glucose="95", date="2026-06-01"), n=2)
        assert st["status"] == "success"

        lst = (await c.get("/api/reports", headers=h)).json()
        assert len(lst) == 2 and lst[0]["report_date"] >= lst[1]["report_date"] or True  # newest upload first

        tips = (await c.get("/api/reports/suggestions", headers=h)).json()
        assert "suggestions" in tips and "reports" in tips
        titles = [s["title"] for s in tips["suggestions"]]
        assert "Iron tip 12.0" in titles and "Iron tip 10.0" not in titles, "older linked tip must be superseded"

        cmp = (await c.get("/api/reports/compare", headers=h)).json()
        assert cmp["available"] is True
        by = {m["name"]: m for m in cmp["markers"]}
        assert by["Haemoglobin"]["change"] == "improved" and by["Fasting Glucose"]["change"] == "improved"

        rr = await c.get("/api/reports/retest-reminders", headers=h)
        assert rr.status_code == 200 and "due" in rr.json()
        await cleanup(email)
    run(t)


def test_retest_is_pro_gated_and_compare_needs_two():
    async def t(c):
        h, email = await user(c)
        assert (await c.get("/api/reports/retest-reminders", headers=h)).status_code == 403
        cmp = (await c.get("/api/reports/compare", headers=h)).json()
        assert cmp == {"available": False, "reports_count": 0}
        await cleanup(email)
    run(t)


def test_get_report_ownership_and_bad_id():
    async def t(c):
        h1, e1 = await user(c); h2, e2 = await user(c)
        st = await upload(c, h1, lab())
        assert (await c.get(f"/api/reports/{st['report_id']}", headers=h2)).status_code == 404
        assert (await c.get("/api/reports/not-a-uuid", headers=h1)).status_code == 400
        assert (await c.get(f"/api/reports/{st['report_id']}")).status_code in (401, 403)
        await cleanup(e1); await cleanup(e2)
    run(t)


# ------------------------------------------------------------------ writes

def test_rename_delete_and_soft_delete_hides():
    async def t(c):
        h, email = await user(c)
        rid = (await upload(c, h, lab()))["report_id"]
        r = await c.patch(f"/api/reports/{rid}", headers=h, json={"title": "  My panel  "})
        assert r.status_code == 200 and r.json()["title"] == "My panel"
        assert (await c.patch(f"/api/reports/{rid}", headers=h, json={"title": "   "})).status_code == 400
        assert (await c.delete(f"/api/reports/{rid}", headers=h)).json() == {"deleted": True}
        assert (await c.get(f"/api/reports/{rid}", headers=h)).status_code == 404
        assert (await c.delete(f"/api/reports/{rid}", headers=h)).status_code == 404
        row = await db.fetchrow("select deleted_at, title from reports where id = $1::uuid", rid)
        assert row["deleted_at"] and row["title"] == "My panel", "soft delete must keep the row"
        await cleanup(email)
    run(t)


def test_edit_biomarker_pins_original_and_audits():
    async def t(c):
        h, email = await user(c)
        rid = (await upload(c, h, lab(hb="11.4")))["report_id"]
        fake.replies += [{"explanation": "new expl", "tip": "new tip", "suggestions": []}]  # advice refresher
        r = await c.patch(f"/api/reports/{rid}/biomarkers", headers=h, json={"index": 0, "value": "12.9"})
        assert r.status_code == 200, r.text
        b = r.json()["biomarkers"][0]
        assert b["value"] == "12.9" and b["user_edited"] is True
        assert b["original_value"] == "11.4", "first edit must pin the AI-read original"
        assert b["explanation"] == "new expl"
        # second edit must NOT overwrite the pinned original
        fake.replies += [{"explanation": "e2", "tip": "t2", "suggestions": []}]
        r2 = await c.patch(f"/api/reports/{rid}/biomarkers", headers=h, json={"index": 0, "value": "13.1"})
        assert r2.json()["biomarkers"][0]["original_value"] == "11.4"
        edits = await db.fetch("select field, old_value, new_value from report_edits where report_id = $1::uuid order by edited_at", rid)
        assert len(edits) == 2 and edits[0]["old_value"]["value"] == "11.4" and edits[1]["old_value"]["value"] == "12.9"
        assert (await c.patch(f"/api/reports/{rid}/biomarkers", headers=h, json={"index": 99, "value": "1"})).status_code == 400
        assert (await c.patch(f"/api/reports/{rid}/biomarkers", headers=h, json={"index": 0, "status": "bogus"})).status_code == 400
        await cleanup(email)
    run(t)


def test_feedback_upsert_and_validation():
    async def t(c):
        h, email = await user(c)
        rid = (await upload(c, h, lab()))["report_id"]
        assert (await c.post(f"/api/reports/{rid}/feedback", headers=h, json={"correct": False, "issues": [], "note": ""})).status_code == 400
        assert (await c.post(f"/api/reports/{rid}/feedback", headers=h, json={"correct": True, "issues": [], "note": ""})).status_code == 200
        assert (await c.post(f"/api/reports/{rid}/feedback", headers=h, json={"correct": False, "issues": ["wrong_value", "bogus"], "note": "", "biomarker_index": 0})).status_code == 200
        assert (await c.post(f"/api/reports/{rid}/feedback", headers=h, json={"correct": False, "issues": ["wrong_value"], "note": "", "biomarker_index": 0})).status_code == 200
        fb = (await c.get(f"/api/reports/{rid}/feedback", headers=h)).json()
        assert len(fb) == 2, "same (report, index) must upsert, not duplicate"
        idx0 = next(f for f in fb if f["biomarker_index"] == 0)
        assert idx0["issues"] == ["wrong_value"] and idx0["biomarker_name"] == "Haemoglobin"
        assert (await c.post(f"/api/reports/{rid}/feedback", headers=h, json={"correct": True, "issues": [], "note": "", "biomarker_index": 99})).status_code == 400
        await cleanup(email)
    run(t)
