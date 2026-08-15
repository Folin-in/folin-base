"""The upload -> report pipeline, as declared in prompts/analysis_pipeline.md.

Stages, each a method, run in this order and every gate can stop the run:
  decode -> profile context -> SCREEN (gate) -> EXTRACT -> AUDIT (gate) -> biomarkers ->
  sanity bounds -> (Pro) VERIFY -> assemble -> persist -> gamification delta.

Raises AnalysisRejected for anything that is the user's file's fault (mapped to 422 by the
router, quota refunded), UnsupportedDocument for a non-medical file (400, still counts),
and lets provider faults propagate (502, refunded).
"""
import base64
import logging
import os
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
from app.constants import ALLOWED_MIME
from app.enums import BiomarkerStatus, Language, Plan, ReportType
from db import session as db
from services.gamification import STATUS_WEIGHT, compute_profile, effective_date, sort_by_effective_date
from app.models import AnalyzeFileIn, Biomarker, Report
from services.analysis_types import AnalysisRejected, AnalysisResult, InvalidUpload, UnsupportedDocument
from services.report_mapper import build_report, display_name
from services.engagement import fetch_events
from services.members import member_id_of, resolve_member_profile

logger = logging.getLogger(__name__)

MAX_DECODED_BYTES = 10 * 1024 * 1024
BMI_BANDS = ((17.0, BiomarkerStatus.LOW), (18.5, BiomarkerStatus.BORDERLINE_LOW),
             (25.0, BiomarkerStatus.NORMAL), (30.0, BiomarkerStatus.BORDERLINE_HIGH))
BMI_REF_RANGE = "18.5 - 24.9"
BMI_UNIT = "kg/m²"
VERIFY_NOTE_MAX = 300

MSG_INVALID_FILE = "Invalid file data."
MSG_TOO_LARGE = "Files too large (max 10 MB total). Try photos of the report instead of scans, or compress the PDF."
MSG_NOT_MEDICAL = "This doesn't look like a medical document. Try a different file."
MSG_UNREADABLE = "The AI couldn't read this report. Please try again or use a clearer file."
MSG_PROVIDER = "AI analysis failed. Please try again in a moment."
MSG_PAGES_MIXED = "These photos don't look like pages of the same report. "
MSG_PAGES_HOW = "Upload one report at a time, with all of its pages together."


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
        return build_report(parsed=r.parsed, report_type=r.report_type, biomarkers=r.biomarkers, files=r.files,
                            raws=r.raws, user_id=r.user_id, member_id=r.member_id, display=r.display_name,
                            names_match=self._names_match)

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


pipeline = AnalysisPipeline()
