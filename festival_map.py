"""Shared, server-backed layout of a festival's brewery zones.

Unlike checkin_queue.py, there is no per-viewer state here - the physical
venue map is the same for every visitor of the SAME festival. But since
different Telegram groups can now be bound to different festivals at the
same time (see group_festivals.py), the saved layout is bucketed per
festival key - otherwise two groups looking at two different festivals
would stomp on each other's map: get_layout prunes any brewery not in the
caller's own `known_breweries`, so a WFP request would delete MBCC's
breweries from a single shared file and vice versa.

Zone names/count aren't fixed here - MBCC happens to have 4 ("Area 1"..
"Area 4"), but a different festival's data could define any number under
any names. The caller (webapp_server.py) is the one that knows how to spot
a "main, editable zone" name for whichever festival dataset a given request
resolved to (see its own `_festival_editable_zone_names`) and passes that
list, plus the resolved festival key, into `get_layout`/`move_brewery` on
every call - this module just persists whatever zones it's told about for
whichever key, generically.

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
into.

A side's list can hold `None` entries - a real, addressable empty slot at
that position, not just "shorter than the other side" (the Mini App renders
left/right as a shared virtual row grid, so a lone brewery on one side can
still be dropped into any one of, say, 5 positions the OTHER side has - see
app.js's renderPerimeterPills). move_brewery decides per-call whether a
move CLAIMS an exact slot (the target position is empty, or past the
current end - place it there, leave a None behind at its old spot, don't
shift anything else) or REORDERS normally (the target position holds a
real brewery - shift everyone from there on, same as always, and fully
remove the old spot rather than leaving a gap) - based on what's actually
at the target index when the move is made. Trailing Nones (nothing real
left after them) are trimmed on every read/write, since they don't align
with anything anymore once nothing follows them.
"""

import asyncio
import json
import os

SIDES = ("top", "left", "right", "bottom")

# Bucket used for callers with no specific festival key (group_key is None
# and there's no tracked default key either) - keeps a single shared layout
# for that edge case rather than erroring, same as before per-key bucketing
# existed.
_DEFAULT_BUCKET = "__default__"

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "festival_map.json")


def _bucket_key(festival_key: str | None) -> str:
    return festival_key or _DEFAULT_BUCKET


def _empty_zones(zones: list[str]) -> dict[str, dict[str, list[str]]]:
    return {z: {s: [] for s in SIDES} for z in zones}


def _load_all() -> dict:
    """Every festival's saved zones, keyed by festival key (or
    _DEFAULT_BUCKET - see _bucket_key). Assumes migrate_legacy_default has
    already run at startup; if the file is somehow still in the old
    single-map shape ({"zones": {...}}, from before per-festival scoping
    existed) when this is called, treats it as empty rather than guessing
    which key it belongs to - migrate_legacy_default is the only place
    that knows the right answer (the app's current default key)."""
    if not _path or not os.path.exists(_path):
        return {}
    try:
        with open(_path, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict) or ("zones" in raw and isinstance(raw["zones"], dict)):
        return {}
    return raw


def _save_all(data: dict) -> None:
    tmp_path = _path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _path)


async def migrate_legacy_default(default_key: str | None) -> None:
    """One-time migration: if festival_map.json is still in the old
    single-map shape (from before per-festival-key bucketing existed),
    rewrite it into the new bucketed shape under `default_key`'s bucket -
    the old file always represented whichever festival was the app's one
    and only dataset at the time, which for a caller right after this
    change ships is exactly `default_key`. Call once at startup, before
    any request-driven get_layout/move_brewery call could otherwise race
    this same rewrite by resolving a DIFFERENT, unrelated key first (which
    would wrongly file the old MBCC-era layout under that other key). A
    no-op if the file doesn't exist yet or is already in the new shape."""
    if not _path or not os.path.exists(_path):
        return
    async with _lock:
        try:
            with open(_path, encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, dict) or "zones" not in raw or not isinstance(raw["zones"], dict):
            return  # already migrated, or not the old shape - nothing to do
        _save_all({_bucket_key(default_key): raw})


