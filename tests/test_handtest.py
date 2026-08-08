"""Manual test-kit results recorded alongside the sensor's own.

A drop-count titration outranks the pod when the two disagree, and while the pod
is failing to measure it's the only chemistry available at all.
"""
import json

import pytest
from conftest import snapshot

import publish
import trust


@pytest.fixture
def events(tmp_path, monkeypatch):
    path = tmp_path / "annotations.json"
    monkeypatch.setattr(trust, "ANNOTATIONS_PATH", path)
    return path


def _record(values, when="2026-08-08T18:00:00+00:00", kit="Taylor K-2006"):
    events = trust.load_events()
    events[when] = {"type": "hand_test", "values": values, "kit": kit, "note": None}
    trust.save_events(events)


def test_no_hand_test_yields_nothing(events):
    assert publish._latest_hand_test(snapshot(), 3400) is None


def test_a_recorded_test_is_returned_with_the_sensor_values_beside_it(events):
    _record({"free_cl": 4.0, "ph": 7.5})
    result = publish._latest_hand_test(snapshot(free_cl=7.3, ph=7.2), 3400)

    assert result["values"] == {"free_cl": 4.0, "ph": 7.5}
    assert result["sensor"]["free_cl"] == 7.3
    assert result["sensor"]["ph"] == 7.2
    assert result["kit"] == "Taylor K-2006"


def test_the_most_recent_test_wins(events):
    _record({"ph": 7.0}, when="2026-08-01T12:00:00+00:00")
    _record({"ph": 7.6}, when="2026-08-08T12:00:00+00:00")
    assert publish._latest_hand_test(snapshot(), 3400)["values"]["ph"] == 7.6


def test_it_carries_its_own_saturation_index(events):
    """Not the sensor's - when the two disagree, what each implies is the point."""
    _record({"ph": 7.5, "ta": 100, "ch": 350, "cya": 40})
    result = publish._latest_hand_test(snapshot(water_temp=84.0), 3200)

    assert result["lsi"] is not None
    assert result["lsi"]["verdict"] == "balanced"


def test_a_hand_test_can_disagree_with_the_sensor_about_the_verdict(events):
    """The sensor says corrosive on CH 152; a kit reading 350 would not."""
    _record({"ph": 7.5, "ta": 100, "ch": 350, "cya": 40})
    healthy = publish._latest_hand_test(snapshot(water_temp=84.0), 3200)

    trust.save_events({})
    _record({"ph": 7.2, "ta": 151, "ch": 152, "cya": 14})
    aggressive = publish._latest_hand_test(snapshot(water_temp=84.0), 4350)

    assert healthy["lsi"]["verdict"] == "balanced"
    assert aggressive["lsi"]["verdict"] == "corrosive"


def test_salt_from_the_kit_overrides_the_controller(events):
    _record({"ph": 7.5, "ta": 100, "ch": 350, "cya": 40, "salt": 3000})
    result = publish._latest_hand_test(snapshot(water_temp=84.0), 4350)
    assert result["lsi"]["inputs"]["salt_ppm"] == 3000


def test_a_partial_test_still_records(events):
    """You might only re-test the one value in dispute."""
    _record({"ph": 7.5})
    result = publish._latest_hand_test(snapshot(), 3400)
    assert result["values"] == {"ph": 7.5}
    assert result["lsi"] is None      # not enough for an index, and it says so


def test_other_event_types_are_not_mistaken_for_tests(events):
    trust.save_events({"2026-08-08T12:00:00+00:00": {"type": "water_added", "note": "topped up"}})
    assert publish._latest_hand_test(snapshot(), 3400) is None
