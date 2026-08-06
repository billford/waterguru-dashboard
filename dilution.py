"""Detecting water top-ups from the salt reading.

Salt is conservative: it doesn't evaporate, degrade in sunlight, or get consumed
by anything. The cell recycles it indefinitely. So the only ordinary way salt
concentration *falls* is dilution - fresh water going in. That makes the salt
reading an accidental flowmeter for top-ups, and a rather good one, because it
measures the thing that matters (how much the pool was diluted) rather than how
long a hose ran.

This matters because dilution corrupts the chlorine burn-rate fit. Chlorine
drops when water is added without the pool having consumed any, and fitted as
demand that reads as a catastrophic burn rate. Top-ups were previously recorded
by hand, which works right up until you forget - and forgetting is the normal
case, since the notebook is never where the pool is.

**How much water.** If salt falls from S0 to S1, the fraction of the pool that
is now fresh water is `1 - S1/S0`, so the volume added is
`volume * (1 - S1/S0)`. Evaporation works in the opposite direction and
concentrates salt, so a rise is never read as a top-up.

**Why the threshold is generous.** The cell's salt reading is temperature
compensated and noisy at the ±100 ppm level, and it re-reads only periodically.
A 3% drop on a 15,000 gallon pool is around 450 gallons, which is a real top-up
and comfortably outside the noise.
"""
from datetime import timedelta

# Minimum drop, as a fraction of the previous reading, before it counts as
# dilution rather than sensor noise.
MIN_DROP_FRACTION = 0.03

# Below this the arithmetic is meaningless.
MIN_PLAUSIBLE_SALT = 500


def dilution_fraction(previous_salt, current_salt) -> float | None:
    """The fraction of the pool that is newly-added water, or None."""
    if not previous_salt or not current_salt:
        return None
    if previous_salt < MIN_PLAUSIBLE_SALT or current_salt < MIN_PLAUSIBLE_SALT:
        return None
    if current_salt >= previous_salt:
        return None  # concentrating, i.e. evaporating - the opposite of a top-up
    return 1 - (current_salt / previous_salt)


def detect_top_up(previous_salt, current_salt, gallons=None) -> dict | None:
    """A top-up inferred from a fall in salt concentration."""
    fraction = dilution_fraction(previous_salt, current_salt)
    if fraction is None or fraction < MIN_DROP_FRACTION:
        return None

    added = round(gallons * fraction) if gallons else None
    detail = f" - roughly {added:,} gallons" if added else ""
    return {
        "fraction": round(fraction, 4),
        "gallons_added": added,
        "from_salt": previous_salt,
        "to_salt": current_salt,
        "text": (
            f"Salt fell from {previous_salt:g} to {current_salt:g} ppm, which means water was "
            f"added{detail}. Salt doesn't evaporate or get used up, so a drop can only be dilution."
        ),
    }


def recent_salt_pair(rows: list[dict], window_hours: int = 24):
    """Salt now versus salt a window ago, using medians to ride out noise.

    A single pair of readings is too noisy to act on; comparing the middle of
    two clusters is not.
    """
    salted = [r for r in rows if r.get("salt_ppm")]
    if len(salted) < 2:
        return None, None

    from freshness import parse_ts
    from statistics import median

    newest_at = parse_ts(salted[-1].get("read_at"))
    if newest_at is None:
        return None, None
    cutoff = newest_at - timedelta(hours=window_hours)

    older = [r["salt_ppm"] for r in salted if (parse_ts(r.get("read_at")) or newest_at) < cutoff]
    newer = [r["salt_ppm"] for r in salted if (parse_ts(r.get("read_at")) or newest_at) >= cutoff]
    if not older or not newer:
        return None, None
    return median(older), median(newer)
