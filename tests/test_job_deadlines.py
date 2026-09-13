"""Offline regressions: no elapsed-job cutoff; renewable ownership and safe shutdown."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import services.jobs as J
from app.models import AnalyzeRequest

LOOP = asyncio.get_event_loop()
OLD = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
REQ = AnalyzeRequest(files=[{"filename": "test.png", "mime_type": "image/png", "file_base64": "AAAA"}])


def test_old_queued_job_still_runs(monkeypatch):
    service = J.AnalyzeJobService()
    model, fail = AsyncMock(), AsyncMock()
    monkeypatch.setattr(service, "_fail", fail)
    monkeypatch.setattr(J.pipeline, "run", model)
    monkeypatch.setattr(J.db, "fetchrow", AsyncMock(return_value={"user_id": "owner"}))
    LOOP.run_until_complete(service._execute_claimed({
        "job_id": "old", "created_at": OLD, "started_at": "claim", "user_id": "owner",
        "request_payload": REQ.model_dump(),
    }))
    model.assert_awaited_once()
    assert model.call_args.kwargs["claim_started_at"] == "claim"
    assert "deadline_at" not in model.call_args.kwargs
    fail.assert_not_awaited()


def test_old_active_job_is_not_expired_or_erased(monkeypatch):
    job = {"job_id": "old", "created_at": OLD, "status": "processing", "request_payload": {"files": []}}
    monkeypatch.setattr(J.db, "fetch", AsyncMock(return_value=[job]))
    update = AsyncMock()
    monkeypatch.setattr(J.db, "execute", update)
    assert LOOP.run_until_complete(J.AnalyzeJobService.active_job("owner")) == job
    update.assert_not_awaited()


def test_status_has_no_countdown_and_does_not_mutate_old_jobs(monkeypatch):
    monkeypatch.setattr(J.db, "fetchrow", AsyncMock(return_value={
        "job_id": "old", "created_at": OLD, "status": "processing", "request_payload": {"files": ["private"]},
    }))
    fail = AsyncMock()
    service = J.AnalyzeJobService()
    monkeypatch.setattr(service, "_fail", fail)
    status = LOOP.run_until_complete(service.status("old", "owner"))
    assert status["status"] == "processing"
    assert status["timeout_seconds"] is None and status["remaining_seconds"] is None
    assert "request_payload" not in status
    fail.assert_not_awaited()


def test_failure_cannot_overwrite_success_or_another_worker(monkeypatch):
    update = AsyncMock()
    monkeypatch.setattr(J.db, "execute", update)
    LOOP.run_until_complete(J.AnalyzeJobService._fail("race", J.MSG_INTERRUPTED, "my-claim"))
    assert "status = any($5::text[])" in update.call_args.args[0]
    assert "started_at = $6" in update.call_args.args[0]
    assert update.call_args.args[-2:] == (["pending", "processing"], "my-claim")


def test_claim_uses_renewable_heartbeat_not_total_age(monkeypatch):
    query = AsyncMock()
    monkeypatch.setattr(J.db, "fetchrow", query)
    LOOP.run_until_complete(J.AnalyzeJobService()._claim())
    sql = query.call_args.args[0]
    assert "coalesce(heartbeat_at, started_at) < $3" in sql
    assert "heartbeat_at = $4" in sql
    assert "created_at <" not in sql


def test_heartbeat_renews_only_owned_processing_job(monkeypatch):
    renew = AsyncMock(side_effect=["job", None, False])
    monkeypatch.setattr(J.db, "fetchval", renew)
    monkeypatch.setattr(J, "ANALYZE_HEARTBEAT_SECS", .001)
    LOOP.run_until_complete(J.AnalyzeJobService()._heartbeat({"job_id": "job", "started_at": "claim"}))
    assert renew.await_count == 3
    assert "started_at = $3 and status = 'processing'" in renew.call_args_list[0].args[0]
    assert "for update skip locked" in renew.call_args_list[0].args[0]
    assert renew.call_args.args[-2:] == ("job", "claim")


def test_lease_loss_cancels_work_before_reclaimed_worker_can_save(monkeypatch):
    service = J.AnalyzeJobService()
    cancelled = []
    async def work(job):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(service, "_execute_claimed", work)
    monkeypatch.setattr(service, "_heartbeat", AsyncMock())
    with pytest.raises(RuntimeError, match="lost its lease"):
        LOOP.run_until_complete(service._with_heartbeat({"job_id": "job"}))
    assert cancelled == [True]


def test_finished_work_stops_heartbeat(monkeypatch):
    service = J.AnalyzeJobService()
    cancelled = []
    async def heartbeat(job):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(service, "_execute_claimed", AsyncMock())
    monkeypatch.setattr(service, "_heartbeat", heartbeat)
    LOOP.run_until_complete(service._with_heartbeat({"job_id": "job"}))
    assert cancelled == [True]


def test_shutdown_cancels_both_work_and_heartbeat(monkeypatch):
    service = J.AnalyzeJobService()
    cancelled = []
    async def wait(job):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(service, "_execute_claimed", wait)
    monkeypatch.setattr(service, "_heartbeat", wait)
    async def check():
        task = asyncio.create_task(service._with_heartbeat({"job_id": "job"}))
        await asyncio.sleep(.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    LOOP.run_until_complete(check())
    assert len(cancelled) == 2


def test_heartbeat_does_not_cancel_a_save_holding_the_job_lock(monkeypatch):
    # First update skips the saving transaction's row lock; read still sees our
    # claim. A later renewal succeeds. Finally a terminal job ends the heartbeat.
    renew = AsyncMock(side_effect=[None, True, "job", None, False])
    monkeypatch.setattr(J.db, "fetchval", renew)
    monkeypatch.setattr(J, "ANALYZE_HEARTBEAT_SECS", .001)
    LOOP.run_until_complete(J.AnalyzeJobService()._heartbeat({"job_id": "job", "started_at": "claim"}))
    assert renew.await_count == 5


@pytest.mark.parametrize("owns_claim", [True, False])
def test_late_report_save_depends_on_ownership_not_elapsed_time(monkeypatch, owns_claim):
    import services.analysis as A
    from app.models import Report
    statements = []
    class Connection:
        async def fetchval(self, query, *args):
            statements.append(query)
            if query.startswith("select status"):
                assert args[-1] == OLD
                return "processing" if owns_claim else None
            if query.startswith("insert into reports"):
                return "12345678-1234-1234-1234-123456789abc"
            if query.startswith("update analyze_jobs"):
                assert "clock_timestamp" not in query
                assert args[-1] == OLD
                return "old-job"
            raise AssertionError(query)
        async def execute(self, query, *args):
            statements.append(query)
    @asynccontextmanager
    async def transaction():
        yield Connection()
    monkeypatch.setattr(A.db, "transaction", transaction)
    monkeypatch.setattr(A, "fetch_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(A.cache, "invalidate_reports", lambda _: None)
    state = A._Run(user_id="owner", user={}, files=[], member_id="self",
                   job_id="old-job", claim_started_at=OLD)
    report = Report(user_id="owner", uploaded_at=OLD)
    if owns_claim:
        result = LOOP.run_until_complete(A.pipeline._persist(state, report))
        assert result.report_id == "12345678-1234-1234-1234-123456789abc"
        assert any("insert into analysis_notifications" in sql for sql in statements)
    else:
        with pytest.raises(RuntimeError, match="no longer owned"):
            LOOP.run_until_complete(A.pipeline._persist(state, report))
        assert not any("insert into reports" in sql for sql in statements)
