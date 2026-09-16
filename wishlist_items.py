"""Per-Telegram-user native wishlist items, live-editable from the check-in
webapp's own "Мій список" tab.

Distinct from wishlist_sheets.py (a registered Google Sheet CSV URL, read-only
- we only ever GET it, never write it) and from Untappd's own classic Wishlist
(reached via user_tokens/untappd_mcp, boosted in search as the existing ❤️
"вішліст" checkbox). This module is the *editable* side: items a user adds
via the webapp's search-and-tap flow, added/removed instantly, no external
service involved. webapp_server.py's /api/checkin/wishlist/* handlers merge
this with wishlist_sheets' CSV rows for display (union by beer id, native
wins on duplicates - see that module's own comments).

Persisted under DATA_DIR, same pattern as checkin_queue.json, but keyed by
user_id like wishlist_sheets.json since this list is personal, not shared.
"""

import asyncio
import json
import os
import time
import uuid

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "wishlist_items.json")


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


async def list_items(user_id: int) -> list[dict]:
    async with _lock:
        return _load().get(str(user_id), [])


async def add_item(user_id: int, beer: dict) -> tuple[dict, bool]:
    """Returns (item, added). added=False if this beerId was already listed."""
    async with _lock:
        data = _load()
        items = data.get(str(user_id), [])
        existing = next((it for it in items if it.get("beerId") == beer.get("beerId")), None)
        if existing:
            return existing, False
        item = {
            "id": uuid.uuid4().hex,
            "beerId": beer.get("beerId"),
            "name": beer.get("name"),
            "brewery": beer.get("brewery"),
            "style": beer.get("style"),
            "abv": beer.get("abv"),
            "labelUrl": beer.get("labelUrl"),
            "addedAt": int(time.time()),
        }
        items.append(item)
        data[str(user_id)] = items
        _save(data)
        return item, True


async def remove_item(user_id: int, item_id: str) -> bool:
    async with _lock:
        data = _load()
        items = data.get(str(user_id), [])
        new_items = [it for it in items if it.get("id") != item_id]
        if len(new_items) == len(items):
            return False
        data[str(user_id)] = new_items
        _save(data)
        return True
