"""Exports the SQLite history into site/data/history.json for the static dashboard."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from db import DB_PATH

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "site" / "data" / "history.json"
DAYS_KEPT = 180


def export():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT * FROM snapshots
           WHERE fetched_at >= datetime('now', ?)
           ORDER BY water_body_id, fetched_at""",
        (f"-{DAYS_KEPT} days",),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    waterbodies = {}
    for r in rows:
        wb = waterbodies.setdefault(
            r["water_body_id"],
            {
                "name": r["name"],
                "targets": {
                    "free_cl": r["free_cl_target"],
                    "ph": r["ph_target"],
                    "skimmer_flow": r["skimmer_flow_target"],
                },
                "series": [],
            },
        )
        wb["series"].append(
            {
                "t": r["fetched_at"],
                "status": r["status"],
                "water_temp": r["water_temp"],
                "free_cl": r["free_cl"],
                "ph": r["ph"],
                "skimmer_flow": r["skimmer_flow"],
                "cassette_pct_left": r["cassette_pct_left"],
                "cassette_days_left": r["cassette_days_left"],
                "battery_pct_left": r["battery_pct_left"],
                "battery_time_left": r["battery_time_left"],
                "rssi": r["rssi"],
                "rssi_desc": r["rssi_desc"],
                "alerts": json.loads(r["alerts_json"]) if r["alerts_json"] else [],
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "waterbodies": waterbodies,
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, indent=2))
    return payload


if __name__ == "__main__":
    export()
    print(f"Wrote {OUT_FILE}")
