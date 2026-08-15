"""Compare two lab reports marker by marker, and the Pro retest-reminder view.

Compare classifies each shared marker as improved / worsened / same. Status band is
the first signal; when the band did not change, distance outside the printed reference
range decides - so HbA1c 5.5 -> 5.9 (both borderline high) reads as "worsened", not
"same". Only lab reports take part: a prescription photo scores by heuristic and must
not move "your health score".
"""
import re
from dataclasses import dataclass, field
from datetime import date

from fastapi import HTTPException

from agents.tools.sanity_bounds import parse_range
from app.constants import COMPARE_ORDER, RETEST_AFTER_DAYS, SEVERITY
from app.enums import BiomarkerStatus, Plan, ReportType
from core import cache
from services.gamification import effective_date, sort_by_effective_date
from services.members import member_id_of

DEFAULT_SEVERITY = 1
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

MSG_PICK_TWO = "Pick two different reports to compare."
MSG_NOT_LAB = "One of the reports isn't a lab report we can compare."
MSG_RETEST_PRO = "Retest reminders are a Pro feature. Upgrade to unlock."


def valid_id(report_id: str | None) -> bool:
    return bool(report_id) and bool(UUID_RE.match(report_id))


def to_float(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def range_distance(value, ref_range: str) -> float | None:
    """How far a value sits OUTSIDE its printed range (0 when inside; None if unparseable)."""
    v = to_float(value)
    if v is None:
        return None
    lo, hi = parse_range(ref_range)
    if lo is None and hi is None:
        return None
    if lo is not None and v < lo:
        return lo - v
    if hi is not None and v > hi:
        return v - hi
    return 0.0


def report_ref(doc: dict) -> dict:
    return {"id": str(doc["id"]), "report_date": doc.get("report_date"), "uploaded_at": doc.get("uploaded_at"),
            "lab_name": doc.get("lab_name"), "health_score": doc.get("health_score", 0)}


@dataclass
class Comparison:
    available: bool
    reports_count: int = 0
    latest: dict | None = None
    against: dict | None = None
    older_reports: list[dict] = field(default_factory=list)
    score_delta: int = 0
    improved_count: int = 0
    worsened_count: int = 0
    same_count: int = 0
    markers: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        if not self.available:
            return {"available": False, "reports_count": self.reports_count}
        return {"available": True, "latest": self.latest, "against": self.against,
                "older_reports": self.older_reports, "score_delta": self.score_delta,
                "improved_count": self.improved_count, "worsened_count": self.worsened_count,
                "same_count": self.same_count, "markers": self.markers}


class CompareService:
    @staticmethod
    async def _lab_reports(user_id: str, member_id: str | None) -> list[dict]:
        mid = member_id_of(member_id)
        rows = await cache.get_reports(user_id)
        return sort_by_effective_date(
            [r for r in rows if r.get("member_id", "self") == mid and r.get("report_type") == ReportType.LAB],
            reverse=True)

    async def compare(self, user_id: str, member_id: str | None, a: str | None, b: str | None,
                      against: str | None) -> Comparison:
        docs = await self._lab_reports(user_id, member_id)
        if len(docs) < 2:
            return Comparison(available=False, reports_count=len(docs))
        latest, target, older = self._pick(docs, a, b, against)
        return self._diff(latest, target, older)

    @staticmethod
    def _pick(docs, a, b, against):
        if a and b:
            if not valid_id(a) or not valid_id(b) or a == b:
                raise HTTPException(status_code=400, detail=MSG_PICK_TWO)
            by_id = {str(d["id"]): d for d in docs}
            latest, target = by_id.get(a), by_id.get(b)
            if not latest or not target:
                raise HTTPException(status_code=404, detail=MSG_NOT_LAB)
            return latest, target, [d for d in docs if str(d["id"]) != a]
        latest, older = docs[0], docs[1:]
        target = next((d for d in older if str(d["id"]) == against), None) if valid_id(against) else None
        return latest, target or older[0], older

    @staticmethod
    def _classify(cur: dict, old: dict) -> str:
        cur_sev = SEVERITY.get(cur.get("status"), DEFAULT_SEVERITY)
        old_sev = SEVERITY.get(old.get("status"), DEFAULT_SEVERITY)
        if cur_sev != old_sev:
            return "improved" if cur_sev < old_sev else "worsened"
        ref = cur.get("ref_range") or old.get("ref_range") or ""
        cur_d, old_d = range_distance(cur.get("value"), ref), range_distance(old.get("value"), ref)
        if cur_sev == 0 or cur_d is None or old_d is None or cur_d == old_d:
            return "same"
        return "improved" if cur_d < old_d else "worsened"

    def _diff(self, latest: dict, target: dict, older: list[dict]) -> Comparison:
        old_map = {b.get("name", "").strip().lower(): b for b in target.get("biomarkers", [])}
        counts = {"improved": 0, "worsened": 0, "same": 0}
        markers = []
        for b in latest.get("biomarkers", []):
            ob = old_map.get(b.get("name", "").strip().lower())
            if not ob:
                continue
            change = self._classify(b, ob)
            counts[change] += 1
            cv, ov = to_float(b.get("value")), to_float(ob.get("value"))
            markers.append({
                "name": b.get("name", "").strip(), "unit": b.get("unit", ""),
                "category": b.get("category", "Other"), "ref_range": b.get("ref_range", ""),
                "current_value": b.get("value", ""), "previous_value": ob.get("value", ""),
                "delta": round(cv - ov, 2) if cv is not None and ov is not None else None,
                "current_status": b.get("status", BiomarkerStatus.NORMAL),
                "previous_status": ob.get("status", BiomarkerStatus.NORMAL),
                "change": change,
            })
        markers.sort(key=lambda m: (COMPARE_ORDER[m["change"]], m["name"]))
        return Comparison(
            available=True, latest=report_ref(latest), against=report_ref(target),
            older_reports=[report_ref(d) for d in older],
            score_delta=latest.get("health_score", 0) - target.get("health_score", 0),
            improved_count=counts["improved"], worsened_count=counts["worsened"], same_count=counts["same"],
            markers=markers)

    async def retest_reminders(self, user: dict, member_id: str | None) -> dict:
        plan = (user.get("preferences") or {}).get("plan") or Plan.FREE
        if plan != Plan.PRO:
            raise HTTPException(status_code=403, detail=MSG_RETEST_PRO)
        docs = await self._lab_reports(user["user_id"], member_id)
        if not docs:
            return {"due": False, "reminders": []}
        latest = docs[0]
        ed = effective_date(latest)
        try:
            days_ago = (date.today() - date.fromisoformat(ed)).days if ed else None
        except ValueError:
            days_ago = None
        flagged = [b for b in latest.get("biomarkers", []) if b.get("status") != BiomarkerStatus.NORMAL]
        due = bool(flagged) and days_ago is not None and days_ago >= RETEST_AFTER_DAYS
        return {
            "due": due, "days_since_last": days_ago, "last_report_date": ed,
            "reminders": [{"name": b.get("name"), "status": b.get("status"), "last_value": b.get("value"),
                           "unit": b.get("unit"), "category": b.get("category")} for b in flagged] if due else [],
        }


compare = CompareService()
