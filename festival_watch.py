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
            # Off by default - see set_notify_listed's own docstring for why.
            "notifyListedBeers": entry.get("notifyListedBeers", False),
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


async def list_enabled_owners() -> list[int]:
    """Owners with a saved point and the watch turned on, EXCLUDING anyone
    whose point has since resolved to a real venue_id - what
    _auto_toast_loop's friends-only, GPS+radius novelty check iterates each
    tick. An owner with venueId set is handled exclusively by the separate,
    better venue-checkins loop instead (see list_enabled_owners_with_venue) -
    checking them here too would double-notify the same real-world event."""
    async with _lock:
        data = _load()
        return [
            int(owner_id_str) for owner_id_str, entry in data.items()
            if entry.get("enabled") and entry.get("lat") is not None and entry.get("lng") is not None
            and entry.get("venueId") is None
        ]


async def list_enabled_owners_with_venue() -> list[dict]:
    """[{"ownerId", "venueId", "venueName", "lastCheckinId"}] for every
    owner with the watch on AND a resolved venue_id - what the independent
    venue-checkins poll loop (webapp_server._festival_watch_venue_loop)
    iterates each tick, completely separate from list_enabled_owners above."""
    async with _lock:
        data = _load()
        return [
            {
                "ownerId": int(owner_id_str),
                "venueId": entry["venueId"],
                "venueName": entry.get("venueName"),
                "lastCheckinId": entry.get("venueLastCheckinId"),
            }
            for owner_id_str, entry in data.items()
            if entry.get("enabled") and entry.get("venueId") is not None
        ]


async def record_venue_tick(owner_id: int, last_checkin_id: int) -> None:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["venueLastCheckinId"] = last_checkin_id
        _save(data)
