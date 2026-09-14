"""Personal "comment watch" - notifies the owner when someone else comments
on one of their own recent check-ins, with an inline "💬 Відповісти" button
that lets them reply straight from the bot (bot.py's handle_callback
"commentreply:" branch, posting via untappd_mcp.comment_checkin).

Unlike auto_toast/festival_watch, this can't ride along on the shared
get_my_friend_feed poll for free: that poll only ever looks at check-ins
newer than its own cursor, once - a comment typically lands well after the
check-in has already scrolled past that cursor, so it would never be
re-examined. This needs its own small, separate poll of the owner's own
recent check-ins (get_user_checkins on their own username) - see
webapp_server.py's _comment_watch_loop. A real but modest quota cost (one
call per owner per tick), nowhere near the 75-calls-per-lap the
pre-get_my_friend_feed auto-toast design used to cost.

Same shape as festival_watch.py: module-level `_path`, `asyncio.Lock`,
`init(data_dir)`, atomic tmp-file + os.replace() writes.
"""

import asyncio
import json
import os
import time

_path: str | None = None
_lock = asyncio.Lock()

# How many notified comment ids to remember per owner, to avoid re-notifying
# the same comment every tick. Bounded rather than unbounded - old entries
# naturally stop mattering once that check-in scrolls out of the polled
# "last N own check-ins" window anyway.
_MAX_SEEN_COMMENT_IDS = 500


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "comment_watch.json")


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
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _path)


def _owner_entry(data: dict, owner_id: int) -> dict:
    return data.setdefault(str(owner_id), {
        "enabled": False, "seenCommentIds": [], "bootstrapped": False,
        "lastPolledAt": None, "lastError": None,
    })


async def get_config(owner_id: int) -> dict:
    async with _lock:
        data = _load()
        entry = data.get(str(owner_id)) or {}
        return {
            "enabled": entry.get("enabled", False),
            "lastPolledAt": entry.get("lastPolledAt"),
            "lastError": entry.get("lastError"),
        }


async def set_enabled(owner_id: int, enabled: bool) -> None:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["enabled"] = enabled
        _save(data)


async def list_enabled_owners() -> list[int]:
    async with _lock:
        data = _load()
        return [int(k) for k, e in data.items() if e.get("enabled")]


async def record_tick(owner_id: int, comment_ids_seen: list[int], error: str | None = None) -> list[int]:
    """Given every comment id currently visible in this tick's polled
    check-ins, returns the subset never notified before (and marks them
    notified in the same locked operation, so a tick can't double-notify).

    The very first tick for a freshly-enabled owner never returns anything
    to notify - it only seeds the seen-set with whatever's already there
    (same "don't retroactively fire on existing history" rule auto_toast/
    festival_watch already use) - otherwise turning this on would dump a
    backlog of old comments as if they just happened."""
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        seen = set(entry.get("seenCommentIds", []))
        bootstrapped = entry.get("bootstrapped", False)
        new_ids = [c for c in comment_ids_seen if c not in seen] if bootstrapped else []

        updated = entry.setdefault("seenCommentIds", [])
        for cid in comment_ids_seen:
            if cid not in seen:
                updated.append(cid)
        if len(updated) > _MAX_SEEN_COMMENT_IDS:
            entry["seenCommentIds"] = updated[-_MAX_SEEN_COMMENT_IDS:]

        entry["bootstrapped"] = True
        entry["lastPolledAt"] = time.time()
        entry["lastError"] = error
        _save(data)
        return new_ids
