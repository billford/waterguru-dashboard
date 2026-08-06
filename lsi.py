"""Langelier Saturation Index - whether the water is dissolving the pool.

Five numbers already collected (pH, alkalinity, calcium, stabilizer, temperature)
say nothing on their own about the question that actually damages a pool: is this
water hungry for calcium, or oversupplied with it? Water below saturation takes
calcium from wherever it can reach, which in a plaster pool means the plaster.
Above saturation it deposits it, which means scale on the cell and the heater.

    LSI = pH + temperature factor + calcium factor + alkalinity factor - K

- **Below -0.3**: aggressive. Etches plaster, corrodes metal, and eventually
  roughens the surface. Not reversible - the calcium came from somewhere.
- **-0.3 to +0.3**: balanced.
- **Above +0.3**: scaling. Deposits on the salt cell, which shortens its life,
  and on the heater exchanger.

**Two corrections that matter here and are easy to miss.**

*Cyanurate is not carbonate alkalinity.* Stabilizer contributes to a measured
total alkalinity number without providing any of the buffering LSI cares about,
so it has to be subtracted out first. Skipping this overstates LSI on any
stabilized pool.

*K depends on dissolved solids.* The familiar 12.1 is for fresh water. A salt
pool carries thousands of ppm of dissolved salt, pushing K to 12.4 - which moves
LSI by -0.3, straight into the range where the verdict changes. Using the
freshwater constant on a salt pool reports "borderline" where the truth is
"corrosive".

This is the standard index, not a novel model, but every input carries
measurement error and pH dominates the result. Treat it as a direction to
investigate rather than a dosing instruction.
"""
import math

# Temperature factor, °F -> contribution.
_TEMP_FACTORS = [(32, 0.0), (37, 0.1), (46, 0.2), (53, 0.3), (60, 0.4),
                 (66, 0.5), (76, 0.6), (84, 0.7), (94, 0.8), (105, 0.9)]

# Cyanurate's share of a measured total alkalinity reading, near typical pool pH.
CYA_ALKALINITY_SHARE = 0.33

CORROSIVE_BELOW = -0.3
SCALING_ABOVE = 0.3
SEVERE_BELOW = -0.5


def tds_constant(tds_ppm: float | None) -> float:
    """The K term. Higher dissolved solids, higher K, lower LSI."""
    if tds_ppm is None:
        return 12.1
    if tds_ppm <= 1000:
        return 12.1
    if tds_ppm <= 2000:
        return 12.2
    if tds_ppm <= 4000:
        return 12.3
    return 12.4


def temperature_factor(temp_f: float) -> float:
    return min(_TEMP_FACTORS, key=lambda pair: abs(pair[0] - temp_f))[1]


def carbonate_alkalinity(total_alkalinity: float, cya: float | None) -> float:
    """Total alkalinity with the cyanurate contribution removed."""
    return max(total_alkalinity - (cya or 0) * CYA_ALKALINITY_SHARE, 1.0)


def calculate(ph, temp_f, calcium_hardness, total_alkalinity, cya=None, salt_ppm=None) -> dict | None:
    """LSI and what it means, or None if an input is missing."""
    if None in (ph, temp_f, calcium_hardness, total_alkalinity):
        return None
    if calcium_hardness <= 0 or total_alkalinity <= 0:
        return None

    # Salt is the dominant dissolved solid in a salt pool; other sources add a
    # few hundred ppm, so this slightly understates TDS and therefore K.
    tds = (salt_ppm + 500) if salt_ppm else None
    k = tds_constant(tds)
    carbonate = carbonate_alkalinity(total_alkalinity, cya)

    value = (
        ph
        + temperature_factor(temp_f)
        + (math.log10(calcium_hardness) - 0.4)
        + math.log10(carbonate)
        - k
    )
    value = round(value, 2)

    if value < CORROSIVE_BELOW:
        verdict = "corrosive"
        severity = "severe" if value < SEVERE_BELOW else "warning"
    elif value > SCALING_ABOVE:
        verdict = "scaling"
        severity = "warning"
    else:
        verdict = "balanced"
        severity = None

    return {
        "value": value,
        "verdict": verdict,
        "severity": severity,
        "inputs": {
            "ph": ph, "temp_f": temp_f, "calcium_hardness": calcium_hardness,
            "total_alkalinity": total_alkalinity, "cya": cya, "salt_ppm": salt_ppm,
        },
        "carbonate_alkalinity": round(carbonate, 1),
        "k": k,
        "text": _describe(value, verdict, severity),
        "lever": _biggest_lever(ph, calcium_hardness, total_alkalinity, cya),
    }


def _describe(value, verdict, severity) -> str:
    if verdict == "corrosive":
        strength = "strongly " if severity == "severe" else ""
        return (
            f"Saturation index {value:+.2f} - the water is {strength}aggressive. Below saturation "
            "it takes calcium from whatever it can reach, which in a plaster pool is the plaster, "
            "and it attacks metal in the heater. This isn't reversible: the calcium came from "
            "somewhere."
        )
    if verdict == "scaling":
        return (
            f"Saturation index {value:+.2f} - the water is depositing calcium rather than "
            "dissolving it. Scale builds on the salt cell, which shortens its life, and on the "
            "heater exchanger."
        )
    return f"Saturation index {value:+.2f} - balanced. The water is neither dissolving nor depositing calcium."


def _biggest_lever(ph, calcium_hardness, total_alkalinity, cya) -> str | None:
    """Which input is furthest from where it should be.

    pH moves LSI one-for-one and is the fastest to change, so it usually wins -
    but if calcium is the outlier, raising pH alone just masks the real problem.
    """
    if calcium_hardness < 200:
        return (
            f"Calcium is the outlier at {calcium_hardness:g} ppm (a plaster pool wants 300-400). "
            "Raising pH alone would move the index without fixing what's driving it."
        )
    if ph < 7.4:
        return f"pH at {ph:g} is the fastest lever - it moves the index one-for-one."
    if total_alkalinity < 60:
        return f"Alkalinity at {total_alkalinity:g} ppm is low, which also makes pH unstable."
    return None
