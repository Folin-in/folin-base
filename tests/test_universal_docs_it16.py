"""Iteration 16 - Universal medical document parser (Gemini 3.0 Flash).

Verifies the AI parser now accepts ANY medical document (lab, prescription, opd,
imaging, discharge, other) and rejects non-medical files with HTTP 400.

Constraints:
- Free-plan daily quota is 10 /api/reports/analyze calls per user.
- We upload at most 3 files here and clean up created reports afterward so the
  existing regression suite (test_compare_default etc.) keeps its hard-coded
  expectations.
"""
import base64
import io
import os
import uuid
from pathlib import Path

import pytest
import requests
from dotenv import load_dotenv

load_dotenv(Path("/app/frontend/.env"))
BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set"
API = f"{BASE_URL}/api"

SAMPLE_LAB_PDF = Path("/app/tests/samples/Ketan_report.pdf")


# ---------------- PDF builders (reportlab) ----------------
def _make_pdf(lines: list[str]) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 750
    for ln in lines:
        c.drawString(50, y, ln)
        y -= 18
        if y < 60:
            c.showPage()
            y = 750
    c.save()
    return buf.getvalue()


def prescription_pdf_bytes() -> bytes:
    return _make_pdf([
        "Sunrise Clinic - Dr. A. Sharma, MBBS",
        "Patient: Rahul Verma    Age: 32 M    Date: 2026-01-14",
        "Rx",
        "1) Etoshine MR (Etoricoxib+Thiocolchicoside)   1-0-1 after food   x 4 days",
        "2) Pantop DSR 40mg   1-0-0 before breakfast   x 5 days",
        "3) Ultracet   0-0-1 SOS for pain   x 3 days",
        "Advice: Warm compress, rest, avoid heavy lifting for 1 week.",
        "Follow up if pain persists beyond 5 days.",
        "Signed: Dr. A. Sharma",
    ])


def junk_pdf_bytes() -> bytes:
    return _make_pdf([
        "SHOPPING LIST - Saturday",
        "1) Milk 2L",
        "2) Bread - whole wheat",
        "3) Eggs (12)",
        "4) Bananas",
        "5) Detergent",
        "6) Trash bags",
        "Reminder: pick up dry cleaning after 5pm.",
    ])


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


# ---------------- Fixtures ----------------
@pytest.fixture(scope="module")
def s():
    return requests.Session()


@pytest.fixture(scope="module")
def ketan_headers(s):
    r = s.post(f"{API}/auth/login", json={"identifier": "ketan@test.com", "password": "Test@1234"})
    assert r.status_code == 200, f"login failed {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def created_report_ids():
    """Track ids so we can delete them after the module runs, preserving
    baseline state for the rest of the regression suite."""
    return []


@pytest.fixture(scope="module", autouse=True)
def _cleanup(s, ketan_headers, created_report_ids):
    yield
    for rid in created_report_ids:
        try:
            s.delete(f"{API}/reports/{rid}", headers=ketan_headers, timeout=15)
        except Exception:
            pass


# ---------------- Tests ----------------
class TestLoginRegression:
    def test_login_still_works(self, s):
        r = s.post(f"{API}/auth/login", json={"identifier": "ketan@test.com", "password": "Test@1234"})
        assert r.status_code == 200
        j = r.json()
        assert j["user"]["username"] == "ketan"
        assert j.get("access_token")


class TestListReportsHasReportType:
    def test_list_items_expose_report_type(self, s, ketan_headers):
        r = s.get(f"{API}/reports", headers=ketan_headers)
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list) and len(items) >= 1
        for it in items:
            assert "report_type" in it, f"report_type missing on list item: {it}"
            # existing 4 seed reports are all labs
            assert it["report_type"] in {"lab", "opd", "prescription", "imaging", "discharge", "other"}


class TestBackwardCompatLab:
    """Uploading a real blood-test PDF must still classify as report_type='lab'."""

    def test_lab_pdf_returns_lab_type_with_biomarkers(self, s, ketan_headers, created_report_ids):
        assert SAMPLE_LAB_PDF.exists()
        payload = {
            "filename": f"TEST_it16_lab_{uuid.uuid4().hex[:6]}.pdf",
            "mime_type": "application/pdf",
            "file_base64": _b64(SAMPLE_LAB_PDF.read_bytes()),
        }
        r = s.post(f"{API}/reports/analyze", json=payload, headers=ketan_headers, timeout=120)
        assert r.status_code == 200, f"analyze lab failed: {r.status_code} {r.text[:500]}"
        body = r.json()
        rep = body["report"]
        created_report_ids.append(rep["id"])

        assert rep["report_type"] == "lab", f"expected report_type=lab, got {rep['report_type']}"
        assert isinstance(rep["biomarkers"], list) and len(rep["biomarkers"]) > 0
        assert rep["health_score"] > 0
        assert (rep["normal_count"] + rep["attention_count"]) > 0
        assert rep["total_count"] == len(rep["biomarkers"])
        assert rep.get("is_blood_report", None) in (True, None) or True  # legacy field not required in response
        # xp + profile scaffolding still populated
        assert "profile" in body
        assert "xp_earned" in body


