"""Real Untappd badge ("type_pack") progress, computed purely from
had_it_index's/venue_index's already-synced per-beer/per-venue data - no
live Untappd calls, no extra quota.

Catalogs are badge_beer_categories.json (style/country) and
badge_venue_categories.json (venue category), static bundled reference
files manually researched from https://badges.untappd.com/ (see each
file's own _source/_note for citation and exclusions) - loaded eagerly at
import time, same convention webapp_server.py already uses for the latter.
"""

import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

_style_badges: list[dict] = []
_country_badges: list[dict] = []
_distinct_badges: list[dict] = []
_range_badges: list[dict] = []
_venue_badges: list[dict] = []
_special_badges: list[dict] = []

try:
    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "badge_beer_categories.json"),
        encoding="utf-8",
    ) as _f:
        _catalog = json.load(_f)
    _style_badges = _catalog.get("style_badges", [])
    _country_badges = _catalog.get("country_badges", [])
    _distinct_badges = _catalog.get("distinct_badges", [])
    _range_badges = _catalog.get("range_badges", [])
except (OSError, json.JSONDecodeError) as _e:
    logger.warning("Could not load badge_beer_categories.json: %s", _e)

try:
    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "badge_venue_categories.json"),
        encoding="utf-8",
    ) as _vf:
        _venue_catalog = json.load(_vf)
    _venue_badges = _venue_catalog.get("badges", [])
except (OSError, json.JSONDecodeError) as _e:
    logger.warning("Could not load badge_venue_categories.json: %s", _e)

try:
    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "special_badges.json"),
        encoding="utf-8",
    ) as _sf:
        _special_catalog = json.load(_sf)
    _special_badges = _special_catalog.get("special_badges", [])
except (OSError, json.JSONDecodeError) as _e:
    logger.warning("Could not load special_badges.json: %s", _e)

# Deprecated/retired Untappd style names found in real had_it_index data,
# each confirmed absent from Untappd's own current style dropdown (live,
# via untappd.com/beer/top_rated's style <select>) - used only by
# compute_progress's "style" distinct-count branch (Wheel of Styles) to
# fold a stale string into its current equivalent instead of inflating the
# count as its own separate, phantom style. A value of None means "not a
# real style at all, drop it" rather than "rename to X". Expected to grow
# over time as more of these turn up - not an exhaustive list, just the
# ones confirmed live so far.
_STYLE_ALIASES: dict[str, str | None] = {
    "Lager - Euro Pale": "Lager - Pale",
    "Other": None,
}


def _tier(current: int, count: int, levels: int, first_level_count: int | None = None) -> tuple[int, int, int | None]:
    """(achieved_level, level_start, next_threshold) for a repeating "N
    distinct qualifying beers per level" badge - level 1 needs `count`
    (or `first_level_count` for the "Super Style" badges, where the very
    first qualifying beer alone earns level 1), each level after that
    needs `count` more. `level_start` is the cumulative count already
    banked at the start of the CURRENT level - needed so the progress bar
    shows progress *within this level* (e.g. 4/5 toward level 72) rather
    than the cumulative total against the next absolute threshold (which
    for a high level looks misleadingly ~100% full even 4 beers into a
    5-beer step). next_threshold is None once the catalog's level cap is
    reached - a soft ceiling, not a realistic finish line."""
    first = first_level_count or count
    if current < first:
        return 0, 0, first
    level = 1 + (current - first) // count
    level_start = first + (level - 1) * count
    if level >= levels:
        return levels, level_start, None
    return level, level_start, level_start + count


def _split_style_matchers(styles: list[str]) -> tuple[set[str], list[str]]:
    """Splits a catalog styles[] list into (exact, substring) matchers. A
    catalog style already shaped like a complete real "Category -
    Subcategory" leaf name (i.e. it contains " - ") is ALWAYS treated as
    EXACT match, whether or not it carries an explicit "=" prefix (that
    prefix still works too, kept for bare-word entries someone wants exact
    for some other reason) - proven live TWICE independently: "Session
    Life" showed level 93 instead of the real 86 (40 extra beers from the
    different real style "IPA - Session New England / Hazy" substring-
    matching catalog style "IPA - Session"), and "Tripping on TIPAs"
    showed level 71 instead of a real ~18 (269 of 358 counted beers were
    "IPA - Triple New England / Hazy", a distinct sibling leaf style, not
    a "Triple" sub-variant) - both are the SAME general pattern: a
    "Category - Subcategory" string is always a complete, specific leaf in
    Untappd's real two-level taxonomy, never a prefix another leaf
    legitimately extends, so substring-matching it can only ever pick up
    an unrelated sibling by accident. Genuinely broad/umbrella catalog
    entries (bare words with no " - ", like "Stout", "Non-Alcoholic",
    "Imperial / Double") keep matching via substring, which is the
    correct, intended behavior for those - they're deliberately not a
    full leaf name."""
    exact: set[str] = set()
    substring: list[str] = []
    for s in styles:
        if s.startswith("="):
            exact.add(s[1:].lower())
        elif " - " in s:
            exact.add(s.lower())
        else:
            substring.append(s.lower())
    return exact, substring


