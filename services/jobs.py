"""Durable analyze-job queue: validate, enforce quota, persist the request, and let a
database-claiming worker run the pipeline while the client polls.

The temporary request payload is erased on success or failure. A graceful shutdown hands
its current job back immediately; a hard crash leaves enough data for another worker to
reclaim it after its renewable lease expires. There is no total job time cutoff.
Atomic claims and ownership checks support multiple API workers.
"""
import asyncio
import base64
import hashlib
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import HTTPException

from agents.base import ModelUnavailable
from app.config import settings
from app.constants import (ALLOWED_MIME, ANALYZE_HEARTBEAT_SECS, ANALYZE_LEASE_SECS,
                           ANALYZE_WORKER_POLL_SECS, MAX_ANALYZE_FILES, MAX_UPLOAD_BASE64_BYTES,
                           MIN_FILE_BASE64_CHARS)
from app.enums import JobStatus, Plan
from app.models import AnalyzeFileIn, AnalyzeRequest
from core.rate_limit import refund_daily_quota
from db import session as db
from services.analysis import MSG_PROVIDER, pipeline
from services.analysis_types import AnalysisRejected, InvalidUpload, UnsupportedDocument
from services.members import member_id_of

logger = logging.getLogger(__name__)

RUNNING = (JobStatus.PENDING, JobStatus.PROCESSING)
DEAD_AFTER = timedelta(seconds=ANALYZE_LEASE_SECS)
JOB_ID_HEX = 16
REFUND_STATUSES = frozenset({422})  # + any 5xx: the user's file, or our fault - never a consumed analysis

MSG_TOO_MANY = f"You can upload at most {MAX_ANALYZE_FILES} photos at once."
MSG_MIXED_TYPES = "Multi-page upload works with photos only. Upload PDFs one at a time."
MSG_BAD_TYPE = "Unsupported file type. Upload a PDF or a photo (JPG/PNG)."
MSG_EMPTY = "That file looks empty or corrupted. Please pick the file again."
MSG_TOO_LARGE = "Files too large (max 10 MB total). Try photos of the report instead of scans, or compress the PDF."
MSG_INVALID = "Invalid file data."
MSG_DUPLICATE = ("You already analyzed this exact document on {date}. "
                 "Open it from the Reports tab, or delete it first to re-analyze.")
MSG_BUSY = ("“{filename}” is still being analyzed. "
            "You can upload another report when it finishes.")
MSG_FREE_EXHAUSTED = "You've used your {n} free analyses. Upgrade to Pro for unlimited analyses."
MSG_INTERRUPTED = "That analysis was interrupted. Please try uploading again."
MSG_CRASHED = "Analysis crashed. Please try again. ({kind})"


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_files(req: AnalyzeRequest) -> list[AnalyzeFileIn]:
    """Accept both the multi-file shape and the legacy single-file shape."""
    if req.files:
        return req.files
    return [AnalyzeFileIn(filename=req.filename, mime_type=req.mime_type, file_base64=req.file_base64)]


def display_name(files: list[AnalyzeFileIn]) -> str:
    return files[0].filename if len(files) == 1 else f"{files[0].filename} (+{len(files) - 1} pages)"


@dataclass(frozen=True)
class Enqueued:
    job_id: str
    created_at: str
    status: JobStatus = JobStatus.PENDING


