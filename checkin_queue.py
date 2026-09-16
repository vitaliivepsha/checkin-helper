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


async def add_item(beer: dict, added_by: dict) -> tuple[dict, str]:
    """Returns (item, status). status is one of:
    - "added": a genuinely new shared item was created.
    - "already_active": this beerId is already in the shared queue *and*
      already visible in the caller's own view - a plain duplicate tap.
    - "revived_from_hidden": the caller had personally removed ("✕") this
      item before; it's now back in their view.
    - "revived_from_completed": the caller had already checked this item in
      *through the queue* before; it's now back in their view.

    Hiding or completing an item is personal, not a delete (see the module
    docstring) - the item stays in the shared queue for everyone else. If
    the same user deliberately adds that beer again, drop them from
    hiddenBy/completedBy so it actually reappears in *their* view too -
    without this, re-adding silently did nothing forever, since the item
    they were staring at "already existed" but was still filtered out of
    their own list by the very hidden/completed markers add_item never
    touched. The distinct revived_from_* statuses exist so the caller can
    tell the user *why* ("you'd already had this at the festival" vs "you'd
    removed it") instead of a single generic "already queued" message.

    Also drops the user from testForgottenBy (see reset_user) - a genuine,
    deliberate re-add means this is real festival activity again, so the
    "was in queue" badge (webapp_server.py's _annotate_queue_status) should
    resume tracking it instead of staying silenced from a pre-festival
    reset."""
    async with _lock:
        items = _load()
        existing = next((it for it in items if it.get("beerId") == beer.get("beerId")), None)
        if existing:
            user_id = added_by.get("userId")
            hidden = existing.get("hiddenBy") or []
            completed = existing.get("completedBy") or []
            forgotten = existing.get("testForgottenBy") or []
            if user_id in forgotten:
                existing["testForgottenBy"] = [uid for uid in forgotten if uid != user_id]
            if user_id in hidden:
                existing["hiddenBy"] = [uid for uid in hidden if uid != user_id]
                _save(items)
                return existing, "revived_from_hidden"
            if user_id in completed:
                existing["completedBy"] = [uid for uid in completed if uid != user_id]
                _save(items)
                return existing, "revived_from_completed"
            _save(items)
            return existing, "already_active"
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
            "testForgottenBy": [],
        }
        items.append(item)
        _save(items)
        return item, "added"


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


async def reset_user(user_id: int) -> int:
    """Settings-screen "forget my test check-ins" action - for someone who
    checked a few beers in *through the queue* before the real festival
    started (testing the app) and doesn't want add_item's
    revived_from_completed status telling them they "already had this at
    the festival" for something that never happened at the festival.

    Clears `user_id` from completedBy and moves them into hiddenBy instead
    of just dropping the marker outright - a plain clear would make the
    item reappear as active in their own queue view, which is exactly the
    opposite of what a pre-festival cleanup should do (confirmed by the
    user after an earlier version of this did just that: "я не хочу їх
    повертати" - they explicitly do NOT want anything to reappear, only
    the false "already had this at the festival" claim to go away).

    Items the user only ever hid (never completed) are left alone - hiding
    has nothing to do with the completed-at-the-festival claim, and
    silently un-hiding a real, deliberate personal removal isn't this
    button's job either.

    Also marks the item in testForgottenBy, so webapp_server.py's
    _annotate_queue_status stops showing the "was in queue" badge for it
    too - without this, a beer whose test check-in was just "forgotten"
    would still visibly claim it had been queued during the festival,
    which is exactly the false impression this button exists to erase.

    Per-user, like everything else here - doesn't touch anyone else's
    markers. Returns how many items were touched."""
    async with _lock:
        items = _load()
        changed = 0
        for item in items:
            completed = item.get("completedBy") or []
            if user_id not in completed:
                continue
            item["completedBy"] = [uid for uid in completed if uid != user_id]
            hidden = item.setdefault("hiddenBy", [])
            if user_id not in hidden:
                hidden.append(user_id)
            forgotten = item.setdefault("testForgottenBy", [])
            if user_id not in forgotten:
                forgotten.append(user_id)
            changed += 1
        if changed:
            _save(items)
        return changed
