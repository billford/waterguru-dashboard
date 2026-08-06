"""Missing data must produce "unknown", never "fine".

The review found the same shape of bug in four places: an absent value taking a
path that reports health. A green tile for a reading that doesn't exist, a
never-measured panel reporting fresh, a malformed annotation marking a suspect
reading trusted, and an empty API response exiting zero.
"""
import json

import pytest
from conftest import snapshot

import trust
from freshness import freshness_for

FLOW48 = json.dumps([{"source": "FLOW_SENSOR", "condition": "OLD", "status": "RED",
                      "text": "No flow sensor report: 48 hours"}])
KEY = "2026-08-05T21:10:04.000Z"
ROWS = [
    snapshot(fetched_at="2026-08-05T18:35:00+00:00",
             latest_measure_time="2026-08-03T18:13:42.000Z", alerts_json=FLOW48),
    snapshot(fetched_at="2026-08-05T21:34:00+00:00", latest_measure_time=KEY, alerts_json="[]"),
]


# ---- annotations must not fail open ----

@pytest.mark.parametrize("annotation, why", [
    ({"note": "topped up"}, "a note that says nothing about trust"),
    ({"verdict": "unreliable"}, "an unrecognised verdict"),
    ({"verdict": ""}, "an empty verdict"),
    ({}, "an empty annotation"),
])
def test_a_malformed_annotation_does_not_un_flag_a_bad_reading(annotation, why):
    v = trust.evaluate(ROWS, {"2026-08-05T21:10": annotation})[KEY]
    assert v["trusted"] is False, why
    assert "no circulation" in v["reasons"][0]


def test_an_explicit_verdict_still_works_and_is_case_insensitive():
    for value in ("suspect", "SUSPECT", " Suspect "):
        v = trust.evaluate(ROWS, {"2026-08-05T21:10": {"verdict": value}})[KEY]
        assert v["trusted"] is False and v["source"] == "manual"


def test_an_explicit_trusted_verdict_keeps_the_reason_it_overrode():
    """The dashboard should be able to say *what* was overridden."""
    v = trust.evaluate(ROWS, {"2026-08-05T21:10": {"verdict": "trusted", "note": "hand test agreed"}})[KEY]
    assert v["trusted"] is True
    assert v["reasons"][0] == "hand test agreed"
    assert any("overrides" in r for r in v["reasons"])


def test_a_note_only_annotation_still_attaches_its_note():
    v = trust.evaluate(ROWS, {"2026-08-05T21:10": {"note": "hose was running"}})[KEY]
    assert v["note"] == "hose was running"
    assert v["trusted"] is False


# ---- annotation keys must identify one measurement ----

@pytest.mark.parametrize("key", ["2", "2026", "2026-08", "2026-08-05"])
def test_a_vague_key_matches_nothing(key):
    """"2" previously applied one verdict to every reading ever taken."""
    assert trust.find_annotation(KEY, {key: {"verdict": "suspect"}}) is None


def test_a_minute_precision_key_matches():
    assert trust.find_annotation(KEY, {"2026-08-05T21:10": {"verdict": "suspect"}}) is not None


def test_the_most_specific_key_wins():
    anns = {
        "2026-08-05T21:10": {"verdict": "suspect", "note": "vague"},
        "2026-08-05T21:10:04": {"verdict": "trusted", "note": "specific"},
    }
    assert trust.find_annotation(KEY, anns)["note"] == "specific"


# ---- staleness ----

def test_a_panel_that_never_reported_is_unknown_not_fresh():
    assert freshness_for([snapshot(panel_measure_time=None)])["panel_stale"] is True


def test_a_recently_measured_panel_is_not_stale():
    rows = [snapshot(panel_measure_time="2026-08-05T11:00:00.000Z")]
    from datetime import datetime, timezone
    assert freshness_for(rows, datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc))["panel_stale"] is False


def test_a_brand_new_pod_is_not_flagged_for_a_missing_panel():
    rows = [snapshot(panel_measure_time=None, pump_scan_state="PENDING",
                     pod_setup_time="2026-08-06T06:00:00.000Z")]
    from datetime import datetime, timezone
    assert freshness_for(rows, datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc))["panel_stale"] is False
