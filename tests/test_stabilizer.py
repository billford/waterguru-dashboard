import stabilizer


def test_low_stabilizer_blames_the_denominator():
    """At 11 ppm CYA the fault is the stabilizer, not the chlorine number."""
    a = stabilizer.assess(6.5, 11.0, salt_pool=True)
    assert a["severity"] == "warning"
    assert "Stabilizer is 11 ppm" in a["headline"]
    assert "60" in a["headline"]          # salt pools want more, not less
    assert "toward zero" in a["detail"]   # says what turning the cell down costs


def test_the_same_chlorine_is_fine_with_stabilizer_in_range():
    """6.5 ppm stings at CYA 11 and is exactly on target at CYA 60."""
    assert stabilizer.assess(6.5, 11.0)["severity"] == "warning"
    ok = stabilizer.assess(6.5, 60.0)
    assert ok["severity"] == "ok"
    assert abs(ok["band"]["target"] - 6.6) < 0.05


def test_shock_level_is_relative_to_cya():
    assert stabilizer.assess(13.0, 30.0)["severity"] == "warning"   # above 40% of 30
    assert stabilizer.assess(13.0, 80.0)["severity"] == "ok"        # below 40% of 80


def test_below_the_floor_is_flagged():
    a = stabilizer.assess(1.0, 60.0)
    assert a["severity"] == "warning" and "below the minimum" in a["headline"]


def test_unknowable_without_both_numbers():
    assert stabilizer.assess(6.5, None) is None
    assert stabilizer.assess(None, 40.0) is None
    assert stabilizer.assess(6.5, 0) is None


def test_combined_chlorine_caveat_is_always_stated():
    """Nothing here measures chloramines, and the output must not imply it does."""
    assert "Combined chlorine" in stabilizer.assess(6.5, 11.0)["caveat"]
