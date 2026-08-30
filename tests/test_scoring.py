"""The lab health score. Runs offline - pure functions, no model, no DB.

These tests exist because the previous scorer was a weighted average over markers, which
had two behaviours nobody wants: a bigger panel scored better for the same person, and a
value far outside its range counted the same as one a hair outside. The properties below
are the contract; the anchors at the bottom are real panels, kept so a future tweak to the
constants has to face what it does to actual reports.

    ./venv/bin/python -m pytest tests/test_scoring.py -v
"""
import pytest

from services.scoring import (PENALTY_DAMPING, SEVERITY_TIERS, ScoreBreakdown, category_score,
                              compute_health_score, deviation, marker_penalty, score_breakdown,
                              severity)

CBC = "Complete Blood Count"


def m(status="normal", value="1", ref="1 - 10", category=CBC, name="X"):
    return {"name": name, "status": status, "value": value, "ref_range": ref, "category": category}


# ------------------------------------------------------------------ deviation

def test_value_inside_range_has_no_deviation():
    assert deviation("14", "13 - 17") == 0.0


def test_deviation_grows_with_distance_outside():
    d = [deviation(v, "83 - 101") for v in ("82.9", "75", "69.8", "55")]
    assert d == sorted(d), d


def test_unparseable_range_is_unknown_not_zero():
    """Unknown must not read as 'fine' - that would silently forgive every odd range."""
    assert deviation("5", "see report") is None
    assert deviation("abc", "1 - 10") is None


@pytest.mark.parametrize("value,ref", [("260", "<200"), ("43", ">50"), ("109", ">= 90")])
def test_one_sided_bounds_parse(value, ref):
    assert deviation(value, ref) is not None


def test_wide_interval_does_not_hide_a_deficiency():
    """Vitamin D 4.8 in 30-100 is only 0.36 of the interval width but 0.84 of the bound.
    Reading width alone scored profound deficiency as mild."""
    assert deviation("4.8", "30 - 100") > 0.5
    assert severity(deviation("4.8", "30 - 100")) > severity(deviation("27", "30 - 100"))


# ------------------------------------------------------------------ severity

def test_severity_is_monotonic():
    devs = [0.0, 0.05, 0.3, 1.0, 5.0]
    assert [severity(d) for d in devs] == sorted(severity(d) for d in devs)


def test_normal_marker_never_costs_anything():
    assert marker_penalty(m(status="normal", value="99", ref="1 - 10")) == 0.0


def test_borderline_costs_less_than_out_of_range():
    far = m(status="low", value="5", ref="10 - 20")
    near = m(status="borderline_low", value="5", ref="10 - 20")
    assert marker_penalty(near) < marker_penalty(far)


# ------------------------------------------------------------------ the two broken properties

def test_adding_normal_markers_cannot_change_the_score():
    """The old bug: every extra normal result diluted the abnormal ones, so ordering a
    bigger panel raised your score without anything about you changing."""
    sick = [m("low", "69.8", "83 - 101"), m("high", "15.6", "11.6 - 14"),
            m("low", "43", ">50", category="Lipid Profile")]
    base = compute_health_score(sick)
    for extra in (10, 28, 60):
        assert compute_health_score(sick + [m()] * extra) == base


def test_adding_abnormal_markers_always_lowers_the_score():
    """The old bug's twin: adding 6 real abnormalities RAISED a real report 81 -> 82."""
    sick = [m("low", "69.8", "83 - 101")]
    worse = sick + [m("high", "300", "1 - 10", category="Liver Function"),
                    m("low", "0.1", "1 - 10", category="Kidney Function")]
    assert compute_health_score(worse) < compute_health_score(sick)


def test_severity_changes_the_score():
    """Same marker, same label, different distance outside - must not score alike."""
    scores = [compute_health_score([m("low", v, "83 - 101")]) for v in ("82.9", "75", "69.8", "55")]
    assert scores == sorted(scores, reverse=True), scores
    assert len(set(scores)) == len(scores), f"severity tiers collapsed: {scores}"
    # One marker should move the number, but a single finding must not tank a whole panel.
    assert 5 <= scores[0] - scores[-1] <= 15, scores


# ------------------------------------------------------------------ system behaviour

def test_co_moving_markers_in_one_system_stack_with_decay():
    """Low MCV + high RDW + low iron is usually one finding seen three ways, so the third
    must cost less than the first - but never nothing."""
    one = category_score([m("low", "69.8", "83 - 101")])
    three = category_score([m("low", "69.8", "83 - 101")] * 3)
    assert three < one
    assert three > one - 3 * (100 - one)


def test_a_healthy_system_cannot_offset_a_sick_one():
    """Averaging across systems let six healthy ones drown a deranged CBC (score 93)."""
    sick_only = [m("low", "55", "83 - 101")]
    with_healthy = sick_only + [m(category=c) for c in
                                ("Liver Function", "Kidney Function", "Thyroid", "Electrolytes")]
    assert compute_health_score(with_healthy) == compute_health_score(sick_only)


