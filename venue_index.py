"""Per-user lifetime "have I checked in at this venue" index, built by slowly
paginating through the full Untappd check-in history in the background (see
webapp_server.py's venue backfill loop) - the same approach as
had_it_index.py, but keyed by Foursquare venue id and paginated by
checkin_id (get_user_checkins has no offset/total_count, only a
newest-first feed paged backwards via maxId).

Same in-memory-mirror deviation as had_it_index.py, for the same reason
(hot path of every nearby-venue search, potentially large for a heavy
account).
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
    _path = os.path.join(data_dir, "venue_index.json")
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
            "venues": {},
            "next_max_id": None,
            "fully_synced": False,
            "sync_started_at": time.time(),
            "last_synced_at": None,
            "last_fetch_at": None,
            "last_error": None,
        }
    return data[key]


async def lookup_visited(user_id: int, foursquare_id: str) -> bool | None:
    """Tri-state, but callers should only ever act on a confirmed True:
    True if this venue is in the accumulated set; False only once this user
    has completed a full pass and it's absent (confident negative); None if
    unknown (not synced that far yet). Unlike had_it_index, a False/None
    mix-up here is low-stakes (worst case: an already-visited venue still
    shows up) - the one mistake that actually defeats "unique venue mode"
    is treating an unknown venue as visited, which this never does."""
    async with _lock:
        data = _load()
        entry = data.get(str(user_id))
        if entry is None:
            return None
        if foursquare_id in entry["venues"]:
            return True
        if entry.get("fully_synced"):
            return False
        return None


async def record_page(
    user_id: int, username: str, items: list[dict],
    next_max_id: int | None, page_len: int, requested_limit: int,
) -> None:
    """Merges one fetched check-in feed page. Skips check-ins with no venue
    at all. Never removes a previously-known venue. A short/empty page is
    treated as "reached the end" (same good-enough heuristic already
    accepted in had_it_index.py; the resync-after-cooldown in next_turn
    self-heals any pagination drift this misjudges).

    Each venue's Foursquare category names - needed for badge_stats.py's
    venue-badge progress - are captured for free from this same
    already-paid-for get_user_checkins page (same principle as had_it_index's
    style/brewery/country: never a dedicated per-venue lookup). A venue
    recorded before this field existed (or via record_checkin's immediate
    insert, which has no category data available) stores {} until the next
    resync happens to revisit it - an expected transient gap, not a bug."""
    async with _lock:
        entry = _entry(user_id)
        entry["username"] = username
        for it in items:
            venue = it.get("venue") or {}
            fsq = (venue.get("foursquare") or {}).get("foursquare_id")
            if not fsq:
                continue
            record = entry["venues"].get(fsq)
            if not isinstance(record, dict):
                record = {}
                entry["venues"][fsq] = record
            categories = venue.get("categories") or []

            if isinstance(categories, dict):
                categories = categories.get("items") or []
            elif isinstance(categories, str):
                categories = [categories]

            names = []
            for category in categories:
                if isinstance(category, dict):
                    name = category.get("category_name") or category.get("name")
                elif isinstance(category, str):
                    name = category
                else:
                    continue

                if isinstance(name, str):
                    name = name.strip()

                if name and name not in names:
                    names.append(name)
            if names:
                record["categories"] = names
        entry["next_max_id"] = next_max_id
        entry["last_fetch_at"] = time.time()
        entry["last_error"] = None
        if page_len == 0 or page_len < requested_limit:
            entry["fully_synced"] = True
            entry["last_synced_at"] = time.time()
        _save()


async def record_error(user_id: int, message: str) -> None:
    async with _lock:
        entry = _entry(user_id)
        entry["last_error"] = message
        _save()


async def record_checkin(user_id: int, foursquare_id: str | None) -> None:
    """Immediate-insert hook for a real check-in made through this app -
    independent of next_max_id/fully_synced bookkeeping. No-ops when the
    check-in had no venue attached. No category data is available on this
    path (the Mini App only posts foursquareId/name/lat/lng, not the full
    Foursquare category list) - only inserts a placeholder if the venue is
    genuinely new, never overwrites an existing record_page-populated entry
    (which would erase its already-known categories)."""
    if not foursquare_id:
        return
    async with _lock:
        entry = _entry(user_id)
        if foursquare_id not in entry["venues"]:
            entry["venues"][foursquare_id] = {}
        _save()


async def get_visited_venue_categories(user_id: int) -> list[list[str]]:
    """One category-name list per distinct visited venue (venues not yet
    touched by a record_page pass since categories started being captured
    appear as an empty list) - used by badge_stats.py to count DISTINCT
    VENUES qualifying for each venue badge, mirroring had_it_index's
    get_all_beers for style/country badges."""
    async with _lock:
        data = _load()
        entry = data.get(str(user_id))
        if not entry:
            return []
        return [(v.get("categories") or []) if isinstance(v, dict) else [] for v in entry["venues"].values()]


_rotation_cursor = 0


async def next_turn(user_ids: list[int], resync_cooldown_seconds: float) -> tuple[int, int | None] | None:
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
                    entry["next_max_id"] = None
                    entry["fully_synced"] = False
                    _save()
                return user_id, entry["next_max_id"]
        _rotation_cursor = (_rotation_cursor + 1) % n
        return None
