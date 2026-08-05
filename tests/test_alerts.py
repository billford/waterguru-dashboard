from datetime import timedelta

import pytest
from conftest import snapshot

import alerts
from db import was_sent


@pytest.fixture(autouse=True)
def no_real_notifications(monkeypatch):
    """Nothing in this file should be able to fire an actual push."""
    sent = []
    monkeypatch.setattr(alerts, "_mac_notification", lambda t, m: sent.append((t, m)))
    monkeypatch.setattr(alerts, "_ntfy_push", lambda *a, **k: None)
    return sent


def titles(alert_list):
    return [t for t, _ in alert_list]


# ---- water chemistry status ----

def test_red_fires_on_the_transition():
    row = snapshot(status="RED", prev_status="GREEN", alerts_json='[{"status":"RED","text":"pH low"}]')
    assert "pH low" in alerts._status_alerts(row, "Pool")[0][1]


def test_red_does_not_re_fire_while_it_stays_red():
    row = snapshot(status="RED", prev_status="RED")
    assert alerts._status_alerts(row, "Pool") == []


def test_recovery_from_red_fires_once():
    row = snapshot(status="GREEN", prev_status="RED")
    assert "back to normal" in titles(alerts._status_alerts(row, "Pool"))[0]


def test_yellow_never_alerts():
    assert alerts._status_alerts(snapshot(status="YELLOW", prev_status="GREEN"), "Pool") == []


# ---- cassette ----

def test_cassette_red_fires_with_the_level_detail():
    row = snapshot(cassette_status="RED", prev_cassette_status="GREEN",
                   cassette_pct_left=4.0, cassette_days_left="2 days left")
    title, message = alerts._cassette_alerts(row, "Pool")[0]
    assert "replace the cassette" in title
    assert "4% left" in message and "2 days left" in message


def test_cassette_urgent_flag_fires_even_when_status_is_not_red():
    row = snapshot(cassette_status="YELLOW", prev_cassette_status="GREEN", cassette_urgent=1)
    assert alerts._cassette_alerts(row, "Pool")


def test_cassette_replacement_fires_once():
    row = snapshot(cassette_status="GREEN", prev_cassette_status="RED")
    assert "cassette replaced" in titles(alerts._cassette_alerts(row, "Pool"))[0]


# ---- battery (previously stored but never alerted on) ----

def test_battery_red_fires():
    row = snapshot(battery_status="RED", prev_battery_status="GREEN",
                   battery_pct_left=5.0, battery_time_left="3 days left")
    title, message = alerts._battery_alerts(row, "Pool")[0]
    assert "battery low" in title
    assert "5% left" in message


def test_battery_stays_quiet_while_already_red():
    row = snapshot(battery_status="RED", prev_battery_status="RED")
    assert alerts._battery_alerts(row, "Pool") == []


def test_battery_recovery_fires():
    row = snapshot(battery_status="GREEN", prev_battery_status="RED")
    assert "battery replaced" in titles(alerts._battery_alerts(row, "Pool"))[0]


def test_missing_battery_status_is_not_an_alert():
    """Older rows predate the battery_status column."""
    assert alerts._battery_alerts(snapshot(battery_status=None), "Pool") == []


# ---- reorder reminders (standing condition, stored state) ----

def test_reorder_fires_when_runway_drops_under_the_lead_time(db, now):
    row = snapshot(cassette_days_left="10 days left")
    fired = alerts._reorder_alerts(db, row, "Pool", now)
    assert "order a new cassette" in titles(fired)[0]
    assert "About 10 days left" in fired[0][1]
    assert "Aug 15" in fired[0][1]  # projected run-out date, not just the raw string


def test_reorder_stays_quiet_with_plenty_of_runway(db, now):
    assert alerts._reorder_alerts(db, snapshot(cassette_days_left="40 days left"), "Pool", now) == []


def test_reorder_does_not_nag_on_every_run(db, now):
    row = snapshot(cassette_days_left="10 days left")
    assert alerts._reorder_alerts(db, row, "Pool", now)
    assert alerts._reorder_alerts(db, row, "Pool", now + timedelta(hours=12)) == []


def test_replacing_the_cassette_re_arms_the_reminder(db, now):
    low = snapshot(cassette_days_left="10 days left")
    assert alerts._reorder_alerts(db, low, "Pool", now)

    replaced = snapshot(cassette_days_left="90 days left", cassette_pct_left=100.0,
                        prev_cassette_pct_left=8.0)
    alerts._reorder_alerts(db, replaced, "Pool", now + timedelta(days=5))
    assert not was_sent(db, "reorder:cassette:wb-1")

    # Next depletion cycle can remind again.
    assert alerts._reorder_alerts(db, low, "Pool", now + timedelta(days=90))


def test_battery_reorder_uses_its_own_longer_lead_time(db, now):
    # 18 days is inside the battery lead time (21) but outside the cassette's (14).
    fired = alerts._reorder_alerts(db, snapshot(battery_time_left="18 days left"), "Pool", now)
    assert "order a new pod battery" in titles(fired)[0]


# ---- standing-condition suppression ----

def test_once_suppresses_a_repeat_within_the_renag_window(db, now):
    row = snapshot()
    assert alerts._once(db, row, "thing", [("t", "m")], now)
    assert alerts._once(db, row, "thing", [("t", "m")], now + timedelta(days=1)) == []


def test_once_re_arms_after_the_renag_window(db, now):
    row = snapshot()
    assert alerts._once(db, row, "thing", [("t", "m")], now)
    assert alerts._once(db, row, "thing", [("t", "m")], now + timedelta(days=14))


def test_fetch_failure_notification_reaches_a_channel(no_real_notifications):
    alerts.notify_fetch_failure("boom")
    assert "boom" in no_real_notifications[0][1]


def test_applescript_escaping_survives_quotes_and_newlines():
    assert alerts._escape_applescript('say "hi"\nbye') == 'say \\"hi\\", bye'
