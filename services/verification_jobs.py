"""Bounded, durable post-publication verification. No detached patient-file tasks.

One worker per process claims with SKIP LOCKED. A lease token fences stale workers;
restarts resume queued checks. Temporary payloads are removed on completion/failure.
Only warning fields are merged, and never onto values edited since submission.
"""
import asyncio
import os
import time
import uuid
import logging
from contextlib import suppress
from datetime import datetime, timezone

from agents.attachments import attachment_scope
from app.models import AnalyzeFileIn, Biomarker
from core import cache
from db import session as db

logger = logging.getLogger(__name__)
WORK_SECONDS = 60
LEASE_SECONDS = 120
MAX_AGE_SECONDS = 600


def merge_verified(current: list[dict], original: list[dict], verified: list[dict]) -> list[dict]:
    """Conservative positional merge: don't overwrite concurrent user corrections."""
    merged = [dict(b) for b in current]
    for index, (before, after) in enumerate(zip(original, verified)):
        if index >= len(merged) or merged[index] != before or before.get("user_edited"):
            continue
        for key in ("needs_verification", "verification_note"):
            merged[index][key] = after.get(key)
    return merged


class VerificationJobService:
    def __init__(self):
        self._task: asyncio.Task | None = None

    async def start(self):
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="folin-verification-worker")

    async def stop(self):
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _claim(self):
        return await db.fetchrow(
            """with candidate as (
                 select report_id from verification_jobs
                 where payload is not null and (status = 'pending' or
                    (status = 'processing' and started_at < clock_timestamp() - make_interval(secs => $1)))
                 order by created_at for update skip locked limit 1
               ) update verification_jobs j set status = 'processing',
                   started_at = clock_timestamp(), claim_token = $2, attempts = attempts + 1
                 from candidate where j.report_id = candidate.report_id returning j.*""",
            LEASE_SECONDS, uuid.uuid4().hex)

    async def _finish(self, job, status, verified=None, seconds=0, trajectory_id=None):
        async with db.transaction() as conn:
            owned = await conn.fetchval(
                """select report_id from verification_jobs where report_id = $1
                   and status = 'processing' and claim_token = $2 for update""",
                job["report_id"], job["claim_token"])
            if not owned:
                return
            row = await conn.fetchrow(
                "select biomarkers, analysis_meta from reports where id = $1 and user_id = $2 and deleted_at is null for update",
                job["report_id"], job["user_id"])
            if row:
                meta = dict(row["analysis_meta"] or {})
                meta["verification_status"] = status
                meta["verification_completed_at"] = datetime.now(timezone.utc).isoformat()
                meta["verify_trajectory_id"] = trajectory_id
                meta["stage_seconds"] = {**(meta.get("stage_seconds") or {}), "verify": round(seconds, 3)}
                markers = row["biomarkers"]
                if status == "completed" and verified is not None:
                    markers = merge_verified(markers, job["payload"]["biomarkers"], verified)
                await conn.execute(
                    "update reports set biomarkers = $2, analysis_meta = $3 where id = $1",
                    job["report_id"], markers, meta)
            await conn.execute(
                """update verification_jobs set status = $2, completed_at = clock_timestamp(),
                   payload = null, claim_token = null where report_id = $1""", job["report_id"], status)
        cache.invalidate_reports(job["user_id"])

    async def _execute(self, job):
        from services.analysis import _Run, pipeline
        started = time.monotonic()
        r = None
        try:
            async with asyncio.timeout(WORK_SECONDS):
                age = (datetime.now(timezone.utc) - job["created_at"]).total_seconds()
                exists = await db.fetchval(
                    "select id from reports where id = $1 and user_id = $2 and deleted_at is null",
                    job["report_id"], job["user_id"])
                if not exists or age > MAX_AGE_SECONDS or job["attempts"] > 2:
                    await self._finish(job, "unavailable")
                    return
                payload = job["payload"]
                r = _Run(user_id=job["user_id"], user={"preferences": {"plan": "pro"}},
                         files=[AnalyzeFileIn.model_validate(f) for f in payload["files"]], member_id="self")
                r.biomarkers = [Biomarker.model_validate(b) for b in payload["biomarkers"]]
                with attachment_scope():
                    pipeline._decode(r)
                    pipeline._materialise(r)
                    await pipeline._verify(r)
                if r.verify_trajectory_id:
                    await db.execute("update ai_trajectories set report_id = $1 where id = $2",
                                     str(job["report_id"]), r.verify_trajectory_id)
                await self._finish(job, r.verification_status,
                                   [b.model_dump() for b in r.biomarkers],
                                   time.monotonic() - started, r.verify_trajectory_id)
        except Exception:
            logger.exception("background verification failed for report %s", job["report_id"])
            await self._finish(job, "unavailable", seconds=time.monotonic() - started)
        finally:
            if r:
                for path in r.tmp_paths:
                    if os.path.exists(path):
                        os.unlink(path)

    async def _worker(self):
        current = None
        while True:
            try:
                current = await self._claim()
                if current:
                    await self._execute(current)
                    current = None
                else:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                if current:
                    await db.execute(
                        """update verification_jobs set status = 'pending', started_at = null, claim_token = null
                           where report_id = $1 and claim_token = $2 and status = 'processing'""",
                        current["report_id"], current["claim_token"])
                raise
            except Exception:
                logger.exception("verification worker loop failed")
                await asyncio.sleep(2)


verification_jobs = VerificationJobService()
