"""When a full had-it / visited-venue resync is due: once per calendar month,
from the 1st (bot timezone), instead of "30 days after the last one" - so it
lands at a predictable time rather than drifting differently per user.

FULL_RESYNC_NOT_BEFORE (ISO date, default 2026-11-01) holds the first
calendar-month resync back until then: when this rule replaced the 30-day
cooldown, every user's last full sync was already before the 1st of that
month, and they would all have started a ~600-request full walk at once.
After that date it has no effect and can be removed.
"""

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo


def _tz() -> ZoneInfo:
    name = os.getenv("BOT_TIMEZONE") or os.getenv("TZ") or "Europe/Warsaw"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _not_before() -> float:
    d = date.fromisoformat(os.getenv("FULL_RESYNC_NOT_BEFORE", "2026-11-01"))
    return datetime(d.year, d.month, d.day, tzinfo=_tz()).timestamp()


def month_start(now: float) -> float:
    dt = datetime.fromtimestamp(now, _tz())
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def full_resync_due(last_synced: float, now: float) -> bool:
    """True when the last completed full walk is from before the current
    calendar month began (and FULL_RESYNC_NOT_BEFORE has passed)."""
    return now >= _not_before() and last_synced < month_start(now)
