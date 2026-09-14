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
        }


async def set_location(owner_id: int, lat: float, lng: float, label: str | None = None) -> None:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["lat"] = lat
        entry["lng"] = lng
        entry["label"] = label
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
    """Owners with a saved point and the watch turned on - what
    _auto_toast_loop checks each tick before bothering with distance math."""
    async with _lock:
        data = _load()
        return [
            int(owner_id_str) for owner_id_str, entry in data.items()
            if entry.get("enabled") and entry.get("lat") is not None and entry.get("lng") is not None
        ]
