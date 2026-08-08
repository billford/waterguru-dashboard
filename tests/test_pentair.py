"""The Pentair IntelliCenter client.

Protocol shape is pinned down by tests because the API is undocumented and the
response envelope differs by command - GetParamList answers under `objectList`
while GetQuery answers under `answer`, and reading the wrong one makes a
perfectly good 200 look like an empty result.
"""
import json

import pytest

import pentair

CONFIG_ANSWER = [
    {"objnam": "B1101", "params": {
        "OBJTYP": "BODY", "SUBTYP": "POOL", "SNAME": "Pool",
        "OBJLIST": [
            {"objnam": "H0001", "params": {"OBJTYP": "HEATER", "SNAME": "Gas Heater"}},
            {"objnam": "CHR01", "params": {"OBJTYP": "CHEM", "SUBTYP": "ICHLOR", "SNAME": "IntelliChlor 1"}},
            {"objnam": "000FF", "params": {"SNAME": "No Heater Selected."}},
        ],
    }},
    {"objnam": "C0006", "params": {"OBJTYP": "CIRCUIT", "SUBTYP": "POOL", "SNAME": "Pool"}},
    {"objnam": "C0002", "params": {"OBJTYP": "CIRCUIT", "SUBTYP": "INTELLI", "SNAME": "Pool Lights"}},
]

LIVE_PARAMS = {
    "B1101": {"SNAME": "Pool", "STATUS": "ON", "TEMP": "84", "LSTTMP": "83",
              "LOTMP": "81", "HITMP": "100", "HTMODE": "0", "VOL": "15000"},
    "CHR01": {"SNAME": "IntelliChlor 1", "PRIM": "60", "SEC": "20", "SALT": "4350"},
    "H0001": {"SNAME": "Gas Heater", "STATUS": "ON", "SUBTYP": "GENERIC"},
    "C0006": {"SNAME": "Pool", "STATUS": "ON", "SUBTYP": "POOL"},
    "C0002": {"SNAME": "Pool Lights", "STATUS": "OFF", "SUBTYP": "INTELLI"},
}

OBJECTS = {"body": "B1101", "heater": "H0001", "chem": "CHR01", "circuits": ["C0006", "C0002"]}


class FakeSocket:
    """Stands in for the controller, answering with the shapes it really uses."""

    def __init__(self):
        self.sent = []

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        request = self.sent[-1]
        mid = request["messageID"]
        if request["command"] == "GetQuery":
            return json.dumps({"command": "SendQuery", "messageID": mid,
                               "response": "200", "answer": CONFIG_ANSWER})
        return json.dumps({
            "command": "SendParamList", "messageID": mid, "response": "200",
            "objectList": [
                {"objnam": r["objnam"], "params": LIVE_PARAMS.get(r["objnam"], {})}
                for r in request["objectList"]
            ],
        })

    def close(self):
        pass


@pytest.fixture
def fake_controller(monkeypatch):
    monkeypatch.setattr(pentair.websocket, "create_connection", lambda url, timeout: FakeSocket())
    monkeypatch.setenv("PENTAIR_HOST", "192.168.1.241")


# ---- connection details ----

def test_the_default_port_is_filled_in(monkeypatch):
    monkeypatch.setenv("PENTAIR_HOST", "192.168.1.241")
    assert pentair._endpoint() == "ws://192.168.1.241:6680"


def test_an_explicit_port_is_respected():
    assert pentair._endpoint("10.0.0.5:9999") == "ws://10.0.0.5:9999"


def test_a_missing_host_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("PENTAIR_HOST", raising=False)
    with pytest.raises(pentair.IntelliCenterError, match="PENTAIR_HOST"):
        pentair._endpoint()


def test_a_non_200_response_raises(monkeypatch, fake_controller):
    class Failing(FakeSocket):
        def recv(self):
            return json.dumps({"response": "400", "messageID": self.sent[-1]["messageID"]})

    monkeypatch.setattr(pentair.websocket, "create_connection", lambda url, timeout: Failing())
    with pytest.raises(pentair.IntelliCenterError):
        pentair.read_state()


# ---- discovery ----

