"""Everything that's worth interrupting you about.

Channels: a native macOS notification (osascript) and a push via ntfy
(subscribe to the topic in the ntfy app). Points at whatever NTFY_URL names -
a self-hosted, access-controlled server - and falls back to ntfy.sh if unset.

Two different "fire once" mechanisms are at work here, for two different kinds
of alert:

- **Transitions** (water RED, cassette RED, battery RED) compare against the
  previous snapshot's status, so they fire on the edge and stay quiet while the
  condition persists. No stored state needed.
- **Standing conditions** (reorder reminders, stale sensor, anomalies) have no
  edge to hang off - they'd re-fire on every run forever. Those are recorded in
  the `sent_notifications` table and suppressed until the situation resets:
  a reorder reminder clears when the consumable is physically replaced, and the
  softer nags re-arm weekly so a real problem doesn't get silently dropped.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import requests

import anomaly
import dosing
import trust
from config import load_dotenv
from consumables import runway, was_replaced
from db import connect, dedupe_by_measurement, last_sent_at, mark_sent, was_sent
from freshness import stale_measurement_alert

# How long before a standing, non-resetting condition is allowed to nag again.
RENAG_DAYS = 7

# How close to the bottom of the acceptable band counts as 'no margin left'.
FLOW_MARGIN_FRACTION = 0.15

# Pads a completed measurement consumes; fewer than this with no new reading
# means the attempt aborted partway.
COMPLETE_MEASUREMENT_PADS = 8


def _mac_notification(title: str, message: str):
    script = (
        f'display notification "{_escape_applescript(message)}" '
        f'with title "{_escape_applescript(title)}" sound name "Basso"'
    )
    subprocess.run(["osascript", "-e", script], check=False)


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", ", ")


def _ntfy_push(topic: str, title: str, message: str, priority: str = "high", tags: str = "warning,pool"):
    """Pushes to ntfy, and says so loudly if it didn't work.

    Failures used to be swallowed apart from a bare print, which made "my phone
    never buzzed" impossible to tell apart from "the push was never sent".

    NTFY_URL points at a self-hosted ntfy; unset, it falls back to ntfy.sh so
    an existing .env keeps working untouched. The self-hosted server refuses
    anonymous requests, so NTFY_USER/NTFY_PASS are required there - without
    them every push returns 403 and this function reports the failure.
    """
    base = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    user = os.environ.get("NTFY_USER")
    password = os.environ.get("NTFY_PASS")
    try:
        resp = requests.post(
            f"{base}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            auth=(user, password) if user and password else None,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("id")
    except requests.RequestException as e:
        print(f"ntfy push failed: {e}", file=sys.stderr)
        return None


def send(title: str, message: str, priority: str = "high", tags: str = "warning,pool"):
    """Pushes one alert out over every configured channel."""
    _mac_notification(title, message)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        _ntfy_push(topic, title, message, priority=priority, tags=tags)


def check_and_alert(rows: list[dict], now: datetime = None, conn=None):
    now = now or datetime.now(timezone.utc)
    own_conn = conn is None
    conn = conn or connect()
    try:
        for row in rows:
            for title, message in build_alerts(conn, row, now):
                send(title, message)
    finally:
        if own_conn:
            conn.close()


def build_alerts(conn, row: dict, now: datetime) -> list[tuple[str, str]]:
    """All alerts due for one water body on this run, deduped state included."""
    name = row.get("name") or "Pool"
    alerts = []
    alerts += _status_alerts(row, name, _current_trust(conn, row), _dose_factor(conn, row))
    alerts += _cassette_alerts(row, name)
    alerts += _battery_alerts(row, name)

    stale = stale_measurement_alert(row, name, now)
    if stale:
        alerts += _once(conn, row, "stale_measurement", [stale], now)

    alerts += _failed_measurement_alerts(conn, row, name, now)
    alerts += _flow_alerts(conn, row, name, now)
    alerts += _reorder_alerts(conn, row, name, now)
    alerts += _anomaly_alerts(conn, row, name, now)
    return alerts


def _failed_measurement_alerts(conn, row: dict, name: str, now: datetime) -> list[tuple[str, str]]:
    """The pod consumed pads but produced no reading - it tried and gave up.

    Invisible in WaterGuru's own app, and expensive: a failed attempt burns
    cassette pads with nothing to show, and retrying without fixing the cause
    burns more. Seen here when a manually triggered measurement spent 2 pads and
    aborted while skimmer flow sat exactly on its minimum.

    A completed measurement consumes roughly ten pads *and* advances the
    measurement time. Pads going down while the time stands still is the
    signature of an attempt that failed.
    """
    pads, prev_pads = row.get("cassette_pads_left"), row.get("prev_cassette_pads_left")
    if pads is None or prev_pads is None:
        return []

    consumed = prev_pads - pads
    # A replacement resets the count upward; only a decrease is interesting.
    if consumed <= 0 or consumed >= COMPLETE_MEASUREMENT_PADS:
        return []
    if row.get("latest_measure_time") != row.get("prev_measure_time"):
        return []  # pads spent and a reading produced - that is just a measurement

    flow = row.get("skimmer_flow")
    floor = row.get("skimmer_flow_green_min")
    cause = ""
    if flow is not None and floor is not None and flow <= floor * 1.15:
        cause = (f" Skimmer flow was {flow:g} gpm, at the bottom of its range - the pod draws "
                 "its sample through the skimmer, so that is the likely reason.")

    return _once(
        conn, row, "failed_measurement",
        [(
            f"{name}: measurement attempt failed",
            f"The pod used {consumed:g} cassette pads but produced no reading, so it started a "
            f"measurement and gave up.{cause} Retrying before fixing the cause will just spend "
            "more pads.",
        )],
        now,
    )


def _flow_alerts(conn, row: dict, name: str, now: datetime) -> list[tuple[str, str]]:
    """Low skimmer flow, which WaterGuru only rates YELLOW but which stops the
    sensor working entirely.

    The pod draws its test sample from the skimmer, so when flow there falls off
    it simply stops measuring - and the failure presents as "the dashboard looks
    out of date" rather than anything pointing at flow. Worth interrupting for,
    despite not being RED, because every downstream reading depends on it.

    Total system flow can be perfectly healthy while the pod's skimmer is
    starved: a pump moving 51 gpm overall says nothing about how much of it
    reaches this particular skimmer.
    """
    flow_alert = _find_alert(row.get("alerts_json"), "SKIMMER_FLOW")
    low_by_device = flow_alert and flow_alert.get("condition") in ("LOW", "VERY_LOW")

    # Sitting *at* the bottom of the acceptable band gives no warning at all -
    # the device reports GREEN right up to the boundary, and the first alert
    # arrives only once measurement is already at risk. This pool went from 16
    # gpm to exactly 5 (the green minimum) in an afternoon; at 3 the pod stops
    # measuring entirely.
    flow = row.get("skimmer_flow")
    margin_low = (
        flow is not None
        and row.get("skimmer_flow_green_min") is not None
        and flow <= row["skimmer_flow_green_min"] * (1 + FLOW_MARGIN_FRACTION)
    )

    if not low_by_device and not margin_low:
        return []

    reading = f"Skimmer flow is {flow:g} gpm. " if flow is not None else ""
    if margin_low and not low_by_device:
        floor = row["skimmer_flow_green_min"]
        return _once(
            conn, row, "flow_margin",
            [(
                f"{name}: skimmer flow near the limit",
                reading + f"That's at the bottom of the acceptable range ({floor:g} gpm minimum) - "
                "still reported as fine, but with no margin left, and the pod stops measuring "
                "if it falls much further. Check this skimmer's basket first: it's a single "
                "instantaneous reading, and a loading basket is both the likeliest cause and "
                "the quickest to rule out.",
            )],
            now,
        )

    return _once(
        conn, row, "low_flow",
        [(
            f"{name}: skimmer flow too low to measure",
            reading + "The pod draws its sample through the skimmer, so it will stop taking "
            "readings until this recovers. Check the skimmer basket, the pump basket, and the "
            "valves feeding this skimmer - total pump flow can look fine while this one is starved.",
        )],
        now,
    )


def _find_alert(alerts_json, source: str) -> dict | None:
    try:
        alerts = json.loads(alerts_json) if isinstance(alerts_json, str) else (alerts_json or [])
    except (TypeError, ValueError):
        return None
    return next((a for a in alerts if a.get("source") == source), None)


# ---- transition-based ----

def _current_trust(conn, row: dict) -> dict | None:
    """Trust verdict for the measurement this alert is about."""
    wb_id = row.get("water_body_id")
    if not wb_id:
        return None
    history = [
        dict(r)
        for r in conn.execute(
            """SELECT fetched_at, latest_measure_time, alerts_json FROM snapshots
               WHERE water_body_id = ? ORDER BY fetched_at""",
            (wb_id,),
        ).fetchall()
    ]
    key = row.get("latest_measure_time") or row.get("fetched_at")
    return trust.evaluate(history).get(key)


def _dose_factor(conn, row: dict) -> float | None:
    """Ratio between the real pool volume and the one WaterGuru doses from."""
    try:
        controller = conn.execute(
            "SELECT volume_gallons FROM system_snapshots ORDER BY read_at DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return None
    actual = controller["volume_gallons"] if controller else None
    return dosing.scale_factor(actual, row.get("size_gallons"))


def _status_alerts(row: dict, name: str, verdict: dict = None,
                   dose_factor: float = None) -> list[tuple[str, str]]:
    status = row["status"]
    prev_status = row.get("prev_status")

    if status == "RED" and prev_status != "RED":
        title = f"{name}: pool status RED"
        message = _format_alerts(row["alerts_json"], dose_factor) or "Check the dashboard for details."
        # A dose sized from an unrepresentative sample is the most costly thing
        # this pipeline can tell someone to do. Say so in the same breath.
        if verdict and not verdict.get("trusted"):
            reason = (verdict.get("reasons") or ["This reading looks unreliable."])[0]
            message += (
                f"\n\nTREAT WITH CAUTION: {reason} "
                "Confirm with a test kit before adding anything."
            )
        return [(title, message)]
    if prev_status == "RED" and status != "RED":
        return [(f"{name}: back to normal", f"Status is now {status}.")]
    return []


def _cassette_alerts(row: dict, name: str) -> list[tuple[str, str]]:
    return _consumable_alerts(
        row, name,
        status_field="cassette_status", prev_status_field="prev_cassette_status",
        urgent_field="cassette_urgent", prev_urgent_field="prev_cassette_urgent",
        pct_field="cassette_pct_left",
        time_field="cassette_days_left",
        low_title="replace the cassette", low_fallback="Cassette is running low.",
        ok_title="cassette replaced", ok_message="Cassette level is back to normal.",
    )


def _battery_alerts(row: dict, name: str) -> list[tuple[str, str]]:
    return _consumable_alerts(
        row, name,
        status_field="battery_status", prev_status_field="prev_battery_status",
        urgent_field=None, prev_urgent_field=None, pct_field="battery_pct_left",
        time_field="battery_time_left",
        low_title="pod battery low", low_fallback="Pod battery is running low.",
        ok_title="pod battery replaced", ok_message="Battery level is back to normal.",
    )


def _needs_action(status, urgent) -> bool:
    return status == "RED" or bool(urgent)


def _consumable_alerts(row, name, *, status_field, prev_status_field, urgent_field,
                       prev_urgent_field, pct_field, time_field, low_title, low_fallback,
                       ok_title, ok_message) -> list[tuple[str, str]]:
    """Shared RED-edge / recovered-edge logic for the cassette and the battery."""
    status = row.get(status_field)
    prev_status = row.get(prev_status_field)
    if status is None:
        return []

    # The edge must be computed from the SAME condition that triggers, or the
    # two disagree: `status=YELLOW, urgent=1` satisfied needs_action but never
    # was_flagged, so it re-fired on every run forever - and the reverse case
    # announced "replaced" while the consumable was still flagged urgent.
    needs_action = _needs_action(status, row.get(urgent_field) if urgent_field else None)
    was_flagged = _needs_action(prev_status, row.get(prev_urgent_field) if prev_urgent_field else None)

    if needs_action and not was_flagged:
        pct = row.get(pct_field)
        remaining = row.get(time_field) or ""
        pct_text = f"{pct:.0f}% left" if pct is not None else ""
        detail = ", ".join(x for x in [pct_text, remaining] if x)
        return [(f"{name}: {low_title}", detail or low_fallback)]

    if was_flagged and not needs_action:
        return [(f"{name}: {ok_title}", ok_message)]

    return []


# ---- standing conditions, suppressed via sent_notifications ----

def _once(conn, row, key_suffix, alerts, now, renag_days: int = RENAG_DAYS):
    """Passes alerts through only if this condition hasn't fired recently.

    Measured against when it last fired, not a fixed calendar grid. Bucketing on
    `toordinal() // renag_days` meant the gap between re-nags was anywhere from
    one day to seven, depending where in the bucket the first alert landed.
    """
    if not alerts:
        return []

    key = f"{key_suffix}:{row.get('water_body_id')}"
    last = last_sent_at(conn, key)
    if last is not None and renag_days:
        elapsed_days = (now - last).total_seconds() / 86400
        if elapsed_days < renag_days:
            return []

    mark_sent(conn, key, now.isoformat())
    return alerts


def _reorder_alerts(conn, row: dict, name: str, now: datetime) -> list[tuple[str, str]]:
    """Order-it-now reminders, fired once per consumable per depletion cycle.

    The 'cycle' is what makes this safe to fire once and stop: putting in a new
    cassette makes its level jump, which clears the record and re-arms the
    reminder for next time.
    """
    out = []
    levels = runway(row, now)

    for kind, pct_field, prev_pct_field, label in (
        ("cassette", "cassette_pct_left", "prev_cassette_pct_left", "cassette"),
        ("battery", "battery_pct_left", "prev_battery_pct_left", "pod battery"),
    ):
        info = levels[kind]
        key = f"reorder:{kind}:{row.get('water_body_id')}"

        if was_replaced(row.get(pct_field), row.get(prev_pct_field)):
            conn.execute("DELETE FROM sent_notifications WHERE key = ?", (key,))
            conn.commit()
            continue

        if not info["needs_reorder"]:
            continue

        # Re-nag rather than firing once ever: the record was previously written
        # *before* the push was attempted, and ntfy failures are swallowed, so a
        # single transient 5xx silently cost the whole depletion cycle.
        last = last_sent_at(conn, key)
        if last is not None and (now - last).total_seconds() / 86400 < RENAG_DAYS:
            continue

        mark_sent(conn, key, now.isoformat())
        out.append(
            (
                f"{name}: order a new {label}",
                f"About {info['days_left']} days left (runs out around "
                f"{_pretty_date(info['depletes_on'])}). Order now so the replacement "
                "arrives before it does.",
            )
        )
    return out


def _anomaly_alerts(conn, row: dict, name: str, now: datetime) -> list[tuple[str, str]]:
    """Sensor-health findings, at most one nag per finding type per RENAG_DAYS."""
    wb_id = row.get("water_body_id")
    if not wb_id:
        return []

    history = [
        dict(r)
        for r in conn.execute(
            """SELECT fetched_at, latest_measure_time, free_cl, ph, skimmer_flow, water_temp
               FROM snapshots WHERE water_body_id = ? AND fetched_at >= datetime('now', '-30 days')
               ORDER BY fetched_at""",
            (wb_id,),
        ).fetchall()
    ]
    findings = anomaly.detect(dedupe_by_measurement(history))

    out = []
    for finding in findings:
        out += _once(conn, row, anomaly.signature(finding), [(f"{name}: sensor check", finding["text"])], now)
    return out


def _pretty_date(date_str) -> str:
    if not date_str:
        return "soon"
    return datetime.fromisoformat(date_str).strftime("%b %-d")


def _format_alerts(alerts_json: str, dose_factor: float = None) -> str:
    """RED alert texts, each followed by its dose if WaterGuru worked one out.

    "Calcium Hardness very low" tells you something is wrong; "Add 73 cups of
    90% concentration calcium chloride" tells you what to do about it, already
    sized for this pool's volume. The second is the whole point of the alert.
    """
    try:
        alerts = json.loads(alerts_json)
    except (TypeError, ValueError):
        return ""

    alerts = dosing.annotate_alerts(alerts, dose_factor)
    lines = []
    for a in alerts:
        if a.get("status") != "RED" or not a.get("text"):
            continue
        advice = a.get("advice")
        correction = a.get("dose_correction")
        line = f"{a['text']} - {advice}" if advice else a["text"]
        if correction:
            line += f" ({correction})"
        lines.append(line)
    return "; ".join(lines)


def notify_step_failure(label: str = "Pool data fetch", detail: str = ""):
    """Called by run_and_publish.sh when a pipeline step dies.

    These are the alerts that can't come from the data, because the failure is
    that there is no data - or that it never reached the dashboard. Without them
    a broken pipeline looks exactly like a calm pool.
    """
    send(
        f"{label} failed",
        (detail or "exited non-zero.") + " The dashboard is now serving stale data.",
        tags="rotating_light",
    )


# Kept so an older run_and_publish.sh still works if it isn't updated together.
def notify_fetch_failure(detail: str = ""):
    notify_step_failure("Pool data fetch", detail)


def send_test() -> bool:
    """Sends a test alert down every channel and reports what actually happened.

    Answers the question the alerting can't answer for you: did the push leave
    this machine, or did your phone just not show it?
    """
    load_dotenv()
    topic = os.environ.get("NTFY_TOPIC")

    print("macOS notification: sending...")
    _mac_notification("Pool alert test", "If you can see this, macOS notifications work.")
    print("macOS notification: sent (check Notification Center if you missed it)")

    if not topic:
        print("\nntfy: NTFY_TOPIC is not set in .env - phone pushes are disabled.")
        return False

    print(f"\nntfy: posting to https://ntfy.sh/{topic}")
    message_id = _ntfy_push(
        topic,
        "Pool alert test",
        "Test push from the WaterGuru dashboard. If this reached your phone, alerting works.",
        priority="high",
        tags="white_check_mark",
    )
    if message_id:
        print(f"ntfy: accepted, message id {message_id}")
        print("\nIf your phone stayed silent, the push was delivered to ntfy but not to you:")
        print(f"  1. Open the ntfy app and subscribe to the topic: {topic}")
        print("  2. Check the app's notification permission and battery optimization settings")
        print(f"  3. Confirm in a browser at https://ntfy.sh/{topic} - the test should be listed")
        return True

    print("ntfy: FAILED - the push never left this machine (see the error above)")
    return False


if __name__ == "__main__":
    # Usage: python alerts.py fetch-failed "<detail>" | python alerts.py test
    load_dotenv()
    if len(sys.argv) > 1 and sys.argv[1] == "step-failed":
        notify_step_failure(*sys.argv[2:4])
    elif len(sys.argv) > 1 and sys.argv[1] == "fetch-failed":
        notify_fetch_failure(sys.argv[2] if len(sys.argv) > 2 else "")
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        sys.exit(0 if send_test() else 1)
    else:
        print(__doc__)
        print("Commands:\n  test          send a test alert to every channel"
              "\n  step-failed <label> <detail>   report a failed pipeline step")
