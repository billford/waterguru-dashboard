from datetime import datetime, timezone

from chlorine_forecast import (
    DEFAULT_RATE_PPM_PER_DAY,
    build_forecast,
    decay_segments,
    fit_rate,
)


def reading(day, free_cl, temp=80.0, cya=45.0):
    return {
        "fetched_at": f"2026-08-{day:02d}T12:00:00+00:00",
        "latest_measure_time": f"2026-08-{day:02d}T11:00:00.000Z",
        "free_cl": free_cl,
        "water_temp": temp,
        "cya": cya,
    }


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

WEATHER = {
    "days": [
        {"date": "2026-08-10", "temp_f": 80},
        {"date": "2026-08-11", "temp_f": 80},
        {"date": "2026-08-12", "temp_f": 80},
    ]
}


# ---- rate fitting ----

def test_decline_between_readings_becomes_a_daily_rate():
    segs = decay_segments([reading(1, 5.0), reading(2, 4.0)])
    assert len(segs) == 1
    assert round(segs[0]["rate_ppm_per_day"], 2) == 1.0


def test_chlorine_going_up_is_dosing_and_is_excluded():
    """Adding chlorine says nothing about how fast the pool burns it."""
    segs = decay_segments([reading(1, 3.0), reading(2, 9.0), reading(3, 7.0)])
    assert len(segs) == 1
    assert round(segs[0]["rate_ppm_per_day"], 2) == 2.0


def test_hot_stretch_normalizes_to_a_lower_baseline_rate():
    hot = decay_segments([reading(1, 6.0, temp=90.0), reading(2, 4.0, temp=90.0)])[0]
    assert hot["normalized_rate"] < hot["rate_ppm_per_day"]


def test_too_little_history_falls_back_to_a_generic_rate_and_says_so():
    rate = fit_rate([reading(1, 5.0), reading(2, 4.0)])
    assert rate["source"] == "default"
    assert rate["ppm_per_day"] == DEFAULT_RATE_PPM_PER_DAY
    assert rate["confidence"] == "low"


def test_enough_declines_produce_a_fitted_rate():
    rows = [reading(d, 10.0 - d) for d in range(1, 6)]
    rate = fit_rate(rows)
    assert rate["source"] == "fitted"
    assert rate["segments"] == 4
    assert round(rate["ppm_per_day"], 2) == 1.0


def test_one_wild_reading_does_not_drag_the_fitted_rate():
    """Median, not mean - a single bad measurement shouldn't reset the model."""
    rows = [reading(1, 9.0), reading(2, 8.0), reading(3, 7.0), reading(4, 6.0), reading(5, 0.1)]
    assert round(fit_rate(rows)["ppm_per_day"], 2) == 1.0


# ---- projection ----

def test_projection_starts_today_not_at_a_stale_measurement():
    """The measurement can be days old; those days have already happened."""
    f = build_forecast([reading(8, 6.0)], 3.0, WEATHER, NOW)
    assert f["projection"][0]["date"] == "2026-08-10"
    assert all(p["date"] >= "2026-08-10" for p in f["projection"])


def test_decay_during_the_elapsed_days_is_still_applied():
    f = build_forecast([reading(8, 6.0)], 3.0, WEATHER, NOW)
    assert f["current"]["measurement_age_days"] == 2
    assert f["current"]["estimated_now"] < 6.0


def test_chlorine_never_projects_below_zero():
    f = build_forecast([reading(9, 0.5)], 3.0, WEATHER, NOW)
    assert all(p["free_cl"] >= 0 for p in f["projection"])


def test_days_beyond_the_weather_forecast_are_flagged_as_assumed():
    f = build_forecast([reading(10, 8.0)], 3.0, WEATHER, NOW)
    assert any(p["temp_assumed"] for p in f["projection"])
    assert not f["projection"][0]["temp_assumed"]


def test_high_chlorine_reports_when_it_returns_to_range():
    f = build_forecast([reading(10, 9.8)], 3.0, WEATHER, NOW)
    assert f["in_range_on"] is not None
    assert "back in range" in f["headline"]
    assert "no need to add" in f["headline"]


def test_low_chlorine_says_to_top_it_up():
    f = build_forecast([reading(10, 1.0)], 3.0, WEATHER, NOW)
    assert "needs topping up" in f["headline"]


def test_headline_leads_with_the_estimate_when_the_reading_is_old():
    f = build_forecast([reading(7, 9.8)], 3.0, WEATHER, NOW)
    assert "Last measured 9.8 ppm 3 days ago" in f["headline"]


# ---- the estimate is anchored at now, not at the next whole day ----

