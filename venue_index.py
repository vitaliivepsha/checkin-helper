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

import resync_schedule

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
            "quick_max_id": None,
            "quick_checkins_seen": 0,
            "last_quick_synced_at": None,
            # Cumulative check-ins paged through THIS full-walk cycle (reset
            # to 0 whenever a new one starts - see next_turn's resync
            # branch) - lets a caller report real "N of ~total_checkins"
            # progress instead of a raw, uninterpretable max_id cursor
            # (checkin_id is a platform-wide counter, not per-user, so its
            # own value/delta says nothing about % complete on its own).
            "full_checkins_seen": 0,
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


def _merge_venues(entry: dict, items: list[dict]) -> None:
    """Shared by record_page (full walk) and record_quick_page (daily
    top-N recheck). Skips check-ins with no venue at all. Never removes a
    previously-known venue.

    Each venue's Foursquare category names - needed for badge_stats.py's
    venue-badge progress - are captured for free from this same
    already-paid-for get_user_checkins page (same principle as had_it_index's
    style/brewery/country: never a dedicated per-venue lookup). A venue
    recorded before this field existed (or via record_checkin's immediate
    insert, which has no category data available) stores {} until a resync
    happens to revisit it - an expected transient gap, not a bug.

    `name` (the venue's own display name, e.g. "Gdańsk Główny Ferry
    Terminal") is captured the same way, purely for investigating category-
    matching questions by hand later (see webapp_server.py's own
    venue_name field on a checkin's own "venue" dict for the matching
    convention) - nothing currently reads it back programmatically, so a
    venue recorded before this existed just has no name until a resync
    revisits it, same as categories above."""
    for it in items:
        venue = it.get("venue") or {}
        fsq = (venue.get("foursquare") or {}).get("foursquare_id")
        if not fsq:
            continue
        record = entry["venues"].get(fsq)
        if not isinstance(record, dict):
            record = {}
            entry["venues"][fsq] = record
        venue_name = venue.get("venue_name")
        if venue_name:
            record["name"] = venue_name
        # country/state - needed for badge_stats.py's "Brew Traveler"
        # (distinct US states/Canadian provinces/Mexican states OR distinct
        # non-US/CA/MX countries, pooled into one count - see that badge's
        # own real "How to Earn It" text). Both come from the checkin's own
        # venue.location, already-paid-for, same free-ride principle as
        # categories/name above. venue_country/venue_state are apparently
        # localized to wherever the venue itself is (confirmed live: a
        # Polish venue returns "Polska", not "Poland") - badge_stats.py's
        # own region-matching accounts for that, this module just stores
        # whatever string Untappd sent verbatim.
        location = venue.get("location") or {}
        country = location.get("venue_country")
        if country:
            record["country"] = country
        state = location.get("venue_state")
        if state:
            record["state"] = state
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


async def record_page(
    user_id: int, username: str, items: list[dict],
    next_max_id: int | None, page_len: int, requested_limit: int,
) -> None:
    """Merges one fetched check-in feed page (full-walk state - see
    next_turn). A short/empty page is treated as "reached the end" (same
    good-enough heuristic already accepted in had_it_index.py; the
    resync-after-cooldown in next_turn self-heals any pagination drift
    this misjudges)."""
    async with _lock:
        entry = _entry(user_id)
        entry["username"] = username
        _merge_venues(entry, items)
        entry["next_max_id"] = next_max_id
        entry["full_checkins_seen"] = entry.get("full_checkins_seen", 0) + page_len
        entry["last_fetch_at"] = time.time()
        entry["last_error"] = None
        if page_len == 0 or page_len < requested_limit:
            entry["fully_synced"] = True
            entry["last_synced_at"] = time.time()
        _save()


