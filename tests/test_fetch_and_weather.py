import botocore.exceptions
import pytest
import requests

import fetch
from db import parse_waterbody
from weather import _parse_wind_mph, swim_score


# ---- retry behaviour ----

def test_a_transient_failure_is_retried_and_can_succeed(monkeypatch):
    attempts = []

    def flaky(user, password):
        attempts.append(1)
        if len(attempts) < 2:
            raise requests.ConnectionError("network blip")
        return {"ok": True}

    monkeypatch.setattr(fetch, "fetch_dashboard", flaky)
    assert fetch.fetch_with_retries("u", "p", sleep=lambda s: None) == {"ok": True}
    assert len(attempts) == 2


def test_retries_run_out_and_the_last_error_is_raised(monkeypatch):
    monkeypatch.setattr(fetch, "fetch_dashboard",
                        lambda u, p: (_ for _ in ()).throw(requests.ConnectionError("down")))
    with pytest.raises(requests.ConnectionError):
        fetch.fetch_with_retries("u", "p", sleep=lambda s: None)


def test_a_wrong_password_is_not_retried(monkeypatch):
    """Three retries on bad credentials is three failed logins, not a recovery."""
    attempts = []

    def bad_auth(user, password):
        attempts.append(1)
        raise botocore.exceptions.ClientError(
            {"Error": {"Code": "NotAuthorizedException", "Message": "bad password"}}, "InitiateAuth"
        )

    monkeypatch.setattr(fetch, "fetch_dashboard", bad_auth)
    with pytest.raises(botocore.exceptions.ClientError):
        fetch.fetch_with_retries("u", "p", sleep=lambda s: None)
    assert len(attempts) == 1


def test_a_server_error_is_retried_but_a_client_error_is_not(monkeypatch):
    def http_error(status):
        response = requests.Response()
        response.status_code = status
        return requests.HTTPError(response=response)

    assert fetch._is_fatal(http_error(400)) is True
    assert fetch._is_fatal(http_error(503)) is False


def test_a_throttling_error_is_retried():
    err = botocore.exceptions.ClientError(
        {"Error": {"Code": "TooManyRequestsException"}}, "InitiateAuth"
    )
    assert fetch._is_fatal(err) is False


# ---- WaterGuru payload parsing ----

WATERBODY = {
    "waterBodyId": "wb-1",
    "name": "Pool",
    "status": "RED",
    "waterTemp": 82.0,
    "latestMeasureTime": "2026-08-03T18:13:42.000Z",
    "measurements": [
        {"type": "FREE_CL", "floatValue": 9.8, "target": 3.0},
        {"type": "PH", "floatValue": 7.3, "target": 7.6},
        {"type": "SKIMMER_FLOW", "intValue": 13, "target": None},
    ],
    "pods": [{
        "refillables": [
            {"type": "LAB", "pctLeft": 95.0, "timeLeftText": "18 days left", "status": "GREEN", "urgent": False},
            {"type": "BATT", "pctLeft": 72.0, "timeLeftText": "4 months left", "status": "GREEN"},
        ],
        "rssiInfo": {"rssi": -60, "desc": "Good"},
    }],
    "alerts": [{"source": "FREE_CL", "condition": "VERY_HIGH", "status": "RED", "text": "Free Chlorine very high"}],
}


def test_measurements_and_targets_are_pulled_out():
    row = parse_waterbody("2026-08-05T12:00:00+00:00", WATERBODY)
    assert row["free_cl"] == 9.8 and row["free_cl_target"] == 3.0
    assert row["ph"] == 7.3
    assert row["skimmer_flow"] == 13  # intValue, not floatValue


def test_refillables_and_signal_are_pulled_out():
    row = parse_waterbody("2026-08-05T12:00:00+00:00", WATERBODY)
    assert row["cassette_pct_left"] == 95.0
    assert row["cassette_urgent"] == 0
    assert row["battery_time_left"] == "4 months left"
    assert row["rssi"] == -60


def test_a_waterbody_with_no_pod_does_not_explode():
    row = parse_waterbody("2026-08-05T12:00:00+00:00", {"waterBodyId": "wb-2", "pods": []})
    assert row["cassette_pct_left"] is None and row["rssi"] is None


def test_a_missing_measurement_type_is_none_not_an_error():
    row = parse_waterbody("2026-08-05T12:00:00+00:00", {"waterBodyId": "wb-3", "measurements": []})
    assert row["free_cl"] is None


# ---- rule-based swim scoring (the fallback when Ollama is unreachable) ----

def test_a_warm_calm_dry_day_scores_top_marks():
    score, reason = swim_score(88, 0, 5, "Sunny")
    assert score == 100 and reason == "warm, calm, dry"


def test_storms_sink_the_score():
    score, reason = swim_score(88, 70, 5, "Thunderstorms")
    assert score < 40 and "storms" in reason


def test_a_cold_day_scores_poorly():
    score, reason = swim_score(55, 0, 3, "Sunny")
    assert score <= 60 and "cool air temp" in reason


def test_the_score_never_leaves_the_zero_to_one_hundred_range():
    assert swim_score(30, 100, 40, "Thunderstorms")[0] == 0
    assert swim_score(95, 0, 0, "Sunny")[0] == 100


def test_wind_speed_ranges_take_the_upper_bound():
    assert _parse_wind_mph("10 to 15 mph") == 15
    assert _parse_wind_mph("8 mph") == 8
    assert _parse_wind_mph("") == 0.0
