"""Iteration 17 - Cumulative score, per-report rename, aggregated Tips,
Compare picker (a/b), and heuristic score for non-lab reports.

Constraints:
- Free-plan daily quota is 10 /api/reports/analyze calls per user.
- We upload at most 2 files here (prescription + junk) and clean up.
- No modifications to seed data.
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
load_dotenv(Path("/app/backend/.env"))
BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set"
API = f"{BASE_URL}/api"


# ---------------- PDF builders ----------------
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
        "4) Detergent",
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
def user2(s):
    uid = uuid.uuid4().hex[:8]
    payload = {
        "email": f"TEST_it17_{uid}@vqtest.com",
        "username": f"TEST_it17_{uid}",
        "password": "Password@123",
        "name": "TEST IT17",
        "consent": True,
    }
    r = s.post(f"{API}/auth/register", json=payload)
    assert r.status_code == 200, f"register user2 failed: {r.status_code} {r.text}"
    body = r.json()
    return {
        "token": body["access_token"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


@pytest.fixture(scope="module")
def created_report_ids():
    return []


@pytest.fixture(scope="module", autouse=True)
def _cleanup(s, ketan_headers, created_report_ids):
    yield
    for rid in created_report_ids:
        try:
            s.delete(f"{API}/reports/{rid}", headers=ketan_headers, timeout=15)
        except Exception:
            pass


# ---------------- 1. Cumulative dashboard score ----------------
class TestCumulativeScore:
    def test_profile_returns_cumulative_score(self, s, ketan_headers):
        r = s.get(f"{API}/profile", headers=ketan_headers)
        assert r.status_code == 200
        p = r.json()
        assert "cumulative_score" in p
        assert isinstance(p["cumulative_score"], int)

    def test_cumulative_matches_average_of_all_reports(self, s, ketan_headers):
        pr = s.get(f"{API}/profile", headers=ketan_headers)
        assert pr.status_code == 200
        cum = pr.json()["cumulative_score"]

        rr = s.get(f"{API}/reports", headers=ketan_headers)
        assert rr.status_code == 200
        scores = [it["health_score"] for it in rr.json()]
        assert len(scores) >= 1
        expected = round(sum(scores) / len(scores))
        assert cum == expected, f"cumulative_score {cum} != round(avg({scores}))={expected}"

    def test_user2_empty_cumulative_is_zero(self, s, user2):
        r = s.get(f"{API}/profile", headers=user2["headers"])
        assert r.status_code == 200
        assert r.json()["cumulative_score"] == 0


# ---------------- 6. Heuristic score + prescription analyze ----------------
class TestHeuristicPrescription:
    def test_analyze_prescription_populates_heuristic(self, s, ketan_headers, created_report_ids):
        payload = {
            "filename": f"TEST_it17_rx_{uuid.uuid4().hex[:6]}.pdf",
            "mime_type": "application/pdf",
            "file_base64": _b64(prescription_pdf_bytes()),
        }
        r = s.post(f"{API}/reports/analyze", json=payload, headers=ketan_headers, timeout=180)
        assert r.status_code == 200, f"analyze prescription failed: {r.status_code} {r.text[:400]}"
        rep = r.json()["report"]
        created_report_ids.append(rep["id"])
        assert rep["report_type"] == "prescription", f"expected prescription, got {rep['report_type']}"
        # Heuristic score must be non-zero and within compute_document_score bounds
        assert 30 <= rep["health_score"] <= 92, f"heuristic score out of [30,92]: {rep['health_score']}"
        # Medications populated
        meds = rep.get("medications") or []
        assert len(meds) >= 1, f"expected medications populated; got {meds}"
        names = " ".join((m.get("name") or "").lower() for m in meds)
        assert any(k in names for k in ("etoshine", "pantop")), (
            f"expected Etoshine/Pantop; got: {[m.get('name') for m in meds]}"
        )

    def test_cumulative_updated_after_analyze(self, s, ketan_headers):
        pr = s.get(f"{API}/profile", headers=ketan_headers)
        cum = pr.json()["cumulative_score"]
        rr = s.get(f"{API}/reports", headers=ketan_headers)
        scores = [it["health_score"] for it in rr.json()]
        expected = round(sum(scores) / len(scores))
        assert cum == expected, f"cumulative {cum} != recomputed {expected}"


# ---------------- 6b. Non-medical rejection + quota refund ----------------
class TestNonMedicalQuotaRefund:
    def test_non_medical_refunds_quota(self, s, ketan_headers):
        # Snapshot mongo counter before
        from datetime import datetime, timezone
        from pymongo import MongoClient

        mongo_url = os.environ.get("MONGO_URL")
        db_name = os.environ.get("DB_NAME")
        assert mongo_url and db_name, "MONGO_URL/DB_NAME missing"
        client = MongoClient(mongo_url)
        db = client[db_name]

        # ketan user_id - fetch via /auth/me
        me = s.get(f"{API}/auth/me", headers=ketan_headers).json()
        user_id = me["user_id"]
        day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        doc_id = f"reports_analyze:{user_id}:{day_key}"

        before = db.quota_counters.find_one({"_id": doc_id})
        before_count = (before or {}).get("count", 0)

        payload = {
            "filename": f"TEST_it17_junk_{uuid.uuid4().hex[:6]}.pdf",
            "mime_type": "application/pdf",
            "file_base64": _b64(junk_pdf_bytes()),
        }
        r = s.post(f"{API}/reports/analyze", json=payload, headers=ketan_headers, timeout=180)
        assert r.status_code == 400, f"expected 400 for non-medical doc, got {r.status_code}"

        after = db.quota_counters.find_one({"_id": doc_id})
        after_count = (after or {}).get("count", 0)
        assert after_count == before_count, (
            f"quota not refunded: before={before_count} after={after_count}"
        )


# ---------------- 2. Report rename ----------------
class TestReportRename:
    def _first_report_id(self, s, headers):
        r = s.get(f"{API}/reports", headers=headers)
        return r.json()[0]["id"]

    def test_rename_happy_path(self, s, ketan_headers):
        rid = self._first_report_id(s, ketan_headers)
        new_title = f"TEST My Renamed Panel {uuid.uuid4().hex[:4]}"
        r = s.patch(f"{API}/reports/{rid}", json={"title": new_title}, headers=ketan_headers)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        body = r.json()
        assert body["id"] == rid
        assert body["title"] == new_title
        # GET reflects it
        g = s.get(f"{API}/reports/{rid}", headers=ketan_headers)
        assert g.status_code == 200
        assert g.json()["title"] == new_title
        # List reflects it
        lst = s.get(f"{API}/reports", headers=ketan_headers).json()
        titles = {it["id"]: it.get("title") for it in lst}
        assert titles.get(rid) == new_title
        # Restore to null for regression cleanliness - but PATCH requires min_length 1.
        # Restore to something innocuous, then delete? We won't delete seed. Reset via db.
        from pymongo import MongoClient
        db = MongoClient(os.environ.get("MONGO_URL"))[os.environ.get("DB_NAME")]
        from bson import ObjectId
        db.reports.update_one({"_id": ObjectId(rid)}, {"$set": {"title": None}})

    def test_rename_empty_400(self, s, ketan_headers):
        rid = self._first_report_id(s, ketan_headers)
        r = s.patch(f"{API}/reports/{rid}", json={"title": "   "}, headers=ketan_headers)
        # Pydantic min_length=1 strips? No - min_length=1 on non-stripped string. "   " has length 3, passes.
        # Then handler strips and returns 400 "Title cannot be empty".
        assert r.status_code == 400, f"expected 400 got {r.status_code}: {r.text}"

    def test_rename_empty_string_422(self, s, ketan_headers):
        rid = self._first_report_id(s, ketan_headers)
        r = s.patch(f"{API}/reports/{rid}", json={"title": ""}, headers=ketan_headers)
        # min_length=1 → 422
        assert r.status_code in (400, 422)

    def test_rename_over_80_chars(self, s, ketan_headers):
        rid = self._first_report_id(s, ketan_headers)
        long_title = "x" * 81
        r = s.patch(f"{API}/reports/{rid}", json={"title": long_title}, headers=ketan_headers)
        assert r.status_code in (400, 422), f"expected 400/422 got {r.status_code}"

    def test_rename_nonexistent_404(self, s, ketan_headers):
        # valid ObjectId not present
        r = s.patch(f"{API}/reports/5f8d0e5f7b0d5a1a2b3c4d5e",
                    json={"title": "Nope"}, headers=ketan_headers)
        assert r.status_code == 404

    def test_rename_invalid_objectid_400(self, s, ketan_headers):
        r = s.patch(f"{API}/reports/not-a-real-id",
                    json={"title": "Nope"}, headers=ketan_headers)
        assert r.status_code == 400

    def test_rename_requires_auth(self, s, ketan_headers):
        rid = self._first_report_id(s, ketan_headers)
        r = s.patch(f"{API}/reports/{rid}", json={"title": "x"})
        assert r.status_code == 401

    def test_rename_owner_scoped(self, s, ketan_headers, user2):
        rid = self._first_report_id(s, ketan_headers)
        r = s.patch(f"{API}/reports/{rid}", json={"title": "hax"}, headers=user2["headers"])
        assert r.status_code == 404, f"expected 404 owner-scoped got {r.status_code}"


# ---------------- 3. Report list exposes title ----------------
class TestListExposesTitle:
    def test_list_items_include_title_field(self, s, ketan_headers):
        r = s.get(f"{API}/reports", headers=ketan_headers)
        assert r.status_code == 200
        items = r.json()
        assert len(items) >= 1
        for it in items:
            assert "title" in it, f"list item missing title: {it}"

    def test_rename_reflected_in_list(self, s, ketan_headers):
        rid = s.get(f"{API}/reports", headers=ketan_headers).json()[0]["id"]
        new_title = f"TEST List Title {uuid.uuid4().hex[:4]}"
        s.patch(f"{API}/reports/{rid}", json={"title": new_title}, headers=ketan_headers)
        lst = s.get(f"{API}/reports", headers=ketan_headers).json()
        assert next(it for it in lst if it["id"] == rid)["title"] == new_title
        # cleanup title
        from pymongo import MongoClient
        from bson import ObjectId
        db = MongoClient(os.environ.get("MONGO_URL"))[os.environ.get("DB_NAME")]
        db.reports.update_one({"_id": ObjectId(rid)}, {"$set": {"title": None}})


# ---------------- 4. Aggregated suggestions ----------------
class TestAggregatedSuggestions:
    def test_shape_and_dedup(self, s, ketan_headers):
        r = s.get(f"{API}/reports/suggestions", headers=ketan_headers)
        assert r.status_code == 200
        body = r.json()
        assert "reports" in body and "suggestions" in body
        assert isinstance(body["reports"], list)
        assert isinstance(body["suggestions"], list)
        rr = s.get(f"{API}/reports", headers=ketan_headers).json()
        assert len(body["reports"]) == len(rr)
        # Each report meta shape
        for m in body["reports"]:
            for k in ("id", "title", "report_type", "report_date", "uploaded_at"):
                assert k in m
        # Suggestions shape + normalized categories
        allowed = {"nutrition", "movement", "sleep", "supplements", "lifestyle"}
        seen = set()
        for s_item in body["suggestions"]:
            for k in ("title", "body", "category", "linked_marker",
                      "report_id", "report_title", "report_type", "report_date"):
                assert k in s_item, f"missing key {k} in suggestion: {s_item}"
            assert s_item["category"] in allowed, f"bad category: {s_item['category']}"
            key = (s_item["category"], s_item["title"].strip().lower())
            assert key not in seen, f"duplicate suggestion after dedup: {key}"
            seen.add(key)

    def test_empty_for_new_user(self, s, user2):
        r = s.get(f"{API}/reports/suggestions", headers=user2["headers"])
        assert r.status_code == 200
        body = r.json()
        assert body == {"reports": [], "suggestions": []}

    def test_requires_auth(self, s):
        r = s.get(f"{API}/reports/suggestions")
        assert r.status_code == 401


# ---------------- 5. Compare picker (a & b) ----------------
class TestComparePicker:
    def _lab_ids(self, s, ketan_headers):
        items = s.get(f"{API}/reports", headers=ketan_headers).json()
        return [it["id"] for it in items if it.get("report_type") == "lab"]

    def test_picker_two_valid_labs(self, s, ketan_headers):
        ids = self._lab_ids(s, ketan_headers)
        assert len(ids) >= 2
        a, b = ids[0], ids[1]
        r = s.get(f"{API}/reports/compare", params={"a": a, "b": b}, headers=ketan_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is True
        assert body["latest"]["id"] == a
        assert body["against"]["id"] == b

    def test_picker_same_id_400(self, s, ketan_headers):
        ids = self._lab_ids(s, ketan_headers)
        a = ids[0]
        r = s.get(f"{API}/reports/compare", params={"a": a, "b": a}, headers=ketan_headers)
        assert r.status_code == 400
        assert "different" in r.json().get("detail", "").lower()

    def test_picker_invalid_objectid_400(self, s, ketan_headers):
        ids = self._lab_ids(s, ketan_headers)
        r = s.get(f"{API}/reports/compare", params={"a": "garbage", "b": ids[0]}, headers=ketan_headers)
        assert r.status_code == 400
        r2 = s.get(f"{API}/reports/compare", params={"a": ids[0], "b": "garbage"}, headers=ketan_headers)
        assert r2.status_code == 400

    def test_picker_non_lab_id_404(self, s, ketan_headers, created_report_ids):
        # Use the prescription created in TestHeuristicPrescription
        assert created_report_ids, "prescription test must have run first"
        rx_id = created_report_ids[0]
        ids = self._lab_ids(s, ketan_headers)
        # Prescription is NOT in the lab-only doc list, so it maps to "not a lab report" → 404
        r = s.get(f"{API}/reports/compare",
                  params={"a": rx_id, "b": ids[0]}, headers=ketan_headers)
        assert r.status_code == 404, f"expected 404 got {r.status_code}: {r.text}"
        assert "lab report" in r.json().get("detail", "").lower()

    def test_backcompat_default(self, s, ketan_headers):
        r = s.get(f"{API}/reports/compare", headers=ketan_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert "latest" in body and "against" in body

    def test_backcompat_against(self, s, ketan_headers):
        # Get an older id via default compare
        default = s.get(f"{API}/reports/compare", headers=ketan_headers).json()
        older = default["older_reports"]
        assert len(older) >= 1
        target = older[0]["id"]
        r = s.get(f"{API}/reports/compare", params={"against": target}, headers=ketan_headers)
        assert r.status_code == 200
        assert r.json()["against"]["id"] == target


# ---------------- Delete recomputes cumulative ----------------
class TestCumulativeAfterDelete:
    def test_delete_recomputes(self, s, ketan_headers, created_report_ids):
        assert created_report_ids, "prescription must have been created"
        rid = created_report_ids[0]
        # Before delete
        before_scores = [it["health_score"] for it in s.get(f"{API}/reports", headers=ketan_headers).json()]
        # Delete
        r = s.delete(f"{API}/reports/{rid}", headers=ketan_headers)
        assert r.status_code == 200
        # Remove from tracked ids since we already deleted it
        created_report_ids.remove(rid)
        after_scores = [it["health_score"] for it in s.get(f"{API}/reports", headers=ketan_headers).json()]
        assert len(after_scores) == len(before_scores) - 1
        pr = s.get(f"{API}/profile", headers=ketan_headers).json()
        expected = round(sum(after_scores) / len(after_scores))
        assert pr["cumulative_score"] == expected, (
            f"post-delete cumulative {pr['cumulative_score']} != {expected}"
        )
