import asyncio
from unittest.mock import AsyncMock

import pytest

from services.report_notes import select_report_notes


def decision(index, category, **extra):
    return {"index": index, "category": category, "reason": "Reviewed role in source", **extra}


def test_selection_uses_model_categories_not_words():
    # Deliberately arbitrary strings: the validator must not contain phrase rules.
    notes = ["First source passage", "Second source passage", "Third source passage"]
    review = select_report_notes(notes, [decision(0, "administrative"), decision(1, "clinical"), decision(2, "uncertain")])
    assert review.reviewed and review.notes == notes[1:]
    assert select_report_notes(notes, [decision(i, "administrative") for i in range(3)]).notes == []


@pytest.mark.parametrize("note", [
    "Correlate clinically.", "Repeat sample: haemolysed specimen.",
    "Not diagnostic in isolation.", "Doctor's Note: rule out iron deficiency.",
    "TREATMENT PLAN: Adv:- Tab. Etoshine MR (after food) 1-0-1 X 4 days",
    "दोबारा जाँच कराएं",
    "NOT VALID FOR MEDICOLEGAL PURPOSES. Repeat sample tomorrow.",
])
def test_clinical_or_ambiguous_content_is_preserved_verbatim(note):
    assert select_report_notes([note], [decision(0, "clinical")]).notes == [note]
    assert select_report_notes([note], [decision(0, "uncertain")]).notes == [note]


@pytest.mark.parametrize("decisions", [None, [], {}, [None], [decision(0, "unknown")],
    [decision(True, "administrative")], [decision(-1, "administrative")], [decision(1, "administrative")],
    [{"index": 0, "category": "administrative"}], [decision(0, "clinical"), decision(0, "administrative")]])
def test_invalid_review_keeps_notes_instead_of_silently_hiding_them(decisions):
    review = select_report_notes(["Source note"], decisions)
    assert not review.reviewed and review.notes == ["Source note"]


def test_duplicate_coverage_requires_real_structured_fields():
    notes = ["Pain in left jaw", "Additional caution"]
    decisions = [decision(0, "covered_elsewhere", covered_by=["chief_complaint"]), decision(1, "clinical")]
    review = select_report_notes(notes, decisions, {"chief_complaint": "Left jaw pain"})
    assert review.reviewed and review.notes == ["Additional caution"]
    for sections in ({}, {"chief_complaint": ""}, {"chief_complaint": "   "}, {"chief_complaint": [None]}, {"summary": "Left jaw pain"}):
        review = select_report_notes(notes, decisions, sections)
        assert not review.reviewed and review.notes == notes


def test_covered_note_cannot_point_to_a_malformed_medication():
    review = select_report_notes(["Treatment"], [decision(0, "covered_elsewhere", covered_by=["medications"])],
                                 {"medications": [{"name": "", "schedule": "daily"}]})
    assert not review.reviewed and review.notes == ["Treatment"]


def test_empty_valid_review_and_malformed_source():
    assert select_report_notes([], []).reviewed
    review = select_report_notes([None, "Keep"], [])
    assert not review.reviewed and review.notes == ["Keep"]


def test_existing_extraction_call_selects_notes_after_structured_details():
    from services import analysis as a
    pipeline = object.__new__(a.AnalysisPipeline)
    row = {"name": "Hb", "value": "12.5", "unit": "g/dL"}
    clinical = [f"Clinical finding {i}" for i in range(a.LAB_NOTES_MAX + 1)]
    pipeline.transcriber = AsyncMock()
    notes = ["Administrative passage"] * 50 + clinical
    pipeline.transcriber.run.return_value = ({"results": [row], "notes": notes}, None)
    pipeline.extractor = AsyncMock()
    pipeline.extractor.run.return_value = ({"report_type": "opd", "note_decisions": [
        decision(i, "administrative" if i < 50 else "clinical") for i in range(len(notes))]}, None)
    run = a._Run(user_id="test", user={}, files=[], member_id="self")
    asyncio.get_event_loop().run_until_complete(pipeline._transcribe(run))
    assert run.source_notes == notes
    asyncio.get_event_loop().run_until_complete(pipeline._extract(run))
    assert run.lab_notes == clinical[:a.LAB_NOTES_MAX]
    assert run.transcript == [row]
    assert run.note_review["status"] == "completed"
    assert pipeline.extractor.run.call_args.kwargs["source_notes"] == notes


def test_saved_note_reviewer_uses_same_policy_and_rejects_partial_output():
    from agents.note_reviewer import NoteReviewer
    from agents.base import BadModelReply
    client = AsyncMock()
    client.call.return_value = {"note_decisions": [decision(0, "covered_elsewhere", covered_by=["exam_findings"])]}
    reviewer = NoteReviewer(client)
    review = asyncio.get_event_loop().run_until_complete(reviewer.run(["Printed exam"], {"exam_findings": ["Exam"]}, user_id="test", report_id="test"))
    assert review.notes == []
    assert "Source-note relevance policy" in client.call.call_args.kwargs["system_prompt"]
    client.call.return_value = {"note_decisions": []}
    with pytest.raises(BadModelReply):
        asyncio.get_event_loop().run_until_complete(reviewer.run(["Keep"], {}, user_id="test", report_id="test"))
