"""A once-a-week summary push, so the dashboard has something to say when
nothing is wrong.

Alerts only fire on problems, which means a healthy pool is silent - and a
silent pipeline is indistinguishable from a broken one until you go looking.
The digest closes that gap: one push a week that proves the whole chain still
works and hands over the things worth knowing in advance.

It assembles from the JSON the rest of the run has already written rather than
re-querying anything, so it reflects exactly what the dashboard shows. Keyed by
ISO week in `sent_notifications`, so a catch-up run after a sleeping Mac won't
send a second copy.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from alerts import send
from consumables import runway
from db import connect, last_sent_at, mark_sent, was_sent
from poolclock import pool_now, pool_weekday

HERE = Path(__file__).resolve().parent

# Sunday. Pool decisions are mostly about the week ahead.
DIGEST_WEEKDAY = 6

DIGEST_KEY = "digest"

# Never two digests inside a week, even across a DST shift.
MIN_DIGEST_GAP_DAYS = 6.0

# Past this, a Sunday was missed - send on the next run whatever day it is.
OVERDUE_DAYS = 8.0


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def is_due(now: datetime, conn) -> bool:
    """Sunday at the pool - or overdue, if a Sunday was missed.

    Two bugs here. The weekday was read in UTC, so Saturday 20:00 local (which
    is Sunday 00:00 UTC) fired the "Sunday" digest on Saturday evening.

    And it asked "is it Sunday?" rather than "when did one last go out?", so a
    Mac asleep through both of Sunday's windows dropped that week's digest
    entirely - no record, no retry, nothing due again until the next Sunday.
    The digest exists to prove the pipeline still works when nothing is wrong,
    which makes a silently skipped one the worst failure it has.

    Tracking elapsed time instead means a missed Sunday is caught up on the very
    next run, whatever day that is.
    """
    last = last_sent_at(conn, DIGEST_KEY)
    if last is None:
        return pool_weekday(now) == DIGEST_WEEKDAY

    elapsed_days = (now - last).total_seconds() / 86400
    if elapsed_days < MIN_DIGEST_GAP_DAYS:
        return False
    return pool_weekday(now) == DIGEST_WEEKDAY or elapsed_days >= OVERDUE_DAYS


def _week_key(now: datetime) -> str:
    """Retained for the historical record; delivery is gated on elapsed time."""
    year, week, _ = pool_now(now).isocalendar()
    return f"digest:{year}-W{week:02d}"


def build_digest(history: dict, summary: dict, weather: dict, advice: dict,
                 chlorine: dict, now: datetime) -> tuple[str, str] | None:
    waterbodies = (history or {}).get("waterbodies") or {}
    if not waterbodies:
        return None

    wb_id, wb = next(iter(waterbodies.items()))
    name = wb.get("name") or "Pool"
    latest = wb.get("latest") or {}
    parts = []

    trend = (summary or {}).get(wb_id, {}).get("text")
    if trend:
        parts.append(trend)

    headline = (chlorine or {}).get("headline")
    if headline:
        parts.append(headline)

    swim_days = _good_swim_days(weather, advice)
    if swim_days:
        parts.append("Best swim days ahead: " + ", ".join(swim_days) + ".")
    elif weather:
        parts.append("No standout swim days in the forecast this week.")

    consumables = _consumable_line(latest, now)
    if consumables:
        parts.append(consumables)

    freshness = wb.get("freshness") or {}
    if freshness.get("measure_stale"):
        parts.append("Heads up: the sensor hasn't produced a new measurement recently.")

    return (f"{name}: weekly summary", " ".join(parts))


def _good_swim_days(weather, advice) -> list[str]:
    days = (weather or {}).get("days") or []
    verdicts = (advice or {}).get("days") or {}
    out = []
    for d in days:
        verdict = (verdicts.get(d["date"]) or {}).get("verdict")
        if verdict is None:
            verdict = "good" if d.get("good_swim_day") else "poor"
        if verdict in ("great", "good"):
            out.append(f"{d.get('name', d['date'])} ({d.get('temp_f')}°)")
    return out


def _consumable_line(latest: dict, now: datetime) -> str:
    levels = runway(latest, now)
    bits = []
    for kind, label in (("cassette", "Cassette"), ("battery", "battery")):
        info = levels[kind]
        if info["days_left"] is not None:
            flag = " - order one" if info["needs_reorder"] else ""
            bits.append(f"{label} {info['days_left']}d{flag}")
    return ("Supplies: " + ", ".join(bits) + ".") if bits else ""


def maybe_send_digest(now: datetime = None, force: bool = False, site_data: Path = None):
    now = now or datetime.now(timezone.utc)
    data = site_data or (HERE / "site" / "data")

    conn = connect()
    try:
        if not force and not is_due(now, conn):
            return None

        digest = build_digest(
            _load(data / "history.json"),
            _load(data / "summary.json"),
            _load(data / "weather.json"),
            _load(data / "swim_advice.json"),
            _load(data / "chlorine_forecast.json"),
            now,
        )
        if digest is None:
            return None

        title, message = digest
        send(title, message, priority="default", tags="swimmer")
        if not force:
            mark_sent(conn, DIGEST_KEY, now.isoformat())
            mark_sent(conn, _week_key(now), now.isoformat())
        return digest
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    print(maybe_send_digest(force="--force" in sys.argv))
