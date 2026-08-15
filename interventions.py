"""Things that happened to the pool that nobody wrote down.

The equipment log records what the controller did. The event log records what
you told it you did. This covers the gap between them: chemistry that moved in a
direction the pool cannot move on its own, which means a person did it.

That gap is the whole point of the dashboard for a pool under service. A visit
that adds fifty pounds of salt and a jug of chlorine leaves no note, no receipt
in the controller, and no entry anywhere - but it does leave a signature in the
numbers, because salt and chlorine can only rise if someone raised them.

**Salt.** Conservative: it doesn't evaporate, degrade or get consumed, and the
cell recycles it indefinitely. It falls only through dilution and rises only
through addition. Evaporation concentrates it, but slowly and steadily - a step
change over hours is a bag of salt, not a hot afternoon, which is why rate
matters as much as size here.

**Chlorine.** The cell's maximum output over an interval is known exactly
(`saltcell.generation_ppm_per_day` at 100%). A rise larger than that is chlorine
that came from somewhere else.

This module reports what the numbers show and converts it to familiar units. It
does not attribute motive or judge whether the addition was warranted - a
service visit adding salt to a pool that needed salt looks identical to one
adding salt to a pool that didn't, and only the reading tells you which.
"""
from datetime import timedelta

from freshness import parse_ts

WATER_LB_PER_GALLON = 8.34

# A rise this large is an addition rather than evaporation.
SALT_STEP_PPM = 150
SALT_STEP_WINDOW_HOURS = 12

# ...but not this fast. Salt takes hours to dissolve and circulate, so a jump
# inside a few minutes is the cell's reading moving, not the pool's chemistry.
# Without this the detector reported three 25 lb additions that never happened,
# each "occurring" in six minutes, all at the same time of day.
SALT_MIN_RISE_HOURS = 1.0

# And it has to stick. Salt that goes up and comes back down was never added -
# the pool cannot lose it. Confirmation is the median of the readings that
# follow, which ignores a spike however large.
SALT_PERSIST_HOURS = 6.0
SALT_PERSIST_SAMPLES = 3

# Chlorine above what the cell could have made in the interval, with headroom
# for measurement noise on both readings.
CHLORINE_TOLERANCE_PPM = 0.5


def salt_pounds(delta_ppm: float, gallons: float) -> float | None:
    """Pounds of salt needed to raise a pool by `delta_ppm`."""
    if not gallons or delta_ppm is None:
        return None
    return delta_ppm * gallons * WATER_LB_PER_GALLON / 1_000_000


def chlorine_pounds(delta_ppm: float, gallons: float) -> float | None:
    if not gallons or delta_ppm is None:
        return None
    return delta_ppm * gallons * WATER_LB_PER_GALLON / 1_000_000


def detect_salt_addition(rows: list[dict], gallons: float = None) -> list[dict]:
    """Step rises in salt concentration, which only a person can cause.

    Three things must hold, and the last two were missing: the rise must be
    large, it must take long enough to be physically possible, and the new
    level must persist. Salt cannot leave the pool, so a rise that reverses was
    an instrument artefact rather than a bag of salt.

    `rows` are system snapshots, oldest first.
    """
    points = []
    for r in rows:
        ts, salt = parse_ts(r.get("read_at")), r.get("salt_ppm")
        if ts is not None and salt:
            points.append((ts, salt))

    found = []
    for i, ((t0, s0), (t1, s1)) in enumerate(zip(points, points[1:])):
        rise = s1 - s0
        gap_hours = (t1 - t0).total_seconds() / 3600
        if rise < SALT_STEP_PPM or gap_hours > SALT_STEP_WINDOW_HOURS:
            continue
        if gap_hours < SALT_MIN_RISE_HOURS:
            continue  # faster than salt can dissolve and circulate
        if not _persists(points, i + 1, s0, rise):
            continue  # went back down, so nothing was added

        pounds = salt_pounds(rise, gallons)
        bags = f" - about {pounds / 40:.1f} × 40 lb bags" if pounds else ""
        found.append({
            "kind": "salt_added",
            "at": t1.isoformat(),
            "from": s0,
            "to": s1,
            "delta_ppm": round(rise, 1),
            "pounds": round(pounds, 1) if pounds else None,
            "text": (
                f"Salt rose from {s0:g} to {s1:g} ppm in {gap_hours:.1f}h"
                + (f", which takes roughly {pounds:.0f} lb of salt{bags}" if pounds else "")
                + ". Salt only rises if someone adds it."
            ),
        })
    return found


def detect_chlorine_addition(rows: list[dict], generation_ppm_per_day: float = 0.0,
                             gallons: float = None) -> list[dict]:
    """Chlorine rising faster than the cell could possibly have raised it.

    `rows` are deduped chemistry measurements, oldest first.
    """
    points = []
    for r in rows:
        ts = parse_ts(r.get("latest_measure_time") or r.get("fetched_at"))
        if ts is not None and r.get("free_cl") is not None:
            points.append((ts, r["free_cl"]))

    found = []
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        gap_days = (t1 - t0).total_seconds() / 86400
        if gap_days <= 0:
            continue

        rise = v1 - v0
        # The cell can only ever have added; the pool can only ever have
        # consumed. So the most the cell could account for is its full output
        # over the interval, with nothing lost - a deliberately generous bound.
        cell_could_add = generation_ppm_per_day * gap_days
        unexplained = rise - cell_could_add - CHLORINE_TOLERANCE_PPM
        if unexplained <= 0:
            continue

        pounds = chlorine_pounds(unexplained, gallons)
        found.append({
            "kind": "chlorine_added",
            "at": t1.isoformat(),
            "from": v0,
            "to": v1,
            "delta_ppm": round(rise, 2),
            "unexplained_ppm": round(unexplained, 2),
            "pounds": round(pounds, 2) if pounds else None,
            "text": (
                f"Free chlorine rose from {v0:g} to {v1:g} ppm over {gap_days:.1f} days - "
                f"{unexplained:.1f} ppm more than the cell could have produced at full output"
                + (f", roughly {pounds:.1f} lb of chlorine" if pounds else "")
                + ". Something was added by hand."
            ),
        })
    return found


def _persists(points, start_index: int, before: float, rise: float) -> bool:
    """Did the raised level hold, or was it a spike?

    Confirmed against the median of the readings in the window that follows, so
    one more excursion in either direction cannot decide it either way.
    """
    from statistics import median

    end = points[start_index][0] + timedelta(hours=SALT_PERSIST_HOURS)
    after = [salt for ts, salt in points[start_index:] if ts <= end]
    if len(after) < SALT_PERSIST_SAMPLES:
        return False          # not enough evidence yet; wait rather than guess
    # Still clearly above where it started, allowing for the reading's coarseness.
    return median(after) >= before + rise * 0.5


def summarize(salt_events: list[dict], chlorine_events: list[dict], days: int = 30) -> str | None:
    """A neutral one-line summary of unlogged additions."""
    total = len(salt_events) + len(chlorine_events)
    if not total:
        return None

    parts = []
    if salt_events:
        pounds = sum(e["pounds"] or 0 for e in salt_events)
        parts.append(f"{len(salt_events)} salt addition{'s' if len(salt_events) != 1 else ''}"
                     + (f" (~{pounds:.0f} lb)" if pounds else ""))
    if chlorine_events:
        parts.append(f"{len(chlorine_events)} manual chlorine addition"
                     f"{'s' if len(chlorine_events) != 1 else ''}")
    return (
        f"Detected in the last {days} days: " + " and ".join(parts) +
        ". These weren't logged by hand or done by the controller, so they were done by a person."
    )
