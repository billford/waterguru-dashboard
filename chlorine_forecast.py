"""Projects free chlorine forward: when does it come back into range, and when
does it fall through the floor?

Nothing else in this pipeline answers the question you actually act on. The
dashboard tells you chlorine is 9.8 ppm against a 3.0 target; what you want to
know is whether to do anything about it, and that depends on how fast this pool
burns chlorine and how hot the next few days are.

**How the rate is worked out.** Between consecutive measurements, chlorine only
ever goes down on its own - any increase means someone added some, so rising
segments are thrown away rather than averaged in. The remaining declines give a
ppm/day loss rate; the median is used so one weird reading can't drag it. Each
segment is temperature-normalized before averaging and re-scaled per forecast
day afterwards, because chlorine demand climbs with heat and sun.

**The temperature model is an approximation**, not pool-chemistry gospel: loss
scales by `Q10 ** ((temp - 80F) / 10)`, a standard rate-doubling-per-10-degrees
shape with a deliberately gentle exponent. It captures "hot week burns faster"
without pretending to model UV index or bather load.

**Stabilizer is modelled too**, because it's the single biggest control on how
fast sunlight destroys chlorine, and the device does report it (CYA, on a
monthly cycle). An unstabilized pool in summer sun can lose most of its free
chlorine in a day; 30-50 ppm of cyanuric acid slows that dramatically. The
adjustment is a coarse band multiplier, not a photochemistry model.

Both adjustments are applied the same careful way: segments are **divided** by
their temperature and CYA factors before being pooled into a baseline, then the
baseline is **multiplied** by the projected day's factors. That normalization is
what stops the model double-counting - a rate fitted from a hot, unstabilized
week already has that burn baked in, so re-applying the multiplier on top would
compound it. It also means the projection responds correctly when conditions
change: add stabilizer and the same fitted history projects a slower burn.

**With too little history** (fewer than three usable declines) it falls back to
a generic outdoor-pool loss rate and says so, and the dashboard labels the
result low-confidence. Better a flagged estimate than a confident fiction.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import trust
from db import connect, dedupe_by_measurement
from freshness import parse_ts

HERE = Path(__file__).resolve().parent

# Loss-rate model. REF_TEMP_F is where DEFAULT_RATE applies; Q10 is the factor
# the rate changes by per 10°F away from it.
REF_TEMP_F = 80.0
Q10 = 1.6
DEFAULT_RATE_PPM_PER_DAY = 1.5

# Declines needed before trusting a fitted rate over the generic one.
MIN_SEGMENTS = 3

# Days to project. Runs past the 5-day forecast; days beyond it reuse the last
# forecast temperature, which is flagged in the output as an assumption.
HORIZON_DAYS = 14

LOOKBACK_DAYS = 60

# Chlorine is considered "back in range" once it's within this multiple of
# target - WaterGuru reports a single target, not a band.
IN_RANGE_FACTOR = 1.35


# Chlorine loss multipliers by cyanuric acid level, relative to an adequately
# stabilized pool (30+ ppm, the bottom of the device's own GREEN band). Coarse
# bands rather than a curve, because the honest precision here is low.
CYA_BANDS = (
    (30.0, 1.0),   # adequately stabilized - the reference
    (20.0, 1.3),
    (10.0, 1.8),
    (0.0, 2.5),    # essentially unprotected from UV
)


def _temp_factor(temp_f: float | None) -> float:
    if temp_f is None:
        return 1.0
    return Q10 ** ((temp_f - REF_TEMP_F) / 10.0)


def _cya_factor(cya: float | None) -> float:
    """How much faster this pool burns chlorine for want of stabilizer."""
    if cya is None:
        return 1.0
    for threshold, factor in CYA_BANDS:
        if cya >= threshold:
            return factor
    return CYA_BANDS[-1][1]


def decay_segments(rows: list[dict]) -> list[dict]:
    """Per-day chlorine losses between consecutive measurements, dosing excluded."""
    points = []
    for r in rows:
        ts = parse_ts(r.get("latest_measure_time") or r.get("fetched_at"))
        if ts is not None and r.get("free_cl") is not None:
            points.append((ts, r["free_cl"], r.get("water_temp"), r.get("cya")))

    segments = []
    for (t0, v0, temp0, cya0), (t1, v1, temp1, cya1) in zip(points, points[1:]):
        gap_days = (t1 - t0).total_seconds() / 86400
        if gap_days <= 0:
            continue
        drop = v0 - v1
        if drop <= 0:
            continue  # chlorine was added - tells us nothing about natural loss
        temps = [t for t in (temp0, temp1) if t is not None]
        avg_temp = sum(temps) / len(temps) if temps else None
        cya = cya1 if cya1 is not None else cya0
        rate = drop / gap_days
        segments.append(
            {
                "from": t0.isoformat(),
                "to": t1.isoformat(),
                "gap_days": round(gap_days, 2),
                "drop_ppm": round(drop, 2),
                "rate_ppm_per_day": rate,
                "avg_temp_f": avg_temp,
                "cya": cya,
                # Divided out so segments measured under different heat and
                # stabilizer conditions can be pooled into one baseline, then
                # re-applied per projected day. Without this the model would
                # double-count whatever conditions it was fitted under.
                "normalized_rate": rate / (_temp_factor(avg_temp) * _cya_factor(cya)),
            }
        )
    return segments


def fit_rate(rows: list[dict]) -> dict:
    """Baseline ppm/day loss at REF_TEMP_F, fitted if there's enough history."""
    segments = decay_segments(rows)
    if len(segments) >= MIN_SEGMENTS:
        return {
            "ppm_per_day": round(median(s["normalized_rate"] for s in segments), 3),
            "source": "fitted",
            "segments": len(segments),
            "ref_temp_f": REF_TEMP_F,
            "confidence": "high" if len(segments) >= 6 else "medium",
        }
    return {
        "ppm_per_day": DEFAULT_RATE_PPM_PER_DAY,
        "source": "default",
        "segments": len(segments),
        "ref_temp_f": REF_TEMP_F,
        "confidence": "low",
    }


