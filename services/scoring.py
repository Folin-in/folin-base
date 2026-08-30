"""The lab health score.

Panels in the market are not the same size - a 40-check package and an 89-test package
both need to produce a number the user can compare against their own last report. Two
properties follow from that, and they are what this module exists to guarantee:

  1. Adding NORMAL markers must not move the score. A weighted average over markers has
     the opposite behaviour: every extra normal result dilutes the abnormal ones, so the
     bigger panel scores better for the same person. Here the panel is scored per body
     SYSTEM, and a system's score comes from what is wrong inside it, not from how many
     tests happened to be run.
  2. Adding ABNORMAL markers must not be free. Within a system the penalties stack with
     decay - five markers moving together (low MCV, high RDW, low iron ...) are usually
     one finding seen five ways, so the second and third cost less than the first, but
     never zero.

Severity is read from the printed reference interval rather than the status label: a
value 16% below the floor is not the same finding as one a hair under it, and the old
label-only weighting scored them identically.
"""
from dataclasses import dataclass

from agents.tools.sanity_bounds import parse_range, to_float
from app.enums import BiomarkerStatus
from services.gamification_rules import CATEGORY_WEIGHTS

# Points a single abnormal marker takes off its own system before severity scaling.
STATUS_PENALTY: dict[str, float] = {
    str(BiomarkerStatus.HIGH): 8.0,
    str(BiomarkerStatus.LOW): 8.0,
    str(BiomarkerStatus.BORDERLINE_HIGH): 3.5,
    str(BiomarkerStatus.BORDERLINE_LOW): 3.5,
}

# (deviation at or below this, multiplier). A reference interval is a 95% population range,
# so on a 100-marker panel roughly five results fall outside it in a person with nothing
# wrong. The first tier is that noise floor: a hair outside costs almost nothing, because
# almost always it means nothing.
SEVERITY_TIERS: tuple[tuple[float, float], ...] = ((0.05, 0.10), (0.20, 0.45), (0.50, 1.0), (1.50, 1.5))
SEVERITY_MAX = 2.0
SEVERITY_UNKNOWN = 1.0                     # unparseable range: neither excuse nor punish

# Markers that move together because they measure one thing, or are arithmetically tied.
# A differential adds to 100%, so a low neutrophil % FORCES a high lymphocyte % and a low
# NLR - one observation, three rows. Red-cell indices likewise describe a single picture.
# Scored as one finding, or a panel that reports the same thing five ways is penalised
# five times for it. First match wins, so narrower groups come first.
FINDING_GROUPS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # (finding, categories it may apply to - empty means any, marker-name keys)
    ("iron studies",     ("Vitamins & Minerals", "Complete Blood Count"),
                         ("iron", "tibc", "uibc", "transferrin", "ferritin")),
    ("eosinophils",      ("Complete Blood Count",), ("eosinophil",)),
    ("neutrophils",      ("Complete Blood Count",), ("neutrophil",)),
    ("lymphocytes",      ("Complete Blood Count",), ("lymphocyte",)),
    ("monocytes",        ("Complete Blood Count",), ("monocyte",)),
    ("basophils",        ("Complete Blood Count",), ("basophil",)),
    ("platelet indices", ("Complete Blood Count",),
                         ("platelet", "mpv", "pdw", "p-lcr", "p-lcc", "plateletcrit")),
    ("lipids",           ("Lipid Profile",),
                         ("cholesterol", "lipoprotein", "ldl", "hdl", "vldl", "triglycer")),
    # Scoped to the blood count: a urine microscopy RBC is a different sample entirely and
    # must never be read as a red-cell index.
    ("red cell indices", ("Complete Blood Count",),
                         ("mcv", "mch", "mchc", "rdw", "rbc", "red blood", "red cell",
                          "haematocrit", "hematocrit", "pcv", "haemoglobin", "hemoglobin")),
    ("liver enzymes",    ("Liver Function",),
                         ("ast", "alt", "sgot", "sgpt", "alkaline phos", "gamma-glutamyl", "ggt")),
    ("proteins",         ("Liver Function", "Other"), ("total protein", "albumin", "globulin")),
)

# A corroborating marker in the same finding adds a little, never another full penalty.
GROUP_ECHO = 0.15

# Small red cells WITH a normal haemoglobin and a raised red-cell count is a common,
# usually harmless carrier pattern in India: the cells are small, but there are more of
# them and oxygen-carrying capacity is intact. Scored as one mild finding instead of four
# marked ones. This asserts nothing about the cause - it only declines to alarm when the
# panel is internally consistent with a well person.
RED_CELL_FINDING = "red cell indices"
BENIGN_MICROCYTOSIS_CAP = 4.0
_MICRO_SMALL = ("mcv", "mch")
_MICRO_COUNT = ("rbc", "red blood", "red cell count")
_MICRO_OXYGEN = ("haemoglobin", "hemoglobin")
# "Mean Corpuscular Haemoglobin" and "Glycated Haemoglobin" both contain "haemoglobin" and
# are not the oxygen-carrying measure. Without this the pattern silently never matched.
_MICRO_NOT_OXYGEN = ("corpuscular", "mch", "glycated", "hba1", "concentration")

