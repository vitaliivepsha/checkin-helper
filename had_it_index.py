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
            "quick_offset": 0,
            "last_quick_synced_at": None,
            "full_walk_seen_bids": [],
            "full_walk_had_gap": False,
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


async def enrich_from_checkin(
    user_id: int, beer_id, style: str | None, brewery_name: str | None, country: str | None,
    name: str | None = None, state: str | None = None,
    abv: float | None = None, ibu: float | None = None, brewery_type: str | None = None,
) -> None:
    """Fills in style/brewery/country/name for an ALREADY-known beer from a
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
        if name and not record.get("name"):
            record["name"] = name
            changed = True
        if style and not record.get("style"):
            record["style"] = style
            changed = True
        if brewery_name and not record.get("brewery"):
            record["brewery"] = brewery_name
            changed = True
        if country and not record.get("country"):
            record["country"] = country
            changed = True
        if state and not record.get("state"):
            record["state"] = state
            changed = True
        if brewery_type and not record.get("breweryType"):
            record["breweryType"] = brewery_type
            changed = True
        if abv is not None and record.get("abv") is None:
            record["abv"] = abv
            changed = True
        if ibu is not None and record.get("ibu") is None:
            record["ibu"] = ibu
            changed = True
        if changed:
            _save()


def _merge_beers(entry: dict, items: list[dict]) -> None:
    """Shared by record_page (full walk) and record_quick_page (daily
    top-N recheck) - additive only, never removes a previously-known beer,
    so a partial/incomplete pass can't regress a known-true answer back to
    unknown."""
    for it in items:
        beer = it.get("beer") or {}
        brewery = it.get("brewery") or {}
        bid = beer.get("bid")
        if bid is None:
            continue
        record = entry["beers"].setdefault(str(bid), {})
        record["rating"] = it.get("rating_score")
        # Name/style/brewery/country - added later than `rating` (see
        # README.md) - captured for free from this same already-paid-for
        # get_user_beers page, never a dedicated per-beer lookup. Only
        # overwrite when actually present, so a page that's somehow
        # missing one of these (shouldn't happen) can't erase what an
        # earlier pass already recorded. `name` exists specifically for
        # badge_stats.py's "matchName" badges (e.g. Winter Wonderland,
        # whose own real Untappd rule counts a beer whose NAME - not
        # formal style - contains a themed keyword) - every other badge
        # still only ever looks at `style`.
        name = beer.get("beer_name")
        if name:
            record["name"] = name
        style = beer.get("beer_style")
        if style:
            record["style"] = style
        brewery_name = brewery.get("brewery_name")
        if brewery_name:
            record["brewery"] = brewery_name
        brewery_type = brewery.get("brewery_type")
        if brewery_type:
            record["breweryType"] = brewery_type
        country = brewery.get("country_name")
        if country:
            record["country"] = country
        # brewery_state - for badge_stats.py's "Beer of the World" (distinct
        # state-or-province+country regions, confirmed live via the badge's
        # own real "Your Regions List" - every country gets sub-national
        # granularity there, not just US/CA/MX like Brew Traveler's own
        # region rule - see _beer_region_key).
        state = (brewery.get("location") or {}).get("brewery_state")
        if state:
            record["state"] = state
        # abv/ibu - for badge_stats.py's range-matched badges (Riding
        # Steady, Sky's the Limit, Hopped Down, Hopped Up, Middle of the
        # Road - see compute_range_progress). IBU is frequently absent on
        # Untappd's own data (not every beer has it recorded) - a missing
        # value here just never matches an IBU-range badge, same as every
        # other "not yet known" field in this file.
        abv = beer.get("beer_abv")
        if abv is not None:
            record["abv"] = abv
        ibu = beer.get("beer_ibu")
        if ibu is not None:
            record["ibu"] = ibu


async def record_page(user_id: int, username: str, items: list[dict], offset_after: int, total_count: int) -> None:
    """Merges one fetched page into the accumulated set (full-walk state -
    see next_turn). Advances next_offset by the actual number of items
    received (not the requested page size) - a transient short page
    (observed to happen occasionally, unrelated to reaching the real end)
    just means slower progress next tick, not a data gap, since the next
    fetch resumes exactly where this one left off.

    Tracks every bid seen this pass in full_walk_seen_bids so that, once
    the pass reaches the end, any beer in `beers` NOT seen this time can be
    dropped - Untappd occasionally merges one beer's bid into another's, and
    since every merge everywhere else is additive-only (never removes), that
    kind of stale bid would otherwise linger forever. Only does this
    cleanup when full_walk_seen_bids is a list (not None) AND no page was
    skipped this pass (full_walk_had_gap) - a pass with a skipped page, or
    one that started before this tracking existed (an already-in-flight
    walk from before this feature shipped - full_walk_seen_bids is simply
    absent for those), hasn't actually seen everything, so treating its
    gaps as "gone" would wrongly delete real data. Also skipped when
    total_count is falsy - never trust a zero/missing total enough to wipe
    an existing beers dict."""
    async with _lock:
        entry = _entry(user_id)
        entry["username"] = username
        _merge_beers(entry, items)
        seen = entry.get("full_walk_seen_bids")
        if seen is not None:
            seen_set = set(seen)
            for it in items:
                bid = (it.get("beer") or {}).get("bid")
                if bid is not None:
                    seen_set.add(str(bid))
            entry["full_walk_seen_bids"] = list(seen_set)
        entry["next_offset"] = offset_after
        entry["total_count"] = total_count
        entry["last_fetch_at"] = time.time()
        entry["last_error"] = None
        if len(items) == 0 or (total_count is not None and offset_after >= total_count):
            entry["fully_synced"] = True
            entry["last_synced_at"] = time.time()
            if seen is not None and total_count and not entry.get("full_walk_had_gap"):
                stale_bids = set(entry["beers"].keys()) - set(entry["full_walk_seen_bids"])
                for bid in stale_bids:
                    del entry["beers"][bid]
            entry["full_walk_seen_bids"] = []
            entry["full_walk_had_gap"] = False
        _save()


async def record_quick_page(user_id: int, username: str, items: list[dict], offset_after: int, quick_limit: int) -> None:
    """Merges one page of the cheap daily top-N recheck (see next_turn's
    "quick" branch) - same merge as record_page, but tracks its own
    quick_offset cursor and never touches next_offset/total_count/
    fully_synced, which stay reserved for the (now monthly) full walk.
    A short page here means this account's whole history is smaller than
    quick_limit - already fully covered, same as reaching quick_limit."""
    async with _lock:
        entry = _entry(user_id)
        entry["username"] = username
        _merge_beers(entry, items)
        entry["last_fetch_at"] = time.time()
        entry["last_error"] = None
        if len(items) == 0 or offset_after >= quick_limit:
            entry["quick_offset"] = 0
            entry["last_quick_synced_at"] = time.time()
        else:
            entry["quick_offset"] = offset_after
        _save()


async def skip_page(user_id: int, offset_after: int, error: str, kind: str = "full") -> None:
    """Advances past a page that couldn't be parsed at all (observed cause:
    a specific beer/brewery name the upstream server itself serializes into
    genuinely broken JSON - no client-side parsing fix can repair that).
    Deliberately does NOT touch total_count/fully_synced/beers, unlike
    record_page/record_quick_page - this is "we don't know what was here",
    not a real result page, so it must never be mistaken for reaching the
    true end. Without this, a single poisoned page would retry the
    identical offset forever and wedge that pass for that user. `kind`
    picks which cursor ("full"'s next_offset or "quick"'s quick_offset) to
    advance, matching whichever pass hit the bad page. A "full" skip also
    flags full_walk_had_gap - see record_page's own comment on why that
    flag blocks this pass's stale-bid cleanup."""
    async with _lock:
        entry = _entry(user_id)
        if kind == "quick":
            entry["quick_offset"] = offset_after
        else:
            entry["next_offset"] = offset_after
            entry["full_walk_had_gap"] = True
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


