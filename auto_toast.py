"""Personal "auto-toast" - watches a chosen list of Untappd usernames and
toasts their new check-ins automatically, using the connected account's own
toast_checkin MCP tool. Entirely per-viewer (keyed by Telegram user id, same
"everything personal" convention as had_it_index.py/venue_index.py) - one
person's target list and country exclusions never affect another's.

Deliberately does NOT toast a target's existing history on first sight - see
`peek_owner_turn`'s bootstrap comment. Only check-ins made *after* a target
is added get toasted, the same way a real person turning on notifications
for someone wouldn't retroactively toast their last 5 years of check-ins.

Polling is per-OWNER, not per-target: get_my_friend_feed (Untappd's
checkin/recent, wrapping the connected account's whole friend feed) returns
everyone's new activity in one call, so there's a single shared feed cursor
per owner rather than one cursor per watched username - a big efficiency
win over the original per-target design (see git history / README for why
that existed first: this tool didn't exist on the MCP server until we asked
for it mid-project). Per-target state now only holds a cumulative toast
count, for display.

Same shape as checkin_queue.py: module-level `_path`, `asyncio.Lock`,
`init(data_dir)`, atomic tmp-file + os.replace() writes, no in-memory mirror
(this file stays tiny - a handful of targets per owner, nothing like
had_it_index.py's scale).
"""

import asyncio
import json
import os
import time

_path: str | None = None
_lock = asyncio.Lock()

