"""Corrects WaterGuru's dose recommendations for the real pool volume.

WaterGuru computes doses from the volume configured in its app. That figure was
20,000 gallons; the pool is 15,000. Chemical doses scale linearly with volume,
so every recommendation it makes is 33% too large - and these are instructions
like "add 73.3 cups of calcium chloride", where a third too much is a real
overshoot that then has to be diluted back out over weeks.

The right fix is to correct the volume in the WaterGuru app, after which this
module does nothing. Until then it shows the corrected figure alongside the
original rather than letting the page state a number that would overdose the
pool. It never rewrites WaterGuru's text - the original stays visible, because
silently editing someone else's dosing advice is its own kind of dangerous.
"""
import re

# "Add 73.3 cups of 90% concentration calcium chloride." - the leading quantity
# is the dose. Percentages are concentrations, not amounts, so they're excluded
# by requiring a unit of measure.
_DOSE = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>cups?|ounces?|oz|pounds?|lbs?|gallons?|quarts?|liters?|litres?|ml)\b",
    re.IGNORECASE,
)

# Below this the correction isn't worth the words.
MIN_CORRECTION = 0.05


def scale_factor(actual_gallons, configured_gallons) -> float | None:
    if not actual_gallons or not configured_gallons:
        return None
    return actual_gallons / configured_gallons


def correct_dose(advice: str, factor: float | None) -> str | None:
    """A sentence giving the volume-corrected dose, or None if not applicable."""
    if not advice or not factor or abs(factor - 1) < MIN_CORRECTION:
        return None

    match = _DOSE.search(advice)
    if not match:
        return None

    amount = float(match.group("amount"))
    unit = match.group("unit")
    corrected = amount * factor

    # Keep the precision of the original rather than inventing more.
    corrected_text = f"{corrected:.1f}".rstrip("0").rstrip(".")
    return (
        f"That dose is sized for the volume set in the WaterGuru app. "
        f"For this pool's actual volume it's about {corrected_text} {unit}."
    )


def annotate_alerts(alerts: list[dict], factor: float | None) -> list[dict]:
    """Adds a `dose_correction` to any alert carrying a scalable dose."""
    if not factor:
        return alerts
    for alert in alerts:
        correction = correct_dose(alert.get("advice"), factor)
        if correction:
            alert["dose_correction"] = correction
    return alerts
