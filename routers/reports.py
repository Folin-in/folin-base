"""Report routes: AI analysis, listing, detail, delete, biomarker edit - all
user-scoped and auth-protected. Members-aware (?member_id=self by default)."""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re as _re
from re import escape as _re_escape, search as _re_search
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

import cache
import config
import database as db
import storage
from ai_agent import reconcile_tips, refresh_marker_advice, run_ai_analysis, verify_biomarkers
from ai_tools import build_profile_context
from gamification import (STATUS_WEIGHT, compute_document_score, compute_health_score,
                          compute_profile, effective_date, sort_by_effective_date)
from models import (REPORT_TYPES, AnalyzeFileIn, AnalyzeRequest, Biomarker, BiomarkerPatch,
                    Medication, Report, ReportFeedbackIn, ReportTitleUpdate, Suggestion)
from rate_limit import check_and_increment_daily_quota, refund_daily_quota
from routers.members import resolve_member_profile
from routers.users import fetch_events
from sanity import sanity_note
from security import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reports", tags=["reports"])

ALLOWED_MIME = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
SUGGESTION_CATEGORIES = {"nutrition", "movement", "sleep", "supplements", "lifestyle"}
LEGACY_CATEGORY_MAP = {"diet": "nutrition", "exercise": "movement"}
# Analysis jobs die after this many seconds so errors can't hang forever.
ANALYZE_JOB_TIMEOUT_SECS = 180
MAX_ANALYZE_FILES = 5


def _req_files(req: AnalyzeRequest) -> list[AnalyzeFileIn]:
    """Normalize AnalyzeRequest to a list of files (new `files` field or legacy single-file)."""
    if req.files:
        return req.files
    return [AnalyzeFileIn(filename=req.filename, mime_type=req.mime_type, file_base64=req.file_base64)]


def _files_display_name(files: list[AnalyzeFileIn]) -> str:
    first = files[0].filename or "report"
    return first if len(files) == 1 else f"{first} (+{len(files) - 1} more)"


def _normalize_category(cat: str | None) -> str:
    if not cat:
        return "lifestyle"
    cat = cat.strip().lower()
    cat = LEGACY_CATEGORY_MAP.get(cat, cat)
    return cat if cat in SUGGESTION_CATEGORIES else "lifestyle"


def _valid_id(report_id: str) -> bool:
    try:
        uuid.UUID(report_id)
        return True
    except ValueError:
        return False


_NAME_TITLES = {"mr", "mrs", "ms", "miss", "mx", "dr", "master", "shri", "smt", "sri", "kumari", "baby"}


def _name_tokens(name: str) -> set[str]:
    return {t for t in _re.sub(r"[^a-z ]", " ", name.lower()).split() if len(t) > 1 and t not in _NAME_TITLES}


def _names_match(profile_name: str | None, patient_name: str | None) -> bool:
    """Lenient: any shared name token (or substring token) counts as a match. Only a
    complete disjoint - e.g. profile 'Ketan Raj' vs report 'Sunita Sharma' - is a mismatch.
    Missing either name = no judgement (returns True)."""
    a, b = _name_tokens(profile_name or ""), _name_tokens(patient_name or "")
    if not a or not b:
        return True
    if a & b:
        return True
    return any(x in y or y in x for x in a for y in b)


def _member_id_of(member_id: str | None) -> str:
    return (member_id or "self").strip() or "self"


async def _member_reports(user_id: str, member_id: str | None, lab_only: bool = False) -> list[dict]:
    mid = _member_id_of(member_id)
    rows = await cache.get_reports(user_id)
    return [
        r for r in rows
        if r.get("member_id", "self") == mid and (not lab_only or r.get("report_type") == "lab")
    ]


