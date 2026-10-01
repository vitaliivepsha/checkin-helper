"""Per-Telegram-user "pending" check-in attempts - a real check-in that
failed (Untappd rate-limited the request, or the MCP call errored out for
any other reason) gets saved here instead of just being lost, so the user
can retry it with one tap once Untappd is working again.

Personal, not shared (unlike checkin_queue.py) - same "keyed by user_id"
shape as wishlist_items.py, which this module otherwise mirrors exactly
(persistence, id/timestamp conventions). Deliberately no dedupe by beerId
(unlike wishlist_items.add_item): the same beer can legitimately fail twice
on different occasions with different ratings/venues, and the Mini App's
submit button is already disabled mid-request, so one submit can't double-
queue itself.

See webapp_server.py's handle_submit (saves here on failure) and
handle_pending_retry/handle_pending_list/handle_pending_remove (the Mini
App's own "Відкладені чекіни" screen) for how this gets used."""

import asyncio
import json
import os
import time
import uuid

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "pending_checkins.json")


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


async def add_item(user_id: int, data: dict) -> dict:
    """`data` carries everything a retry needs to re-attempt the exact same
    check-in (beerId, rating, shout, foursquareId, geolat, geolng,
    venueName, queueItemId), plus display-only fields the client already
    has loaded (beerName, brewery, style, abv, labelUrl) so the pending
    list can render a normal-looking row without an extra quota-costing
    lookup. failReason is "rate_limited" or "checkin_failed" (mirrors
    handle_submit's own error codes) - shown in the list so the user knows
    roughly what happened."""
    async with _lock:
        all_data = _load()
        items = all_data.get(str(user_id), [])
        item = {
            "id": uuid.uuid4().hex,
            "beerId": data.get("beerId"),
            "beerName": data.get("beerName"),
            "brewery": data.get("brewery"),
            "style": data.get("style"),
            "abv": data.get("abv"),
            "labelUrl": data.get("labelUrl"),
            "rating": data.get("rating"),
            "shout": data.get("shout"),
            "foursquareId": data.get("foursquareId"),
            "geolat": data.get("geolat"),
            "geolng": data.get("geolng"),
            "venueName": data.get("venueName"),
            "queueItemId": data.get("queueItemId"),
            "failReason": data.get("failReason"),
            "createdAt": int(time.time()),
        }
        items.append(item)
        all_data[str(user_id)] = items
        _save(all_data)
        return item


async def get_item(user_id: int, item_id: str) -> dict | None:
    async with _lock:
        items = _load().get(str(user_id), [])
        return next((it for it in items if it.get("id") == item_id), None)


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
