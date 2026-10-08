"""Personal "festival novelty watch" - notifies the owner when *any* friend
checks in a beer within a saved radius that ISN'T on the currently-loaded
festival beer list (mbcc_beers.json) - a tap change, a surprise limited
release, anything the static list missed. Deliberately NOT about "have I
had this" (that's auto_toast.py/had_it_index.py's question) - the whole
point is catching what's being poured at a specific physical place right
now, regardless of whether the watcher personally cares about that beer.

Reuses the exact same get_my_friend_feed poll _auto_toast_loop already
makes every tick, at zero extra Untappd quota cost - see
webapp_server.py's _auto_toast_loop, which runs this module's
is_within/get_config against the same fetched feed items, sending a
Telegram message instead of calling toast_checkin. A direct consequence:
this only actually fires while that feed poll is running, i.e. while the
owner has auto-toast enabled with at least one target - a deliberate v1
limitation (documented in README.md) rather than a second independent
poll loop, since it comes for free this way instead of spending its own
quota budget.

Same shape as checkin_queue.py: module-level `_path`, `asyncio.Lock`,
`init(data_dir)`, atomic tmp-file + os.replace() writes.
"""

import asyncio
import json
import math
import os

_path: str | None = None
_lock = asyncio.Lock()

DEFAULT_RADIUS_METERS = 500

# Novelty filter defaults: every style, every average rating = no filtering.
RATING_MIN, RATING_MAX = 0.0, 5.0
_STYLES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "untappd_styles.json")
_styles_cache: list[str] | None = None


def styles_taxonomy() -> list[str]:
    """Untappd's beer-like style names (untappd_styles.json), in the file's
    alphabetical order - what the style filter's picker offers and the only
    names it accepts."""
    global _styles_cache
    if _styles_cache is None:
        try:
            with open(_STYLES_FILE, encoding="utf-8") as fh:
                _styles_cache = [x for x in json.load(fh).get("styles", []) if isinstance(x, str)]
        except (OSError, json.JSONDecodeError):
            _styles_cache = []
    return _styles_cache


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "festival_watch.json")


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


def _owner_entry(data: dict, owner_id: int) -> dict:
    return data.setdefault(str(owner_id), {
        "enabled": False, "lat": None, "lng": None,
        "radiusMeters": DEFAULT_RADIUS_METERS, "label": None,
        "venueId": None, "venueName": None, "venueLastCheckinId": None,
        "notifyListedBeers": False,
    })


async def get_config(owner_id: int) -> dict:
    async with _lock:
        data = _load()
        entry = data.get(str(owner_id)) or {}
        return {
            "enabled": entry.get("enabled", False),
            "lat": entry.get("lat"),
            "lng": entry.get("lng"),
            "radiusMeters": entry.get("radiusMeters", DEFAULT_RADIUS_METERS),
            "label": entry.get("label"),
            # venueId set means this watch point resolved to a real Untappd
            # venue - see set_venue's own docstring for what that unlocks.
            "venueId": entry.get("venueId"),
            "venueName": entry.get("venueName"),
            # [{"venueId", "venueName"}] - see add_extra_venue.
            "extraVenues": [
                {"venueId": v["venueId"], "venueName": v.get("venueName")}
                for v in entry.get("extraVenues", [])
            ],
            # Cursor of the loop's own direct friends-feed poll - see
            # webapp_server._festival_watch_friends_owner.
            "friendsLastCheckinId": entry.get("friendsLastCheckinId"),
            # Off by default - see set_notify_listed's own docstring for why.
            "notifyListedBeers": entry.get("notifyListedBeers", False),
            # Novelty filter (see set_novelty_filter): no styles listed = all
            # styles; 0..5 = every average rating.
            "noveltyStyles": list(entry.get("noveltyStyles") or []),
            "noveltyRatingMin": entry.get("noveltyRatingMin", RATING_MIN),
            "noveltyRatingMax": entry.get("noveltyRatingMax", RATING_MAX),
        }


