"""Validate semantic note decisions; never classify clinical text with keywords."""
from dataclasses import dataclass

NOTE_POLICY_VERSION = "clinical-relevance-v1"
DETAIL_FIELDS = ("chief_complaint", "exam_findings", "diagnoses", "referrals", "medications",
                 "findings", "impression", "procedures", "follow_up")


def detail_sections(report: dict) -> dict:
    sections = {}
    for key in DETAIL_FIELDS:
        value = report.get(key)
        if isinstance(value, str) and value.strip():
            sections[key] = value
        elif isinstance(value, list):
            if key == "medications":
                populated = [v for v in value if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip()]
            else:
                populated = [v for v in value if isinstance(v, str) and v.strip()]
            if populated:
                sections[key] = populated
    return sections


@dataclass
class NoteReview:
    notes: list[str]
    reviewed: bool
    decisions: list[dict]


def select_report_notes(notes: object, decisions: object, sections: dict | None = None) -> NoteReview:
    """Only a complete, valid model review may hide a source note.

    Select original strings by index so the model cannot paraphrase doses or add
    instructions. Unknown/uncertain content is kept. Invalid output keeps all
    available source notes, distinct from a successful review selecting none.
    """
    raw = notes if isinstance(notes, list) else []
    sections = detail_sections(sections or {})
    source = [n.strip() for n in raw if isinstance(n, str) and n.strip()]
    if not isinstance(notes, list) or any(not isinstance(n, str) for n in raw):
        return NoteReview(source, False, [])
    if not raw:
        return NoteReview([], True, [])
    if not isinstance(decisions, list) or len(decisions) != len(raw):
        return NoteReview(source, False, [])
    by_index = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            return NoteReview(source, False, [])
        index = decision.get("index")
        category = decision.get("category")
        reason = decision.get("reason")
        if (type(index) is not int or not 0 <= index < len(raw) or index in by_index
                or category not in ("clinical", "administrative", "uncertain", "covered_elsewhere")
                or not isinstance(reason, str) or not reason.strip()):
            return NoteReview(source, False, [])
        by_index[index] = {"index": index, "category": category, "reason": reason.strip()}
        if category == "covered_elsewhere":
            covered_by = decision.get("covered_by")
            if (not isinstance(covered_by, list) or not covered_by
                    or any(not isinstance(key, str) or key not in DETAIL_FIELDS or not (sections or {}).get(key)
                           for key in covered_by)):
                return NoteReview(source, False, [])
            by_index[index]["covered_by"] = covered_by
    return NoteReview(
        [note.strip() for i, note in enumerate(raw) if note.strip() and by_index[i]["category"] not in ("administrative", "covered_elsewhere")],
        True, [by_index[i] for i in range(len(raw))],
    )
