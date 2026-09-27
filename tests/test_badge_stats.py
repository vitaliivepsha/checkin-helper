"""Unit tests for badge_stats.py's pure compute_* functions.

Every test monkeypatches the module-level catalog lists (_style_badges,
_country_badges, etc.) with small synthetic fixtures instead of relying on
the real badge_beer_categories.json/badge_venue_categories.json/
special_badges.json content - that keeps these tests exercising the MATH
and matching rules (which is what actually broke, repeatedly, in this
project's history - see _split_style_matchers' own docstring for two real
live regressions) rather than today's catalog data, which changes often
and independently.
"""

import datetime

import pytest

import badge_stats


# ---------------------------------------------------------------------------
# _tier / level_floor - the core level-math shared by every progress badge
# ---------------------------------------------------------------------------

def test_tier_before_first_level():
    assert badge_stats._tier(0, count=5, levels=100) == (0, 0, 5)
    assert badge_stats._tier(4, count=5, levels=100) == (0, 0, 5)


def test_tier_reaches_level_1():
    assert badge_stats._tier(5, count=5, levels=100) == (1, 5, 10)
    assert badge_stats._tier(9, count=5, levels=100) == (1, 5, 10)


def test_tier_mid_levels():
    assert badge_stats._tier(17, count=5, levels=100) == (3, 15, 20)


def test_tier_caps_at_max_level():
    level, level_start, next_threshold = badge_stats._tier(10_000, count=5, levels=100)
    assert level == 100
    assert next_threshold is None


def test_tier_first_level_count_super_style():
    # "Super Style" badges: the very first qualifying beer earns level 1.
    assert badge_stats._tier(1, count=5, levels=100, first_level_count=1) == (1, 1, 6)
    assert badge_stats._tier(0, count=5, levels=100, first_level_count=1) == (0, 0, 1)


def test_level_floor_is_tier_inverse_at_level_boundaries():
    for level in (1, 2, 3, 57, 99, 100):
        level_start, next_threshold = badge_stats.level_floor(count=5, levels=100, level=level)
        tier_level, tier_level_start, tier_next = badge_stats._tier(level_start, count=5, levels=100)
        assert tier_level == level
        assert tier_level_start == level_start
        assert tier_next == next_threshold


def test_level_floor_level_zero():
    assert badge_stats.level_floor(count=5, levels=100, level=0) == (0, 5)


def test_level_floor_first_level_count():
    assert badge_stats.level_floor(count=5, levels=100, level=1, first_level_count=1) == (1, 6)


# ---------------------------------------------------------------------------
# _split_style_matchers / _style_matches
# ---------------------------------------------------------------------------

def test_split_style_matchers_compound_name_is_always_exact():
    # "IPA - Session" (no "=") must still NOT substring-match "IPA - Session
    # New England / Hazy" - the exact live regression this function exists
    # to prevent (see its own docstring: "Session Life" showed level 93
    # instead of 86 for exactly this reason).
    exact, substring = badge_stats._split_style_matchers(["IPA - Session"])
    assert exact == {"ipa - session"}
    assert substring == []
    assert not badge_stats._style_matches("IPA - Session New England / Hazy", exact, substring)
    assert badge_stats._style_matches("IPA - Session", exact, substring)


def test_split_style_matchers_bare_word_is_substring():
    exact, substring = badge_stats._split_style_matchers(["Stout"])
    assert exact == set()
    assert substring == ["stout"]
    assert badge_stats._style_matches("Stout - Imperial / Double Pastry", exact, substring)


def test_split_style_matchers_equals_prefix_forces_exact():
    exact, substring = badge_stats._split_style_matchers(["=Gose"])
    assert exact == {"gose"}
    assert substring == []
    assert badge_stats._style_matches("Gose", exact, substring)
    assert not badge_stats._style_matches("Sour - Fruited Gose", exact, substring)


def test_style_matches_is_case_insensitive():
    exact, substring = badge_stats._split_style_matchers(["=Märzen"])
    assert badge_stats._style_matches("märzen", exact, substring)
    assert badge_stats._style_matches("MÄRZEN", exact, substring)


# ---------------------------------------------------------------------------
# compute_progress - style/country catalog badges
# ---------------------------------------------------------------------------

def test_compute_progress_style_counts_matching_beers(monkeypatch):
    monkeypatch.setattr(badge_stats, "_style_badges", [
        {"badge": "Test Style Badge", "count": 5, "levels": 100, "styles": ["IPA - Session", "Stout"]},
    ])
    monkeypatch.setattr(badge_stats, "_country_badges", [])
    beers = {
        1: {"style": "IPA - Session"},
        2: {"style": "Stout - Imperial / Double Pastry"},
        3: {"style": "IPA - Session New England / Hazy"},  # must NOT count (sibling leaf style)
        4: {"style": "Lager - Pale"},  # must NOT count
    }
    rows = badge_stats.compute_progress(beers)
    assert len(rows) == 1
    assert rows[0]["current"] == 2
    assert rows[0]["name"] == "Test Style Badge"