# Out of range in THIS direction carries no concern, so it should not cost anything.
# Low GGT says nothing about the liver; high HDL and high eGFR are good news that a
# two-sided interval flags anyway.
#
# Matching is by substring, which is sharp: a bare "cholesterol" key in the low list also
# matched "HDL Cholesterol" and silently forgave a genuinely low HDL. Keys are therefore
# specific, and the exception list below wins over both.
BENIGN_WHEN_LOW = ("ggt", "gamma-glutamyl", "alt", "ast", "sgot", "sgpt", "alkaline phos",
                   "ldl", "total cholesterol", "cholesterol, total", "triglycer",
                   "uric acid", "bilirubin", "neutrophil lymphocyte")
BENIGN_WHEN_HIGH = ("hdl", "egfr", "glomerular filtration")
# Names that look like a benign case but are not. Non-HDL rising IS a concern, and HDL
# falling IS a concern - the protective one behaves opposite to the rest of the panel.
NEVER_BENIGN = ("non-hdl", "non hdl", "nonhdl", "non- hdl", "hdl/ldl", "ldl/hdl",
                "cholesterol/high density")

# The nth distinct FINDING inside one system counts this much - never the nth marker.
REPEAT_DECAY: tuple[float, ...] = (1.0, 0.6, 0.4, 0.25, 0.15)
REPEAT_DECAY_TAIL = 0.1

SCORE_MAX = 100
SCORE_MIN = 0
DEFAULT_CATEGORY = "Other"

# Systems are compared against the average importance in CATEGORY_WEIGHTS, so a system of
# ordinary importance contributes its own penalty and the weighting only tilts around it.
CATEGORY_BASELINE = sum(CATEGORY_WEIGHTS.values()) / len(CATEGORY_WEIGHTS)
# Penalties are SUMMED across systems, never averaged: averaging lets six healthy systems
# drown one badly affected one, which is how a panel with a deranged CBC scored 93.
# Calibrated on real panels - see tests/test_scoring.py for the anchors this must hold.
PENALTY_DAMPING = 0.6


def deviation(value, ref_range: str) -> float | None:
    """How far outside its reference interval a value sits, normalised. 0.0 when inside.

    A two-sided range is read both ways - against the interval width (its own idea of
    normal spread) and against the bound that was crossed - and the worse of the two
    wins, because width alone understates a deficiency sitting inside a wide interval.
    A one-sided bound has no width, so it can only be read against the bound.
    """
    v = to_float(value)
    if v is None:
        return None
    lo, hi = parse_range(ref_range)
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        width = hi - lo
        if width <= 0:
            return None
        if lo <= v <= hi:
            return 0.0
        bound, gap = (lo, lo - v) if v < lo else (hi, v - hi)
        # Worse of the two readings. Width alone understates a deficiency inside a wide
        # interval - vitamin D 4.8 against 30-100 is 0.36 by width but 0.84 by bound.
        return max(gap / width, gap / abs(bound)) if bound else gap / width
    bound = hi if hi is not None else lo
    if not bound:
        return None
    if hi is not None and v > hi:
        return (v - hi) / abs(hi)
    if lo is not None and v < lo:
        return (lo - v) / abs(lo)
    return 0.0


def severity(dev: float | None) -> float:
    if dev is None:
        return SEVERITY_UNKNOWN
    for limit, mult in SEVERITY_TIERS:
        if dev <= limit:
            return mult
    return SEVERITY_MAX


def is_benign_direction(name, status) -> bool:
    """True when the marker is outside its interval the way that carries no concern."""
    n = str(name or "").lower()
    if any(k in n for k in NEVER_BENIGN):
        return False
    s = str(status or "")
    if s in (str(BiomarkerStatus.LOW), str(BiomarkerStatus.BORDERLINE_LOW)):
        return any(k in n for k in BENIGN_WHEN_LOW)
    if s in (str(BiomarkerStatus.HIGH), str(BiomarkerStatus.BORDERLINE_HIGH)):
        return any(k in n for k in BENIGN_WHEN_HIGH)
    return False


def finding_of(name, category: str = "") -> str:
    """Which finding this marker belongs to. Unmatched markers stand alone."""
    n = str(name or "").lower()
    cat = str(category or "").strip()
    for group, cats, keys in FINDING_GROUPS:
        if cats and cat and cat not in cats:
            continue
        if any(k in n for k in keys):
            return group
    return f"solo:{n.strip()}"


