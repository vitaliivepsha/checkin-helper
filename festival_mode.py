"""Personal pause switch for quota-consuming background work - the
had_it/venue backfill loops and auto-toast (see webapp_server.py's three
loops, each checking is_enabled before spending any quota on a paused
user's turn). For an actual festival, that quota is better spent entirely
on live search/check-ins - see handle_autotoast_toggle's own docstring,
which already anticipated exactly this for auto-toast alone; this
generalizes it to all three at once, as one switch.

Per-user, not global: quota is per-token, and this fits alongside the
other three independent per-user toggles already on the settings screen
(auto-toast, festival-watch, comment-watch) - not owner-restricted like
auto-toast itself, since pausing backfill is useful to any connected user.

Same shape as comment_watch.py's own enabled-only slice: module-level
`_path`, `asyncio.Lock`, `init(data_dir)`, atomic tmp-file + os.replace()
writes.
"""

import asyncio
import json
import os

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "festival_mode.json")


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


async def is_enabled(user_id: int) -> bool:
    async with _lock:
        data = _load()
        entry = data.get(str(user_id)) or {}
        return bool(entry.get("enabled", False))


async def set_enabled(user_id: int, enabled: bool) -> None:
    async with _lock:
        data = _load()
        entry = data.setdefault(str(user_id), {})
        entry["enabled"] = enabled
        _save(data)
