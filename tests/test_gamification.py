"""Behaviour-only XP: the gamification invariants, moved here from the module's old
__main__ self-check so they run with the suite instead of by hand."""
from services.gamification import compute_profile
from services.gamification_rules import EVENT_XP, XP_FIRST_UPLOAD, XP_ON_TIME_RETEST, XP_REPEAT_UPLOAD


def test_gamification_invariants():
    def rep(ed, score=70, markers=None, mid="self", rid="r1"):
        return {"id": rid, "report_date": ed, "uploaded_at": ed + "T10:00:00+00:00",
                "health_score": score, "biomarkers": markers or [], "member_id": mid}

    # First upload: 100 XP, first_report badge, no biology bonus.
    p = compute_profile([rep("2026-01-01")])
    assert p["xp"] == XP_FIRST_UPLOAD and any(b["id"] == "first_report" and b["unlocked"] for b in p["badges"])
    # Weekly spam: repeat uploads only earn 10 XP, no cadence bonus.
    p = compute_profile([rep("2026-01-01"), rep("2026-01-08"), rep("2026-01-15")])
    assert p["xp"] == XP_FIRST_UPLOAD + 2 * XP_REPEAT_UPLOAD
    # On-time retest (90 days): cadence bonus awarded; two in a row unlocks on_schedule.
    p = compute_profile([rep("2026-01-01"), rep("2026-04-01"), rep("2026-07-01")])
    assert p["xp"] == 3 * XP_FIRST_UPLOAD + 2 * XP_ON_TIME_RETEST
    assert any(b["id"] == "on_schedule" and b["unlocked"] for b in p["badges"])
    # Own reports never unlock family_first; a member report does.
    assert not any(b["id"] == "family_first" and b["unlocked"] for b in p["badges"])
    p = compute_profile([rep("2026-01-01", mid="m1")])
    assert any(b["id"] == "family_first" and b["unlocked"] for b in p["badges"])
    # Comeback: flagged marker returns to normal.
    flagged = [{"name": "LDL", "status": "high", "category": "Lipid Profile"}]
    fixed = [{"name": "LDL", "status": "normal", "category": "Lipid Profile"}]
    p = compute_profile([rep("2026-01-01", markers=flagged), rep("2026-04-01", markers=fixed, rid="r2")])
    assert any(b["id"] == "comeback_kid" and b["unlocked"] for b in p["badges"])
    assert any(b["id"] == "tracked_it" and b["unlocked"] for b in p["badges"])
    # Engagement: once-only events count once; explanation_read daily cap enforced.
    evs = [{"type": "profile_completed", "at": "2026-01-02T10:00:00"}] * 3
    evs += [{"type": "explanation_read", "at": f"2026-01-03T10:0{i}:00", "report_id": "r1", "ref": "LDL"} for i in range(7)]
    p = compute_profile([rep("2026-01-01", markers=flagged)], events=evs)
    assert p["xp"] == XP_FIRST_UPLOAD + EVENT_XP["profile_completed"] + 5 * EVENT_XP["explanation_read"]
    # Well Read: the only flagged marker's explanation was opened.
    assert any(b["id"] == "well_read" and b["unlocked"] for b in p["badges"])