def marker_penalty(b: dict) -> float:
    """Points this marker takes off its system. 0 for anything not flagged."""
    base = STATUS_PENALTY.get(str(b.get("status") or BiomarkerStatus.NORMAL))
    if not base or is_benign_direction(b.get("name"), b.get("status")):
        return 0.0
    return base * severity(deviation(b.get("value"), b.get("ref_range") or ""))


def _status_of(markers: list[dict], keys: tuple[str, ...],
               exclude: tuple[str, ...] = ()) -> set[str]:
    out = set()
    for b in markers:
        n = str(b.get("name") or "").lower()
        if any(k in n for k in keys) and not any(x in n for x in exclude):
            out.add(str(b.get("status") or ""))
    return out


def has_benign_microcytosis(markers: list[dict]) -> bool:
    """Small cells, raised count, haemoglobin holding - the pattern, not a diagnosis."""
    small = _status_of(markers, _MICRO_SMALL)
    count = _status_of(markers, _MICRO_COUNT)
    oxygen = _status_of(markers, _MICRO_OXYGEN, exclude=_MICRO_NOT_OXYGEN)
    return (str(BiomarkerStatus.LOW) in small
            and str(BiomarkerStatus.HIGH) in count
            and oxygen == {str(BiomarkerStatus.NORMAL)})


def finding_penalties(markers: list[dict]) -> list[tuple[float, str]]:
    """Collapse markers to findings: the worst marker carries the finding, the rest echo."""
    groups: dict[str, list[float]] = {}
    for b in markers:
        if (p := marker_penalty(b)) > 0:
            groups.setdefault(finding_of(b.get("name"), b.get("category")), []).append(p)
    benign_red_cells = has_benign_microcytosis(markers)
    out = []
    for name, ps in groups.items():
        ps.sort(reverse=True)
        penalty = ps[0] + GROUP_ECHO * sum(ps[1:])
        if name == RED_CELL_FINDING and benign_red_cells:
            penalty = min(penalty, BENIGN_MICROCYTOSIS_CAP)
        out.append((penalty, name))
    return sorted(out, reverse=True)


def category_score(markers: list[dict]) -> float:
    """A system's own 0-100. Driven by the distinct findings in it, never by how many tests
    ran nor by how many ways one finding happened to be reported."""
    total = sum(p * (REPEAT_DECAY[i] if i < len(REPEAT_DECAY) else REPEAT_DECAY_TAIL)
                for i, (p, _) in enumerate(finding_penalties(markers)))
    return max(float(SCORE_MIN), SCORE_MAX - total)


@dataclass(frozen=True)
class ScoreBreakdown:
    """The score plus why, so the number can be explained instead of just displayed."""
    score: int
    categories: dict[str, float]
    flagged: int
    total: int

    @property
    def systems(self) -> int:
        return len(self.categories)

    def as_dict(self) -> dict:
        return {"score": self.score, "categories": self.categories, "flagged": self.flagged,
                "total": self.total, "systems": self.systems}


def score_breakdown(biomarkers: list[dict]) -> ScoreBreakdown:
    if not biomarkers:
        return ScoreBreakdown(score=0, categories={}, flagged=0, total=0)

    grouped: dict[str, list[dict]] = {}
    for b in biomarkers:
        grouped.setdefault((b.get("category") or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY,
                           []).append(b)

    scores = {cat: round(category_score(ms), 1) for cat, ms in grouped.items()}
    penalty = sum((SCORE_MAX - scores[cat]) * CATEGORY_WEIGHTS.get(cat, 1.0) / CATEGORY_BASELINE
                  for cat in grouped)
    overall = max(SCORE_MIN, min(SCORE_MAX, SCORE_MAX - penalty * PENALTY_DAMPING))

    flagged = sum(1 for b in biomarkers
                  if str(b.get("status") or BiomarkerStatus.NORMAL) != str(BiomarkerStatus.NORMAL))
    return ScoreBreakdown(score=int(round(overall)), categories=scores,
                          flagged=flagged, total=len(biomarkers))


def compute_health_score(biomarkers: list[dict]) -> int:
    return score_breakdown(biomarkers).score


BORDERLINE_BAND = 0.05           # within this fraction of the interval edge reads as borderline


def status_from_range(value, ref_range) -> str:
    """Status straight from the printed interval, with no model in the loop.

    Used to reinstate rows the extractor omitted: those come back with a value and a
    reference interval but no opinion, and a silent "normal" would be a lie.
    """
    dev = deviation(value, ref_range)
    if dev is None:
        return str(BiomarkerStatus.NORMAL)
    if dev <= 0:
        return str(BiomarkerStatus.NORMAL)
    v, (lo, hi) = to_float(value), parse_range(ref_range)
    below = lo is not None and v is not None and v < lo
    if dev <= BORDERLINE_BAND:
        return str(BiomarkerStatus.BORDERLINE_LOW if below else BiomarkerStatus.BORDERLINE_HIGH)
    return str(BiomarkerStatus.LOW if below else BiomarkerStatus.HIGH)
