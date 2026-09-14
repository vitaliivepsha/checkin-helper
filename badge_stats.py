"""Real Untappd badge ("type_pack") progress, computed purely from
had_it_index's/venue_index's already-synced per-beer/per-venue data - no
live Untappd calls, no extra quota.

Catalogs are badge_beer_categories.json (style/country) and
badge_venue_categories.json (venue category), static bundled reference
files manually researched from https://badges.untappd.com/ (see each
file's own _source/_note for citation and exclusions) - loaded eagerly at
import time, same convention webapp_server.py already uses for the latter.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_style_badges: list[dict] = []
_country_badges: list[dict] = []
_venue_badges: list[dict] = []

try:
    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "badge_beer_categories.json"),
        encoding="utf-8",
    ) as _f:
        _catalog = json.load(_f)
    _style_badges = _catalog.get("style_badges", [])
    _country_badges = _catalog.get("country_badges", [])
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
    "=" prefix means EXACT match only - see badge_beer_categories.json's
    own _note: a catalog style already shaped like a complete real
    "Category - Subcategory" leaf name (e.g. "IPA - Session") can otherwise
    also substring-match a DIFFERENT, more specific sibling real style that
    happens to extend it with extra words (e.g. "IPA - Session New England
    / Hazy" - a distinct leaf style, not a "Session" sub-variant) - proven
    live (Session Life showed level 93 instead of the real 86 from exactly
    this). Genuinely broad/umbrella catalog entries (bare words like
    "Stout", "Imperial / Double") are never marked "=" and keep matching
    via substring, which is the correct, intended behavior for those."""
    exact: set[str] = set()
    substring: list[str] = []
    for s in styles:
        if s.startswith("="):
            exact.add(s[1:].lower())
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
        current = sum(
            1 for b in beers.values()
            if (style := b.get("style")) and _style_matches(style, exact, substring)
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


def compute_venue_progress(visited_categories: list[list[str]]) -> list[dict]:
    """`visited_categories` is one venue_index user entry's list of per-venue
    category-name lists (see venue_index.get_visited_venue_categories) - one
    entry per DISTINCT visited venue, so a badge's progress here counts
    distinct qualifying venues, not check-ins (matching how these badges are
    actually earned - checking in 5 times at the same brewery doesn't count
    5x toward a "visit 5 breweries" badge). Skips catalog entries that don't
    yet have a "count" threshold (older badge_venue_categories.json entries,
    added before that field existed for the live-search "badge only" filter,
    back-filled separately - see that file's own _note) rather than showing
    them as permanently stuck at 0."""
    rows = []
    lowered_venues = [{c.lower() for c in cats} for cats in visited_categories]
    for badge in _venue_badges:
        if "count" not in badge or badge["badge"] in _VENUE_BADGES_NOT_COMPUTABLE:
            continue
        wanted = {c.lower() for c in badge.get("categories", [])}
        current = sum(1 for cats in lowered_venues if cats & wanted)
        rows.append(_row(badge, current, "venue", badge.get("categories", [])))
    return rows


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
        "url": badge.get("url"),
    }
