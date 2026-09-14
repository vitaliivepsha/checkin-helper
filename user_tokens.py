"""Per-Telegram-user Untappd MCP token storage.

Each friend connects their own Untappd account via /connect_untappd so their
check-ins land on their own account, not the bot owner's. Persisted under
DATA_DIR (the Fly volume) so it survives redeploys - same pattern as
checkins.json/pinned.json in bot.py.
"""

import asyncio
import json
import os
import time

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "untappd_tokens.json")


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


async def get_token(user_id: int) -> str | None:
    async with _lock:
        entry = _load().get(str(user_id))
        return entry.get("token") if entry else None


async def get_profile(user_id: int) -> dict | None:
    async with _lock:
        return _load().get(str(user_id))


async def list_user_ids() -> list[int]:
    async with _lock:
        return [int(uid) for uid in _load().keys()]


async def set_token(user_id: int, token: str, username: str, first_name: str, is_supporter: bool = False) -> None:
    async with _lock:
        data = _load()
        data[str(user_id)] = {
            "token": token,
            "username": username,
            "first_name": first_name,
            "connected_at": int(time.time()),
            "is_supporter": is_supporter,
        }
        _save(data)


async def set_is_supporter(user_id: int, is_supporter: bool) -> None:
    """Refreshes the cached Untappd Insiders (nee "Supporter") flag - see
    badge_stats.py's Super Style badges, which only a subscriber can
    actually earn. Captured for free from the venue backfill loop's
    already-paid get_user_checkins page (each check-in's own "user" object
    carries the checkin owner's current is_supporter status) rather than a
    dedicated get_my_profile call, so it drifts back into sync within a day
    of a real subscription change instead of staying frozen at whatever it
    was at /connect_untappd time."""
    async with _lock:
        data = _load()
        entry = data.get(str(user_id))
        if entry is None:
            return  # no registered profile to attach this to
        entry["is_supporter"] = is_supporter
        _save(data)


async def set_last_venue(user_id: int, venue: dict) -> None:
    """Remembers the venue picked for a check-in, so the Mini App can
    pre-fill it next time instead of asking again - useful since the venue
    is typically unchanged for an entire festival day."""
    async with _lock:
        data = _load()
        entry = data.get(str(user_id))
        if entry is None:
            return  # no registered profile to attach this to
        entry["last_venue"] = venue
        _save(data)