# canonical exclusion key -> known spellings/scripts it should match. Untappd
# venue_country reflects the *venue's own* locale (a Polish venue's country
# came back as "Polska", not "Poland" - confirmed live this session), so a
# single English name isn't enough to reliably exclude a country.
_COUNTRY_ALIASES: dict[str, list[str]] = {
    "russia": [
        "russia", "russian federation", "rossiya", "россия", "росія", "рф",
        "russie", "russland", "rusia",
    ],
    "belarus": [
        "belarus", "byelorussia", "belorussia", "беларусь", "білорусь",
        "biélorussie", "weißrussland", "weissrussland", "bielorrusia",
    ],
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _key, _aliases in _COUNTRY_ALIASES.items():
    _ALIAS_TO_CANONICAL[_key] = _key
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL[_alias] = _key


def normalize_country_input(text: str) -> str:
    """Maps free-typed input ("рф", "росія", "russia"...) to the canonical
    key when recognized, so they all dedupe to one exclusion entry; anything
    unrecognized is kept as-is (lowercased) so a country not in the built-in
    alias list can still be excluded verbatim, matched literally later."""
    return _ALIAS_TO_CANONICAL.get(text.strip().lower(), text.strip().lower())


def is_country_excluded(venue_country: str | None, excluded_keys: list[str]) -> bool:
    """venue_country is the raw string from a check-in's venue.location -
    absent for check-ins with no venue at all, which are never excluded
    (nothing to judge by, default to allowing the toast)."""
    if not venue_country:
        return False
    normalized = venue_country.strip().lower()
    if not normalized:
        return False
    for key in excluded_keys:
        candidates = _COUNTRY_ALIASES.get(key, [key])
        for alias in candidates:
            if alias in normalized or normalized in alias:
                return True
    return False


# Untappd's own in-app "Filter by Style" screen (Settings) splits every
# style into "Legacy Drinks" (beer, cider, mead, hard kombucha, malt-based
# seltzers, hard root beer, "and other legacy styles" - always on) vs 6
# newer non-beer toggles, added in Untappd's Nov-2025 "expands beyond beer"
# update, all off by default: Non-Alcoholic, Ready-to-Drink, Sake, Spirit,
# THC-based Drink, Wine - screenshotted live this session, exact wording:
#
#   "Non-Alcoholic - Packaged drinks like sodas, juices, waters, mocktails,
#   and functional beverages that may contain CBD. This setting does NOT
#   include non-alcoholic versions of legacy drinks."
#
# That last sentence is NOT a blanket rule, though - it's a per-substyle
# assignment, not "every 'Non-Alcoholic - X' stays legacy". Confirmed two
# ways: (1) screenshotted live, expanding the "Non-Alcoholic" toggle
# in-app showed 4 real "Non-Alcoholic - X" substyles this account has
# actual check-ins for - Fassbrause/Kombucha/Malt Soda/Malta; (2) the
# user then pasted Untappd's own full <select> style-picker markup for
# every "Non-Alcoholic - X" option that exists (35 total, each with its
# real numeric style_id). That full list makes the split obvious by BOTH
# content and id range: ids 33/358-366 are the ORIGINAL pre-expansion
# style ids (Other Beer/Lager/IPA/Pale Ale/Wheat/Shandy-Radler/Porter-
# Stout/Sour/Mead/Cider-Perry - genuine beer/cider/mead sub-styles, all
# Legacy), ids 999-1016 are a first new-ids block added in the Nov-2025
# expansion and are never beer (CBD Beverage/Coffee/Energy Drink/
# Fassbrause/Functional Beverage/Hop Water/Kombucha/Malt Energy Drink/
# Malt Soda/Malta/RTD Cocktail/Shrub-Drinking Vinegar/Soda-Craft/Soda-
# Mass Market/Sparkling Water/Spirit Alternative/Tea-Lemonade/Wine), and
# ids 1255-1264 are a SECOND, later new-ids block that (unlike the first)
# mostly added MORE real beer-style variants (Fruit Beer/Blonde-Golden
# Ale/Brown Ale/Festbier-Märzen/Bitter/Red Ale/Farmhouse Ale - Legacy
# despite the "new" id) - except "Non-Alcoholic - Other" (id 1264, no
# "Beer" qualifier, unlike "...- Other Beer" id 33 which is Legacy) -
# the generic non-beer catch-all for this newer block, confirmed by the
# user. Untappd could still add more "Non-Alcoholic - X" options later -
# an unrecognized one falls through to legacy by default (same as any
# other unrecognized style - see is_legacy_style's docstring) until
# confirmed one way or the other.
_NON_LEGACY_PREFIXES = {"rtd", "sake", "spirit", "thc", "wine"}
_NON_LEGACY_STYLES = {
    "non-alcoholic - cbd beverage",
    "non-alcoholic - coffee",
    "non-alcoholic - energy drink",
    "non-alcoholic - fassbrause",
    "non-alcoholic - functional beverage",
    "non-alcoholic - hop water",
    "non-alcoholic - kombucha",
    "non-alcoholic - malt energy drink",
    "non-alcoholic - malt soda",
    "non-alcoholic - malta",
    "non-alcoholic - other",  # confirmed by the user, not "...- Other Beer" (id 33, Legacy)
    "non-alcoholic - rtd cocktail",
    "non-alcoholic - shrub/drinking vinegar",
    "non-alcoholic - soda - craft",
    "non-alcoholic - soda - mass market",
    "non-alcoholic - sparkling water",
    "non-alcoholic - spirit alternative",
    "non-alcoholic - tea/lemonade",
    "non-alcoholic - wine",
}


def is_legacy_style(beer_style: str | None) -> bool:
    """True for anything NOT in one of the non-beer categories above -
    including a check-in with no style at all (nothing to judge by, default
    to allowing it, same "absent = don't exclude" convention as
    is_country_excluded)."""
    if not beer_style:
        return True
    style_lower = beer_style.strip().lower()
    if style_lower in _NON_LEGACY_STYLES:
        return False
    prefix = style_lower.split(" - ", 1)[0].strip()
    return prefix not in _NON_LEGACY_PREFIXES


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "auto_toast.json")


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


def _migrate_entry(entry: dict) -> dict:
    """Normalizes an owner's entry to the current shape, in place -
    idempotent, safe to call on every access. Migrates the old per-target
    polling state (one {last_checkin_id, catchup_max_id, ...} per watched
    username, from before get_my_friend_feed existed and every target had
    to be polled individually) into the new shared-feed shape. There's no
    single correct shared cursor to derive from several independent old
    per-target ones, so the feed just bootstraps fresh on migration (same
    "don't retroactively toast" rule as a brand-new target) - only the
    cumulative toast counts are worth carrying forward."""
    entry.setdefault("targets", [])
    entry.setdefault("excludedCountries", [])
    entry.setdefault("enabled", True)
    # Defaults True (not False) even for pre-existing entries: this filter
    # was added specifically because auto-toast was firing on Non-Alcoholic/
    # RTD/Spirit/Wine check-ins nobody wanted toasted, so the safe default
    # for a migrating entry is "on", not "unchanged old behavior".
    entry.setdefault("legacyOnly", True)
    entry.setdefault("feed", {})
    if "state" in entry:
        old_state = entry.pop("state")
        stats = entry.setdefault("stats", {})
        for username, st in old_state.items():
            stats.setdefault(username, {})["total_toasted"] = st.get("total_toasted", 0)
    entry.setdefault("stats", {})
    return entry


