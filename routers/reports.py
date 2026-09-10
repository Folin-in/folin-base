"""Report routes. Thin: parse the request, call the service, return its result."""
from fastapi import APIRouter, Depends, Query

from app.constants import ANALYZE_JOB_TIMEOUT_SECS
from app.models import AnalyzeRequest, BiomarkerPatch, ReportFeedbackIn, ReportTitleUpdate
from core.security import get_current_user
from services.compare import compare
from services.jobs import jobs, remaining_seconds
from services.reports import reports
from services.tips import tips

router = APIRouter(prefix="/reports", tags=["reports"])


# ---------------------------------------------------------------- analyze jobs

@router.get("/analyze/active")
async def active_analyze_job(current_user: dict = Depends(get_current_user)):
    """The caller's in-flight job, if any, so the app can resume polling after a restart."""
    job = await jobs.active_job(current_user["user_id"])
    if not job:
        return {"active": False}
    return {"active": True, "job_id": job["job_id"], "status": job["status"],
            "filename": job["filename"], "created_at": job["created_at"],
            "timeout_seconds": ANALYZE_JOB_TIMEOUT_SECS,
            "remaining_seconds": remaining_seconds(job["created_at"])}


@router.post("/analyze")
async def analyze_report(req: AnalyzeRequest, current_user: dict = Depends(get_current_user)):
    """Enqueue an analysis; poll /analyze/status/{job_id} until success or error."""
    q = await jobs.enqueue(req, current_user)
    return {"job_id": q.job_id, "status": q.status, "created_at": q.created_at,
            "timeout_seconds": ANALYZE_JOB_TIMEOUT_SECS,
            "remaining_seconds": remaining_seconds(q.created_at)}


@router.get("/analyze/status/{job_id}")
async def analyze_status(job_id: str, current_user: dict = Depends(get_current_user)):
    return await jobs.status(job_id, current_user["user_id"])


# ---------------------------------------------------------------- reads

@router.get("")
async def list_reports(member_id: str | None = Query(default=None),
                       current_user: dict = Depends(get_current_user)):
    return await reports.list_for(current_user["user_id"], member_id)


@router.get("/suggestions")
async def list_all_suggestions(member_id: str | None = Query(default=None),
                               current_user: dict = Depends(get_current_user)):
    feed = await tips.feed(current_user, member_id)
    return {"reports": feed.reports, "suggestions": feed.suggestions}


@router.get("/retest-reminders")
async def retest_reminders(member_id: str | None = Query(default=None),
                           current_user: dict = Depends(get_current_user)):
    return await compare.retest_reminders(current_user, member_id)


@router.get("/compare")
async def compare_reports(a: str | None = None, b: str | None = None, against: str | None = None,
                          member_id: str | None = Query(default=None),
                          current_user: dict = Depends(get_current_user)):
    return (await compare.compare(current_user["user_id"], member_id, a, b, against)).as_dict()


@router.get("/{report_id}")
async def get_report(report_id: str, current_user: dict = Depends(get_current_user)):
    return await reports.get(report_id, current_user["user_id"])


# ---------------------------------------------------------------- writes

@router.delete("/{report_id}")
async def delete_report(report_id: str, current_user: dict = Depends(get_current_user)):
    await reports.soft_delete(report_id, current_user["user_id"])
    return {"deleted": True}


@router.patch("/{report_id}")
async def rename_report(report_id: str, req: ReportTitleUpdate,
                        current_user: dict = Depends(get_current_user)):
    return await reports.rename(report_id, current_user["user_id"], req)


@router.patch("/{report_id}/biomarkers")
async def edit_biomarker(report_id: str, req: BiomarkerPatch,
                         current_user: dict = Depends(get_current_user)):
    return await reports.edit_biomarker(report_id, current_user, req)


# ---------------------------------------------------------------- feedback

@router.post("/{report_id}/feedback")
async def submit_report_feedback(report_id: str, req: ReportFeedbackIn,
                                 current_user: dict = Depends(get_current_user)):
    await reports.submit_feedback(report_id, current_user["user_id"], req)
    return {"ok": True}


@router.get("/{report_id}/feedback")
async def get_report_feedback(report_id: str, current_user: dict = Depends(get_current_user)):
    return await reports.feedback(report_id, current_user["user_id"])