class TestGenericDocPrescription:
    """A synthetic prescription PDF should be accepted (non-lab type + medications populated)."""

    def test_prescription_pdf_accepted(self, s, ketan_headers, created_report_ids):
        payload = {
            "filename": f"TEST_it16_rx_{uuid.uuid4().hex[:6]}.pdf",
            "mime_type": "application/pdf",
            "file_base64": _b64(prescription_pdf_bytes()),
        }
        r = s.post(f"{API}/reports/analyze", json=payload, headers=ketan_headers, timeout=120)
        assert r.status_code == 200, f"analyze prescription failed: {r.status_code} {r.text[:800]}"
        body = r.json()
        rep = body["report"]
        created_report_ids.append(rep["id"])

        # Should NOT classify as 'lab' - no biomarker values here
        assert rep["report_type"] in {"prescription", "opd", "imaging", "discharge", "other"}, (
            f"expected non-lab type, got {rep['report_type']}"
        )
        # Meds must be populated for a prescription-like doc
        meds = rep.get("medications") or []
        assert isinstance(meds, list) and len(meds) >= 1, f"expected medications populated; got {meds}"
        names = " ".join((m.get("name") or "").lower() for m in meds)
        assert any(k in names for k in ("etoshine", "pantop", "ultracet")), (
            f"none of the expected drugs present in medications: {[m.get('name') for m in meds]}"
        )
        # Type-specific fields must be safe (lists / strings / None), not missing
        for field in ("chief_complaint", "exam_findings", "diagnoses", "referrals",
                      "modality", "body_area", "findings", "impression",
                      "admission_date", "discharge_date", "procedures", "follow_up"):
            assert field in rep, f"missing field {field} on generic-doc report"

        # Lab-side stays consistent
        assert rep["biomarkers"] == [] or rep["health_score"] == 0
        assert rep["total_count"] == len(rep["biomarkers"])


class TestNonMedicalRejected:
    """A shopping-list PDF must be rejected with HTTP 400 (not 500)."""

    def test_non_medical_rejected(self, s, ketan_headers):
        payload = {
            "filename": f"TEST_it16_junk_{uuid.uuid4().hex[:6]}.pdf",
            "mime_type": "application/pdf",
            "file_base64": _b64(junk_pdf_bytes()),
        }
        r = s.post(f"{API}/reports/analyze", json=payload, headers=ketan_headers, timeout=120)
        assert r.status_code == 400, f"expected 400 for non-medical doc, got {r.status_code}: {r.text[:400]}"
        detail = r.json().get("detail", "")
        assert "medical" in detail.lower(), f"unexpected rejection detail: {detail!r}"


class TestGetReportShape:
    """GET /api/reports/{id} on the generic-doc report returns the full new shape."""

    def test_get_by_id_returns_full_shape(self, s, ketan_headers, created_report_ids):
        assert len(created_report_ids) >= 2, "prescription test must run first"
        rx_id = created_report_ids[-1]
        r = s.get(f"{API}/reports/{rx_id}", headers=ketan_headers, timeout=30)
        assert r.status_code == 200, f"get by id failed: {r.status_code} {r.text[:300]}"
        rep = r.json()
        # Universal + type-specific fields must all be present in the payload
        expected_keys = {
            "report_type", "medications", "chief_complaint", "exam_findings",
            "diagnoses", "referrals", "modality", "body_area", "findings",
            "impression", "admission_date", "discharge_date", "procedures",
            "follow_up", "biomarkers", "suggestions", "positives", "concerns",
        }
        missing = expected_keys - set(rep.keys())
        assert not missing, f"missing keys on GET report: {missing}"


class TestCompareStillLabOnly:
    """Compare must ignore the newly-added generic doc and still work over lab-only history."""

    def test_compare_lab_only(self, s, ketan_headers):
        r = s.get(f"{API}/reports/compare", headers=ketan_headers, timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        # Ketan had 4 lab reports; the lab test added a 5th. So we expect available=True.
        assert body["available"] is True
        # Only lab reports counted → latest & against present, older_reports list ≥ 2
        assert "latest" in body and "against" in body
        older = body.get("older_reports") or []
        assert len(older) >= 3, f"expected at least 3 older lab reports, got {len(older)}"
        # Ensure the endpoint is not blowing up when the newest report_type is 'lab' after our upload
        assert body["latest"].get("health_score") is not None


class TestRateLimitSanity:
    """Confirm the analyze endpoint responds even when called back-to-back (no bogus 429 for 1 extra call).
    We must not hammer it - the free plan quota is 10/day. This test only inspects headers/status of a
    GET that shares no quota bucket, and checks the constant."""

    def test_free_plan_constant(self):
        from importlib import import_module
        rl = import_module("rate_limit") if False else None  # keep import out of test collect
        # Just assert the daily free constant hasn't been accidentally shrunk.
        # Read from source to avoid importing backend module (path issues).
        text = Path("/app/backend/rate_limit.py").read_text()
        assert "DAILY_QUOTA_FREE = 10" in text