def test_a_measurement_taken_minutes_ago_barely_moves():
    """The bug: estimated_now read projection[0], which was *tomorrow*, so a
    fresh 5.2 ppm reading was reported as a day's worth of burn already gone."""
    just_now = datetime(2026, 8, 10, 12, 30, tzinfo=timezone.utc)
    rows = [{
        "fetched_at": "2026-08-10T12:00:00+00:00",
        "latest_measure_time": "2026-08-10T12:00:00.000Z",
        "free_cl": 5.2, "water_temp": 85.0, "cya": 45.0,
    }]
    f = build_forecast(rows, 3.0, WEATHER, just_now)
    assert f["current"]["estimated_now"] > 5.0
    assert f["projection"][0]["date"] == "2026-08-10"
    assert f["projection"][0]["free_cl"] == f["current"]["estimated_now"]


def test_the_projection_opens_on_today_flagged_as_an_estimate():
    f = build_forecast([reading(8, 6.0)], 3.0, WEATHER, NOW)
    assert f["projection"][0]["date"] == NOW.date().isoformat()
    assert f["projection"][0]["estimated"] is True


def test_a_three_day_old_reading_decays_by_about_three_days():
    fresh = build_forecast([reading(10, 8.0)], 3.0, WEATHER, NOW)["current"]["estimated_now"]
    old = build_forecast([reading(7, 8.0)], 3.0, WEATHER, NOW)["current"]["estimated_now"]
    assert old < fresh
    assert fresh > 7.5   # measured hours ago, barely moved


# ---- stabilizer ----

def test_an_unstabilized_pool_burns_chlorine_faster():
    stabilized = build_forecast([reading(10, 8.0, cya=50)], 3.0, WEATHER, NOW)
    bare = build_forecast([reading(10, 8.0, cya=5)], 3.0, WEATHER, NOW)
    assert bare["projection"][2]["free_cl"] < stabilized["projection"][2]["free_cl"]


def test_low_stabilizer_is_explained_rather_than_left_as_a_mystery():
    f = build_forecast([reading(10, 8.0, cya=10)], 3.0, WEATHER, NOW, cya_target=65)
    assert "Stabilizer is low at 10 ppm" in f["stabilizer_note"]
    assert "target 65" in f["stabilizer_note"]


def test_an_adequately_stabilized_pool_gets_no_lecture():
    assert build_forecast([reading(10, 8.0, cya=50)], 3.0, WEATHER, NOW)["stabilizer_note"] is None


def test_fitting_normalizes_stabilizer_so_it_is_not_double_counted():
    """A rate fitted from an unstabilized week already contains that fast burn."""
    bare = fit_rate([reading(d, 10.0 - d, cya=5) for d in range(1, 6)])
    stabilized = fit_rate([reading(d, 10.0 - d, cya=50) for d in range(1, 6)])
    assert bare["ppm_per_day"] < stabilized["ppm_per_day"]


def test_missing_stabilizer_data_changes_nothing():
    with_cya = build_forecast([reading(10, 8.0, cya=45)], 3.0, WEATHER, NOW)
    without = build_forecast([reading(10, 8.0, cya=None)], 3.0, WEATHER, NOW)
    assert with_cya["projection"][1]["free_cl"] == without["projection"][1]["free_cl"]


# ---- in-range uses the device's own band ----

def test_in_range_follows_the_devices_green_band_not_a_made_up_tolerance():
    """5.2 ppm against a 3.0 target is GREEN on the device (band 1.6-5.4)."""
    f = build_forecast([reading(10, 5.2)], 3.0, WEATHER, NOW, green_min=1.6, green_max=5.4)
    assert "in range" in f["headline"]
    assert "below the 3.0" not in f["headline"]


def test_above_the_band_reports_when_it_comes_back_down():
    f = build_forecast([reading(10, 9.8)], 3.0, WEATHER, NOW, green_min=1.6, green_max=5.4)
    assert "above the 5.4 ppm top of range" in f["headline"]
    assert f["in_range_on"] is not None


def test_below_the_band_asks_for_a_top_up():
    f = build_forecast([reading(10, 1.0)], 3.0, WEATHER, NOW, green_min=1.6, green_max=5.4)
    assert "below the 1.6 ppm bottom of range" in f["headline"]


def test_without_a_band_it_falls_back_to_the_target():
    f = build_forecast([reading(10, 9.8)], 3.0, WEATHER, NOW)
    assert f["in_range_on"] is not None


def test_no_readings_yields_an_unavailable_forecast():
    assert build_forecast([], 3.0, WEATHER, NOW)["available"] is False


def test_works_with_no_weather_data_at_all():
    f = build_forecast([reading(10, 8.0)], 3.0, None, NOW)
    assert f["available"] and f["projection"]
