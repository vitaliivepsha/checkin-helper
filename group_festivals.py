"""Per-Telegram-group "bound festival" pointer.

Lets different group chats run different festivals' beer lists at the same
time, instead of the single global default in festivals.json/active_festival.json
(see bot.py's reload_beer_db/resolve_active_festival). A group binds one by
running /set_festival <key> *inside* that chat (see bot.py's set_festival_cmd),
which records this mapping; running it again with a different key switches
the group's festival (one at a time, no history to manage). A group that
never runs /set_festival keeps using the global default.

Same shape as group_membership.py, but keyed by str(chat_id) instead of
str(user_id) - a group's bound festival, not a user's active group.
"""

import asyncio
import json
import os

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "group_festivals.json")


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


async def get_group_festival(chat_id: int) -> str | None:
    async with _lock:
        entry = _load().get(str(chat_id))
        return entry.get("key") if entry else None


async def set_group_festival(chat_id: int, key: str) -> None:
    """Overwrites any previous binding - re-running /set_festival in the
    same chat is how switching a group's festival works, there's no
    separate "unbind" step."""
    async with _lock:
        data = _load()
        data[str(chat_id)] = {"key": key}
        _save(data)