def _load_bucket(all_data: dict, bucket: str, zones: list[str]) -> dict[str, dict[str, list[str | None]]]:
    loaded = _empty_zones(zones)
    bucket_data = all_data.get(bucket)
    saved = bucket_data.get("zones", {}) if isinstance(bucket_data, dict) else {}
    for z in zones:
        saved_zone = saved.get(z)
        if not isinstance(saved_zone, dict):
            continue
        for s in SIDES:
            items = saved_zone.get(s)
            if isinstance(items, list):
                loaded[z][s] = [b for b in items if isinstance(b, str) or b is None]
    return loaded


def _trim_trailing_none(loaded: dict[str, dict[str, list[str | None]]]) -> bool:
    """A None past the last real entry in a side's list doesn't align with
    anything anymore (nothing further along to leave room for) - trims it
    so an empty tail doesn't linger/grow forever as items get moved
    around. Returns True if anything was actually trimmed (the caller's
    cue to persist the change)."""
    trimmed = False
    for zone in loaded.values():
        for side in SIDES:
            lst = zone[side]
            while lst and lst[-1] is None:
                lst.pop()
                trimmed = True
    return trimmed


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
    festival_key: str | None, known_breweries: list[str], brewery_zone_hint: dict[str, str], zones: list[str]
) -> dict[str, dict[str, list[str | None]]]:
    """Returns the current zone layout for `festival_key` (for the given
    `zones` - whatever the caller currently considers that festival's main,
    editable zones), seeding in any brewery from `known_breweries` that
    isn't placed on any side of any zone yet, and dropping any placed
    brewery that ISN'T in `known_breweries` - proven live necessary: the
    underlying festival beer data file is swappable, and without this,
    switching from one festival's data to a completely different one left
    the OLD festival's breweries stuck on the map forever. A brewery whose
    name happens to be identical across both datasets keeps its existing
    position rather than being reset - harmless, and avoids needlessly
    reshuffling a coincidental overlap. `None` entries (empty, addressable
    slots - see this module's own docstring) are never pruned as "unknown
    breweries" and never seeded into."""
    if not zones:
        return {}
    bucket = _bucket_key(festival_key)
    async with _lock:
        all_data = _load_all()
        loaded = _load_bucket(all_data, bucket, zones)
        known = set(known_breweries)
        pruned = False
        for zone in loaded.values():
            for side in SIDES:
                filtered = [b for b in zone[side] if b is None or b in known]
                if len(filtered) != len(zone[side]):
                    pruned = True
                    zone[side] = filtered
        placed = {b for zone in loaded.values() for side in zone.values() for b in side if b is not None}
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
        if _trim_trailing_none(loaded):
            pruned = True
        if new_by_zone or pruned:
            all_data[bucket] = {"zones": loaded}
            _save_all(all_data)
        return loaded


async def move_brewery(
    festival_key: str | None, brewery: str, zone: str, side: str, index: int, zones: list[str]
) -> bool:
    """Moves `brewery` (from wherever it currently sits, if anywhere) into
    `zone`/`side` at `index`, within `festival_key`'s own bucket. Returns
    False only if `zone`/`side` is invalid for the given `zones` list.

    Two different behaviors, chosen by what's AT the target position when
    the move is made (see this module's own docstring):
    - Target is empty (`None`) or past the current end: CLAIMS that exact
      slot - `brewery` goes there and nowhere shifts, and wherever it USED
      to be becomes `None` rather than being spliced away, so no OTHER
      row's alignment changes just because this one moved.
    - Target is a real, occupied position: an ordinary reorder - shifts
      everything from that point on, and its old position is fully
      removed (collapsed), same as this function always did before slots
      existed."""
    if zone not in zones or side not in SIDES:
        return False
    bucket = _bucket_key(festival_key)
    async with _lock:
        all_data = _load_all()
        loaded = _load_bucket(all_data, bucket, zones)

        target = loaded[zone][side]
        index = max(0, index)
        claim_slot = index >= len(target) or target[index] is None

        for z in loaded.values():
            for s in SIDES:
                lst = z[s]
                for i, b in enumerate(lst):
                    if b == brewery:
                        if claim_slot:
                            lst[i] = None
                        else:
                            lst.pop(i)
                        break  # a brewery only ever occupies one slot at a time

        if claim_slot:
            while len(target) <= index:
                target.append(None)
            target[index] = brewery
        else:
            target.insert(min(index, len(target)), brewery)

        _trim_trailing_none(loaded)
        all_data[bucket] = {"zones": loaded}
        _save_all(all_data)
        return True
