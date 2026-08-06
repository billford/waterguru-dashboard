"""Volume-corrected doses.

WaterGuru computes doses from the volume configured in its app. That was set to
20,000 gallons; the pool is 15,000. Doses scale linearly with volume, so every
recommendation was a third too large - including "add 73.3 cups of calcium
chloride", where a third too much has to be diluted back out over weeks.
"""
import dosing


FACTOR = dosing.scale_factor(15000, 20000)


def test_the_factor_is_the_ratio_of_real_to_configured_volume():
    assert FACTOR == 0.75
    assert dosing.scale_factor(None, 20000) is None
    assert dosing.scale_factor(15000, None) is None


def test_a_dose_is_corrected_to_the_real_volume():
    note = dosing.correct_dose("Add 73.3 cups of 90% concentration calcium chloride.", FACTOR)
    assert "55 cups" in note


def test_the_units_of_the_original_are_kept():
    assert "1.5 pounds" in dosing.correct_dose("Add 2 pounds of soda ash.", FACTOR)
    assert "6 oz" in dosing.correct_dose("Add 8 oz of acid.", FACTOR)


def test_a_concentration_percentage_is_not_mistaken_for_a_dose():
    """"90% concentration" is a property of the chemical, not an amount."""
    note = dosing.correct_dose("Add 73.3 cups of 90% concentration calcium chloride.", FACTOR)
    assert "67.5" not in note  # 90 * 0.75, i.e. the percentage scaled by mistake


def test_advice_with_no_quantity_is_left_alone():
    assert dosing.correct_dose("Decrease chlorine generator output.", FACTOR) is None


def test_matching_volumes_produce_no_correction():
    assert dosing.correct_dose("Add 10 cups of X.", 1.0) is None


def test_a_trivial_difference_is_not_worth_the_words():
    assert dosing.correct_dose("Add 10 cups of X.", 0.98) is None


def test_missing_volume_data_produces_no_correction():
    assert dosing.correct_dose("Add 10 cups of X.", None) is None


def test_annotating_alerts_leaves_the_original_advice_intact():
    """Silently rewriting someone else's dosing advice is its own hazard."""
    alerts = [{"text": "Calcium Hardness very low",
               "advice": "Add 73.3 cups of 90% concentration calcium chloride."}]
    annotated = dosing.annotate_alerts(alerts, FACTOR)
    assert annotated[0]["advice"] == "Add 73.3 cups of 90% concentration calcium chloride."
    assert "55 cups" in annotated[0]["dose_correction"]


def test_alerts_without_doses_get_no_correction_key():
    alerts = [{"text": "pH low", "advice": None}]
    assert "dose_correction" not in dosing.annotate_alerts(alerts, FACTOR)[0]
