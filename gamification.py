"""Gamification engine: health score, XP, levels, cadence and badges.

Design rule (changed): XP and badges reward BEHAVIOUR the user controls - testing on
the schedule the app itself recommends, completing their profile, reading the
explanation for flagged markers, logging a follow-up. They do NOT reward biological
outcomes the user may be unable to change.

Two reasons this matters:
  1. Rewarding test frequency is an incentive to over-test. Over-testing produces
     incidental findings, anxiety and downstream procedures.
  2. Outcome-locked badges (score >= 90, "all lipids in range") are permanently
     unreachable for users with chronic conditions - i.e. the users who need the app
     most are the ones the reward system tells they are failing.

Derivation is still deterministic. It now reads from reports PLUS an optional
engagement-events list, so deleting a report still leaves state consistent.

All report ordering keys off effective_date(report) = report_date if present else
uploaded_at[:10], so uploading an old report late never corrupts cadence or badges.
"""
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

STATUS_WEIGHT = {"normal": 1.0, "borderline_high": 0.6, "borderline_low": 0.6, "high": 0.2, "low": 0.2}

# Weighted health score: clinically-important categories count more.
CATEGORY_WEIGHTS = {
    "Lipid Profile": 2.0,
    "Diabetes": 2.0,
    "Kidney Function": 1.8,
    "Liver Function": 1.8,
    "Thyroid": 1.5,
    "Complete Blood Count": 1.5,
    "Electrolytes": 1.2,
    "Urine": 1.0,
    "Other": 1.0,
    "Vitamins & Minerals": 0.8,
}

XP_PER_LEVEL = 500

# ---------------------------------------------------------------------------
# Retest cadence. These MUST stay aligned with RETEST_AFTER_DAYS in reports.py.
# A retest earns cadence credit only inside the on-time band. Testing sooner is
# never punished - it simply earns no cadence bonus, so there is no XP reason to
# do it. Testing later is also not punished; the streak just resets quietly.
# ---------------------------------------------------------------------------
RETEST_TARGET_DAYS = 90
RETEST_MIN_DAYS = 70          # below this, no cadence credit (too soon)
RETEST_MAX_DAYS = 140         # above this, the cadence streak resets

# Anti-spam window: a report is "first in window" only when no prior report exists
# within the previous 30 effective-days.
FIRST_IN_WINDOW_DAYS = 30

# ---- XP economy ----
XP_FIRST_UPLOAD = 100         # first report in a 30-day window
XP_REPEAT_UPLOAD = 10         # additional reports inside that window
XP_ON_TIME_RETEST = 60        # retest landed inside the recommended band
XP_TRACKED_FLAGGED = 15       # per previously-flagged marker that was retested (max 3)
XP_TRACKED_CAP = 3

# Engagement XP. Requires engagement events to be passed in; see EVENT_XP below.
EVENT_XP: Dict[str, int] = {
    "profile_completed": 75,      # once
    "explanation_read": 10,       # per flagged marker whose detail sheet was opened
    "followup_logged": 80,        # user recorded a doctor visit / action on a flagged marker
    "allergies_declared": 40,     # once - materially improves suggestion safety
}
# Events that may only ever be counted once, regardless of how many rows exist.
EVENT_ONCE = {"profile_completed", "allergies_declared"}
# Per-event daily cap so a user cannot farm by reopening a sheet repeatedly
# (followup_logged capped too - it is the biggest single award).
EVENT_DAILY_CAP = {"explanation_read": 5, "followup_logged": 1}

