"""What day it is *at the pool*.

Timestamps are stored in UTC and should stay that way - that part was never the
problem. The problem is day-boundary decisions made in UTC when everything they
are compared against is local: the NWS forecast is keyed by local calendar days,
the pod's measurement schedule is local, the digest means a local Sunday, and
the owner asking "what about today?" means their today.

`launchd` runs the pipeline at 08:00 and 20:00 local. In an eastern summer that
second run is 00:00 UTC *the next day*, so half of all runs believed it was
tomorrow. Three consequences, each of which reached the dashboard:

- the chlorine projection skipped the current day entirely and priced tomorrow
  as today, discarding the forecast temperature for the day being lived in;
- the swim advisor's prompt asserted the wrong day of the week, so every verdict
  and the heater advice reasoned about the wrong day;
- the weekly digest fired on Saturday evening, because Saturday 20:00 local is
  Sunday 00:00 UTC.

The clock comes from the machine running this, which sits on the same LAN as the
pool controller and therefore beside the pool. Crucially it is DST-aware: the
controller reports a *fixed* `TIMZON` offset (-5 here) with no daylight-saving
information, so trusting it would put the pool an hour out for two thirds of the
swimming season. `POOL_TZ_OFFSET` overrides for the case where the pipeline runs
somewhere other than the pool.

The controller's offset is still read, but used to *check* rather than to
decide - if the two disagree by more than DST can explain, something is
misconfigured and worth saying so.
"""
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYSTEM_JSON = HERE / "site" / "data" / "system.json"


def _offset_from(raw) -> timezone | None:
    if raw is None:
        return None
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return None
    return timezone(timedelta(hours=hours)) if -14 <= hours <= 14 else None


def _configured_offset() -> timezone | None:
    """An explicit override, for running the pipeline away from the pool."""
    return _offset_from(os.environ.get("POOL_TZ_OFFSET"))


def controller_offset_hours() -> float | None:
    """The controller's declared offset. Fixed - it carries no DST information."""
    try:
        return json.loads(SYSTEM_JSON.read_text()).get("utc_offset_hours")
    except (OSError, json.JSONDecodeError):
        return None


def offset_disagreement(now: datetime = None) -> str | None:
    """Flags a machine clock and a controller clock that can't both be right.

    An hour apart is just daylight saving. More than that means one of them is
    set wrong, and every day-boundary decision inherits the error.
    """
    declared = controller_offset_hours()
    if declared is None:
        return None
    local_hours = pool_now(now).utcoffset().total_seconds() / 3600
    drift = abs(local_hours - float(declared))
    if drift <= 1.01:
        return None
    return (
        f"This machine is at UTC{local_hours:+g} but the pool controller is set to "
        f"UTC{float(declared):+g}. Day boundaries follow this machine, so one of them "
        "is wrong by more than daylight saving explains."
    )


def pool_now(now: datetime = None) -> datetime:
    """`now` expressed on the pool's clock.

    Accepts an aware datetime (typically UTC) and returns the same instant in
    the pool's timezone. The instant never changes - only which calendar day it
    falls on, which is the entire point.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    offset = _configured_offset()
    if offset is not None:
        return now.astimezone(offset)
    # The machine running this is on the pool controller's LAN, so it is at the
    # pool - and unlike the controller's fixed TIMZON it knows about DST.
    return now.astimezone()


def pool_today(now: datetime = None) -> date:
    return pool_now(now).date()


def pool_weekday(now: datetime = None) -> int:
    return pool_now(now).weekday()