def _forecast_temps(weather: dict | None) -> dict:
    if not weather:
        return {}
    return {d["date"]: d.get("temp_f") for d in weather.get("days", []) if d.get("date")}


def project(start_value, start_date, rate_ppm_per_day, forecast_temps, fallback_temp,
            horizon=HORIZON_DAYS, cya=None) -> list[dict]:
    """Steps chlorine forward a day at a time, scaling loss by heat and stabilizer."""
    out = []
    value = start_value
    cya_factor = _cya_factor(cya)
    for i in range(1, horizon + 1):
        day = start_date + timedelta(days=i)
        key = day.date().isoformat()
        temp = forecast_temps.get(key)
        assumed = temp is None
        if assumed:
            temp = fallback_temp
        value = max(0.0, value - rate_ppm_per_day * _temp_factor(temp) * cya_factor)
        out.append(
            {
                "date": key,
                "free_cl": round(value, 2),
                "temp_f": temp,
                "temp_assumed": assumed,
            }
        )
    return out


def decay_to_now(value, measured_at, now, rate_ppm_per_day, forecast_temps, fallback_temp, cya=None):
    """Chlorine level right now, decayed forward from the last measurement.

    Walks whole days at each day's own temperature, then applies the remaining
    part-day, so a reading taken an hour ago barely moves and one from three
    days ago moves by three days of that period's actual weather.
    """
    cya_factor = _cya_factor(cya)
    elapsed = max(0.0, (now - measured_at).total_seconds() / 86400)
    remaining = elapsed
    cursor = measured_at

    while remaining > 0:
        step = min(1.0, remaining)
        temp = forecast_temps.get(cursor.date().isoformat(), fallback_temp)
        value = max(0.0, value - rate_ppm_per_day * _temp_factor(temp) * cya_factor * step)
        cursor += timedelta(days=step)
        remaining -= step

    return round(value, 2)


def _first_date_where(projection, predicate):
    for p in projection:
        if predicate(p["free_cl"]):
            return p["date"]
    return None


