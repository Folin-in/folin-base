"""Synthetic reports only. Model responses stubbed; leased rows cannot be claimed by
the running application's worker. No existing account or report is touched."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import uuid

import pytest

import services.analysis as A
from app.models import BiomarkerPatch
from db import session as db
from services.reports import ReportService
from services.verification_jobs import VerificationJobService, merge_verified
from tests.test_pipeline import ACCEPT, GOOD_LAB, cleanup, files, make_user, pipeline_with, run


def test_default_upload_does_not_wait_for_verifier(monkeypatch):
    async def t():
        p = pipeline_with(ACCEPT, GOOD_LAB)
        p.defer_verification = True
        async def context(r):
            pass
        async def persist(r, report):
            assert r.verification_status == "pending"
            assert "verify" not in r.stage_seconds
            assert [c["kind"] for c in p._fake.calls] == ["screen", "analyze"]
            return report
        monkeypatch.setattr(p, "_context", context)
        monkeypatch.setattr(p, "_persist", persist)
        report = await p.run(files(), "synthetic", {"preferences": {"plan": "pro"}}, None)
        assert report.biomarkers
        assert A.AnalysisPipeline().defer_verification
    asyncio.get_event_loop().run_until_complete(t())


def test_merge_never_overwrites_concurrent_corrections():
    original = [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}]
    verified = [{**b, "needs_verification": True, "verification_note": "Mismatch"} for b in original]
    current = [dict(original[0]), {**original[1], "value": "3", "user_edited": True}]
    merged = merge_verified(current, original, verified)
    assert merged[0]["needs_verification"]
    assert merged[1] == current[1]
    assert "needs_verification" not in current[0]


async def leased_fixture():
    uid, user, email = await make_user()
    p = pipeline_with(ACCEPT, GOOD_LAB)
    res = await p.run(files(), uid, user, None)
    token = uuid.uuid4().hex
    payload = {"files": [f.model_dump() for f in files()], "biomarkers": res.report["biomarkers"]}
    job = await db.fetchrow(
        """insert into verification_jobs(report_id,user_id,status,payload,started_at,claim_token,attempts)
           values($1::uuid,$2,'processing',$3,clock_timestamp(),$4,1) returning *""",
        res.report_id, uid, payload, token)
    await db.execute("update reports set analysis_meta = analysis_meta || '{\"verification_status\":\"pending\"}'::jsonb where id=$1::uuid", res.report_id)
    return job, user, email


@pytest.mark.parametrize("failure", [False, True])
def test_background_check_persists_outcome_and_erases_payload(monkeypatch, failure):
    async def t():
        job, user, email = await leased_fixture()
        try:
            async def verify(r):
                if failure:
                    raise TimeoutError()
                r.verification_status = "completed"
                r.biomarkers[0].needs_verification = True
                r.biomarkers[0].verification_note = "Document shows 11.9"
            monkeypatch.setattr(A.pipeline, "_verify", verify)
            await VerificationJobService()._execute(job)
            saved = await db.fetchrow("select * from verification_jobs where report_id=$1", job["report_id"])
            assert saved["status"] == ("unavailable" if failure else "completed")
            assert saved["payload"] is None and saved["claim_token"] is None
            report = await ReportService().get(str(job["report_id"]), user["user_id"])
            assert report["verification_status"] == saved["status"]
            if not failure:
                assert report["biomarkers"][0]["verification_note"] == "Document shows 11.9"
        finally:
            await cleanup(email)
    run(t)


def test_stale_claim_cannot_publish_and_expired_work_is_not_sent_to_ai(monkeypatch):
    async def t():
        job, user, email = await leased_fixture()
        service = VerificationJobService()
        try:
            stale = {**job, "claim_token": "old-worker"}
            await service._finish(stale, "completed")
            assert await db.fetchval("select status from verification_jobs where report_id=$1", job["report_id"]) == "processing"
            job["created_at"] = datetime.now(timezone.utc) - timedelta(seconds=601)
            async def unexpected(r):
                pytest.fail("expired job invoked AI")
            monkeypatch.setattr(A.pipeline, "_verify", unexpected)
            await service._execute(job)
            assert await db.fetchval("select payload from verification_jobs where report_id=$1", job["report_id"]) is None
        finally:
            await cleanup(email)
    run(t)


def test_late_user_edit_preserves_background_warnings_on_other_values(monkeypatch):
    async def t():
        job, user, email = await leased_fixture()
        try:
            service = ReportService()
            verified = copy.deepcopy(job["payload"]["biomarkers"])
            verified[1].update(needs_verification=True, verification_note="Document differs")
            async def finish_during_advice(*args):
                await VerificationJobService()._finish(job, "completed", verified)
            monkeypatch.setattr(service, "_refresh_advice", finish_during_advice)
            updated = await service.edit_biomarker(str(job["report_id"]), user, BiomarkerPatch(index=0, value="12"))
            assert updated["biomarkers"][0]["value"] == "12"
            assert updated["biomarkers"][1]["verification_note"] == "Document differs"
            assert updated["verification_status"] == "completed"
        finally:
            await cleanup(email)
    run(t)