async def _do_analyze(req: AnalyzeRequest, user_id: str, current_user_snapshot: dict) -> dict:
    """The actual AI analyze work. Returns {report_id, report, xp_earned, new_badges, profile}.
    Extracted so it can run in a background task detached from the request lifecycle.
    Raises HTTPException on user-facing errors, generic Exception on unexpected failures.
    """
    files = _req_files(req)
    raws: list[bytes] = []
    for f in files:
        try:
            raws.append(base64.b64decode(f.file_base64, validate=False))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid file data.")
    if sum(len(r) for r in raws) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Files too large (max 10 MB total). Try photos of the report instead of scans, or compress the PDF.")

    member_id = _member_id_of(req.member_id)
    display_name, hp = await resolve_member_profile(current_user_snapshot, member_id)
    language = (hp.get("language") or "english").lower()
    profile_context = build_profile_context(display_name or current_user_snapshot.get("name", ""), hp)

    existing = sort_by_effective_date(await _member_reports(user_id, member_id))
    if existing:
        prev = existing[-1]
        prev_date = effective_date(prev) or (prev.get("uploaded_at", "")[:10])
        flagged = [
            f"{b.get('name')}: {b.get('value')} {b.get('unit', '')} ({b.get('status')})"
            for b in prev.get("biomarkers", []) if b.get("status") != "normal"
        ]
        profile_context += (
            f" PREVIOUS REPORT ({prev_date}, score {prev.get('health_score')}): "
            + ("flagged markers were " + "; ".join(flagged) if flagged else "all markers were in range")
            + ". Compare new values against these where the same marker appears and explicitly mention improvements or declines."
        )

    tmp_paths: list[str] = []
    try:
        attachments: list[tuple[str, str]] = []
        for f, raw in zip(files, raws):
            with tempfile.NamedTemporaryFile(suffix=ALLOWED_MIME[f.mime_type], delete=False) as tmp:
                tmp.write(raw)
                tmp_paths.append(tmp.name)
                attachments.append((tmp.name, f.mime_type))

        try:
            parsed = await run_ai_analysis(attachments, profile_context, language=language)
        except (ValueError, json.JSONDecodeError) as e:
            logger.error(f"AI JSON parse failure: {e}")
            raise HTTPException(status_code=502, detail="The AI couldn't read this report. Please try again or use a clearer file.")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"AI analysis failed: {e}")
            raise HTTPException(status_code=502, detail="AI analysis failed. Please try again in a moment.")

        report_type = str(parsed.get("report_type") or "").strip().lower()
        if not report_type and parsed.get("is_blood_report"):
            report_type = "lab"
        if report_type == "unknown":
            raise HTTPException(status_code=400, detail="This doesn't look like a medical document. Try a different file.")
        if report_type not in REPORT_TYPES:
            report_type = "lab" if parsed.get("biomarkers") else "other"

        biomarkers = []
        for b in parsed.get("biomarkers", []):
            try:
                biomarkers.append(Biomarker(
                    name=str(b.get("name", "")).strip(),
                    value=str(b.get("value", "")).strip(),
                    unit=str(b.get("unit") or "").strip(),
                    ref_range=str(b.get("ref_range") or "").strip(),
                    status=b.get("status") if b.get("status") in STATUS_WEIGHT else "normal",
                    category=str(b.get("category") or "Other").strip(),
                    explanation=str(b.get("explanation") or "").strip(),
                    tip=str(b.get("tip") or "").strip(),
                ))
            except Exception:
                continue

        # Sanity guardrails (free, instant): flag implausible values / status-range contradictions.
        for b in biomarkers:
            note = sanity_note(b.model_dump())
            if note:
                b.needs_verification = True
                b.verification_note = note

        # PRO self-check: second AI pass cross-checks every value/status against the document.
        plan = ((current_user_snapshot.get("preferences") or {}).get("plan")) or "free"
        if plan == "pro" and biomarkers:
            try:
                mismatches = await verify_biomarkers(attachments, [b.model_dump() for b in biomarkers])
                by_name = {str(m.get("name", "")).strip().lower(): m for m in mismatches}
                for b in biomarkers:
                    m = by_name.get(b.name.strip().lower())
                    if m:
                        b.needs_verification = True
                        issue = str(m.get("issue") or "").strip()
                        expected = str(m.get("expected") or "").strip()
                        b.verification_note = (
                            (issue or "This value may not match the document - please double-check.")
                            + (f" (Document shows: {expected})" if expected else "")
                        )[:300]
            except Exception as e:
                logger.warning(f"AI self-check failed (skipping): {e}")

        suggestions = []
        for s in parsed.get("suggestions", []) or []:
            try:
                cat = _normalize_category(s.get("category"))
                linked = s.get("linked_marker")
                suggestions.append(Suggestion(
                    title=str(s.get("title", "")).strip(),
                    body=str(s.get("body", "")).strip(),
                    category=cat,
                    linked_marker=str(linked).strip() if linked else None,
                ))
            except Exception:
                continue

        # BMI from the profile as a first-class marker: shows in the report's marker list,
        # counts in in-range totals, and feeds the weighted health score like any other test.
        # Members (sub-profiles) don't carry weight/height, so this is naturally self-only.
        w, h = hp.get("weight_kg"), hp.get("height_cm")
        if report_type == "lab" and w and h and not any(b.name.strip().lower() == "bmi" for b in biomarkers):
            bmi = round(w / ((h / 100) ** 2), 1)
            if bmi < 17:
                bmi_status = "low"
            elif bmi < 18.5:
                bmi_status = "borderline_low"
            elif bmi < 25:
                bmi_status = "normal"
            elif bmi < 30:
                bmi_status = "borderline_high"
            else:
                bmi_status = "high"
            biomarkers.append(Biomarker(
                name="BMI",
                value=str(bmi),
                unit="kg/m²",
                ref_range="18.5 - 24.9",
                status=bmi_status,
                category="Other",
                explanation=f"Body Mass Index, worked out from your profile ({w} kg, {h} cm). It's a rough gauge of weight for your height - not a full picture of health on its own.",
                tip="This comes from your profile, not the lab report - keep your weight and height up to date there so it stays accurate.",
            ))

        bio_dicts = [b.model_dump() for b in biomarkers]
        if report_type == "lab" and bio_dicts:
            health_score = compute_health_score(bio_dicts)
        else:
            positives_raw = [str(p).strip() for p in (parsed.get("positives") or []) if str(p).strip()]
            concerns_raw = [str(c).strip() for c in (parsed.get("concerns") or []) if str(c).strip()]
            health_score = compute_document_score(positives_raw, concerns_raw)
        normal_count = sum(1 for b in bio_dicts if b["status"] == "normal")
        attention_count = sum(1 for b in bio_dicts if b["status"] in ("high", "low"))

        medications: list[Medication] = []
        for m in parsed.get("medications") or []:
            try:
                name = str(m.get("name") or "").strip()
                if not name:
                    continue
                medications.append(Medication(
                    name=name,
                    dose=(str(m["dose"]).strip() if m.get("dose") else None),
                    schedule=(str(m["schedule"]).strip() if m.get("schedule") else None),
                    duration=(str(m["duration"]).strip() if m.get("duration") else None),
                    purpose=(str(m["purpose"]).strip() if m.get("purpose") else None),
                ))
            except Exception:
                continue

        def _str_list(key: str) -> list[str]:
            v = parsed.get(key) or []
            # Model occasionally returns a single string instead of a list - iterating it
            # would produce one-character items.
            if isinstance(v, str):
                v = [v]
            return [str(x).strip() for x in v if str(x).strip()]

        summary_text = _re.sub(r"\s*\(?\b\d{1,3}\s*/\s*100\b\)?", "", str(parsed.get("summary") or "")).strip()
        lab_or_provider = parsed.get("lab_name") or parsed.get("provider")

        patient_name = parsed.get("patient_name")
        name_mismatch = not _names_match(display_name, str(patient_name) if patient_name else None)

        # Keep the original document in Supabase Storage so the user can re-view it.
        # Best-effort: analysis result is stored either way. Multi-page uploads store
        # every page; paths are comma-joined in the single storage_path text column
        # (paths are "<uuid>/<hex><ext>" - never contain commas).
        stored_paths: list[str] = []
        for f, raw in zip(files, raws):
            page_path = f"{user_id}/{uuid.uuid4().hex}{ALLOWED_MIME[f.mime_type]}"
            if await storage.upload_report_file(page_path, raw, f.mime_type):
                stored_paths.append(page_path)
        storage_path = ",".join(stored_paths) or None
        file_hash = hashlib.sha256(b"".join(raws)).hexdigest()

        report = Report(
            user_id=user_id,
            member_id=member_id,
            filename=_files_display_name(files),
            mime_type=files[0].mime_type,
            storage_path=storage_path,
            file_hash=file_hash,
            uploaded_at=datetime.now(timezone.utc).isoformat(),
            report_type=report_type,
            report_date=parsed.get("report_date"),
            patient_name=(str(patient_name).strip() if patient_name else None),
            name_mismatch=name_mismatch,
            lab_name=(str(lab_or_provider).strip() if lab_or_provider else None),
            summary=summary_text,
            overall_assessment=str(parsed.get("overall_assessment") or ""),
            positives=_str_list("positives"),
            concerns=_str_list("concerns"),
            suggestions=suggestions,
            biomarkers=biomarkers,
            health_score=health_score,
            normal_count=normal_count,
            attention_count=attention_count,
            total_count=len(biomarkers),
            chief_complaint=(str(parsed["chief_complaint"]).strip() if parsed.get("chief_complaint") else None),
            exam_findings=_str_list("exam_findings"),
            diagnoses=_str_list("diagnoses"),
            referrals=_str_list("referrals"),
            medications=medications,
            modality=(str(parsed["modality"]).strip() if parsed.get("modality") else None),
            body_area=(str(parsed["body_area"]).strip() if parsed.get("body_area") else None),
            findings=_str_list("findings"),
            impression=(str(parsed["impression"]).strip() if parsed.get("impression") else None),
            admission_date=parsed.get("admission_date"),
            discharge_date=parsed.get("discharge_date"),
            procedures=_str_list("procedures"),
            follow_up=(str(parsed["follow_up"]).strip() if parsed.get("follow_up") else None),
        )

        events = await fetch_events(user_id)
        profile_before = compute_profile(existing, events=events)

        d = report.model_dump(exclude={"id"})
        cols = list(d.keys())
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        new_id = await db.fetchval(
            f"insert into reports ({', '.join(cols)}) values ({placeholders}) returning id",
            *d.values(),
        )
        report.id = str(new_id)
        cache.invalidate_reports(user_id)

        all_reports = existing + [report.model_dump()]
        profile_after = compute_profile(all_reports, events=events)

        before_ids = {b["id"] for b in profile_before["badges"] if b["unlocked"]}
        new_badges = [b for b in profile_after["badges"] if b["unlocked"] and b["id"] not in before_ids]

        return {
            "report_id": report.id,
            "report": report.model_dump(),
            "xp_earned": profile_after["xp"] - profile_before["xp"],
            "new_badges": new_badges,
            "profile": profile_after,
        }
    finally:
        for p in tmp_paths:
            if os.path.exists(p):
                os.unlink(p)