def test_compute_progress_country_badge(monkeypatch):
    monkeypatch.setattr(badge_stats, "_style_badges", [])
    monkeypatch.setattr(badge_stats, "_country_badges", [
        {"badge": "Test Country Badge", "count": 3, "levels": 10, "countries": ["Canada", "Ukraine"]},
    ])
    beers = {1: {"country": "Canada"}, 2: {"country": "canada"}, 3: {"country": "Poland"}}
    rows = badge_stats.compute_progress(beers)
    assert rows[0]["current"] == 2


def test_compute_progress_supporter_gated_badge_hidden_by_default(monkeypatch):
    super_style = {
        "badge": "Super Style: Test", "count": 5, "levels": 100,
        "first_level_count": 1, "styles": ["IPA"],
    }
    monkeypatch.setattr(badge_stats, "_style_badges", [super_style])
    monkeypatch.setattr(badge_stats, "_country_badges", [])
    beers = {1: {"style": "IPA - American"}}
    assert badge_stats.compute_progress(beers, is_supporter=False) == []
    rows = badge_stats.compute_progress(beers, is_supporter=True)
    assert len(rows) == 1
    assert rows[0]["current"] == 1


def test_compute_progress_match_name_keyword_badge(monkeypatch):
    themed = {
        "badge": "Winter Wonderland", "count": 1, "levels": 1,
        "styles": ["=Winter"], "matchName": True,
    }
    monkeypatch.setattr(badge_stats, "_style_badges", [themed])
    monkeypatch.setattr(badge_stats, "_country_badges", [])
    beers = {1: {"style": "Stout", "name": "Winter"}}
    rows = badge_stats.compute_progress(beers)
    assert rows[0]["current"] == 1


def test_compute_progress_match_brewery_type_badge(monkeypatch):
    home_brewed = {
        "badge": "Home Brewed Goodness", "count": 1, "levels": 1,
        "styles": ["=Home Brewery"], "matchBreweryType": True,
    }
    monkeypatch.setattr(badge_stats, "_style_badges", [home_brewed])
    monkeypatch.setattr(badge_stats, "_country_badges", [])
    beers = {1: {"style": "IPA", "breweryType": "Home Brewery"}}
    rows = badge_stats.compute_progress(beers)
    assert rows[0]["current"] == 1


# ---------------------------------------------------------------------------
# compute_distinct_progress - Wheel of Styles' deprecated-alias handling
# ---------------------------------------------------------------------------

def test_compute_distinct_progress_style_field_folds_aliases(monkeypatch):
    monkeypatch.setattr(badge_stats, "_distinct_badges", [
        {"badge": "Wheel of Styles", "field": "style", "count": 5, "levels": 100},
    ])
    beers = {
        1: {"style": "Lager - Pale"},
        2: {"style": "Lager - Euro Pale"},  # aliases to "Lager - Pale" - must not add a 2nd distinct value
        3: {"style": "Other"},  # aliased to None - must be dropped entirely, not counted as its own style
        4: {"style": "IPA - American"},
    }
    rows = badge_stats.compute_distinct_progress(beers)
    assert rows[0]["current"] == 2  # "Lager - Pale" + "IPA - American"


def test_compute_distinct_progress_region_field_combines_state_and_country(monkeypatch):
    monkeypatch.setattr(badge_stats, "_distinct_badges", [
        {"badge": "Beer of the World", "field": "region", "count": 5, "levels": 100},
    ])
    beers = {
        1: {"country": "United States", "state": "California"},
        2: {"country": "United States", "state": "Oregon"},
        3: {"country": "United States", "state": "California"},  # duplicate region
        4: {"country": "Iran", "state": None},
    }
    rows = badge_stats.compute_distinct_progress(beers)
    assert rows[0]["current"] == 3  # CA, OR, Iran


def test_compute_distinct_progress_generic_field(monkeypatch):
    monkeypatch.setattr(badge_stats, "_distinct_badges", [
        {"badge": "Brewery Pioneer", "field": "brewery", "count": 5, "levels": 100},
    ])
    beers = {1: {"brewery": "A"}, 2: {"brewery": "B"}, 3: {"brewery": "A"}}
    rows = badge_stats.compute_distinct_progress(beers)
    assert rows[0]["current"] == 2


