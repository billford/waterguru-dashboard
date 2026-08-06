"""Salt cell generation.

The pool has a Pentair IntelliChlor Plus40 (part 523735), rated 1.40 lb of
chlorine per 24h at 100% output, in 15,000 gallons - a cell sized for 40,000,
which is why chlorine runs high at modest output settings.
"""
import saltcell

GALLONS = 15000


def test_the_plus_line_matches_the_legacy_cell_it_replaces():
    assert saltcell.cell_rating("PLUS40") == saltcell.cell_rating("IC40") == 1.40
    assert saltcell.cell_rating("IC60") == 2.00


def test_model_names_are_matched_loosely():
    assert saltcell.cell_rating("plus40") == 1.40
    assert saltcell.cell_rating("IC-40") == 1.40
    assert saltcell.cell_rating("nonsense") is None


def test_a_pound_of_chlorine_goes_further_in_a_smaller_pool():
    assert round(saltcell.ppm_per_pound(15000), 1) == 8.0
    assert round(saltcell.ppm_per_pound(40000), 1) == 3.0


def test_generation_scales_with_output_and_runtime():
    full = saltcell.generation_ppm_per_day(GALLONS, 100, 1.0)
    assert round(full, 1) == 11.2

    assert round(saltcell.generation_ppm_per_day(GALLONS, 50, 1.0), 1) == round(full / 2, 1)
    assert round(saltcell.generation_ppm_per_day(GALLONS, 100, 0.5), 1) == round(full / 2, 1)


def test_runtime_is_the_difference_between_a_third_and_all_of_it():
    """8h vs 24h of pump is a 3x swing - which is why it can't be assumed."""
    eight = saltcell.generation_ppm_per_day(GALLONS, 100, 8 / 24)
    day = saltcell.generation_ppm_per_day(GALLONS, 100, 1.0)
    assert round(day / eight) == 3


def test_demand_is_the_observed_decline_plus_what_the_cell_was_adding():
    """A pool losing 1 ppm/day while its cell adds 3 really consumes 4."""
    generation = saltcell.generation_ppm_per_day(GALLONS, 50, 1.0)
    gross = saltcell.gross_loss_ppm_per_day(1.0, GALLONS, 50, 1.0)
    assert round(gross, 2) == round(1.0 + generation, 2)


def test_the_recommended_output_is_where_generation_matches_demand():
    demand = saltcell.generation_ppm_per_day(GALLONS, 40, 1.0)
    assert saltcell.recommended_output_pct(demand, GALLONS, 1.0) == 40


def test_the_recommendation_is_clamped_to_a_real_setting():
    assert saltcell.recommended_output_pct(999, GALLONS, 1.0) == 100
    assert saltcell.recommended_output_pct(-5, GALLONS, 1.0) == 0


def test_missing_inputs_produce_no_number_rather_than_a_wrong_one():
    assert saltcell.generation_ppm_per_day(None, 50, 1.0) is None
    assert saltcell.generation_ppm_per_day(GALLONS, None, 1.0) is None
    assert saltcell.recommended_output_pct(None, GALLONS, 1.0) is None
    assert saltcell.gross_loss_ppm_per_day(None, GALLONS, 50, 1.0) is None


def test_the_description_states_its_assumptions():
    text = saltcell.describe(GALLONS, 50, 8 / 24)
    assert "50% output" in text and "8h of pump runtime" in text and "15,000 gallons" in text
