"""Shared, server-backed "queue" of beers the whole group should try.

Replaces the earlier per-device localStorage "flight" - anyone in the group
can add a beer someone just brought back, and everyone's phone sees it.

Each item tracks `completedBy` - the Telegram user ids who have checked it in
*through this queue*. This is deliberately independent of Untappd's own
lifetime "have I ever had this beer" - someone may have tried a beer years
ago and still want to queue it up and check in again today. Untappd's
lifetime hadIt is only used as an informational badge (see webapp_server.py's
had-it annotation), never to decide whether an item drops off your queue view.

Each item also tracks `hiddenBy` - Telegram user ids who tapped "remove"
without actually checking the beer in. This is a *personal* dismissal, not a
delete: the item stays in the shared queue for everyone who hasn't hidden or
completed it. An earlier version had "remove" delete the item globally for
every viewer - if someone else hadn't gotten to it yet, it vanished from
their queue too, which is exactly the confusion this field exists to avoid.
`remove_item` (a true, all-viewers delete) still exists for internal/admin
use but is no longer wired to the app's "✕" button.
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
    _path = os.path.join(data_dir, "checkin_queue.json")


def _load() -> list[dict]:
    if not _path or not os.path.exists(_path):
        return []
    try:
        with open(_path, encoding="utf-8") as f:
            return json.load(f).get("items", [])
    except (json.JSONDecodeError, OSError):
        return []


def _save(items: list[dict]) -> None:
    tmp_path = _path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _path)


async def list_items() -> list[dict]:
    async with _lock:
        return _load()


async def add_item(beer: dict, added_by: dict) -> tuple[dict, bool]:
    """Returns (item, added). added=False if this beerId was already queued."""
    async with _lock:
        items = _load()
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
            "sessions": beer.get("sessions") or [],
            "addedBy": added_by,
            "addedAt": int(time.time()),
            "completedBy": [],
            "hiddenBy": [],
        }
        items.append(item)
        _save(items)
        return item, True


async def remove_item(item_id: str) -> bool:
    async with _lock:
        items = _load()
        new_items = [it for it in items if it.get("id") != item_id]
        if len(new_items) == len(items):
            return False
        _save(new_items)
        return True


async def mark_completed(item_id: str, user_id: int) -> bool:
    """Records that `user_id` checked this queue item in - it drops off
    their own view from then on, but stays for everyone else."""
    async with _lock:
        items = _load()
        item = next((it for it in items if it.get("id") == item_id), None)
        if not item:
            return False
        completed = item.setdefault("completedBy", [])
        if user_id not in completed:
            completed.append(user_id)
            _save(items)
        return True


async def hide_item(item_id: str, user_id: int) -> bool:
    """Personal dismissal: `user_id` no longer sees this item, but it stays
    in the shared queue for everyone else. This is what the app's "✕"
    button calls - not remove_item (see module docstring for why)."""
    async with _lock:
        items = _load()
        item = next((it for it in items if it.get("id") == item_id), None)
        if not item:
            return False
        hidden = item.setdefault("hiddenBy", [])
        if user_id not in hidden:
            hidden.append(user_id)
            _save(items)
        return True


async def hide_all(user_id: int) -> int:
    """Personal "clear all" - the app's queue-screen button that empties
    *your own* view of the queue in one tap. Same mechanism as hide_item
    (adds to each item's hiddenBy), applied to every current item at once -
    still doesn't touch the shared queue for anyone else, and a switch to a
    new festival's beer list doesn't auto-clear this for anyone (a stale
    queue item just becomes irrelevant, not deleted - this button is the
    manual way to tidy that up per-person). Returns how many items were
    newly hidden."""
    async with _lock:
        items = _load()
        newly_hidden = 0
        for item in items:
            hidden = item.setdefault("hiddenBy", [])
            if user_id not in hidden:
                hidden.append(user_id)
                newly_hidden += 1
        if newly_hidden:
            _save(items)
        return newly_hidden
