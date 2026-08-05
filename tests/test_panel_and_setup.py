"""The full chemistry panel, the pump-scan grace period, and dosing advice.

All three came out of a real incident: a new pod took one reading and went quiet
for two days, which looked like a hardware fault and wasn't - it was the 36-hour
post-setup pump scan. Reseating the cassette then produced a full panel of
measurements the pipeline had never seen.
"""
import json
from datetime import datetime, timedelta, timezone

from conftest import snapshot

import alerts
from db import parse_waterbody
from freshness import (
    PANEL_STALE_DAYS,
    freshness_for,
    in_setup_grace,
    scheduled_measurement,
    stale_measurement_alert,
)
from publish import build_payload

NOW = datetime(2026, 8, 5, 22, 0, tzinfo=timezone.utc)

PAYLOAD = {
    "waterBodyId": "wb-1",
    "name": "flounder",
    "status": "RED",
    "waterTemp": 85.0,
    "latestMeasureTime": "2026-08-05T21:10:04.000Z",
    "measurements": [
        {"type": "FREE_CL", "floatValue": 5.2, "target": 3.0,
         "measureTime": "2026-08-05T21:10:04.000Z",
         "cfg": {"floatRanges": {"GREEN_MIN": 1.6, "GREEN_MAX": 5.4}}},
        {"type": "TA", "floatValue": 134, "target": 80.0, "measureTime": "2026-08-05T21:10:05.832Z"},
        {"type": "CH", "floatValue": 153, "target": 400.0, "measureTime": "2026-08-05T21:10:05.832Z"},
        {"type": "CYA", "floatValue": 10, "target": 65.0, "measureTime": "2026-08-05T21:10:05.832Z"},
        {"type": "TH", "floatValue": 153, "target": 400.0, "measureTime": "2026-08-05T21:10:05.832Z"},
    ],
    "pods": [{
        "pod": {
            "setUpTime": "2026-08-03T17:55:12.210Z",
            "pumpScanState": "DONE_COMPUTED_WB_MEAS_TIME_SET",
            "measDoseTimes": [{"hour": 20, "minute": 56}],
        },
        "refillables": [],
        "rssiInfo": {},
    }],
    "alerts": [{
        "source": "CH", "condition": "VERY_LOW", "status": "RED",
        "text": "Calcium Hardness very low",
        "advice": {"action": {"summary": "Add 73 cups of 90% concentration calcium chloride."}},
    }],
}


# ---- the panel the pipeline used to discard ----

def test_the_whole_panel_is_parsed_not_just_chlorine_and_ph():
    row = parse_waterbody("2026-08-05T22:00:00+00:00", PAYLOAD)
    assert (row["ta"], row["ch"], row["cya"], row["th"]) == (134, 153, 10, 153)
    assert (row["ta_target"], row["cya_target"]) == (80.0, 65.0)


def test_the_panel_carries_its_own_measurement_time():
    """It's on a ~monthly cycle, so it can't share the daily timestamp."""
    row = parse_waterbody("2026-08-05T22:00:00+00:00", PAYLOAD)
    assert row["panel_measure_time"] == "2026-08-05T21:10:05.832Z"
    assert row["latest_measure_time"] == "2026-08-05T21:10:04.000Z"


def test_the_devices_own_green_band_is_captured():
    row = parse_waterbody("2026-08-05T22:00:00+00:00", PAYLOAD)
    assert row["free_cl_green_min"] == 1.6
    assert row["free_cl_green_max"] == 5.4


def test_setup_and_schedule_state_is_captured():
    row = parse_waterbody("2026-08-05T22:00:00+00:00", PAYLOAD)
    assert row["pump_scan_state"] == "DONE_COMPUTED_WB_MEAS_TIME_SET"
    assert (row["meas_hour"], row["meas_minute"]) == (20, 56)


def test_the_panel_reaches_the_dashboard_payload():
    row = parse_waterbody("2026-08-05T22:00:00+00:00", PAYLOAD)
    row["name"] = "flounder"
    wb = build_payload([row], NOW)["waterbodies"]["wb-1"]
    assert wb["series"][0]["cya"] == 10
    assert wb["targets"]["cya"] == 65.0


