"""Two things the pool controller can tell us that the water sensor cannot.

Salt is conservative, so a fall in salt concentration can only mean water was
added — which makes the salt reading an accidental flowmeter for top-ups, and
removes the dependence on remembering to record one by hand.

A variable-speed pump holds its commanded RPM, so flow at a fixed speed measures
how hard the water finds it to get through. Falling flow at unchanged speed is
resistance building upstream — the failure that took two days to notice through
the water sensor alone.
"""
import pytest

import dilution
import restriction


# ---- dilution from salt ----

def test_a_salt_drop_is_read_as_dilution():
    found = dilution.detect_top_up(4350, 4000, gallons=15000)
    assert found is not None
    assert found["gallons_added"] == 1207          # 15000 * (1 - 4000/4350)
    assert "can only be dilution" in found["text"]


def test_the_dilution_fraction_is_the_share_of_new_water():
    assert dilution.dilution_fraction(4000, 2000) == pytest.approx(0.5)
    assert dilution.dilution_fraction(4000, 3800) == pytest.approx(0.05)


def test_salt_rising_is_evaporation_not_a_top_up():
    """Evaporation concentrates salt - the opposite of adding water."""
    assert dilution.detect_top_up(4000, 4300, gallons=15000) is None
    assert dilution.dilution_fraction(4000, 4300) is None


def test_sensor_noise_does_not_invent_a_top_up():
    """The cell's salt reading is noisy at the +/-100 ppm level."""
    assert dilution.detect_top_up(4350, 4300, gallons=15000) is None


def test_implausible_readings_are_ignored():
    assert dilution.detect_top_up(0, 4000) is None
    assert dilution.detect_top_up(4000, 0) is None
    assert dilution.detect_top_up(None, 4000) is None


def test_volume_is_optional_and_omitted_rather_than_guessed():
    found = dilution.detect_top_up(4350, 4000)
    assert found["gallons_added"] is None
    assert "gallons" not in found["text"].split("added")[1][:20]


def test_medians_are_compared_rather_than_single_readings():
    rows = ([{"read_at": f"2026-08-05T{h:02d}:00:00+00:00", "salt_ppm": 4350} for h in range(0, 12)]
            + [{"read_at": f"2026-08-07T{h:02d}:00:00+00:00", "salt_ppm": 4000} for h in range(0, 6)])
    before, after = dilution.recent_salt_pair(rows)
    assert (before, after) == (4350, 4000)


def test_too_little_history_yields_no_pair():
    assert dilution.recent_salt_pair([{"read_at": "2026-08-05T00:00:00+00:00", "salt_ppm": 4350}]) == (None, None)


# ---- restriction from pump telemetry ----

def _reading(i, gpm, rpm=2050, watts=610):
    return {"read_at": f"2026-08-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00",
            "pump_rpm": rpm, "pump_gpm": gpm, "pump_watts": watts}


def test_flow_falling_at_a_fixed_speed_is_reported():
    rows = [_reading(i, 51) for i in range(14)] + [_reading(14 + i, 38) for i in range(6)]
    found = restriction.detect(rows)
    assert found is not None
    assert found["baseline_gpm"] == 51
    assert found["recent_gpm"] == 38
    assert "resistance is upstream" in found["text"]
    assert "skimmer basket" in found["text"]


def test_a_severe_drop_is_distinguished_from_a_mild_one():
    mild = [_reading(i, 51) for i in range(14)] + [_reading(14 + i, 42) for i in range(6)]
    severe = [_reading(i, 51) for i in range(14)] + [_reading(14 + i, 30) for i in range(6)]
    assert restriction.detect(mild)["severity"] == "warning"
    assert restriction.detect(severe)["severity"] == "severe"


def test_steady_flow_is_not_reported():
    assert restriction.detect([_reading(i, 51) for i in range(25)]) is None


def test_normal_jitter_is_not_a_restriction():
    rows = [_reading(i, 51 + (i % 3) - 1) for i in range(25)]
    assert restriction.detect(rows) is None


def test_a_speed_change_is_not_mistaken_for_a_restriction():
    """Lower RPM means lower flow by design; comparing across speeds would read
    every schedule change as a fault."""
    rows = [_reading(i, 51, rpm=2050) for i in range(14)] + [_reading(14 + i, 20, rpm=1000) for i in range(6)]
    assert restriction.detect(rows) is None


def test_too_little_history_reports_why_rather_than_nothing():
    s = restriction.status([_reading(i, 51) for i in range(4)])
    assert s["available"] is False
    assert s["samples"] == 4
    assert s["samples_needed"] == restriction.MIN_BASELINE_SAMPLES + restriction.MIN_RECENT_SAMPLES
    assert s["current_gpm"] == 51


def test_readings_with_the_pump_off_are_excluded():
    rows = [_reading(i, 51) for i in range(20)] + [_reading(20, 0, rpm=0)]
    assert len(restriction.usable_readings(rows)) == 20


def test_status_surfaces_live_telemetry_even_without_a_finding():
    s = restriction.status([_reading(i, 51, watts=609) for i in range(25)])
    assert s["current_watts"] == 609
    assert s["finding"] is None