def _style_matches(style: str, exact: set[str], substring: list[str]) -> bool:
    style_lower = style.lower()
    if style_lower in exact:
        return True
    return any(w in style_lower for w in substring)


def compute_progress(beers: dict, is_supporter: bool = False) -> list[dict]:
    """`beers` is one had_it_index user entry's `beers` dict ({beer_id: {rating,
    style, brewery, country}}, see had_it_index.get_all_beers). Returns one
    row per catalog badge with this user's current count and next-level
    threshold - unsorted, caller sorts for display. The catalog's
    subscription-gated "Super Style: ..." badges (marked by having
    `first_level_count`) are only included when `is_supporter` is true -
    other connected accounts may well have an active Untappd Insiders
    subscription even though this app's own owner doesn't, so this is a
    per-viewer check (user_tokens' cached is_supporter, refreshed from each
    check-in's own embedded status - see webapp_server's venue backfill
    loop), never a blanket skip for everyone."""
    rows = []
    for badge in _style_badges:
        if badge.get("first_level_count") and not is_supporter:
            continue
        raw_styles = badge.get("styles", [])
        exact, substring = _split_style_matchers(raw_styles)
        # match_name: a handful of badges (flagged in the catalog itself,
        # e.g. Winter Wonderland) are a themed KEYWORD match against the
        # beer's own product name, not a formal style category at all - see
        # that badge's own "note" field. had_it_index only started
        # capturing `name` once this was added, so an old record can be
        # missing it entirely even after a full resync predates the change;
        # treated the same as a missing style (just doesn't match yet,
        # self-heals on the next sync that touches it).
        match_name = bool(badge.get("matchName"))
        # match_brewery/match_brewery_type: same reuse of the style matcher
        # for badges keyed off which BREWERY made the beer rather than its
        # style - Trappist Travesty (exact brewery-name list, see the
        # catalog's own "=" prefixes) and Home Brewed Goodness (a single
        # brewery_type value, "Home Brewery"). Same self-healing story as
        # matchName: an old had_it_index record predating breweryType
        # capture just doesn't match yet, catches up on its next resync.
        match_brewery = bool(badge.get("matchBrewery"))
        match_brewery_type = bool(badge.get("matchBreweryType"))
        current = sum(
            1 for b in beers.values()
            if ((style := b.get("style")) and _style_matches(style, exact, substring))
            or (match_name and (name := b.get("name")) and _style_matches(name, exact, substring))
            or (match_brewery and (brewery := b.get("brewery")) and _style_matches(brewery, exact, substring))
            or (match_brewery_type and (btype := b.get("breweryType")) and _style_matches(btype, exact, substring))
        )
        # Display tags never leak the "=" marker - it's an internal matching
        # hint, not part of the style name shown to the reader.
        display_tags = [s[1:] if s.startswith("=") else s for s in raw_styles]
        rows.append(_row(badge, current, "style", display_tags))
    for badge in _country_badges:
        wanted = {c.lower() for c in badge.get("countries", [])}
        current = sum(1 for b in beers.values() if (b.get("country") or "").lower() in wanted)
        rows.append(_row(badge, current, "country", badge.get("countries", [])))
    return rows


def _beer_region_key(country: str | None, state: str | None) -> str | None:
    """"{state}, {country}" when a state/province is known, else just
    "{country}" - confirmed live against "Beer of the World"'s own real
    "Your Regions List" (e.g. "Plzeň Region, Czech Republic" vs a bare
    "Iran" for a brewery whose state Untappd itself never had). Every
    country gets this sub-national breakdown, not just US/Canada/Mexico
    like Brew Traveler's own narrower rule - a DIFFERENT badge, a
    DIFFERENT region granularity, proven live to genuinely differ (the
    same regions list showed 3 separate Cape Verde entries, not one)."""
    if not country:
        return None
    country = country.strip()
    if not country:
        return None
    if state and state.strip():
        return f"{state.strip()}, {country}"
    return country


