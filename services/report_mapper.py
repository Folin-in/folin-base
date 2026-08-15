"""Maps the extractor's parsed JSON onto a Report. Pure: no I/O, no model, no state -
so it can be unit-tested against fixture JSON without a database.

Tolerant by design: the model occasionally returns a single string where a list is
expected, or drops a field. Every accessor here degrades to empty rather than raising,
because a report with one missing list is still worth storing; a crash loses it all.
"""
import hashlib
import re
from datetime import datetime, timezone

from app.constants import LEGACY_CATEGORY_MAP, SUGGESTION_CATEGORIES
from app.enums import BiomarkerStatus, ReportType
from app.models import AnalyzeFileIn, Biomarker, Medication, Report, Suggestion
from services.gamification import compute_document_score, compute_health_score

_SCORE_IN_SUMMARY = re.compile(r"\s*\(?\b\d{1,3}\s*/\s*100\b\)?")
_ATTENTION = {BiomarkerStatus.HIGH, BiomarkerStatus.LOW}
DEFAULT_CATEGORY = "lifestyle"


def normalize_category(cat: str | None) -> str:
    c = str(cat or "").strip().lower()
    c = LEGACY_CATEGORY_MAP.get(c, c)
    return c if c in SUGGESTION_CATEGORIES else DEFAULT_CATEGORY


def display_name(files: list[AnalyzeFileIn]) -> str:
    return files[0].filename if len(files) == 1 else f"{files[0].filename} (+{len(files) - 1} pages)"


def str_list(p: dict, key: str) -> list[str]:
    v = p.get(key) or []
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in v if str(x).strip()]


def opt_str(p: dict, key: str) -> str | None:
    return str(p[key]).strip() if p.get(key) else None


def suggestions_from(p: dict) -> list[Suggestion]:
    out = []
    for s in p.get("suggestions", []) or []:
        try:
            linked = s.get("linked_marker")
            out.append(Suggestion(title=str(s.get("title", "")).strip(), body=str(s.get("body", "")).strip(),
                                  category=normalize_category(s.get("category")),
                                  linked_marker=str(linked).strip() if linked else None))
        except Exception:
            continue
    return out


def medications_from(p: dict) -> list[Medication]:
    out = []
    for m in p.get("medications") or []:
        try:
            name = str(m.get("name") or "").strip()
            if name:
                out.append(Medication(name=name, dose=opt_str(m, "dose"), schedule=opt_str(m, "schedule"),
                                      duration=opt_str(m, "duration"), purpose=opt_str(m, "purpose")))
        except Exception:
            continue
    return out


def build_report(*, parsed: dict, report_type: ReportType, biomarkers: list[Biomarker], files: list[AnalyzeFileIn],
                 raws: list[bytes], user_id: str, member_id: str, display: str, names_match) -> Report:
    p = parsed
    bio = [b.model_dump() for b in biomarkers]
    if report_type == ReportType.LAB and bio:
        score = compute_health_score(bio)
    else:
        score = compute_document_score(str_list(p, "positives"), str_list(p, "concerns"))
    patient_name = p.get("patient_name")
    provider = p.get("lab_name") or p.get("provider")
    return Report(
        user_id=user_id, member_id=member_id, filename=display_name(files), mime_type=files[0].mime_type,
        storage_path=None, file_hash=hashlib.sha256(b"".join(raws)).hexdigest(),
        uploaded_at=datetime.now(timezone.utc).isoformat(),
        report_type=report_type, report_date=p.get("report_date"),
        patient_name=str(patient_name).strip() if patient_name else None,
        name_mismatch=not names_match(display, str(patient_name) if patient_name else None),
        lab_name=str(provider).strip() if provider else None,
        summary=_SCORE_IN_SUMMARY.sub("", str(p.get("summary") or "")).strip(),
        overall_assessment=str(p.get("overall_assessment") or ""),
        positives=str_list(p, "positives"), concerns=str_list(p, "concerns"),
        suggestions=suggestions_from(p), biomarkers=biomarkers, health_score=score,
        normal_count=sum(1 for b in bio if b["status"] == BiomarkerStatus.NORMAL),
        attention_count=sum(1 for b in bio if b["status"] in _ATTENTION),
        total_count=len(biomarkers),
        chief_complaint=opt_str(p, "chief_complaint"), exam_findings=str_list(p, "exam_findings"),
        diagnoses=str_list(p, "diagnoses"), referrals=str_list(p, "referrals"), medications=medications_from(p),
        modality=opt_str(p, "modality"), body_area=opt_str(p, "body_area"), findings=str_list(p, "findings"),
        impression=opt_str(p, "impression"), admission_date=p.get("admission_date"),
        discharge_date=p.get("discharge_date"), procedures=str_list(p, "procedures"), follow_up=opt_str(p, "follow_up"),
    )