BADGE_DEFS = [
    # --- Behavioural: reachable by every user regardless of health status ---
    {"id": "first_report", "name": "First Drop",
     "description": "Upload your first report", "icon": "water"},
    {"id": "reports_3", "name": "Lab Legend",
     "description": "Analyze 3 reports", "icon": "flask"},
    {"id": "reports_5", "name": "Data Devotee",
     "description": "Analyze 5 reports", "icon": "albums"},
    {"id": "profile_complete", "name": "Know Thyself",
     "description": "Complete your health profile", "icon": "person-circle"},
    {"id": "well_read", "name": "Well Read",
     "description": "Read the explanation for every flagged result in a report", "icon": "book"},
    {"id": "on_schedule", "name": "On Schedule",
     "description": "Retest close to the recommended window, twice in a row", "icon": "calendar"},
    {"id": "tracked_it", "name": "Tracked It",
     "description": "Retest a result that was previously flagged", "icon": "eye"},
    {"id": "followed_up", "name": "Followed Up",
     "description": "Log a doctor visit or action after a flagged result", "icon": "medkit"},
    {"id": "family_first", "name": "Family First",
     "description": "Add a family member's report", "icon": "people"},
    # --- Change-based, not state-based. Reachable by anyone whose numbers move at
    #     all; deliberately NOT gated on reaching a "good" absolute value. ---
    {"id": "comeback_kid", "name": "Comeback Kid",
     "description": "Bring a result back into range", "icon": "trending-up"},
]

# Badges removed in this revision, kept here so a migration can grandfather them for
# users who already unlocked them rather than silently deleting earned rewards:
#   score_80 (Vital Victor), score_90 (Peak Performer), perfect_panel (Perfect Panel),
#   heart_hero (Heart Hero), sugar_steady (Sugar Steady), on_fire (testing streak)
RETIRED_BADGE_IDS = {"score_80", "score_90", "perfect_panel", "heart_hero", "sugar_steady", "on_fire"}
RETIRED_BADGE_DEFS = [
    {"id": "score_80", "name": "Vital Victor", "description": "Legacy badge", "icon": "shield-checkmark"},
    {"id": "score_90", "name": "Peak Performer", "description": "Legacy badge", "icon": "trophy"},
    {"id": "perfect_panel", "name": "Perfect Panel", "description": "Legacy badge", "icon": "checkmark-done-circle"},
    {"id": "heart_hero", "name": "Heart Hero", "description": "Legacy badge", "icon": "heart"},
    {"id": "sugar_steady", "name": "Sugar Steady", "description": "Legacy badge", "icon": "pulse"},
    {"id": "on_fire", "name": "On Fire", "description": "Legacy badge", "icon": "flame"},
]


# ---------- Date helpers ----------
def effective_date(report: dict) -> str:
    """Canonical ordering key. Prefer report_date (the clinical event date) over
    uploaded_at (when the user happened to scan the paper)."""
    d = report.get("report_date")
    if isinstance(d, str) and len(d) >= 10 and d[4] == "-" and d[7] == "-":
        return d[:10]
    ua = report.get("uploaded_at") or ""
    return ua[:10] if isinstance(ua, str) else ""


def _effective_sort_key(r: dict):
    return (effective_date(r), r.get("uploaded_at") or "")


def sort_by_effective_date(reports: List[dict], reverse: bool = False) -> List[dict]:
    return sorted(reports, key=_effective_sort_key, reverse=reverse)


def _parse_effective(r: dict) -> Optional[date]:
    s = effective_date(r)
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


# ---------- Scoring (unchanged) ----------
def compute_health_score(biomarkers: List[dict]) -> int:
    """Weighted average. Categories with higher clinical importance count more."""
    if not biomarkers:
        return 0
    num = 0.0
    den = 0.0
    for b in biomarkers:
        sw = STATUS_WEIGHT.get(b.get("status", "normal"), 0.6)
        cw = CATEGORY_WEIGHTS.get((b.get("category") or "Other").strip(), 1.0)
        num += sw * cw
        den += cw
    if den == 0:
        return 0
    return round(100 * num / den)


def compute_document_score(positives: List[str], concerns: List[str]) -> int:
    p, c = len(positives or []), len(concerns or [])
    if p == 0 and c == 0:
        return 65
    raw = 60 + p * 7 - c * 8
    return max(30, min(92, raw))


# ---------- Cadence ----------
def _is_on_time(gap_days: int) -> bool:
    """True when a retest gap falls inside the recommended band."""
    return RETEST_MIN_DAYS <= gap_days <= RETEST_MAX_DAYS


