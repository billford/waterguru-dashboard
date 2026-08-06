"""Measurements taken from water that hasn't circulated.

The incident: the pump died, water sat stagnant for 48 hours, and the pod
measured minutes after flow was restored. It reported calcium hardness 153 ppm
and stabilizer 10 ppm - against a hand test that came back in range - and the
pipeline turned that into "add 73 cups of calcium chloride" to 20,000 gallons.
"""
import json

import pytest
from conftest import snapshot

import alerts
import trust
from chlorine_forecast import build_forecast
from publish import build_payload
from tests.test_chlorine_forecast import NOW as FORECAST_NOW, WEATHER


def flow_outage(hours):
    return json.dumps([{
        "source": "FLOW_SENSOR", "condition": "OLD", "status": "RED",
        "text": f"No flow sensor report: {hours} hours",
    }])


# ---- reading the outage out of WaterGuru's own alert ----

def test_the_outage_length_comes_from_the_device_not_a_guess():
    assert trust.flow_outage_hours(flow_outage(48)) == 48.0
    assert trust.flow_outage_hours("[]") is None
    assert trust.flow_outage_hours(None) is None


def test_unparseable_alert_json_is_not_fatal():
    assert trust.flow_outage_hours("not json") is None


# ---- the automatic verdict ----

def test_a_measurement_after_a_long_outage_is_flagged():
    rows = [
        snapshot(fetched_at="2026-08-05T18:35:00+00:00", latest_measure_time="2026-08-03T18:13:42.000Z",
                 alerts_json=flow_outage(48)),
        snapshot(fetched_at="2026-08-05T21:34:00+00:00", latest_measure_time="2026-08-05T21:10:04.000Z",
                 alerts_json="[]"),
    ]
    verdicts = trust.evaluate(rows, annotations={})
    assert verdicts["2026-08-05T21:10:04.000Z"]["trusted"] is False
    assert "48h with no circulation" in verdicts["2026-08-05T21:10:04.000Z"]["reasons"][0]


def test_a_brief_outage_does_not_condemn_the_next_reading():
    rows = [
        snapshot(fetched_at="2026-08-05T18:35:00+00:00", latest_measure_time="2026-08-04T18:00:00.000Z",
                 alerts_json=flow_outage(6)),
        snapshot(fetched_at="2026-08-05T21:34:00+00:00", latest_measure_time="2026-08-05T21:10:04.000Z",
                 alerts_json="[]"),
    ]
    assert trust.evaluate(rows, annotations={})["2026-08-05T21:10:04.000Z"]["trusted"]


def test_readings_taken_with_healthy_circulation_are_trusted():
    rows = [
        snapshot(fetched_at="2026-08-04T12:00:00+00:00", latest_measure_time="2026-08-04T11:00:00.000Z"),
        snapshot(fetched_at="2026-08-05T12:00:00+00:00", latest_measure_time="2026-08-05T11:00:00.000Z"),
    ]
    assert all(v["trusted"] for v in trust.evaluate(rows, annotations={}).values())


def test_the_first_ever_reading_has_no_prior_evidence_against_it():
    rows = [snapshot(fetched_at="2026-08-03T19:00:00+00:00",
                     latest_measure_time="2026-08-03T18:13:42.000Z")]
    assert trust.evaluate(rows, annotations={})["2026-08-03T18:13:42.000Z"]["trusted"]


# ---- manual override ----

def test_a_hand_verdict_beats_the_automatic_one():
    """Someone standing at the pool with a test kit outranks a rule."""
    rows = [
        snapshot(fetched_at="2026-08-05T18:35:00+00:00", latest_measure_time="2026-08-03T18:13:42.000Z",
                 alerts_json=flow_outage(48)),
        snapshot(fetched_at="2026-08-05T21:34:00+00:00", latest_measure_time="2026-08-05T21:10:04.000Z",
                 alerts_json="[]"),
    ]
    annotations = {"2026-08-05T21:10": {"verdict": "trusted", "note": "hand test agreed"}}
    v = trust.evaluate(rows, annotations)["2026-08-05T21:10:04.000Z"]
    assert v["trusted"] is True
    assert v["source"] == "manual"


