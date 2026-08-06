import pytest
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


def test_one_measured_interval_beats_a_generic_rate():
    """This pool's own data, however thin, is better than a generic figure -
    but it is labelled partial rather than dressed up as a fit."""
    rate = fit_rate([reading(1, 5.0), reading(2, 4.0)])
    assert rate["source"] == "partial"
    assert rate["ppm_per_day"] == pytest.approx(1.0, abs=0.01)
    assert rate["confidence"] == "low"


def test_no_data_at_all_falls_back_to_generic_only_without_a_cell():
    rate = fit_rate([reading(1, 5.0)])
    assert rate["source"] == "default"
    assert rate["ppm_per_day"] == DEFAULT_RATE_PPM_PER_DAY


def test_no_data_and_a_generating_cell_yields_no_rate_at_all():
    """The generic rate is a NET decline for a pool with no generator. Pairing
    it with a generation term double-counted: a measurably flat pool projected
    to 41 ppm in 14 days while the headline called it steady."""
    rate = fit_rate([reading(1, 5.0)], generation=4.5)
    assert rate["ppm_per_day"] is None
    assert rate["source"] == "none"


def test_a_generating_cell_with_no_measured_demand_refuses_to_project():
    system = {"volume_gallons": 15000, "chlorinator_output_pct": 40,
              "pump_runtime_fraction": 0.99, "runtime_source": "declared"}
    f = build_forecast([reading(10, 3.0)], 3.0, WEATHER, NOW, system=system)
    assert f["available"] is False
    assert "hasn't been measured yet" in f["headline"]
    assert f["current"]["free_cl"] == 3.0


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


def test_the_burn_model_runs_on_water_temperature_not_air():
    """The fit normalizes by water temperature; the projection must re-apply the
    same quantity or the divide and multiply don't cancel. Projecting on the NWS
    air forecast overstated a 2.0 ppm/day burn as 3.2."""
    rows = [reading(d, 30.0 - 2.0 * d, temp=85.0) for d in range(1, 5)]
    hot_air = {"days": [{"date": f"2026-08-{d:02d}", "temp_f": 95} for d in range(10, 25)]}
    f = build_forecast(rows, 3.0, hot_air, NOW)

    assert all(p["water_temp_f"] == 85.0 for p in f["projection"])
    daily_loss = f["projection"][0]["free_cl"] - f["projection"][1]["free_cl"]
    assert daily_loss == pytest.approx(2.0, abs=0.05)


def test_chlorine_climbing_out_of_range_is_reported():
    """There was a "first day back in range" and a "first day below floor" but
    no "first day above the ceiling", so a rising projection read as holding."""
    rising = [{"date": f"2026-08-{d:02d}", "free_cl": v}
              for d, v in zip(range(10, 18), [4.2, 4.9, 5.6, 6.3, 7.0, 7.7, 8.4, 9.1])]
    from chlorine_forecast import _first_date_where, _headline

    assert _first_date_where(rising, lambda v: v > 5.4) == "2026-08-12"
    headline = _headline(4.2, 4.2, 3.0, 5.4, 1.6, "2026-08-10", None,
                         {"confidence": "medium"}, 0, above_on="2026-08-12")
    assert "climbing past 5.4" in headline
    assert "making more than the pool uses" in headline


def test_demand_is_un_normalized_by_temperature_as_well_as_stabilizer():
    """gross_loss restored only CYA, leaving demand pinned at the 80F reference -
    understating it 2x at 95F water, and halving the recommended output."""
    rows = [reading(d, 20.0 - 1.0 * d, temp=95.0) for d in range(1, 6)]
    system = {"volume_gallons": 15000, "chlorinator_output_pct": 40,
              "pump_runtime_fraction": 0.99, "runtime_source": "declared"}
    f = build_forecast(rows, 3.0, WEATHER, NOW, system=system)
    cell = f["salt_cell"]

    from chlorine_forecast import _temp_factor
    expected = f["rate"]["ppm_per_day"] * _temp_factor(95.0)
    assert cell["gross_loss_ppm_per_day"] == pytest.approx(expected, abs=0.02)


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


# ---- salt cell generation ----

SYSTEM = {"volume_gallons": 15000, "chlorinator_output_pct": 50,
          "pump_runtime_fraction": 0.35, "runtime_source": "measured"}


def test_modelling_generation_leaves_a_fitted_projection_unchanged():
    """The invariant that explains why the decay-only model was never visibly
    wrong: a rate fitted from observed data already contains the cell's output,
    so adding generation and adding it back into demand cancel out. The value of
    separating them is answering "what if the output changed?", not moving this
    projection."""
    rows = [reading(d, 10.0 - d) for d in range(1, 6)]
    without = build_forecast(rows, 3.0, WEATHER, NOW)
    with_cell = build_forecast(rows, 3.0, WEATHER, NOW, system=SYSTEM)
    assert with_cell["projection"][3]["free_cl"] == pytest.approx(
        without["projection"][3]["free_cl"], abs=0.05)


def test_demand_is_larger_than_the_observed_decline_when_a_cell_is_running():
    """A pool declining 1 ppm/day while its cell adds 2 really consumes 3."""
    rows = [reading(d, 10.0 - d) for d in range(1, 6)]
    plain = build_forecast(rows, 3.0, WEATHER, NOW)["rate"]["ppm_per_day"]
    with_cell = build_forecast(rows, 3.0, WEATHER, NOW, system=SYSTEM)["rate"]["ppm_per_day"]
    assert with_cell > plain


def test_a_rise_the_cell_can_explain_is_used_rather_than_discarded():
    """With an overproducing cell most intervals rise; discarding them all left
    the model unable to ever fit a rate."""
    from chlorine_forecast import decay_segments

    rising = [reading(1, 5.0), reading(2, 6.0)]
    assert decay_segments(rising) == []
    assert len(decay_segments(rising, {}, generation=5.0)) == 1


def test_a_rise_too_large_for_the_cell_is_still_treated_as_dosing():
    from chlorine_forecast import decay_segments

    shocked = [reading(1, 3.0), reading(2, 20.0)]
    assert decay_segments(shocked, {}, generation=5.0) == []


def test_no_output_recommendation_without_a_measured_demand():
    """A guess compounded with a guess would confidently say "turn it up" while
    chlorine sits over the top of range."""
    f = build_forecast([reading(10, 7.3)], 3.0, WEATHER, NOW, system=SYSTEM)
    cell = f["salt_cell"]
    assert cell["recommended_output_pct"] is None
    assert "hasn't been measured yet" in cell["caveat"]


def test_a_recommendation_appears_once_both_inputs_are_measured():
    rows = [reading(d, 10.0 - d) for d in range(1, 6)]
    cell = build_forecast(rows, 3.0, WEATHER, NOW, system=SYSTEM)["salt_cell"]
    assert cell["recommended_output_pct"] is not None
    assert cell["caveat"] is None


def test_assumed_runtime_blocks_the_recommendation():
    """Runtime neither measured nor declared: an 8h vs 24h schedule is a 3x
    swing, so a percentage built on the assumption isn't worth stating."""
    rows = [reading(d, 10.0 - d) for d in range(1, 6)]
    assumed = {**SYSTEM, "pump_runtime_fraction": 1.0, "runtime_source": None}
    cell = build_forecast(rows, 3.0, WEATHER, NOW, system=assumed)["salt_cell"]
    assert cell["recommended_output_pct"] is None
    assert "pump runtime" in cell["caveat"]


def test_no_controller_data_means_no_salt_cell_block():
    assert build_forecast([reading(10, 7.3)], 3.0, WEATHER, NOW)["salt_cell"] is None