async def set_location(owner_id: int, lat: float, lng: float, label: str | None = None) -> None:
    """Also clears any previously-resolved venue (see set_venue) - a fresh
    lat/lng always means either a bare GPS point (never had a venue to
    begin with) or the caller picked a NEW named place and will call
    set_venue again right after if that one resolves; either way, a stale
    venue_id pointing at the PREVIOUS watch point must not survive this
    call, or the venue-checkins loop would keep watching the wrong place."""
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["lat"] = lat
        entry["lng"] = lng
        entry["label"] = label
        entry["venueId"] = None
        entry["venueName"] = None
        entry["venueLastCheckinId"] = None
        _save(data)


async def set_venue(owner_id: int, venue_id: int, venue_name: str | None) -> None:
    """Attaches a resolved Untappd venue_id to the CURRENT watch point -
    called right after set_location, only when the just-picked place's
    foursquareId actually resolved to a real Untappd venue (see
    untappd_direct.lookup_venue_by_foursquare). Once set, the venue-checkins
    poll loop takes over novelty detection for this owner entirely (sees
    EVERYONE at that venue, not just friends) - see
    webapp_server._festival_watch_venue_loop. Resets the cursor to None
    (fresh bootstrap) since a different venue's check-in ids are a
    completely unrelated numbering."""
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["venueId"] = venue_id
        entry["venueName"] = venue_name
        entry["venueLastCheckinId"] = None
        _save(data)


MAX_EXTRA_VENUES = 30


async def add_extra_venue(owner_id: int, venue_id: int, venue_name: str | None) -> bool:
    """Adds an Untappd venue to the owner's extra-venues list (neighbouring
    addresses people check into by mistake, other bars near the festival...).
    Polled by webapp_server._festival_watch_scrape_loop via
    venue_scrape - no API quota, so the list can be long. Independent of
    set_location. False when it's already listed or the list is full."""
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        extras = entry.setdefault("extraVenues", [])
        if len(extras) >= MAX_EXTRA_VENUES or any(v["venueId"] == venue_id for v in extras):
            return False
        extras.append({"venueId": venue_id, "venueName": venue_name, "lastCheckinId": None})
        _save(data)
        return True


async def remove_extra_venue(owner_id: int, venue_id: int) -> bool:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        extras = entry.get("extraVenues", [])
        kept = [v for v in extras if v["venueId"] != venue_id]
        if len(kept) == len(extras):
            return False
        entry["extraVenues"] = kept
        _save(data)
        return True


async def list_extra_venue_jobs() -> list[dict]:
    """[{"ownerId", "venueId", "venueName", "lastCheckinId"}] for every extra
    venue of every owner with the watch enabled."""
    async with _lock:
        return [
            {
                "ownerId": int(owner_id_str), "venueId": v["venueId"],
                "venueName": v.get("venueName"), "lastCheckinId": v.get("lastCheckinId"),
            }
            for owner_id_str, entry in _load().items() if entry.get("enabled")
            for v in entry.get("extraVenues", [])
        ]


async def record_extra_venue_tick(owner_id: int, venue_id: int, last_checkin_id: int) -> None:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        for v in entry.get("extraVenues", []):
            if v["venueId"] == venue_id:
                v["lastCheckinId"] = last_checkin_id
                _save(data)
                return


async def record_friends_tick(owner_id: int, last_checkin_id: int | None) -> None:
    """None resets the cursor (next poll re-baselines instead of replaying
    whatever piled up while the poll wasn't running)."""
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        if entry.get("friendsLastCheckinId") == last_checkin_id:
            return
        entry["friendsLastCheckinId"] = last_checkin_id
        _save(data)


async def set_radius(owner_id: int, meters: int) -> None:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["radiusMeters"] = meters
        _save(data)


async def set_enabled(owner_id: int, enabled: bool) -> None:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["enabled"] = enabled
        _save(data)


async def set_notify_listed(owner_id: int, enabled: bool) -> None:
    """Whether _notify_festival_novelty should ALSO ping for a beer that's
    already on the festival's own static list but not yet in the shared
    queue (the "📋" message), on top of the always-on "🆕 genuinely off the
    list" one. Deliberately a separate, off-by-default toggle - early in a
    session almost nothing is queued yet, so the "listed but not queued"
    signal would fire for nearly every single check-in anyone at the venue
    makes (near-total noise); it only becomes a meaningful "something
    changed - a keg swap, a limited tap" signal once most of the list IS
    already queued, typically partway through the session."""
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["notifyListedBeers"] = enabled
        _save(data)


