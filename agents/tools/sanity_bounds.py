"""Free, instant plausibility guard for extracted biomarkers. Flags physiologically
impossible values and status/reference-range contradictions. Conservative on purpose:
only clear discrepancies are flagged, with a 10% margin so borderline cases pass."""
import re

from app.enums import BiomarkerStatus
from agents.tools.base import Tool

# (name keywords, unit keywords - empty means any unit, min, max)
PLAUSIBILITY: tuple[tuple[tuple[str, ...], tuple[str, ...], float, float], ...] = (
    (("haemoglobin", "hemoglobin"), ("g/dl", "gm/dl", "gm%", "gm", "g%"), 2.0, 25.0),
    (("glucose", "sugar (f", "sugar (pp", "blood sugar"), ("mg/dl",), 20.0, 1200.0),
    (("hba1c",), ("%",), 3.0, 20.0),
    (("creatinine",), ("mg/dl",), 0.1, 25.0),
    (("cholesterol", "ldl", "hdl", "triglycer", "vldl"), ("mg/dl",), 5.0, 1500.0),
    (("tsh",), (), 0.001, 200.0),
    (("vitamin d", "25-oh"), ("ng/ml",), 1.0, 200.0),
    (("vitamin b12", "b-12"), ("pg/ml",), 20.0, 5000.0),
)
BORDERLINE_MARGIN = 0.10

MSG_IMPLAUSIBLE = "This value looks physiologically unusual - please double-check it against your printed report."
MSG_NORMAL_BUT_OUT = "Marked in range, but the value looks outside the printed reference range - please double-check."
MSG_HIGH_BUT_IN = "Marked high, but the value looks within the printed reference range - please double-check."
MSG_LOW_BUT_IN = "Marked low, but the value looks within the printed reference range - please double-check."

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_RANGE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(-?\d+(?:\.\d+)?)")
_LT = re.compile(r"<\s*=?\s*(\d+(?:\.\d+)?)")
_GT = re.compile(r">\s*=?\s*(\d+(?:\.\d+)?)")

_HIGH = {BiomarkerStatus.HIGH, BiomarkerStatus.BORDERLINE_HIGH}
_LOW = {BiomarkerStatus.LOW, BiomarkerStatus.BORDERLINE_LOW}


def to_float(v) -> float | None:
    m = _NUM.search(str(v).replace(",", ""))
    return float(m.group()) if m else None


def parse_range(ref) -> tuple[float | None, float | None]:
    """'13-17', '13.0 - 17.0', '< 200', '>= 40' -> (lo, hi). En/em dashes accepted."""
    ref = str(ref or "").replace(",", "").strip()
    if m := _RANGE.search(ref):
        return float(m.group(1)), float(m.group(2))
    if m := _LT.search(ref):
        return None, float(m.group(1))
    if m := _GT.search(ref):
        return float(m.group(1)), None
    return None, None


class SanityBoundsTool(Tool):
    name = "sanity_bounds"

    def run(self, b: dict) -> str | None:
        """A user-facing note when the biomarker looks wrong, else None."""
        val = to_float(b.get("value"))
        if val is None:
            return None
        name = str(b.get("name") or "").lower()
        unit = str(b.get("unit") or "").lower().replace(" ", "")
        for names, units, lo, hi in PLAUSIBILITY:
            if any(k in name for k in names) and (not units or any(u in unit for u in units)):
                if not (lo <= val <= hi):
                    return MSG_IMPLAUSIBLE
                break
        lo, hi = parse_range(b.get("ref_range"))
        if lo is None and hi is None:
            return None
        status = b.get("status") or BiomarkerStatus.NORMAL
        below = lo is not None and val < lo * (1 - BORDERLINE_MARGIN)
        above = hi is not None and val > hi * (1 + BORDERLINE_MARGIN)
        inside = (lo is None or val >= lo) and (hi is None or val <= hi)
        if status == BiomarkerStatus.NORMAL and (below or above):
            return MSG_NORMAL_BUT_OUT
        if status in _HIGH and inside and (hi is None or val < hi * (1 - BORDERLINE_MARGIN)):
            return MSG_HIGH_BUT_IN
        if status in _LOW and inside and (lo is None or val > lo * (1 + BORDERLINE_MARGIN)):
            return MSG_LOW_BUT_IN
        return None