def compute_distinct_progress(beers: dict) -> list[dict]:
    """Badges that count the number of DISTINCT values of some beer field
    the user has ever had - a genuinely different shape from
    compute_progress's style/country matching (membership in a fixed
    catalog list): Wheel of Styles (distinct styles), Beer Connoisseur
    (distinct countries), Brewery Pioneer (distinct breweries), Beer of the
    World (distinct state+country regions - see _beer_region_key). No
    "tags" to show (there's no fixed list to display), and no exact/
    substring matching involved at all."""
    rows = []
    for badge in _distinct_badges:
        field = badge.get("field")
        if field == "region":
            values = {
                key for b in beers.values()
                if (key := _beer_region_key(b.get("country"), b.get("state"))) is not None
            }
        elif field == "style":
            # Wheel of Styles counts DISTINCT style strings ever recorded -
            # a beer's had_it_index record keeps whatever raw beer_style
            # Untappd returned AT SYNC TIME and never revisits it once set
            # (had_it_index.py's own non-destructive merge convention), so
            # an old check-in can carry a style name Untappd has since
            # renamed/retired - confirmed live for this exact badge: local
            # count of 300 distinct strings vs. Untappd's own reported
            # level 57 (285-289 styles) included "Lager - Euro Pale" (no
            # longer in Untappd's current style list at all - merges into
            # today's "Lager - Pale", which this account already has as
            # its own separate entry) and a bare "Other" (not a real
            # style in Untappd's current taxonomy either - every current
            # category is its own "X - Other", never a bare catch-all).
            # Both are almost certainly stub records whose style field was
            # set once from a stale source and never refreshed - see
            # _STYLE_ALIASES's own note.
            values = {
                v for b in beers.values()
                if (raw := b.get(field)) and (v := _STYLE_ALIASES.get(raw, raw)) is not None
            }
        else:
            values = {v for b in beers.values() if (v := b.get(field))}
        rows.append(_row(badge, len(values), "distinct", []))
    return rows


def compute_range_progress(beers: dict) -> list[dict]:
    """ABV/IBU-window badges (Riding Steady/Sky's the Limit on abv, Hopped
    Down/Hopped Up on ibu, Middle of the Road on abv) - counts beers whose
    numeric `field` value falls within [min, max] (either bound optional,
    e.g. Sky's the Limit is abv >= 10 with no upper bound). IBU in
    particular is frequently absent from Untappd's own data (not every beer
    has it recorded) - a beer missing the field just never counts, same
    "self-heals as data fills in" story as every other field here, not an
    error."""
    rows = []
    for badge in _range_badges:
        field = badge.get("field")
        lo = badge.get("min")
        hi = badge.get("max")
        current = sum(
            1 for b in beers.values()
            if (v := b.get(field)) is not None
            and (lo is None or v >= lo)
            and (hi is None or v <= hi)
        )
        rows.append(_row(badge, current, "range", []))
    return rows


def _special_badge_active(badge: dict, today: datetime.date) -> bool:
    try:
        start = datetime.date.fromisoformat(badge["activeFrom"])
        end = datetime.date.fromisoformat(badge["activeUntil"])
    except (KeyError, ValueError):
        return False
    return start <= today <= end


def compute_special_badges(beers: dict, today: datetime.date | None = None) -> list[dict]:
    """Untappd's own time-limited promotional badges (special_badges.json,
    hand-curated from untappd.com/blog's "New Badge: X" posts - see that
    file's own _source note) - never in the permanent badges.untappd.com
    catalog, so there's no static reference page to sync against; the
    catalog itself has to be re-checked and re-curated by hand as new
    posts appear.

    Unlike every other compute_* function here, this ISN'T a progress/
    level readout - had_it_index only ever records "have I EVER had this
    beer", never WHEN, so there's no local way to tell whether an
    already-known matching beer was actually checked in inside the
    badge's specific date window. Untappd's own backend knows that and
    awards the real badge independently the moment a qualifying check-in
    happens - this function's only job is RECOMMENDING what to go check
    in for a currently-active badge, not tracking completion.

    Returns only badges currently active (today within [activeFrom,
    activeUntil]), each with up to 5 `matchingKnownBeers` already in this
    user's own had_it_index that satisfy the style/country criteria -
    purely "here's something you already know you like that would
    count" inspiration, not a claim they've already earned it."""
    today = today or datetime.date.today()
    rows = []
    for badge in _special_badges:
        if not _special_badge_active(badge, today):
            continue
        kind = badge.get("kind")
        matches: list[dict] = []
        if kind == "style":
            exact, substring = _split_style_matchers(badge.get("styles", []))
            matches = [
                b for b in beers.values()
                if (style := b.get("style")) and _style_matches(style, exact, substring)
            ]
        elif kind == "country":
            countries = {c.lower() for c in badge.get("countries", [])}
            matches = [b for b in beers.values() if (b.get("country") or "").lower() in countries]
        end = datetime.date.fromisoformat(badge["activeUntil"])
        rows.append({
            "badge": badge["badge"],
            "icon": badge.get("icon"),
            "kind": kind,
            # "=Exact" markers (see _split_style_matchers) are an internal
            # matching detail - stripped here so the UI shows the plain
            # style name a person actually recognizes.
            "styles": [s.lstrip("=") for s in badge["styles"]] if kind == "style" else None,
            "countries": badge.get("countries") if kind == "country" else None,
            "venueChain": badge.get("venueChain"),
            "activeFrom": badge["activeFrom"],
            "activeUntil": badge["activeUntil"],
            "daysRemaining": (end - today).days,
            "note": badge.get("note"),
            "sourceUrl": badge.get("sourceUrl"),
            "matchingKnownBeers": [
                {"name": b.get("name"), "brewery": b.get("brewery")}
                for b in matches[:5] if b.get("name")
            ],
        })
    return rows


