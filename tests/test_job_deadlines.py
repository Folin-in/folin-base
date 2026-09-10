"""Deadline regression checks without live AI calls or waiting 200 seconds."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import services.jobs as J
from app.models import AnalyzeRequest

LOOP = asyncio.get_event_loop()


def test_queue_time_uses_the_same_budget():
    created = (datetime.now(timezone.utc) - timedelta(seconds=175)).isoformat()
    assert 24 <= J.remaining_seconds(created) <= 25
    assert J.remaining_seconds((datetime.now(timezone.utc) - timedelta(seconds=201)).isoformat()) == 0


def test_expired_recovered_job_never_calls_ai(monkeypatch):
    service = J.AnalyzeJobService()
    fail = AsyncMock()
    model = AsyncMock()
    lookup = AsyncMock()
    monkeypatch.setattr(service, "_fail", fail)
    monkeypatch.setattr(J.pipeline, "run", model)
    monkeypatch.setattr(J.db, "fetchrow", lookup)
    LOOP.run_until_complete(service._execute_claimed({
        "job_id": "expired", "created_at": (datetime.now(timezone.utc) - timedelta(seconds=201)).isoformat(),
        "user_id": "owner", "request_payload": {},
    }))
    fail.assert_awaited_once_with("expired", J.MSG_TIMEOUT)
    model.assert_not_awaited()
    lookup.assert_not_awaited()


def test_deadline_cancels_an_inflight_stage(monkeypatch):
    service = J.AnalyzeJobService()
    fail = AsyncMock()
    cancelled = []

    async def slow_stage(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(J, "ANALYZE_JOB_TIMEOUT_SECS", 0.03)
    monkeypatch.setattr(service, "_mark", AsyncMock())
    monkeypatch.setattr(service, "_fail", fail)
    monkeypatch.setattr(J, "refund_daily_quota", AsyncMock())
    monkeypatch.setattr(J.pipeline, "run", slow_stage)
    req = AnalyzeRequest(files=[{"filename": "test.png", "mime_type": "image/png", "file_base64": "AAAA"}])
    LOOP.run_until_complete(service._run("running", req, "owner", {}, created_at=J._iso()))
    assert cancelled == [True]
    fail.assert_awaited_once_with("running", J.MSG_TIMEOUT)


def test_expired_active_job_clears_payload_and_upload_slot(monkeypatch):
    monkeypatch.setattr(J.db, "fetch", AsyncMock(return_value=[{
        "job_id": "old", "created_at": (datetime.now(timezone.utc) - timedelta(seconds=201)).isoformat(),
        "request_payload": {"files": ["private payload"]},
    }]))
    update = AsyncMock()
    monkeypatch.setattr(J.db, "execute", update)
    assert LOOP.run_until_complete(J.AnalyzeJobService.active_job("owner")) is None
    assert "request_payload = null" in update.call_args.args[0]
    assert J.MSG_TIMEOUT in update.call_args.args


def test_status_does_not_overwrite_a_simultaneous_success(monkeypatch):
    service = J.AnalyzeJobService()
    created = (datetime.now(timezone.utc) - timedelta(seconds=201)).isoformat()
    lookup = AsyncMock(side_effect=[
        {"job_id": "race", "created_at": created, "status": "processing"},
        {"job_id": "race", "created_at": created, "status": "success", "report_id": "saved", "request_payload": None},
    ])
    monkeypatch.setattr(J.db, "fetchrow", lookup)
    monkeypatch.setattr(service, "_fail", AsyncMock())
    status = LOOP.run_until_complete(service.status("race", "owner"))
    assert status["status"] == "success" and status["report_id"] == "saved"
    assert "request_payload" not in status


def test_failure_update_only_changes_running_jobs(monkeypatch):
    update = AsyncMock()
    monkeypatch.setattr(J.db, "execute", update)
    LOOP.run_until_complete(J.AnalyzeJobService._fail("race", J.MSG_TIMEOUT))
    assert "status = any($5::text[])" in update.call_args.args[0]
    assert update.call_args.args[-1] == ["pending", "processing"]
