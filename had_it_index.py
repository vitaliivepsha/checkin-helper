"""Per-user lifetime "have I had this beer" index, built by slowly paginating
through the full Untappd check-in history in the background (see
webapp_server.py's backfill loop), not by asking live per search.

Unlike checkin_queue.py/user_tokens.py, this file also keeps an in-memory
mirror of the loaded JSON (populated on first load, updated on every write)
instead of re-reading from disk per call: a heavy account can have tens of
thousands of entries here, and this file sits on the hot path of every
search's had-it annotation - re-parsing a multi-MB file per lookup would be
wasteful in a way the two small, low-frequency files never had to worry
about. Pretty-printing is skipped on write for the same reason.
"""

import asyncio
import json
import os
import time

_path: str | None = None
_lock = asyncio.Lock()
_mirror: dict | None = None


def init(data_dir: str) -> None:
    global _path, _mirror
    _path = os.path.join(data_dir, "had_it_index.json")
    _mirror = None  # force a fresh load from disk on next access


def _load() -> dict:
    global _mirror
    if _mirror is not None:
        return _mirror
    if not _path or not os.path.exists(_path):
        _mirror = {}
        return _mirror
    try:
        with open(_path, encoding="utf-8") as f:
            _mirror = json.load(f)
    except (json.JSONDecodeError, OSError):
        _mirror = {}
    return _mirror


def _save() -> None:
    tmp_path = _path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(_mirror, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, _path)


def _entry(user_id: int) -> dict:
    data = _load()
    key = str(user_id)
    if key not in data:
        data[key] = {
            "username": None,
            "beers": {},
            "next_offset": 0,
            "total_count": None,
            "fully_synced": False,
            "sync_started_at": time.time(),
            "last_synced_at": None,
            "last_fetch_at": None,
            "last_error": None,
        }
    return data[key]


async def lookup_had_it(user_id: int, beer_id) -> dict | None:
    """Tri-state answer: {"hadIt": True, "userRating": r} if known-had;
    {"hadIt": False} only if this user has completed at least one full pass
    and the beer is absent (a confident negative); None if unknown (not
    synced that far yet - caller should fall back to a live check)."""
    async with _lock:
        data = _load()
        entry = data.get(str(user_id))
        if entry is None:
            return None
        beer = entry["beers"].get(str(beer_id))
        if beer is not None:
            return {"hadIt": True, "userRating": beer.get("rating")}
        if entry.get("fully_synced"):
            return {"hadIt": False}
        return None


async def enrich_from_checkin(user_id: int, beer_id, style: str | None, brewery_name: str | None, country: str | None) -> None:
    """Fills in style/brewery/country for an ALREADY-known beer from a
    single checkin item's embedded beer/brewery data - called from
    webapp_server's venue backfill loop (get_user_checkins, paged by the
    monotonic checkin_id cursor `maxId`), never creates a bare entry (that
    stays record_page's job, keyed off the distinct-beer list's own
    `rating_score`/total_count bookkeeping, which this has none of).

    This exists because record_page's own walk (get_user_beers, paged by
    numeric offset) is vulnerable to a real, confirmed data-loss bug on an
    active account: that list is sorted by recency, so re-checking in an
    already-known beer bumps it back to the front and shifts every other
    entry's offset by one - a beer can land on an offset the walker already
    passed, and then simply never gets touched again on that pass (or the
    next, if it keeps getting re-drunk during the walk). get_user_checkins'
    checkin_id cursor has no such problem (strictly monotonic, immune to
    reordering), so this piggybacks on that already-running, already-paid
    walk to backfill what the offset walk keeps missing - zero extra quota.
    Only fills fields not already known, same non-destructive merge
    convention as record_page (never touches `rating`, which is a
    per-checkin value here, not the distinct-beer aggregate record_page
    reads from get_user_beers)."""
    async with _lock:
        data = _load()
        entry = data.get(str(user_id))
        if entry is None:
            return
        record = entry["beers"].get(str(beer_id))
        if record is None:
            return  # not yet seen by the main get_user_beers walk - not this function's job to create it
        changed = False
        if style and not record.get("style"):
            record["style"] = style
            changed = True
        if brewery_name and not record.get("brewery"):
            record["brewery"] = brewery_name
            changed = True
        if country and not record.get("country"):
            record["country"] = country
            changed = True
        if changed:
            _save()