async def record_quick_page(
    user_id: int, username: str, items: list[dict],
    next_max_id: int | None, page_len: int, quick_limit: int,
) -> None:
    """Merges one page of the cheap daily top-N recheck (see next_turn's
    "quick" branch) - same merge as record_page, but tracks its own
    quick_checkins_seen/quick_max_id cursor and never touches
    next_max_id/fully_synced, which stay reserved for the (now monthly)
    full walk. A short page means this account's whole check-in history is
    smaller than quick_limit - already fully covered, same as reaching it."""
    async with _lock:
        entry = _entry(user_id)
        entry["username"] = username
        _merge_venues(entry, items)
        entry["last_fetch_at"] = time.time()
        entry["last_error"] = None
        seen = entry.get("quick_checkins_seen", 0) + page_len
        if page_len == 0 or seen >= quick_limit:
            entry["quick_max_id"] = None
            entry["quick_checkins_seen"] = 0
            entry["last_quick_synced_at"] = time.time()
        else:
            entry["quick_max_id"] = next_max_id
            entry["quick_checkins_seen"] = seen
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


async def get_visited_regions(user_id: int) -> list[tuple[str | None, str | None]]:
    """One (country, state) pair per distinct visited venue - used by
    badge_stats.py's "Brew Traveler" region count. A venue with no captured
    location yet contributes (None, None), which that counting logic
    already ignores, same as an empty categories list does for the other
    venue badges."""
    async with _lock:
        data = _load()
        entry = data.get(str(user_id))
        if not entry:
            return []
        return [
            ((v.get("country"), v.get("state")) if isinstance(v, dict) else (None, None))
            for v in entry["venues"].values()
        ]


async def is_fully_synced(user_id: int) -> bool:
    """Whether this user's full walk has ever completed at least once - see
    had_it_index.is_fully_synced's own docstring for why webapp_server.py's
    handle_badges_get needs this (same reasoning, for venue badges)."""
    async with _lock:
        data = _load()
        entry = data.get(str(user_id))
        return bool(entry and entry.get("fully_synced"))


_rotation_cursor = 0


async def next_turn(
    user_ids: list[int], quick_recheck_cooldown_seconds: float
) -> tuple[int, int | None, str] | None:
    """Round-robin entry point for the backfill loop. Advances the rotation
    cursor on every call (even when nobody turns out to be eligible), so one
    problem user can never wedge the rotation and starve everyone else.

    Same 3-tier priority as had_it_index.next_turn (see its own docstring
    for the full reasoning), adapted to this file's cursor-based (max_id,
    newest-first) pagination: (1) an initial/in-progress full walk always
    continues; (2) once fully synced, a full walk restarts after
    once per calendar month (resync_schedule); (3) otherwise a cheap "quick" recheck of
    the most recent check-ins runs (continuing mid-cycle, or starting a
    fresh one from max_id=None after quick_recheck_cooldown_seconds).

    Tier 1 has one carve-out: once this user has completed a full walk at
    least once before (last_synced_at is set - true for a resync, false for
    the very first walk), an overdue quick recheck still gets to interleave
    in. Without this, a resync monopolizes every turn until it fully
    finishes - confirmed to take well over a week on a large account - which
    would silently stop badge_index.py's badge levels (and this module's own
    "unique venue" data) from picking up anything new for that whole
    stretch, every single resync cycle. A first-ever walk skips this: its
    own newest-first pages already cover the same ground a quick recheck
    would, so interleaving would just spend quota twice on the same
    check-ins for zero freshness benefit."""
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

            last_synced = entry.get("last_synced_at") or 0
            has_baseline = last_synced > 0
            quick_checkins_seen = entry.get("quick_checkins_seen", 0)
            last_quick_synced = entry.get("last_quick_synced_at") or 0
            quick_due = quick_checkins_seen > 0 or now - last_quick_synced > quick_recheck_cooldown_seconds

            if not entry.get("fully_synced"):
                if has_baseline and quick_due:
                    _rotation_cursor = (idx + 1) % n
                    return user_id, entry.get("quick_max_id"), "quick"
                _rotation_cursor = (idx + 1) % n
                return user_id, entry["next_max_id"], "full"

            if resync_schedule.full_resync_due(last_synced, now):
                entry["next_max_id"] = None
                entry["fully_synced"] = False
                entry["full_checkins_seen"] = 0
                _save()
                _rotation_cursor = (idx + 1) % n
                return user_id, entry["next_max_id"], "full"

            if quick_due:
                _rotation_cursor = (idx + 1) % n
                return user_id, entry.get("quick_max_id"), "quick"
        _rotation_cursor = (_rotation_cursor + 1) % n
        return None
