"""The upload -> report pipeline, as declared in prompts/analysis_pipeline.md.

Stages, each a method, run in this order and every gate can stop the run:
  decode -> profile context -> SCREEN (gate) -> EXTRACT -> AUDIT (gate) -> biomarkers ->
  sanity bounds -> (Pro) VERIFY -> assemble -> persist -> gamification delta.

Raises AnalysisRejected for anything that is the user's file's fault (mapped to 422 by the
router, quota refunded), UnsupportedDocument for a non-medical file (400, still counts),
and lets provider faults propagate (502, refunded).
"""
import base64
import hashlib
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core import cache
from core import storage
from agents import DocumentRejected, ExtractorAgent, OutputAuditor, ScreenerAgent, VerifierAgent
from agents.base import BadModelReply, ModelUnavailable
from agents.registry import get as get_spec
from agents.tools import TOOLS
from app.constants import ALLOWED_MIME, LEGACY_CATEGORY_MAP, SUGGESTION_CATEGORIES
from app.enums import BiomarkerStatus, Language, Plan, ReportType
from db import session as db
from services.gamification import (STATUS_WEIGHT, compute_document_score, compute_health_score, compute_profile,
                          effective_date, sort_by_effective_date)
from app.models import AnalyzeFileIn, Biomarker, Medication, Report, Suggestion
from services.engagement import fetch_events
from services.members import member_id_of, resolve_member_profile

logger = logging.getLogger(__name__)

MAX_DECODED_BYTES = 10 * 1024 * 1024
BMI_BANDS = ((17.0, BiomarkerStatus.LOW), (18.5, BiomarkerStatus.BORDERLINE_LOW),
             (25.0, BiomarkerStatus.NORMAL), (30.0, BiomarkerStatus.BORDERLINE_HIGH))
BMI_REF_RANGE = "18.5 - 24.9"
BMI_UNIT = "kg/m²"
VERIFY_NOTE_MAX = 300
_SCORE_IN_SUMMARY = re.compile(r"\s*\(?\b\d{1,3}\s*/\s*100\b\)?")
_ATTENTION = {BiomarkerStatus.HIGH, BiomarkerStatus.LOW}

MSG_INVALID_FILE = "Invalid file data."
MSG_TOO_LARGE = "Files too large (max 10 MB total). Try photos of the report instead of scans, or compress the PDF."
MSG_NOT_MEDICAL = "This doesn't look like a medical document. Try a different file."
MSG_UNREADABLE = "The AI couldn't read this report. Please try again or use a clearer file."
MSG_PROVIDER = "AI analysis failed. Please try again in a moment."
MSG_PAGES_MIXED = "These photos don't look like pages of the same report. "
MSG_PAGES_HOW = "Upload one report at a time, with all of its pages together."


class AnalysisRejected(Exception):
    """The user's file is the problem (mixed pages, injection, disguise). 422, refund."""
    def __init__(self, user_message: str, reason: str = "", evidence: str = ""):
        super().__init__(reason or user_message)
        self.user_message, self.reason, self.evidence = user_message, reason, evidence


class UnsupportedDocument(Exception):
    """Not a medical document. 400 - the analysis was done, so it still counts."""
    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


class InvalidUpload(Exception):
    """Bad or oversized bytes. `status` is the HTTP code the router should use."""
    def __init__(self, user_message: str, status: int = 400):
        super().__init__(user_message)
        self.user_message, self.status = user_message, status


@dataclass
class AnalysisResult:
    report_id: str
    report: dict
    xp_earned: int
    new_badges: list[dict]
    profile: dict


@dataclass
class _Run:
    """Per-upload working state, so stages pass one object instead of a dozen locals."""
    user_id: str
    user: dict
    files: list[AnalyzeFileIn]
    member_id: str
    raws: list[bytes] = field(default_factory=list)
    attachments: list[tuple[str, str]] = field(default_factory=list)
    tmp_paths: list[str] = field(default_factory=list)
    display_name: str = ""
    hp: dict = field(default_factory=dict)
    language: str = Language.ENGLISH
    profile_context: str = ""
    existing: list[dict] = field(default_factory=list)
    parsed: dict = field(default_factory=dict)
    report_type: ReportType = ReportType.OTHER
    biomarkers: list[Biomarker] = field(default_factory=list)
    analyze_trajectory: tuple[int | None, str | None] = (None, None)
    verify_trajectory_id: int | None = None