def _flagged_names(report: dict) -> set:
    return {
        (b.get("name") or "").strip().lower()
        for b in report.get("biomarkers", [])
        if b.get("status") and b.get("status") != "normal" and (b.get("name") or "").strip()
    }


def _retested_flagged_count(prev: dict, curr: dict) -> int:
    """How many of the previously-flagged markers appear again in this report.

    Rewards following up on the thing that was actually flagged, whichever way the
    number moved. A user whose condition is progressive still earns this.
    """
    prev_flagged = _flagged_names(prev)
    if not prev_flagged:
        return 0
    curr_names = {(b.get("name") or "").strip().lower() for b in curr.get("biomarkers", [])}
    return len(prev_flagged & curr_names)


def _has_comeback(prev: dict, curr: dict) -> bool:
    prev_bad = _flagged_names(prev)
    for b in curr.get("biomarkers", []):
        if b.get("status") == "normal" and (b.get("name") or "").strip().lower() in prev_bad:
            return True
    return False


# ---------- Engagement events ----------
def _engagement_xp(events: List[dict]) -> tuple:
    """Score engagement events. Each event: {type, at (ISO), report_id, ref (optional)}.

    Returns (xp, seen_types, per_report_reads) where per_report_reads maps
    report_id -> set of marker names whose explanation was opened.
    """
    xp = 0
    seen_types: set = set()
    counted_once: set = set()
    daily_counts: Dict[tuple, int] = {}
    per_report_reads: Dict[str, set] = {}

    for e in sorted(events or [], key=lambda x: x.get("at") or ""):
        etype = e.get("type")
        if etype not in EVENT_XP:
            continue
        seen_types.add(etype)

        if etype in EVENT_ONCE:
            if etype in counted_once:
                continue
            counted_once.add(etype)
            xp += EVENT_XP[etype]
            continue

        # Record what was read BEFORE the XP cap check - the cap limits farming XP,
        # it must not stop the Well Read badge from seeing markers 6+ of a report.
        if etype == "explanation_read":
            rid = str(e.get("report_id") or "")
            marker = (e.get("ref") or "").strip().lower()
            if rid and marker:
                per_report_reads.setdefault(rid, set()).add(marker)

        day = (e.get("at") or "")[:10]
        cap = EVENT_DAILY_CAP.get(etype)
        if cap is not None:
            key = (etype, day)
            if daily_counts.get(key, 0) >= cap:
                continue
            daily_counts[key] = daily_counts.get(key, 0) + 1
        xp += EVENT_XP[etype]

    return xp, seen_types, per_report_reads


