"""The dashboard, rendered in a real browser.

1,700 lines of vanilla JS that nothing was watching. Every case here is a bug
that actually shipped and was found by hand — the point of writing them down is
that they can't come back quietly.

These are slower than the rest of the suite (Chrome costs a second or two per
distinct scenario, and renders are cached across tests that share one), but the
alternative has been finding these by eye.
"""
import re

import pytest
from dashboard import DELETE, render, waterbody

# Marked so `-m "not dashboard"` skips them while iterating on Python; they add
# ~50s to a suite that otherwise runs in under a second. They still run by
# default, because the whole point is that this code had nothing watching it.
pytestmark = [
    pytest.mark.dashboard,
    pytest.mark.skipif(
        __import__("dashboard").chrome_path() is None,
        reason="needs Chrome or Chromium to render the page",
    ),
]


def _tiles(dom):
    """Count tile elements.

    Matching `class="tile"` exactly would miss every styled tile
    (`class="tile status-RED"`), and matching `class="tile` alone would also
    catch the `tiles` container.
    """
    return len(re.findall(r'class="tile[ "]', dom))


def _charts(dom):
    return dom.count('class="chart-svg"')


def _green_tiles(dom):
    """Tiles carrying the in-range stripe. The bare string `status-GREEN` also
    appears once in the stylesheet, which is not a rendered tile."""
    return len(re.findall(r'class="tile[^"]*status-GREEN', dom))


# ---- the page renders at all ----

def test_the_intact_dashboard_renders_tiles_and_charts():
    dom = render()
    assert _tiles(dom) > 10
    assert _charts(dom) >= 4
    assert 'id="pageBanner"' not in dom


def test_no_failure_banner_when_everything_is_present():
    assert 'id="pageBanner"' not in render()


# ---- one missing field must not blank the page ----
# Each of these previously aborted every render after it, because load() ran the
# eight render calls as a bare sequence with no error handling.

def _drop_field(field):
    def mutate(data):
        waterbody(data).pop(field, None)
        return data
    return mutate


@pytest.mark.parametrize("field", ["targets", "series", "temp_series", "latest", "freshness"])
def test_a_missing_waterbody_field_does_not_blank_the_page(field):
    dom = render(**{"history.json": _drop_field(field)})
    assert _tiles(dom) > 0, f"dropping {field} blanked the tiles"


def test_a_missing_targets_still_draws_the_charts():
    """The original failure: no targets meant no tiles and no charts at all."""
    dom = render(**{"history.json": _drop_field("targets")})
    assert _charts(dom) >= 4


def test_a_weather_day_missing_its_name_does_not_kill_five_cards():
    """A missing field in an *optional* file used to take out everything after
    the forecast: chlorine outlook, sensor health, system, pool log, swim tip."""
    def mutate(data):
        for day in data.get("days", []):
            day.pop("name", None)
        return data

    dom = render(**{"weather.json": mutate})
    assert 'id="systemCard" style="display: none' not in dom
    assert _tiles(dom) > 10


# ---- missing optional feeds degrade quietly ----

@pytest.mark.parametrize("feed", [
    "summary.json", "weather.json", "swim_advice.json",
    "chlorine_forecast.json", "anomalies.json", "system.json", "pool_log.json",
])
def test_each_optional_feed_can_be_absent(feed):
    dom = render(**{feed: DELETE})
    assert _tiles(dom) >= 7
    assert 'id="pageBanner"' not in dom


def test_every_optional_feed_absent_at_once_still_renders():
    """Only history.json is required; everything else is enrichment."""
    absent = {f: DELETE for f in [
        "summary.json", "weather.json", "swim_advice.json", "chlorine_forecast.json",
        "anomalies.json", "system.json", "pool_log.json"]}
    dom = render(**absent)
    assert _tiles(dom) >= 7        # the seven core readings survive
    assert 'id="pageBanner"' not in dom


# ---- a failure that IS fatal must say so ----

def test_losing_the_core_data_shows_a_notice_rather_than_an_empty_dashboard():
    """Without this, a failed load looked identical to a pool with no readings."""
    dom = render(**{"history.json": DELETE})
    assert 'id="pageBanner"' in dom
    assert "Could not load pool data" in dom


def test_a_missing_waterbodies_key_is_reported_not_crashed():
    dom = render(**{"history.json": lambda d: {k: v for k, v in d.items() if k != "waterbodies"}})
    assert 'id="pageBanner"' in dom


# ---- the XSS that shipped ----

def test_markup_in_a_reading_is_not_executed():
    """renderTable was the one innerHTML sink that received data, and it runs on
    load even while the table is hidden - so this needed no interaction."""
    def mutate(data):
        waterbody(data)["series"][-1]["status"] = (
            '<img src=x onerror="document.title=\'XSS\'">')
        return data

    dom = render(**{"history.json": mutate})
    assert "<title>XSS</title>" not in dom
    assert "Pool Dashboard" in dom


def test_markup_in_a_reading_is_shown_as_text():
    def mutate(data):
        waterbody(data)["series"][-1]["status"] = "<b>bold</b>"
        return data

    dom = render(**{"history.json": mutate})
    assert "&lt;b&gt;bold&lt;/b&gt;" in dom


# ---- absent readings must not look healthy ----

def test_an_empty_series_does_not_render_green_in_range_tiles():
    """statusFor returned GREEN whenever no alert matched - including when there
    was no reading to alert about - so a missing value got the green stripe and
    a "1h ago" subtitle."""
    def mutate(data):
        waterbody(data)["series"] = []
        return data

    dom = render(**{"history.json": mutate})
    assert _green_tiles(dom) == 0


def test_a_pool_with_no_readings_still_renders_the_page():
    def mutate(data):
        waterbody(data)["series"] = []
        return data

    assert _tiles(render(**{"history.json": mutate})) > 0
