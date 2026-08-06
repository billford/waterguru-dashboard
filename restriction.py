"""Detecting a restriction building in the plumbing, from pump telemetry.

A variable-speed pump holds the RPM it is told to hold. So at a *fixed* speed,
the flow it achieves is a measurement of how hard the water finds it to get
through: a filling filter, a loading skimmer basket, a pump basket, a closing
valve, or - as happened here - a cassette wedging a skimmer weir open. Flow
falling while speed is unchanged means resistance rising upstream.

This is the check that would have caught the incident that motivated it. The
weir problem developed over roughly 48 hours and was eventually noticed because
WaterGuru's flow sensor went silent and the pod stopped measuring entirely, by
which point the chemistry was two days stale. Flow at fixed RPM would have been
visibly falling well before any of that, and the pump console - which reports
total system flow and looked perfectly healthy - could never have shown it.

**Comparisons are made within an RPM bucket.** Comparing raw GPM across
different speeds would read every schedule change as a restriction. Only
readings at the same speed are compared with each other.

**Baseline versus recent, both medians.** A pump reports instantaneous flow, and
a single low sample means nothing. The baseline is the older half of the
history at that speed; recent is the newest cluster. A restriction shows up as
the recent median sitting persistently below the baseline.
"""
from statistics import median

from freshness import parse_ts

# Round RPM to this, so 2048 and 2052 are the same operating point.
RPM_BUCKET = 50

# Flow this far below the baseline at the same speed is worth reporting.
WARN_DROP_FRACTION = 0.15
SEVERE_DROP_FRACTION = 0.30

# Readings needed on each side before the comparison means anything.
MIN_BASELINE_SAMPLES = 12
MIN_RECENT_SAMPLES = 3

# The newest readings form the "recent" cluster.
RECENT_SAMPLES = 6


def _bucket(rpm) -> int | None:
    if not rpm:
        return None
    return int(round(rpm / RPM_BUCKET) * RPM_BUCKET)


def usable_readings(rows: list[dict]) -> list[dict]:
    """Rows with a real flow measurement at a real speed, oldest first."""
    out = []
    for r in rows:
        rpm, gpm = r.get("pump_rpm"), r.get("pump_gpm")
        if not rpm or not gpm or gpm <= 0:
            continue
        out.append({"at": r.get("read_at"), "rpm": rpm, "gpm": gpm,
                    "watts": r.get("pump_watts"), "bucket": _bucket(rpm)})
    out.sort(key=lambda r: r["at"] or "")
    return out


def detect(rows: list[dict]) -> dict | None:
    """Flow degradation at a fixed pump speed, or None."""
    readings = usable_readings(rows)
    if len(readings) < MIN_BASELINE_SAMPLES + MIN_RECENT_SAMPLES:
        return None

    # Work at the speed the pump is running *now*; other buckets describe
    # operating points it isn't currently in.
    current_bucket = readings[-1]["bucket"]
    at_speed = [r for r in readings if r["bucket"] == current_bucket]
    if len(at_speed) < MIN_BASELINE_SAMPLES + MIN_RECENT_SAMPLES:
        return None

    recent = at_speed[-RECENT_SAMPLES:]
    baseline = at_speed[:-RECENT_SAMPLES]
    if len(recent) < MIN_RECENT_SAMPLES or len(baseline) < MIN_BASELINE_SAMPLES:
        return None

    baseline_gpm = median(r["gpm"] for r in baseline)
    recent_gpm = median(r["gpm"] for r in recent)
    if not baseline_gpm:
        return None

    drop = (baseline_gpm - recent_gpm) / baseline_gpm
    if drop < WARN_DROP_FRACTION:
        return None

    severity = "severe" if drop >= SEVERE_DROP_FRACTION else "warning"
    return {
        "kind": "restriction",
        "severity": severity,
        "rpm": current_bucket,
        "baseline_gpm": round(baseline_gpm, 1),
        "recent_gpm": round(recent_gpm, 1),
        "drop_fraction": round(drop, 3),
        "baseline_samples": len(baseline),
        "text": (
            f"Flow has dropped {drop * 100:.0f}% at the same pump speed - "
            f"{recent_gpm:.0f} gpm now against {baseline_gpm:.0f} gpm normal at {current_bucket} rpm. "
            "The pump is working just as hard, so the resistance is upstream of it: "
            "check the skimmer basket, the pump basket, the filter, and the valves. "
            "Total system flow can look fine while one skimmer is starved."
        ),
    }


def status(rows: list[dict]) -> dict:
    """What the dashboard shows: the finding, or why there isn't one yet."""
    readings = usable_readings(rows)
    finding = detect(rows)
    current = readings[-1] if readings else None

    return {
        "available": finding is not None,
        "finding": finding,
        "samples": len(readings),
        "samples_needed": MIN_BASELINE_SAMPLES + MIN_RECENT_SAMPLES,
        "current_gpm": current["gpm"] if current else None,
        "current_rpm": current["rpm"] if current else None,
        "current_watts": current["watts"] if current else None,
    }
