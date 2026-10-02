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
            # Optional second venue, polled less often than the main one -
            # see set_alt_venue.
            "altVenueId": entry.get("altVenueId"),
            "altVenueName": entry.get("altVenueName"),
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


async def set_alt_venue(owner_id: int, venue_id: int, venue_name: str | None) -> None:
    """A second Untappd venue to watch next to the main one (e.g. the
    neighbouring address people sometimes check into by mistake). Independent
    of set_location - changing the main point never touches it. Polled by
    the same venue-checkins loop but only every Nth tick (see
    webapp_server.FESTIVAL_WATCH_ALT_VENUE_EVERY_N_TICKS), so it costs a
    fraction of the main venue's quota. Fresh cursor, since another venue's
    check-in ids are an unrelated numbering."""
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["altVenueId"] = venue_id
        entry["altVenueName"] = venue_name
        entry["altVenueLastCheckinId"] = None
        _save(data)


async def clear_alt_venue(owner_id: int) -> None:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["altVenueId"] = None
        entry["altVenueName"] = None
        entry["altVenueLastCheckinId"] = None
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


async def list_venue_jobs() -> list[dict]:
    """One job per watched venue - what the independent venue-checkins poll
    loop (webapp_server._festival_watch_venue_loop) iterates. Each job is
    {"ownerId", "slot", "venueId", "venueName", "lastCheckinId"}, slot being
    "main" (polled every tick) or "alt" (polled only every Nth tick). Runs
    alongside the friends+radius check (_check_festival_novelty), not
    instead of it - a check-in logged at a neighbouring venue is invisible
    to venue/checkins but still caught by the radius check."""
    async with _lock:
        data = _load()
        jobs = []
        for owner_id_str, entry in data.items():
            if not entry.get("enabled"):
                continue
            owner_id = int(owner_id_str)
            if entry.get("venueId") is not None:
                jobs.append({
                    "ownerId": owner_id, "slot": "main", "venueId": entry["venueId"],
                    "venueName": entry.get("venueName"), "lastCheckinId": entry.get("venueLastCheckinId"),
                })
            if entry.get("altVenueId") is not None:
                jobs.append({
                    "ownerId": owner_id, "slot": "alt", "venueId": entry["altVenueId"],
                    "venueName": entry.get("altVenueName"), "lastCheckinId": entry.get("altVenueLastCheckinId"),
                })
        return jobs


async def record_venue_tick(owner_id: int, last_checkin_id: int, slot: str = "main") -> None:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["altVenueLastCheckinId" if slot == "alt" else "venueLastCheckinId"] = last_checkin_id
        _save(data)
