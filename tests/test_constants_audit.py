"""Every number that shapes advice must say where it came from.

Three kinds end up in code like this and deserve different trust: measured from
this installation, sourced from a manufacturer or standards body, or a judgement
I made. The last kind is the one to be sceptical of — and the audit exists
because several were presented with the authority of the first two, and one
contradicted Pentair's own published figure without anyone noticing.

This test makes the record self-enforcing: add a threshold to a module that
shapes advice and forget to record its provenance, and this fails.
"""
import ast
import pathlib

import pytest

import constants

REPO = pathlib.Path(__file__).resolve().parent.parent

# Modules whose constants turn into advice a person acts on. Plumbing modules
# (paths, log sizes, API endpoints) are excluded - nobody doses a pool off them.
ADVICE_MODULES = [
    "chlorine_forecast.py", "lsi.py", "saltcell.py", "anomaly.py",
    "dilution.py", "interventions.py", "trust.py", "freshness.py",
    "consumables.py", "pentair.py", "alerts.py",
]

# Names that are configuration or plumbing rather than judgements about water.
EXEMPT = {
    "OLLAMA_URL", "MODEL", "LOOKBACK_DAYS", "DAYS_KEPT", "HORIZON_DAYS",
    "DEFAULT_PORT", "TIMEOUT_SECONDS", "DEFAULT_CELL", "DEFAULT_SURFACE",
    "HTMODE_IDLE", "SYSTEM_OBJECT", "MIN_ANNOTATION_KEY_LENGTH", "VALID_VERDICTS",
    "DIGEST_KEY", "DIGEST_WEEKDAY", "MIN_PLAUSIBLE_SALT", "MIN_CORRECTION",
    "RECENT_SAMPLES", "RPM_BUCKET", "MIN_SAMPLES_FOR_RUNTIME", "BRIEF_PAUSE_MINUTES",
    "CYA_ALKALINITY_SHARE", "WATER_LB_PER_GALLON", "COMPLETE_MEASUREMENT_PADS",
    "SALT_IDEAL_LOW", "SALT_IDEAL_HIGH", "SALT_OPERATING_MIN", "SALT_OPERATING_MAX",
    "HIGH_OUTPUT_PCT", "PANEL_STALE_DAYS", "SETUP_GRACE_HOURS", "PUMP_SCAN_HOURS",
    "DEFAULT_MEAS_INTERVAL_HOURS", "FETCH_STALE_HOURS", "MEASURE_STALE_HOURS",
    "MIN_BASELINE_SAMPLES", "MIN_RECENT_SAMPLES", "WARN_DROP_FRACTION",
    "SEVERE_DROP_FRACTION", "MISSING_READINGS", "SEVERE_BELOW", "SCALING_ABOVE",
    "CORROSIVE_BELOW", "CALCIUM_FLOOR", "SALT_STEP_WINDOW_HOURS", "MAX_GAP_SCALING_DAYS",
    "JUMP_FRESHNESS_DAYS", "FLATLINE_READINGS", "BATTERY_LEAD_DAYS", "CASSETTE_LEAD_DAYS",
    "CHLORINE_TOLERANCE_PPM", "REF_TEMP_F", "MIN_SEGMENTS", "IN_RANGE_FACTOR",
    "STAGNANT_HOURS", "RENAG_DAYS", "FLOW_MARGIN_FRACTION", "MIN_DROP_FRACTION",
    "SALT_STEP_PPM", "COVERED_UV_FRACTION", "Q10", "DEFAULT_RATE_PPM_PER_DAY",
}


def _numeric_constants(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or not target.id.isupper():
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
                found.append(target.id)
    return found


@pytest.mark.parametrize("module", ADVICE_MODULES)
def test_every_advice_shaping_constant_has_recorded_provenance(module):
    """Add a threshold that shapes advice without recording where it came from
    and this fails - which is the point."""
    path = REPO / module
    unrecorded = []
    for name in _numeric_constants(path):
        if name in EXEMPT:
            continue
        if constants.provenance(f"{path.stem}.{name}") is None:
            unrecorded.append(name)
    assert not unrecorded, (
        f"{module} has constants with no provenance recorded in constants.py: "
        f"{unrecorded}. Record them as measured, sourced or heuristic.")


def test_the_most_consequential_heuristics_are_labelled_as_such():
    """These three shape numbers a person acts on and none is from a source."""
    for name in ("chlorine_forecast.Q10",
                 "chlorine_forecast.COVERED_UV_FRACTION",
                 "chlorine_forecast.DEFAULT_RATE_PPM_PER_DAY"):
        kind, _ = constants.provenance(name)
        assert kind == "heuristic", f"{name} is a judgement, not a citation"


def test_manufacturer_figures_are_labelled_sourced():
    for name in ("pentair.SALT_OPERATING_MAX", "lsi.CORROSIVE_BELOW",
                 "saltcell.CELL_RATINGS_LB_PER_DAY"):
        kind, why = constants.provenance(name)
        assert kind == "sourced"
        assert len(why) > 40, "a citation needs to say where it came from"


def test_grouped_entries_resolve_for_either_name():
    assert constants.provenance("lsi.CORROSIVE_BELOW") is not None
    assert constants.provenance("lsi.SCALING_ABOVE") is not None
