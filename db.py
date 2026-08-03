"""SQLite storage for parsed WaterGuru snapshots."""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "waterguru.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    water_body_id TEXT NOT NULL,
    name TEXT,
    status TEXT,
    water_temp REAL,
    latest_measure_time TEXT,
    free_cl REAL,
    free_cl_target REAL,
    ph REAL,
    ph_target REAL,
    skimmer_flow REAL,
    skimmer_flow_target REAL,
    cassette_pct_left REAL,
    cassette_days_left TEXT,
    cassette_status TEXT,
    cassette_urgent INTEGER,
    battery_pct_left REAL,
    battery_time_left TEXT,
    battery_status TEXT,
    rssi INTEGER,
    rssi_desc TEXT,
    alerts_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_wb_time ON snapshots(water_body_id, fetched_at);
"""

NEW_COLUMNS = {
    "cassette_status": "TEXT",
    "cassette_urgent": "INTEGER",
    "battery_status": "TEXT",
}


def _migrate(conn: sqlite3.Connection):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
    for col, col_type in NEW_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {col_type}")


def _measure(measurements, mtype):
    for m in measurements or []:
        if m.get("type") == mtype:
            return m.get("floatValue", m.get("intValue")), m.get("target")
    return None, None


def _refillable(refillables, rtype):
    for r in refillables or []:
        if r.get("type") == rtype:
            return r.get("pctLeft"), r.get("timeLeftText"), r.get("status"), bool(r.get("urgent"))
    return None, None, None, False


def parse_waterbody(fetched_at: str, wb: dict) -> dict:
    measurements = wb.get("measurements", [])
    free_cl, free_cl_target = _measure(measurements, "FREE_CL")
    ph, ph_target = _measure(measurements, "PH")
    flow, flow_target = _measure(measurements, "SKIMMER_FLOW")

    pods = wb.get("pods", [])
    refillables = pods[0]["refillables"] if pods and pods[0].get("refillables") else []
    cassette_pct, cassette_days, cassette_status, cassette_urgent = _refillable(refillables, "LAB")
    batt_pct, batt_time, batt_status, _ = _refillable(refillables, "BATT")

    rssi_info = pods[0].get("rssiInfo", {}) if pods else {}

    alerts = [
        {"source": a.get("source"), "condition": a.get("condition"), "status": a.get("status"), "text": a.get("text")}
        for a in wb.get("alerts", [])
    ]

    return {
        "fetched_at": fetched_at,
        "water_body_id": wb.get("waterBodyId"),
        "name": wb.get("name"),
        "status": wb.get("status"),
        "water_temp": wb.get("waterTemp"),
        "latest_measure_time": wb.get("latestMeasureTime"),
        "free_cl": free_cl,
        "free_cl_target": free_cl_target,
        "ph": ph,
        "ph_target": ph_target,
        "skimmer_flow": flow,
        "skimmer_flow_target": flow_target,
        "cassette_pct_left": cassette_pct,
        "cassette_days_left": cassette_days,
        "cassette_status": cassette_status,
        "cassette_urgent": int(cassette_urgent),
        "battery_pct_left": batt_pct,
        "battery_time_left": batt_time,
        "battery_status": batt_status,
        "rssi": rssi_info.get("rssi"),
        "rssi_desc": rssi_info.get("desc"),
        "alerts_json": json.dumps(alerts),
    }


def store_snapshot(fetched_at: str, data: dict):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        rows = [parse_waterbody(fetched_at, wb) for wb in data.get("waterBodies", [])]
        for row in rows:
            prev = conn.execute(
                """SELECT status, cassette_status FROM snapshots WHERE water_body_id = ?
                   ORDER BY fetched_at DESC LIMIT 1""",
                (row["water_body_id"],),
            ).fetchone()
            row["prev_status"] = prev[0] if prev else None
            row["prev_cassette_status"] = prev[1] if prev else None
            conn.execute(
                """INSERT INTO snapshots (
                    fetched_at, water_body_id, name, status, water_temp, latest_measure_time,
                    free_cl, free_cl_target, ph, ph_target, skimmer_flow, skimmer_flow_target,
                    cassette_pct_left, cassette_days_left, cassette_status, cassette_urgent,
                    battery_pct_left, battery_time_left, battery_status,
                    rssi, rssi_desc, alerts_json
                ) VALUES (
                    :fetched_at, :water_body_id, :name, :status, :water_temp, :latest_measure_time,
                    :free_cl, :free_cl_target, :ph, :ph_target, :skimmer_flow, :skimmer_flow_target,
                    :cassette_pct_left, :cassette_days_left, :cassette_status, :cassette_urgent,
                    :battery_pct_left, :battery_time_left, :battery_status,
                    :rssi, :rssi_desc, :alerts_json
                )""",
                row,
            )
        conn.commit()
        return rows
    finally:
        conn.close()
