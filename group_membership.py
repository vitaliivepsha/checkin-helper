"""Per-Telegram-user "active group" pointer.

A group is just a Telegram group chat the bot can see - there's no separate
group-creation step. A user joins one by running /join_group *inside* that
chat (see bot.py's join_group_cmd), which records this mapping; running it
again in a different chat switches the active group (one at a time, no
membership list to manage). webapp_server.py reads this to scope the shared
queue (checkin_queue.py) to whichever group the caller last joined.

Same shape as user_tokens.py: flat dict keyed by str(user_id), persisted
under DATA_DIR.
"""

import asyncio
import json
import os
import time

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "group_membership.json")


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


async def get_active_group(user_id: int) -> dict | None:
    async with _lock:
        return _load().get(str(user_id))


async def set_active_group(user_id: int, chat_id: int, chat_title: str) -> None:
    """Overwrites any previous active group - re-running /join_group in a
    different chat is how switching groups works, there's no separate
    "leave" step."""
    async with _lock:
        data = _load()
        data[str(user_id)] = {"chatId": chat_id, "chatTitle": chat_title, "joinedAt": int(time.time())}
        _save(data)
