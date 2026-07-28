"""Iteration 18 backend spot check.
Verifies iteration 17 features still work after the ai_service.py system-prompt update:
1. GET /api/profile returns cumulative_score = 68 for Ketan's 4 seed reports.
2. PATCH /api/reports/{id} rename still works (title round-trip).
3. GET /api/reports/suggestions returns non-empty aggregate for Ketan.
4. GET /api/reports/compare?a=X&b=Y still works with two lab ids.
5. Analyze one FRESH PDF with the updated system prompt (voice + diet lock).
   - Verifies no meat words in suggestions (except in avoidance clauses).
   - Verifies personalization (age/BMI/strength training) shows up.
   - Verifies non-jargon voice.
   - Cleans up the created report at the end.
"""
import os
import re
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/") or "http://localhost:8001"
LOGIN_EMAIL = "ketan@test.com"
LOGIN_PASSWORD = "Test@1234"
SAMPLE_PDF = "/app/tests/samples/Ketan_report.pdf"


@pytest.fixture(scope="module")
def token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"identifier": LOGIN_EMAIL, "password": LOGIN_PASSWORD},
        timeout=30,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tk = r.json().get("access_token")
    assert tk
    return tk


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}"}


class TestRegression:
    """Iteration-17 features must still work after the ai_service prompt update."""

    def test_profile_cumulative_score_is_68(self, headers):
        r = requests.get(f"{BASE_URL}/api/profile", headers=headers, timeout=15)
        assert r.status_code == 200, r.text
        p = r.json()
        assert "cumulative_score" in p
        # Iteration 17 established cumulative_score = 68 for Ketan's 4 seed reports.
        assert p["cumulative_score"] == 68, f"cumulative_score expected 68, got {p['cumulative_score']}"

    def test_reports_list_carries_title(self, headers):
        r = requests.get(f"{BASE_URL}/api/reports", headers=headers, timeout=15)
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list) and len(items) >= 4
        # every item must expose title (nullable) + report_type keys
        for it in items:
            assert "title" in it
            assert "report_type" in it

    def test_patch_rename_roundtrip(self, headers):
        # pick oldest report to avoid interfering with compare/suggestion tests
        r = requests.get(f"{BASE_URL}/api/reports", headers=headers, timeout=15)
        assert r.status_code == 200
        items = r.json()
        target = items[-1]
        original = target.get("title")
        new_title = "TEST_it18_rename_check"
        try:
            resp = requests.patch(
                f"{BASE_URL}/api/reports/{target['id']}",
                json={"title": new_title},
                headers=headers,
                timeout=15,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["id"] == target["id"]
            assert body["title"] == new_title

            # verify persistence via GET
            g = requests.get(f"{BASE_URL}/api/reports/{target['id']}", headers=headers, timeout=15)
            assert g.status_code == 200
            assert g.json().get("title") == new_title
        finally:
            # restore original (or clear to null)
            restore = original if original else ""
            if restore:
                requests.patch(
                    f"{BASE_URL}/api/reports/{target['id']}",
                    json={"title": restore},
                    headers=headers,
                    timeout=15,
                )

    def test_aggregated_suggestions_non_empty(self, headers):
        r = requests.get(f"{BASE_URL}/api/reports/suggestions", headers=headers, timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert "reports" in body and "suggestions" in body
        assert len(body["reports"]) >= 4
        assert len(body["suggestions"]) > 0
        for s in body["suggestions"][:5]:
            assert "title" in s and "body" in s and "category" in s
            assert s["category"] in {"nutrition", "movement", "sleep", "supplements", "lifestyle"}
            assert "report_id" in s and "report_title" in s

    def test_compare_picker_two_lab_ids(self, headers):
        r = requests.get(f"{BASE_URL}/api/reports", headers=headers, timeout=15)
        assert r.status_code == 200
        labs = [it for it in r.json() if (it.get("report_type") or "lab") == "lab"]
        assert len(labs) >= 2
        a, b = labs[0]["id"], labs[1]["id"]
        c = requests.get(
            f"{BASE_URL}/api/reports/compare",
            params={"a": a, "b": b},
            headers=headers,
            timeout=15,
        )
        assert c.status_code == 200, c.text
        data = c.json()
        assert data.get("available") is True
        assert data["latest"]["id"] == a
        assert data["against"]["id"] == b
        assert "improved_count" in data and "worsened_count" in data and "same_count" in data


# ---------------------------------------------------------------------------
# Fresh analyze under the new prompt - vegetarian voice + personalization check
# ---------------------------------------------------------------------------

MEAT_WORDS = [
    "chicken", "beef", "pork", "mutton", "lamb", "bacon", "sausage",
    "fish", "salmon", "tuna", "sardine", "mackerel", "shrimp", "prawn",
    "seafood", "meat",
]
JARGON_WORDS = ["biomarker", "elevated", "dyslipidemia", "prophylactic"]


def _extract_suggestion_text(report_obj):
    """Return a lowercase blob of all suggestion titles+bodies."""
    parts = []
    for s in report_obj.get("suggestions", []) or []:
        parts.append((s.get("title") or "") + " " + (s.get("body") or ""))
    return " ".join(parts).lower()


class TestFreshAnalyzeVegetarianVoice:
    """Burn 1 quota slot to verify the diet-lock + plain-language prompt."""

    created_report_id = None

    def test_analyze_new_pdf(self, headers):
        assert os.path.exists(SAMPLE_PDF), f"missing {SAMPLE_PDF}"
        with open(SAMPLE_PDF, "rb") as fh:
            files = {"file": ("Ketan_report.pdf", fh, "application/pdf")}
            r = requests.post(
                f"{BASE_URL}/api/reports/analyze",
                files=files,
                headers={"Authorization": headers["Authorization"]},
                timeout=180,
            )
        # Might return 429 (quota) or 200
        if r.status_code == 429:
            pytest.skip(f"quota exhausted: {r.text}")
        assert r.status_code == 200, f"analyze failed {r.status_code}: {r.text[:400]}"
        body = r.json()
        rid = body.get("id") or body.get("report_id")
        assert rid, f"missing id in analyze response: {body}"
        TestFreshAnalyzeVegetarianVoice.created_report_id = rid

        # fetch full detail
        d = requests.get(f"{BASE_URL}/api/reports/{rid}", headers=headers, timeout=30)
        assert d.status_code == 200
        report = d.json()
        blob = _extract_suggestion_text(report)
        assert blob, "no suggestions returned"

        # ---- Diet lock: no meat words except in an 'avoid' clause ------------
        bad_hits = []
        for w in MEAT_WORDS:
            for m in re.finditer(rf"\b{re.escape(w)}\b", blob):
                start = max(0, m.start() - 40)
                window = blob[start : m.end()]
                if not re.search(r"avoid|instead of|skip|no\s+|limit|reduce|cut back|swap", window):
                    bad_hits.append((w, window))
        assert not bad_hits, f"Meat words found outside avoidance context: {bad_hits[:3]}"

        # ---- Personalization: at least one of Ketan's markers appears --------
        # 28y OR strength training OR BMI 24.5 OR vegetarian
        personalized = any(
            k in blob
            for k in ["28", "strength", "vegetarian", "24.5", "bmi 24", "lift", "muscle"]
        )
        assert personalized, f"suggestions do not reference profile. sample: {blob[:400]}"

        # ---- Plain language: no raw jargon -----------------------------------
        jargon_hits = [w for w in JARGON_WORDS if re.search(rf"\b{w}\b", blob)]
        assert not jargon_hits, f"Jargon words found: {jargon_hits}. sample: {blob[:400]}"

    def test_zzz_cleanup_created_report(self, headers):
        rid = TestFreshAnalyzeVegetarianVoice.created_report_id
        if not rid:
            pytest.skip("no report to clean up")
        r = requests.delete(f"{BASE_URL}/api/reports/{rid}", headers=headers, timeout=15)
        assert r.status_code in (200, 204), r.text
