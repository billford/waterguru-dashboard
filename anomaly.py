"""Catches a sensor that has stopped telling the truth.

WaterGuru's own status flags answer "is the water OK?". They don't answer "is
the *sensor* OK?", and a pod that fails quietly is worse than one that fails
loudly - the dashboard stays green and you stop looking at it.

Three failure shapes worth catching:

- **Flatline** - the same value to the decimal across many *distinct*
  measurements. Real pool chemistry drifts; free chlorine especially. Identical
  readings measurement after measurement suggests a spent cassette pad or a
  cached value, not a miraculously stable pool.
- **Implausible jump** - a change between consecutive measurements larger than
  the chemistry can actually move in that time, which usually means a bad read
  rather than a real event.
- **Missing channel** - a field that used to report and has now gone null for
  several measurements running, which is how a partially failed cassette looks.

Everything here works off deduped measurements, so "5 readings" means five
genuine measurements, not five fetches of the same one.
"""
from datetime import datetime, timezone

from freshness import parse_ts

# How many consecutive identical measurements before a flat series is suspicious.
FLATLINE_READINGS = 5

# Largest believable change between two consecutive measurements, per day of gap.
# Chlorine can legitimately swing hard after shocking, so its bound is loose;
# pH is strongly buffered by total alkalinity and moves slowly.
MAX_DAILY_CHANGE = {
    "free_cl": 8.0,
    "ph": 1.2,
    "water_temp": 15.0,
}

# How many consecutive nulls before a channel counts as having dropped out.
MISSING_READINGS = 3

# The jump allowance stops growing past this. Beyond a few days, "how much could
# this plausibly have moved" stops being a useful question.
MAX_GAP_SCALING_DAYS = 3.0

# A jump older than this is history, not news - it shouldn't be reported in the
# present tense on every run forever.
JUMP_FRESHNESS_DAYS = 7.0

FIELD_LABELS = {
    "free_cl": "free chlorine",
    "ph": "pH",
    "water_temp": "water temperature",
    "skimmer_flow": "skimmer flow",
}


def _label(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def _values(rows: list[dict], field: str) -> list:
    return [r.get(field) for r in rows]


def detect_flatline(rows: list[dict], field: str, threshold: int = FLATLINE_READINGS) -> dict | None:
    # Nulls are stripped before the tail is taken, so the count below refers to
    # readings that actually reported, not to calendar measurements.
    values = [v for v in _values(rows, field) if v is not None]
    if len(values) < threshold:
        return None
    tail = values[-threshold:]
    if len(set(tail)) != 1:
        return None
    return {
        "kind": "flatline",
        "field": field,
        "value": tail[0],
        "readings": threshold,
        "text": (
            f"{_label(field)} has read exactly {tail[0]} for the last {threshold} readings that reported. "
            "That's flatter than a real pool drifts - worth checking the cassette."
        ),
    }


def detect_jump(rows: list[dict], field: str, now=None) -> dict | None:
    limit = MAX_DAILY_CHANGE.get(field)
    if limit is None:
        return None
    now = now or datetime.now(timezone.utc)

    points = [
        (parse_ts(r.get("latest_measure_time") or r.get("fetched_at")), r.get(field))
        for r in rows
    ]
    points = [(t, v) for t, v in points if t is not None and v is not None]
    if len(points) < 2:
        return None

    (t_prev, v_prev), (t_last, v_last) = points[-2], points[-1]
    gap_days = (t_last - t_prev).total_seconds() / 86400

    # Capped. Scaling the allowance linearly with an unbounded gap switched the
    # check off exactly when it was most needed: across a 20-day outage the
    # chlorine limit became 160 ppm and the pH limit 24 units, so a pod coming
    # back reporting garbage could never trip it.
    scaled_gap = min(max(gap_days, 0.5), MAX_GAP_SCALING_DAYS)
    change = abs(v_last - v_prev)
    if change <= limit * scaled_gap:
        return None

    # points[-1] is the newest *non-null* reading, which after a dropout can be
    # weeks old. Without this the finding fired forever, present tense, next to
    # the (correct) "stopped reporting" finding.
    if (now - t_last).total_seconds() / 86400 > JUMP_FRESHNESS_DAYS:
        return None

    return {
        "kind": "jump",
        "field": field,
        "from": v_prev,
        "to": v_last,
        "gap_days": round(gap_days, 1),
        "at": t_last.isoformat(),
        "text": (
            f"{_label(field)} jumped from {v_prev} to {v_last} in "
            f"{round(gap_days, 1)} days, which is more than it can really move. "
            "Likely a bad read rather than a real change."
        ),
    }


def detect_missing(rows: list[dict], field: str, threshold: int = MISSING_READINGS) -> dict | None:
    values = _values(rows, field)
    if len(values) < threshold:
        return None
    tail = values[-threshold:]
    if any(v is not None for v in tail):
        return None
    if all(v is None for v in values):
        return None  # never reported at all - not a regression
    return {
        "kind": "missing",
        "field": field,
        "readings": threshold,
        "text": (
            f"{_label(field)} has reported nothing for the last {threshold} measurements, "
            "after previously working."
        ),
    }


def detect(rows: list[dict], fields=("free_cl", "ph", "skimmer_flow", "water_temp")) -> list[dict]:
    """Runs every check over deduped, oldest-first measurements."""
    findings = []
    for field in fields:
        for check in (detect_flatline, detect_jump, detect_missing):
            found = check(rows, field)
            if found:
                findings.append(found)
    return findings


def signature(finding: dict) -> str:
    """Stable identity for a finding, so a reminder can fire once and not repeat."""
    return f"anomaly:{finding['kind']}:{finding['field']}"


def export_anomalies(out_path, days: int = 30):
    """Writes current findings for the dashboard to show alongside the alerts."""
    import json
    from datetime import datetime, timezone

    from db import connect, dedupe_by_measurement

    conn = connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT water_body_id, fetched_at, latest_measure_time,
                          free_cl, ph, skimmer_flow, water_temp
                   FROM snapshots WHERE fetched_at >= datetime('now', ?)
                   ORDER BY water_body_id, fetched_at""",
                (f"-{days} days",),
            ).fetchall()
        ]
    finally:
        conn.close()

    by_wb = {}
    for r in rows:
        by_wb.setdefault(r["water_body_id"], []).append(r)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "waterbodies": {
            wb_id: detect(dedupe_by_measurement(wb_rows)) for wb_id, wb_rows in by_wb.items()
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    return payload
