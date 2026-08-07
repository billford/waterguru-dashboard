"""Alerts that fire once, re-nag on a real clock, and don't lie about recovery.

Every case here was found by review. The failure mode they share is an alert
that trains you to ignore it — which then mutes the genuine one behind it.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import snapshot

import alerts
import anomaly

BASE = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


# ---- the trigger and the edge must agree ----

def test_a_persistently_urgent_cassette_alerts_once_not_every_run():
    """status=YELLOW with urgent=1 satisfied the trigger but never the edge, so
    it re-fired twice a day until the cassette was physically changed."""
    row = snapshot(cassette_status="YELLOW", cassette_urgent=1,
                   prev_cassette_status="YELLOW", prev_cassette_urgent=1)
    assert alerts._cassette_alerts(row, "Pool") == []


def test_a_newly_urgent_cassette_still_alerts():
    row = snapshot(cassette_status="YELLOW", cassette_urgent=1,
                   prev_cassette_status="GREEN", prev_cassette_urgent=0)
    assert "replace the cassette" in alerts._cassette_alerts(row, "Pool")[0][0]


def test_downgrading_red_to_yellow_while_still_urgent_is_not_a_recovery():
    """WaterGuru can drop RED to YELLOW while still flagging urgent; the old
    edge announced "cassette replaced" at that moment."""
    row = snapshot(cassette_status="YELLOW", cassette_urgent=1,
                   prev_cassette_status="RED", prev_cassette_urgent=1)
    assert alerts._cassette_alerts(row, "Pool") == []


def test_a_genuine_replacement_still_reports_recovery():
    row = snapshot(cassette_status="GREEN", cassette_urgent=0,
                   prev_cassette_status="RED", prev_cassette_urgent=1)
    assert "cassette replaced" in alerts._cassette_alerts(row, "Pool")[0][0]


# ---- re-nag on elapsed time, not a calendar grid ----

@pytest.mark.parametrize("days, expected", [(1, False), (6, False), (7, True), (8, True)])
def test_a_standing_condition_re_nags_on_elapsed_time(db, days, expected):
    """Measured from the last send, not a calendar grid. Bucketing on
    toordinal()//7 made the gap anywhere from one day to seven depending on
    where in the bucket the first alert happened to land."""
    row = snapshot()
    assert alerts._once(db, row, "thing", [("t", "m")], BASE)   # primes the clock
    assert bool(alerts._once(db, row, "thing", [("t", "m")], BASE + timedelta(days=days))) is expected


def test_the_re_nag_clock_restarts_from_each_send(db):
    row = snapshot()
    alerts._once(db, row, "thing", [("t", "m")], BASE)
    assert alerts._once(db, row, "thing", [("t", "m")], BASE + timedelta(days=7))
    # Having just re-fired on day 7, day 8 must be quiet again.
    assert alerts._once(db, row, "thing", [("t", "m")], BASE + timedelta(days=8)) == []


def test_a_reorder_reminder_re_nags_rather_than_firing_once_ever(db):
    """It was marked sent before the push was attempted, and ntfy failures are
    swallowed - so one transient 5xx cost the whole depletion cycle."""
    low = snapshot(cassette_days_left="10 days left")
    assert alerts._reorder_alerts(db, low, "Pool", BASE)
    assert alerts._reorder_alerts(db, low, "Pool", BASE + timedelta(days=3)) == []
    assert alerts._reorder_alerts(db, low, "Pool", BASE + timedelta(days=7))


def test_replacing_the_consumable_still_clears_the_reminder(db):
    low = snapshot(cassette_days_left="10 days left")
    alerts._reorder_alerts(db, low, "Pool", BASE)
    replaced = snapshot(cassette_days_left="90 days left", cassette_pct_left=100.0,
                        prev_cassette_pct_left=8.0)
    alerts._reorder_alerts(db, replaced, "Pool", BASE + timedelta(days=1))
    assert alerts._reorder_alerts(db, low, "Pool", BASE + timedelta(days=2))


# ---- anomaly detection across gaps ----

def _rd(day, **kw):
    row = {"fetched_at": f"2026-08-{day:02d}T12:00:00+00:00",
           "latest_measure_time": f"2026-08-{day:02d}T11:00:00.000Z",
           "free_cl": 3.0, "ph": 7.5}
    row.update(kw)
    return row


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def test_a_garbage_reading_after_a_long_outage_is_still_caught():
    """The allowance scaled linearly with the gap, so a 20-day outage permitted
    a 24-unit pH swing - switching the check off exactly when it was needed."""
    assert anomaly.detect_jump([_rd(1, ph=7.4), _rd(21, ph=0.0)], "ph", NOW) is not None


def test_a_believable_drift_over_a_gap_is_still_allowed():
    assert anomaly.detect_jump([_rd(1, ph=7.4), _rd(21, ph=7.9)], "ph", NOW) is None


def test_a_stale_jump_is_not_reported_as_news():
    """points[-1] is the newest non-null reading, which after a dropout can be
    weeks old; the finding fired forever in the present tense."""
    old = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert anomaly.detect_jump([_rd(1, ph=7.4), _rd(2, ph=0.0)], "ph", old) is None


def test_a_recent_jump_carries_when_it_happened():
    found = anomaly.detect_jump([_rd(20, ph=7.4), _rd(21, ph=0.0)], "ph", NOW)
    assert found["at"].startswith("2026-08-21")


# ---- sitting AT the limit gives no warning from the device ----

def _flow_row(flow, condition=None, green_min=5.0):
    alerts_json = "[]" if condition is None else json.dumps([
        {"source": "SKIMMER_FLOW", "condition": condition, "status": "YELLOW",
         "text": "Skimmer Flow low"}])
    return snapshot(skimmer_flow=flow, skimmer_flow_green_min=green_min, alerts_json=alerts_json)


def test_flow_at_the_bottom_of_the_band_warns_even_though_the_device_says_green(db):
    """The device reports GREEN right up to the boundary, so the first alert
    arrives only once measurement is already at risk. This pool went 16 -> 5 gpm
    (the green minimum) in an afternoon; at 3 the pod stops measuring."""
    fired = alerts._flow_alerts(db, _flow_row(5.0), "Pool", BASE)
    assert fired
    assert "near the limit" in fired[0][0]
    assert "no margin left" in fired[0][1]
    assert "basket" in fired[0][1]   # the cheapest thing to rule out


def test_healthy_flow_with_margin_stays_quiet(db):
    assert alerts._flow_alerts(db, _flow_row(16.0), "Pool", BASE) == []


def test_the_device_saying_low_still_takes_precedence(db):
    fired = alerts._flow_alerts(db, _flow_row(3.0, condition="LOW"), "Pool", BASE)
    assert "too low to measure" in fired[0][0]


def test_the_margin_warning_does_not_repeat_every_run(db):
    assert alerts._flow_alerts(db, _flow_row(5.0), "Pool", BASE)
    assert alerts._flow_alerts(db, _flow_row(5.0), "Pool", BASE) == []


def test_without_a_known_band_only_the_device_verdict_applies(db):
    assert alerts._flow_alerts(db, _flow_row(5.0, green_min=None), "Pool", BASE) == []


# ---- the pod trying and giving up ----

def _attempt(pads_before, pads_after, measure_time, prev_measure_time, flow=5.0):
    return snapshot(
        cassette_pads_left=pads_after, prev_cassette_pads_left=pads_before,
        latest_measure_time=measure_time, prev_measure_time=prev_measure_time,
        skimmer_flow=flow, skimmer_flow_green_min=5.0,
    )


def test_pads_spent_with_no_new_reading_is_a_failed_attempt(db):
    """Invisible in WaterGuru's app, and expensive - a failed attempt burns pads
    with nothing to show, and retrying without fixing the cause burns more."""
    row = _attempt(182, 180, "2026-08-06T15:06:44.000Z", "2026-08-06T15:06:44.000Z")
    fired = alerts._failed_measurement_alerts(db, row, "Pool", BASE)
    assert fired
    assert "attempt failed" in fired[0][0]
    assert "2 cassette pads" in fired[0][1]


def test_the_likely_cause_is_named_when_flow_is_at_its_limit(db):
    row = _attempt(182, 180, "2026-08-06T15:06:44.000Z", "2026-08-06T15:06:44.000Z", flow=5.0)
    assert "draws its sample through the skimmer" in alerts._failed_measurement_alerts(
        db, row, "Pool", BASE)[0][1]


def test_a_completed_measurement_is_not_a_failure(db):
    """Ten pads spent AND the measurement time advanced - that's just a reading."""
    row = _attempt(192, 182, "2026-08-07T20:56:00.000Z", "2026-08-06T15:06:44.000Z")
    assert alerts._failed_measurement_alerts(db, row, "Pool", BASE) == []


def test_pads_unchanged_is_not_a_failure(db):
    row = _attempt(182, 182, "2026-08-06T15:06:44.000Z", "2026-08-06T15:06:44.000Z")
    assert alerts._failed_measurement_alerts(db, row, "Pool", BASE) == []


def test_a_replacement_resetting_the_count_upward_is_not_a_failure(db):
    row = _attempt(10, 192, "2026-08-06T15:06:44.000Z", "2026-08-06T15:06:44.000Z")
    assert alerts._failed_measurement_alerts(db, row, "Pool", BASE) == []


def test_healthy_flow_leaves_the_cause_unstated(db):
    row = _attempt(182, 180, "2026-08-06T15:06:44.000Z", "2026-08-06T15:06:44.000Z", flow=30.0)
    message = alerts._failed_measurement_alerts(db, row, "Pool", BASE)[0][1]
    assert "skimmer" not in message