# ---------------------------------------------------------------------------
# compute_range_progress
# ---------------------------------------------------------------------------

def test_compute_range_progress_bounds(monkeypatch):
    monkeypatch.setattr(badge_stats, "_range_badges", [
        {"badge": "Sky's the Limit", "field": "abv", "min": 10, "max": None, "count": 5, "levels": 10},
    ])
    beers = {1: {"abv": 9.9}, 2: {"abv": 10.0}, 3: {"abv": 15.0}, 4: {}}
    rows = badge_stats.compute_range_progress(beers)
    assert rows[0]["current"] == 2


def test_compute_range_progress_missing_field_never_counts(monkeypatch):
    monkeypatch.setattr(badge_stats, "_range_badges", [
        {"badge": "Hopped Down", "field": "ibu", "min": None, "max": 20, "count": 5, "levels": 10},
    ])
    beers = {1: {}, 2: {"ibu": None}}
    rows = badge_stats.compute_range_progress(beers)
    assert rows[0]["current"] == 0


# ---------------------------------------------------------------------------
# compute_special_badges - special/time-limited promo badges
# ---------------------------------------------------------------------------

def test_compute_special_badges_only_active_window_included(monkeypatch):
    monkeypatch.setattr(badge_stats, "_special_badges", [
        {
            "badge": "Active One", "kind": "style", "styles": ["=Gose"],
            "activeFrom": "2026-09-01", "activeUntil": "2026-09-30",
            "sourceUrl": "https://example.com/a",
        },
        {
            "badge": "Expired One", "kind": "style", "styles": ["=Gose"],
            "activeFrom": "2026-01-01", "activeUntil": "2026-01-31",
            "sourceUrl": "https://example.com/b",
        },
    ])
    rows = badge_stats.compute_special_badges({}, today=datetime.date(2026, 9, 26))
    assert [r["badge"] for r in rows] == ["Active One"]
    assert rows[0]["daysRemaining"] == 4


def test_compute_special_badges_strips_equals_prefix_and_finds_matches(monkeypatch):
    monkeypatch.setattr(badge_stats, "_special_badges", [
        {
            "badge": "Sour Day", "kind": "style", "styles": ["=Gose", "Lambic - Gueuze"],
            "activeFrom": "2026-09-01", "activeUntil": "2026-09-30",
        },
    ])
    beers = {
        1: {"name": "Best Gose Ever", "brewery": "Test Brewery", "style": "Gose"},
        2: {"name": "Not A Match", "brewery": "Test Brewery", "style": "IPA"},
    }
    rows = badge_stats.compute_special_badges(beers, today=datetime.date(2026, 9, 15))
    assert rows[0]["styles"] == ["Gose", "Lambic - Gueuze"]  # "=" stripped for display
    assert rows[0]["matchingKnownBeers"] == [{"name": "Best Gose Ever", "brewery": "Test Brewery"}]


def test_compute_special_badges_country_kind(monkeypatch):
    monkeypatch.setattr(badge_stats, "_special_badges", [
        {
            "badge": "Canada Day", "kind": "country", "countries": ["Canada"],
            "activeFrom": "2026-09-01", "activeUntil": "2026-09-30",
        },
    ])
    beers = {1: {"country": "Canada", "name": "Canadian Beer", "brewery": "Test"}, 2: {"country": "Poland"}}
    rows = badge_stats.compute_special_badges(beers, today=datetime.date(2026, 9, 15))
    assert len(rows[0]["matchingKnownBeers"]) == 1


def test_compute_special_badges_caps_matching_beers_at_five(monkeypatch):
    monkeypatch.setattr(badge_stats, "_special_badges", [
        {
            "badge": "Many Matches", "kind": "style", "styles": ["=IPA"],
            "activeFrom": "2026-09-01", "activeUntil": "2026-09-30",
        },
    ])
    beers = {i: {"name": f"Beer {i}", "brewery": "B", "style": "IPA"} for i in range(10)}
    rows = badge_stats.compute_special_badges(beers, today=datetime.date(2026, 9, 15))
    assert len(rows[0]["matchingKnownBeers"]) == 5


# ---------------------------------------------------------------------------
# compute_venue_progress
# ---------------------------------------------------------------------------

def test_compute_venue_progress_category_count(monkeypatch):
    monkeypatch.setattr(badge_stats, "_venue_badges", [
        {"badge": "Brewery Explorer", "count": 3, "levels": 10, "categories": ["Brewery"]},
    ])
    visited = [["Brewery", "Bar"], ["Bar"], ["Brewery"]]
    rows = badge_stats.compute_venue_progress(visited)
    assert rows[0]["current"] == 2