class AnalysisPipeline:
    """Composes the agents. Instantiated once; safe to share - agents hold no per-run state."""

    def __init__(self):
        spec = get_spec("analysis_pipeline")
        for sub in ("screener", "extractor", "auditor", "verifier"):
            spec.assert_may_call(subagent=sub)
        for tool in ("sanity_bounds", "name_match"):
            spec.assert_may_call(tool=tool)
        self.screener = ScreenerAgent()
        self.extractor = ExtractorAgent()
        self.auditor = OutputAuditor()
        self.verifier = VerifierAgent()
        self._sanity = TOOLS["sanity_bounds"]
        self._names_match = TOOLS["name_match"]

    # ------------------------------------------------------------------ public

    async def run(self, files: list[AnalyzeFileIn], user_id: str, user: dict,
                  member_id: str | None) -> AnalysisResult:
        r = _Run(user_id=user_id, user=user, files=files, member_id=member_id_of(member_id))
        try:
            self._decode(r)
            await self._context(r)
            self._materialise(r)
            await self._screen(r)
            await self._extract(r)
            self._audit(r)
            self._biomarkers(r)
            await self._verify(r)
            report = self._assemble(r)
            return await self._persist(r, report)
        finally:
            for p in r.tmp_paths:
                if os.path.exists(p):
                    os.unlink(p)

    # ------------------------------------------------------------------ stages

    def _decode(self, r: _Run) -> None:
        for f in r.files:
            try:
                r.raws.append(base64.b64decode(f.file_base64, validate=False))
            except Exception:
                raise InvalidUpload(MSG_INVALID_FILE)
        if sum(len(b) for b in r.raws) > MAX_DECODED_BYTES:
            raise InvalidUpload(MSG_TOO_LARGE, status=413)

    async def _context(self, r: _Run) -> None:
        r.display_name, r.hp = await resolve_member_profile(r.user, r.member_id)
        r.language = (r.hp.get("language") or Language.ENGLISH).lower()
        r.profile_context = self.extractor.profile_context(r.display_name or r.user.get("name", ""), r.hp)
        r.existing = sort_by_effective_date(await self._member_reports(r.user_id, r.member_id))
        if r.existing:
            prev = r.existing[-1]
            prev_date = effective_date(prev) or prev.get("uploaded_at", "")[:10]
            flagged = [f"{b.get('name')}: {b.get('value')} {b.get('unit', '')} ({b.get('status')})"
                       for b in prev.get("biomarkers", []) if b.get("status") != BiomarkerStatus.NORMAL]
            r.profile_context += (
                f" PREVIOUS REPORT ({prev_date}, score {prev.get('health_score')}): "
                + ("flagged markers were " + "; ".join(flagged) if flagged else "all markers were in range")
                + ". Compare new values against these where the same marker appears and explicitly mention improvements or declines."
            )

    def _materialise(self, r: _Run) -> None:
        for f, raw in zip(r.files, r.raws):
            with tempfile.NamedTemporaryFile(suffix=ALLOWED_MIME[f.mime_type], delete=False) as tmp:
                tmp.write(raw)
                r.tmp_paths.append(tmp.name)
                r.attachments.append((tmp.name, f.mime_type))

    async def _screen(self, r: _Run) -> None:
        try:
            await self.screener.run(r.attachments, user_id=r.user_id)
        except DocumentRejected as rej:
            logger.warning("rejected at screen: %s :: %s", rej.reason, rej.evidence)
            raise AnalysisRejected(rej.user_message, rej.reason, rej.evidence)

    async def _extract(self, r: _Run) -> None:
        try:
            r.parsed, meta = await self.extractor.run(r.attachments, r.profile_context,
                                                     language=r.language, user_id=r.user_id)
        except BadModelReply:
            raise UnsupportedDocument(MSG_UNREADABLE)
        if meta:
            r.analyze_trajectory = (meta.trajectory_id, meta.model)
        rt = str(r.parsed.get("report_type") or "").strip().lower()
        if not rt and r.parsed.get("is_blood_report"):
            rt = ReportType.LAB
        if rt == ReportType.UNKNOWN:
            raise UnsupportedDocument(MSG_NOT_MEDICAL)
        if len(r.files) > 1 and r.parsed.get("pages_coherent") is False:
            issue = str(r.parsed.get("pages_issue") or "").strip()
            raise AnalysisRejected(MSG_PAGES_MIXED + (issue + " " if issue else "") + MSG_PAGES_HOW,
                                   "pages_incoherent", issue)
        try:
            r.report_type = ReportType(rt)
        except ValueError:
            r.report_type = ReportType.LAB if r.parsed.get("biomarkers") else ReportType.OTHER

    def _audit(self, r: _Run) -> None:
        try:
            self.auditor.run(r.parsed)
        except DocumentRejected as rej:
            logger.warning("rejected at audit: %s :: %s", rej.reason, rej.evidence)
            raise AnalysisRejected(rej.user_message, rej.reason, rej.evidence)

    def _biomarkers(self, r: _Run) -> None:
        for b in r.parsed.get("biomarkers", []) or []:
            try:
                r.biomarkers.append(Biomarker(
                    name=str(b.get("name", "")).strip(),
                    value=str(b.get("value", "")).strip(),
                    unit=str(b.get("unit") or "").strip(),
                    ref_range=str(b.get("ref_range") or "").strip(),
                    status=b.get("status") if b.get("status") in STATUS_WEIGHT else BiomarkerStatus.NORMAL,
                    category=str(b.get("category") or "Other").strip(),
                    explanation=str(b.get("explanation") or "").strip(),
                    tip=str(b.get("tip") or "").strip(),
                ))
            except Exception:
                continue
        for b in r.biomarkers:
            if note := self._sanity(b.model_dump()):
                b.needs_verification, b.verification_note = True, note
        self._add_bmi(r)

    def _add_bmi(self, r: _Run) -> None:
        """BMI from the profile as a first-class marker. Members carry no weight/height, so
        this is naturally self-only."""
        w, h = r.hp.get("weight_kg"), r.hp.get("height_cm")
        if r.report_type != ReportType.LAB or not (w and h):
            return
        if any(b.name.strip().lower() == "bmi" for b in r.biomarkers):
            return
        bmi = round(w / ((h / 100) ** 2), 1)
        status = next((s for limit, s in BMI_BANDS if bmi < limit), BiomarkerStatus.HIGH)
        r.biomarkers.append(Biomarker(
            name="BMI", value=str(bmi), unit=BMI_UNIT, ref_range=BMI_REF_RANGE, status=status, category="Other",
            explanation=f"Body Mass Index, worked out from your profile ({w} kg, {h} cm). It's a rough gauge of weight for your height - not a full picture of health on its own.",
            tip="This comes from your profile, not the lab report - keep your weight and height up to date there so it stays accurate.",
        ))

    async def _verify(self, r: _Run) -> None:
        plan = (r.user.get("preferences") or {}).get("plan") or Plan.FREE
        if plan != Plan.PRO or not r.biomarkers:
            return
        try:
            mismatches, meta = await self.verifier.run(r.attachments, [b.model_dump() for b in r.biomarkers],
                                                       user_id=r.user_id)
        except Exception as e:
            logger.warning("verifier failed (skipping): %s", e)
            return
        if meta:
            r.verify_trajectory_id = meta.trajectory_id
        by_name = {str(m.get("name", "")).strip().lower(): m for m in mismatches}
        for b in r.biomarkers:
            if m := by_name.get(b.name.strip().lower()):
                issue = str(m.get("issue") or "").strip()
                expected = str(m.get("expected") or "").strip()
                b.needs_verification = True
                b.verification_note = ((issue or "This value may not match the document - please double-check.")
                                       + (f" (Document shows: {expected})" if expected else ""))[:VERIFY_NOTE_MAX]

    def _assemble(self, r: _Run) -> Report:
        p = r.parsed
        suggestions = []
        for s in p.get("suggestions", []) or []:
            try:
                linked = s.get("linked_marker")
                suggestions.append(Suggestion(
                    title=str(s.get("title", "")).strip(), body=str(s.get("body", "")).strip(),
                    category=self._normalize_category(s.get("category")),
                    linked_marker=str(linked).strip() if linked else None))
            except Exception:
                continue
        medications = []
        for m in p.get("medications") or []:
            try:
                name = str(m.get("name") or "").strip()
                if name:
                    medications.append(Medication(
                        name=name,
                        dose=str(m["dose"]).strip() if m.get("dose") else None,
                        schedule=str(m["schedule"]).strip() if m.get("schedule") else None,
                        duration=str(m["duration"]).strip() if m.get("duration") else None,
                        purpose=str(m["purpose"]).strip() if m.get("purpose") else None))
            except Exception:
                continue

        bio = [b.model_dump() for b in r.biomarkers]
        if r.report_type == ReportType.LAB and bio:
            score = compute_health_score(bio)
        else:
            score = compute_document_score(self._str_list(p, "positives"), self._str_list(p, "concerns"))

        patient_name = p.get("patient_name")
        provider = p.get("lab_name") or p.get("provider")
        return Report(
            user_id=r.user_id, member_id=r.member_id,
            filename=self._display_name(r.files), mime_type=r.files[0].mime_type,
            storage_path=None, file_hash=hashlib.sha256(b"".join(r.raws)).hexdigest(),
            uploaded_at=datetime.now(timezone.utc).isoformat(),
            report_type=r.report_type, report_date=p.get("report_date"),
            patient_name=str(patient_name).strip() if patient_name else None,
            name_mismatch=not self._names_match(r.display_name, str(patient_name) if patient_name else None),
            lab_name=str(provider).strip() if provider else None,
            summary=_SCORE_IN_SUMMARY.sub("", str(p.get("summary") or "")).strip(),
            overall_assessment=str(p.get("overall_assessment") or ""),
            positives=self._str_list(p, "positives"), concerns=self._str_list(p, "concerns"),
            suggestions=suggestions, biomarkers=r.biomarkers, health_score=score,
            normal_count=sum(1 for b in bio if b["status"] == BiomarkerStatus.NORMAL),
            attention_count=sum(1 for b in bio if b["status"] in _ATTENTION),
            total_count=len(r.biomarkers),
            chief_complaint=self._opt_str(p, "chief_complaint"),
            exam_findings=self._str_list(p, "exam_findings"), diagnoses=self._str_list(p, "diagnoses"),
            referrals=self._str_list(p, "referrals"), medications=medications,
            modality=self._opt_str(p, "modality"), body_area=self._opt_str(p, "body_area"),
            findings=self._str_list(p, "findings"), impression=self._opt_str(p, "impression"),
            admission_date=p.get("admission_date"), discharge_date=p.get("discharge_date"),
            procedures=self._str_list(p, "procedures"), follow_up=self._opt_str(p, "follow_up"),
        )

    async def _persist(self, r: _Run, report: Report) -> AnalysisResult:
        stored = []
        for f, raw in zip(r.files, r.raws):
            path = f"{r.user_id}/{uuid.uuid4().hex}{ALLOWED_MIME[f.mime_type]}"
            if await storage.upload_report_file(path, raw, f.mime_type):
                stored.append(path)
        report.storage_path = ",".join(stored) or None

        events = await fetch_events(r.user_id)
        profile_before = compute_profile(r.existing, events=events)

        d = report.model_dump(exclude={"id"})
        cols = list(d.keys())
        new_id = await db.fetchval(
            f"insert into reports ({', '.join(cols)}) values ({', '.join(f'${i + 1}' for i in range(len(cols)))}) returning id",
            *d.values())
        report.id = str(new_id)

        traj_id, model = r.analyze_trajectory
        if traj_id:
            await db.execute("update reports set analysis_meta = $1 where id = $2::uuid",
                             {"model": model, "trajectory_id": traj_id}, report.id)
            await db.execute("update ai_trajectories set report_id = $1 where id = $2", report.id, traj_id)
        if r.verify_trajectory_id:
            await db.execute("update ai_trajectories set report_id = $1 where id = $2",
                             report.id, r.verify_trajectory_id)
        cache.invalidate_reports(r.user_id)

        profile_after = compute_profile(r.existing + [report.model_dump()], events=events)
        before_ids = {b["id"] for b in profile_before["badges"] if b["unlocked"]}
        return AnalysisResult(
            report_id=report.id, report=report.model_dump(),
            xp_earned=profile_after["xp"] - profile_before["xp"],
            new_badges=[b for b in profile_after["badges"] if b["unlocked"] and b["id"] not in before_ids],
            profile=profile_after)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _member_reports(user_id: str, member_id: str) -> list[dict]:
        rows = await cache.get_reports(user_id)
        return [x for x in rows if x.get("member_id", "self") == member_id]

    @staticmethod
    def _normalize_category(cat: str | None) -> str:
        c = str(cat or "").strip().lower()
        c = LEGACY_CATEGORY_MAP.get(c, c)
        return c if c in SUGGESTION_CATEGORIES else "lifestyle"

    @staticmethod
    def _display_name(files: list[AnalyzeFileIn]) -> str:
        return files[0].filename if len(files) == 1 else f"{files[0].filename} (+{len(files) - 1} pages)"

    @staticmethod
    def _str_list(p: dict, key: str) -> list[str]:
        v = p.get(key) or []
        if isinstance(v, str):
            v = [v]
        return [str(x).strip() for x in v if str(x).strip()]

    @staticmethod
    def _opt_str(p: dict, key: str) -> str | None:
        return str(p[key]).strip() if p.get(key) else None


pipeline = AnalysisPipeline()
