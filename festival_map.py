"""Shared, server-backed layout of a festival's brewery zones.

Unlike checkin_queue.py, there is no per-viewer state here - the physical
venue map is the same for everyone, so every connected user reads and
writes the exact same `zones` dict.

Zone names/count aren't fixed here - MBCC happens to have 4 ("Area 1"..
"Area 4"), but a different festival's data could define any number under
any names. The caller (webapp_server.py) is the one that knows how to spot
a "main, editable zone" name for whichever festival is currently loaded
(see its own `_festival_editable_zone_names`) and passes that list into
`get_layout`/`move_brewery` on every call - this module just persists
whatever zones it's told about, generically.

Each zone renders as a rectangle perimeter (a short top row, tall left/right
columns, a short bottom row, matching the venue's real layout) rather than a
flat list, so each zone stores 4 *independent* ordered lists - one per side
- instead of a single flat list with the perimeter shape re-derived from it
on every read. That independence matters: an earlier version derived the
left/right split from a flat list by alternating position parity, and
because a single insertion anywhere in that list shifts the parity of every
item after it, dragging one brewery a few slots would silently flip a bunch
of unrelated breweries into the other column too. Storing each side
separately means a move only ever touches the one side's list it's dropped
into - moving a brewery is always "remove it from whichever side currently
has it, insert it at the target index in the target side's list."
"""

import asyncio
import json
import os

SIDES = ("top", "left", "right", "bottom")

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "festival_map.json")


def _empty_zones(zones: list[str]) -> dict[str, dict[str, list[str]]]:
    return {z: {s: [] for s in SIDES} for z in zones}


def _load(zones: list[str]) -> dict[str, dict[str, list[str]]]:
    loaded = _empty_zones(zones)
    if not _path or not os.path.exists(_path):
        return loaded
    try:
        with open(_path, encoding="utf-8") as f:
            saved = json.load(f).get("zones", {})
    except (json.JSONDecodeError, OSError):
        return loaded
    for z in zones:
        saved_zone = saved.get(z)
        if not isinstance(saved_zone, dict):
            continue
        for s in SIDES:
            items = saved_zone.get(s)
            if isinstance(items, list):
                loaded[z][s] = [b for b in items if isinstance(b, str)]
    return loaded


def _save(zones: dict[str, dict[str, list[str]]]) -> None:
    tmp_path = _path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"zones": zones}, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _path)


def _seed_sides(breweries: list[str]) -> dict[str, list[str]]:
    """Initial perimeter placement for a batch of never-before-seen
    breweries in one zone - a short top/bottom edge (capped at 4) and the
    rest alternating between the two columns. Only used once per brewery,
    at first sight; after that, position is entirely drag-and-drop driven
    and this function is never consulted again for it."""
    n = len(breweries)
    edge = n if n <= 2 else min(4, max(1, round(n * 0.15)))
    top = breweries[:edge]
    has_bottom = edge < n - edge
    bottom = breweries[n - edge:] if has_bottom else []
    middle = breweries[edge:n - edge]
    return {"top": top, "left": middle[0::2], "right": middle[1::2], "bottom": bottom}


async def get_layout(
    known_breweries: list[str], brewery_zone_hint: dict[str, str], zones: list[str]
) -> dict[str, dict[str, list[str]]]:
    """Returns the current zone layout (for the given `zones` - whatever
    the caller currently considers the festival's main, editable zones),
    seeding in any brewery from `known_breweries` that isn't placed on any
    side of any zone yet, and dropping any placed brewery that ISN'T in
    `known_breweries` - proven live necessary: the underlying festival beer
    data file is swappable (webapp_server.py's FESTIVAL_BEERS_FILE), and
    without this, switching from one festival's data to a completely
    different one left the OLD festival's breweries stuck on the map
    forever (this module has no concept of "which dataset" a saved
    position came from, and get_layout previously only ever ADDED, never
    removed), making the two datasets' breweries visibly pile up together
    in the same zones. A brewery whose name happens to be identical across
    both datasets keeps its existing position rather than being reset -
    harmless, and avoids needlessly reshuffling a coincidental overlap."""
    if not zones:
        return {}
    async with _lock:
        loaded = _load(zones)
        known = set(known_breweries)
        pruned = False
        for zone in loaded.values():
            for side in SIDES:
                filtered = [b for b in zone[side] if b in known]
                if len(filtered) != len(zone[side]):
                    pruned = True
                    zone[side] = filtered
        placed = {b for zone in loaded.values() for side in zone.values() for b in side}
        new_by_zone: dict[str, list[str]] = {}
        for brewery in known_breweries:
            if brewery in placed:
                continue
            zone = brewery_zone_hint.get(brewery, zones[0])
            if zone not in zones:
                zone = zones[0]
            new_by_zone.setdefault(zone, []).append(brewery)
            placed.add(brewery)
        if new_by_zone:
            for zone, breweries in new_by_zone.items():
                seeded = _seed_sides(breweries)
                for side in SIDES:
                    loaded[zone][side].extend(seeded[side])
        if new_by_zone or pruned:
            _save(loaded)
        return loaded


async def move_brewery(brewery: str, zone: str, side: str, index: int, zones: list[str]) -> bool:
    """Moves `brewery` (from wherever it currently sits, if anywhere) into
    `zone`/`side` at `index`. Returns False only if `zone`/`side` is invalid
    for the given `zones` list."""
    if zone not in zones or side not in SIDES:
        return False
    async with _lock:
        loaded = _load(zones)
        for z in loaded.values():
            for s in SIDES:
                if brewery in z[s]:
                    z[s].remove(brewery)
        target = loaded[zone][side]
        index = max(0, min(index, len(target)))
        target.insert(index, brewery)
        _save(loaded)
        return True