def _owner_entry(data: dict, owner_id: int) -> dict:
    entry = data.setdefault(str(owner_id), {})
    return _migrate_entry(entry)


async def get_config(owner_id: int) -> dict:
    async with _lock:
        data = _load()
        entry = _migrate_entry(dict(data.get(str(owner_id)) or {}))
        return {
            "targets": list(entry["targets"]),
            "excludedCountries": list(entry["excludedCountries"]),
            "enabled": entry["enabled"],
            "legacyOnly": entry["legacyOnly"],
            "feed": dict(entry["feed"]),
            "stats": {u: dict(s) for u, s in entry["stats"].items()},
        }


async def set_enabled(owner_id: int, enabled: bool) -> None:
    """Pause/resume - e.g. during a festival, where every toast_checkin
    competes with the same per-token quota that real check-ins need most.
    Deliberately keeps `targets`/`stats` untouched (unlike remove_target) -
    this is a temporary toggle, not "forget who I was watching"."""
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["enabled"] = enabled
        _save(data)


async def set_legacy_only(owner_id: int, legacy_only: bool) -> None:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["legacyOnly"] = legacy_only
        _save(data)


async def add_targets(owner_id: int, usernames: list[str]) -> list[str]:
    """Returns the usernames actually newly added (dedupe against what was
    already there, case-insensitively - Untappd usernames aren't case
    sensitive)."""
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        existing_lower = {u.lower() for u in entry["targets"]}
        added = []
        for username in usernames:
            username = username.strip().lstrip("@")
            if not username or username.lower() in existing_lower:
                continue
            entry["targets"].append(username)
            existing_lower.add(username.lower())
            added.append(username)
        if added:
            _save(data)
        return added


async def remove_target(owner_id: int, username: str) -> bool:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        before = len(entry["targets"])
        entry["targets"] = [u for u in entry["targets"] if u.lower() != username.strip().lower()]
        entry["stats"].pop(username.strip(), None)
        if len(entry["targets"]) == before:
            return False
        _save(data)
        return True


async def set_targets(owner_id: int, usernames: list[str]) -> None:
    """Full replace, driven by the Mini App's checkbox UI (unlike
    add_targets/remove_target, which are incremental and bot-command
    driven). Existing per-target stats (total_toasted) carry over
    case-insensitively for anyone who stays checked; anyone left unchecked
    loses their count entirely (same as remove_target). Unlike the old
    per-target-cursor design, this never affects the shared feed cursor -
    toggling someone on/off no longer risks re-bootstrapping or disrupting
    anyone else's polling."""
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        old_stats_by_lower = {k.lower(): v for k, v in entry["stats"].items()}
        clean: list[str] = []
        seen_lower = set()
        new_stats = {}
        for username in usernames:
            username = str(username).strip().lstrip("@")
            if not username or username.lower() in seen_lower:
                continue
            seen_lower.add(username.lower())
            clean.append(username)
            if username.lower() in old_stats_by_lower:
                new_stats[username] = old_stats_by_lower[username.lower()]
        entry["targets"] = clean
        entry["stats"] = new_stats
        _save(data)


async def set_excluded_countries(owner_id: int, keys: list[str]) -> None:
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        entry["excludedCountries"] = keys
        _save(data)


async def exclude_country(owner_id: int, text: str) -> str:
    """Returns the canonical/normalized key that was added (or already
    present)."""
    key = normalize_country_input(text)
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        if key not in entry["excludedCountries"]:
            entry["excludedCountries"].append(key)
            _save(data)
    return key


async def include_country(owner_id: int, text: str) -> bool:
    key = normalize_country_input(text)
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        if key not in entry["excludedCountries"]:
            return False
        entry["excludedCountries"] = [k for k in entry["excludedCountries"] if k != key]
        _save(data)
        return True


# Identity of the last owner actually served, not a positional index - same
# reasoning as the old per-target rotation's fix (see git history): a bare
# index breaks the moment the underlying list is reordered/resized between
# calls. With only one real owner today this mostly matters for whenever a
# second one shows up.
_last_served_owner: int | None = None

