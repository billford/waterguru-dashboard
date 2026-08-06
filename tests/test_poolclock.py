"""Day boundaries belong to the pool, not to UTC.

launchd runs the pipeline at 08:00 and 20:00 local. In an eastern summer the
evening run is 00:00 UTC *the next day*, so half of all runs believed it was
tomorrow — the forecast skipped the current day, the swim advisor's prompt
asserted the wrong weekday, and the weekly digest fired on Saturday evening.
"""
from datetime import datetime, timezone

import pytest

import digest
import poolclock

# 20:00 EDT on the given day == 00:00 UTC the next day.
SAT_EVENING = datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)    # Sat Aug 8 local
SUN_MORNING = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)   # Sun Aug 9 local
SUN_EVENING = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)   # Sun Aug 9 local


@pytest.fixture
def eastern(monkeypatch):
    """Pin the pool to UTC-4 so the test doesn't depend on the host's zone."""
    monkeypatch.setenv("POOL_TZ_OFFSET", "-4")


def test_the_evening_run_is_still_the_same_local_day(eastern):
    assert poolclock.pool_today(SAT_EVENING).isoformat() == "2026-08-08"
    assert SAT_EVENING.date().isoformat() == "2026-08-09", "UTC really is a day ahead here"


def test_saturday_evening_is_saturday_at_the_pool(eastern):
    assert poolclock.pool_weekday(SAT_EVENING) == 5
    assert SAT_EVENING.weekday() == 6, "in UTC it looks like Sunday, which is the bug"


def test_sunday_evening_is_still_sunday_at_the_pool(eastern):
    assert poolclock.pool_weekday(SUN_EVENING) == 6
    assert SUN_EVENING.weekday() == 0, "in UTC it looks like Monday"


def test_the_instant_is_never_changed_only_the_calendar_day(eastern):
    assert poolclock.pool_now(SAT_EVENING).timestamp() == SAT_EVENING.timestamp()


def test_a_naive_datetime_is_assumed_utc(eastern):
    assert poolclock.pool_today(datetime(2026, 8, 9, 0, 0)).isoformat() == "2026-08-08"


def test_an_implausible_offset_is_ignored(monkeypatch):
    monkeypatch.setenv("POOL_TZ_OFFSET", "99")
    assert poolclock.pool_now(SAT_EVENING).utcoffset() is not None   # falls back to the machine


def test_a_non_numeric_offset_is_ignored(monkeypatch):
    monkeypatch.setenv("POOL_TZ_OFFSET", "Eastern")
    assert poolclock.pool_now(SAT_EVENING) is not None


# ---- the digest, which the timezone bug fired a day early ----

def test_the_digest_no_longer_fires_on_saturday_evening(db, eastern):
    assert digest.is_due(SAT_EVENING, db) is False


def test_the_digest_fires_on_a_local_sunday(db, eastern):
    assert digest.is_due(SUN_MORNING, db) is True


def test_the_sunday_evening_run_can_still_send_it(db, eastern):
    """Previously this read as Monday in UTC, so a Mac asleep through the
    morning window dropped the week's digest entirely."""
    assert digest.is_due(SUN_EVENING, db) is True


def test_a_missed_sunday_is_caught_up_rather_than_dropped(db, eastern):
    """The digest is the pipeline's only heartbeat, so a silently skipped one is
    the worst failure it has. Asleep through both Sunday windows used to mean
    nothing was due again until the following Sunday."""
    from datetime import timedelta
    from db import mark_sent
    mark_sent(db, digest.DIGEST_KEY, SUN_MORNING.isoformat())

    # Next Sunday missed entirely; the run after it is a Tuesday.
    tuesday = SUN_MORNING + timedelta(days=9)
    assert poolclock.pool_weekday(tuesday) != digest.DIGEST_WEEKDAY
    assert digest.is_due(tuesday, db) is True


def test_it_is_not_sent_twice_in_one_week(db, eastern):
    from db import mark_sent
    mark_sent(db, digest.DIGEST_KEY, SUN_MORNING.isoformat())
    assert digest.is_due(SUN_EVENING, db) is False


def test_the_next_week_is_due_again(db, eastern):
    from datetime import timedelta
    from db import mark_sent
    mark_sent(db, digest.DIGEST_KEY, SUN_MORNING.isoformat())
    assert digest.is_due(SUN_MORNING + timedelta(days=7), db) is True


def test_a_clock_disagreement_beyond_dst_is_reported(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv("POOL_TZ_OFFSET", "-4")
    system = tmp_path / "system.json"
    system.write_text(json.dumps({"utc_offset_hours": -8}))
    monkeypatch.setattr(poolclock, "SYSTEM_JSON", system)
    assert "wrong by more than daylight saving" in poolclock.offset_disagreement(SUN_MORNING)


def test_an_hour_of_dst_difference_is_not_reported(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv("POOL_TZ_OFFSET", "-4")
    system = tmp_path / "system.json"
    system.write_text(json.dumps({"utc_offset_hours": -5}))
    monkeypatch.setattr(poolclock, "SYSTEM_JSON", system)
    assert poolclock.offset_disagreement(SUN_MORNING) is None
