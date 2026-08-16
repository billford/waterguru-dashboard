"""Free chlorine judged against stabilizer, rather than on its own.

A chlorine reading means nothing without the cyanuric acid level beside it.
Cyanuric acid binds most of the free chlorine into a reserve and releases it
slowly; what stays unbound is hypochlorous acid, which is simultaneously the
thing that sanitises the water, the thing UV destroys, and the thing that stings
eyes. So the *same* 6.5 ppm reading is comfortable in a pool holding 50 ppm of
stabilizer and harsh in a pool holding 11.

This matters here because two complaints that look unrelated - "the chlorine
keeps hitting zero" and "the chlorine is burning eyes" - are the same fault seen
from either side. Without stabilizer the unbound fraction is high, so it stings
while it is present and burns off quickly once the sun reaches it. Turning the
cell down fixes the sting by walking toward the zero.

The percentages come from the Trouble Free Pool chlorine/CYA relationship, which
is the most widely replicated version of this: roughly 7.5% of CYA as a floor,
about 11% as an ordinary target, 40% while shocking. They are ratios rather than
fixed ppm values precisely because the absolute number is not the thing.

One limit worth stating plainly: this cannot see combined chlorine. Chloramines
are the other common cause of stinging eyes and a pool smelling strongly of
"chlorine", and neither the pod nor the controller measures them. A ratio that
looks fine here does not rule them out.
"""

MIN_FRACTION = 0.075
TARGET_FRACTION = 0.11
SHOCK_FRACTION = 0.40

# Below this, the ratio rules stop being useful: the target lands so low that
# ordinary measurement error spans the whole band, and no cell can hold it there.
MIN_USEFUL_CYA = 20.0

# Outdoor pools want stabilizer in this range. Sources vary at the edges - Pentair
# says 30-50, Trouble Free Pool prefers 60-80 for a salt pool - but nobody
# recommends single digits.
CYA_OUTDOOR_MIN = 30.0
CYA_SALT_POOL_MIN = 60.0


def targets(cya: float) -> dict:
    """The chlorine band this pool's stabilizer level implies."""
    return {
        "minimum": round(cya * MIN_FRACTION, 1),
        "target": round(cya * TARGET_FRACTION, 1),
        "shock": round(cya * SHOCK_FRACTION, 1),
    }


def assess(free_cl: float | None, cya: float | None, salt_pool: bool = False) -> dict | None:
    """How the chlorine reading sits against the stabilizer, or None if unknowable."""
    if free_cl is None or cya is None or cya <= 0:
        return None

    band = targets(cya)
    ratio = free_cl / cya
    recommended_cya = CYA_SALT_POOL_MIN if salt_pool else CYA_OUTDOOR_MIN

    if cya < MIN_USEFUL_CYA:
        # The honest reading: the problem is the denominator, not the numerator.
        severity = "warning"
        headline = (
            f"Stabilizer is {cya:g} ppm, far below the {recommended_cya:g}+ ppm an outdoor "
            f"{'salt ' if salt_pool else ''}pool needs.")
        detail = (
            f"At that level almost none of the {free_cl:g} ppm of chlorine is held in "
            "reserve - it is nearly all in the unbound, active form. That is the form that "
            "stings eyes, and it is also the form sunlight destroys within hours. Turning "
            "the cell down will ease the sting by lowering chlorine toward zero, which is "
            "where this pool has already been once. Raising stabilizer fixes both ends: the "
            "same chlorine becomes gentler to swim in and lasts long enough to hold."
        )
    elif free_cl >= band["shock"]:
        severity = "warning"
        headline = f"Chlorine is at shock level for {cya:g} ppm stabilizer."
        detail = (f"{free_cl:g} ppm against a {band['target']:g} ppm target. Swimming is "
                  "usually held off until it falls below "
                  f"{band['shock']:g} ppm.")
    elif free_cl < band["minimum"]:
        severity = "warning"
        headline = f"Chlorine is below the minimum for {cya:g} ppm stabilizer."
        detail = (f"{free_cl:g} ppm against a {band['minimum']:g} ppm floor. Below this, "
                  "algae can establish faster than the chlorine suppresses it.")
    else:
        severity = "ok"
        headline = f"Chlorine sits correctly against {cya:g} ppm stabilizer."
        detail = f"{free_cl:g} ppm, within the {band['minimum']:g}-{band['shock']:g} ppm band."

    return {
        "free_cl": free_cl,
        "cya": cya,
        "ratio": round(ratio, 3),
        "ratio_pct": round(ratio * 100),
        "band": band,
        "recommended_cya": recommended_cya,
        "severity": severity,
        "headline": headline,
        "detail": detail,
        "caveat": (
            "Combined chlorine is the other usual cause of stinging eyes, and nothing in "
            "this system measures it. A DPD test kit that reports both free and total "
            "chlorine would settle it - the difference between the two is the part that "
            "irritates."
        ),
    }
