"""Beers the user marked "this is the wrong beer" on a shop page (the lens
userscript's ✕ button): per (brewery, shop title) text, the Untappd ids the
resolver must not return for it. beer_match.resolve_beer takes them as
`rejected_bids`, so the next lookup - the page itself, the daily crawl and its
recheck alike - picks the next best candidate or reports no match, instead of
repeating the wrong one until the matcher itself is fixed.

Keyed like lens_log (beer_match.scan_norm of brewery and title), so every shop
that prints the same text shares the rejection. Same module shape as the other
JSON stores (module-level _path/_lock, atomic tmp-file + os.replace writes)
with an in-memory mirror, since every lookup reads it."""

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
    _path = os.path.join(data_dir, "lens_rejections.json")
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


def rejected_bids(brewery: str, name: str) -> frozenset[int]:
    """The Untappd ids rejected for this shop text (empty when none). Synchronous
    on purpose: a dict lookup in the in-memory mirror, called per lookup item."""
    entry = _load().get(_key(brewery or "", name or ""))
    return frozenset(entry["bids"]) if entry else frozenset()


async def add(brewery: str, name: str, bid: int) -> bool:
    """Remembers `bid` as wrong for this shop text. False when the text is blank
    or the store is full of other products."""
    brewery, name = (brewery or "").strip(), (name or "").strip()
    if not name:
        return False
    async with _lock:
        data = _load()
        key = _key(brewery, name)
        entry = data.get(key)
        if entry is None:
            if len(data) >= MAX_ENTRIES:
                return False
            entry = data[key] = {"queryBrewery": brewery, "queryName": name, "bids": []}
        if bid not in entry["bids"]:
            entry["bids"].append(bid)
        entry["at"] = int(time.time())
        _save()
    return True


async def remove(brewery: str, name: str, bid: int) -> bool:
    """Undoes add(); True when the rejection existed."""
    async with _lock:
        data = _load()
        key = _key((brewery or "").strip(), (name or "").strip())
        entry = data.get(key)
        if not entry or bid not in entry["bids"]:
            return False
        entry["bids"].remove(bid)
        if not entry["bids"]:
            del data[key]
        _save()
    return True


async def all_entries() -> list[dict]:
    """Every rejection, newest first - for the lens report."""
    async with _lock:
        return sorted(_load().values(), key=lambda e: e.get("at", 0), reverse=True)
