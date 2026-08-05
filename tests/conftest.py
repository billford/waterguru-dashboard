import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# The modules under test live at the repo root and import each other by bare
# name (`from db import ...`), matching how the scripts are actually run.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def now():
    return datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A throwaway database, so tests never touch data/waterguru.db."""
    import db as db_module

    path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    conn = db_module.connect(path)
    yield conn
    conn.close()


def snapshot(**overrides) -> dict:
    """A stored row shaped like store_snapshot() produces, including prev_* fields."""
    row = {
        "water_body_id": "wb-1",
        "name": "Pool",
        "fetched_at": "2026-08-05T12:00:00+00:00",
        "latest_measure_time": "2026-08-05T11:00:00.000Z",
        "status": "GREEN",
        "prev_status": "GREEN",
        "water_temp": 82.0,
        "free_cl": 3.0,
        "free_cl_target": 3.0,
        "free_cl_green_min": 1.6,
        "free_cl_green_max": 5.4,
        "ph": 7.5,
        "ph_target": 7.6,
        "skimmer_flow": 13.0,
        "skimmer_flow_target": 15.0,
        # The slow panel, on its own ~monthly measurement clock.
        "ta": 134.0,
        "ta_target": 80.0,
        "ch": 153.0,
        "ch_target": 400.0,
        "cya": 45.0,
        "cya_target": 65.0,
        "th": 153.0,
        "th_target": 400.0,
        "panel_measure_time": "2026-08-05T11:00:05.000Z",
        # A settled pod, well past setup - tests that care about the pump-scan
        # grace period override these explicitly.
        "pod_setup_time": "2026-07-01T12:00:00.000Z",
        "pump_scan_state": "DONE_COMPUTED_WB_MEAS_TIME_SET",
        "meas_hour": 20,
        "meas_minute": 56,
        "cassette_pct_left": 80.0,
        "cassette_days_left": "40 days left",
        "cassette_status": "GREEN",
        "cassette_urgent": 0,
        "prev_cassette_status": "GREEN",
        "prev_cassette_pct_left": 82.0,
        "battery_pct_left": 72.0,
        "battery_time_left": "4 months left",
        "battery_status": "GREEN",
        "prev_battery_status": "GREEN",
        "prev_battery_pct_left": 73.0,
        "rssi": -60,
        "rssi_desc": "Good",
        "alerts_json": "[]",
    }
    row.update(overrides)
    return row
