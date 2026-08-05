"""The model must not be asked to describe a trend that the data can't support.

Handed a single reading, llama3.2:3b was observed inventing a chlorine drop from
9.8 to 3.5 ppm and a pH shift to 6.9 - fabricated chemistry, stated confidently,
on a dashboard people use to decide whether the water is safe.
"""
import trend_summary
from trend_summary import MIN_READINGS_FOR_LLM, _rule_based_summary, summarize


def reading(day, free_cl=3.0, ph=7.5, temp=82.0):
    return {
        "fetched_at": f"2026-08-{day:02d}T12:00:00+00:00",
        "latest_measure_time": f"2026-08-{day:02d}T11:00:00.000Z",
        "status": "GREEN",
        "free_cl": free_cl,
        "ph": ph,
        "water_temp": temp,
        "skimmer_flow": 13.0,
    }


def test_a_single_reading_never_reaches_the_model(monkeypatch):
    called = []
    monkeypatch.setattr(trend_summary, "_recent_rows", lambda wb: [reading(1, free_cl=9.8, ph=7.3)])
    monkeypatch.setattr(trend_summary, "_llm_summary", lambda rows, name: called.append(1) or "invented")

    result = summarize("wb-1", "Pool")
    assert called == []
    assert result["source"] == "rule_based"
    assert "not enough history" in result["text"]


def test_the_thin_history_message_still_reports_the_actual_numbers(monkeypatch):
    """Saying nothing useful is its own failure - report what's known, claim no trend."""
    monkeypatch.setattr(trend_summary, "_recent_rows", lambda wb: [reading(1, free_cl=9.8, ph=7.3)])
    text = summarize("wb-1", "Pool")["text"]
    assert "free chlorine 9.8" in text
    assert "pH 7.3" in text
    assert "1 distinct reading" in text


def test_enough_readings_do_reach_the_model(monkeypatch):
    rows = [reading(d) for d in range(1, MIN_READINGS_FOR_LLM + 1)]
    monkeypatch.setattr(trend_summary, "_recent_rows", lambda wb: rows)
    monkeypatch.setattr(trend_summary, "_llm_summary", lambda rows, name: "a real summary")

    result = summarize("wb-1", "Pool")
    assert result["source"] == "llm"
    assert result["text"] == "a real summary"


def test_the_rule_based_fallback_covers_the_model_being_unreachable(monkeypatch):
    rows = [reading(d, free_cl=9.0 - d) for d in range(1, 6)]
    monkeypatch.setattr(trend_summary, "_recent_rows", lambda wb: rows)
    monkeypatch.setattr(trend_summary, "_llm_summary", lambda rows, name: None)

    result = summarize("wb-1", "Pool")
    assert result["source"] == "rule_based"
    assert "falling" in result["text"]


def test_no_readings_at_all_says_so():
    assert "No readings yet" in _rule_based_summary([])


def test_reading_count_reflects_distinct_measurements(monkeypatch):
    rows = [reading(d) for d in range(1, 5)]
    monkeypatch.setattr(trend_summary, "_recent_rows", lambda wb: rows)
    monkeypatch.setattr(trend_summary, "_llm_summary", lambda rows, name: "summary")
    assert summarize("wb-1", "Pool")["reading_count"] == 4