def test_a_clean_looking_reading_can_be_marked_suspect_by_hand():
    rows = [snapshot(fetched_at="2026-08-05T12:00:00+00:00",
                     latest_measure_time="2026-08-05T11:00:00.000Z")]
    annotations = {"2026-08-05T11:00": {"verdict": "suspect", "note": "hand test disagreed"}}
    v = trust.evaluate(rows, annotations)["2026-08-05T11:00:00.000Z"]
    assert v["trusted"] is False
    assert v["reasons"] == ["hand test disagreed"]


def test_timestamps_are_matched_by_prefix_so_they_can_be_typed():
    annotations = {"2026-08-05T21:10": {"verdict": "suspect"}}
    assert trust.find_annotation("2026-08-05T21:10:04.000Z", annotations) is not None
    assert trust.find_annotation("2026-08-06T20:56:00.000Z", annotations) is None


# ---- consequences: nothing untrusted reaches a model or a recommendation ----

def test_an_untrusted_reading_is_kept_out_of_the_burn_rate_fit():
    rows = [
        {"fetched_at": "2026-08-08T12:00:00+00:00", "latest_measure_time": "2026-08-08T11:00:00.000Z",
         "free_cl": 6.0, "water_temp": 80.0, "cya": 45.0},
        {"fetched_at": "2026-08-10T12:00:00+00:00", "latest_measure_time": "2026-08-10T11:00:00.000Z",
         "free_cl": 0.1, "water_temp": 80.0, "cya": 45.0},
    ]
    clean = build_forecast(rows, 3.0, WEATHER, FORECAST_NOW)
    guarded = build_forecast(rows, 3.0, WEATHER, FORECAST_NOW,
                             untrusted={"2026-08-10T11:00:00.000Z"})

    assert guarded["current"]["free_cl"] == 6.0     # fell back to the good reading
    assert guarded["excluded_measurements"] == 1
    assert clean["current"]["free_cl"] == 0.1


def test_every_reading_being_untrusted_yields_no_forecast_rather_than_a_bad_one():
    rows = [{"fetched_at": "2026-08-10T12:00:00+00:00",
             "latest_measure_time": "2026-08-10T11:00:00.000Z",
             "free_cl": 0.1, "water_temp": 80.0, "cya": 45.0}]
    f = build_forecast(rows, 3.0, WEATHER, FORECAST_NOW, untrusted={"2026-08-10T11:00:00.000Z"})
    assert f["available"] is False
    assert "unreliable" in f["headline"]


def test_the_dashboard_payload_carries_the_verdict():
    rows = [
        snapshot(fetched_at="2026-08-05T18:35:00+00:00", latest_measure_time="2026-08-03T18:13:42.000Z",
                 alerts_json=flow_outage(48)),
        snapshot(fetched_at="2026-08-05T21:34:00+00:00", latest_measure_time="2026-08-05T21:10:04.000Z",
                 alerts_json="[]"),
    ]
    wb = build_payload(rows)["waterbodies"]["wb-1"]
    assert wb["series"][-1]["trust"]["trusted"] is False


def test_a_dose_recommendation_from_a_suspect_reading_carries_a_warning():
    """The costliest thing this pipeline can get wrong: telling you to add 73 cups."""
    row = snapshot(status="RED", prev_status="GREEN", alerts_json=json.dumps([{
        "status": "RED", "text": "Calcium Hardness very low",
        "advice": "Add 73 cups of 90% concentration calcium chloride.",
    }]))
    verdict = {"trusted": False, "reasons": ["Taken after 48h with no circulation reported."]}
    _, message = alerts._status_alerts(row, "flounder", verdict)[0]

    assert "Add 73 cups" in message
    assert "TREAT WITH CAUTION" in message
    assert "Confirm with a test kit" in message


def test_a_trusted_reading_gets_no_scare_text():
    row = snapshot(status="RED", prev_status="GREEN", alerts_json=json.dumps([
        {"status": "RED", "text": "Calcium Hardness very low", "advice": "Add 73 cups."},
    ]))
    _, message = alerts._status_alerts(row, "flounder", {"trusted": True, "reasons": []})[0]
    assert "TREAT WITH CAUTION" not in message


