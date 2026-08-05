import anomaly


def reading(day, **fields):
    base = {
        "fetched_at": f"2026-08-{day:02d}T12:00:00+00:00",
        "latest_measure_time": f"2026-08-{day:02d}T11:00:00.000Z",
        "free_cl": 3.0,
        "ph": 7.5,
        "skimmer_flow": 13.0,
        "water_temp": 82.0,
    }
    base.update(fields)
    return base


def test_identical_readings_across_many_measurements_look_like_a_stuck_sensor():
    rows = [reading(d, free_cl=3.0) for d in range(1, 7)]
    found = anomaly.detect_flatline(rows, "free_cl")
    assert found and found["value"] == 3.0
    assert "free chlorine" in found["text"]


def test_a_pool_that_actually_drifts_is_not_flagged():
    rows = [reading(d, free_cl=3.0 + d * 0.1) for d in range(1, 7)]
    assert anomaly.detect_flatline(rows, "free_cl") is None


def test_a_short_history_is_not_enough_to_call_a_flatline():
    rows = [reading(d, free_cl=3.0) for d in range(1, 3)]
    assert anomaly.detect_flatline(rows, "free_cl") is None


def test_ph_moving_further_than_chemistry_allows_is_flagged():
    rows = [reading(1, ph=7.5), reading(2, ph=9.5)]
    found = anomaly.detect_jump(rows, "ph")
    assert found and found["from"] == 7.5 and found["to"] == 9.5


def test_a_believable_ph_drift_is_not_flagged():
    assert anomaly.detect_jump([reading(1, ph=7.5), reading(2, ph=7.8)], "ph") is None


def test_a_big_chlorine_jump_is_allowed_because_shocking_is_real():
    """Chlorine legitimately swings hard after dosing; pH doesn't."""
    assert anomaly.detect_jump([reading(1, free_cl=3.0), reading(2, free_cl=9.0)], "free_cl") is None


def test_a_wider_time_gap_permits_a_larger_change():
    close = [reading(1, ph=7.0), reading(2, ph=8.3)]
    apart = [reading(1, ph=7.0), reading(10, ph=8.3)]
    assert anomaly.detect_jump(close, "ph") is not None
    assert anomaly.detect_jump(apart, "ph") is None


def test_a_channel_that_stops_reporting_is_flagged():
    rows = [reading(1), reading(2)] + [reading(d, skimmer_flow=None) for d in range(3, 6)]
    found = anomaly.detect_missing(rows, "skimmer_flow")
    assert found and "skimmer flow" in found["text"]


def test_a_channel_that_never_reported_is_not_a_regression():
    rows = [reading(d, skimmer_flow=None) for d in range(1, 6)]
    assert anomaly.detect_missing(rows, "skimmer_flow") is None


def test_detect_runs_every_check_and_findings_have_stable_signatures():
    rows = [reading(d, free_cl=3.0) for d in range(1, 7)]
    findings = anomaly.detect(rows)
    assert findings
    assert all(anomaly.signature(f).startswith("anomaly:") for f in findings)
    assert anomaly.signature(findings[0]) == anomaly.signature(findings[0])


def test_a_healthy_series_produces_no_findings():
    rows = [reading(d, free_cl=3.0 + d * 0.15, ph=7.4 + d * 0.02, water_temp=80 + d) for d in range(1, 7)]
    assert anomaly.detect(rows, fields=("free_cl", "ph", "water_temp")) == []
