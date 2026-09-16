"""Per-Telegram-user custom wishlist Google Sheet storage.

Untappd's own "Lists" feature has no API access at all through this app's
Untappd MCP connection (confirmed live - only the classic single Wishlist
is reachable, not custom named lists). Each user can instead publish their
own Google Sheet (File -> Share -> Publish to web -> CSV) and register its
URL via /wishlist_sheet, so the checkin webapp's own wishlist-priority
search (and, for the bot owner specifically, the Untappd Lens browser
extension - see webapp_server.py's WISHLIST_SHEET_CSV_URL fallback) can
also check against it, no Google credentials needed on this app's side -
just a plain HTTP GET on the published CSV URL. Persisted under DATA_DIR,
same pattern as untappd_tokens.json.
"""

import asyncio
import json
import os
import time

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "wishlist_sheets.json")


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


async def get_csv_url(user_id: int) -> str | None:
    async with _lock:
        entry = _load().get(str(user_id))
        return entry.get("csv_url") if entry else None


async def set_csv_url(user_id: int, csv_url: str) -> None:
    async with _lock:
        data = _load()
        data[str(user_id)] = {"csv_url": csv_url, "connected_at": int(time.time())}
        _save(data)


async def clear_csv_url(user_id: int) -> None:
    async with _lock:
        data = _load()
        data.pop(str(user_id), None)
        _save(data)