# ---- annotation storage round-trips ----

def test_annotations_survive_a_write_and_read(tmp_path):
    path = tmp_path / "annotations.json"
    trust.save_annotations({"2026-08-05T21:10": {"verdict": "suspect", "note": "pump was dead"}}, path)
    loaded = trust.load_annotations(path)
    assert loaded["2026-08-05T21:10"]["note"] == "pump was dead"


def test_a_missing_annotations_file_is_simply_empty(tmp_path):
    assert trust.load_annotations(tmp_path / "nope.json") == {}


# ---- the card must not silently quote an older number than the tiles ----

def test_falling_back_to_an_older_reading_is_explained_not_silent():
    """Otherwise the outlook card just looks out of date next to the tiles."""
    rows = [
        {"fetched_at": "2026-08-08T12:00:00+00:00", "latest_measure_time": "2026-08-08T11:00:00.000Z",
         "free_cl": 9.8, "water_temp": 80.0, "cya": 45.0},
        {"fetched_at": "2026-08-10T12:00:00+00:00", "latest_measure_time": "2026-08-10T11:00:00.000Z",
         "free_cl": 5.2, "water_temp": 80.0, "cya": 45.0},
    ]
    f = build_forecast(rows, 3.0, WEATHER, FORECAST_NOW, untrusted={"2026-08-10T11:00:00.000Z"})
    assert f["current"]["free_cl"] == 9.8
    assert "5.2 ppm" in f["excluded_note"]
    assert "older than the one in the tiles" in f["excluded_note"]


def test_excluding_an_older_reading_needs_no_such_explanation():
    rows = [
        {"fetched_at": "2026-08-08T12:00:00+00:00", "latest_measure_time": "2026-08-08T11:00:00.000Z",
         "free_cl": 9.8, "water_temp": 80.0, "cya": 45.0},
        {"fetched_at": "2026-08-10T12:00:00+00:00", "latest_measure_time": "2026-08-10T11:00:00.000Z",
         "free_cl": 5.2, "water_temp": 80.0, "cya": 45.0},
    ]
    f = build_forecast(rows, 3.0, WEATHER, FORECAST_NOW, untrusted={"2026-08-08T11:00:00.000Z"})
    assert f["current"]["free_cl"] == 5.2
    assert f["excluded_note"] is None


# ---- low skimmer flow stops the sensor working, so it earns a push ----

def test_low_skimmer_flow_alerts_even_though_waterguru_calls_it_yellow(db, now):
    row = snapshot(skimmer_flow=3.0, alerts_json=json.dumps([
        {"source": "SKIMMER_FLOW", "condition": "LOW", "status": "YELLOW", "text": "Skimmer Flow low"},
    ]))
    fired = alerts._flow_alerts(db, row, "flounder", now)
    assert fired
    title, message = fired[0]
    assert "too low to measure" in title
    assert "3 gpm" in message
    assert "valves" in message


def test_healthy_flow_stays_quiet(db, now):
    row = snapshot(skimmer_flow=51.0, alerts_json="[]")
    assert alerts._flow_alerts(db, row, "flounder", now) == []


def test_low_flow_does_not_nag_every_run(db, now):
    row = snapshot(skimmer_flow=3.0, alerts_json=json.dumps([
        {"source": "SKIMMER_FLOW", "condition": "LOW", "status": "YELLOW", "text": "Skimmer Flow low"},
    ]))
    assert alerts._flow_alerts(db, row, "flounder", now)
    assert alerts._flow_alerts(db, row, "flounder", now) == []


# ---- adding water dilutes; it isn't chlorine demand ----

def test_a_top_up_is_not_fitted_as_a_burn_rate():
    """Diluting 8 ppm to 4 by adding water is not the pool consuming 4 ppm."""
    from chlorine_forecast import decay_segments

    rows = [
        {"fetched_at": "2026-08-08T12:00:00+00:00", "latest_measure_time": "2026-08-08T11:00:00.000Z",
         "free_cl": 8.0, "water_temp": 80.0, "cya": 45.0},
        {"fetched_at": "2026-08-09T12:00:00+00:00", "latest_measure_time": "2026-08-09T11:00:00.000Z",
         "free_cl": 4.0, "water_temp": 80.0, "cya": 45.0},
    ]
    assert len(decay_segments(rows)) == 1
    events = {"2026-08-09T06:00:00+00:00": {"type": "water_added"}}
    assert decay_segments(rows, events) == []


