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

A zone can also hold "islands" - a handful of small brewery clusters in the
middle of the floor, for venues (WFP's 2nd floor, for one) whose real
layout isn't just a perimeter. Each zone stores an `islands` dict, keyed by
a locally-generated id, each value `{"label": str, "breweries": [...]}` -
the `breweries` list uses this exact same None-slot convention, so
move_brewery's claim-vs-reorder logic needs no island-specific branching,
just a second kind of target list to point at. Dict insertion order is
display order - no separate ordering field. Unlike the 4 perimeter sides,
islands are never auto-seeded (get_layout's new-brewery placement always
lands on the perimeter, same as before islands existed) and never
implicitly created by a move - create_island/delete_island are the only
way an island comes or goes, so move_brewery treats a request for an
island id that doesn't exist as invalid rather than inventing it.

A festival can also ship a LAYOUT TEMPLATE (festival_layouts/<key>.json,
loaded by webapp_server): the official floor plan transcribed as lists of
stand LABELS per zone/side/island in plan order, plus a `version`. Labels are
matched to the festival data's own brewery names by whole-word,
diacritic-insensitive containment (resolve_label - "Bednary" finds "Browar
Bednary"), so the template doesn't need the data's exact spelling. When a
bucket's saved `templateVersion` differs from the template's, the layout is
rebuilt ONCE from the template (the previous zones are kept under
`previousZones`); from then on it is ordinary drag-and-drop data again,
except that a brewery appearing in the data LATER and matching a label is
inserted at its plan position (next to its template neighbours) instead of
being appended to the perimeter.
"""

import asyncio
import copy
import json
import os
import re
import unicodedata

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


def _empty_zones(zones: list[str]) -> dict[str, dict]:
    return {z: {**{s: [] for s in SIDES}, "islands": {}} for z in zones}


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


def _load_bucket(all_data: dict, bucket: str, zones: list[str]) -> dict[str, dict]:
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
        saved_islands = saved_zone.get("islands")
        if isinstance(saved_islands, dict):
            for island_id, island in saved_islands.items():
                if not isinstance(island_id, str) or not isinstance(island, dict):
                    continue
                breweries = island.get("breweries")
                if not isinstance(breweries, list):
                    continue
                label = island.get("label")
                loaded[z]["islands"][island_id] = {
                    "label": label if isinstance(label, str) else "",
                    "breweries": [b for b in breweries if isinstance(b, str) or b is None],
                }
    return loaded


def _island_lists(zone: dict) -> list[list[str | None]]:
    return [island["breweries"] for island in zone["islands"].values()]


def _trim_trailing_none(loaded: dict[str, dict]) -> bool:
    """A None past the last real entry in a side's (or island's) list
    doesn't align with anything anymore (nothing further along to leave
    room for) - trims it so an empty tail doesn't linger/grow forever as
    items get moved around. Returns True if anything was actually trimmed
    (the caller's cue to persist the change)."""
    trimmed = False
    for zone in loaded.values():
        for lst in [zone[side] for side in SIDES] + _island_lists(zone):
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


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", (text or "").replace("ł", "l").replace("Ł", "L"))
    return re.findall(r"[a-z0-9]+", "".join(c for c in folded if not unicodedata.combining(c)).lower())


def resolve_label(label: str, candidates: list[str]) -> str | None:
    """The candidate brewery a plan `label` stands for: every word of the
    label must be a whole word of the brewery name (so "Bednary" finds
    "Browar Bednary", "Magic Road" finds "Magic Road", but "Pinta" does not
    find "Pintaz"), and among several matches the one with the fewest extra
    words wins (the label's own stand over a longer name that merely
    contains it)."""
    wanted = set(_tokens(label))
    if not wanted:
        return None
    best: tuple[int, str] | None = None
    for brewery in candidates:
        words = set(_tokens(brewery))
        if wanted <= words:
            extra = len(words - wanted)
            if best is None or extra < best[0]:
                best = (extra, brewery)
    return best[1] if best else None


def _template_containers(template: dict, zones: list[str]) -> list[tuple[str, str, str, str, list[str]]]:
    """[(zone, kind, key, island_label, labels)] - kind is "side" (key =
    the side name) or "island" (key = the island id) - for the template's
    zones that exist in `zones`, in plan order."""
    out = []
    for zone, spec in (template.get("zones") or {}).items():
        if zone not in zones or not isinstance(spec, dict):
            continue
        for side in SIDES:
            labels = spec.get(side)
            if isinstance(labels, list):
                out.append((zone, "side", side, "", [x for x in labels if isinstance(x, str)]))
        for island_id, island in (spec.get("islands") or {}).items():
            if isinstance(island, dict) and isinstance(island.get("breweries"), list):
                label = island.get("label") if isinstance(island.get("label"), str) else ""
                out.append((zone, "island", island_id, label, [x for x in island["breweries"] if isinstance(x, str)]))
    return out


def _container_list(loaded: dict, zone: str, kind: str, key: str, island_label: str, create: bool) -> list | None:
    if kind == "side":
        return loaded[zone][key]
    islands = loaded[zone]["islands"]
    if key not in islands:
        if not create:
            return None
        islands[key] = {"label": island_label, "breweries": []}
    return islands[key]["breweries"]


def _rebuild_from_template(template: dict, zones: list[str], known_breweries: list[str]) -> dict[str, dict]:
    """A fresh layout holding every known brewery that matches a template
    label, in plan order. Breweries matching no label are left unplaced (the
    caller's normal seeding puts them on the perimeter); labels matching no
    known brewery yet simply don't appear - no empty placeholder slots."""
    fresh = _empty_zones(zones)
    free = list(known_breweries)
    for zone, kind, key, island_label, labels in _template_containers(template, zones):
        placed = []
        for label in labels:
            brewery = resolve_label(label, free)
            if brewery is not None:
                placed.append(brewery)
                free.remove(brewery)
        if placed:
            _container_list(fresh, zone, kind, key, island_label, create=True).extend(placed)
    return fresh


def _place_from_template(loaded: dict, template: dict, zones: list[str], brewery: str) -> bool:
    """Puts a not-yet-placed `brewery` at its plan position, if a template
    label names it: inside the container (side or island) that label lives
    in, before the first already-placed brewery that comes LATER in the
    plan (else at the end). Returns False when no label matches."""
    for zone, kind, key, island_label, labels in _template_containers(template, zones):
        for position, label in enumerate(labels):
            if resolve_label(label, [brewery]) != brewery:
                continue
            target = _container_list(loaded, zone, kind, key, island_label, create=True)
            insert_at = len(target)
            for i, other in enumerate(target):
                if other is None:
                    continue
                order = next((j for j, lab in enumerate(labels) if resolve_label(lab, [other]) == other), None)
                if order is not None and order > position:
                    insert_at = i
                    break
            target.insert(insert_at, brewery)
            return True
    return False


async def get_layout(
    festival_key: str | None, known_breweries: list[str], brewery_zone_hint: dict[str, str], zones: list[str],
    template: dict | None = None,
) -> dict[str, dict]:
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
        bucket_meta = dict(all_data.get(bucket) or {})
        if template and bucket_meta.get("templateVersion") != template.get("version"):
            bucket_meta["previousZones"] = copy.deepcopy(loaded)  # kept so a rebuild can be undone by hand
            bucket_meta["templateVersion"] = template.get("version")
            loaded = _rebuild_from_template(template, zones, known_breweries)
            pruned = True
        for zone in loaded.values():
            for side in SIDES:
                filtered = [b for b in zone[side] if b is None or b in known]
                if len(filtered) != len(zone[side]):
                    pruned = True
                    zone[side] = filtered
            for lst in _island_lists(zone):
                filtered = [b for b in lst if b is None or b in known]
                if len(filtered) != len(lst):
                    pruned = True
                    lst[:] = filtered
        placed = {
            b
            for zone in loaded.values()
            for lst in [zone[side] for side in SIDES] + _island_lists(zone)
            for b in lst
            if b is not None
        }
        new_by_zone: dict[str, list[str]] = {}
        for brewery in known_breweries:
            if brewery in placed:
                continue
            if template and _place_from_template(loaded, template, zones, brewery):
                placed.add(brewery)
                pruned = True  # a change to persist, same as a seeded placement
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
            all_data[bucket] = {**bucket_meta, "zones": loaded}
            _save_all(all_data)
        return loaded


async def move_brewery(
    festival_key: str | None, brewery: str, zone: str, side: str, index: int, zones: list[str],
    island_id: str | None = None,
) -> bool:
    """Moves `brewery` (from wherever it currently sits, if anywhere) into
    `zone`/`side` at `index` (or, if `island_id` is given, into that
    island instead - `side` is then ignored), within `festival_key`'s own
    bucket. Returns False if `zone`/`side` is invalid for the given
    `zones` list, or if `island_id` doesn't name an island that already
    exists in `zone` (islands are only ever created via create_island -
    a stale/unknown id is rejected rather than silently recreated).

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
    if zone not in zones:
        return False
    if island_id is None and side not in SIDES:
        return False
    bucket = _bucket_key(festival_key)
    async with _lock:
        all_data = _load_all()
        loaded = _load_bucket(all_data, bucket, zones)

        if island_id is not None:
            island = loaded[zone]["islands"].get(island_id)
            if island is None:
                return False
            target = island["breweries"]
        else:
            target = loaded[zone][side]
        index = max(0, index)
        claim_slot = index >= len(target) or target[index] is None

        for z in loaded.values():
            for lst in [z[s] for s in SIDES] + _island_lists(z):
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
        all_data[bucket] = {**(all_data.get(bucket) or {}), "zones": loaded}
        _save_all(all_data)
        return True


async def create_island(festival_key: str | None, zone: str, zones: list[str]) -> str | None:
    """Creates a new, empty island in `zone` and returns its id, or None
    if `zone` is invalid. The id is locally unique within the zone
    (`isl_<n>`, one past the highest existing numeric suffix there) - it's
    never shown to the viewer, only used for drag/drop bookkeeping."""
    if zone not in zones:
        return None
    bucket = _bucket_key(festival_key)
    async with _lock:
        all_data = _load_all()
        loaded = _load_bucket(all_data, bucket, zones)
        existing = loaded[zone]["islands"]
        n = 1
        for island_id in existing:
            if island_id.startswith("isl_") and island_id[4:].isdigit():
                n = max(n, int(island_id[4:]) + 1)
        island_id = f"isl_{n}"
        existing[island_id] = {"label": "", "breweries": []}
        all_data[bucket] = {**(all_data.get(bucket) or {}), "zones": loaded}
        _save_all(all_data)
        return island_id


async def delete_island(festival_key: str | None, zone: str, island_id: str, zones: list[str]) -> bool:
    """Removes `island_id` from `zone`. Any breweries it still held are
    appended to that zone's `top` list rather than discarded - same
    "don't silently lose a placement" instinct as the rest of this
    module. Returns False if `zone`/`island_id` don't exist."""
    if zone not in zones:
        return False
    bucket = _bucket_key(festival_key)
    async with _lock:
        all_data = _load_all()
        loaded = _load_bucket(all_data, bucket, zones)
        island = loaded[zone]["islands"].pop(island_id, None)
        if island is None:
            return False
        loaded[zone]["top"].extend(b for b in island["breweries"] if b is not None)
        _trim_trailing_none(loaded)
        all_data[bucket] = {**(all_data.get(bucket) or {}), "zones": loaded}
        _save_all(all_data)
        return True
