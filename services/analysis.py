"""The upload -> report pipeline, as declared in prompts/analysis_pipeline.md.

Stages, each a method, run in this order and every gate can stop the run:
  decode -> profile context -> SCREEN (gate) -> TRANSCRIBE -> EXTRACT -> AUDIT (gate) -> biomarkers ->
  sanity bounds -> assemble -> persist + enqueue Pro verification -> gamification delta.

Raises AnalysisRejected for anything that is the user's file's fault (mapped to 422 by the
router, quota refunded), UnsupportedDocument for a non-medical file (400, still counts),
and lets provider faults propagate (502, refunded).
"""
import base64
import logging
import re
import os
import tempfile
import uuid
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core import cache
from core import storage
from agents import (DocumentRejected, ExtractorAgent, OutputAuditor, ScreenerAgent,
                    TranscriberAgent, VerifierAgent)
from agents.base import BadModelReply, ModelUnavailable
from agents.attachments import attachment_scope
from agents.registry import get as get_spec
from agents.tools import TOOLS
from app.constants import ALLOWED_MIME
from app.enums import BiomarkerStatus, Language, Plan, ReportType
from db import session as db
from services.gamification import STATUS_WEIGHT, compute_profile, effective_date, sort_by_effective_date
from app.models import AnalyzeFileIn, Biomarker, Report
from services.analysis_types import AnalysisRejected, AnalysisResult, InvalidUpload, UnsupportedDocument
from services.report_mapper import build_report, display_name
from services.report_notes import NOTE_POLICY_VERSION, detail_sections, select_report_notes
from services.engagement import fetch_events
from services.members import member_id_of, resolve_member_profile
from services.scoring import status_from_range

logger = logging.getLogger(__name__)


