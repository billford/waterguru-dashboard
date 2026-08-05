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
