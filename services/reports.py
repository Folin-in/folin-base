"""Report CRUD, user corrections to biomarkers (with advice refresh), and accuracy
feedback. Every lookup is scoped to the caller; a report you do not own is a 404, never
a 403, so ids cannot be probed.

Corrections keep two records: the FIRST edit pins the AI-read original on the marker
itself (`original_value` / `original_status`, never overwritten), and every edit appends
a report_edits row carrying the pre-edit value AND the pre-edit advice - so an admin
can always see what the AI showed versus what the user changed it to.
"""
import logging
from datetime import datetime, timezone

from fastapi import HTTPException

from agents import AdviceRefresher
from agents.tools import TOOLS
from app.constants import FEEDBACK_ISSUES
from app.enums import BiomarkerStatus, Language, ReportType
from app.models import BiomarkerPatch, Report, ReportFeedbackIn, ReportTitleUpdate
from core import cache, storage
from db import session as db
from services.compare import valid_id
from services.gamification import STATUS_WEIGHT, compute_health_score
from services.members import member_id_of, resolve_member_profile
from services.tips import normalize_category

logger = logging.getLogger(__name__)

ATTENTION = {BiomarkerStatus.HIGH, BiomarkerStatus.LOW}
MSG_BAD_ID = "Invalid report id"
MSG_NOT_FOUND = "Report not found"
MSG_EMPTY_TITLE = "Title cannot be empty."
MSG_INDEX = "biomarker index out of range"
MSG_FEEDBACK_EMPTY = "Tell us what's wrong - pick an issue or add a note."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_id(report_id: str) -> None:
    if not valid_id(report_id):
        raise HTTPException(status_code=400, detail=MSG_BAD_ID)