def _marker_key(name) -> str:
    """Match names across the two passes without tripping on case or punctuation."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())

MAX_DECODED_BYTES = 10 * 1024 * 1024
# BMI cut-offs are population-specific. Indians carry cardiometabolic risk at a lower BMI,
# so WHO's Asia-Pacific / ICMR thresholds put overweight at 23 and obese at 25, against the
# international 25 and 30. Using the international band told a 24.7 BMI it was "normal".
# Driven by the user's own region preference rather than assumed.
BMI_BANDS_ASIA = ((17.0, BiomarkerStatus.LOW), (18.5, BiomarkerStatus.BORDERLINE_LOW),
                  (23.0, BiomarkerStatus.NORMAL), (25.0, BiomarkerStatus.BORDERLINE_HIGH))
BMI_BANDS_INTL = ((17.0, BiomarkerStatus.LOW), (18.5, BiomarkerStatus.BORDERLINE_LOW),
                  (25.0, BiomarkerStatus.NORMAL), (30.0, BiomarkerStatus.BORDERLINE_HIGH))
BMI_REF_RANGE_ASIA = "18.5 - 22.9"
BMI_REF_RANGE_INTL = "18.5 - 24.9"
REGION_ASIA = "india"
BMI_UNIT = "kg/m²"
VERIFY_NOTE_MAX = 300
LAB_NOTES_MAX = 12                 # interpretive comments printed by the lab itself
DEFAULT_CATEGORY = "Other"

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
    transcript: list[dict] = field(default_factory=list)
    lab_notes: list[str] = field(default_factory=list)
    source_notes: list[str] = field(default_factory=list)
    recovered: int = 0
    report_type: ReportType = ReportType.OTHER
    biomarkers: list[Biomarker] = field(default_factory=list)
    analyze_trajectory: tuple[int | None, str | None] = (None, None)
    verify_trajectory_id: int | None = None
    job_id: str | None = None
    claim_started_at: str | None = None
    stage_seconds: dict[str, float] = field(default_factory=dict)
    transcription_status: str = "unavailable"
    verification_status: str = "not_requested"
    note_review: dict = field(default_factory=dict)


class AnalysisPipeline:
    """Composes the agents. Instantiated once; safe to share - agents hold no per-run state."""

    def __init__(self, *, defer_verification: bool = True):
        self.defer_verification = defer_verification
        spec = get_spec("analysis_pipeline")
        for sub in ("screener", "transcriber", "extractor", "auditor", "verifier"):
            spec.assert_may_call(subagent=sub)
        for tool in ("sanity_bounds", "name_match"):
            spec.assert_may_call(tool=tool)
        self.screener = ScreenerAgent()
        self.transcriber = TranscriberAgent()
        self.extractor = ExtractorAgent()
        self.auditor = OutputAuditor()
        self.verifier = VerifierAgent()
        self._sanity = TOOLS["sanity_bounds"]
        self._names_match = TOOLS["name_match"]

    # ------------------------------------------------------------------ public

    async def run(self, files: list[AnalyzeFileIn], user_id: str, user: dict,
                  member_id: str | None, *, job_id: str | None = None,
                  claim_started_at: str | None = None) -> AnalysisResult:
        r = _Run(user_id=user_id, user=user, files=files, member_id=member_id_of(member_id),
                 job_id=job_id, claim_started_at=claim_started_at)
        try:
            with attachment_scope():
                self._decode(r)
                await self._context(r)
                self._materialise(r)
                for name, stage in (("screen", self._screen), ("transcribe", self._transcribe),
                                    ("analyze", self._extract)):
                    started = time.monotonic()
                    await stage(r)
                    r.stage_seconds[name] = round(time.monotonic() - started, 3)
                self._audit(r)
                self._biomarkers(r)
                if self.defer_verification:
                    if (r.user.get("preferences") or {}).get("plan") == Plan.PRO and r.biomarkers:
                        r.verification_status = "pending"
                else:
                    # Explicit synchronous mode for offline evaluation, not uploads.
                    started = time.monotonic()
                    await self._verify(r)
                    r.stage_seconds["verify"] = round(time.monotonic() - started, 3)
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

    async def _transcribe(self, r: _Run) -> None:
        """Enumerate every printed result row. Fails open: a transcription that does not
        come back costs the completeness guarantee, not the whole upload."""
        try:
            parsed, _ = await self.transcriber.run(r.attachments, user_id=r.user_id)
        except Exception as e:
            logger.warning("transcriber failed (continuing without it): %s", e)
            return
        if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
            logger.warning("transcriber returned no usable result list")
            return
        rows = parsed["results"]
        r.transcript = [x for x in (rows or []) if isinstance(x, dict) and str(x.get("name") or "").strip()]
        r.transcription_status = "completed" if r.transcript else "empty"
        r.source_notes = select_report_notes(parsed.get("notes"), None).notes
        r.lab_notes = r.source_notes[:LAB_NOTES_MAX]
        logger.info("transcribed %d result rows, %d lab notes", len(r.transcript), len(r.lab_notes))

    def _reconcile_transcript(self, r: _Run) -> None:
        """Anything the transcriber saw but the extractor did not return is added back.

        This is the completeness guarantee, and it is deterministic on purpose: the status
        comes from the printed reference interval, not from a second opinion, so a model
        that skips rows can no longer lose them.
        """
        if not r.transcript or r.report_type != ReportType.LAB:
            return
        have = {_marker_key(b.get("name")) for b in (r.parsed.get("biomarkers") or [])}
        recovered = []
        for row in r.transcript:
            key = _marker_key(row.get("name"))
            if not key or key in have:
                continue
            have.add(key)                      # a page repeated in two photos must not double up
            recovered.append({
                "name": str(row.get("name")).strip(),
                "value": str(row.get("value") or "").strip(),
                "unit": str(row.get("unit") or "").strip(),
                "ref_range": str(row.get("ref_range") or "").strip(),
                "status": status_from_range(row.get("value"), row.get("ref_range")),
                "category": DEFAULT_CATEGORY,
                "explanation": "",
                "tip": "",
            })
        if recovered:
            r.parsed["biomarkers"] = list(r.parsed.get("biomarkers") or []) + recovered
            r.recovered = len(recovered)
            logger.info("recovered %d result rows the extractor omitted", r.recovered)

    async def _extract(self, r: _Run) -> None:
        try:
            r.parsed, meta = await self.extractor.run(r.attachments, r.profile_context,
                                                     language=r.language, transcript=r.transcript,
                                                     source_notes=r.source_notes,
                                                     user_id=r.user_id)
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
        self._reconcile_transcript(r)
        review = select_report_notes(r.source_notes, r.parsed.get("note_decisions"), detail_sections(r.parsed))
        r.lab_notes = review.notes[:LAB_NOTES_MAX]
        r.note_review = {"policy": NOTE_POLICY_VERSION, "status": "completed" if review.reviewed else "unavailable",
                         "decisions": review.decisions, "source_notes": r.source_notes}
        if not review.reviewed:
            logger.warning("note relevance review incomplete; preserving source notes")

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
        region = str((r.user.get("preferences") or {}).get("region") or REGION_ASIA).lower()
        bands = BMI_BANDS_ASIA if region == REGION_ASIA else BMI_BANDS_INTL
        ref_range = BMI_REF_RANGE_ASIA if region == REGION_ASIA else BMI_REF_RANGE_INTL
        status = next((s for limit, s in bands if bmi < limit), BiomarkerStatus.HIGH)
        r.biomarkers.append(Biomarker(
            name="BMI", value=str(bmi), unit=BMI_UNIT, ref_range=ref_range, status=status, category="Other",
            explanation=f"Body Mass Index, worked out from your profile ({w} kg, {h} cm). It's a rough gauge of weight for your height - not a full picture of health on its own.",
            tip="This comes from your profile, not the lab report - keep your weight and height up to date there so it stays accurate.",
        ))

    async def _verify(self, r: _Run) -> None:
        plan = (r.user.get("preferences") or {}).get("plan") or Plan.FREE
        if plan != Plan.PRO or not r.biomarkers:
            return
        r.verification_status = "unavailable"
        try:
            mismatches, meta = await self.verifier.run(r.attachments, [b.model_dump() for b in r.biomarkers],
                                                       user_id=r.user_id)
        except Exception as e:
            logger.warning("verifier failed (skipping): %s", e)
            for b in r.biomarkers:
                b.needs_verification = True
                b.verification_note = ("Automatic cross-check did not complete. Please compare this value with the original document. "
                                       + (b.verification_note or "")).strip()[:VERIFY_NOTE_MAX]
            return
        r.verification_status = "completed"
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
        report = build_report(parsed=r.parsed, report_type=r.report_type, biomarkers=r.biomarkers, files=r.files,
                            lab_notes=r.lab_notes,
                            raws=r.raws, user_id=r.user_id, member_id=r.member_id, display=r.display_name,
                            names_match=self._names_match)
        if r.report_type == ReportType.LAB and r.transcription_status != "completed":
            # This is our warning, not a note printed by the laboratory.
            report.overall_assessment = (report.overall_assessment + "\n\nAutomatic row-by-row transcription did not complete. Compare with the original report for missing results.").strip()
        return report

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
        traj_id, model = r.analyze_trajectory
        d["analysis_meta"] = {"model": model, "trajectory_id": traj_id,
                              "stage_seconds": r.stage_seconds,
                              "transcription_status": r.transcription_status,
                              "verification_status": r.verification_status,
                              "note_review": r.note_review}
        cols = list(d.keys())
        profile_after = compute_profile(r.existing + [report.model_dump()], events=events)
        before_ids = {b["id"] for b in profile_before["badges"] if b["unlocked"]}
        xp_earned = profile_after["xp"] - profile_before["xp"]
        new_badges = [b for b in profile_after["badges"] if b["unlocked"] and b["id"] not in before_ids]
        # Save the report and its terminal state together. Ownership fencing prevents
        # a worker whose lease was reclaimed from publishing a duplicate report.
        async with db.transaction() as conn:
            if r.job_id:
                state = await conn.fetchval(
                    """select status from analyze_jobs where job_id = $1 and user_id = $2
                       and ($3::text is null or started_at = $3) for update""",
                    r.job_id, r.user_id, r.claim_started_at)
                if state != "processing":
                    raise RuntimeError("Analysis job is no longer owned by this worker")
            new_id = await conn.fetchval(
                f"insert into reports ({', '.join(cols)}) values ({', '.join(f'${i + 1}' for i in range(len(cols)))}) returning id",
                *d.values())
            report.id = str(new_id)
            if r.verification_status == "pending":
                await conn.execute(
                    """insert into verification_jobs (report_id, user_id, payload)
                       values ($1::uuid, $2, $3)""",
                    report.id, r.user_id,
                    {"files": [f.model_dump(mode="json") for f in r.files],
                     "biomarkers": [b.model_dump() for b in r.biomarkers]})
            if traj_id:
                await conn.execute("update ai_trajectories set report_id = $1 where id = $2", report.id, traj_id)
            if r.verify_trajectory_id:
                await conn.execute("update ai_trajectories set report_id = $1 where id = $2",
                                   report.id, r.verify_trajectory_id)
            if r.job_id:
                completed = await conn.fetchval(
                    """update analyze_jobs set status = 'success', completed_at = $1, report_id = $2,
                       xp_earned = $3, new_badges = $4, error = null, request_payload = null
                       where job_id = $5 and status = 'processing'
                         and ($6::text is null or started_at = $6)
                       returning job_id""",
                    datetime.now(timezone.utc).isoformat(), report.id, xp_earned, new_badges,
                    r.job_id, r.claim_started_at)
                if not completed:
                    raise RuntimeError("Analysis job is no longer owned by this worker")
                await conn.execute(
                    "insert into analysis_notifications (report_id, user_id) values ($1::uuid, $2)",
                    report.id, r.user_id)
        cache.invalidate_reports(r.user_id)
        return AnalysisResult(
            report_id=report.id, report=report.model_dump(),
            xp_earned=xp_earned, new_badges=new_badges,
            profile=profile_after)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _member_reports(user_id: str, member_id: str) -> list[dict]:
        rows = await cache.get_reports(user_id)
        return [x for x in rows if x.get("member_id", "self") == member_id]


pipeline = AnalysisPipeline()
