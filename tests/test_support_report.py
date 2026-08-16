import support_report


def _event(when, consumed, outcome, flow, max_pads=192):
    return {"fetched_at": when, "consumed": consumed, "outcome": outcome,
            "skimmer_flow": flow, "cassette_pads_max": max_pads,
            "cassette_pads_left": 100}


def test_blocks_split_at_each_reinstall():
    events = [
        _event("2026-08-01T00:00:00Z", None, "first observation", 16),
        _event("2026-08-02T00:00:00Z", 3, "ATTEMPT FAILED - no reading produced", 16),
        _event("2026-08-03T00:00:00Z", -12, "cassette replaced/re-registered", 16),
        _event("2026-08-04T00:00:00Z", 10, "measurement completed", 16),
    ]
    blocks = support_report.by_cassette(events)
    assert len(blocks) == 2
    assert len(blocks[0]["failed"]) == 1 and not blocks[0]["succeeded"]
    assert len(blocks[1]["succeeded"]) == 1 and not blocks[1]["failed"]


def test_failures_at_healthy_flow_are_counted_separately():
    """Low flow is the answer support reaches for; this pre-empts it."""
    events = [
        _event("2026-08-01T00:00:00Z", None, "first observation", 16),
        _event("2026-08-02T00:00:00Z", 3, "ATTEMPT FAILED - no reading produced", 3),
        _event("2026-08-03T00:00:00Z", 4, "ATTEMPT FAILED - no reading produced", 16),
        _event("2026-08-04T00:00:00Z", 5, "ATTEMPT FAILED - no reading produced", 17),
    ]
    text = "\n".join(support_report.cross_cassette_summary(events))
    assert "3 failed attempts in total, 2 of them" in text
    assert "low flow does not account for them" in text


def test_summary_asks_for_a_pod_rather_than_more_cassettes():
    events = [
        _event("2026-08-01T00:00:00Z", None, "first observation", 16),
        _event("2026-08-02T00:00:00Z", 3, "ATTEMPT FAILED - no reading produced", 16),
    ]
    text = "\n".join(support_report.cross_cassette_summary(events))
    assert "replacing the pod" in text
