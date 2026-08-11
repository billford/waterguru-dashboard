"""Where every number in this codebase came from.

Three kinds of number end up in code like this, and they deserve very different
amounts of trust:

**MEASURED** - read from this installation's own equipment or history. The pump
scan length, the pod's measurement interval, how many pads a completed
measurement consumes. These are facts about this pool and can be re-derived from
the data at any time.

**SOURCED** - published by the manufacturer or a recognised body, with a
reference. Pentair's salt range, the Langelier balanced band, the calcium floor
below which water turns aggressive. These can be checked against the citation.

**HEURISTIC** - a judgement I made. Sometimes a reasonable one, sometimes just a
round number that seemed about right. These are the ones to be sceptical of, and
the reason this file exists: several were presented as though they carried the
authority of the first two categories, and at least one contradicted the
manufacturer's own published figure without anyone noticing.

The thresholds themselves live in the modules that use them. This is the
provenance record, and the place to look before trusting a number.
"""

# --- MEASURED from this installation -------------------------------------

MEASURED = {
    "freshness.PUMP_SCAN_HOURS": (
        "36h — pumpScanNumCycles (72) x pumpScanIntervalMins (30) read from the "
        "controller, and corroborated: measDoseHrMinModified landed 35.5h after "
        "pumpScanPendingTime."),
    "freshness.DEFAULT_MEAS_INTERVAL_HOURS": (
        "24h — the pod's own measAutoHrs field."),
    "pentair.COMPLETE_MEASUREMENT_PADS": (
        "10 pads — observed directly: the cassette went 192 -> 182 across the one "
        "measurement that completed."),
    "saltcell.WATER_LB_PER_GALLON": (
        "8.34 lb/gal — physical constant, the basis of every ppm conversion here."),
}

# --- SOURCED, with a citation --------------------------------------------

SOURCED = {
    "saltcell.CELL_RATINGS_LB_PER_DAY": (
        "IntelliChlor output per 24h at 100%. Pentair product documentation; the "
        "Plus40 in this pool is 1.40 lb/day and replaces the legacy IC40."),
    "pentair.SALT_IDEAL_LOW / SALT_IDEAL_HIGH": (
        "3200-3400 ppm ideal. Pentair IntelliChlor installation & user's guide."),
    "pentair.SALT_OPERATING_MAX": (
        "4500 ppm — above this the cell suspends production to protect itself. "
        "Pentair IntelliChlor documentation."),
    "pentair.SALT_OPERATING_MIN": (
        "2700 ppm — bottom of the stated operating range. Same source."),
    "lsi.CORROSIVE_BELOW / SCALING_ABOVE": (
        "-0.3 to +0.3 is balanced. Pentair's own manuals state this band and warn "
        "that outside it the water damages equipment or scales the cell."),
    "lsi.CALCIUM_FLOOR": (
        "200 ppm — below this, published guidance agrees water turns aggressive "
        "toward plaster, grout and metal fittings alike."),
    "lsi.SURFACE_TARGETS": (
        "250-350 plaster/pebble/quartz, 200-250 tile, 175-225 vinyl/fibreglass. "
        "Ranges vary by source; the floor does not."),
    "lsi.tds_constant table": (
        "Standard Langelier TDS factor: 12.1 below 1000 ppm, rising to 12.4 above "
        "4000. A salt pool sits at the top, which shifts LSI by -0.3."),
    "lsi.CYA_ALKALINITY_SHARE": (
        "0.33 — cyanurate's contribution to a measured total alkalinity reading "
        "near pool pH. Standard correction in pool chemistry practice."),
}

# --- HEURISTIC: my judgement, not anyone's published figure ---------------
#
# Treat these as tunable opinions. Where one drives advice a person would act
# on, the module says so in its output rather than presenting it as settled.