# "Brew Crawl" (3 different Bar check-ins in ONE NIGHT) and "Last Call" (3
# brews after 1AM on a Fri/Sat at ONE venue) are the catalog's only two
# same-occasion/time-window venue badges (see their own "note" fields in
# badge_venue_categories.json) - everything else is a plain lifetime
# distinct-venue count. venue_index.py only tracks *whether* a venue was
# ever visited, not per-check-in timestamps, so there's no way to compute
# these two correctly - showing a lifetime distinct-Bar-visit count as
# "Brew Crawl progress" would be actively misleading (the same mistake as
# the Super Style/cumulative-progress-bar issues above), so they're
# excluded rather than shown with wrong numbers.
_VENUE_BADGES_NOT_COMPUTABLE = {"Brew Crawl", "Last Call"}

# "Brew Traveler" pools two different kinds of geographic unit into one
# count: for the US/Canada/Mexico specifically, each distinct STATE/
# PROVINCE counts on its own (proven live via the badge's own "How to Earn
# It" text and community documentation - checking in twice in different US
# states counts as 2, not 1 "United States"), while every other country
# counts once as a whole regardless of how many of its own regions were
# visited. venue_country comes back localized to wherever the venue itself
# is (a Polish venue says "Polska", confirmed live) - matched here against
# each of these 3 countries' own local-language name, not a full
# translation table, since a US/CA/MX venue's own country field should say
# so in ITS OWN local language same as everywhere else. Add more spellings
# here if a real account's data ever needs one this list is missing (same
# "hand-maintained, expand as new ones turn up" spirit as beer_match.py's
# own lookup tables).
_STATE_LEVEL_COUNTRIES = {
    "united states", "usa", "us", "united states of america", "u.s.", "u.s.a.",
    "canada",
    "mexico", "méxico",
}


def _travel_region_key(country: str | None, state: str | None) -> str | None:
    if not country:
        return None
    country_key = country.strip().lower()
    if not country_key:
        return None
    if country_key in _STATE_LEVEL_COUNTRIES and state and state.strip():
        return f"{country_key}:{state.strip().lower()}"
    return country_key


def compute_venue_progress(
    visited_categories: list[list[str]],
    visited_regions: list[tuple[str | None, str | None]] | None = None,
) -> list[dict]:
    """`visited_categories` is one venue_index user entry's list of per-venue
    category-name lists (see venue_index.get_visited_venue_categories) - one
    entry per DISTINCT visited venue, so a badge's progress here counts
    distinct qualifying venues, not check-ins (matching how these badges are
    actually earned - checking in 5 times at the same brewery doesn't count
    5x toward a "visit 5 breweries" badge). Skips catalog entries that don't
    yet have a "count" threshold (older badge_venue_categories.json entries,
    added before that field existed for the live-search "badge only" filter,
    back-filled separately - see that file's own _note) rather than showing
    them as permanently stuck at 0.

    visited_regions (see venue_index.get_visited_regions) is a SEPARATE
    per-venue list, only consulted for a badge flagged "regionBased" in the
    catalog (currently just "Brew Traveler") - a plain category match
    doesn't apply to it at all, it needs its own distinct-region count (see
    _travel_region_key)."""
    rows = []
    lowered_venues = [{c.lower() for c in cats} for cats in visited_categories]
    for badge in _venue_badges:
        if badge.get("regionBased"):
            if "count" not in badge:
                continue
            regions = {
                key for country, state in (visited_regions or [])
                if (key := _travel_region_key(country, state)) is not None
            }
            rows.append(_row(badge, len(regions), "venue", []))
            continue
        if "count" not in badge or badge["badge"] in _VENUE_BADGES_NOT_COMPUTABLE:
            continue
        wanted = {c.lower() for c in badge.get("categories", [])}
        current = sum(1 for cats in lowered_venues if cats & wanted)
        rows.append(_row(badge, current, "venue", badge.get("categories", [])))
    return rows


