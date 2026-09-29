"""Per-Telegram-user personal festival override.

Sits ABOVE group_festivals.py in the resolution order (see bot.py's
resolve_festival_key / webapp_server.py's _resolve_festival_key): a user
who sets their own festival here sees it everywhere, regardless of which
group (if any) they're currently active in. Lets someone attending a
festival without a dedicated group chat - or who just wants to look at a
different festival's list than their group's - pick one for themselves,
via /set_festival in a private chat or the Mini App's own "Мій фестиваль"
screen (handle_my_festival_get/handle_my_festival_set).

Same shape as group_membership.py/group_festivals.py: flat dict keyed by
str(user_id), persisted under DATA_DIR. Unlike group_festivals.py, a user
CAN explicitly clear this back to "no personal override" (falls through to
their group's binding, or the global default) - there's no real-world
equivalent of "leaving" a group binding, but "stop overriding for myself"
is a meaningful, common action here.
"""

import asyncio
import json
import os

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "user_festivals.json")


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


async def get_user_festival(user_id: int) -> str | None:
    async with _lock:
        entry = _load().get(str(user_id))
        return entry.get("key") if entry else None


async def set_user_festival(user_id: int, key: str) -> None:
    """Overwrites any previous personal override."""
    async with _lock:
        data = _load()
        data[str(user_id)] = {"key": key}
        _save(data)


async def clear_user_festival(user_id: int) -> None:
    """Removes the override entirely - the user falls back to their
    group's binding (if any), then the global default."""
    async with _lock:
        data = _load()
        if str(user_id) in data:
            del data[str(user_id)]
            _save(data)
