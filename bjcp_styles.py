"""BJCP 2021 beer style guidelines - a static bundled reference file, same
convention as badge_beer_categories.json/badge_venue_categories.json (see
badge_stats.py's own header comment). Untappd has no public style-
description page or API of its own (confirmed live) - a "style guide"
only seems to exist inside Untappd's mobile app - so this is the only
real, citable description source available for the badge-detail screen's
clickable style tags.

Source: https://github.com/beerjson/bjcp-json (BJCP 2021 guidelines in
the BeerJSON format), fetched 2026-09-17. No live API - there isn't one.

Each style's overall_impression/aroma/appearance/flavor/mouthfeel also
carries a hand-translated Ukrainian sibling field (e.g. "aroma_uk") -
translated once, same one-time-curation spirit as the rest of this file's
data. _format prefers the _uk field, falling back to the English original
for the handful of entries where a field was empty in the source (a few
national provisional styles, X1/X2/X4, only ever had a partial writeup).

Matching is inherently approximate: Untappd's own catalog has many
modern/informal style splits (Pastry Stout, Smoothie Sour, Milkshake IPA,
etc.) that BJCP doesn't recognize as their own category at all. find_style
returns the closest BJCP match by fuzzy score, or None below the
confidence cutoff - never a forced, potentially-wrong guess.
"""

import json
import logging
import os
import re
import unicodedata

logger = logging.getLogger(__name__)

_styles: list[dict] = []

try:
    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bjcp_styles.json"),
        encoding="utf-8",
    ) as _f:
        _styles = json.load(_f).get("beerjson", {}).get("styles", [])
except (OSError, json.JSONDecodeError) as _e:
    logger.warning("Could not load bjcp_styles.json: %s", _e)

# How many extra words either side may carry beyond the other before a
# style match is refused rather than guessed - deliberately tight (unlike
# beer_match.py's MAX_SUPERSET_EXTRA_TOKENS=5 for full beer NAMES, which
# can have long flavor-descriptor tails): a style CATEGORY name is short
# and specific by nature, so a generic word-overlap scorer (tried first,
# rapidfuzz's WRatio) turned out to confidently match "Stout - Pastry"
# against plain "Irish Stout" and "IPA - Milkshake" against "English IPA"
# - both wrong, both missing their one distinguishing word entirely.
# Exact/subset TOKEN SET comparison (same principle as beer_match.py's
# own pick_best_match, adapted here) refuses those correctly while still
# finding "IPA - New England / Hazy" -> BJCP's plain "Hazy IPA" (2 extra
# words on the query's side, well within this cap).
MAX_EXTRA_TOKENS = 2

# Hand-curated fallback for popular MODERN/INFORMAL Untappd styles that
# BJCP has no category for at all (so the token-set matcher above always
# correctly returns None for these) but where there's a genuinely
# well-reasoned single "closest relative" - unlike a forced guess, this is
# an editorial opinion, not a derived fact, so find_style marks it with
# "approximate": True and the caller must show it as a stated
# approximation, never as if it were BJCP's own answer. Kept deliberately
# small - only add an entry here when the reasoning is solid, not for
# every unmatched style (most SHOULD stay a graceful "no BJCP
# equivalent" - see the module docstring). "Stout - Imperial / Double
# Pastry" is deliberately NOT here - confirmed live it's already resolved
# confidently by the token-set matcher above (bare "Imperial Stout" is a
# genuine, unambiguous 2-extra-word subset of that query), so an entry
# here would just be unreachable dead code. "Sour - Smoothie / Pastry"
# also has no entry - BJCP's only real catch-all ("Mixed-Style Beer",
# 34B) has zero actual descriptive content (every field just says "Based
# on the declared Base Styles"), so pointing to it would be strictly
# worse than admitting no BJCP equivalent exists.
INFORMAL_STYLE_APPROXIMATIONS = {
    "stout - pastry": "16A",  # Sweet Stout - closest in overall sweetness/richness, no strength implied either way
}