# Sentinel distinguishing "leave this field alone" from "set it to None" in
# record_feed_tick - plain None is a real, meaningful value for
# catchup_max_id/catchup_target_id (it means "not mid a catch-up walk"), so
# it can't also mean "don't touch this field."
_UNSET = object()


class FeedTurn:
    """One owner's shared-feed polling state, as handed to the loop by
    peek_owner_turn(). last_checkin_id is the confirmed-fully-processed
    low-water mark for this owner's WHOLE friend feed (not per-target); it's
    None only before this owner's very first poll ever. catchup_max_id/
    catchup_target_id together track an in-progress multi-tick backward
    walk started when a single page (50 items - get_my_friend_feed's own
    per-call ceiling) wasn't enough to reach back down to last_checkin_id in
    one go - e.g. several friends checking in in a burst, or one bulk-
    importing years of history. Both are None outside of such a walk."""

    __slots__ = ("owner_id", "last_checkin_id", "catchup_max_id", "catchup_target_id")

    def __init__(self, owner_id, last_checkin_id, catchup_max_id, catchup_target_id):
        self.owner_id = owner_id
        self.last_checkin_id = last_checkin_id
        self.catchup_max_id = catchup_max_id
        self.catchup_target_id = catchup_target_id


def _enabled_owners(data: dict) -> list[int]:
    owners = []
    for owner_id_str, entry in data.items():
        entry = _migrate_entry(entry)
        if entry["enabled"] and entry["targets"]:
            owners.append(int(owner_id_str))
    return owners


def _owner_index_after(owners: list[int], last_served: int | None) -> int:
    if last_served is None or last_served not in owners:
        return 0
    return (owners.index(last_served) + 1) % len(owners)


async def peek_owner_turn() -> FeedTurn | None:
    """Returns the next owner in rotation WITHOUT consuming it - call
    advance_owner_turn() once you've actually acted on it (polled it, or
    decided it's genuinely unusable, e.g. no token). Split from a combined
    call for the same reason the old per-target version was: so the caller
    can check "can I actually service this right now" (e.g. does this
    owner's token have enough quota) *before* committing to it - see
    webapp_server.py's _auto_toast_loop."""
    async with _lock:
        data = _load()
        owners = _enabled_owners(data)
        if not owners:
            return None
        idx = _owner_index_after(owners, _last_served_owner)
        owner_id = owners[idx]
        feed = _migrate_entry(data[str(owner_id)])["feed"]
        return FeedTurn(
            owner_id,
            feed.get("last_checkin_id"),
            feed.get("catchup_max_id"),
            feed.get("catchup_target_id"),
        )


async def advance_owner_turn() -> None:
    """Marks whatever peek_owner_turn() most recently returned as served.
    Do NOT call this just because quota was briefly insufficient - see
    peek_owner_turn's docstring."""
    global _last_served_owner
    async with _lock:
        data = _load()
        owners = _enabled_owners(data)
        if not owners:
            return
        idx = _owner_index_after(owners, _last_served_owner)
        _last_served_owner = owners[idx]


async def record_feed_tick(
    owner_id: int, *,
    last_checkin_id=_UNSET, catchup_max_id=_UNSET, catchup_target_id=_UNSET,
    toasted: dict[str, int] | None = None, error: str | None = None,
) -> None:
    """Persists one feed-poll tick's outcome. The three cursor fields default
    to "leave unchanged" (pass an explicit value, including None, to set
    one) - a rate-limited tick that wants to retry the exact same page next
    time calls this with all three left at their default. `toasted` is
    {username: count} - a single feed tick can toast several different
    targets at once (unlike the old per-target design), so counts are
    credited per name, not as one lump sum."""
    async with _lock:
        data = _load()
        entry = _owner_entry(data, owner_id)
        feed = entry["feed"]
        if last_checkin_id is not _UNSET:
            feed["last_checkin_id"] = last_checkin_id
        if catchup_max_id is not _UNSET:
            feed["catchup_max_id"] = catchup_max_id
        if catchup_target_id is not _UNSET:
            feed["catchup_target_id"] = catchup_target_id
        feed["last_polled_at"] = time.time()
        feed["last_error"] = error
        for username, count in (toasted or {}).items():
            stats = entry["stats"].setdefault(username, {})
            stats["total_toasted"] = stats.get("total_toasted", 0) + count
        _save(data)
