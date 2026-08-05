from datetime import datetime, timezone

from conftest import snapshot

from consumables import BATTERY_LEAD_DAYS, parse_time_left, runway, was_replaced
from freshness import (
    describe_age,
    describe_duration,
    freshness_for,
    parse_ts,
    stale_measurement_alert,
)


# ---- timestamp parsing: WaterGuru uses Z, we use +00:00 ----

def test_parses_both_timestamp_formats_in_this_data():
    assert parse_ts("2026-08-03T18:13:42.000Z").tzinfo is not None
    assert parse_ts("2026-08-05T12:00:11.779933+00:00").tzinfo is not None


def test_naive_timestamps_are_assumed_utc():
    assert parse_ts("2026-08-05T12:00:00").tzinfo == timezone.utc


def test_unparseable_timestamps_return_none():
    assert parse_ts("not a date") is None
    assert parse_ts(None) is None


# ---- freshness ----

def test_fresh_data_is_not_flagged(now):
    rows = [snapshot(fetched_at="2026-08-05T11:00:00+00:00",
                     latest_measure_time="2026-08-05T10:00:00.000Z")]
    f = freshness_for(rows, now)
    assert not f["fetch_stale"] and not f["measure_stale"]


def test_a_stalled_pipeline_is_flagged(now):
    rows = [snapshot(fetched_at="2026-08-02T12:00:00+00:00",
                     latest_measure_time="2026-08-02T11:00:00.000Z")]
    assert freshness_for(rows, now)["fetch_stale"]


def test_a_quiet_sensor_is_flagged_even_when_fetches_still_work(now):
    """The failure mode that looks healthiest: the API answers, the pod doesn't measure."""
    rows = [snapshot(fetched_at="2026-08-05T11:00:00+00:00",
                     latest_measure_time="2026-08-01T10:00:00.000Z")]
    f = freshness_for(rows, now)
    assert f["measure_stale"] and not f["fetch_stale"]


def test_no_rows_at_all_counts_as_stale(now):
    f = freshness_for([], now)
    assert f["fetch_stale"] and f["measure_stale"]


def test_stale_measurement_alert_only_fires_past_the_threshold(now):
    fresh = snapshot(latest_measure_time="2026-08-05T10:00:00.000Z")
    assert stale_measurement_alert(fresh, "Pool", now) is None

    stale = snapshot(latest_measure_time="2026-08-01T10:00:00.000Z")
    title, message = stale_measurement_alert(stale, "Pool", now)
    assert "no new readings" in title
    assert "cassette" in message


def test_age_wording_switches_to_days():
    assert describe_age(0.5) == "under an hour ago"
    assert describe_age(6) == "6h ago"
    assert describe_age(72) == "3 days ago"
    assert describe_age(None) == "unknown"


def test_durations_read_as_lengths_not_points_in_time():
    """"no new readings for 2 days", never "for 2 days ago"."""
    assert describe_duration(6) == "6h"
    assert describe_duration(72) == "3 days"


def test_the_stale_alert_reads_as_a_duration(now):
    stale = snapshot(latest_measure_time="2026-08-01T10:00:00.000Z")
    title, _ = stale_measurement_alert(stale, "Pool", now)
    assert "for 4 days" in title
    assert "ago" not in title


# ---- consumables ----

def test_parses_the_human_strings_waterguru_returns():
    assert parse_time_left("18 days left") == 18
    assert parse_time_left("1 day left") == 1
    assert parse_time_left("3 weeks left") == 21
    assert parse_time_left("4 months left") == 120
    assert parse_time_left("Replace now") is None
    assert parse_time_left(None) is None


def test_runway_projects_a_depletion_date(now):
    info = runway(snapshot(cassette_days_left="10 days left"), now)["cassette"]
    assert info["days_left"] == 10
    assert info["depletes_on"] == "2026-08-15"
    assert info["needs_reorder"]


def test_battery_gets_a_longer_lead_time_than_the_cassette(now):
    row = snapshot(cassette_days_left="18 days left", battery_time_left="18 days left")
    levels = runway(row, now)
    assert not levels["cassette"]["needs_reorder"]
    assert levels["battery"]["needs_reorder"]
    assert levels["battery"]["lead_days"] == BATTERY_LEAD_DAYS


def test_a_level_jumping_up_means_a_replacement_went_in():
    assert was_replaced(100.0, 8.0)
    assert not was_replaced(78.0, 80.0)   # normal drain
    assert not was_replaced(None, 80.0)   # unknown, don't guess