HEURISTIC = {
    "chlorine_forecast.Q10": (
        "1.6 per 10F. A rate-doubling-per-10-degrees shape with a deliberately "
        "gentle exponent. Not fitted to this pool and not from a source."),
    "chlorine_forecast.DEFAULT_RATE_PPM_PER_DAY": (
        "1.5 ppm/day generic outdoor loss. Only used when there is no fitted "
        "rate AND no salt cell - with a cell it is refused outright, because "
        "pairing it with generation double-counted and projected 41 ppm."),
    "chlorine_forecast.CYA_BANDS": (
        "1.0 / 1.3 / 1.8 / 2.5 by stabilizer level. Coarse bands I chose. The "
        "direction is well established; the magnitudes are mine."),
    "chlorine_forecast.COVERED_UV_FRACTION": (
        "0.35 — how much of the low-stabilizer penalty survives under a cover. "
        "Entirely a judgement. It changed a projection from 5.9 to 19.4 ppm, "
        "so it is the single most consequential heuristic here."),
    "chlorine_forecast.MIN_SEGMENTS": (
        "3 intervals before trusting a fitted rate. Arbitrary but conservative."),
    "anomaly.MAX_DAILY_CHANGE": (
        "8 ppm chlorine, 1.2 pH, 15F per day. Plausibility bounds I invented; "
        "chlorine is loose because shocking is real, pH tight because it is "
        "buffered."),
    "anomaly.FLATLINE_READINGS / MAX_GAP_SCALING_DAYS / JUMP_FRESHNESS_DAYS": (
        "5 readings, 3 days, 7 days. Detection tuning, no external basis."),
    "trust.STAGNANT_HOURS": (
        "24h without circulation before a reading is distrusted. A pool turns "
        "over in far less, so this is deliberately generous."),
    "freshness.FETCH_STALE_HOURS / MEASURE_STALE_HOURS / PANEL_STALE_DAYS": (
        "26h, 48h, 35 days. Chosen to tolerate one missed run, one missed "
        "measurement, and a monthly panel cycle respectively."),
    "alerts.FLOW_MARGIN_FRACTION": (
        "0.15 — how close to the bottom of a band counts as no margin left."),
    "alerts.RENAG_DAYS": (
        "7 days before a standing condition may nag again."),
    "dilution.MIN_DROP_FRACTION": (
        "0.03 — salt fall counted as a top-up rather than sensor noise. The "
        "cell's salt reading is noisy at the +/-100 ppm level."),
    "interventions.SALT_STEP_PPM / SALT_STEP_WINDOW_HOURS": (
        "150 ppm within 12h reads as an addition rather than evaporation. The "
        "window matters as much as the size."),
    "consumables.CASSETTE_LEAD_DAYS / BATTERY_LEAD_DAYS": (
        "14 and 21 days of reorder notice. Chosen for shipping time."),
    "pentair.VOLUME_MISMATCH_TOLERANCE": (
        "0.10 — how far two systems may disagree about pool volume before it is "
        "worth flagging. Chosen because dose scales linearly with volume, so 10% "
        "is roughly where a recommendation starts being materially wrong."),
}


def provenance(name: str) -> tuple[str, str] | None:
    """Returns (kind, explanation) for a constant, or None if unrecorded.

    Entries may group related constants under one key ("a / b"), since they
    share a justification; a lookup for either half finds it.
    """
    for kind, table in (("measured", MEASURED), ("sourced", SOURCED), ("heuristic", HEURISTIC)):
        for key, why in table.items():
            if name == key or name in [part.strip() for part in key.split("/")]:
                return kind, why
            # "module.A / B" is shorthand for "module.A / module.B"
            module = key.split(".")[0]
            if any(name == f"{module}.{part.strip()}" for part in key.split("/")[1:]):
                return kind, why
    return None


def recorded_names() -> set[str]:
    """Every constant name the audit covers, with grouped keys expanded."""
    names = set()
    for table in (MEASURED, SOURCED, HEURISTIC):
        for key in table:
            module = key.split(".")[0]
            for part in key.split("/"):
                part = part.strip()
                names.add(part if "." in part else f"{module}.{part}")
    return names


def summary() -> str:
    return (f"{len(MEASURED)} measured, {len(SOURCED)} sourced, "
            f"{len(HEURISTIC)} heuristic")
