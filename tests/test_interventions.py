"""Chemistry that moved in a direction the pool cannot move on its own.

The equipment log records what the controller did; the event log records what
you said you did. This covers the gap: a service visit leaves no note and no
receipt in the controller, but it does leave a signature in the numbers, because
salt and chlorine can only rise if someone raised them.
"""
import interventions
import saltcell

GALLONS = 15000
MAX_GEN = saltcell.generation_ppm_per_day(GALLONS, 100.0, 1.0)   # 11.19 ppm/day


def _sys(hour, salt, day=10):
    return {"read_at": f"2026-08-{day:02d}T{hour:02d}:00:00+00:00", "salt_ppm": salt}


def _chem(day, free_cl):
    return {"latest_measure_time": f"2026-08-{day:02d}T11:00:00.000Z", "free_cl": free_cl}


# ---- salt ----

def test_a_step_rise_in_salt_is_an_addition():
    found = interventions.detect_salt_addition([_sys(8, 3200), _sys(14, 3520)], GALLONS)
    assert len(found) == 1
    assert found[0]["delta_ppm"] == 320.0
    assert round(found[0]["pounds"]) == 40
    assert "only rises if someone adds it" in found[0]["text"]


def test_the_weight_matches_the_bag_size_it_would_take():
    """320 ppm across 15,000 gallons is one 40 lb bag."""
    assert round(interventions.salt_pounds(320, 15000)) == 40
    assert round(interventions.salt_pounds(160, 15000)) == 20


def test_slow_concentration_is_evaporation_not_an_addition():
    """Evaporation raises salt too - but over days, not hours."""
    slow = [{"read_at": f"2026-08-{d:02d}T12:00:00+00:00", "salt_ppm": 3200 + d * 12}
            for d in range(10, 20)]
    assert interventions.detect_salt_addition(slow, GALLONS) == []


def test_a_large_rise_spread_over_too_long_is_not_counted():
    """The window matters as much as the size: a step is a bag, a drift isn't."""
    assert interventions.detect_salt_addition(
        [_sys(8, 3200, day=10), _sys(8, 3600, day=12)], GALLONS) == []


def test_salt_falling_is_dilution_and_belongs_to_the_other_detector():
    assert interventions.detect_salt_addition([_sys(8, 4350), _sys(14, 4000)], GALLONS) == []


def test_volume_is_optional():
    found = interventions.detect_salt_addition([_sys(8, 3200), _sys(14, 3520)])
    assert found[0]["pounds"] is None
    assert "lb of salt" not in found[0]["text"]


# ---- chlorine ----

def test_a_rise_beyond_the_cells_maximum_is_a_manual_addition():
    found = interventions.detect_chlorine_addition(
        [_chem(10, 2.0), _chem(11, 18.0)], MAX_GEN, GALLONS)
    assert len(found) == 1
    assert found[0]["unexplained_ppm"] > 0
    assert "added by hand" in found[0]["text"]


def test_a_rise_the_cell_could_have_produced_is_not_flagged():
    """The bound is deliberately generous - full output, nothing lost."""
    assert interventions.detect_chlorine_addition(
        [_chem(10, 2.0), _chem(11, 8.0)], MAX_GEN, GALLONS) == []


def test_measurement_noise_does_not_read_as_an_addition():
    assert interventions.detect_chlorine_addition(
        [_chem(10, 3.0), _chem(11, 3.3)], 0.0, GALLONS) == []


def test_chlorine_falling_is_never_an_addition():
    assert interventions.detect_chlorine_addition(
        [_chem(10, 9.8), _chem(11, 5.2)], MAX_GEN, GALLONS) == []


def test_a_pool_with_no_cell_flags_any_real_rise():
    found = interventions.detect_chlorine_addition(
        [_chem(10, 2.0), _chem(11, 6.0)], 0.0, GALLONS)
    assert len(found) == 1


# ---- the summary stays factual ----

def test_the_summary_counts_and_weighs_without_accusing():
    salt = interventions.detect_salt_addition([_sys(8, 3200), _sys(14, 3520)], GALLONS)
    chlorine = interventions.detect_chlorine_addition(
        [_chem(10, 2.0), _chem(11, 18.0)], MAX_GEN, GALLONS)
    text = interventions.summarize(salt, chlorine)

    assert "1 salt addition" in text and "40 lb" in text
    assert "done by a person" in text
    for loaded in ("should", "unnecessary", "wrong", "failed", "neglect"):
        assert loaded not in text.lower()


def test_no_detections_yields_no_summary():
    assert interventions.summarize([], []) is None