# ---------- Profile computation ----------
def compute_profile(
    reports: List[dict],
    events: Optional[List[dict]] = None,
    include_retired: bool = True,
) -> dict:
    """Deterministically compute XP / level / cadence / badges.

    `reports` may arrive in any order. `events` is the optional engagement log; when
    omitted the function degrades gracefully and simply awards no engagement XP.
    """
    reports = sort_by_effective_date(reports)
    events = events or []

    xp = 0
    unlocked: dict = {}
    prev: Optional[dict] = None

    def unlock(badge_id: str, when: str):
        unlocked.setdefault(badge_id, when)

    for i, r in enumerate(reports):
        when = r.get("uploaded_at", "")
        curr_ed = _parse_effective(r)

        # Base upload XP - 100 for the first report inside any 30-day effective-date
        # window; 10 for subsequent uploads inside the same window.
        base_xp = XP_FIRST_UPLOAD
        if curr_ed is not None:
            for older in reports[:i]:
                oed = _parse_effective(older)
                if oed is not None and (curr_ed - oed).days <= FIRST_IN_WINDOW_DAYS:
                    base_xp = XP_REPEAT_UPLOAD
                    break
        xp += base_xp

        # NOTE: the old `xp += health_score // 10` bonus is deliberately gone.
        # It paid users for their biology.

        if prev is not None:
            prev_ed = _parse_effective(prev)
            if curr_ed is not None and prev_ed is not None:
                gap = (curr_ed - prev_ed).days
                # Cadence bonus ONLY inside the recommended band. Crucially this has a
                # lower bound, so there is no XP reason to test early - the previous
                # `0 <= gap <= 60` rule let a weekly tester collect the bonus every time,
                # which bypassed the 30-day anti-spam throttle entirely.
                if _is_on_time(gap):
                    xp += XP_ON_TIME_RETEST

            tracked = _retested_flagged_count(prev, r)
            if tracked:
                xp += XP_TRACKED_FLAGGED * min(tracked, XP_TRACKED_CAP)
                unlock("tracked_it", when)

            if _has_comeback(prev, r):
                unlock("comeback_kid", when)

        # Milestone badges - pure counts, reachable by anyone.
        if i == 0:
            unlock("first_report", when)
        if i + 1 >= 3:
            unlock("reports_3", when)
        if i + 1 >= 5:
            unlock("reports_5", when)
        # Adapted for this stack: member_id is "self" for the user's own reports,
        # so only a real sub-profile id counts as a family upload.
        if r.get("member_id") and r.get("member_id") != "self":
            unlock("family_first", when)

        prev = r

    # ---- Engagement XP + engagement badges ----
    ev_xp, seen_types, per_report_reads = _engagement_xp(events)
    xp += ev_xp
    latest_event_at = max((e.get("at") or "" for e in events), default="")
    if "profile_completed" in seen_types:
        unlock("profile_complete", latest_event_at)
    if "followup_logged" in seen_types:
        unlock("followed_up", latest_event_at)

    # "Well Read" - every flagged marker in some report had its explanation opened.
    for r in reports:
        rid = str(r.get("id") or r.get("_id") or "")
        flagged = _flagged_names(r)
        if flagged and rid and flagged.issubset(per_report_reads.get(rid, set())):
            unlock("well_read", r.get("uploaded_at", ""))
            break

    # ---- Cadence streak: consecutive on-time retests, trailing ----
    cadence_streak = 0
    if len(reports) >= 2:
        cadence_streak = 1
        for i in range(len(reports) - 1, 0, -1):
            cur_ed = _parse_effective(reports[i])
            prv_ed = _parse_effective(reports[i - 1])
            if cur_ed is None or prv_ed is None:
                break
            if _is_on_time((cur_ed - prv_ed).days):
                cadence_streak += 1
            else:
                break
    elif reports:
        cadence_streak = 1

    if cadence_streak >= 3:  # two consecutive on-time gaps
        unlock("on_schedule", reports[-1].get("uploaded_at", ""))

    level = xp // XP_PER_LEVEL + 1
    xp_in_level = xp % XP_PER_LEVEL
    # Only lab reports carry a measured score. A prescription/visit note scores by
    # document heuristic (~65) and used to overwrite "your health score".
    scored = [r for r in reports if (r.get("report_type") or "lab") == "lab"]
    scores = [r.get("health_score", 0) for r in scored]

    defs = list(BADGE_DEFS)
    if include_retired:
        # Grandfather: only surface a retired badge if this user already holds it.
        defs += [b for b in RETIRED_BADGE_DEFS if b["id"] in unlocked]
    badges = [{**b, "unlocked": b["id"] in unlocked, "unlocked_at": unlocked.get(b["id"])} for b in defs]

    active_total = len(BADGE_DEFS)
    active_unlocked = len([b for b in badges if b["unlocked"] and b["id"] not in RETIRED_BADGE_IDS])

    return {
        "xp": xp,
        "level": level,
        "xp_in_level": xp_in_level,
        "xp_to_next": XP_PER_LEVEL - xp_in_level,
        "level_progress": xp_in_level / XP_PER_LEVEL,
        # `streak` retained for frontend compatibility but now means "on-time retests
        # in a row", not "reports in the last 60 days".
        "streak": cadence_streak,
        "cadence_streak": cadence_streak,
        "reports_count": len(reports),
        "latest_score": scores[-1] if scores else None,
        "best_score": max(scores) if scores else None,
        "badges": badges,
        "badges_unlocked": active_unlocked,
        "badges_total": active_total,
    }


if __name__ == "__main__":
    # Self-check: run `python gamification.py`
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
    print("gamification self-check passed")
