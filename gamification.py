"""Gamification engine: health score, XP, levels, streaks and badges.
Everything is derived deterministically from the user's reports collection.

All ordering keys off effective_date(report) = report_date if present else uploaded_at[:10]
so that uploading an old report late never corrupts streaks, comparisons or badge unlocks.
"""
from datetime import date, datetime, timezone
from typing import List, Optional

STATUS_WEIGHT = {"normal": 1.0, "borderline_high": 0.6, "borderline_low": 0.6, "high": 0.2, "low": 0.2}

# Weighted health score: clinically-important categories count more. See spec item #3.
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
STREAK_WINDOW_DAYS = 60
# Anti-spam window: a report counts as "first in window" only when no prior report exists
# within the previous 30 effective-days.
FIRST_IN_WINDOW_DAYS = 30
# XP economy weights
XP_FIRST_UPLOAD = 100
XP_REPEAT_UPLOAD = 10
XP_STREAK_BONUS = 50
XP_PER_IMPROVED_MARKER = 30

BADGE_DEFS = [
    {"id": "first_report", "name": "First Drop", "description": "Upload your first blood report", "icon": "water"},
    {"id": "reports_3", "name": "Lab Legend", "description": "Analyze 3 reports", "icon": "flask"},
    {"id": "reports_5", "name": "Data Devotee", "description": "Analyze 5 reports", "icon": "albums"},
    {"id": "score_80", "name": "Vital Victor", "description": "Reach a health score of 80+", "icon": "shield-checkmark"},
    {"id": "score_90", "name": "Peak Performer", "description": "Reach a health score of 90+", "icon": "trophy"},
    {"id": "perfect_panel", "name": "Perfect Panel", "description": "Every marker in range in one panel (3+ markers)", "icon": "checkmark-done-circle"},
    {"id": "heart_hero", "name": "Heart Hero", "description": "All lipid markers in range", "icon": "heart"},
    # Fix (spec item #8): pulse icon is more tone-appropriate for a metabolic marker than "ice-cream".
    {"id": "sugar_steady", "name": "Sugar Steady", "description": "All diabetes markers in range", "icon": "pulse"},
    {"id": "comeback_kid", "name": "Comeback Kid", "description": "Bring a marker back into range", "icon": "trending-up"},
    {"id": "on_fire", "name": "On Fire", "description": "Keep a testing streak of 2+ reports", "icon": "flame"},
]


# ---------- Date helpers ----------
def effective_date(report: dict) -> str:
    """Canonical ordering key. Prefer report_date (the clinical event date) over uploaded_at
    (when the user happened to scan the paper). Always returns a YYYY-MM-DD string or ''.
    """
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


def _parse_uploaded_dt(iso: str) -> datetime:
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return datetime.now(timezone.utc)


# ---------- Scoring ----------
def compute_health_score(biomarkers: List[dict]) -> int:
    """Weighted average. Categories with higher clinical importance count more, so a bad
    lipid panel can no longer be masked by a stack of normal vitamins."""
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


# ---------- Badge helpers ----------
def _cat_all_normal(report: dict, keyword: str, min_markers: int = 2) -> bool:
    ms = [b for b in report.get("biomarkers", []) if keyword.lower() in (b.get("category") or "").lower()]
    return len(ms) >= min_markers and all(b.get("status") == "normal" for b in ms)


def _has_perfect_panel(report: dict) -> bool:
    groups: dict = {}
    for b in report.get("biomarkers", []):
        groups.setdefault(b.get("category", "Other"), []).append(b)
    return any(len(ms) >= 3 and all(m.get("status") == "normal" for m in ms) for ms in groups.values())


def _has_comeback(prev: dict, curr: dict) -> bool:
    prev_bad = {b["name"].strip().lower() for b in prev.get("biomarkers", []) if b.get("status") != "normal"}
    for b in curr.get("biomarkers", []):
        if b.get("status") == "normal" and b.get("name", "").strip().lower() in prev_bad:
            return True
    return False