async def set_novelty_filter(owner_id: int, styles, rating_min, rating_max) -> bool:
    """Narrows which novelties get a notification: only beers of the chosen
    `styles` (an empty list, or every style ticked, means all styles) whose
    average Untappd rating is within [rating_min, rating_max] (0..5 = all).
    Names outside the taxonomy are dropped; False for a malformed request
    (non-list styles, non-numeric or inverted/out-of-range ratings)."""
    if not isinstance(styles, list) or not all(isinstance(x, str) for x in styles):
        return False
    try:
        lo, hi = round(float(rating_min), 1), round(float(rating_max), 1)
    except (TypeError, ValueError):
        return False
    if not (RATING_MIN <= lo <= hi <= RATING_MAX):
        return False
    taxonomy = styles_taxonomy()
    chosen = set(styles)
    kept = [x for x in taxonomy if x in chosen]
    if len(kept) == len(taxonomy):
        kept = []  # every style ticked = no style filter
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["noveltyStyles"] = kept
        entry["noveltyRatingMin"] = lo
        entry["noveltyRatingMax"] = hi
        _save(data)
    return True


def novelty_filter_active(config: dict) -> bool:
    return bool(config.get("noveltyStyles")) or config.get("noveltyRatingMin", RATING_MIN) > RATING_MIN         or config.get("noveltyRatingMax", RATING_MAX) < RATING_MAX


def novelty_filter_needs_facts(config: dict, style: str | None) -> bool:
    """Whether judging a beer needs a lookup beyond what the check-in item
    itself carries: its style (scraped items have none) or its average rating
    (no check-in item has it)."""
    rating_filtered = config.get("noveltyRatingMin", RATING_MIN) > RATING_MIN         or config.get("noveltyRatingMax", RATING_MAX) < RATING_MAX
    return rating_filtered or (bool(config.get("noveltyStyles")) and not style)


def novelty_passes_filter(config: dict, style: str | None, rating: float | None) -> bool:
    """Whether a beer passes the owner's novelty filter. Unknown facts never
    suppress a notification (missing a new beer is worse than one extra
    ping): a beer without a known style isn't dropped by the style filter,
    and one with no ratings yet (None/0 - typical for a fresh release) isn't
    dropped by the rating filter."""
    styles = config.get("noveltyStyles") or []
    if styles and style and style not in styles:
        return False
    lo = config.get("noveltyRatingMin", RATING_MIN)
    hi = config.get("noveltyRatingMax", RATING_MAX)
    if rating and (lo > RATING_MIN or hi < RATING_MAX) and not (lo <= rating <= hi):
        return False
    return True


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two lat/lng points, in meters."""
    r = 6_371_000  # Earth's mean radius
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def is_within(lat: float, lng: float, center_lat: float, center_lng: float, radius_meters: float) -> bool:
    return haversine_meters(lat, lng, center_lat, center_lng) <= radius_meters


async def list_venue_jobs() -> list[dict]:
    """[{"ownerId", "venueId", "venueName", "lastCheckinId"}] for every owner
    with the watch on AND a resolved main venue_id - what the API venue-
    checkins loop (webapp_server._festival_watch_venue_loop) iterates. Runs
    alongside the friends+radius check (_check_festival_novelty) and the
    extra-venue scrape, not instead of them."""
    async with _lock:
        return [
            {
                "ownerId": int(owner_id_str), "venueId": entry["venueId"],
                "venueName": entry.get("venueName"), "lastCheckinId": entry.get("venueLastCheckinId"),
            }
            for owner_id_str, entry in _load().items()
            if entry.get("enabled") and entry.get("venueId") is not None
        ]


async def record_venue_tick(owner_id: int, last_checkin_id: int) -> None:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["venueLastCheckinId"] = last_checkin_id
        _save(data)
