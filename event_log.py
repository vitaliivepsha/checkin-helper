"""Lightweight per-owner rolling log of notable events (an auto-toasted
check-in, a new comment on your own check-in, a festival novelty) - purely
so the Mini App's "🔔" screen can show recent activity at a glance,
independent of whatever Telegram already pushed as separate messages.
Bounded to the last _MAX_EVENTS_PER_OWNER entries per owner, not a full
history - this is a glance-back convenience, not an audit log.

Same shape as checkin_queue.py: module-level `_path`, `asyncio.Lock`,
`init(data_dir)`, atomic tmp-file + os.replace() writes.
"""

import asyncio
import json
import os
import time

_path: str | None = None
_lock = asyncio.Lock()
_MAX_EVENTS_PER_OWNER = 50


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "event_log.json")


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


async def add_event(
    owner_id: int, kind: str, text: str, *,
    beer_id: int | None = None, checkin_id: int | None = None, username: str | None = None,
) -> None:
    """kind: "toast" | "comment" | "novelty" - picks the icon the Mini App
    shows next to it. `text` is a short, already-formatted plain string (no
    HTML/Markdown - the Mini App inserts it as text, not markup).

    beer_id/checkin_id/username are optional interactive-affordance data for
    the "🔔" screen: beer_id powers a "🔗 open on Untappd" button (same as
    everywhere else in this app); for "comment" events, checkin_id+username
    (the commenter) power an inline "💬 Відповісти" reply, posted straight
    from that screen via untappd_mcp.comment_checkin - no need to switch to
    the bot chat."""
    async with _lock:
        data = _load()
        events = data.setdefault(str(owner_id), [])
        events.append({
            "kind": kind, "text": text, "at": time.time(),
            "beerId": beer_id, "checkinId": checkin_id, "username": username,
        })
        if len(events) > _MAX_EVENTS_PER_OWNER:
            del events[: len(events) - _MAX_EVENTS_PER_OWNER]
        _save(data)


async def get_events(owner_id: int, limit: int = 50) -> list[dict]:
    """Newest first."""
    async with _lock:
        data = _load()
        events = data.get(str(owner_id), [])
        return list(reversed(events[-limit:]))
