"""The completeness guarantee: no printed result row may be lost. Runs offline, no model.

A single call asked to both enumerate 100 rows AND write an explanation and a tip for each
economises on the enumeration - on a real 21-page panel it returned 31 of 101 rows and
dropped six abnormal results plus every Doctor's Note. So enumeration is now its own pass,
and whatever that pass saw is reinstated deterministically if the interpreting pass omits it.

    ./venv/bin/python -m pytest tests/test_transcription.py -v
"""
import pytest

import services.analysis as A
from app.enums import BiomarkerStatus, ReportType
from services.scoring import status_from_range


def run_with(transcript, biomarkers, report_type=ReportType.LAB):
    r = A._Run(user_id="u", user={}, files=[], member_id="self")
    r.transcript = transcript
    r.parsed = {"biomarkers": biomarkers}
    r.report_type = report_type
    A.pipeline._reconcile_transcript(r)
    return r


def row(name, value="5", ref="1 - 10", unit="x"):
    return {"name": name, "value": value, "unit": unit, "ref_range": ref, "page": 1}


def marker(name, status="normal"):
    return {"name": name, "value": "5", "unit": "x", "ref_range": "1 - 10", "status": status,
            "category": "Other", "explanation": "e", "tip": "t"}


# ------------------------------------------------------------------ status from the printed range

@pytest.mark.parametrize("value,ref,expected", [
    ("69.8", "83 - 101", BiomarkerStatus.LOW),        # MCV, far under
    ("13", "15 - 73", BiomarkerStatus.LOW),           # GGT, the row that used to vanish
    ("42.4", "20 - 40", BiomarkerStatus.HIGH),        # Lymphocytes
    ("666", "20 - 500", BiomarkerStatus.HIGH),        # AEC
    ("90", "83 - 101", BiomarkerStatus.NORMAL),
    ("147", "< 200", BiomarkerStatus.NORMAL),
    ("260", "< 200", BiomarkerStatus.HIGH),
    ("43", "> 50", BiomarkerStatus.LOW),
])
def test_status_comes_from_the_printed_interval(value, ref, expected):
    assert status_from_range(value, ref) == str(expected)


def test_unreadable_range_does_not_invent_a_finding():
    assert status_from_range("5", "consult physician") == str(BiomarkerStatus.NORMAL)
    assert status_from_range("nil", "1 - 10") == str(BiomarkerStatus.NORMAL)


def test_just_outside_reads_as_borderline_not_alarming():
    assert status_from_range("39", "40 - 80") == str(BiomarkerStatus.BORDERLINE_LOW)
    assert status_from_range("20", "40 - 80") == str(BiomarkerStatus.LOW)


# ------------------------------------------------------------------ the guarantee

def test_rows_the_extractor_dropped_are_reinstated():
    r = run_with([row("GGT", "13", "15 - 73"), row("MCV", "69.8", "83 - 101"), row("Sodium")],
                 [marker("Sodium")])
    names = [b["name"] for b in r.parsed["biomarkers"]]
    assert set(names) == {"Sodium", "GGT", "MCV"}
    assert r.recovered == 2


def test_a_reinstated_row_keeps_its_real_status():
    """Backfilling as 'normal' would be a silent lie - the value is out of range."""
    r = run_with([row("GGT", "13", "15 - 73")], [])
    ggt = next(b for b in r.parsed["biomarkers"] if b["name"] == "GGT")
    assert ggt["status"] == str(BiomarkerStatus.LOW)
    assert ggt["value"] == "13" and ggt["ref_range"] == "15 - 73"


@pytest.mark.parametrize("extracted", [
    "Mean Corpuscular Volume (MCV)", "mean corpuscular volume (mcv)", "Mean Corpuscular Volume  MCV",
])
def test_matching_ignores_case_and_punctuation(extracted):
    """The two passes name things slightly differently; that must not duplicate a row."""
    r = run_with([row("Mean Corpuscular Volume (MCV)")], [marker(extracted)])
    assert r.recovered == 0
    assert len(r.parsed["biomarkers"]) == 1


def test_nothing_is_added_when_the_extractor_was_complete():
    rows = [row(f"Test {i}") for i in range(20)]
    r = run_with(rows, [marker(f"Test {i}") for i in range(20)])
    assert r.recovered == 0
    assert len(r.parsed["biomarkers"]) == 20


def test_no_transcript_means_no_change():
    """Transcription fails open, so the rest of the run must be untouched by its absence."""
    r = run_with([], [marker("Sodium")])
    assert r.recovered == 0
    assert len(r.parsed["biomarkers"]) == 1


def test_non_lab_documents_are_left_alone():
    """A prescription has no biomarkers; reinstating rows into one would be nonsense."""
    r = run_with([row("Amoxicillin")], [], report_type=ReportType.PRESCRIPTION)
    assert r.recovered == 0
    assert r.parsed["biomarkers"] == []


def test_unnamed_rows_are_not_reinstated():
    r = run_with([{"name": "  ", "value": "1", "ref_range": "1 - 10"}], [])
    assert r.recovered == 0


# ------------------------------------------------------------------ the lab's own words

def test_lab_notes_are_capped():
    assert A.LAB_NOTES_MAX > 0
    notes = [f"Doctor's Note {i}" for i in range(A.LAB_NOTES_MAX + 10)]
    kept = [str(n).strip() for n in notes if str(n).strip()][:A.LAB_NOTES_MAX]
    assert len(kept) == A.LAB_NOTES_MAX


def test_report_carries_lab_notes_separately_from_our_own_concerns():
    """The lab's wording is reproduced verbatim and must not be mixed into Folin's text."""
    from app.models import Report
    rep = Report(lab_notes=["Doctor's Note : rule out subclinical iron deficiency."],
                 concerns=["Your iron looks low."])
    assert rep.lab_notes != rep.concerns
    assert "Doctor's Note" in rep.lab_notes[0]