def _improved_marker_count(prev: dict, curr: dict) -> int:
    """Count markers that moved from any non-normal status to normal vs the previous report.
    This is the behavior we want to reward (spec item #1).
    """
    prev_status = {
        (b.get("name") or "").strip().lower(): (b.get("status") or "normal")
        for b in prev.get("biomarkers", [])
    }
    improved = 0
    for b in curr.get("biomarkers", []):
        name = (b.get("name") or "").strip().lower()
        if not name:
            continue
        old = prev_status.get(name)
        if old and old != "normal" and b.get("status") == "normal":
            improved += 1
    return improved


# ---------- Profile computation ----------
def compute_profile(reports: List[dict]) -> dict:
    """Deterministically compute XP / level / streak / badges from reports.
    Reports may be in any input order - they're sorted canonically by effective_date.
    """
    reports = sort_by_effective_date(reports)

    xp = 0
    unlocked: dict = {}
    prev: Optional[dict] = None

    def unlock(badge_id: str, when: str):
        unlocked.setdefault(badge_id, when)

    for i, r in enumerate(reports):
        when = r.get("uploaded_at", "")  # badge unlock timestamps stay in real-time.
        curr_ed = _parse_effective(r)

        # (spec #1) Base upload XP - 100 for the first report inside any 30-day effective-date
        # window; 10 for subsequent uploads inside the same window.
        base_xp = XP_FIRST_UPLOAD
        if curr_ed is not None:
            for older in reports[:i]:
                oed = _parse_effective(older)
                if oed is not None and (curr_ed - oed).days <= FIRST_IN_WINDOW_DAYS:
                    base_xp = XP_REPEAT_UPLOAD
                    break
        xp += base_xp

        # Health-score bonus (max +10)
        xp += max(0, int(r.get("health_score", 0)) // 10)

        # Improvement + streak vs previous (by effective date)
        if prev is not None:
            prev_ed = _parse_effective(prev)
            if curr_ed is not None and prev_ed is not None:
                gap = (curr_ed - prev_ed).days
                if 0 <= gap <= STREAK_WINDOW_DAYS:
                    xp += XP_STREAK_BONUS
            improved = _improved_marker_count(prev, r)
            if improved:
                xp += XP_PER_IMPROVED_MARKER * improved
            if _has_comeback(prev, r):
                unlock("comeback_kid", when)

        # Milestone / score badges
        if i == 0:
            unlock("first_report", when)
        if i + 1 >= 3:
            unlock("reports_3", when)
        if i + 1 >= 5:
            unlock("reports_5", when)
        if r.get("health_score", 0) >= 80:
            unlock("score_80", when)
        if r.get("health_score", 0) >= 90:
            unlock("score_90", when)
        if _has_perfect_panel(r):
            unlock("perfect_panel", when)
        if _cat_all_normal(r, "lipid"):
            unlock("heart_hero", when)
        if _cat_all_normal(r, "diabetes", min_markers=1):
            unlock("sugar_steady", when)
        prev = r

    # Streak length - count trailing reports whose effective-date gap is within window.
    streak = 0
    if reports:
        streak = 1
        for i in range(len(reports) - 1, 0, -1):
            cur_ed = _parse_effective(reports[i])
            prv_ed = _parse_effective(reports[i - 1])
            if cur_ed is None or prv_ed is None:
                break
            gap = (cur_ed - prv_ed).days
            if 0 <= gap <= STREAK_WINDOW_DAYS:
                streak += 1
            else:
                break
    if streak >= 2:
        unlocked.setdefault("on_fire", reports[-1].get("uploaded_at", ""))

    level = xp // XP_PER_LEVEL + 1
    xp_in_level = xp % XP_PER_LEVEL
    scores = [r.get("health_score", 0) for r in reports]
    cumulative_score = round(sum(scores) / len(scores)) if scores else 0
    badges = [{**b, "unlocked": b["id"] in unlocked, "unlocked_at": unlocked.get(b["id"])} for b in BADGE_DEFS]
    return {
        "xp": xp,
        "level": level,
        "xp_in_level": xp_in_level,
        "xp_to_next": XP_PER_LEVEL - xp_in_level,
        "level_progress": xp_in_level / XP_PER_LEVEL,
        "streak": streak,
        "reports_count": len(reports),
        "latest_score": scores[-1] if scores else None,
        "best_score": max(scores) if scores else None,
        "cumulative_score": cumulative_score,
        "badges": badges,
        "badges_unlocked": len(unlocked),
        "badges_total": len(BADGE_DEFS),
    }
