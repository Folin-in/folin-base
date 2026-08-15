"""The gamification RULES: weights, the XP economy, cadence bands, and the badge catalogue.
Pure data - no computation lives here.

The one invariant that must never change: XP and badges reward BEHAVIOUR (uploading,
retesting on time, reading explanations, completing the profile), never BIOLOGY. A
score improving is not an achievement Folin hands out; the RETIRED_* lists are the
outcome-based badges that were removed for exactly that reason and must stay retired.
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