def test_heater_and_chlorinator_are_found_nested_under_the_body(fake_controller):
    with pentair.Client() as client:
        found = pentair.discover_objects(client)
    assert found["body"] == "B1101"
    assert found["heater"] == "H0001"
    assert found["chem"] == "CHR01"
    assert set(found["circuits"]) == {"C0006", "C0002"}


def test_entries_without_an_object_type_are_skipped(fake_controller):
    """The body's child list includes a "No Heater Selected." placeholder."""
    with pentair.Client() as client:
        found = pentair.discover_objects(client)
    assert "000FF" not in found["circuits"]


# ---- normalization ----

def test_live_state_is_normalized_into_readable_fields(fake_controller):
    state = pentair.read_state()
    assert state["water_temp"] == 84.0
    assert state["setpoint"] == 81.0
    assert state["volume_gallons"] == 15000.0
    assert state["chlorinator_output_pct"] == 60.0
    assert state["salt_ppm"] == 4350.0
    assert state["pump_running"] is True


def test_an_idle_heater_is_distinguished_from_a_firing_one():
    """HTMODE 0 means at setpoint, which is not the same as switched off."""
    idle = pentair.normalize({"B1101": {"STATUS": "ON", "HTMODE": "0"},
                              "H0001": {"STATUS": "ON"}}, OBJECTS)
    assert idle["heater_enabled"] is True
    assert idle["heater_calling"] is False

    firing = pentair.normalize({"B1101": {"STATUS": "ON", "HTMODE": "4"},
                                "H0001": {"STATUS": "ON"}}, OBJECTS)
    assert firing["heater_calling"] is True


def test_circuits_are_reported_by_name(fake_controller):
    state = pentair.read_state()
    assert state["circuits"] == {"Pool": True, "Pool Lights": False}


def test_unparseable_numbers_become_none():
    state = pentair.normalize({"B1101": {"TEMP": "", "VOL": "N/A"}}, OBJECTS)
    assert state["water_temp"] is None
    assert state["volume_gallons"] is None


# ---- the interpretation that makes the data useful ----

def test_high_salt_is_called_out_with_the_reason_it_matters():
    note = pentair.system_note({"salt_ppm": 4350})
    assert "4350 ppm" in note
    assert "corrodes" in note


def test_salt_in_range_says_nothing():
    assert pentair.system_note({"salt_ppm": 3200}) is None


def test_low_salt_is_called_out_too():
    assert "low" in pentair.system_note({"salt_ppm": 2500})


def test_a_hard_working_cell_with_no_stabilizer_is_explained():
    """The actionable insight: raising CYA beats raising output."""
    note = pentair.system_note({"salt_ppm": 3200, "chlorinator_output_pct": 60}, cya=10)
    assert "60%" in note and "10 ppm" in note
    assert "raising CYA does more" in note


def test_a_hard_working_cell_with_adequate_stabilizer_is_not_flagged():
    assert pentair.system_note({"salt_ppm": 3200, "chlorinator_output_pct": 60}, cya=50) is None


def test_pump_runtime_is_a_fraction_of_recent_readings():
    rows = [{"pump_running": 1}, {"pump_running": 1}, {"pump_running": 0}, {"pump_running": 0}]
    assert pentair.pump_runtime_fraction(rows) == 0.5
    assert pentair.pump_runtime_fraction([]) is None


# ---- cross-device checks ----

def test_a_volume_disagreement_is_flagged_with_its_dosing_consequence():
    """WaterGuru sizes doses from its own volume, so a wrong figure skews them all."""
    note = pentair.volume_mismatch(15000, 20000)
    assert "15,000" in note and "20,000" in note
    assert "33% too high" in note


def test_a_volume_disagreement_the_other_way_reads_correctly():
    assert "too low" in pentair.volume_mismatch(20000, 15000)


def test_close_enough_volumes_are_not_flagged():
    assert pentair.volume_mismatch(15000, 15500) is None


def test_a_missing_volume_is_not_a_mismatch():
    assert pentair.volume_mismatch(None, 20000) is None
    assert pentair.volume_mismatch(15000, None) is None


# ---- the notebook that writes itself ----

