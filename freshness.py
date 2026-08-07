"""How old the data is, and whether that's a problem.

There are two independent ways this pipeline can go quiet, and they mean
different things:

- **The fetch stopped.** launchd didn't fire, the Mac was asleep, or Cognito auth
  failed. The dashboard keeps serving whatever it last published, which looks
  fine and isn't. Measured against `fetched_at`.
- **The pod stopped measuring.** The fetch works, WaterGuru answers, but
  `latest_measure_time` hasn't advanced - a dead cassette, a pod knocked out of
  the skimmer, a dead battery. Measured against `latest_measure_time`.

Thresholds are deliberately loose. Fetches run every 12h, so 26h tolerates one
missed run before complaining. The cassette measures roughly daily but not on a
guaranteed schedule, so measurements get 48h before it counts as a fault.
"""
from datetime import datetime, timedelta, timezone

FETCH_STALE_HOURS = 26
MEASURE_STALE_HOURS = 48

# The rest of the chemistry panel (alkalinity, calcium, stabilizer, hardness) is
# measured on a ~monthly cycle - the device marks it valid for 30 days, against
# 2 days for chlorine and pH. Judging it by the daily threshold would report a
# perfectly normal panel as a fault every single day.
PANEL_STALE_DAYS = 35

# After setup, a Sense spends 36 hours (72 scans x 30 min) watching for flow to
# learn when the pump runs before it schedules daily measurements. During that
# window taking no readings is correct behaviour, not a failure - so silence
# gets a grace period rather than an alert. The extra hours past 36 cover the
# gap between the scan finishing and the first scheduled measurement landing.
PUMP_SCAN_HOURS = 36
SETUP_GRACE_HOURS = PUMP_SCAN_HOURS + 26


