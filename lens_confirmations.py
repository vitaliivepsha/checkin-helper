"""Shop products the user confirmed as "yes, this is that Untappd beer" (the Telegram review queue's
✅ and the lens's ✓ on a probable match): per (brewery, shop title) text, the Untappd id the matcher
takes as soon as search lists it (beer_match.resolve_beer(confirmed_bid=...)). The counterpart of
lens_rejections; keyed the same way (lens_log's key), one confirmed beer per text.

Same module shape as the other JSON stores (module-level _path/_lock, atomic tmp-file +
os.replace writes) with an in-memory mirror, since every lookup reads it."""

import asyncio
import json
import os
import time

import beer_match

_path: str | None = None
_lock = asyncio.Lock()
_mirror: dict | None = None
MAX_ENTRIES = 5000


def init(data_dir: str) -> None:
    global _path, _mirror
    _path = os.path.join(data_dir, "lens_confirmations.json")
    _mirror = None


def _load() -> dict:
    global _mirror
    if _mirror is not None:
        return _mirror
    _mirror = {}
    if _path and os.path.exists(_path):
        try:
            with open(_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _mirror = loaded
        except (json.JSONDecodeError, OSError):
            pass
    return _mirror


def _save() -> None:
    tmp = _path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_mirror, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, _path)


def _key(brewery: str, name: str) -> str:
    return f"{beer_match.scan_norm(brewery)}|{beer_match.scan_norm(name)}"


def confirmed_bid(brewery: str, name: str) -> int | None:
    """The confirmed Untappd id for this shop text, or None. Synchronous on purpose: a dict
    lookup in the in-memory mirror, called per lookup item."""
    entry = _load().get(_key(brewery or "", name or ""))
    return entry["bid"] if entry else None


async def add(brewery: str, name: str, bid: int) -> bool:
    """Remembers `bid` as the right beer for this shop text (replacing an earlier confirmation).
    False when the text is blank or the store is full of other products."""
    brewery, name = (brewery or "").strip(), (name or "").strip()
    if not name:
        return False
    async with _lock:
        data = _load()
        key = _key(brewery, name)
        if key not in data and len(data) >= MAX_ENTRIES:
            return False
        data[key] = {"queryBrewery": brewery, "queryName": name, "bid": bid, "at": int(time.time())}
        _save()
    return True


async def remove(brewery: str, name: str, bid: int) -> bool:
    """Undoes add(); True when that confirmation existed."""
    async with _lock:
        data = _load()
        key = _key((brewery or "").strip(), (name or "").strip())
        if key not in data or data[key]["bid"] != bid:
            return False
        del data[key]
        _save()
    return True


async def count() -> int:
    async with _lock:
        return len(_load())