class AnalyzeJobService:
    def __init__(self):
        self._worker_task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None

    # ---------------------------------------------------------------- validation

    @staticmethod
    def validate(files: list[AnalyzeFileIn]) -> None:
        if len(files) > MAX_ANALYZE_FILES:
            raise HTTPException(status_code=400, detail=MSG_TOO_MANY)
        if len(files) > 1 and any(not f.mime_type.startswith("image/") for f in files):
            raise HTTPException(status_code=400, detail=MSG_MIXED_TYPES)
        for f in files:
            if f.mime_type not in ALLOWED_MIME:
                raise HTTPException(status_code=400, detail=MSG_BAD_TYPE)
            if len(f.file_base64 or "") < MIN_FILE_BASE64_CHARS:
                raise HTTPException(status_code=400, detail=MSG_EMPTY)
        if sum(len(f.file_base64) for f in files) > MAX_UPLOAD_BASE64_BYTES:
            raise HTTPException(status_code=413, detail=MSG_TOO_LARGE)

    @staticmethod
    def file_hash(files: list[AnalyzeFileIn]) -> str:
        digest = hashlib.sha256()
        for f in files:
            try:
                digest.update(base64.b64decode(f.file_base64, validate=False))
            except Exception:
                raise HTTPException(status_code=400, detail=MSG_INVALID)
        return digest.hexdigest()

    @staticmethod
    async def reject_duplicate(user_id: str, member_id: str, file_hash: str) -> None:
        """The same bytes analyzed twice can score differently (the second run sees previous-
        report context), which reads as a bug and wastes quota."""
        dup = await db.fetchrow(
            """select id, uploaded_at from reports
               where user_id = $1 and member_id = $2 and file_hash = $3 and deleted_at is null limit 1""",
            user_id, member_id, file_hash)
        if dup:
            raise HTTPException(status_code=409, detail=MSG_DUPLICATE.format(date=str(dup["uploaded_at"])[:10]))

    @staticmethod
    async def active_job(user_id: str) -> dict | None:
        rows = await db.fetch(
            """select * from analyze_jobs where user_id = $1 and status = any($2::text[])
               order by created_at desc limit 5""",
            user_id, [str(s) for s in RUNNING])
        return rows[0] if rows else None

    @staticmethod
    async def charge_quota(user: dict) -> None:
        """Free = a lifetime trial allowance; Pro = unlimited. Checked BEFORE the job is
        created so a refused upload never occupies the one-job slot."""
        plan = (user.get("preferences") or {}).get("plan") or Plan.FREE
        if plan == Plan.PRO:
            return
        used = await db.fetchval(
            "select count(*) from analyze_jobs where user_id = $1 and status = $2",
            user["user_id"], str(JobStatus.SUCCESS))
        if used >= settings.free_total_analyses:
            raise HTTPException(status_code=403, detail=MSG_FREE_EXHAUSTED.format(
                n=settings.free_total_analyses))

    # ---------------------------------------------------------------- lifecycle

    async def enqueue(self, req: AnalyzeRequest, user: dict) -> Enqueued:
        created_at = _iso()
        files = request_files(req)
        self.validate(files)
        member_id = member_id_of(req.member_id)
        await self.reject_duplicate(user["user_id"], member_id, self.file_hash(files))
        if running := await self.active_job(user["user_id"]):
            raise HTTPException(status_code=409, detail=MSG_BUSY.format(filename=running["filename"]))
        await self.charge_quota(user)

        job_id = uuid.uuid4().hex[:JOB_ID_HEX]
        try:
            await db.execute(
                """insert into analyze_jobs
                   (job_id, user_id, member_id, filename, mime_type, status, created_at, request_payload)
                   values ($1, $2, $3, $4, $5, $6, $7, $8)""",
                job_id, user["user_id"], member_id, display_name(files), files[0].mime_type,
                str(JobStatus.PENDING), created_at, req.model_dump(mode="json"))
        except asyncpg.UniqueViolationError:
            running = await self.active_job(user["user_id"])
            raise HTTPException(status_code=409, detail=MSG_BUSY.format(
                filename=(running or {}).get("filename") or "Your report"))
        if self._worker_task and not self._worker_task.done() and self._wake:
            self._wake.set()
        else:
            # In-process ASGI tests intentionally do not run app lifespan. Production
            # always uses the durable worker started there.
            asyncio.create_task(self._run(job_id, req, user["user_id"], dict(user), created_at=created_at))
        return Enqueued(job_id=job_id, created_at=created_at)

    async def status(self, job_id: str, user_id: str) -> dict:
        doc = await db.fetchrow("select * from analyze_jobs where job_id = $1 and user_id = $2", job_id, user_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Job not found")
        doc["timeout_seconds"] = None
        doc["remaining_seconds"] = None
        # Never return the base64 medical document to a polling client.
        doc.pop("request_payload", None)
        return doc

    # ---------------------------------------------------------------- background

    async def start(self) -> None:
        """Start one database-backed worker for this API process."""
        if self._worker_task and not self._worker_task.done():
            return
        self._wake = asyncio.Event()
        self._worker_task = asyncio.create_task(self._worker(), name="folin-analyze-worker")

    async def stop(self) -> None:
        if not self._worker_task:
            return
        self._worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker_task
        self._worker_task = None
        self._wake = None

    async def _heartbeat(self, job: dict) -> None:
        """Renew ownership while working; loss of ownership cancels the old worker."""
        while True:
            await asyncio.sleep(ANALYZE_HEARTBEAT_SECS)
            renewed = await asyncio.wait_for(db.fetchval(
                """with owned as (
                     select job_id from analyze_jobs
                     where job_id = $2 and started_at = $3 and status = 'processing'
                     for update skip locked
                   ) update analyze_jobs as j set heartbeat_at = $1
                   from owned where j.job_id = owned.job_id returning j.job_id""",
                _iso(), job["job_id"], job["started_at"]),
                timeout=ANALYZE_HEARTBEAT_SECS)
            if not renewed:
                # Saving holds this row's lock. Don't wait on our own transaction
                # or cancel a healthy save because renewal skipped its locked row.
                owned = await asyncio.wait_for(db.fetchval(
                    """select exists(select 1 from analyze_jobs where job_id = $1
                       and started_at = $2 and status = 'processing')""",
                    job["job_id"], job["started_at"]), timeout=ANALYZE_HEARTBEAT_SECS)
                if not owned:
                    return

    async def _with_heartbeat(self, job: dict) -> None:
        work = asyncio.create_task(self._execute_claimed(job))
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            done, _ = await asyncio.wait((work, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
            else:
                await heartbeat
                raise RuntimeError("Analysis worker lost its lease")
        finally:
            for task in (work, heartbeat):
                task.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)

    async def _claim(self, job_id: str | None = None) -> dict | None:
        """Atomically claim a pending job, or a processing job whose worker died.

        `skip locked` lets several API processes run this loop without two of them ever
        analyzing the same upload.
        """
        stale = (datetime.now(timezone.utc) - DEAD_AFTER).isoformat()
        return await db.fetchrow(
            """with candidate as (
                 select job_id from analyze_jobs
                 where request_payload is not null
                   and (status = $1 or (status = $2 and coalesce(heartbeat_at, started_at) < $3))
                   and ($5::text is null or job_id = $5)
                 order by created_at asc
                 for update skip locked limit 1
               )
               update analyze_jobs as j
               set status = $2, started_at = $4, heartbeat_at = $4, completed_at = null, error = null
               from candidate where j.job_id = candidate.job_id
               returning j.*""",
            str(JobStatus.PENDING), str(JobStatus.PROCESSING), stale, _iso(), job_id)

    async def _worker(self) -> None:
        current: dict | None = None
        while True:
            try:
                job = await self._claim()
                if job:
                    current = job
                    await self._with_heartbeat(job)
                    current = None
                    continue
                if self._wake:
                    self._wake.clear()
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=ANALYZE_WORKER_POLL_SECS)
            except asyncio.CancelledError:
                if current:
                    # A controlled shutdown hands the upload back immediately. A hard
                    # crash is reclaimed after DEAD_AFTER instead.
                    await db.execute(
                        """update analyze_jobs set status = $1, started_at = null, heartbeat_at = null
                           where job_id = $2 and status = $3 and request_payload is not null
                             and started_at = $4""",
                        str(JobStatus.PENDING), current["job_id"], str(JobStatus.PROCESSING), current["started_at"])
                raise
            except Exception:
                logger.exception("analysis worker loop failed")
                await asyncio.sleep(ANALYZE_WORKER_POLL_SECS)

    async def _execute_claimed(self, job: dict) -> None:
        payload = job.get("request_payload")
        user = await db.fetchrow("select * from users where user_id = $1", job["user_id"])
        if not user or not payload:
            await self._fail(job["job_id"], MSG_INTERRUPTED, job["started_at"])
            return
        try:
            req = AnalyzeRequest.model_validate(payload)
        except Exception:
            await self._fail(job["job_id"], MSG_INTERRUPTED, job["started_at"])
            await refund_daily_quota(job["user_id"])
            return
        await self._run(job["job_id"], req, job["user_id"], user,
                        created_at=job["created_at"], mark_started=False, claim_started_at=job["started_at"])

    async def _run(self, job_id: str, req: AnalyzeRequest, user_id: str, user: dict,
                   *, created_at: str, mark_started: bool = True, claim_started_at: str | None = None) -> None:
        async def fail(message):
            await self._fail(job_id, message, claim_started_at)
        try:
            if mark_started:
                await self._mark(job_id, JobStatus.PROCESSING, started=True)
            await pipeline.run(request_files(req), user_id, user, req.member_id,
                               job_id=job_id, claim_started_at=claim_started_at)
        except InvalidUpload as e:
            await fail(e.user_message)
            if e.status >= 500 or e.status in REFUND_STATUSES:
                await refund_daily_quota(user_id)
        except AnalysisRejected as e:
            await fail(e.user_message); await refund_daily_quota(user_id)
        except UnsupportedDocument as e:
            await fail(e.user_message)  # the work was done: no refund
        except ModelUnavailable as e:
            logger.error("AI analysis failed for job %s: %s", job_id, e)
            await fail(MSG_PROVIDER); await refund_daily_quota(user_id)
        except Exception as e:
            logger.exception("analyze job %s crashed", job_id)
            await fail(MSG_CRASHED.format(kind=type(e).__name__)); await refund_daily_quota(user_id)

    @staticmethod
    async def _mark(job_id: str, status: JobStatus, started: bool = False) -> None:
        if started:
            await db.execute("update analyze_jobs set status = $1, started_at = $2 where job_id = $3 and status = 'pending'", str(status), _iso(), job_id)
        else:
            await db.execute("update analyze_jobs set status = $1, completed_at = $2 where job_id = $3", str(status), _iso(), job_id)

    @staticmethod
    async def _fail(job_id: str, message: str, claim_started_at: str | None = None) -> None:
        await db.execute("""update analyze_jobs set status = $1, completed_at = $2, error = $3,
                            request_payload = null where job_id = $4 and status = any($5::text[])
                            and ($6::text is null or started_at = $6)""",
                         str(JobStatus.ERROR), _iso(), message, job_id, [str(s) for s in RUNNING], claim_started_at)


jobs = AnalyzeJobService()