class ReportService:
    def __init__(self):
        self._refresher = AdviceRefresher()
        self._profile_context = TOOLS["profile_context"]

    # ------------------------------------------------------------------ reads

    @staticmethod
    async def list_for(user_id: str, member_id: str | None) -> list[dict]:
        """Newest UPLOAD first - a just-uploaded old report should still show on top.
        Trend / compare / streaks use effective-date order instead."""
        mid = member_id_of(member_id)
        rows = await cache.get_reports(user_id)
        docs = sorted([r for r in rows if r.get("member_id", "self") == mid],
                      key=lambda r: r.get("uploaded_at") or "", reverse=True)
        out = []
        for d in docs:
            r = Report.from_row(d)
            out.append({"id": r.id, "uploaded_at": r.uploaded_at, "report_date": r.report_date,
                        "lab_name": r.lab_name, "patient_name": r.patient_name, "summary": r.summary,
                        "health_score": r.health_score, "normal_count": r.normal_count,
                        "attention_count": r.attention_count, "total_count": r.total_count,
                        "filename": r.filename, "report_type": r.report_type, "title": r.title,
                        "member_id": r.member_id})
        return out

    @staticmethod
    async def _owned(report_id: str, user_id: str, cols: str = "*") -> dict:
        _require_id(report_id)
        doc = await db.fetchrow(
            f"select {cols} from reports where id = $1::uuid and user_id = $2 and deleted_at is null",
            report_id, user_id)
        if not doc:
            raise HTTPException(status_code=404, detail=MSG_NOT_FOUND)
        return doc

    async def get(self, report_id: str, user_id: str) -> dict:
        doc = await self._owned(report_id, user_id)
        for s in doc.get("suggestions", []) or []:
            s["category"] = normalize_category(s.get("category"))
        out = Report.from_row(doc).model_dump()
        paths = [p for p in (doc.get("storage_path") or "").split(",") if p]
        urls = [u for u in [await storage.signed_url(p) for p in paths] if u]
        out["file_url"] = urls[0] if urls else None
        out["file_urls"] = urls
        return out

    # ------------------------------------------------------------------ writes

    @staticmethod
    async def soft_delete(report_id: str, user_id: str) -> None:
        """The row and its stored file survive with deleted_at set, so an accidental delete
        is recoverable by support. Account deletion still hard-deletes via FK cascade."""
        _require_id(report_id)
        marked = await db.fetchval(
            """update reports set deleted_at = $3 where id = $1::uuid and user_id = $2
               and deleted_at is null returning 1""",
            report_id, user_id, _now())
        if not marked:
            raise HTTPException(status_code=404, detail=MSG_NOT_FOUND)
        cache.invalidate_reports(user_id)

    @staticmethod
    async def rename(report_id: str, user_id: str, req: ReportTitleUpdate) -> dict:
        _require_id(report_id)
        new_title = req.title.strip()
        if not new_title:
            raise HTTPException(status_code=400, detail=MSG_EMPTY_TITLE)
        row = await db.fetchrow("select title from reports where id = $1::uuid and user_id = $2", report_id, user_id)
        if not row:
            raise HTTPException(status_code=404, detail=MSG_NOT_FOUND)
        await db.execute("update reports set title = $1 where id = $2::uuid", new_title, report_id)
        await db.execute(
            """insert into report_edits (user_id, report_id, field, old_value, new_value, edited_at)
               values ($1, $2::uuid, 'title', $3, $4, $5)""",
            user_id, report_id, {"title": row["title"]}, {"title": new_title}, _now())
        cache.invalidate_reports(user_id)
        return {"id": report_id, "title": new_title}

    async def edit_biomarker(self, report_id: str, user: dict, req: BiomarkerPatch) -> dict:
        user_id = user["user_id"]
        doc = await self._owned(report_id, user_id)
        biomarkers = list(doc.get("biomarkers") or [])
        if req.index < 0 or req.index >= len(biomarkers):
            raise HTTPException(status_code=400, detail=MSG_INDEX)
        if req.status is not None and req.status not in STATUS_WEIGHT:
            raise HTTPException(status_code=400, detail=f"status must be one of {sorted(STATUS_WEIGHT)}")

        b = dict(biomarkers[req.index])
        old = {"value": b.get("value"), "status": b.get("status"), "tip": b.get("tip"), "explanation": b.get("explanation")}
        if b.get("original_value") is None:
            b["original_value"], b["original_status"] = old["value"], old["status"]
        if req.value is not None:
            b["value"] = str(req.value).strip()
        if req.status is not None:
            b["status"] = req.status
        b["user_edited"], b["needs_verification"], b["verification_note"] = True, False, None

        suggestions = list(doc.get("suggestions") or [])
        changed = ((req.value is not None and str(req.value).strip() != str(old["value"] or "").strip())
                   or (req.status is not None and req.status != old["status"]))
        if changed:
            await self._refresh_advice(b, suggestions, doc, user, report_id)

        biomarkers[req.index] = b
        is_lab = (doc.get("report_type") or ReportType.LAB) == ReportType.LAB
        await db.execute(
            """update reports set biomarkers = $1, normal_count = $2, attention_count = $3,
               total_count = $4, health_score = $5, suggestions = $6 where id = $7::uuid""",
            biomarkers,
            sum(1 for x in biomarkers if x.get("status") == BiomarkerStatus.NORMAL),
            sum(1 for x in biomarkers if x.get("status") in ATTENTION),
            len(biomarkers),
            compute_health_score(biomarkers) if is_lab else doc["health_score"],
            suggestions, report_id)
        await db.execute(
            """insert into report_edits (user_id, report_id, field, old_value, new_value, edited_at)
               values ($1, $2::uuid, $3, $4, $5, $6)""",
            user_id, report_id, f"biomarker:{b.get('name', '')}", old,
            {"value": b["value"], "status": b["status"], "tip": b.get("tip"), "explanation": b.get("explanation")},
            _now())
        cache.invalidate_reports(user_id)
        fresh = await self._owned(report_id, user_id)
        return Report.from_row(fresh).model_dump()

    async def _refresh_advice(self, b: dict, suggestions: list[dict], doc: dict, user: dict, report_id: str) -> None:
        """Advice must follow the value. Best-effort: if the model fails, the edit still saves
        and the old advice stays."""
        try:
            name = str(b.get("name") or "").strip().lower()
            linked_idx = [i for i, sg in enumerate(suggestions)
                          if str(sg.get("linked_marker") or "").strip().lower() == name]
            display_name, hp = await resolve_member_profile(user, doc.get("member_id") or "self")
            fresh = await self._refresher.run(
                self._profile_context(display_name or user.get("name", ""), hp),
                b, [suggestions[i] for i in linked_idx],
                language=(hp.get("language") or Language.ENGLISH).lower(),
                user_id=user["user_id"], report_id=report_id)
            if fresh.get("explanation"):
                b["explanation"] = str(fresh["explanation"]).strip()
            if fresh.get("tip"):
                b["tip"] = str(fresh["tip"]).strip()
            for slot, new_sg in zip(linked_idx, fresh.get("suggestions") or []):
                if isinstance(new_sg, dict) and new_sg.get("body"):
                    suggestions[slot] = {**suggestions[slot],
                                         "title": str(new_sg.get("title") or suggestions[slot].get("title")).strip(),
                                         "body": str(new_sg["body"]).strip()}
        except Exception as e:
            logger.warning("advice refresh after biomarker edit failed (keeping old advice): %s", e)

    # ------------------------------------------------------------------ feedback

    async def submit_feedback(self, report_id: str, user_id: str, req: ReportFeedbackIn) -> None:
        """One entry per (report, biomarker_index); resubmitting overwrites."""
        doc = await self._owned(report_id, user_id, cols="biomarkers")
        issues = [i for i in req.issues if i in FEEDBACK_ISSUES]
        if not req.correct and not issues and not req.note.strip():
            raise HTTPException(status_code=400, detail=MSG_FEEDBACK_EMPTY)
        biomarker_name = None
        if req.biomarker_index is not None:
            markers = doc.get("biomarkers") or []
            if req.biomarker_index >= len(markers):
                raise HTTPException(status_code=400, detail=MSG_INDEX)
            biomarker_name = markers[req.biomarker_index].get("name")
        now = _now()
        fb_id = f"{report_id}:{req.biomarker_index if req.biomarker_index is not None else 'report'}"
        await db.execute(
            """insert into report_feedback (id, user_id, report_id, biomarker_index, biomarker_name,
                 correct, issues, note, created_at, updated_at)
               values ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $9)
               on conflict (id) do update set correct = $6, issues = $7, note = $8,
                 biomarker_name = $5, updated_at = $9""",
            fb_id, user_id, report_id, req.biomarker_index, biomarker_name, req.correct, issues,
            req.note.strip(), now)

    @staticmethod
    async def feedback(report_id: str, user_id: str) -> list[dict]:
        _require_id(report_id)
        rows = await db.fetch(
            """select report_id, biomarker_index, biomarker_name, correct, issues, note, created_at, updated_at
               from report_feedback where report_id = $1::uuid and user_id = $2""",
            report_id, user_id)
        return [{**r, "report_id": str(r["report_id"])} for r in rows]


reports = ReportService()
