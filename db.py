"""SQLite storage for parsed WaterGuru snapshots."""
import json
import sqlite3
from datetime import datetime, timezone
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

-- Records one-shot notifications (reorder reminders, weekly digests) so they
-- fire once rather than on every run. Keyed by a caller-built string, e.g.
-- "reorder:LAB:<water_body_id>:<expiry_date>".
CREATE TABLE IF NOT EXISTS sent_notifications (
    key TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

-- Pool equipment state from the Pentair IntelliCenter. Separate table because
-- it's a different device on a different clock: read every fetch, and it
-- reports what the system is *doing* rather than what the water contains.
CREATE TABLE IF NOT EXISTS system_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    read_at TEXT NOT NULL,
    water_temp REAL,
    setpoint REAL,
    volume_gallons REAL,
    pump_running INTEGER,
    heater_enabled INTEGER,
    heater_calling INTEGER,
    chlorinator_output_pct REAL,
    salt_ppm REAL,
    pump_rpm REAL,
    pump_gpm REAL,
    pump_watts REAL,
    circuits_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_system_time ON system_snapshots(read_at);

-- Equipment settings changing, detected by comparing consecutive controller
-- reads. This is the pool notebook writing itself: the controller knows exactly
-- what changed and when, so the only part a human needs to supply is why.
CREATE TABLE IF NOT EXISTS system_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    description TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_time ON system_changes(detected_at);
"""

# Columns added after the original schema shipped. Applied by _migrate() on
# every connect, so an existing database picks them up without a rebuild.
NEW_COLUMNS = {
    "cassette_status": "TEXT",
    "cassette_urgent": "INTEGER",
    "battery_status": "TEXT",
    # The rest of the chemistry panel. The device reports these on a ~30-day
    # cycle (cfg.validDays 30) rather than the ~daily cycle of chlorine and pH,
    # so they get their own measurement timestamp - see PANEL_FIELDS.
    "ta": "REAL",
    "ta_target": "REAL",
    "ch": "REAL",
    "ch_target": "REAL",
    "cya": "REAL",
    "cya_target": "REAL",
    "th": "REAL",
    "th_target": "REAL",
    "panel_measure_time": "TEXT",
    # Setup/scheduling state, used to tell "new install still calibrating" apart
    # from "sensor has died" and to say when the next reading is due.
    "pod_setup_time": "TEXT",
    "pump_scan_state": "TEXT",
    "meas_hour": "INTEGER",
    "meas_minute": "INTEGER",
    "meas_auto_hours": "INTEGER",
    # The device ships the band it actually judges chlorine against (green
    # 1.6-5.4 ppm around a 3.0 target). Storing it means "in range" can mean
    # what WaterGuru means, rather than a tolerance invented at this end.
    "free_cl_green_min": "REAL",
    "free_cl_green_max": "REAL",
    # The full RED/YELLOW/GREEN bounds for every measurement type, as the device
    # ships them. Storing only the midpoint threw away what "in range" means -
    # a target of 400 says nothing about whether 300 is fine or alarming.
    "ranges_json": "TEXT",
    "skimmer_flow_green_min": "REAL",
    # WaterGuru's configured pool volume. Stored because every dose it
    # recommends is computed from it, so a wrong figure skews them all.
    "size_gallons": "REAL",
}

# The slow half of the panel: measured roughly monthly, valid for 30 days.
PANEL_FIELDS = ("ta", "ch", "cya", "th")

# The fast half: measured roughly daily, valid for 2 days.
DAILY_FIELDS = ("free_cl", "ph", "skimmer_flow")


# Added to system_snapshots after it shipped.
NEW_SYSTEM_COLUMNS = {"pump_rpm": "REAL", "pump_gpm": "REAL", "pump_watts": "REAL"}


def _migrate(conn: sqlite3.Connection):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
    for col, col_type in NEW_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {col_type}")

    existing = {row[1] for row in conn.execute("PRAGMA table_info(system_snapshots)")}
    for col, col_type in NEW_SYSTEM_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE system_snapshots ADD COLUMN {col} {col_type}")


def connect(db_path: Path = None) -> sqlite3.Connection:
    """Opens the database with the schema applied and Row access enabled."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def was_sent(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM sent_notifications WHERE key = ?", (key,)).fetchone() is not None