async def _run_analyze_job(job_id: str, req: AnalyzeRequest, user_id: str, current_user_snapshot: dict):
    """Fire-and-forget task. Bounded by ANALYZE_JOB_TIMEOUT_SECS so it can never
    hang forever. On any failure, marks the job errored AND refunds the daily quota
    so the user isn't punished for something that never produced a report.
    """
    def _iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    try:
        await db.execute(
            "update analyze_jobs set status = 'processing', started_at = $1 where job_id = $2", _iso(), job_id
        )
        result = await asyncio.wait_for(
            _do_analyze(req, user_id, current_user_snapshot),
            timeout=ANALYZE_JOB_TIMEOUT_SECS,
        )
        await db.execute(
            """update analyze_jobs set status = 'success', completed_at = $1, report_id = $2,
               xp_earned = $3, new_badges = $4, error = null where job_id = $5""",
            _iso(), result["report_id"], result["xp_earned"], result["new_badges"], job_id,
        )
    except asyncio.TimeoutError:
        await db.execute(
            "update analyze_jobs set status = 'error', completed_at = $1, error = $2 where job_id = $3",
            _iso(), "Analysis timed out. Please try again with a clearer file.", job_id,
        )
        await refund_daily_quota(user_id)
    except HTTPException as e:
        await db.execute(
            "update analyze_jobs set status = 'error', completed_at = $1, error = $2 where job_id = $3",
            _iso(), str(e.detail), job_id,
        )
        # Refund quota only for retryable errors - unknown-document (400) should still count.
        if e.status_code >= 500:
            await refund_daily_quota(user_id)
    except Exception as e:
        logger.exception(f"analyze job {job_id} crashed")
        await db.execute(
            "update analyze_jobs set status = 'error', completed_at = $1, error = $2 where job_id = $3",
            _iso(), f"Analysis crashed. Please try again. ({type(e).__name__})", job_id,
        )
        await refund_daily_quota(user_id)


