"""Durable analyze-job queue: validate, enforce quota, persist the request, and let a
database-claiming worker run the pipeline while the client polls.

The temporary request payload is erased on success or failure. A graceful shutdown hands
its current job back immediately; a hard crash leaves enough data for another worker to
reclaim it after timeout+grace. Atomic `skip locked` claims support multiple API workers.
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
from app.constants import (ALLOWED_MIME, ANALYZE_JOB_GRACE_SECS, ANALYZE_JOB_TIMEOUT_SECS,
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
DEAD_AFTER = timedelta(seconds=ANALYZE_JOB_TIMEOUT_SECS + ANALYZE_JOB_GRACE_SECS)
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
MSG_TIMEOUT = "Analysis reached the 200-second limit and was stopped. Please try again."
MSG_INTERRUPTED = "That analysis was interrupted. Please try uploading again."
MSG_CRASHED = "Analysis crashed. Please try again. ({kind})"


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def deadline_for(created_at: str) -> datetime:
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created + timedelta(seconds=ANALYZE_JOB_TIMEOUT_SECS)


def remaining_seconds(created_at: str) -> float:
    return max(0.0, (deadline_for(created_at) - datetime.now(timezone.utc)).total_seconds())


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
        self._expiry_task: asyncio.Task | None = None

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
        for r in rows:
            try:
                if remaining_seconds(r["created_at"]) > 0:
                    return r
            except (ValueError, TypeError):
                pass
            await db.execute(
                """update analyze_jobs set status = $1, completed_at = $2, error = $3,
                   request_payload = null
                   where job_id = $4 and status = any($5::text[])""",
                str(JobStatus.ERROR), _iso(), MSG_TIMEOUT, r["job_id"], [str(s) for s in RUNNING])
        return None

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
        if doc.get("status") in RUNNING:
            try:
                expired = remaining_seconds(doc["created_at"]) <= 0
            except (ValueError, TypeError):
                expired = True
            if expired:
                await self._fail(job_id, MSG_TIMEOUT)
                # A simultaneous successful commit wins over expiry.
                doc = await db.fetchrow("select * from analyze_jobs where job_id = $1 and user_id = $2", job_id, user_id)
        doc["timeout_seconds"] = ANALYZE_JOB_TIMEOUT_SECS
        doc["remaining_seconds"] = remaining_seconds(doc["created_at"]) if doc.get("status") in RUNNING else 0
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
        self._expiry_task = asyncio.create_task(self._expire_loop(), name="folin-analyze-expiry")

    async def stop(self) -> None:
        if self._expiry_task:
            self._expiry_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._expiry_task
            self._expiry_task = None
        if not self._worker_task:
            return
        self._worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker_task
        self._worker_task = None
        self._wake = None

    async def _expire_loop(self) -> None:
        """Expire queued/orphaned work even while the analysis worker is busy."""
        while True:
            try:
                cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ANALYZE_JOB_TIMEOUT_SECS)).isoformat()
                await db.execute(
                    """update analyze_jobs set status = $1, completed_at = $2, error = $3,
                       request_payload = null where status = any($4::text[]) and created_at <= $5""",
                    str(JobStatus.ERROR), _iso(), MSG_TIMEOUT, [str(s) for s in RUNNING], cutoff)
            except Exception:
                logger.exception("analysis expiry sweep failed")
            await asyncio.sleep(ANALYZE_WORKER_POLL_SECS)

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
                   and (status = $1 or (status = $2 and started_at < $3))
                   and ($5::text is null or job_id = $5)
                 order by created_at asc
                 for update skip locked limit 1
               )
               update analyze_jobs as j
               set status = $2, started_at = $4, completed_at = null, error = null
               from candidate where j.job_id = candidate.job_id
               returning j.*""",
            str(JobStatus.PENDING), str(JobStatus.PROCESSING), stale, _iso(), job_id)

    async def _worker(self) -> None:
        current: str | None = None
        while True:
            try:
                job = await self._claim()
                if job:
                    current = job["job_id"]
                    await self._execute_claimed(job)
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
                        """update analyze_jobs set status = $1, started_at = null
                           where job_id = $2 and status = $3 and request_payload is not null""",
                        str(JobStatus.PENDING), current, str(JobStatus.PROCESSING))
                raise
            except Exception:
                logger.exception("analysis worker loop failed")
                await asyncio.sleep(ANALYZE_WORKER_POLL_SECS)

    async def _execute_claimed(self, job: dict) -> None:
        if remaining_seconds(job["created_at"]) <= 0:
            await self._fail(job["job_id"], MSG_TIMEOUT)
            return
        payload = job.get("request_payload")
        try:
            user = await asyncio.wait_for(
                db.fetchrow("select * from users where user_id = $1", job["user_id"]),
                timeout=remaining_seconds(job["created_at"]))
        except TimeoutError:
            await self._fail(job["job_id"], MSG_TIMEOUT)
            return
        if not user or not payload:
            await self._fail(job["job_id"], MSG_INTERRUPTED)
            return
        try:
            req = AnalyzeRequest.model_validate(payload)
        except Exception:
            await self._fail(job["job_id"], MSG_INTERRUPTED)
            await refund_daily_quota(job["user_id"])
            return
        await self._run(job["job_id"], req, job["user_id"], user,
                        created_at=job["created_at"], mark_started=False)

    async def _run(self, job_id: str, req: AnalyzeRequest, user_id: str, user: dict,
                   *, created_at: str, mark_started: bool = True) -> None:
        try:
            remaining = remaining_seconds(created_at)
            if remaining <= 0:
                raise TimeoutError
            # One cancellation budget covers all AI requests, sleeps and storage work.
            # Cancellation propagates through httpx and isn't swallowed by Exception handlers.
            async with asyncio.timeout(remaining):
                if mark_started:
                    await self._mark(job_id, JobStatus.PROCESSING, started=True)
                await pipeline.run(request_files(req), user_id, user, req.member_id,
                                   job_id=job_id, deadline_at=deadline_for(created_at))
        except asyncio.TimeoutError:
            await self._fail(job_id, MSG_TIMEOUT); await refund_daily_quota(user_id)
        except InvalidUpload as e:
            await self._fail(job_id, e.user_message)
            if e.status >= 500 or e.status in REFUND_STATUSES:
                await refund_daily_quota(user_id)
        except AnalysisRejected as e:
            await self._fail(job_id, e.user_message); await refund_daily_quota(user_id)
        except UnsupportedDocument as e:
            await self._fail(job_id, e.user_message)  # the work was done: no refund
        except ModelUnavailable as e:
            logger.error("AI analysis failed for job %s: %s", job_id, e)
            await self._fail(job_id, MSG_PROVIDER); await refund_daily_quota(user_id)
        except Exception as e:
            logger.exception("analyze job %s crashed", job_id)
            await self._fail(job_id, MSG_CRASHED.format(kind=type(e).__name__)); await refund_daily_quota(user_id)

    @staticmethod
    async def _mark(job_id: str, status: JobStatus, started: bool = False) -> None:
        if started:
            await db.execute("update analyze_jobs set status = $1, started_at = $2 where job_id = $3 and status = 'pending'", str(status), _iso(), job_id)
        else:
            await db.execute("update analyze_jobs set status = $1, completed_at = $2 where job_id = $3", str(status), _iso(), job_id)

    @staticmethod
    async def _fail(job_id: str, message: str) -> None:
        await db.execute("""update analyze_jobs set status = $1, completed_at = $2, error = $3,
                            request_payload = null where job_id = $4 and status = any($5::text[])""",
                         str(JobStatus.ERROR), _iso(), message, job_id, [str(s) for s in RUNNING])


jobs = AnalyzeJobService()