def last_sent_at(conn: sqlite3.Connection, key: str):
    """When this notification last fired, or None. Drives elapsed-time re-nags."""
    row = conn.execute("SELECT sent_at FROM sent_notifications WHERE key = ?", (key,)).fetchone()
    if not row or not row["sent_at"]:
        return None
    try:
        parsed = datetime.fromisoformat(str(row["sent_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def mark_sent(conn: sqlite3.Connection, key: str, sent_at: str):
    conn.execute(
        "INSERT OR REPLACE INTO sent_notifications (key, sent_at) VALUES (?, ?)",
        (key, sent_at),
    )
    conn.commit()


def dedupe_by_measurement(rows: list[dict]) -> list[dict]:
    """Collapses repeat fetches of the same pod measurement into one row.

    The cassette only produces a new chemistry measurement every day or two, but
    we fetch twice a day - so several snapshots share one `latest_measure_time`
    and identical free_cl/ph/skimmer_flow. Charting or summarizing those as
    separate readings invents data points that never happened.

    Rows are assumed to be ordered oldest-first. Within one measurement the
    freshest value of each field wins, but fields are merged rather than the
    whole row being replaced: an individual sensor can drop out mid-measurement
    (the flow sensor going quiet nulls `skimmer_flow` while everything else keeps
    reporting), and taking the last row wholesale would throw away a reading the
    earlier fetches did have.

    Rows with no measurement time fall back to being kept as-is, keyed on
    fetched_at.
    """
    by_measurement = {}
    for row in rows:
        key = row.get("latest_measure_time") or row.get("fetched_at")
        if key not in by_measurement:
            by_measurement[key] = dict(row)
            continue
        merged = by_measurement[key]
        for field, value in row.items():
            if value is not None:
                merged[field] = value
    return list(by_measurement.values())


def _measure(measurements, mtype):
    for m in measurements or []:
        if m.get("type") == mtype:
            return m.get("floatValue", m.get("intValue")), m.get("target")
    return None, None


def _measure_time(measurements, mtype):
    for m in measurements or []:
        if m.get("type") == mtype:
            return m.get("measureTime")
    return None


def _all_ranges(measurements) -> dict:
    """Every measurement type's bands, keyed by type.

    Values are floats; the device sends some as strings and some as numbers
    depending on whether it populated `floatRanges` or only `ranges`.
    """
    out = {}
    for m in measurements or []:
        cfg = m.get("cfg") or {}
        raw = cfg.get("floatRanges") or cfg.get("ranges") or {}
        if not raw:
            continue
        bounds = {k: _as_float(v) for k, v in raw.items()}
        bounds = {k: v for k, v in bounds.items() if v is not None}
        if bounds:
            bounds["unit"] = cfg.get("unit")
            bounds["title"] = m.get("title")
            out[m["type"]] = bounds
    return out


def _green_range(measurements, mtype):
    """The device's own acceptable band for a measurement type.

    Falls back to the string `ranges` map, because not every type ships
    `floatRanges` - SKIMMER_FLOW carries only the string form (green 5-79 gpm).
    """
    for m in measurements or []:
        if m.get("type") != mtype:
            continue
        cfg = m.get("cfg") or {}
        ranges = cfg.get("floatRanges") or cfg.get("ranges") or {}
        low, high = ranges.get("GREEN_MIN"), ranges.get("GREEN_MAX")
        return _as_float(low), _as_float(high)
    return None, None


def _as_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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

    ta, ta_target = _measure(measurements, "TA")
    ch, ch_target = _measure(measurements, "CH")
    cya, cya_target = _measure(measurements, "CYA")
    th, th_target = _measure(measurements, "TH")
    free_cl_green_min, free_cl_green_max = _green_range(measurements, "FREE_CL")

    rssi_info = pods[0].get("rssiInfo", {}) if pods else {}
    pod = pods[0].get("pod", {}) if pods else {}
    meas_times = pod.get("measDoseTimes") or [{}]

    alerts = [
        {
            "source": a.get("source"),
            "condition": a.get("condition"),
            "status": a.get("status"),
            "text": a.get("text"),
            # WaterGuru works out the actual dose for the pool's volume
            # ("Add 73 cups of 90% concentration calcium chloride"). That's the
            # only genuinely actionable part of an alert, and it was being
            # thrown away with the rest of the advice block.
            "advice": (a.get("advice") or {}).get("action", {}).get("summary"),
        }
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
        "ta": ta,
        "ta_target": ta_target,
        "ch": ch,
        "ch_target": ch_target,
        "cya": cya,
        "cya_target": cya_target,
        "th": th,
        "th_target": th_target,
        "panel_measure_time": _measure_time(measurements, "CYA"),
        "ranges_json": json.dumps(_all_ranges(measurements)),
        "skimmer_flow_green_min": _green_range(measurements, "SKIMMER_FLOW")[0],
        "free_cl_green_min": free_cl_green_min,
        "free_cl_green_max": free_cl_green_max,
        "size_gallons": (wb.get("waterBody") or {}).get("sizeGallons"),
        "pod_setup_time": pod.get("setUpTime"),
        "pump_scan_state": pod.get("pumpScanState"),
        "meas_hour": meas_times[0].get("hour"),
        "meas_minute": meas_times[0].get("minute"),
        "meas_auto_hours": pod.get("measAutoHrs"),
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


def store_snapshot(fetched_at: str, data: dict, db_path: Path = None):
    conn = connect(db_path)
    try:
        rows = [parse_waterbody(fetched_at, wb) for wb in data.get("waterBodies", [])]
        for row in rows:
            prev = conn.execute(
                """SELECT status, cassette_status, battery_status, latest_measure_time,
                          cassette_pct_left, battery_pct_left, cassette_urgent
                   FROM snapshots WHERE water_body_id = ?
                   ORDER BY fetched_at DESC LIMIT 1""",
                (row["water_body_id"],),
            ).fetchone()
            row["prev_status"] = prev["status"] if prev else None
            row["prev_cassette_status"] = prev["cassette_status"] if prev else None
            row["prev_battery_status"] = prev["battery_status"] if prev else None
            row["prev_measure_time"] = prev["latest_measure_time"] if prev else None
            row["prev_cassette_pct_left"] = prev["cassette_pct_left"] if prev else None
            row["prev_cassette_urgent"] = prev["cassette_urgent"] if prev else None
            row["prev_battery_pct_left"] = prev["battery_pct_left"] if prev else None
            conn.execute(
                """INSERT INTO snapshots (
                    fetched_at, water_body_id, name, status, water_temp, latest_measure_time,
                    free_cl, free_cl_target, ph, ph_target, skimmer_flow, skimmer_flow_target,
                    ta, ta_target, ch, ch_target, cya, cya_target, th, th_target,
                    panel_measure_time, free_cl_green_min, free_cl_green_max, size_gallons, ranges_json, skimmer_flow_green_min,
                    cassette_pct_left, cassette_days_left, cassette_status, cassette_urgent,
                    battery_pct_left, battery_time_left, battery_status,
                    pod_setup_time, pump_scan_state, meas_hour, meas_minute, meas_auto_hours,
                    rssi, rssi_desc, alerts_json
                ) VALUES (
                    :fetched_at, :water_body_id, :name, :status, :water_temp, :latest_measure_time,
                    :free_cl, :free_cl_target, :ph, :ph_target, :skimmer_flow, :skimmer_flow_target,
                    :ta, :ta_target, :ch, :ch_target, :cya, :cya_target, :th, :th_target,
                    :panel_measure_time, :free_cl_green_min, :free_cl_green_max, :size_gallons, :ranges_json, :skimmer_flow_green_min,
                    :cassette_pct_left, :cassette_days_left, :cassette_status, :cassette_urgent,
                    :battery_pct_left, :battery_time_left, :battery_status,
                    :pod_setup_time, :pump_scan_state, :meas_hour, :meas_minute, :meas_auto_hours,
                    :rssi, :rssi_desc, :alerts_json
                )""",
                row,
            )
        conn.commit()
        return rows
    finally:
        conn.close()