async def _active_job(user_id: str) -> dict | None:
    """The user's currently-running analyze job, if any. Jobs older than the server
    timeout (+ grace) are treated as dead even if a crash left them 'processing' -
    otherwise a stuck row would block uploads forever."""
    rows = await db.fetch(
        """select * from analyze_jobs where user_id = $1 and status in ('pending', 'processing')
           order by created_at desc limit 5""",
        user_id,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ANALYZE_JOB_TIMEOUT_SECS + 60)
    for r in rows:
        try:
            if datetime.fromisoformat(r["created_at"]) > cutoff:
                return r
        except ValueError:
            continue
    return None


@router.get("/analyze/active")
async def active_analyze_job(current_user: dict = Depends(get_current_user)):
    """The in-flight analyze job for this user (or {active: false}). Lets the upload
    screen block new uploads and say which report is still being analyzed."""
    job = await _active_job(current_user["user_id"])
    if not job:
        return {"active": False}
    return {
        "active": True,
        "job_id": job["job_id"],
        "filename": job["filename"],
        "member_id": job["member_id"],
        "status": job["status"],
        "created_at": job["created_at"],
    }


@router.post("/analyze")
async def analyze_report(req: AnalyzeRequest, current_user: dict = Depends(get_current_user)):
    """Async analyze: creates a job, starts a background task, returns {job_id, status}.
    Client polls GET /reports/analyze/status/{job_id} until status ∈ {success, error}.
    Job survives client disconnect and self-times-out after ANALYZE_JOB_TIMEOUT_SECS.
    One job per user at a time - a second upload while one is running gets a 409.
    """
    files = _req_files(req)
    if len(files) > MAX_ANALYZE_FILES:
        raise HTTPException(status_code=400, detail=f"You can upload at most {MAX_ANALYZE_FILES} photos at once.")
    if len(files) > 1 and any(not f.mime_type.startswith("image/") for f in files):
        raise HTTPException(status_code=400, detail="Multi-page upload works with photos only. Upload PDFs one at a time.")
    for f in files:
        if f.mime_type not in ALLOWED_MIME:
            raise HTTPException(status_code=400, detail="Unsupported file type. Upload a PDF or a photo (JPG/PNG).")
    # HARDENING-4: reject oversized payloads BEFORE decoding.
    # Base64 inflates by ~4/3, so 10 MB decoded is ~14 MB encoded. Reject before decoding.
    if sum(len(f.file_base64) for f in files) > 14 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Files too large (max 10 MB total). Try photos of the report instead of scans, or compress the PDF.")

    # Duplicate detection: the same bytes analyzed twice can score differently
    # (the second run sees previous-report context), which reads as a bug and
    # wastes quota. Same document for the same member -> point at the existing report.
    digest = hashlib.sha256()
    for f in files:
        try:
            digest.update(base64.b64decode(f.file_base64, validate=False))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid file data.")
    file_hash = digest.hexdigest()
    dup = await db.fetchrow(
        """select id, uploaded_at from reports
           where user_id = $1 and member_id = $2 and file_hash = $3 limit 1""",
        current_user["user_id"], _member_id_of(req.member_id), file_hash,
    )
    if dup:
        raise HTTPException(
            status_code=409,
            detail=f"You already analyzed this exact document on {str(dup['uploaded_at'])[:10]}. "
                   "Open it from the Reports tab, or delete it first to re-analyze.",
        )

    # One analysis at a time per user - checked BEFORE burning a quota slot.
    running = await _active_job(current_user["user_id"])
    if running:
        raise HTTPException(
            status_code=409,
            detail=f"“{running['filename']}” is still being analyzed. Please wait for it to finish before uploading another report.",
        )

    # Quotas: free = FREE_TOTAL_ANALYSES total (a trial, not per-day); Pro = daily cap.
    plan = (current_user.get("preferences") or {}).get("plan") or "free"
    if plan == "pro":
        await check_and_increment_daily_quota(current_user["user_id"], plan)
    else:
        used = await db.fetchval(
            "select count(*) from analyze_jobs where user_id = $1 and status = 'success'",
            current_user["user_id"],
        )
        if used >= config.FREE_TOTAL_ANALYSES:
            raise HTTPException(
                status_code=403,
                detail=f"You've used your {config.FREE_TOTAL_ANALYSES} free analyses. Upgrade to Pro for {config.PRO_DAILY_ANALYSES} every day.",
            )

    job_id = uuid.uuid4().hex[:16]
    await db.execute(
        """insert into analyze_jobs (job_id, user_id, member_id, filename, mime_type, status, created_at)
           values ($1, $2, $3, $4, $5, 'pending', $6)""",
        job_id, current_user["user_id"], _member_id_of(req.member_id), _files_display_name(files),
        files[0].mime_type, datetime.now(timezone.utc).isoformat(),
    )
    # Snapshot the user so the background task doesn't need to re-fetch (avoids stale-context issues).
    snapshot = dict(current_user)
    asyncio.create_task(_run_analyze_job(job_id, req, current_user["user_id"], snapshot))
    return {"job_id": job_id, "status": "pending"}