def parse_ts(value) -> datetime | None:
    """Parses the two timestamp shapes in this data: ours (+00:00) and WaterGuru's (Z)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_hours(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    return round((now - ts).total_seconds() / 3600, 1)


def in_setup_grace(row: dict, now: datetime = None) -> bool:
    """Is this pod still doing its post-setup pump scan?

    Two independent signals, because either alone can mislead: the device's own
    scan state, and the wall-clock time since setup (in case the state string
    ever sticks). Only a pod past both counts as "should be measuring by now".
    """
    now = now or datetime.now(timezone.utc)

    state = row.get("pump_scan_state")
    if state and not state.startswith("DONE"):
        return True

    setup_ts = parse_ts(row.get("pod_setup_time"))
    if setup_ts is not None and (now - setup_ts).total_seconds() / 3600 < SETUP_GRACE_HOURS:
        return True

    return False


def scheduled_measurement(row: dict) -> str | None:
    """The pod's daily measurement time as "HH:MM".

    Reported in the device's own local time, and the payload carries no timezone
    to resolve it against, so it's passed through as a label rather than turned
    into an instant we can't actually justify.
    """
    hour, minute = row.get("meas_hour"), row.get("meas_minute")
    if hour is None or minute is None:
        return None
    return f"{hour:02d}:{minute:02d}"


def freshness_for(rows: list[dict], now: datetime = None) -> dict:
    """Summarizes data age for one water body. `rows` is oldest-first."""
    now = now or datetime.now(timezone.utc)
    if not rows:
        return {
            "last_fetch_at": None,
            "fetch_age_hours": None,
            "last_measure_at": None,
            "measure_age_hours": None,
            "fetch_stale": True,
            "measure_stale": True,
        }

    newest = rows[-1]
    fetch_ts = parse_ts(newest.get("fetched_at"))
    measure_ts = parse_ts(newest.get("latest_measure_time"))
    panel_ts = parse_ts(newest.get("panel_measure_time"))
    fetch_age = _age_hours(fetch_ts, now)
    measure_age = _age_hours(measure_ts, now)
    panel_age = _age_hours(panel_ts, now)
    setting_up = in_setup_grace(newest, now)

    return {
        "last_fetch_at": newest.get("fetched_at"),
        "fetch_age_hours": fetch_age,
        "last_measure_at": newest.get("latest_measure_time"),
        "measure_age_hours": measure_age,
        "last_panel_at": newest.get("panel_measure_time"),
        "panel_age_hours": panel_age,
        "fetch_stale": fetch_age is None or fetch_age > FETCH_STALE_HOURS,
        # A pod still learning the pump schedule isn't stale, it's calibrating.
        "measure_stale": not setting_up and (measure_age is None or measure_age > MEASURE_STALE_HOURS),
        # A panel that has never reported is unknown, not fresh. The daily
        # equivalent above gets this right; this one returned False forever,
        # so a cassette whose panel pads never fired read as perfectly current.
        "panel_stale": not setting_up and (
            panel_age is None or panel_age > PANEL_STALE_DAYS * 24
        ),
        "setting_up": setting_up,
        "scheduled_measurement": scheduled_measurement(newest),
        "next_measurement_at": (
            lambda nxt: nxt.isoformat() if nxt else None
        )(next_expected_measurement(newest, now)),
        "fetch_stale_after_hours": FETCH_STALE_HOURS,
        "measure_stale_after_hours": MEASURE_STALE_HOURS,
    }


def describe_duration(hours: float | None) -> str:
    """A length of time: "6h", "2 days"."""
    if hours is None:
        return "an unknown time"
    if hours < 1:
        return "under an hour"
    if hours < 48:
        return f"{round(hours)}h"
    return f"{round(hours / 24)} days"


def describe_age(hours: float | None) -> str:
    """A point in the past: "6h ago", "2 days ago"."""
    if hours is None:
        return "unknown"
    if hours < 1:
        return "under an hour ago"
    return f"{describe_duration(hours)} ago"


def stale_measurement_alert(row: dict, name: str, now: datetime = None) -> tuple[str, str] | None:
    """A pod that has stopped producing new measurements, while fetches still work.

    Fires off the freshly-fetched row, so it only reaches here when the fetch
    itself succeeded - a dead pipeline is caught by run_and_publish.sh instead.
    """
    now = now or datetime.now(timezone.utc)

    # A freshly set-up pod spends 36 hours scanning for the pump before it
    # schedules anything. Alerting through that window cries wolf on every new
    # install, at exactly the moment the owner is least able to tell a real
    # fault from normal setup behaviour.
    if in_setup_grace(row, now):
        return None

    measure_ts = parse_ts(row.get("latest_measure_time"))
    if measure_ts is None:
        return None
    age = (now - measure_ts).total_seconds() / 3600
    if age <= MEASURE_STALE_HOURS:
        return None
    return (
        f"{name}: no new readings for {describe_duration(age)}",
        "WaterGuru is responding but the pod hasn't measured since "
        f"{measure_ts.strftime('%b %d %H:%M UTC')}. Check the cassette, the battery, "
        "and that the pod is seated in the skimmer.",
    )


DEFAULT_MEAS_INTERVAL_HOURS = 24


def next_expected_measurement(row: dict, now: datetime = None) -> datetime | None:
    """When the pod should next measure.

    The pod has a daily slot (`measDoseTimes`) *and* a minimum interval
    (`measAutoHrs`, normally 24). A slot arriving before the interval has
    elapsed is skipped, which is why reseating a cassette - which triggers an
    immediate off-schedule measurement - can push the next one a day out and
    make a perfectly healthy pod look dead.

    An earlier version of this added the 48-hour *alarm threshold* as if it were
    the cadence, and ignored the schedule entirely. It had no callers, which is
    the only reason it never misled anyone.
    """
    from poolclock import pool_now

    hour, minute = row.get("meas_hour"), row.get("meas_minute")
    last = parse_ts(row.get("latest_measure_time"))
    if hour is None or minute is None or last is None:
        return None

    interval = row.get("meas_auto_hours") or DEFAULT_MEAS_INTERVAL_HOURS
    earliest = last + timedelta(hours=interval)

    # Walk forward through daily slots, in the pod's own local time, until one
    # lands at or after the earliest permitted moment.
    slot = pool_now(now or datetime.now(timezone.utc)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    for _ in range(4):
        if slot >= earliest and slot >= pool_now(now):
            return slot
        slot += timedelta(days=1)
    return None
