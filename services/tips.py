"""The aggregated tips feed across a member's reports.

Three rules, in order:
1. SUPERSEDE - once the newest lab report re-measures a marker, its advice replaces
   anything an older report said about that marker. Older tips linked to a re-measured
   marker are dropped, not shown next to fresh (possibly contradictory) advice.
2. ALLERGY GUARD - deterministic, never AI-dependent. A tip naming a declared allergen
   is dropped outright (word-boundary match). The profile may have changed since the
   tip was written; stored advice can now contradict it.
3. AI RECONCILE - cross-report and profile contradictions ("eat more red meat" vs "cut
   saturated fat"; meat advice after switching to vegetarian). Cached by a hash of tips
   + profile so any change produces a fresh verdict. NEVER blocks the screen: a cached
   verdict applies instantly; otherwise the unfiltered list is served now and the
   verdict computed in the background for the next view. Fails open.
"""
import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

from agents import TipsReconciler
from agents.tools import TOOLS
from app.constants import DEFAULT_REPORT_TITLES, LEGACY_CATEGORY_MAP, SUGGESTION_CATEGORIES
from app.enums import ReportType
from app.models import Report
from core import cache
from services.gamification import sort_by_effective_date
from services.members import member_id_of, resolve_member_profile

logger = logging.getLogger(__name__)

MIN_TIPS_FOR_RECONCILE = 2
MIN_ALLERGEN_CHARS = 3
DEFAULT_CATEGORY = "lifestyle"


def normalize_category(cat: str | None) -> str:
    c = str(cat or "").strip().lower()
    c = LEGACY_CATEGORY_MAP.get(c, c)
    return c if c in SUGGESTION_CATEGORIES else DEFAULT_CATEGORY


def default_title(report_type: str | None) -> str:
    try:
        return DEFAULT_REPORT_TITLES[ReportType(str(report_type or ReportType.LAB))]
    except ValueError:
        return DEFAULT_REPORT_TITLES[ReportType.OTHER]


@dataclass
class TipsFeed:
    reports: list[dict] = field(default_factory=list)
    suggestions: list[dict] = field(default_factory=list)


class TipsService:
    def __init__(self):
        self._reconciler = TipsReconciler()
        self._profile_context = TOOLS["profile_context"]

    async def feed(self, user: dict, member_id: str | None) -> TipsFeed:
        mid = member_id_of(member_id)
        rows = await cache.get_reports(user["user_id"])
        docs = sort_by_effective_date([r for r in rows if r.get("member_id", "self") == mid], reverse=True)
        out = self._collect(docs)
        display_name, hp = await resolve_member_profile(user, mid)
        out.suggestions = self._allergy_guard(out.suggestions, hp)
        out.suggestions = await self._reconcile(out.suggestions, display_name or user.get("name", ""), hp,
                                                user["user_id"])
        return out

    # ------------------------------------------------------------------ rules

    @staticmethod
    def _collect(docs: list[dict]) -> TipsFeed:
        latest_lab = next((d for d in docs if (d.get("report_type") or ReportType.LAB) == ReportType.LAB), None)
        latest_markers = {(b.get("name") or "").strip().lower()
                          for b in (latest_lab.get("biomarkers") or [])} if latest_lab else set()
        latest_lab_id = str(latest_lab["id"]) if latest_lab else None
        seen: set[tuple[str, str]] = set()
        feed = TipsFeed()
        for d in docs:
            r = Report.from_row(d)
            title = r.title or default_title(r.report_type)
            feed.reports.append({"id": r.id, "title": title, "report_type": r.report_type,
                                 "report_date": r.report_date, "uploaded_at": r.uploaded_at})
            is_latest_lab = r.id == latest_lab_id
            for s in r.suggestions or []:
                key = (s.category or DEFAULT_CATEGORY, (s.title or "").strip().lower())
                if not key[1] or key in seen:
                    continue
                linked = (s.linked_marker or "").strip().lower()
                if not is_latest_lab and linked and linked in latest_markers:
                    continue
                seen.add(key)
                feed.suggestions.append({
                    "title": s.title, "body": s.body, "category": normalize_category(s.category),
                    "linked_marker": s.linked_marker, "report_id": r.id, "report_title": title,
                    "report_type": r.report_type,
                    "report_date": r.report_date or (r.uploaded_at[:10] if r.uploaded_at else None),
                })
        return feed

    @staticmethod
    def _allergy_guard(tips: list[dict], hp: dict) -> list[dict]:
        allergens = [str(a).strip().lower() for a in (hp.get("allergies") or [])
                     if len(str(a).strip()) >= MIN_ALLERGEN_CHARS]
        if not allergens or not tips:
            return tips
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in allergens) + r")\b")
        return [t for t in tips if not pattern.search(f"{t['title']} {t['body']}".lower())]

    async def _reconcile(self, tips: list[dict], name: str, hp: dict, user_id: str) -> list[dict]:
        if len(tips) < MIN_TIPS_FOR_RECONCILE:
            return tips
        ctx = self._profile_context(name, hp)
        key = hashlib.sha256((ctx + json.dumps([[t["title"], t["body"], t["report_date"]] for t in tips])).encode()).hexdigest()
        drop = cache.get_tips_verdict(key)
        if drop is None:
            asyncio.create_task(self._warm(key, ctx, list(tips), user_id))
            return tips
        if not drop:
            return tips
        dropset = set(drop)
        return [t for i, t in enumerate(tips) if i not in dropset]

    async def _warm(self, key: str, ctx: str, snapshot: list[dict], user_id: str) -> None:
        try:
            verdict = await self._reconciler.run(ctx, snapshot, user_id=user_id)
        except Exception as e:
            logger.warning("tips reconcile failed (leaving unfiltered): %s", e)
            verdict = []
        cache.set_tips_verdict(key, verdict)


tips = TipsService()
