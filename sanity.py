"""Free, instant code-level sanity guardrails for extracted biomarkers.
Flags physiologically implausible values and status/ref-range contradictions.
Conservative by design - only clear discrepancies get flagged."""
import re

# (name keywords, unit keywords - empty means any unit, min, max)
PLAUSIBILITY: list[tuple[tuple[str, ...], tuple[str, ...], float, float]] = [
    (("haemoglobin", "hemoglobin"), ("g/dl", "gm/dl", "gm%", "gm", "g%"), 2.0, 25.0),
    (("glucose", "sugar (f", "sugar (pp", "blood sugar"), ("mg/dl",), 20.0, 1200.0),
    (("hba1c",), ("%",), 3.0, 20.0),
    (("creatinine",), ("mg/dl",), 0.1, 25.0),
    (("cholesterol", "ldl", "hdl", "triglycer", "vldl"), ("mg/dl",), 5.0, 1500.0),
    (("tsh",), (), 0.001, 200.0),
    (("vitamin d", "25-oh"), ("ng/ml",), 1.0, 200.0),
    (("vitamin b12", "b-12"), ("pg/ml",), 20.0, 5000.0),
]


def _to_float(v) -> float | None:
    try:
        m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
        return float(m.group()) if m else None
    except Exception:
        return None


def _parse_range(ref) -> tuple[float | None, float | None]:
    """Parse '13-17', '13.0 - 17.0', '< 200', '>= 40' → (lo, hi)."""
    ref = str(ref or "").replace(",", "").strip()
    # –/— = en/em dash - real lab reports print ranges like "13 - 17" with either.
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(-?\d+(?:\.\d+)?)", ref)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"<\s*=?\s*(\d+(?:\.\d+)?)", ref)
    if m:
        return None, float(m.group(1))
    m = re.search(r">\s*=?\s*(\d+(?:\.\d+)?)", ref)
    if m:
        return float(m.group(1)), None
    return None, None


def sanity_note(b: dict) -> str | None:
    """Return a user-facing note when the biomarker looks wrong, else None."""
    val = _to_float(b.get("value"))
    if val is None:
        return None
    name = str(b.get("name") or "").lower()
    unit = str(b.get("unit") or "").lower().replace(" ", "")

    for names, units, lo, hi in PLAUSIBILITY:
        if any(k in name for k in names) and (not units or any(u in unit for u in units)):
            if not (lo <= val <= hi):
                return "This value looks physiologically unusual - please double-check it against your printed report."
            break

    lo, hi = _parse_range(b.get("ref_range"))
    if lo is None and hi is None:
        return None
    status = b.get("status") or "normal"
    # 10% margins keep borderline cases from false-flagging.
    clearly_below = lo is not None and val < lo * 0.9
    clearly_above = hi is not None and val > hi * 1.1
    clearly_inside = (lo is None or val >= lo) and (hi is None or val <= hi)
    if status == "normal" and (clearly_below or clearly_above):
        return "Marked in range, but the value looks outside the printed reference range - please double-check."
    if status in ("high", "borderline_high") and clearly_inside and (hi is None or val < hi * 0.9):
        return "Marked high, but the value looks within the printed reference range - please double-check."
    if status in ("low", "borderline_low") and clearly_inside and (lo is None or val > lo * 1.1):
        return "Marked low, but the value looks within the printed reference range - please double-check."
    return None


if __name__ == "__main__":
    # Self-check: run `python sanity.py`
    assert sanity_note({"name": "Haemoglobin", "value": "142", "unit": "g/dL", "ref_range": "13-17", "status": "normal"})  # implausible
    assert sanity_note({"name": "Glucose (F)", "value": "250", "unit": "mg/dL", "ref_range": "70-100", "status": "normal"})  # contradiction
    assert sanity_note({"name": "TSH", "value": "2.1", "unit": "mIU/L", "ref_range": "0.4 - 4.2", "status": "high"}) is not None  # marked high but inside
    assert sanity_note({"name": "Haemoglobin", "value": "14.2", "unit": "g/dL", "ref_range": "13-17", "status": "normal"}) is None
    assert sanity_note({"name": "LDL", "value": "128", "unit": "mg/dL", "ref_range": "< 130", "status": "borderline_high"}) is None  # borderline tolerance
    print("sanity self-check passed")
