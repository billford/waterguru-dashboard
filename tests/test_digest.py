from datetime import datetime, timedelta, timezone

import pytest

import digest
from db import mark_sent

SUNDAY = datetime(2026, 8, 9, 9, 0, tzinfo=timezone.utc)
MONDAY = SUNDAY + timedelta(days=1)

HISTORY = {
    "waterbodies": {
        "wb-1": {
            "name": "Pool",
            "latest": {"cassette_days_left": "10 days left", "battery_time_left": "4 months left"},
            "freshness": {"measure_stale": False},
        }
    }
}
SUMMARY = {"wb-1": {"text": "Chlorine is holding steady."}}
WEATHER = {"days": [
    {"date": "2026-08-10", "name": "Monday", "temp_f": 88, "good_swim_day": True},
    {"date": "2026-08-11", "name": "Tuesday", "temp_f": 62, "good_swim_day": False},
]}
ADVICE = {"days": {"2026-08-10": {"verdict": "great"}, "2026-08-11": {"verdict": "poor"}}}
CHLORINE = {"headline": "Chlorine is 4.0 ppm and drops below target Thu."}


def test_the_digest_only_goes_out_on_its_day(db):
    assert digest.is_due(SUNDAY, db)
    assert not digest.is_due(MONDAY, db)


def test_a_catch_up_run_does_not_send_a_second_copy(db):
    """A sleeping Mac can fire two runs the same day; that's one digest, not two."""
    mark_sent(db, digest._week_key(SUNDAY), SUNDAY.isoformat())
    assert not digest.is_due(SUNDAY, db)


def test_a_new_week_re_arms_the_digest(db):
    mark_sent(db, digest._week_key(SUNDAY), SUNDAY.isoformat())
    assert digest.is_due(SUNDAY + timedelta(days=7), db)


def test_the_digest_pulls_together_everything_the_run_produced():
    title, message = digest.build_digest(HISTORY, SUMMARY, WEATHER, ADVICE, CHLORINE, SUNDAY)
    assert title == "Pool: weekly summary"
    assert "holding steady" in message
    assert "drops below target" in message
    assert "Monday (88°)" in message
    assert "Tuesday" not in message      # poor verdict, not worth listing
    assert "Cassette 10d - order one" in message


def test_a_quiet_sensor_gets_called_out():
    history = {"waterbodies": {"wb-1": {
        "name": "Pool", "latest": {}, "freshness": {"measure_stale": True},
    }}}
    _, message = digest.build_digest(history, {}, None, None, None, SUNDAY)
    assert "hasn't produced a new measurement" in message


def test_missing_pieces_do_not_break_the_digest():
    """Ollama down or weather unreachable shouldn't cost you the whole digest."""
    _, message = digest.build_digest(HISTORY, None, None, None, None, SUNDAY)
    assert "Cassette" in message


def test_no_data_means_no_digest():
    assert digest.build_digest({"waterbodies": {}}, {}, {}, {}, {}, SUNDAY) is None


def test_a_forecast_with_no_good_days_says_so():
    weather = {"days": [{"date": "2026-08-11", "name": "Tuesday", "temp_f": 55, "good_swim_day": False}]}
    _, message = digest.build_digest(HISTORY, SUMMARY, weather, {}, CHLORINE, SUNDAY)
    assert "No standout swim days" in message
