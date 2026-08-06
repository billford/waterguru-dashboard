"""Chlorine generation from the salt cell.

Until now the forecast modelled chlorine as decaying and nothing else, which is
wrong for a salt pool: the cell manufactures chlorine continuously whenever
water is moving. Getting this right is what lets the dashboard answer the
question actually worth asking - *what output percentage should I run?* - rather
than just reporting how fast chlorine is disappearing.

**The decomposition that matters.** What the sensor observes between two
measurements is the *net* of two opposing processes:

    observed change = generation - gross loss

A fitted decline is therefore not the pool's chlorine demand; it's demand minus
whatever the cell was making at the time. Projecting that net figure forward is
fine as long as nothing changes, which is why the old decay-only model wasn't
visibly wrong. It breaks the moment you ask "what if I turn the cell down?",
because that question needs the two terms separated:

    gross loss = observed net decline + generation at the settings in force
    net at a new output % = generation(new %) - gross loss

**The cell rating.** A Pentair IntelliChlor Plus40 (part 523735) produces 1.40
lb of chlorine per 24 hours at 100% output. That is a mass, so converting it to
ppm needs the pool volume - and the same cell in a smaller pool raises chlorine
proportionally faster. This pool is 15,000 gallons against a cell rated for
40,000, which is why it runs hot at modest output settings.

**Runtime is half the answer.** The cell only produces while the pump runs, so
an output percentage means nothing on its own. Two samples a day cannot
distinguish an 8-hour schedule from a 24-hour one, and the difference is a
factor of three in the result - hence poll_system.py.
"""

# Water weighs this much, which is what converts pounds of chlorine into ppm.
WATER_LB_PER_GALLON = 8.34

# Pentair IntelliChlor cell ratings, lb of chlorine per 24h at 100% output.
CELL_RATINGS_LB_PER_DAY = {
    "IC15": 0.60,
    "IC20": 0.70,
    "IC40": 1.40,
    "IC60": 2.00,
    # The 2025 "Plus" line replaces the legacy cells at the same output ratings.
    "PLUS40": 1.40,
    "PLUS60": 2.00,
}

DEFAULT_CELL = "PLUS40"


def cell_rating(model: str = DEFAULT_CELL) -> float | None:
    if not model:
        return None
    return CELL_RATINGS_LB_PER_DAY.get(str(model).upper().replace("-", "").replace(" ", ""))


def ppm_per_pound(gallons: float) -> float | None:
    """How much 1 lb of chlorine raises this pool, in ppm."""
    if not gallons:
        return None
    return 1_000_000 / (gallons * WATER_LB_PER_GALLON)


def generation_ppm_per_day(gallons, output_pct, runtime_fraction=1.0, model=DEFAULT_CELL) -> float | None:
    """Chlorine the cell adds per day at a given output and pump duty cycle."""
    rating = cell_rating(model)
    per_pound = ppm_per_pound(gallons)
    if rating is None or per_pound is None or output_pct is None:
        return None
    runtime = 1.0 if runtime_fraction is None else runtime_fraction
    return rating * per_pound * (output_pct / 100.0) * runtime


def gross_loss_ppm_per_day(observed_net_decline, gallons, output_pct,
                           runtime_fraction=1.0, model=DEFAULT_CELL) -> float | None:
    """The pool's actual chlorine demand, with generation added back in.

    A pool that appears to lose 1 ppm/day while its cell is quietly adding 3 is
    really consuming 4. Mistaking the net for the demand makes every "what if"
    answer wrong in the same direction.
    """
    generation = generation_ppm_per_day(gallons, output_pct, runtime_fraction, model)
    if generation is None or observed_net_decline is None:
        return None
    return observed_net_decline + generation


def recommended_output_pct(gross_loss, gallons, runtime_fraction=1.0, model=DEFAULT_CELL) -> float | None:
    """The output percentage at which generation matches demand.

    Below this chlorine trends down, above it trends up. It's a starting point
    to adjust from, not a setting to trust blindly - demand changes with sun,
    heat, bather load and stabilizer.
    """
    full_output = generation_ppm_per_day(gallons, 100.0, runtime_fraction, model)
    if not full_output or gross_loss is None:
        return None
    pct = (gross_loss / full_output) * 100.0
    return round(max(0.0, min(100.0, pct)))


def describe(gallons, output_pct, runtime_fraction, model=DEFAULT_CELL) -> str | None:
    """Plain-language summary of what the cell is contributing."""
    generation = generation_ppm_per_day(gallons, output_pct, runtime_fraction, model)
    if generation is None:
        return None

    if runtime_fraction is None:
        runtime_text = "continuous pump runtime"
    else:
        hours = runtime_fraction * 24
        # 23.75h shouldn't round to a flat "24h" - the distinction is the whole
        # reason runtime is tracked at all.
        shown = f"{hours:.0f}" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f}"
        runtime_text = f"{shown}h of pump runtime a day"
    return (
        f"At {output_pct:g}% output and {runtime_text}, the cell adds about "
        f"{generation:.1f} ppm of chlorine per day to {gallons:,.0f} gallons."
    )
