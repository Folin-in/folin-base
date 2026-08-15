"""The analyze job queue: validate an upload, enforce quotas and one-job-per-user, run
the pipeline in the background, and let the client poll.

A job outlives the request that started it (the client can disconnect), is bounded by
ANALYZE_JOB_TIMEOUT_SECS so it can never hang, and a crash mid-job cannot strand the
user: any row still 'processing' past timeout+grace is treated as dead by both the
poller (reports it failed) and the upload path (stops it blocking new uploads).
"""
import asyncio
import base64
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from agents.base import ModelUnavailable
from app.config import settings
from app.constants import (ALLOWED_MIME, ANALYZE_JOB_GRACE_SECS, ANALYZE_JOB_TIMEOUT_SECS, MAX_ANALYZE_FILES,
                           MAX_UPLOAD_BASE64_BYTES, MIN_FILE_BASE64_CHARS)
from app.enums import JobStatus, Plan
from app.models import AnalyzeFileIn, AnalyzeRequest
from core.rate_limit import check_and_increment_daily_quota, refund_daily_quota
from db import session as db
from services.analysis import (MSG_PROVIDER, AnalysisRejected, InvalidUpload, UnsupportedDocument,
                               pipeline)
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
MSG_BUSY = ("“{filename}” is still being analyzed. It'll finish in under a minute - "
            "your new report can go up right after.")
MSG_FREE_EXHAUSTED = ("You've used your {n} free analyses. Upgrade to Pro for {daily} every day.")
MSG_TIMEOUT = "Analysis timed out. Please try again with a clearer file."
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
    status: JobStatus = JobStatus.PENDING


class AnalyzeJobService:
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
        cutoff = datetime.now(timezone.utc) - DEAD_AFTER
        for r in rows:
            try:
                if datetime.fromisoformat(r["created_at"]) > cutoff:
                    return r
            except ValueError:
                continue
        return None

    @staticmethod
    async def charge_quota(user: dict) -> None:
        """Free = a lifetime trial allowance; Pro = a daily cap. Checked BEFORE the job is
        created so a refused upload never occupies the one-job slot."""
        plan = (user.get("preferences") or {}).get("plan") or Plan.FREE
        if plan == Plan.PRO:
            await check_and_increment_daily_quota(user["user_id"], plan)
            return
        used = await db.fetchval(
            "select count(*) from analyze_jobs where user_id = $1 and status = $2",
            user["user_id"], str(JobStatus.SUCCESS))
        if used >= settings.free_total_analyses:
            raise HTTPException(status_code=403, detail=MSG_FREE_EXHAUSTED.format(
                n=settings.free_total_analyses, daily=settings.pro_daily_analyses))

    # ---------------------------------------------------------------- lifecycle

    async def enqueue(self, req: AnalyzeRequest, user: dict) -> Enqueued:
        files = request_files(req)
        self.validate(files)
        member_id = member_id_of(req.member_id)
        await self.reject_duplicate(user["user_id"], member_id, self.file_hash(files))
        if running := await self.active_job(user["user_id"]):
            raise HTTPException(status_code=409, detail=MSG_BUSY.format(filename=running["filename"]))
        await self.charge_quota(user)

        job_id = uuid.uuid4().hex[:JOB_ID_HEX]
        await db.execute(
            """insert into analyze_jobs (job_id, user_id, member_id, filename, mime_type, status, created_at)
               values ($1, $2, $3, $4, $5, $6, $7)""",
            job_id, user["user_id"], member_id, display_name(files), files[0].mime_type,
            str(JobStatus.PENDING), _iso())
        asyncio.create_task(self._run(job_id, req, user["user_id"], dict(user)))
        return Enqueued(job_id=job_id)

    async def status(self, job_id: str, user_id: str) -> dict:
        doc = await db.fetchrow("select * from analyze_jobs where job_id = $1 and user_id = $2", job_id, user_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Job not found")
        if doc.get("status") in RUNNING:
            try:
                if datetime.now(timezone.utc) - datetime.fromisoformat(doc["created_at"]) > DEAD_AFTER:
                    doc = {**doc, "status": str(JobStatus.ERROR), "error": MSG_INTERRUPTED}
            except ValueError:
                pass
        return doc

    # ---------------------------------------------------------------- background

    async def _run(self, job_id: str, req: AnalyzeRequest, user_id: str, user: dict) -> None:
        try:
            await self._mark(job_id, JobStatus.PROCESSING, started=True)
            result = await asyncio.wait_for(
                pipeline.run(request_files(req), user_id, user, req.member_id),
                timeout=ANALYZE_JOB_TIMEOUT_SECS)
            await db.execute(
                """update analyze_jobs set status = $1, completed_at = $2, report_id = $3,
                   xp_earned = $4, new_badges = $5, error = null where job_id = $6""",
                str(JobStatus.SUCCESS), _iso(), result.report_id, result.xp_earned, result.new_badges, job_id)
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
        col = "started_at" if started else "completed_at"
        await db.execute(f"update analyze_jobs set status = $1, {col} = $2 where job_id = $3",
                         str(status), _iso(), job_id)

    @staticmethod
    async def _fail(job_id: str, message: str) -> None:
        await db.execute("update analyze_jobs set status = $1, completed_at = $2, error = $3 where job_id = $4",
                         str(JobStatus.ERROR), _iso(), message, job_id)


jobs = AnalyzeJobService()