def test_score_is_bounded():
    assert compute_health_score([m()] * 5) == 100
    catastrophic = [m("low", "0.001", "100 - 200", category=c)
                    for c in ("Lipid Profile", "Diabetes", "Kidney Function", "Liver Function")] * 6
    assert 0 <= compute_health_score(catastrophic) <= 100


def test_empty_panel_scores_zero_not_a_hundred():
    assert compute_health_score([]) == 0
    assert score_breakdown([]).systems == 0


# ------------------------------------------------------------------ breakdown

def test_breakdown_explains_the_number():
    bd = score_breakdown([m("low", "69.8", "83 - 101"), m(), m(category="Thyroid")])
    assert isinstance(bd, ScoreBreakdown)
    assert bd.total == 3 and bd.flagged == 1
    assert bd.systems == 2
    assert bd.categories[CBC] < bd.categories["Thyroid"]
    assert set(bd.as_dict()) == {"score", "categories", "flagged", "total", "systems"}


# ------------------------------------------------------------------ real-panel anchors

def _panel(flags, size, category=CBC):
    """`size` markers of which `flags` are moderately out of range."""
    return [m("high", "20", "1 - 10", category=category) for _ in range(flags)] + \
           [m(category=category) for _ in range(size - flags)]


def test_a_clean_panel_of_any_size_scores_full_marks():
    for size in (5, 31, 69, 89):
        assert compute_health_score(_panel(0, size)) == 100


def test_a_40_check_and_an_89_check_panel_agree_on_the_same_person():
    """The user's case: packages in the market run 40 to 89 tests. The same findings must
    not produce a different number just because the bigger package ran more normal tests."""
    assert compute_health_score(_panel(3, 40)) == compute_health_score(_panel(3, 89))


def test_more_findings_score_worse_regardless_of_panel_size():
    assert compute_health_score(_panel(6, 89)) < compute_health_score(_panel(3, 40))


def test_damping_and_tiers_stay_sane():
    """Guards the calibration: these constants were fitted against real panels."""
    assert 0 < PENALTY_DAMPING <= 1
    limits = [lim for lim, _ in SEVERITY_TIERS]
    assert limits == sorted(limits)


# ------------------------------------------------------------------ population fit (India)

def cbc(name, status="normal", value="1", ref="1 - 10"):
    return m(status=status, value=value, ref=ref, category=CBC, name=name)


def test_urine_rbc_is_not_a_red_cell_index():
    """Different sample entirely - grouping keys are scoped to the category they belong to."""
    from services.scoring import finding_of
    assert finding_of("RBC", "Urine") != finding_of("Red Blood Cells (RBC) Count", CBC)
    assert finding_of("Red Blood Cells (RBC) Count", CBC) == "red cell indices"


def test_small_cells_with_holding_haemoglobin_is_one_mild_finding():
    """Low MCV + high RBC + normal Hb is a common benign carrier pattern in India. It must
    not be scored like anaemia - four marked findings for one harmless picture."""
    benign = [cbc("Mean Corpuscular Volume (MCV)", "low", "69.8", "83 - 101"),
              cbc("Mean Corpuscular Hemoglobin (MCH)", "low", "22", "27 - 32"),
              cbc("Red Blood Cells (RBC) Count", "high", "6.39", "4.5 - 5.5"),
              cbc("Hemoglobin (Hb)", "normal", "14.1", "13 - 17")]
    anaemic = [cbc("Mean Corpuscular Volume (MCV)", "low", "69.8", "83 - 101"),
               cbc("Mean Corpuscular Hemoglobin (MCH)", "low", "22", "27 - 32"),
               cbc("Red Blood Cells (RBC) Count", "low", "3.2", "4.5 - 5.5"),
               cbc("Hemoglobin (Hb)", "low", "8.1", "13 - 17")]
    assert compute_health_score(benign) > compute_health_score(anaemic)
    assert compute_health_score(benign) >= 90


def test_the_pattern_needs_all_three_legs():
    """Haemoglobin dropping means it is no longer the reassuring picture."""
    from services.scoring import has_benign_microcytosis
    assert has_benign_microcytosis([cbc("MCV", "low"), cbc("RBC Count", "high"), cbc("Hemoglobin", "normal")])
    assert not has_benign_microcytosis([cbc("MCV", "low"), cbc("RBC Count", "high"), cbc("Hemoglobin", "low")])
    assert not has_benign_microcytosis([cbc("MCV", "low"), cbc("Hemoglobin", "normal")])


def test_low_hdl_is_never_forgiven_as_a_direction():
    """A bare 'cholesterol' key once matched 'HDL Cholesterol' and hid a real finding."""
    from services.scoring import is_benign_direction
    assert not is_benign_direction("High-Density Lipoprotein (HDL) Cholesterol", "low")
    assert is_benign_direction("High-Density Lipoprotein (HDL) Cholesterol", "high")
    assert not is_benign_direction("Non- HDL Cholesterol", "high")
    assert is_benign_direction("Cholesterol, Total", "low")
    assert is_benign_direction("Gamma-Glutamyl Transferase (GGT)", "low")
    assert not is_benign_direction("Gamma-Glutamyl Transferase (GGT)", "high")