# i18n.py keys, not text - webapp_server.handle_style_info translates them
# into the viewer's language before replying.
BEST_EFFORT_NOTE = "bjcp_note_best_effort"
MIXED_STYLE_NOTE = "bjcp_note_mixed"
FRUIT_BEER_NOTE = "bjcp_note_fruit"


def _fold_diacritics(text: str) -> str:
    """Same fold as beer_match.py's own _fold_diacritics (not imported
    directly - a private helper, one module each) - NFKD decomposition
    handles most accented Latin letters, "ł"/"Ł" substituted by hand
    first since Unicode has no combining-stroke decomposition for it."""
    text = (text or "").replace("ł", "l").replace("Ł", "L")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# Untappd calls it "Pilsner", BJCP's own style names call it "Pils" (e.g.
# "German Pils") - proven live: "Pilsner - German" found no match at all
# purely because "pilsner" and "pils" are different tokens, despite the
# rest of the query lining up perfectly. A plain word-level synonym, same
# spirit as beer_match.py's own KNOWN_TERM_SUBSTITUTIONS. "Framboise"
# (French, raspberry) and "kriek" (Flemish, cherry) are the traditional
# NAMES for a fruited lambic by its most common fruits - proven live:
# "Lambic - Framboise"/"Lambic - Kriek" both matched plain "Lambic" (23D)
# instead of the correct "Fruit Lambic" (23F), since neither word reads
# as "fruit" on its own. Untappd's own catalog never spells out "fruit"
# for these specific lambic sub-styles, so the synonym is necessary here
# even though most OTHER fruited styles do say "fruit(ed)" explicitly.
_TOKEN_SYNONYMS = {"pilsner": "pils", "framboise": "fruit", "kriek": "fruit"}


def _token_set(text: str) -> frozenset:
    folded = _fold_diacritics(text).lower()
    words = re.sub(r"[^a-z0-9]+", " ", folded).split()
    return frozenset(_TOKEN_SYNONYMS.get(w, w) for w in words)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _fold_diacritics(name or "").lower()).strip("-")


def _style_url(entry: dict) -> str:
    return (
        f"https://www.bjcp.org/style/2021/{entry.get('category_id')}/"
        f"{entry.get('style_id')}/{_slugify(entry.get('name', ''))}/"
    )


def _uk(entry: dict, field: str) -> str | None:
    return entry.get(field + "_uk") or entry.get(field)


_STYLE_BY_ID = {entry["style_id"]: entry for entry in _styles}


def _format(
    entry: dict, *, approximate: bool = False, note: str | None = None, alternates: list[dict] | None = None,
) -> dict:
    return {
        "name": entry.get("name"),
        "category": entry.get("category"),
        "styleId": entry.get("style_id"),
        "note": note,
        "overallImpression": _uk(entry, "overall_impression"),
        "aroma": _uk(entry, "aroma"),
        "appearance": _uk(entry, "appearance"),
        "flavor": _uk(entry, "flavor"),
        "mouthfeel": _uk(entry, "mouthfeel"),
        "url": _style_url(entry),
        "approximate": approximate,
        # Other BJCP entries TIED for the same best-effort score (see
        # find_style's last-resort tier) - e.g. a bare "Imperial / Double"
        # tag scores Double IPA and Imperial Stout equally, two completely
        # different real styles with nothing in common but that one shared
        # word. Only ever populated by that tier; every other tier already
        # guarantees a single unambiguous answer so has nothing to list
        # here. Just {name, styleId, url} - not a full description, so the
        # caller can offer them as extra links without fetching more data.
        "alternates": alternates or [],
    }


