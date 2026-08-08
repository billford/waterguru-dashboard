"""Exports the SQLite history into site/data/history.json for the static dashboard.

Two things share the `snapshots` table but move at different rates, and the
export keeps them apart:

- **Chemistry** (free chlorine, pH, skimmer flow) comes from the cassette, which
  only measures every day or two. Repeat fetches see the same measurement, so
  these are deduped on `latest_measure_time` and timestamped with it.
- **Water temperature** is read by the pod on every fetch, so it gets its own
  per-fetch series timestamped with `fetched_at`.

Charting both off `fetched_at` would draw a single chemistry measurement as
several distinct readings.
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import dosing
import lsi
import trust
from db import connect, dedupe_by_measurement
from freshness import freshness_for

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "site" / "data" / "history.json"
DAYS_KEPT = 180


def _chemistry_point(r: dict, verdicts: dict = None, actual_gallons: float = None) -> dict:
    key = r["latest_measure_time"] or r["fetched_at"]
    return {
        "t": key,
        "trust": (verdicts or {}).get(key, {"trusted": True, "reasons": [], "source": "auto"}),
        "fetched_at": r["fetched_at"],
        "status": r["status"],
        "water_temp": r["water_temp"],
        "free_cl": r["free_cl"],
        "ph": r["ph"],
        "skimmer_flow": r["skimmer_flow"],
        # The slow panel rides along on the same row, but carries its own
        # measurement time - it's on a ~monthly cycle, not the daily one.
        "ta": r["ta"],
        "ch": r["ch"],
        "cya": r["cya"],
        "th": r["th"],
        "panel_measure_time": r["panel_measure_time"],
        "cassette_pct_left": r["cassette_pct_left"],
        "cassette_days_left": r["cassette_days_left"],
        "battery_pct_left": r["battery_pct_left"],
        "battery_time_left": r["battery_time_left"],
        "rssi": r["rssi"],
        "rssi_desc": r["rssi_desc"],
        "alerts": dosing.annotate_alerts(
            json.loads(r["alerts_json"]) if r["alerts_json"] else [],
            dosing.scale_factor(actual_gallons, r.get("size_gallons")),
        ),
    }


def _latest_block(r: dict) -> dict:
    """Per-fetch equipment state - fresh on every run, unlike the chemistry."""
    return {
        "fetched_at": r["fetched_at"],
        "status": r["status"],
        "water_temp": r["water_temp"],
        "cassette_pct_left": r["cassette_pct_left"],
        "cassette_days_left": r["cassette_days_left"],
        "cassette_status": r["cassette_status"],
        "battery_pct_left": r["battery_pct_left"],
        "battery_time_left": r["battery_time_left"],
        "battery_status": r["battery_status"],
        "rssi": r["rssi"],
        "rssi_desc": r["rssi_desc"],
    }


def rows_for_export(conn: sqlite3.Connection, days: int = DAYS_KEPT) -> list[dict]:
    cur = conn.execute(
        """SELECT * FROM snapshots
           WHERE fetched_at >= datetime('now', ?)
           ORDER BY water_body_id, fetched_at""",
        (f"-{days} days",),
    )
    return [dict(r) for r in cur.fetchall()]


def _controller_volume(conn=None) -> float | None:
    """The pool controller's volume, which is the figure we believe.

    Takes a connection rather than opening one: reaching into DB_PATH from what
    looks like a pure function meant `build_payload` scaled doses against the
    production controller volume no matter which database the rows came from.
    """
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT volume_gallons FROM system_snapshots ORDER BY read_at DESC LIMIT 1"
        ).fetchone()
        return row["volume_gallons"] if row else None
    except sqlite3.Error as e:
        # Narrow, and loud: silently returning None here drops the dose
        # correction with no trace, and that is the costliest thing to get wrong.
        print(f"could not read controller volume: {e}", file=sys.stderr)
        return None


def _latest_hand_test(newest: dict, salt: float | None) -> dict | None:
    """The most recent manual test, with its own saturation index.

    Worth its own index rather than borrowing the sensor's: when the two
    disagree, seeing what each implies about the water is the point. And while
    the pod is failing to measure, this is the only chemistry there is.
    """
    tests = [
        (when, event) for when, event in trust.load_events().items()
        if event.get("type") == "hand_test" and event.get("values")
    ]
    if not tests:
        return None

    when, event = max(tests, key=lambda pair: pair[0])
    values = event["values"]
    return {
        "at": when,
        "kit": event.get("kit"),
        "note": event.get("note"),
        "values": values,
        # Sensor readings the same test can be compared against.
        "sensor": {f: newest.get(f) for f in ("free_cl", "ph", "ta", "ch", "cya")},
        "lsi": lsi.calculate(
            values.get("ph"), newest.get("water_temp"), values.get("ch"),
            values.get("ta"), values.get("cya"), values.get("salt") or salt,
        ),
    }


def _controller_salt(conn=None) -> float | None:
    """Salt from the controller - the dominant dissolved solid, which sets K."""
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT salt_ppm FROM system_snapshots WHERE salt_ppm IS NOT NULL"
            " ORDER BY read_at DESC LIMIT 1"
        ).fetchone()
        return row["salt_ppm"] if row else None
    except sqlite3.Error:
        return None


def build_payload(rows: list[dict], now: datetime = None, conn=None) -> dict:
    now = now or datetime.now(timezone.utc)

    by_wb: dict[str, list[dict]] = {}
    for r in rows:
        by_wb.setdefault(r["water_body_id"], []).append(r)

    waterbodies = {}
    for wb_id, wb_rows in by_wb.items():
        newest = wb_rows[-1]
        # Trust is judged on the raw snapshots: the one *preceding* a
        # measurement carries the evidence about the conditions it was taken in.
        verdicts = trust.evaluate(wb_rows)
        actual_gallons = _controller_volume(conn)
        waterbodies[wb_id] = {
            "name": newest["name"],
            # The device's own bands, so the dashboard can say what "in range"
            # means rather than showing a bare midpoint.
            "ranges": json.loads(newest["ranges_json"]) if newest.get("ranges_json") else {},
            "targets": {
                "free_cl": newest["free_cl_target"],
                "ph": newest["ph_target"],
                "skimmer_flow": newest["skimmer_flow_target"],
                "ta": newest["ta_target"],
                "ch": newest["ch_target"],
                "cya": newest["cya_target"],
                "th": newest["th_target"],
            },
            "series": [
                _chemistry_point(r, verdicts, actual_gallons) for r in dedupe_by_measurement(wb_rows)
            ],
            "temp_series": [
                {"t": r["fetched_at"], "water_temp": r["water_temp"]}
                for r in wb_rows
                if r["water_temp"] is not None
            ],
            "latest": _latest_block(newest),
            "freshness": freshness_for(wb_rows, now),
            # Ties the five panel numbers together into the question none of
            # them answers alone: is this water dissolving the pool?
            "hand_test": _latest_hand_test(newest, _controller_salt(conn)),
            "lsi": lsi.calculate(
                newest.get("ph"), newest.get("water_temp"), newest.get("ch"),
                newest.get("ta"), newest.get("cya"), _controller_salt(conn),
            ),
        }

    return {
        "generated_at": now.isoformat(),
        "waterbodies": waterbodies,
    }


def export(out_path: Path = None):
    conn = connect()
    try:
        rows = rows_for_export(conn)
        payload = build_payload(rows, conn=conn)
    finally:
        conn.close()
    path = out_path or OUT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    return payload


if __name__ == "__main__":
    export()
    print(f"Wrote {OUT_FILE}")