def test_a_monthly_panel_is_not_judged_by_the_daily_threshold():
    """30-day validity: flagging it stale at 48h would fault a healthy panel daily."""
    rows = [snapshot(panel_measure_time="2026-07-25T11:00:00.000Z")]  # 11 days old
    assert not freshness_for(rows, NOW)["panel_stale"]

    old = [snapshot(panel_measure_time="2026-06-01T11:00:00.000Z")]   # ~65 days old
    assert freshness_for(old, NOW)["panel_stale"]
    assert PANEL_STALE_DAYS > 30


# ---- pump-scan grace ----

def test_a_pod_mid_scan_is_calibrating_not_broken():
    row = snapshot(pump_scan_state="PENDING", pod_setup_time="2026-08-05T12:00:00.000Z")
    assert in_setup_grace(row, NOW)


def test_a_freshly_set_up_pod_is_still_in_grace_even_once_the_scan_reports_done():
    """The scan finishing and the first scheduled reading landing aren't the same moment."""
    row = snapshot(pump_scan_state="DONE_COMPUTED_WB_MEAS_TIME_SET",
                   pod_setup_time="2026-08-05T12:00:00.000Z")
    assert in_setup_grace(row, NOW)


def test_a_settled_pod_is_not_in_grace():
    row = snapshot(pump_scan_state="DONE_COMPUTED_WB_MEAS_TIME_SET",
                   pod_setup_time="2026-07-01T12:00:00.000Z")
    assert not in_setup_grace(row, NOW)


def test_no_stale_alert_while_a_new_pod_is_still_scanning():
    """The false alarm this whole guard exists to prevent."""
    row = snapshot(
        pod_setup_time="2026-08-04T18:00:00.000Z",
        pump_scan_state="PENDING",
        latest_measure_time="2026-08-03T18:13:42.000Z",
    )
    assert stale_measurement_alert(row, "flounder", NOW) is None


def test_a_settled_pod_going_quiet_still_alerts():
    row = snapshot(
        pod_setup_time="2026-07-01T12:00:00.000Z",
        pump_scan_state="DONE_COMPUTED_WB_MEAS_TIME_SET",
        latest_measure_time="2026-08-01T18:13:42.000Z",
    )
    assert stale_measurement_alert(row, "flounder", NOW) is not None


def test_freshness_reports_setup_state_and_schedule():
    rows = [snapshot(pod_setup_time="2026-08-05T12:00:00.000Z", pump_scan_state="PENDING")]
    f = freshness_for(rows, NOW)
    assert f["setting_up"] is True
    assert f["measure_stale"] is False
    assert f["scheduled_measurement"] == "20:56"


def test_a_missing_schedule_is_not_invented():
    assert scheduled_measurement(snapshot(meas_hour=None, meas_minute=None)) is None


# ---- dosing advice ----

def test_the_dose_is_kept_off_the_alert_not_just_the_label():
    row = parse_waterbody("2026-08-05T22:00:00+00:00", PAYLOAD)
    stored = json.loads(row["alerts_json"])[0]
    assert stored["advice"] == "Add 73 cups of 90% concentration calcium chloride."


def test_the_push_tells_you_what_to_add():
    row = snapshot(status="RED", prev_status="GREEN", alerts_json=json.dumps([{
        "status": "RED", "text": "Calcium Hardness very low",
        "advice": "Add 73 cups of 90% concentration calcium chloride.",
    }]))
    _, message = alerts._status_alerts(row, "flounder")[0]
    assert "Calcium Hardness very low" in message
    assert "Add 73 cups" in message


def test_an_alert_without_advice_still_reads_cleanly():
    row = snapshot(status="RED", prev_status="GREEN", alerts_json=json.dumps([
        {"status": "RED", "text": "Free Chlorine very high", "advice": None},
    ]))
    _, message = alerts._status_alerts(row, "flounder")[0]
    assert message == "Free Chlorine very high"
