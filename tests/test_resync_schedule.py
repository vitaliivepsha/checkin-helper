from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import had_it_index
import resync_schedule
import venue_index

WARSAW = ZoneInfo("Europe/Warsaw")


def ts(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=WARSAW).timestamp()


@pytest.fixture(autouse=True)
def _tz(monkeypatch):
    monkeypatch.setenv("BOT_TIMEZONE", "Europe/Warsaw")
    monkeypatch.delenv("FULL_RESYNC_NOT_BEFORE", raising=False)


def test_not_due_before_the_first_allowed_month():
    # last sync in September, "now" in October: would be due by the calendar
    # rule, but the transition guard holds it back until November.
    assert resync_schedule.full_resync_due(ts(2026, 9, 17), ts(2026, 10, 5)) is False
    assert resync_schedule.full_resync_due(ts(2026, 9, 17), ts(2026, 10, 31, 23)) is False


def test_due_from_the_first_of_the_month_then_not_again_within_it():
    assert resync_schedule.full_resync_due(ts(2026, 9, 17), ts(2026, 11, 1, 0)) is True
    assert resync_schedule.full_resync_due(ts(2026, 10, 3), ts(2026, 11, 1, 0)) is True
    # a walk that finished on Nov 4 covers all of November
    assert resync_schedule.full_resync_due(ts(2026, 11, 4), ts(2026, 11, 20)) is False
    assert resync_schedule.full_resync_due(ts(2026, 11, 4), ts(2026, 12, 1, 0)) is True


def test_month_boundary_uses_the_configured_timezone():
    # 00:30 on Nov 1 in Warsaw is still Oct 31 22:30/23:30 UTC, so a rule
    # evaluated in UTC would call it October and not yet due.
    last = ts(2026, 10, 15)
    just_after_midnight_warsaw = datetime(2026, 11, 1, 0, 30, tzinfo=WARSAW).timestamp()
    assert resync_schedule.full_resync_due(last, just_after_midnight_warsaw) is True


def test_not_before_is_configurable(monkeypatch):
    monkeypatch.setenv("FULL_RESYNC_NOT_BEFORE", "2026-10-01")
    assert resync_schedule.full_resync_due(ts(2026, 9, 17), ts(2026, 10, 5)) is True


async def test_had_it_next_turn_starts_a_full_resync_only_when_due(tmp_path, monkeypatch):
    had_it_index.init(str(tmp_path))
    entry = had_it_index._entry(1)
    entry.update({"fully_synced": True, "next_offset": 500, "total_count": 500,
                  "last_synced_at": ts(2026, 9, 17), "last_quick_synced_at": ts(2026, 11, 1, 11)})
    monkeypatch.setattr("time.time", lambda: ts(2026, 10, 5))
    assert await had_it_index.next_turn([1], 86400) is None  # nothing due in October
    assert entry["fully_synced"] is True

    monkeypatch.setattr("time.time", lambda: ts(2026, 11, 1, 12))
    turn = await had_it_index.next_turn([1], 86400)
    assert turn == (1, 0, "full")
    assert entry["fully_synced"] is False


async def test_venue_next_turn_starts_a_full_resync_only_when_due(tmp_path, monkeypatch):
    venue_index.init(str(tmp_path))
    entry = venue_index._entry(1)
    entry.update({"fully_synced": True, "last_synced_at": ts(2026, 9, 22),
                  "last_quick_synced_at": ts(2026, 11, 1, 11)})
    monkeypatch.setattr("time.time", lambda: ts(2026, 10, 5))
    assert await venue_index.next_turn([1], 86400) is None
    monkeypatch.setattr("time.time", lambda: ts(2026, 11, 1, 12))
    turn = await venue_index.next_turn([1], 86400)
    assert turn == (1, None, "full")
    assert entry["fully_synced"] is False