def _headline(measured, estimated, target, ceiling, floor, in_range_on, below_on,
              rate, stale_days) -> str:
    if measured is None or target is None:
        return "Not enough data yet to project chlorine."

    hedge = "roughly " if rate["confidence"] == "low" else ""

    # When the last measurement is old, the number on the dashboard isn't the
    # number in the pool - lead with the estimate and say where it came from.
    # When it's fresh, the two agree and quoting the measurement is clearer.
    if stale_days >= 1:
        opening = (
            f"Last measured {measured} ppm {stale_days} day{'s' if stale_days != 1 else ''} ago; "
            f"estimated {estimated} ppm now"
        )
        subject = estimated
    else:
        opening = f"Chlorine is {measured} ppm"
        subject = measured

    if ceiling is not None and subject > ceiling:
        if in_range_on:
            return (
                f"{opening}, above the {ceiling} ppm top of range. At the current burn rate it "
                f"should be back in range {hedge}around {_pretty(in_range_on)} - no need to add "
                "any before then."
            )
        return (
            f"{opening}, above the {ceiling} ppm top of range, and won't be back in range "
            f"within {HORIZON_DAYS} days at the current burn rate."
        )
    if floor is not None and subject < floor:
        return f"{opening}, below the {floor} ppm bottom of range - it needs topping up."
    if below_on:
        return (
            f"{opening}, in range, dropping below {floor} ppm {hedge}around {_pretty(below_on)}."
        )
    return f"{opening}, in range and holding there for the next {HORIZON_DAYS} days."


def _stabilizer_note(cya, cya_target) -> str | None:
    """Explains a fast burn when the cause is missing stabilizer, not the pool.

    Worth saying out loud: low CYA makes chlorine look like it's vanishing, and
    the intuitive fix (add more chlorine) treats the symptom while the actual
    problem is that sunlight is destroying it as fast as it goes in.
    """
    if cya is None:
        return None
    factor = _cya_factor(cya)
    if factor <= 1.0:
        return None
    target_text = f" (target {cya_target:g})" if cya_target else ""
    return (
        f"Stabilizer is low at {cya:g} ppm{target_text}, so chlorine burns off in sunlight "
        f"roughly {factor:g}x faster than it would in a stabilized pool. Bringing CYA up "
        "will do more for chlorine holding than adding more chlorine will."
    )


def _pretty(date_str: str) -> str:
    return datetime.fromisoformat(date_str).strftime("%a %b %-d")