def test_compute_venue_progress_skips_entries_without_count(monkeypatch):
    monkeypatch.setattr(badge_stats, "_venue_badges", [
        {"badge": "No Threshold Yet", "categories": ["Brewery"]},
    ])
    assert badge_stats.compute_venue_progress([["Brewery"]]) == []


def test_compute_venue_progress_skips_not_computable_badges(monkeypatch):
    monkeypatch.setattr(badge_stats, "_venue_badges", [
        {"badge": "Brew Crawl", "count": 3, "levels": 10, "categories": ["Bar"]},
    ])
    assert badge_stats.compute_venue_progress([["Bar"]]) == []


def test_compute_venue_progress_region_based_state_level_countries(monkeypatch):
    monkeypatch.setattr(badge_stats, "_venue_badges", [
        {"badge": "Brew Traveler", "count": 3, "levels": 50, "regionBased": True},
    ])
    visited_categories = [[], [], []]
    visited_regions = [
        ("United States", "California"),
        ("United States", "Oregon"),
        ("Poland", None),
        ("united states", "california"),  # same region, different case - must not double count
    ]
    rows = badge_stats.compute_venue_progress(visited_categories, visited_regions)
    assert rows[0]["current"] == 3  # CA, OR, Poland


def test_compute_venue_progress_region_based_without_count_is_skipped(monkeypatch):
    monkeypatch.setattr(badge_stats, "_venue_badges", [
        {"badge": "Brew Traveler", "regionBased": True},
    ])
    assert badge_stats.compute_venue_progress([[]], [("Canada", None)]) == []


# ---------------------------------------------------------------------------
# badges_matching_beer
# ---------------------------------------------------------------------------

def test_badges_matching_beer_only_returns_not_done_rows(monkeypatch):
    monkeypatch.setattr(badge_stats, "_style_badges", [
        {"badge": "IPA Fan", "count": 1, "levels": 1, "styles": ["=IPA"]},
    ])
    monkeypatch.setattr(badge_stats, "_country_badges", [
        {"badge": "Canada Lover", "count": 1, "levels": 1, "countries": ["Canada"]},
    ])
    rows = [
        {"name": "IPA Fan", "kind": "style", "done": True},
        {"name": "Canada Lover", "kind": "country", "done": False},
    ]
    matches = badge_stats.badges_matching_beer(rows, style="IPA", country="Canada")
    assert [m["name"] for m in matches] == ["Canada Lover"]


def test_badges_matching_beer_no_match_returns_empty(monkeypatch):
    monkeypatch.setattr(badge_stats, "_style_badges", [
        {"badge": "IPA Fan", "count": 1, "levels": 1, "styles": ["=IPA"]},
    ])
    monkeypatch.setattr(badge_stats, "_country_badges", [])
    rows = [{"name": "IPA Fan", "kind": "style", "done": False}]
    assert badge_stats.badges_matching_beer(rows, style="Stout", country="") == []


# ---------------------------------------------------------------------------
# reload_special_badges - the live-reload path webapp_server.py's
# _special_badges_sync_loop depends on to pick up GitHub-synced catalog
# updates without a bot restart.
# ---------------------------------------------------------------------------

def test_reload_special_badges_picks_up_file_changes(tmp_path, monkeypatch):
    catalog_path = tmp_path / "special_badges.json"
    catalog_path.write_text(
        '{"special_badges": [{"badge": "First", "kind": "style", "styles": ["=IPA"], '
        '"activeFrom": "2000-01-01", "activeUntil": "2999-01-01"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(badge_stats, "_SPECIAL_BADGES_PATH", str(catalog_path))
    badge_stats.reload_special_badges()
    assert [b["badge"] for b in badge_stats._special_badges] == ["First"]

    catalog_path.write_text(
        '{"special_badges": [{"badge": "Second", "kind": "style", "styles": ["=IPA"], '
        '"activeFrom": "2000-01-01", "activeUntil": "2999-01-01"}]}',
        encoding="utf-8",
    )
    badge_stats.reload_special_badges()
    assert [b["badge"] for b in badge_stats._special_badges] == ["Second"]


def test_reload_special_badges_survives_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(badge_stats, "_SPECIAL_BADGES_PATH", str(tmp_path / "does_not_exist.json"))
    monkeypatch.setattr(badge_stats, "_special_badges", [{"badge": "Kept"}])
    badge_stats.reload_special_badges()
    # A missing/broken file must NOT wipe the last known-good catalog - the
    # sync loop only calls this after writing a file it just fetched, but a
    # corrupt fetch anywhere upstream shouldn't blank the live badges screen.
    assert badge_stats._special_badges == [{"badge": "Kept"}]
