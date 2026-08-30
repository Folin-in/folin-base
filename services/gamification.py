"""Gamification COMPUTATION: effective dates, health / document scores, retest cadence,
engagement XP, and the profile (level, XP, badges, streaks) built from a member's reports.
The rules it applies live in gamification_rules.py.
"""
from datetime import date, datetime, timezone
from typing import Dict, List, Optional
from services.scoring import compute_health_score  # noqa: F401  (re-exported)
from services.gamification_rules import (BADGE_DEFS, CATEGORY_WEIGHTS, EVENT_DAILY_CAP, EVENT_ONCE, EVENT_XP, FIRST_IN_WINDOW_DAYS, RETEST_MAX_DAYS, RETEST_MIN_DAYS, RETEST_TARGET_DAYS, RETIRED_BADGE_DEFS, RETIRED_BADGE_IDS, STATUS_WEIGHT, XP_FIRST_UPLOAD, XP_ON_TIME_RETEST, XP_PER_LEVEL, XP_REPEAT_UPLOAD, XP_TRACKED_CAP, XP_TRACKED_FLAGGED)


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


# ---------- Scoring ----------
# The lab score lives in services/scoring.py - it is panel-size independent and reads
# severity from the printed reference interval. Re-exported so callers keep one import.
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