def build_forecast(rows: list[dict], target, weather: dict | None, now: datetime = None,
                   cya_target=None, green_min=None, green_max=None,
                   untrusted: set = None) -> dict:
    now = now or datetime.now(timezone.utc)
    untrusted = untrusted or set()
    rows = dedupe_by_measurement(rows)
    measured = [r for r in rows if r.get("free_cl") is not None]

    # A reading taken from water that hasn't circulated describes the pipe, not
    # the pool. Fitting a burn rate to it, or anchoring a projection on it,
    # launders a bad sample into a confident recommendation.
    excluded = [r for r in measured if (r.get("latest_measure_time") or r.get("fetched_at")) in untrusted]
    measured = [r for r in measured if r not in excluded]

    if not measured:
        return {
            "generated_at": now.isoformat(),
            "available": False,
            "excluded_measurements": len(excluded),
            "headline": (
                "The most recent readings were taken in conditions that make them unreliable, "
                "so there's nothing dependable to project from yet."
                if excluded else "No chlorine readings yet."
            ),
        }

    latest = measured[-1]
    current = latest["free_cl"]
    measured_at = parse_ts(latest.get("latest_measure_time") or latest.get("fetched_at")) or now

    rate = fit_rate(measured)
    forecast_temps = _forecast_temps(weather)
    fallback_temp = (
        list(forecast_temps.values())[-1] if forecast_temps else latest.get("water_temp")
    )

    # The cassette measures every day or two, so the last reading is usually
    # already hours or days old. The projection is anchored at *now* - decay the
    # measurement forward to the present first, then forecast from there. The
    # earlier version started the daily walk at the measurement and trimmed, so
    # a reading taken an hour ago reported tomorrow's estimate as "now".
    stale_days = max(0, (now.date() - measured_at.date()).days)
    cya = next((r.get("cya") for r in reversed(measured) if r.get("cya") is not None), None)

    estimated_now = decay_to_now(
        current, measured_at, now, rate["ppm_per_day"], forecast_temps, fallback_temp, cya
    )
    projection = [
        {
            "date": now.date().isoformat(),
            "free_cl": estimated_now,
            "temp_f": forecast_temps.get(now.date().isoformat(), fallback_temp),
            "temp_assumed": now.date().isoformat() not in forecast_temps,
            "estimated": True,
        }
    ] + project(
        estimated_now, now, rate["ppm_per_day"], forecast_temps, fallback_temp,
        horizon=HORIZON_DAYS, cya=cya,
    )

    # "In range" means what the device means, when it tells us: it judges
    # chlorine against a green band (1.6-5.4 ppm around a 3.0 target), not
    # against the target alone.
    ceiling = green_max if green_max is not None else (target * IN_RANGE_FACTOR if target else None)
    floor = green_min if green_min is not None else target

    in_range_on = _first_date_where(projection, lambda v: v <= ceiling) if ceiling else None
    below_target_on = _first_date_where(projection, lambda v: v < floor) if floor else None

    return {
        "generated_at": now.isoformat(),
        "available": True,
        "current": {
            "free_cl": current,
            "measured_at": latest.get("latest_measure_time") or latest.get("fetched_at"),
            "measurement_age_days": stale_days,
            "estimated_now": estimated_now,
            "target": target,
            "green_min": floor,
            "green_max": ceiling,
            "water_temp": latest.get("water_temp"),
            "cya": cya,
            "cya_factor": round(_cya_factor(cya), 2),
        },
        "rate": rate,
        "projection": projection,
        "in_range_on": in_range_on,
        "below_target_on": below_target_on,
        "headline": _headline(
            current, estimated_now, target, ceiling, floor,
            in_range_on, below_target_on, rate, stale_days,
        ),
        "stabilizer_note": _stabilizer_note(cya, cya_target),
        "excluded_measurements": len(excluded),
        "model": {
            "ref_temp_f": REF_TEMP_F,
            "q10": Q10,
            "horizon_days": HORIZON_DAYS,
            "note": "Loss scales with forecast air temperature and stabilizer level; "
                    "ignores UV index and bather load.",
        },
    }


def export_forecast(weather_path: Path, out_path: Path, now: datetime = None):
    weather = None
    if weather_path.exists():
        try:
            weather = json.loads(weather_path.read_text())
        except json.JSONDecodeError:
            weather = None

    conn = connect()
    try:
        wb = conn.execute(
            """SELECT water_body_id, name, free_cl_target, cya_target,
                      free_cl_green_min, free_cl_green_max
               FROM snapshots ORDER BY fetched_at DESC LIMIT 1"""
        ).fetchone()
        if wb is None:
            payload = {"available": False, "headline": "No readings yet."}
        else:
            rows = [
                dict(r)
                for r in conn.execute(
                    """SELECT fetched_at, latest_measure_time, free_cl, water_temp, cya, alerts_json
                       FROM snapshots
                       WHERE water_body_id = ? AND fetched_at >= datetime('now', ?)
                       ORDER BY fetched_at""",
                    (wb["water_body_id"], f"-{LOOKBACK_DAYS} days"),
                ).fetchall()
            ]
            payload = build_forecast(
                rows, wb["free_cl_target"], weather, now,
                cya_target=wb["cya_target"],
                green_min=wb["free_cl_green_min"],
                green_max=wb["free_cl_green_max"],
                untrusted=trust.untrusted_keys(rows),
            )
            payload["water_body_id"] = wb["water_body_id"]
    finally:
        conn.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    return payload


if __name__ == "__main__":
    print(
        export_forecast(
            HERE / "site" / "data" / "weather.json",
            HERE / "site" / "data" / "chlorine_forecast.json",
        )["headline"]
    )