async def is_fully_synced(user_id: int) -> bool:
    """Whether this user's full walk has ever completed at least once - used
    by webapp_server.py's handle_badges_get to decide whether a style/
    country badge's own compute_progress count is trustworthy enough to
    justify showing a KNOWN-stale personal badge link (see badge_index.py) -
    a full walk still in progress could plausibly be undercounting, so that
    fallback is only offered once there's nothing more passive discovery
    could still turn up on its own."""
    async with _lock:
        data = _load()
        entry = data.get(str(user_id))
        return bool(entry and entry.get("fully_synced"))


_rotation_cursor = 0


async def next_turn(
    user_ids: list[int], full_resync_cooldown_seconds: float, quick_recheck_cooldown_seconds: float
) -> tuple[int, int, str] | None:
    """Round-robin entry point for the backfill loop. Advances the rotation
    cursor on every call (even when nobody turns out to be eligible), so one
    problem user can never wedge the rotation and starve everyone else.

    Per user, in priority order: (1) an initial/in-progress full walk always
    continues; (2) once fully synced, a full walk restarts (offset reset to
    0) after full_resync_cooldown_seconds - the only way to catch anything a
    quick recheck can't (a skipped/malformed page, data backfilled late, or
    a bid Untappd itself merged away - see record_page's stale-bid cleanup);
    (3) otherwise a cheap top-N "quick" recheck runs (continuing mid-cycle,
    or starting a new one after quick_recheck_cooldown_seconds) - catches
    new check-ins/rating edits made in the real Untappd app, which always
    land at the front of get_user_beers' recency-sorted list (a check-in
    made *through this bot* is already recorded instantly via
    record_checkin and needs neither pass).

    Tier 1 has one carve-out: once this user has completed a full walk at
    least once before (last_synced_at is set - true for a resync, false for
    the very first walk), an overdue quick recheck still gets to interleave
    in rather than being locked out for the resync's entire, potentially
    week-plus duration on a large account - confirmed to otherwise starve
    had-it freshness (and, via the same pattern in venue_index.py,
    badge_index.py's badge levels) for that whole stretch, every single
    resync cycle. Doesn't touch next_offset or the full walk's own
    full_walk_seen_bids/full_walk_had_gap tracking - record_quick_page never
    writes those fields, so the full walk's own pagination and stale-bid
    detection stay exactly as gapless as before, just spread across more
    wall-clock time. A first-ever walk skips this carve-out: its own
    newest-first pages already cover the same ground a quick recheck would,
    so interleaving would just spend quota twice for zero freshness
    benefit."""
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
            quick_offset = entry.get("quick_offset", 0)
            last_quick_synced = entry.get("last_quick_synced_at") or 0
            quick_due = quick_offset > 0 or now - last_quick_synced > quick_recheck_cooldown_seconds

            if not entry.get("fully_synced"):
                if has_baseline and quick_due:
                    _rotation_cursor = (idx + 1) % n
                    return user_id, quick_offset, "quick"
                _rotation_cursor = (idx + 1) % n
                return user_id, entry["next_offset"], "full"

            if now - last_synced > full_resync_cooldown_seconds:
                entry["next_offset"] = 0
                entry["fully_synced"] = False
                # Fresh pass starting now - see record_page's own comment
                # on why stale-bid cleanup only ever applies to a pass that
                # tracked every bid it saw from offset 0 onward.
                entry["full_walk_seen_bids"] = []
                entry["full_walk_had_gap"] = False
                _save()
                _rotation_cursor = (idx + 1) % n
                return user_id, entry["next_offset"], "full"

            if quick_due:
                _rotation_cursor = (idx + 1) % n
                return user_id, quick_offset, "quick"
        _rotation_cursor = (_rotation_cursor + 1) % n
        return None