def test_a_top_up_outside_the_interval_leaves_the_segment_alone():
    from chlorine_forecast import decay_segments

    rows = [
        {"fetched_at": "2026-08-08T12:00:00+00:00", "latest_measure_time": "2026-08-08T11:00:00.000Z",
         "free_cl": 8.0, "water_temp": 80.0, "cya": 45.0},
        {"fetched_at": "2026-08-09T12:00:00+00:00", "latest_measure_time": "2026-08-09T11:00:00.000Z",
         "free_cl": 7.0, "water_temp": 80.0, "cya": 45.0},
    ]
    events = {"2026-08-01T06:00:00+00:00": {"type": "water_added"}}
    assert len(decay_segments(rows, events)) == 1


def test_events_and_measurement_verdicts_share_a_file_without_clobbering(tmp_path):
    path = tmp_path / "annotations.json"
    trust.save_annotations({"2026-08-05T21:10": {"verdict": "suspect"}}, path)
    trust.save_events({"2026-08-06T14:00:00+00:00": {"type": "water_added"}}, path)

    assert trust.load_annotations(path)["2026-08-05T21:10"]["verdict"] == "suspect"
    assert trust.load_events(path)["2026-08-06T14:00:00+00:00"]["type"] == "water_added"


def test_other_event_types_do_not_block_a_segment():
    assert not trust.water_added_between(
        trust._parse("2026-08-08T00:00:00+00:00"),
        trust._parse("2026-08-10T00:00:00+00:00"),
        {"2026-08-09T00:00:00+00:00": {"type": "something_else"}},
    )


# ---- a fill is a window, not an instant ----

def _fill(start, end=None):
    event = {"type": "water_added"}
    if end:
        event["ends_at"] = end
    return {start: event}


def _at(s):
    return trust._parse(s)


def test_an_interval_starting_mid_fill_is_excluded():
    """The interval most affected by dilution is the one that begins while the
    hose is still running - treating a fill as a point would keep it."""
    events = _fill("2026-08-06T14:28:00+00:00", "2026-08-06T16:22:00+00:00")
    assert trust.water_added_between(
        _at("2026-08-06T15:00:00+00:00"), _at("2026-08-07T15:00:00+00:00"), events)


def test_an_interval_wholly_containing_a_fill_is_excluded():
    events = _fill("2026-08-06T14:28:00+00:00", "2026-08-06T16:22:00+00:00")
    assert trust.water_added_between(
        _at("2026-08-05T00:00:00+00:00"), _at("2026-08-08T00:00:00+00:00"), events)


def test_an_interval_ending_before_the_fill_is_kept():
    events = _fill("2026-08-06T14:28:00+00:00", "2026-08-06T16:22:00+00:00")
    assert not trust.water_added_between(
        _at("2026-08-04T00:00:00+00:00"), _at("2026-08-06T12:00:00+00:00"), events)


def test_an_interval_starting_after_the_fill_is_kept():
    events = _fill("2026-08-06T14:28:00+00:00", "2026-08-06T16:22:00+00:00")
    assert not trust.water_added_between(
        _at("2026-08-06T18:00:00+00:00"), _at("2026-08-08T00:00:00+00:00"), events)


def test_a_fill_with_no_recorded_end_is_treated_as_an_instant():
    """The safe reading of an unfinished record: exclude the least, not the most."""
    events = _fill("2026-08-06T14:28:00+00:00")
    assert trust.water_added_between(
        _at("2026-08-06T00:00:00+00:00"), _at("2026-08-07T00:00:00+00:00"), events)
    assert not trust.water_added_between(
        _at("2026-08-06T15:00:00+00:00"), _at("2026-08-07T00:00:00+00:00"), events)
