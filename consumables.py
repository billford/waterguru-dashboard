"""Runway on the two things in the pod that run out: the cassette and the battery.

WaterGuru reports these as human strings ("18 days left", "4 months left")
rather than numbers, so they get parsed back into days to answer the only
question that matters - is it time to order a replacement?

Lead times are deliberately generous. A cassette that arrives the day after the
old one dies still means a blind spot, so the reminder fires while there's
enough runway to order, ship, and forget about it for a bit.
"""
import re
from datetime import timedelta

# Roughly how much notice you want before each one runs out.
CASSETTE_LEAD_DAYS = 14
BATTERY_LEAD_DAYS = 21

_UNIT_DAYS = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}

_TIME_LEFT = re.compile(r"(\d+)\s*(day|week|month|year)s?", re.IGNORECASE)


def parse_time_left(text) -> int | None:
    """"18 days left" -> 18, "4 months left" -> 120. None if unparseable.

    Months and years are approximated; they only ever appear when the runway is
    long enough that the approximation can't change the decision.
    """
    if not text:
        return None
    match = _TIME_LEFT.search(str(text))
    if not match:
        return None
    return int(match.group(1)) * _UNIT_DAYS[match.group(2).lower()]


def runway(row: dict, now) -> dict:
    """Days left and projected depletion date for both consumables."""
    out = {}
    for kind, days_field, pct_field, lead in (
        ("cassette", "cassette_days_left", "cassette_pct_left", CASSETTE_LEAD_DAYS),
        ("battery", "battery_time_left", "battery_pct_left", BATTERY_LEAD_DAYS),
    ):
        days = parse_time_left(row.get(days_field))
        out[kind] = {
            "days_left": days,
            "pct_left": row.get(pct_field),
            "depletes_on": (now + timedelta(days=days)).date().isoformat() if days is not None else None,
            "lead_days": lead,
            "needs_reorder": days is not None and days <= lead,
        }
    return out


def was_replaced(pct_left, prev_pct_left, jump: float = 20.0) -> bool:
    """A consumable level jumping up means a fresh one went in.

    Used to clear a fired reorder reminder so the next depletion cycle can
    remind again, rather than the reminder being a one-time-ever event.
    """
    if pct_left is None or prev_pct_left is None:
        return False
    return pct_left - prev_pct_left >= jump
