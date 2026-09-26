"""Per-user {badge_name: user_badge_id} - lets the badge detail screen link
to the REAL, personal earned-badge page (untappd.com/user/{username}/
badges/{user_badge_id} - confirmed live to be exactly what Untappd itself
generates when you share a badge, and on the untappd.com domain known to
hand off to the native app) instead of the generic badges.untappd.com
catalog page (which has no per-user meaning at all).

user_badge_id only ever appears in a check-in's own "badges" array, at the
moment that badge was earned - there's no dedicated "list my earned badges"
endpoint. Captured for free from the venue backfill loop's already-paid
get_user_checkins page (see webapp_server.py's _venue_backfill_loop), same
principle as had_it_index's style/brewery/country. badge_name in that data
carries a "(Level N)" suffix for repeating badges (e.g. "Iron Man (Level
92)") - stripped before storing, since our own catalog's canonical name
never has one and this is keyed to match it.

Same shape as festival_watch.py/comment_watch.py: module-level `_path`,
`asyncio.Lock`, `init(data_dir)`, atomic tmp-file + os.replace() writes.
"""

import asyncio
import json
import os
import re
import time

_path: str | None = None
_lock = asyncio.Lock()

# Separate file/lock from the badge data above - sync-cursor bookkeeping for
# the full-refresh loop (see next_sync_turn/record_sync_page below), kept
# apart so it never risks colliding with get_all()'s assumption that every
# key under a user entry in badge_index.json is a badge name.
_sync_path: str | None = None
_sync_lock = asyncio.Lock()
_sync_rotation_cursor = 0

_LEVEL_SUFFIX_RE = re.compile(r"\s*\(Level (\d+)\)\s*$")


def strip_level_suffix(badge_name: str) -> str:
    return _LEVEL_SUFFIX_RE.sub("", badge_name or "").strip()


def _parse(badge_name: str) -> tuple[str, int | None]:
    """(canonical_name, level) - level is None for a badge instance with no
    "(Level N)" suffix (single-tier, non-repeating badges)."""
    raw = badge_name or ""
    m = _LEVEL_SUFFIX_RE.search(raw)
    if m:
        return _LEVEL_SUFFIX_RE.sub("", raw).strip(), int(m.group(1))
    return raw.strip(), None


def init(data_dir: str) -> None:
    global _path, _sync_path
    _path = os.path.join(data_dir, "badge_index.json")
    _sync_path = os.path.join(data_dir, "badge_index_sync.json")


def _load() -> dict:
    if not _path or not os.path.exists(_path):
        return {}
    try:
        with open(_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    tmp_path = _path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, _path)


async def record(user_id: int, badge_name: str, user_badge_id) -> None:
    """Keeps the HIGHEST-level instance seen for this badge, not the first -
    each user_badge_id is a frozen snapshot of one specific award moment
    (confirmed live: an old instance's page shows the level it was AT, not
    the account's current progress), so "first seen" during a backward,
    restart-interrupted walk can easily land on a stale, long-superseded
    level rather than the most recent one. For a single-tier badge (no
    "(Level N)" suffix at all, level=None both times) the first instance
    found is kept, same as before - there's only ever one to compare."""
    name, level = _parse(badge_name)
    if not name or not user_badge_id:
        return
    async with _lock:
        data = _load()
        entry = data.setdefault(str(user_id), {})
        existing = entry.get(name)
        existing_level = existing.get("level") if isinstance(existing, dict) else None
        if existing is not None and not (level is not None and (existing_level is None or level > existing_level)):
            return
        entry[name] = {"userBadgeId": user_badge_id, "level": level}
        _save(data)


async def get_all(user_id: int) -> dict:
    """{badge_name: {"userBadgeId": ..., "level": int|None}} - the real
    level Untappd itself assigned (from record's "(Level N)" parse), used
    by webapp_server.py's handle_badges_get to correct badge_stats.py's own
    computed level when they disagree (see that function's own comment).
    Tolerates a bare int, the pre-level-tracking shape written by an
    earlier version of this module, if any lingers on disk."""
    async with _lock:
        data = _load()
        entry = data.get(str(user_id)) or {}
        return {
            name: (v if isinstance(v, dict) else {"userBadgeId": v, "level": None})
            for name, v in entry.items()
        }


def _load_sync() -> dict:
    if not _sync_path or not os.path.exists(_sync_path):
        return {}
    try:
        with open(_sync_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_sync(data: dict) -> None:
    tmp_path = _sync_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, _sync_path)


async def next_sync_turn(user_ids: list[int], full_resync_cooldown_seconds: float) -> tuple[int, int] | None:
    """Round-robin entry point for the untappd_direct-powered full-badge-
    list sync loop (webapp_server._badge_index_sync_loop) - (user_id,
    offset) for whoever needs the next page of GET /v4/user/badges/
    {username} fetched. Simpler than had_it_index/venue_index's own
    next_turn: no "quick recheck" tier, since a full walk here is cheap
    (a badge list is a small fraction the size of a beer/check-in history -
    a handful of pages even for a badge-heavy account) rather than
    something that needs interleaved freshness the way a 30k+-beer walk
    does. Advances the rotation cursor on every call (even when nobody
    turns out to be eligible), so one problem user can never wedge the
    rotation and starve everyone else - same reasoning as
    had_it_index.next_turn."""
    global _sync_rotation_cursor
    if not user_ids:
        return None
    async with _sync_lock:
        data = _load_sync()
        now = time.time()
        n = len(user_ids)
        for i in range(n):
            idx = (_sync_rotation_cursor + i) % n
            user_id = user_ids[idx]
            entry = data.setdefault(str(user_id), {"offset": 0, "fullySynced": False, "lastSyncedAt": None})
            if not entry.get("fullySynced"):
                _sync_rotation_cursor = (idx + 1) % n
                _save_sync(data)
                return user_id, entry.get("offset", 0)
            last_synced = entry.get("lastSyncedAt") or 0
            if now - last_synced > full_resync_cooldown_seconds:
                entry["offset"] = 0
                entry["fullySynced"] = False
                _sync_rotation_cursor = (idx + 1) % n
                _save_sync(data)
                return user_id, 0
        _sync_rotation_cursor = (_sync_rotation_cursor + 1) % n
        return None


async def record_sync_page(user_id: int, offset_after: int, got_count: int, page_size: int) -> None:
    """Advances the walk's offset; got_count < page_size is the real
    end-of-list signal (see get_user_badges' own docstring) - marks this
    pass complete and resets the resync clock."""
    async with _sync_lock:
        data = _load_sync()
        entry = data.setdefault(str(user_id), {"offset": 0, "fullySynced": False, "lastSyncedAt": None})
        entry["offset"] = offset_after
        if got_count < page_size:
            entry["fullySynced"] = True
            entry["lastSyncedAt"] = time.time()
        _save_sync(data)