BEFORE = {"chlorinator_output_pct": 60.0, "setpoint": 81.0, "heater_enabled": True,
          "volume_gallons": 15000.0, "salt_ppm": 4350.0}


def test_a_setting_change_is_detected_and_described_in_plain_words():
    changes = pentair.detect_changes(BEFORE, {**BEFORE, "chlorinator_output_pct": 50.0})
    assert len(changes) == 1
    assert changes[0]["description"] == "Salt cell output changed from 60% to 50%"


def test_the_heater_setpoint_carries_its_unit():
    changes = pentair.detect_changes(BEFORE, {**BEFORE, "setpoint": 84.0})
    assert "81°F to 84°F" in changes[0]["description"]


def test_switching_the_heater_reads_as_on_and_off():
    changes = pentair.detect_changes(BEFORE, {**BEFORE, "heater_enabled": False})
    assert "Heater changed from on to off" in changes[0]["description"]


def test_nothing_changing_logs_nothing():
    assert pentair.detect_changes(BEFORE, dict(BEFORE)) == []


def test_the_first_ever_read_has_nothing_to_compare_against():
    assert pentair.detect_changes(None, BEFORE) == []


def test_scheduled_pump_and_circuit_activity_is_not_logged_as_a_change():
    """The pump cycles on its schedule constantly; logging it would bury the
    handful of entries that represent an actual decision."""
    noisy = {**BEFORE, "pump_running": False, "circuits": {"Pool": False}}
    assert pentair.detect_changes({**BEFORE, "pump_running": True}, noisy) == []


def test_salt_drifting_slowly_is_ignored_but_a_step_is_recorded():
    assert pentair.detect_changes(BEFORE, {**BEFORE, "salt_ppm": 4300.0}) == []
    stepped = pentair.detect_changes(BEFORE, {**BEFORE, "salt_ppm": 4000.0})
    assert "Salt fell from 4350 to 4000 ppm" in stepped[0]["description"]


def test_several_changes_at_once_are_all_recorded():
    changes = pentair.detect_changes(
        BEFORE, {**BEFORE, "chlorinator_output_pct": 40.0, "setpoint": 86.0})
    assert {c["field"] for c in changes} == {"chlorinator_output_pct", "setpoint"}


def test_a_missing_value_is_not_reported_as_a_change():
    assert pentair.detect_changes(BEFORE, {**BEFORE, "setpoint": None}) == []


# ---- a pump that is off for four minutes is not a pump that has stopped ----

def _hist(*minutes_ago_running):
    """History newest-first: (minutes before 08:00, running)."""
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    return [{"read_at": (base - timedelta(minutes=m)).isoformat(), "pump_running": r}
            for m, r in minutes_ago_running]


def test_a_running_pump_needs_no_explanation():
    ctx = pentair.pump_context({"pump_running": True, "read_at": "2026-08-08T12:00:00+00:00"}, [])
    assert ctx["running"] is True and ctx["note"] is None


def test_a_pump_off_for_minutes_is_a_scheduled_pause():
    """The twice-daily fetch lands inside this pump's cooldown, so the published
    snapshot claimed the pump was off every morning until the evening run."""
    state = {"pump_running": False, "read_at": "2026-08-08T12:00:00+00:00"}
    ctx = pentair.pump_context(state, _hist((14, 0), (24, 1), (34, 1)))
    assert ctx["brief_pause"] is True
    assert "the schedule rather than a fault" in ctx["note"]


def test_a_pump_off_for_hours_is_reported_as_a_problem():
    state = {"pump_running": False, "read_at": "2026-08-08T12:00:00+00:00"}
    ctx = pentair.pump_context(state, _hist((60, 0), (240, 0), (300, 1)))
    assert ctx["brief_pause"] is False
    assert "Nothing circulates" in ctx["note"]


def test_a_pump_with_no_record_of_running_is_not_called_a_pause():
    state = {"pump_running": False, "read_at": "2026-08-08T12:00:00+00:00"}
    ctx = pentair.pump_context(state, _hist((10, 0), (20, 0)))
    assert ctx["brief_pause"] is False
    assert "no recent record" in ctx["note"]