@router.get("/analyze/status/{job_id}")
async def analyze_status(job_id: str, current_user: dict = Depends(get_current_user)):
    """Poll the status of an analyze job. Response shape:
    {job_id, status: "pending"|"processing"|"success"|"error", report_id?, xp_earned?, new_badges?, error?, created_at, ...}
    """
    doc = await db.fetchrow(
        "select * from analyze_jobs where job_id = $1 and user_id = $2", job_id, current_user["user_id"]
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Job not found")
    return doc


@router.get("")
async def list_reports(
    member_id: str | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    # Recency here means "when you uploaded it", not the report's own date - a just-uploaded
    # old report should still show on top. Trend/compare/streaks keep effective-date order.
    docs = sorted(
        await _member_reports(current_user["user_id"], member_id),
        key=lambda r: r.get("uploaded_at") or "", reverse=True,
    )
    out = []
    for d in docs:
        r = Report.from_row(d)
        out.append({
            "id": r.id, "uploaded_at": r.uploaded_at, "report_date": r.report_date,
            "lab_name": r.lab_name, "patient_name": r.patient_name, "summary": r.summary,
            "health_score": r.health_score, "normal_count": r.normal_count,
            "attention_count": r.attention_count, "total_count": r.total_count,
            "filename": r.filename, "report_type": r.report_type, "title": r.title,
            "member_id": r.member_id,
        })
    return out


@router.get("/suggestions")
async def list_all_suggestions(
    member_id: str | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    docs = sort_by_effective_date(await _member_reports(current_user["user_id"], member_id), reverse=True)
    # Tips must reflect the LATEST measurements: once the newest lab report has
    # re-measured a marker, its advice supersedes anything an older report said
    # about that marker - so older suggestions linked to it are dropped, not shown
    # alongside fresh (possibly contradictory) advice.
    latest_lab = next((d for d in docs if (d.get("report_type") or "lab") == "lab"), None)
    latest_markers = {
        (b.get("name") or "").strip().lower()
        for b in (latest_lab.get("biomarkers") or [])
    } if latest_lab else set()
    latest_lab_id = str(latest_lab["id"]) if latest_lab else None
    seen: set[tuple] = set()
    out: list[dict] = []
    reports_meta: list[dict] = []
    for d in docs:
        r = Report.from_row(d)
        source_title = r.title or _default_report_title(r.report_type)
        reports_meta.append({
            "id": r.id,
            "title": source_title,
            "report_type": r.report_type,
            "report_date": r.report_date,
            "uploaded_at": r.uploaded_at,
        })
        is_latest_lab = r.id == latest_lab_id
        for s in r.suggestions or []:
            key = (s.category or "lifestyle", (s.title or "").strip().lower())
            if not key[1] or key in seen:
                continue
            linked = (s.linked_marker or "").strip().lower()
            if not is_latest_lab and linked and linked in latest_markers:
                continue  # superseded by the latest report's advice for this marker
            seen.add(key)
            out.append({
                "title": s.title,
                "body": s.body,
                "category": _normalize_category(s.category),
                "linked_marker": s.linked_marker,
                "report_id": r.id,
                "report_title": source_title,
                "report_type": r.report_type,
                "report_date": r.report_date or (r.uploaded_at[:10] if r.uploaded_at else None),
            })

    # The profile may have changed SINCE these tips were generated (diet switched,
    # allergies added) - stored advice can now contradict it.
    display_name, hp = await resolve_member_profile(current_user, _member_id_of(member_id))

    # 1) Allergy guard - deterministic safety boundary, never AI-dependent. A tip
    #    naming a declared allergen is dropped outright (word-boundary match).
    allergens = [str(a).strip().lower() for a in (hp.get("allergies") or []) if len(str(a).strip()) > 2]
    if allergens and out:
        def _mentions_allergen(t: dict) -> bool:
            text = f"{t['title']} {t['body']}".lower()
            return any(_re_search(r"\b" + _re_escape(a) + r"\b", text) for a in allergens)
        out = [t for t in out if not _mentions_allergen(t)]

    # 2) AI conflict pass - cross-report contradictions AND profile conflicts
    #    (e.g. meat advice after switching to vegetarian). Cached by a hash of the
    #    tips + profile, so any change in either produces a fresh verdict; fails
    #    open to the unfiltered list.
    if len(out) >= 2:
        ctx = build_profile_context(display_name or current_user.get("name", ""), hp)
        key = hashlib.sha256(
            (ctx + json.dumps([[t["title"], t["body"], t["report_date"]] for t in out])).encode()
        ).hexdigest()
        drop = cache.get_tips_verdict(key)
        if drop is None:
            try:
                drop = await reconcile_tips(ctx, out)
            except Exception as e:
                logger.warning(f"tips reconcile failed (serving unfiltered): {e}")
                drop = []
            cache.set_tips_verdict(key, drop)
        if drop:
            dropset = set(drop)
            out = [t for i, t in enumerate(out) if i not in dropset]

    return {"reports": reports_meta, "suggestions": out}


RETEST_AFTER_DAYS = 90


@router.get("/retest-reminders")
async def retest_reminders(
    member_id: str | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """PRO feature: markers that were out-of-range in the latest lab report and are due
    for a retest (report older than RETEST_AFTER_DAYS). Free plan → 403 upsell."""
    plan = (current_user.get("preferences") or {}).get("plan") or "free"
    if plan != "pro":
        raise HTTPException(status_code=403, detail="Retest reminders are a Pro feature. Upgrade to unlock.")
    docs = sort_by_effective_date(
        await _member_reports(current_user["user_id"], member_id, lab_only=True), reverse=True
    )
    if not docs:
        return {"due": False, "reminders": []}
    latest = docs[0]
    ed = effective_date(latest)
    try:
        from datetime import date as _date
        days_ago = (_date.today() - _date.fromisoformat(ed)).days if ed else None
    except ValueError:
        days_ago = None
    flagged = [b for b in latest.get("biomarkers", []) if b.get("status") != "normal"]
    due = bool(flagged) and days_ago is not None and days_ago >= RETEST_AFTER_DAYS
    return {
        "due": due,
        "days_since_last": days_ago,
        "last_report_date": ed,
        "reminders": [
            {"name": b.get("name"), "status": b.get("status"), "last_value": b.get("value"),
             "unit": b.get("unit"), "category": b.get("category")}
            for b in flagged
        ] if due else [],
    }


def _default_report_title(report_type: str | None) -> str:
    return {
        "lab": "Blood panel",
        "opd": "Clinic visit",
        "prescription": "Prescription",
        "imaging": "Imaging report",
        "discharge": "Discharge summary",
        "other": "Medical document",
    }.get(report_type or "lab", "Medical document")


SEVERITY = {"normal": 0, "borderline_high": 1, "borderline_low": 1, "high": 2, "low": 2}


def _to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def _report_ref(doc: dict) -> dict:
    return {
        "id": str(doc["id"]),
        "report_date": doc.get("report_date"),
        "uploaded_at": doc.get("uploaded_at"),
        "lab_name": doc.get("lab_name"),
        "health_score": doc.get("health_score", 0),
    }


@router.get("/compare")
async def compare_reports(
    a: str | None = None,
    b: str | None = None,
    against: str | None = None,
    member_id: str | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """Compare any two lab reports - both reports must belong to the given member."""
    docs = sort_by_effective_date(
        await _member_reports(current_user["user_id"], member_id, lab_only=True), reverse=True
    )
    if len(docs) < 2:
        return {"available": False, "reports_count": len(docs)}

    if a and b:
        if not _valid_id(a) or not _valid_id(b) or a == b:
            raise HTTPException(status_code=400, detail="Pick two different reports to compare.")
        docs_by_id = {str(d["id"]): d for d in docs}
        latest = docs_by_id.get(a)
        target = docs_by_id.get(b)
        if not latest or not target:
            raise HTTPException(status_code=404, detail="One of the reports isn't a lab report we can compare.")
        older_docs = [d for d in docs if str(d["id"]) != a]
    else:
        latest, older_docs = docs[0], docs[1:]
        target = None
        if against and _valid_id(against):
            target = next((d for d in older_docs if str(d["id"]) == against), None)
        if target is None:
            target = older_docs[0]

    old_map = {b.get("name", "").strip().lower(): b for b in target.get("biomarkers", [])}
    markers, counts = [], {"improved": 0, "worsened": 0, "same": 0}
    for b in latest.get("biomarkers", []):
        ob = old_map.get(b.get("name", "").strip().lower())
        if not ob:
            continue
        cur_sev = SEVERITY.get(b.get("status"), 1)
        old_sev = SEVERITY.get(ob.get("status"), 1)
        change = "improved" if cur_sev < old_sev else "worsened" if cur_sev > old_sev else "same"
        counts[change] += 1
        cv, ov = _to_float(b.get("value")), _to_float(ob.get("value"))
        markers.append({
            "name": b.get("name", "").strip(),
            "unit": b.get("unit", ""),
            "category": b.get("category", "Other"),
            "ref_range": b.get("ref_range", ""),
            "current_value": b.get("value", ""),
            "previous_value": ob.get("value", ""),
            "delta": round(cv - ov, 2) if cv is not None and ov is not None else None,
            "current_status": b.get("status", "normal"),
            "previous_status": ob.get("status", "normal"),
            "change": change,
        })
    order = {"improved": 0, "worsened": 1, "same": 2}
    markers.sort(key=lambda m: (order[m["change"]], m["name"]))

    return {
        "available": True,
        "latest": _report_ref(latest),
        "against": _report_ref(target),
        "older_reports": [_report_ref(d) for d in older_docs],
        "score_delta": latest.get("health_score", 0) - target.get("health_score", 0),
        "improved_count": counts["improved"],
        "worsened_count": counts["worsened"],
        "same_count": counts["same"],
        "markers": markers,
    }


@router.get("/{report_id}")
async def get_report(report_id: str, current_user: dict = Depends(get_current_user)):
    if not _valid_id(report_id):
        raise HTTPException(status_code=400, detail="Invalid report id")
    doc = await db.fetchrow(
        "select * from reports where id = $1::uuid and user_id = $2", report_id, current_user["user_id"]
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Report not found")
    for s in doc.get("suggestions", []) or []:
        s["category"] = _normalize_category(s.get("category"))
    out = Report.from_row(doc).model_dump()
    # Signed URLs (1h) to view the original file(s), when stored. Multi-page uploads
    # keep comma-joined paths; file_url stays the first page for older clients.
    paths = [p for p in (doc.get("storage_path") or "").split(",") if p]
    urls = [u for u in [await storage.signed_url(p) for p in paths] if u]
    out["file_url"] = urls[0] if urls else None
    out["file_urls"] = urls
    return out


@router.delete("/{report_id}")
async def delete_report(report_id: str, current_user: dict = Depends(get_current_user)):
    if not _valid_id(report_id):
        raise HTTPException(status_code=400, detail="Invalid report id")
    path = await db.fetchval(
        "delete from reports where id = $1::uuid and user_id = $2 returning coalesce(storage_path, '')",
        report_id, current_user["user_id"],
    )
    if path is None:
        raise HTTPException(status_code=404, detail="Report not found")
    cache.invalidate_reports(current_user["user_id"])
    if path:
        await storage.delete_files(path.split(","))
    return {"deleted": True}


@router.patch("/{report_id}")
async def rename_report(
    report_id: str,
    req: ReportTitleUpdate,
    current_user: dict = Depends(get_current_user),
):
    if not _valid_id(report_id):
        raise HTTPException(status_code=400, detail="Invalid report id")
    new_title = req.title.strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="Title cannot be empty.")
    row = await db.fetchrow(
        "select title from reports where id = $1::uuid and user_id = $2", report_id, current_user["user_id"]
    )
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")
    old_title = row["title"]
    await db.execute(
        "update reports set title = $1 where id = $2::uuid", new_title, report_id
    )
    await db.execute(
        """insert into report_edits (user_id, report_id, field, old_value, new_value, edited_at)
           values ($1, $2::uuid, 'title', $3, $4, $5)""",
        current_user["user_id"], report_id, {"title": old_title}, {"title": new_title},
        datetime.now(timezone.utc).isoformat(),
    )
    cache.invalidate_reports(current_user["user_id"])
    return {"id": report_id, "title": new_title}


# ---------- Spec item #4: user-editable biomarkers ----------
@router.patch("/{report_id}/biomarkers")
async def edit_biomarker(
    report_id: str,
    req: BiomarkerPatch,
    current_user: dict = Depends(get_current_user),
):
    """Correct an OCR mistake on a single biomarker. Sets user_edited=true then
    recomputes and persists health_score / normal_count / attention_count.
    Ownership-checked - you can only edit your own reports."""
    if not _valid_id(report_id):
        raise HTTPException(status_code=400, detail="Invalid report id")
    doc = await db.fetchrow(
        "select * from reports where id = $1::uuid and user_id = $2", report_id, current_user["user_id"]
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Report not found")
    biomarkers = list(doc.get("biomarkers") or [])
    if req.index < 0 or req.index >= len(biomarkers):
        raise HTTPException(status_code=400, detail="biomarker index out of range")
    if req.status is not None and req.status not in STATUS_WEIGHT:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(STATUS_WEIGHT)}")
    b = dict(biomarkers[req.index])
    old_value, old_status = b.get("value"), b.get("status")
    # First edit pins the AI-read original on the marker itself (report_edits keeps
    # the full history); later edits never overwrite it.
    if b.get("original_value") is None:
        b["original_value"] = old_value
        b["original_status"] = old_status
    if req.value is not None:
        b["value"] = str(req.value).strip()
    if req.status is not None:
        b["status"] = req.status
    b["user_edited"] = True
    # The user just looked at this value - clear any verification flag.
    b["needs_verification"] = False
    b["verification_note"] = None

    # Advice must follow the value: rewrite this marker's explanation/tip and any
    # report suggestions linked to it so they fit the corrected number. Best-effort -
    # if the AI call fails the edit still saves, advice just stays as-is.
    suggestions = list(doc.get("suggestions") or [])
    value_changed = (req.value is not None and str(req.value).strip() != str(old_value or "").strip()) or \
                    (req.status is not None and req.status != old_status)
    if value_changed:
        try:
            marker_name = str(b.get("name") or "").strip().lower()
            linked_idx = [i for i, sg in enumerate(suggestions)
                          if str(sg.get("linked_marker") or "").strip().lower() == marker_name]
            display_name, hp = await resolve_member_profile(current_user, doc.get("member_id") or "self")
            language = (hp.get("language") or "english").lower()
            fresh_advice = await refresh_marker_advice(
                build_profile_context(display_name or current_user.get("name", ""), hp),
                b, [suggestions[i] for i in linked_idx], language=language,
            )
            if fresh_advice.get("explanation"):
                b["explanation"] = str(fresh_advice["explanation"]).strip()
            if fresh_advice.get("tip"):
                b["tip"] = str(fresh_advice["tip"]).strip()
            rewritten = fresh_advice.get("suggestions") or []
            for slot, new_sg in zip(linked_idx, rewritten):
                if isinstance(new_sg, dict) and new_sg.get("body"):
                    suggestions[slot] = {**suggestions[slot],
                                         "title": str(new_sg.get("title") or suggestions[slot].get("title")).strip(),
                                         "body": str(new_sg["body"]).strip()}
        except Exception as e:
            logger.warning(f"advice refresh after biomarker edit failed (keeping old advice): {e}")

    biomarkers[req.index] = b

    normal_count = sum(1 for x in biomarkers if x.get("status") == "normal")
    attention_count = sum(1 for x in biomarkers if x.get("status") in ("high", "low"))
    # Recompute weighted health_score only for lab reports; others keep their existing score.
    health_score = compute_health_score(biomarkers) if (doc.get("report_type") or "lab") == "lab" else doc["health_score"]

    await db.execute(
        """update reports set biomarkers = $1, normal_count = $2, attention_count = $3,
           total_count = $4, health_score = $5, suggestions = $6 where id = $7::uuid""",
        biomarkers, normal_count, attention_count, len(biomarkers), health_score, suggestions, report_id,
    )
    # Audit trail: the pre-edit value survives so an admin can always see what the
    # AI showed vs what the user changed it to.
    await db.execute(
        """insert into report_edits (user_id, report_id, field, old_value, new_value, edited_at)
           values ($1, $2::uuid, $3, $4, $5, $6)""",
        current_user["user_id"], report_id, f"biomarker:{b.get('name', '')}",
        {"value": old_value, "status": old_status},
        {"value": b["value"], "status": b["status"]},
        datetime.now(timezone.utc).isoformat(),
    )
    cache.invalidate_reports(current_user["user_id"])
    fresh = await db.fetchrow("select * from reports where id = $1::uuid", report_id)
    return Report.from_row(fresh).model_dump()


# ---------- Accuracy feedback: "Is this analysis correct?" ----------
FEEDBACK_ISSUES = {"wrong_value", "missed_marker", "wrong_status", "bad_tip", "wrong_language", "other"}


@router.post("/{report_id}/feedback")
async def submit_report_feedback(
    report_id: str,
    req: ReportFeedbackIn,
    current_user: dict = Depends(get_current_user),
):
    """Record thumbs-up/down on a report (biomarker_index=null) or a single biomarker.
    One entry per (report, biomarker_index) - resubmitting overwrites."""
    if not _valid_id(report_id):
        raise HTTPException(status_code=400, detail="Invalid report id")
    doc = await db.fetchrow(
        "select biomarkers from reports where id = $1::uuid and user_id = $2",
        report_id, current_user["user_id"],
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Report not found")
    issues = [i for i in req.issues if i in FEEDBACK_ISSUES]
    if not req.correct and not issues and not req.note.strip():
        raise HTTPException(status_code=400, detail="Tell us what's wrong - pick an issue or add a note.")
    biomarker_name = None
    if req.biomarker_index is not None:
        markers = doc.get("biomarkers") or []
        if req.biomarker_index >= len(markers):
            raise HTTPException(status_code=400, detail="biomarker index out of range")
        biomarker_name = markers[req.biomarker_index].get("name")
    now = datetime.now(timezone.utc).isoformat()
    fb_id = f"{report_id}:{req.biomarker_index if req.biomarker_index is not None else 'report'}"
    await db.execute(
        """insert into report_feedback (id, user_id, report_id, biomarker_index, biomarker_name,
             correct, issues, note, created_at, updated_at)
           values ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $9)
           on conflict (id) do update set correct = $6, issues = $7, note = $8,
             biomarker_name = $5, updated_at = $9""",
        fb_id, current_user["user_id"], report_id, req.biomarker_index, biomarker_name,
        req.correct, issues, req.note.strip(), now,
    )
    return {"ok": True}


@router.get("/{report_id}/feedback")
async def get_report_feedback(report_id: str, current_user: dict = Depends(get_current_user)):
    if not _valid_id(report_id):
        raise HTTPException(status_code=400, detail="Invalid report id")
    rows = await db.fetch(
        """select report_id, biomarker_index, biomarker_name, correct, issues, note,
             created_at, updated_at
           from report_feedback where report_id = $1::uuid and user_id = $2""",
        report_id, current_user["user_id"],
    )
    return [{**r, "report_id": str(r["report_id"])} for r in rows]
