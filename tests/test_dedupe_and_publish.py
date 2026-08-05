"""The bug these cover: fetching twice a day against a sensor that measures once
a day made one measurement look like several readings."""
from conftest import snapshot

from db import dedupe_by_measurement
from publish import build_payload


def test_repeat_fetches_of_one_measurement_collapse_to_one_reading():
    rows = [
        snapshot(fetched_at="2026-08-04T00:00:00+00:00", latest_measure_time="2026-08-03T18:13:42.000Z"),
        snapshot(fetched_at="2026-08-04T12:00:00+00:00", latest_measure_time="2026-08-03T18:13:42.000Z"),
        snapshot(fetched_at="2026-08-05T00:00:00+00:00", latest_measure_time="2026-08-03T18:13:42.000Z"),
    ]
    assert len(dedupe_by_measurement(rows)) == 1


def test_distinct_measurements_are_kept():
    rows = [
        snapshot(latest_measure_time="2026-08-03T18:00:00.000Z"),
        snapshot(latest_measure_time="2026-08-04T18:00:00.000Z"),
    ]
    assert len(dedupe_by_measurement(rows)) == 2


def test_freshest_snapshot_wins_for_a_repeated_measurement():
    rows = [
        snapshot(fetched_at="2026-08-04T00:00:00+00:00", battery_pct_left=80.0),
        snapshot(fetched_at="2026-08-05T00:00:00+00:00", battery_pct_left=72.0),
    ]
    assert dedupe_by_measurement(rows)[0]["battery_pct_left"] == 72.0


def test_a_sensor_dropping_out_does_not_erase_the_value_earlier_fetches_had():
    """The flow sensor goes quiet while the rest of the pod keeps reporting.

    Taking the last row wholesale would lose the reading entirely.
    """
    rows = [
        snapshot(fetched_at="2026-08-04T00:00:00+00:00", skimmer_flow=13.0),
        snapshot(fetched_at="2026-08-05T00:00:00+00:00", skimmer_flow=None),
    ]
    assert dedupe_by_measurement(rows)[0]["skimmer_flow"] == 13.0


def test_a_fresher_non_null_value_still_wins():
    rows = [
        snapshot(fetched_at="2026-08-04T00:00:00+00:00", skimmer_flow=13.0),
        snapshot(fetched_at="2026-08-05T00:00:00+00:00", skimmer_flow=9.0),
    ]
    assert dedupe_by_measurement(rows)[0]["skimmer_flow"] == 9.0


def test_rows_without_a_measurement_time_fall_back_to_fetch_time():
    rows = [
        snapshot(fetched_at="2026-08-04T00:00:00+00:00", latest_measure_time=None),
        snapshot(fetched_at="2026-08-05T00:00:00+00:00", latest_measure_time=None),
    ]
    assert len(dedupe_by_measurement(rows)) == 2


def test_chemistry_is_deduped_but_temperature_is_not(now):
    """Water temp is read every fetch; chemistry isn't. They can't share a clock."""
    rows = [
        snapshot(fetched_at="2026-08-04T00:00:00+00:00", latest_measure_time="2026-08-03T18:00:00.000Z",
                 water_temp=81.0, free_cl_target=3.0, ph_target=7.6, skimmer_flow_target=None),
        snapshot(fetched_at="2026-08-04T12:00:00+00:00", latest_measure_time="2026-08-03T18:00:00.000Z",
                 water_temp=84.0, free_cl_target=3.0, ph_target=7.6, skimmer_flow_target=None),
        snapshot(fetched_at="2026-08-05T00:00:00+00:00", latest_measure_time="2026-08-03T18:00:00.000Z",
                 water_temp=82.0, free_cl_target=3.0, ph_target=7.6, skimmer_flow_target=None),
    ]
    wb = build_payload(rows, now)["waterbodies"]["wb-1"]

    assert len(wb["series"]) == 1
    assert len(wb["temp_series"]) == 3
    assert [p["water_temp"] for p in wb["temp_series"]] == [81.0, 84.0, 82.0]


def test_chemistry_points_are_timestamped_with_the_measurement_not_the_fetch(now):
    rows = [snapshot(fetched_at="2026-08-05T00:00:00+00:00",
                     latest_measure_time="2026-08-03T18:00:00.000Z",
                     free_cl_target=3.0, ph_target=7.6, skimmer_flow_target=None)]
    wb = build_payload(rows, now)["waterbodies"]["wb-1"]
    assert wb["series"][0]["t"] == "2026-08-03T18:00:00.000Z"
    assert wb["series"][0]["fetched_at"] == "2026-08-05T00:00:00+00:00"


def test_latest_block_carries_per_fetch_equipment_state(now):
    rows = [snapshot(free_cl_target=3.0, ph_target=7.6, skimmer_flow_target=None,
                     battery_pct_left=71.0, cassette_pct_left=64.0)]
    latest = build_payload(rows, now)["waterbodies"]["wb-1"]["latest"]
    assert latest["battery_pct_left"] == 71.0
    assert latest["cassette_pct_left"] == 64.0