def find_style(untappd_style: str) -> dict | None:
    """Closest BJCP 2021 style entry for an Untappd style string (e.g.
    "IPA - American", "Bock - Doppelbock"), or None if nothing matches
    confidently. Compares TOKEN SETS (order-independent, diacritic-
    folded), not fuzzy string distance - checked both against
    "{category} {name}" and bare "{name}" for every BJCP entry, since
    Untappd says "Category - Subcategory" but BJCP's own `name` field is
    often just the subcategory alone ("Doppelbock", not "Bock -
    Doppelbock"), so a single search form would miss the reordering.

    An exact token-set match wins outright, UNLESS more than one DISTINCT
    entry is exactly equal (shouldn't happen in practice, but handled the
    same as the tie case below rather than assumed impossible). Otherwise,
    EITHER side may be the "fuller" one (query ⊂ candidate, e.g. a shop-
    shortened style name; or candidate ⊂ query, e.g. Untappd's "IPA - New
    England / Hazy" vs BJCP's plainer "Hazy IPA") - accepted only up to
    MAX_EXTRA_TOKENS extra words, and only when exactly ONE distinct entry
    achieves the best (fewest extra words) score. A bare, generic tag like
    "Stout" or "Imperial / Double" is a genuine tie - proven live: "Stout"
    alone is one extra word away from SIX different real BJCP entries
    (Irish/Sweet/Oatmeal/Tropical/American/Imperial Stout) at once, and an
    earlier version of this function silently picked whichever one
    happened to iterate first, which is exactly the kind of confident-but-
    arbitrary wrong answer this module exists to avoid. Beyond the cap, or
    with no subset relation at all in either direction (informal Untappd-
    only terms like "Pastry"/"Milkshake"/"Smoothie" that BJCP has no
    equivalent category for), or with a tie, returns None - a wrong style
    description shown as if authoritative is worse than a graceful "no
    BJCP equivalent"."""
    query_tokens = _token_set(untappd_style)
    if not query_tokens:
        return None

    exact_entries: dict[str, dict] = {}
    best_extra = None
    best_entries: dict[str, dict] = {}
    for entry in _styles:
        entry_best_extra = None
        for form in (f"{entry.get('category', '')} {entry.get('name', '')}", entry.get("name", "")):
            cand_tokens = _token_set(form)
            if not cand_tokens:
                continue
            if query_tokens == cand_tokens:
                exact_entries[entry["style_id"]] = entry
                continue
            if query_tokens < cand_tokens:
                extra = len(cand_tokens - query_tokens)
            elif cand_tokens < query_tokens:
                extra = len(query_tokens - cand_tokens)
            else:
                continue
            if entry_best_extra is None or extra < entry_best_extra:
                entry_best_extra = extra
        if entry_best_extra is None:
            continue
        if best_extra is None or entry_best_extra < best_extra:
            best_extra, best_entries = entry_best_extra, {entry["style_id"]: entry}
        elif entry_best_extra == best_extra:
            best_entries[entry["style_id"]] = entry

    if len(exact_entries) == 1:
        return _format(next(iter(exact_entries.values())))
    if exact_entries:
        return None  # multiple distinct exact matches - genuinely ambiguous

    if best_extra is not None and best_extra <= MAX_EXTRA_TOKENS and len(best_entries) == 1:
        return _format(next(iter(best_entries.values())))

    approx_id = INFORMAL_STYLE_APPROXIMATIONS.get((untappd_style or "").strip().lower())
    approx_entry = _STYLE_BY_ID.get(approx_id) if approx_id else None
    if approx_entry:
        return _format(approx_entry, approximate=True)

    # "<base style> - Fruited" (e.g. "IPA - Fruited", "Pale Ale - Fruited")
    # has no BJCP entry of its own to subset-match against - a fruited
    # variant of a base style isn't its own named BJCP subcategory. This
    # ISN'T a guess though: 29A Fruit Beer's own category_description
    # states outright that BJCP classifies "beer made with any fruit"
    # under Fruit Beer regardless of base style (the base style gets
    # declared separately in competition, not folded into the style
    # name) - confirmed by reading that description directly. Still
    # marked approximate=True since it's a different KIND of answer than
    # the token-set match above (a general BJCP classification rule, not
    # an identification of the specific tag's own named style) and the
    # caller must word it accordingly.
    if "fruited" in query_tokens and "29A" in _STYLE_BY_ID:
        return _format(
            _STYLE_BY_ID["29A"], approximate=True,
            note=FRUIT_BEER_NOTE,
        )

    # Last-resort best-effort guess - added after the user pointed out the
    # strict tiers above were refusing too often to be useful in practice.
    # Ranks every BJCP entry by plain word-overlap (Jaccard similarity)
    # against the query instead of requiring a clean subset relation, and
    # takes the single highest-scoring entry as the PRIMARY answer - ties
    # broken by style_id purely for a stable, reproducible result, not
    # because it's more "correct". Always marked approximate=True with an
    # explicit warning note, since - unlike every tier above - this one
    # has no structural guarantee of being right, just "shares the most
    # words". A genuine tie (e.g. a bare "Imperial / Double" tag scores
    # Double IPA and Imperial Stout equally - two completely different
    # real styles, proven live by the user's own example) is NOT silently
    # collapsed to one answer - the other tied entries ride along as
    # `alternates` so the caller can offer them as extra links. If
    # literally nothing shares even one word with the query, falls back
    # to BJCP's own "Mixed-Style Beer" (34B) catch-all with its own
    # distinct note - the user's suggested last resort for a tag with no
    # BJCP relative at all.
    best_score = 0.0
    best_score_entries: dict[str, dict] = {}
    for entry in _styles:
        entry_best_score = 0.0
        for form in (f"{entry.get('category', '')} {entry.get('name', '')}", entry.get("name", "")):
            cand_tokens = _token_set(form)
            if not cand_tokens:
                continue
            score = len(query_tokens & cand_tokens) / len(query_tokens | cand_tokens)
            if score > entry_best_score:
                entry_best_score = score
        if entry_best_score > best_score:
            best_score, best_score_entries = entry_best_score, {entry["style_id"]: entry}
        elif entry_best_score > 0 and entry_best_score == best_score:
            best_score_entries[entry["style_id"]] = entry

    if best_score_entries:
        # Plain Jaccard treats every word equally, but Untappd's own
        # "Category - Subcategory" shape means the part BEFORE the dash is
        # the stated base style, not a strength/flavor modifier - proven
        # live: "Pilsner - Imperial / Double" tied Imperial Stout, Double
        # IPA, AND German Pils at the same score (each shares exactly one
        # word with the query), but only German Pils actually contains
        # the query's own declared base style ("Pilsner"/"pils") at all -
        # the other two only matched on the generic "Imperial/Double"
        # modifier, which (as established earlier - see
        # INFORMAL_STYLE_APPROXIMATIONS's own comments) applies to nearly
        # any style and carries no real identifying weight by itself.
        # Prefer whichever tied entry actually contains the declared base
        # style before falling back to the plain style_id tiebreak.
        base_part = untappd_style.split(" - ", 1)[0] if " - " in untappd_style else ""
        base_tokens = _token_set(base_part)

        def _contains_base(entry: dict) -> bool:
            if not base_tokens:
                return False
            return any(
                base_tokens <= _token_set(form)
                for form in (f"{entry.get('category', '')} {entry.get('name', '')}", entry.get("name", ""))
            )

        base_matches = [e for e in best_score_entries.values() if _contains_base(e)]
        ranked_candidates = base_matches or list(best_score_entries.values())
        chosen = min(ranked_candidates, key=lambda e: e["style_id"])
        alternates = [
            {"name": e.get("name"), "styleId": e.get("style_id"), "url": _style_url(e)}
            for e in sorted(best_score_entries.values(), key=lambda e: e["style_id"])
            if e["style_id"] != chosen["style_id"]
        ]
        return _format(chosen, approximate=True, note=BEST_EFFORT_NOTE, alternates=alternates)

    mixed_style = _STYLE_BY_ID.get("34B")
    if mixed_style:
        return _format(mixed_style, approximate=True, note=MIXED_STYLE_NOTE)
    return None
