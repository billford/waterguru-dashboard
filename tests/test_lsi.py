"""Saturation index — whether the water is dissolving the pool or scaling it.

Five numbers already collected say nothing individually about the question that
actually damages a pool. Two corrections are easy to get wrong and both change
the verdict rather than just the value.
"""
import pytest

import lsi


def test_a_balanced_pool_reads_near_zero():
    result = lsi.calculate(ph=7.5, temp_f=84, calcium_hardness=350,
                           total_alkalinity=100, cya=40, salt_ppm=3200)
    assert -0.3 <= result["value"] <= 0.3
    assert result["verdict"] == "balanced"


def test_this_pool_is_corrosive():
    """pH 7.2, calcium 152, in a salt pool — the live numbers."""
    result = lsi.calculate(ph=7.2, temp_f=84, calcium_hardness=152,
                           total_alkalinity=151, cya=14, salt_ppm=4350)
    assert result["verdict"] == "corrosive"
    assert result["severity"] == "severe"
    assert "takes calcium" in result["text"]


def test_high_calcium_and_ph_scales_instead():
    result = lsi.calculate(ph=8.0, temp_f=84, calcium_hardness=600,
                           total_alkalinity=180, cya=40, salt_ppm=3200)
    assert result["verdict"] == "scaling"
    assert "salt cell" in result["text"]


# ---- the two corrections that change the verdict ----

def test_a_salt_pool_uses_a_higher_constant_than_fresh_water():
    """The familiar 12.1 is for fresh water. Salt pushes K to 12.4, which moves
    LSI by -0.3 - straight across the corrosive threshold."""
    fresh = lsi.calculate(ph=7.2, temp_f=84, calcium_hardness=152,
                          total_alkalinity=151, cya=14, salt_ppm=None)
    salty = lsi.calculate(ph=7.2, temp_f=84, calcium_hardness=152,
                          total_alkalinity=151, cya=14, salt_ppm=4350)
    assert salty["k"] == 12.4 and fresh["k"] == 12.1
    assert salty["value"] == pytest.approx(fresh["value"] - 0.3, abs=0.01)


@pytest.mark.parametrize("tds, k", [(500, 12.1), (1500, 12.2), (3500, 12.3), (5000, 12.4)])
def test_the_constant_follows_dissolved_solids(tds, k):
    assert lsi.tds_constant(tds) == k


def test_cyanurate_is_removed_from_alkalinity():
    """Stabilizer inflates a measured TA number without providing the buffering
    LSI cares about; not subtracting it overstates the index."""
    assert lsi.carbonate_alkalinity(151, 14) == pytest.approx(146.4, abs=0.1)
    assert lsi.carbonate_alkalinity(151, None) == 151


def test_high_stabilizer_meaningfully_lowers_the_index():
    low = lsi.calculate(ph=7.5, temp_f=84, calcium_hardness=300,
                        total_alkalinity=100, cya=0, salt_ppm=3200)
    high = lsi.calculate(ph=7.5, temp_f=84, calcium_hardness=300,
                         total_alkalinity=100, cya=90, salt_ppm=3200)
    assert high["value"] < low["value"]


# ---- behaviour ----

def test_ph_moves_the_index_one_for_one():
    a = lsi.calculate(ph=7.0, temp_f=84, calcium_hardness=300,
                      total_alkalinity=100, cya=40, salt_ppm=3200)
    b = lsi.calculate(ph=7.5, temp_f=84, calcium_hardness=300,
                      total_alkalinity=100, cya=40, salt_ppm=3200)
    assert b["value"] == pytest.approx(a["value"] + 0.5, abs=0.01)


def test_warmer_water_is_less_aggressive():
    cool = lsi.calculate(ph=7.4, temp_f=60, calcium_hardness=300,
                         total_alkalinity=100, cya=40, salt_ppm=3200)
    warm = lsi.calculate(ph=7.4, temp_f=94, calcium_hardness=300,
                         total_alkalinity=100, cya=40, salt_ppm=3200)
    assert warm["value"] > cool["value"]


