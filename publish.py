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
from datetime import datetime, timezone
from pathlib import Path

import trust
from db import connect, dedupe_by_measurement
from freshness import freshness_for

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "site" / "data" / "history.json"
DAYS_KEPT = 180


def _chemistry_point(r: dict, verdicts: dict = None) -> dict:
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
        "alerts": json.loads(r["alerts_json"]) if r["alerts_json"] else [],
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


def build_payload(rows: list[dict], now: datetime = None) -> dict:
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
        waterbodies[wb_id] = {
            "name": newest["name"],
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
                _chemistry_point(r, verdicts) for r in dedupe_by_measurement(wb_rows)
            ],
            "temp_series": [
                {"t": r["fetched_at"], "water_temp": r["water_temp"]}
                for r in wb_rows
                if r["water_temp"] is not None
            ],
            "latest": _latest_block(newest),
            "freshness": freshness_for(wb_rows, now),
        }

    return {
        "generated_at": now.isoformat(),
        "waterbodies": waterbodies,
    }


def export(out_path: Path = None):
    conn = connect()
    try:
        rows = rows_for_export(conn)
    finally:
        conn.close()

    payload = build_payload(rows)
    path = out_path or OUT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    return payload


if __name__ == "__main__":
    export()
    print(f"Wrote {OUT_FILE}")