def badges_matching_beer(rows: list[dict], style: str, country: str, beer_name: str = "") -> list[dict]:
    """Given already-computed `rows` (from compute_progress) and a
    hypothetical new beer's style/country/name, return the NOT-DONE style/
    country rows this beer would count toward - for /scan's "should I get
    this" check. Re-matches against the raw catalog matchers (_style_badges/
    _country_badges) via _style_matches rather than against rows' own
    already-"="-stripped `tags`, so the exact-vs-substring distinction isn't
    silently lost (see _split_style_matchers's own note on why that
    distinction matters). beer_name only matters for the same handful of
    "matchName" keyword badges compute_progress special-cases (see its own
    comment) - optional since most callers only ever cared about style/
    country before this existed. Venue badges are never included - a
    store-shelf scan has no venue/check-in context."""
    not_done_by_name = {
        r["name"]: r for r in rows
        if r.get("kind") in ("style", "country") and not r.get("done")
    }
    matches: list[dict] = []
    matched_badge_names: set[str] = set()

    style_norm = (style or "").strip()
    name_norm = (beer_name or "").strip()
    if style_norm or name_norm:
        for badge in _style_badges:
            row = not_done_by_name.get(badge["badge"])
            if not row:
                continue
            exact, substring = _split_style_matchers(badge.get("styles", []))
            hit = (style_norm and _style_matches(style_norm, exact, substring)) or (
                badge.get("matchName") and name_norm and _style_matches(name_norm, exact, substring)
            )
            if hit and badge["badge"] not in matched_badge_names:
                matches.append(row)
                matched_badge_names.add(badge["badge"])

    country_norm = (country or "").strip().lower()
    if country_norm:
        for badge in _country_badges:
            row = not_done_by_name.get(badge["badge"])
            if not row:
                continue
            wanted = {c.lower() for c in badge.get("countries", [])}
            if country_norm in wanted:
                matches.append(row)

    return matches


def _row(badge: dict, current: int, kind: str, tags: list[str]) -> dict:
    level, level_start, next_threshold = _tier(
        current, badge["count"], badge.get("levels", 1), badge.get("first_level_count"),
    )
    span = (next_threshold - level_start) if next_threshold else 0
    pct = 100 if next_threshold is None else round(100 * (current - level_start) / span) if span else 100
    return {
        "name": badge["badge"],
        "icon": badge.get("icon"),
        "current": current,
        "nextThreshold": next_threshold,
        "pct": pct,
        "level": level,
        "levelLabel": f"рівень {level}" if level else None,
        "done": next_threshold is None,
        # For the detail screen's search-by-tag and "how to earn it" text -
        # kind says which of tags/countPerLevel means ("N distinct beers of
        # style X" vs "...from country X" vs "...venues categorized X").
        # url is a badges.untappd.com link, filled in only once catalogued
        # (see badge_beer_categories.json/badge_venue_categories.json's own
        # _note) - None means "not yet researched", not "doesn't exist".
        "kind": kind,
        "tags": tags,
        "countPerLevel": badge["count"],
        "levels": badge.get("levels", 1),
        "firstLevelCount": badge.get("first_level_count"),
        "url": badge.get("url"),
    }


def level_floor(count: int, levels: int, level: int, first_level_count: int | None = None) -> tuple[int, int | None]:
    """(level_start, next_threshold) for a GIVEN confirmed level, the
    inverse of _tier's own math - used by webapp_server.py's
    handle_badges_get when badge_index.py's ground-truth level (straight
    from Untappd's own check-in data) is higher than what our own style/
    country counting computed: we can't know the real current count behind
    that confirmed level, only that it's at least level_start, so pct is
    shown as 0 (just reached) rather than fabricating a fraction our own
    undercounted `current` can't support."""
    first = first_level_count or count
    if level <= 0:
        return 0, first
    level_start = first + (level - 1) * count
    next_threshold = None if level >= levels else level_start + count
    return level_start, next_threshold