def test_low_calcium_is_named_as_the_real_lever():
    """Raising pH alone would move the number without fixing the cause."""
    result = lsi.calculate(ph=7.2, temp_f=84, calcium_hardness=152,
                           total_alkalinity=151, cya=14, salt_ppm=4350)
    assert "Calcium is the outlier" in result["lever"]
    assert "raising pH alone would move the" in result["lever"]


def test_ph_is_the_lever_when_calcium_is_fine():
    result = lsi.calculate(ph=7.0, temp_f=84, calcium_hardness=350,
                           total_alkalinity=100, cya=40, salt_ppm=3200)
    assert "pH at 7" in result["lever"]


# ---- missing inputs ----

@pytest.mark.parametrize("missing", ["ph", "temp_f", "calcium_hardness", "total_alkalinity"])
def test_a_missing_input_yields_no_index_rather_than_a_wrong_one(missing):
    kwargs = dict(ph=7.5, temp_f=84, calcium_hardness=300, total_alkalinity=100,
                  cya=40, salt_ppm=3200)
    kwargs[missing] = None
    assert lsi.calculate(**kwargs) is None


def test_impossible_inputs_yield_nothing():
    assert lsi.calculate(ph=7.5, temp_f=84, calcium_hardness=0,
                         total_alkalinity=100) is None


def test_stabilizer_and_salt_are_optional():
    assert lsi.calculate(ph=7.5, temp_f=84, calcium_hardness=300,
                         total_alkalinity=100) is not None


# ---- what the water is in contact with decides what a corrosive index costs ----

def test_the_index_itself_does_not_depend_on_the_surface():
    """LSI is pH, calcium, alkalinity, temperature and dissolved solids. The
    surface decides whether the answer matters, not what the answer is."""
    plaster = lsi.calculate(ph=7.2, temp_f=84, calcium_hardness=152,
                            total_alkalinity=151, cya=14, salt_ppm=4300, surface="PLASTER")
    tile = lsi.calculate(ph=7.2, temp_f=84, calcium_hardness=152,
                         total_alkalinity=151, cya=14, salt_ppm=4300, surface="TILE")
    assert plaster["value"] == tile["value"] == -0.55


def test_a_plaster_pool_is_told_its_surface_is_at_stake():
    result = lsi.calculate(ph=7.2, temp_f=84, calcium_hardness=152,
                           total_alkalinity=151, cya=14, salt_ppm=4300, surface="PLASTER")
    assert "the plaster itself" in result["lever"]
    assert "250-350" in result["lever"]


def test_a_tiled_pool_is_told_the_grout_and_equipment_are_at_stake():
    """Glazed tile is inert; the exposure is the cementitious grout and the metal."""
    result = lsi.calculate(ph=7.2, temp_f=84, calcium_hardness=152,
                           total_alkalinity=151, cya=14, salt_ppm=4300, surface="TILE")
    assert "grout" in result["lever"]
    assert "200-250" in result["lever"]
    assert "plaster" not in result["lever"]


def test_a_tiled_pool_needs_less_calcium_than_a_plastered_one():
    assert lsi.SURFACE_TARGETS["TILE"][0] < lsi.SURFACE_TARGETS["PLASTER"][0]


def test_an_unknown_surface_falls_back_to_the_most_demanding_case():
    """Guessing low would under-protect a plaster pool; guessing high only costs
    a little calcium."""
    result = lsi.calculate(ph=7.2, temp_f=84, calcium_hardness=152,
                           total_alkalinity=151, cya=14, salt_ppm=4300, surface="MYSTERY")
    assert "250-350" in result["lever"]


def test_calcium_above_the_floor_is_not_named_as_the_lever():
    result = lsi.calculate(ph=7.0, temp_f=84, calcium_hardness=250,
                           total_alkalinity=100, cya=40, salt_ppm=3200, surface="TILE")
    assert "Calcium is the outlier" not in (result["lever"] or "")