async def record_page(user_id: int, username: str, items: list[dict], offset_after: int, total_count: int) -> None:
    """Merges one fetched page into the accumulated set. Never removes a
    previously-known beer, so a partial/incomplete resync can't regress a
    known-true answer back to unknown. Advances next_offset by the actual
    number of items received (not the requested page size) - a transient
    short page (observed to happen occasionally, unrelated to reaching the
    real end) just means slower progress next tick, not a data gap, since
    the next fetch resumes exactly where this one left off."""
    async with _lock:
        entry = _entry(user_id)
        entry["username"] = username
        for it in items:
            beer = it.get("beer") or {}
            brewery = it.get("brewery") or {}
            bid = beer.get("bid")
            if bid is None:
                continue
            record = entry["beers"].setdefault(str(bid), {})
            record["rating"] = it.get("rating_score")
            # Style/brewery/country - added later than `rating` (see
            # README.md) - captured for free from this same already-paid-for
            # get_user_beers page, never a dedicated per-beer lookup. Only
            # overwrite when actually present, so a page that's somehow
            # missing one of these (shouldn't happen) can't erase what an
            # earlier pass already recorded.
            style = beer.get("beer_style")
            if style:
                record["style"] = style
            brewery_name = brewery.get("brewery_name")
            if brewery_name:
                record["brewery"] = brewery_name
            country = brewery.get("country_name")
            if country:
                record["country"] = country
        entry["next_offset"] = offset_after
        entry["total_count"] = total_count
        entry["last_fetch_at"] = time.time()
        entry["last_error"] = None
        if len(items) == 0 or (total_count is not None and offset_after >= total_count):
            entry["fully_synced"] = True
            entry["last_synced_at"] = time.time()
        _save()


async def skip_page(user_id: int, offset_after: int, error: str) -> None:
    """Advances past a page that couldn't be parsed at all (observed cause:
    a specific beer/brewery name the upstream server itself serializes into
    genuinely broken JSON - no client-side parsing fix can repair that).
    Deliberately does NOT touch total_count/fully_synced/beers, unlike
    record_page - this is "we don't know what was here", not a real result
    page, so it must never be mistaken for reaching the true end. Without
    this, a single poisoned page would retry the identical offset forever
    and wedge the whole backfill for that user."""
    async with _lock:
        entry = _entry(user_id)
        entry["next_offset"] = offset_after
        entry["last_fetch_at"] = time.time()
        entry["last_error"] = error
        _save()


async def record_error(user_id: int, message: str) -> None:
    async with _lock:
        entry = _entry(user_id)
        entry["last_error"] = message
        _save()


async def record_checkin(user_id: int, beer_id, rating) -> None:
    """Immediate-insert hook for a real check-in made through this app -
    independent of next_offset/total_count bookkeeping, so it doesn't
    interfere with an in-progress backfill pass."""
    async with _lock:
        entry = _entry(user_id)
        entry["beers"][str(beer_id)] = {"rating": rating}
        _save()


async def seed_from_export(user_id: int, username: str, entries: list[tuple[int, float | None]]) -> int:
    """Bulk-seeds from an official Untappd data export (CSV/JSON uploaded via
    bot.py's /import_history) - bypasses the paginated backfill entirely.
    Merges into any existing beers (never removes a previously-known one,
    same convention as record_page) and marks fully_synced immediately,
    since a full account export is trusted to cover the whole history -
    next_turn will then skip this user until the normal resync cooldown.
    Returns how many (beerId, rating) entries were recorded."""
    async with _lock:
        entry = _entry(user_id)
        entry["username"] = username
        for bid, rating in entries:
            entry["beers"][str(bid)] = {"rating": rating}
        entry["fully_synced"] = True
        entry["last_synced_at"] = time.time()
        entry["last_fetch_at"] = time.time()
        entry["last_error"] = None
        _save()
        return len(entries)


async def get_all_beers(user_id: int) -> dict:
    """This user's full {beer_id: {rating, style, brewery, country}} map -
    used by badge_stats.py to compute style/country badge progress without
    re-deriving had_it_index's storage format itself. Empty dict if the user
    has no entry yet (never connected / backfill hasn't started)."""
    async with _lock:
        data = _load()
        entry = data.get(str(user_id))
        return dict(entry["beers"]) if entry else {}


_rotation_cursor = 0


async def next_turn(user_ids: list[int], resync_cooldown_seconds: float) -> tuple[int, int] | None:
    """Round-robin entry point for the backfill loop. Advances the rotation
    cursor on every call (even when nobody turns out to be eligible), so one
    problem user can never wedge the rotation and starve everyone else."""
    global _rotation_cursor
    if not user_ids:
        return None
    async with _lock:
        now = time.time()
        n = len(user_ids)
        for i in range(n):
            idx = (_rotation_cursor + i) % n
            user_id = user_ids[idx]
            entry = _entry(user_id)
            eligible = not entry.get("fully_synced")
            due_for_resync = False
            if not eligible:
                last_synced = entry.get("last_synced_at") or 0
                if now - last_synced > resync_cooldown_seconds:
                    eligible = True
                    due_for_resync = True
            if eligible:
                _rotation_cursor = (idx + 1) % n
                if due_for_resync:
                    entry["next_offset"] = 0
                    entry["fully_synced"] = False
                    _save()
                return user_id, entry["next_offset"]
        _rotation_cursor = (_rotation_cursor + 1) % n
        return None
