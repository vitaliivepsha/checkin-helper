"""Shared "identify one beer by name/brewery on Untappd" logic - used by
both bot.py's /scan (a Claude-vision-extracted name from a photo) and
webapp_server.py's /api/lens/lookup (a browser userscript's page-scraped
name), so the exact-match discipline lives in exactly one place instead of
being duplicated (and potentially drifting) between the two callers.

Kept as its own module rather than living in bot.py: bot.py imports and
starts webapp_server.py at runtime (see bot.py's post_init), so
webapp_server.py importing back from bot.py would be circular.
"""

import logging
import os
import re
import time
import unicodedata
from urllib.parse import quote

import had_it_index
import untappd_mcp

logger = logging.getLogger(__name__)

SEARCH_RESULT_LIMIT = 15  # a generic 1-2 word query (e.g. "IPA") can rank the
# exact-name match past position 5 among a brewery's many similarly-styled
# beers - proven live (Magic Road "IPA" ranked 9th of a real 10-candidate
# pull) - 15 gives the exact match room to surface without pulling in so
# many candidates that two unrelated beers coincidentally share a name.

# Which Untappd beer (if any) a given shop-provided (name, brewery) text
# resolves to is impersonal, near-static data - the same text matches the
# same catalog beer (or fails to match) for every caller, so it's cached
# process-wide rather than re-derived from scratch on every call. This is
# the dominant quota cost in this module by far: a single resolution can
# retry up to ~10 cleaned name variants x however many brewery variants,
# each one a live search_beers call (see resolve_beer's own retry loop) -
# proven live to help push a whole account over Untappd's 100/hour limit
# from ONE shop-page scan, let alone a repeat scan of the same page (no
# caching at all meant every rescan re-paid the full retry cost for every
# beer, even ones already resolved seconds earlier). Never caches "hadIt" -
# that's genuinely personal per user_id and computed fresh below, outside
# this cache, every call.
_IDENTITY_CACHE_TTL_SECONDS = float(os.environ.get("BEER_MATCH_CACHE_TTL_SECONDS", str(6 * 60 * 60)))
_identity_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_country_cache: dict[int, tuple[float, str]] = {}


def _cache_get(cache: dict, key) -> object | None:
    entry = cache.get(key)
    if entry is None:
        return None
    stored_at, value = entry
    if time.monotonic() - stored_at > _IDENTITY_CACHE_TTL_SECONDS:
        del cache[key]
        return None
    return value


def _cache_set(cache: dict, key, value) -> None:
    cache[key] = (time.monotonic(), value)


# Non-alcoholic beers get labelled inconsistently across breweries/shops/
# languages - English "non-alcoholic"/"non alco"/"alcohol free" (a Freeky
# non-alcoholic listing used this form), Polish "bezalkoholowe", Czech
# "nealko" (proven live: Untappd's own catalog name for a Litovel beer is
# "... Nealko / Free") - and even Untappd's OWN catalog names aren't
# consistent about which form they use (a Polish brewery's entry says
# "Bezalko", an English-market one says "Non-Alcoholic"). "Bezalkoholwe"
# (missing the second "o") is a genuine TYPO in Untappd's own catalog name
# for a real beer (Wielka Sowa's "Sowie Bezalkoholwe Jasne") - proven live:
# the shop spells it correctly ("Bezalkoholowe"), so without this variant
# the two sides never canonicalize to the same token despite being the same
# real beer. Canonicalizing both sides to the same token before comparing
# means the match succeeds regardless of which spelling (or misspelling)
# either side happens to use.
_NON_ALCO_RE = re.compile(
    r"\bnon[\s-]?alco(?:holic)?\b|\balcohol[\s-]?free\b|\bbezalkoholo?we\b|\bnealko\b", re.IGNORECASE
)


def _fold_diacritics(text: str) -> str:
    """Folds accented Latin letters to their plain ASCII base (ą->a, ć->c,
    ń->n, ó->o, ś->s, ź/ż->z, é->e, ü->u, etc.) - MUST run before
    _simple_norm's [^a-z0-9%] stripping, which otherwise treats every
    accented letter as punctuation and SPLITS the word there instead of
    folding it - proven live: "Bałtycki" tokenized as two unrelated
    fragments "ba" + "tycki" (the "ł" itself vanishing as a separator),
    which then coincidentally exact-matched against a DIFFERENT beer's own
    "...Old Forester BA..." substring, and "Amburaną" (grammatically
    inflected) fragmenting to "amburan" so it could never equal the
    catalog's own nominative "Amburana" no matter how the query was
    otherwise cleaned. Unicode NFKD decomposition handles most of these
    automatically (an accented letter decomposes to its base letter plus a
    separate combining mark, which is then dropped); "ł"/"Ł" is the one
    common exception with no such decomposition, substituted by hand
    first."""
    text = (text or "").replace("ł", "l").replace("Ł", "L")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _simple_norm(text: str) -> str:
    # "%" is kept (not treated as discardable punctuation like other
    # symbols) - Untappd's own naming convention uses a trailing "%" to
    # mark a non-alcoholic sibling of a same-named beer (e.g. Birbant's
    # "Turbo" vs "Turbo%", two genuinely different real beers) - stripping
    # it would make the two compare as the same name and pick one at
    # random between them.
    return re.sub(r"[^a-z0-9%]+", " ", _fold_diacritics(text).lower()).strip()


def scan_norm(text: str) -> str:
    """Same normalization as _simple_norm, plus:
    - canonicalizing non-alcoholic phrasing (see _NON_ALCO_RE) so a query
      and a candidate that spell it differently still compare equal.
    - dropping the bare word "and" - a flavor list punctuated "X, Y & Z" on
      a label and "X, Y and Z" in Untappd's own catalog name would
      otherwise normalize to two different token sequences (the "&" is
      stripped as punctuation, but the literal word "and" is not) and miss
      what should count as the same name."""
    canonicalized = _NON_ALCO_RE.sub("bezalko", text or "")
    words: list[str] = []
    for w in _simple_norm(canonicalized).split():
        if w == "and":
            continue
        if w == "ba":
            # "BA" = "barrel-aged": a shop writes the abbreviation where
            # Untappd's catalog spells it out ("BA Speedway Stout 2023" vs
            # "Barrel-Aged Speedway Stout (2023)"), so the two never
            # compared equal and - worse - the only candidate whose own name
            # literally says "BA" ("BA Speedway Stout: Mexican Hot Chocolate
            # Edition (2023)") then won as the sole superset match: a WRONG
            # beer shown with full confidence. Expanding on both sides
            # keeps a catalog entry that literally says "BA" comparing equal
            # to its own spelled-out form too.
            words.extend(("barrel", "aged"))
            continue
        words.append(w)
    return " ".join(words)


def _token_set(text: str) -> frozenset:
    return frozenset(scan_norm(text).split())


# An em/en dash in a catalog beerName marks a deliberate "core name -
# descriptive subtitle" split - proven live: Piwne Podziemie's own
# "OVERSATURATED — Citra X Hallertau Blanc X Nelson Sauvin X Riwaka" is the
# beer's full hop bill tacked on after the real name. Used only to compute
# the superset CAP in pick_best_match (see _core_token_set) - tokens that
# only ever appear after the dash don't count against
# MAX_SUPERSET_EXTRA_TOKENS, because they're the cataloger's own explicit
# signal that this is elaboration, not part of the identity a shop's
# shorter title would be expected to repeat.
_SUBTITLE_DASH_RE = re.compile(r"[—–]")


def _core_token_set(text: str) -> frozenset:
    """Same as _token_set, but only the portion of `text` before the first
    em/en dash (see _SUBTITLE_DASH_RE) - the catalog name's own "core"
    identity, without a trailing descriptive subtitle. A name with no dash
    at all returns its full token set unchanged (nothing to strip)."""
    return _token_set(_SUBTITLE_DASH_RE.split(text or "", maxsplit=1)[0])


def _brewery_matches_query(result: dict, query_brewery_name: str) -> bool:
    """Whether a search result's own brewery - or one of UNTAPPD'S OWN
    aliases for it - plausibly matches the query's brewery text. Proven
    live necessary: a renamed brewery's beers sometimes stay catalogued
    under BOTH the old and new identity (Browar Stu Mostów's "Black IPA"
    also exists as a "WRCLW" entry, cross-referenced via WRCLW's own
    `aliases` list) - checking only `brewery.name` would miss that the two
    entries are really the same real brewery. Substring, either direction
    and case-insensitive - the query brewery is often a shortened form of
    Untappd's fuller name (shop's "Stu Mostów" vs catalog's "Browar Stu
    Mostów"), or vice versa."""
    query_key = (query_brewery_name or "").strip().lower()
    if not query_key:
        return False
    names = [(result.get("brewery") or {}).get("name") or ""]
    names.extend(result.get("aliases") or [])
    for name in names:
        name_key = name.strip().lower()
        if name_key and (name_key in query_key or query_key in name_key):
            return True
    return False


# How many extra words a "fuller catalog name" superset match may add
# beyond the query before it's refused as more likely a DIFFERENT,
# fancier product than a fuller name for the same one - see
# pick_best_match's own docstring for the calibration case (4 extra words
# accepted, 5-6 refused).
MAX_SUPERSET_EXTRA_TOKENS = 5


def pick_best_match(results: list[dict], beer_name: str, brewery_name: str = "") -> dict | None:
    """search_beers ranks by its own relevance score, which is not reliable
    enough to trust blindly: it can rank a DIFFERENT same-brewery beer
    ABOVE the actual correct match, both for a short/generic detected name
    (e.g. "Velvet" ranked "Gelato XTREME: Blue Velvet" above the plain
    "Velvet" IPA) and for an entire flavor LINE sharing one base name (e.g.
    "Wonders" ranked one sibling flavor above the actually-correct one) -
    both proven live on real shelf photos. A LONE result is no exception -
    proven live: search_beers returning exactly one hit for a shop's plain
    "Porter Bałtycki z Amburaną" was Komes' own barrel-aged "Wymrażany
    Imperialny Porter Bałtycki Old Forester BA z Amburaną" (21% ABV) - the
    correct, far more common plain "Porter Bałtycki Amburana" (9% ABV)
    simply didn't rank for that exact query text, so there was no sibling
    for Algolia itself to return alongside it, but it's still the WRONG
    beer to hand back with full confidence.

    A wrong beer presented with full confidence (wrong style/ABV/link/
    badges) is worse than admitting no match, so this refuses to guess and
    treats every result the same way, lone or not:
    - compares TOKEN SETS (order-independent - a shop's own word order
      doesn't always match Untappd's, e.g. "Bezalko Jasne" vs the
      catalog's "Jasne Bezalko"), not the joined string. The brewery's own
      tokens are dropped from beer_name's side first (see brewery_name
      below) - a shop's title routinely repeats the brewery name as a
      prefix, but a real catalog beerName essentially never does, so
      keeping those tokens in the comparison only risks a false match,
      never a genuine one.
    - an exact token-set match wins if exactly one candidate has it; two+
      candidates with the identical name are a genuine catalog duplicate,
      not something to guess between (see _brewery_matches_query below for
      how those get narrowed instead of refused outright).
    - failing that, a query whose tokens are a STRICT SUBSET of exactly one
      candidate's tokens is accepted - the common "shop listed a shortened
      name, Untappd's is fuller" case (e.g. "Salty Love vol.1" for the
      catalog's "Salty Love vol.1 - Mango + Peach + Coconut + Lemon", 4
      extra words) - but only up to MAX_SUPERSET_EXTRA_TOKENS extra words:
      past that, a "superset" is more likely a different, fancier product
      sharing the same base name (the Komes case above: 5-6 extra words -
      "wymrażany", "imperialny", "old", "forester", "ba" - for what's
      really a different beer at a different ABV) than a fuller name for
      the SAME one. Only ever query-subset-of-candidate, never the
      reverse - accepting a shorter candidate for a longer/noisier query
      would risk matching on whatever of the query's words happen to
      overlap, not a real identity.
    - None means the caller should tell the user it couldn't confidently
      identify it, not silently substitute a lookalike.

    brewery_name: when the beer_name still has the brewery's own name
    baked in (proven live, a common shop pattern - "PINTA Hazy Morning"),
    its tokens are excluded from the comparison - proven live necessary:
    "PINTA PINTA Hazy Morning" (brewery duplicated in the query) exact-
    matched "Pinta Hazy Morning" by a COMPLETELY UNRELATED brewery
    ("Upside Down") that just happens to credit "Pinta" in its own beer
    name, while the real "Hazy Morning" by PINTA itself doesn't repeat its
    own brewery name and so wasn't an exact match at all until "pinta" was
    dropped from the query's side of the comparison.
    """
    raw_query_tokens = _token_set(beer_name)
    if not raw_query_tokens:
        return None
    # Brewery tokens are excluded for the EXACT check only, not the
    # superset one below - proven live both ways: excluding them for exact
    # is what fixes the Pinta case (a real catalog beerName essentially
    # never repeats its own brewery name), but ALSO excluding them from the
    # superset check reintroduces a different false positive (Primátor's
    # "PRIMÁTOR PREMIUM LAGER" superset-matching "Diver Premium Lager" the
    # moment "primátor" no longer disqualifies it) - the superset check
    # already only fires when every OTHER query word is a genuine subset,
    # so leaving the brewery token in place there costs nothing when it's
    # truly a duplicate (still matches fine) but adds a safety margin
    # against exactly this kind of coincidence.
    query_tokens = raw_query_tokens - _token_set(brewery_name)
    if not query_tokens:
        return None

    exact = [r for r in results if _token_set(r.get("beerName") or "") == query_tokens]
    if len(exact) > 1:
        # Multiple identically-named catalog entries - not necessarily
        # ambiguous. The exact-match check above deliberately never looks
        # at brewery (see brewery_name's own docstring note), so narrow by
        # it now before giving up: proven live, a shop's generic "Pale
        # Ale"/"American IPA"/"Black IPA" each matched TWO real Untappd
        # entries under the query's own brewery (one current, one an older
        # duplicate Untappd itself marks "no longer in production" - a
        # field search_beers doesn't expose at all) - genuinely ambiguous
        # by name alone, not by brewery.
        brewery_matched = [r for r in exact if _brewery_matches_query(r, brewery_name)]
        if brewery_matched:
            exact = brewery_matched
        else:
            return None  # no brewery signal to narrow by - genuinely ambiguous
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # Still tied after brewery narrowing - genuine same-brewery catalog
        # duplicates. The higher bid is the more recently catalogued entry
        # - confirmed live to correlate with "currently in production" in
        # every observed case, DESPITE having fewer ratings than the
        # older, retired entry (which accumulated ratings for longer
        # before being superseded) - ratingCount would pick the wrong one.
        return max(exact, key=lambda r: r.get("bid") or 0)

    if raw_query_tokens != query_tokens:
        # The brewery-token subtraction above (query_tokens) exists to stop
        # an UNRELATED brewery's incidental name-credit inside its own
        # beerName from exact-matching (the Pinta case in this docstring),
        # but it can itself strip a token that was genuinely part of the
        # correct beer's own name - proven live: Maryensztadt's "Freeky"
        # sub-line is catalogued under its OWN brewery "FREEKY non-
        # alcoholic", whose real beerName is "Freeky APA" - subtracting the
        # brewery's own tokens from the query ("freeky", "non", "alcoholic")
        # left only "apa", no longer an exact match for the real "Freeky
        # APA" entry. Retried here against the FULL, unstripped tokens, but
        # ONLY accepted when the candidate's own actual brewery genuinely
        # matches the query brewery (_brewery_matches_query) - that extra
        # check is what keeps this safe from reintroducing the Pinta false
        # positive it would otherwise cause (there, the wrong candidate's
        # real brewery is "Upside Down", not "Pinta", so this guard refuses
        # it and the brewery-stripped comparison above remains the deciding
        # one for that case).
        raw_exact = [
            r for r in results
            if _token_set(r.get("beerName") or "") == raw_query_tokens
            and _brewery_matches_query(r, brewery_name)
        ]
        if len(raw_exact) == 1:
            return raw_exact[0]

    def _within_superset_cap(name: str) -> bool:
        # The cap only ever counts extra tokens within the candidate's own
        # CORE name (see _core_token_set) - a trailing "— full hop/flavor
        # bill" subtitle doesn't count against it, proven live necessary
        # (Piwne Podziemie's "OVERSATURATED — Citra X Hallertau Blanc X
        # Nelson Sauvin X Riwaka", 7 extra tokens past the dash for a shop's
        # bare "Oversaturated" - the ONLY beer of that name on Untappd, so
        # the cap isn't protecting against anything by refusing it here).
        # Komes' own "Wymrażany Imperialny Porter Bałtycki Old Forester BA
        # z Amburaną" - the case the cap exists for - has no dash at all,
        # so its extra tokens are still fully counted and still refused.
        core = _core_token_set(name)
        extra_in_core = core - raw_query_tokens
        return len(extra_in_core) <= MAX_SUPERSET_EXTRA_TOKENS

    supersets = [
        r for r in results
        if raw_query_tokens < _token_set(r.get("beerName") or "")
        and _within_superset_cap(r.get("beerName") or "")
    ]
    if len(supersets) == 1:
        return supersets[0]
    return None


# ---- Known terms -----------------------------------------------------------
# Brewery-specific in-house abbreviations/nicknames (e.g. Trzech Kumpli's
# own "ONINNI" for "Our New IPA Needs No Introduction") and known shop-
# listing misspellings (e.g. "Kawastrofa" for the real "Kawastorfa") - by
# nature these are NOT general rules, no pattern-based cleanup below could
# ever infer them, so they're a plain, hand-maintained lookup instead. Add
# to this as new ones turn up; keys are matched case-insensitively as whole
# words/phrases, wherever they appear in a beer name.
KNOWN_TERM_SUBSTITUTIONS = {
    "oninni": "Our New IPA Needs No Introduction",
    "bcbs": "Bourbon County Brand Stout",  # generic - most breweries' "BCBS" nods at Goose Island's famous one
    "kawastrofa": "Kawastorfa",
}

# Some abbreviations mean something DIFFERENT for a specific brewery's own
# in-house pun - proven live: 3 Sons' own "BCBS" is "Broward County Brand
# Stout" (a play on their home county), not the generic "Bourbon County
# Brand Stout" above. Checked first, keyed by a lowercase substring of the
# (cleaned) brewery name; falls back to KNOWN_TERM_SUBSTITUTIONS when this
# brewery has no override for that particular term.
BREWERY_TERM_SUBSTITUTIONS = {
    "3 sons": {"bcbs": "Broward County Brand Stout"},
    # Zichovec's own shorthand for its yearly "Winter Affair" imperial
    # stout series/collabs (e.g. a shop's "WA Gossip Pūhaste 28") - proven
    # live: search_beers returns ZERO results for "Zichovec WA Gossip
    # Pühaste" (with or without a trailing edition number like "28"), but
    # finds the real "Winter Affair Gossip: Pühaste" instantly once "WA" is
    # either dropped or expanded - bare "wa" is too ambiguous a 2-letter
    # token to add to the generic KNOWN_TERM_SUBSTITUTIONS above (it isn't
    # a fixed abbreviation for anything outside this one brewery's own
    # labeling convention).
    "zichovec": {"wa": "Winter Affair"},
}

# A brewery sometimes spins a whole sub-line off into its own separate
# Untappd brewery entry - proven live: Maryensztadt's "Freeky" line is
# entirely non-alcoholic and catalogued under its own "FREEKY non-
# alcoholic" brewery, not under "Maryensztadt" itself (a shop's own
# "Producent" field still says "Maryensztadt" regardless). Keyed by a
# lowercase substring of the (cleaned) brewery name, mapping to a whole-word
# trigger that must appear in the beer NAME -> the real brewery to search
# under instead. Tried as an extra, highest-priority brewery variant (see
# resolve_beer) - the shop-provided brewery is still tried too as a
# fallback, in case a future listing under this same trigger word turns out
# not to need the override.
BREWERY_OVERRIDE_BY_NAME_TERM = {
    "maryensztadt": {"freeky": "FREEKY non-alcoholic"},
    # onemorebeer.pl's own "Producent: Fortuna" is wrong specifically for
    # the "Grodziskie" style beers it also lists under that same producer -
    # proven live: "Fortuna Grodziskie ... Pils" found nothing, the real
    # brewery is "Browar Grodzisk" (a specialty brewery for this one
    # style). Not a blanket BREWERY_RENAME like Piotrków/Drink ID below -
    # Fortuna's OTHER (non-Grodziskie) listings are presumably correct as
    # Fortuna, per the user's own observation this only misfires here.
    "fortuna": {"grodziskie": "Browar Grodzisk"},
}

# A shop's "Producent" field is sometimes a manufacturing/contract-brewing
# location that never existed as its OWN brewery on Untappd at all - proven
# live: a shop's "Piotrków" returned zero results no matter what, because
# that beer is actually catalogued under the brand "Drink ID" instead.
# Unconditional (no beer-name trigger needed, unlike
# BREWERY_OVERRIDE_BY_NAME_TERM above) - keyed by a lowercase substring of
# the (cleaned) brewery name. Same fallback-first-then-original-too
# priority as the name-term overrides.
BREWERY_RENAME = {
    "piotrków": "Drink ID",
    # "Krachla" (a shop's own Producent, sometimes preceded by the town
    # "Grybów") - proven live: "Krachla Góralskie Krzepkie" (with or
    # without "Grybów") found nothing, the real brewery is "Pilsvar".
    "krachla": "Pilsvar",
    # Shops list the full legal name "Rodinný Pivovar Zichovec", but
    # Untappd has long since catalogued the brewery under just "Zichovec" -
    # the full name returns nothing.
    "zichovec": "Zichovec",
    # onemorebeer.pl abbreviates "Browar Stu Mostów" down to its bare
    # initials "BSM" in its own Producent field - proven live: "BSM Schops"
    # returns only unrelated junk (no beer of theirs is catalogued under
    # the literal initials "BSM" at all), while "Stu Mostów Schops" finds
    # the real beer ("WRCLW Schöps" - Stu Mostów's own beers are catalogued
    # under its "WRCLW" sub-brand, cross-referenced via that entry's own
    # "Stu Mostów"/"Browar Stu Mostów" aliases, which search_beers already
    # matches on).
    "bsm": "Stu Mostów",
}

# A brewery is sometimes catalogued under MULTIPLE names on Untappd at
# once, inconsistently per-beer, rather than one single "real" name a
# BREWERY_RENAME could swap to - proven live: "Jurajskie" (this shop's own
# Producent field) correctly resolves plenty of its own beers ("Jurajskie
# Porter Bałtycki" etc. all work fine as-is), but OTHER beers from the same
# brewery are catalogued as "Na Jurze X" ("Motocyklowe") or even "Stacja X"
# ("Jabłko-Mięta") instead, with no way to predict which convention a given
# beer uses from the shop's text alone. Unlike BREWERY_OVERRIDE_BY_NAME_TERM,
# not conditioned on any beer-name trigger - these are just extra brewery
# variants worth trying, appended AFTER the shop-provided ones (see
# resolve_beer) so the common case (shop's own name already works) isn't
# disturbed; only consulted at all once those have failed.
BREWERY_ALIASES = {
    "jurajskie": ["Na Jurze"],
}


def _brewery_override(brewery_name: str, beer_name: str) -> str | None:
    brewery_key = (brewery_name or "").strip().lower()
    for key, rename in BREWERY_RENAME.items():
        if key in brewery_key:
            return rename
    for key, triggers in BREWERY_OVERRIDE_BY_NAME_TERM.items():
        if key not in brewery_key:
            continue
        for trigger, override in triggers.items():
            if re.search(r"\b" + re.escape(trigger) + r"\b", beer_name or "", re.IGNORECASE):
                return override
    return None


def _brewery_aliases(brewery_name: str) -> list[str]:
    brewery_key = (brewery_name or "").strip().lower()
    aliases: list[str] = []
    for key, names in BREWERY_ALIASES.items():
        if key in brewery_key:
            aliases.extend(names)
    return aliases


_ALL_KNOWN_TERMS = set(KNOWN_TERM_SUBSTITUTIONS) | {
    term for overrides in BREWERY_TERM_SUBSTITUTIONS.values() for term in overrides
}
_KNOWN_TERM_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_ALL_KNOWN_TERMS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _apply_known_terms(text: str, brewery_name: str = "") -> str:
    brewery_key = (brewery_name or "").strip().lower()
    overrides = next(
        (subs for key, subs in BREWERY_TERM_SUBSTITUTIONS.items() if key in brewery_key),
        {},
    )

    def _replace(m: re.Match) -> str:
        term = m.group(0).lower()
        return overrides.get(term) or KNOWN_TERM_SUBSTITUTIONS.get(term) or m.group(0)

    return _KNOWN_TERM_RE.sub(_replace, text or "")


# ---- Query cleanup ---------------------------------------------------------
# A shop's own product-listing text is close to, but not identical to,
# Untappd's catalog name - and this search backend needs a fairly close
# match to return anything at all (proven live: several of these, left
# unstripped, made search_beers return ZERO results, not just a worse-
# ranked one). None of this is per-brewery - every rule below is a generic
# pattern observed across multiple, unrelated breweries on one real shop's
# listing page.

# A generic "this is a brewery" noun prefixing OR trailing the actual
# brewery name, in whichever language a given shop happens to use - proven
# live: "Browar Lubrow" as a PREFIX returned zero results (catalogued as
# "Lubrow Brewery"), "PINTA Brewery" as a SUFFIX also returned zero
# (catalogued as plain "PINTA"), and hoptimaal.com's own vendor field lists
# breweries as "Brasserie Caulier" (French), "Arpus Brewing Co." / "Polly's
# Brew Co." (English), "X Brouwerij"/"X Bierbrouwerij" (Dutch) - same
# pattern, just not Polish. The separator is EITHER real whitespace OR (via
# the lookahead) a following capital letter with no space at all - proven
# live: onemorebeer.pl's own producer field for Trzech Kumpli is
# "BreweryTrzech Kumpli", the generic label glued directly onto the real
# name with no space (a template bug on the shop's own side) - requiring at
# least one space meant this prefix never matched, leaving the whole glued
# string in the query, which returns ZERO results (confirmed live) where
# "Trzech Kumpli" alone finds the beer instantly. The lookahead's capital-
# letter requirement is deliberately case-SENSITIVE even though the prefix
# word itself is matched case-insensitively (scoped via `(?i:...)`, not the
# whole pattern) - a bare `\s*` would also wrongly strip "browar" out of an
# unrelated real word that merely starts with those letters in lowercase
# (e.g. "Browarnia"), which this avoids: a glued run-on only looks like a
# genuine second word, not a continuation of the same one, when the next
# letter is capitalized.
_BREWERY_PREFIX_RE = re.compile(
    r"^(?i:browar|brewery|brewing|piwowarnia|brasserie|brouwerij|bierbrouwerij)(?:\s+|(?=[A-ZĄĆĘŁŃÓŚŹŻ]))"
)

# Strips exactly ONE trailing generic word - used iteratively (see
# _brewery_query_variants), not in one greedy pass. A site can stack
# several of these words at the end ("PINTA Barrel Brewing Brewery",
# "Arpus Brewing Co."), but blindly stripping ALL of them in one shot is
# too aggressive when one of those "generic" words is actually part of the
# real brewery name - proven live: ontap.pl's own "Ale Browar Brewery" (it
# appends "Brewery" to every listing) has a real Untappd brewery of
# "AleBrowar" - stripping only the site's own "Brewery" suffix ("Ale
# Browar Dortmunder") found the exact, unique match, while also stripping
# "Browar" ("Ale Dortmunder") left only generic words and returned 6
# unrelated, ambiguous candidates instead.
_BREWERY_SUFFIX_WORD_RE = re.compile(
    r"\s+(browar|brewery|brewing|piwowarnia|brasserie|brouwerij|bierbrouwerij|company|co\.?|brew)$",
    re.IGNORECASE,
)

# "X x Y" collaboration notation - Untappd typically credits a collab beer
# to one side only (which side varies and isn't predictable from the shop's
# own text - proven live both ways), and searching with the full "X x Y"
# phrase reliably returned zero results. Keep only the brewery before " x ".
_COLLAB_SUFFIX_RE = re.compile(r"\s+x\s+\S.*$", re.IGNORECASE)

# Generic marketing/edition words that are never part of a beer's own
# catalog name but do appear in a shop's product title - each confirmed
# live to independently make search_beers return zero results when left in
# (including the tier words alone, without "Series" attached). "Prozdrowotne"
# (Polish "health-promoting") is the same kind of marketing descriptor, just
# for a health-halo claim instead of an edition tier.
_NOISE_WORDS_RE = re.compile(
    r"\b(series|festiwal|festival|platinum|gold|silver|bronze|prozdrowotne|gluten)\b", re.IGNORECASE
)

# A shop appends the physical packaging (container word + volume, in
# EITHER order, English or Polish) to the product title - never part of
# Untappd's own catalog name - proven live, both word orders and both
# languages independently confirmed to make search_beers return zero
# results when left in: "Black IPA - bottle 500 ml", "Pale Ale - 500 ml
# bottle", "For.rest - butelka 500 ml". Anchored to the END of the string
# (with an optional leading "-") rather than a bare \b(...)\b scan - a
# generic word like "can" is too common a real word/name fragment to
# safely strip wherever it appears, but "can/bottle/etc immediately next
# to a volume number, trailing the whole title" is unambiguously packaging
# metadata. The third branch is a BARE trailing volume with no container
# word at all - proven live (onemorebeer.pl's own "... Bezalkoholowe 0,5
# L", no "butelka"/"but." anywhere in the title) - a beer name never
# legitimately ends in a bare volume unit either way.
_PACKAGING_SUFFIX_RE = re.compile(
    r"\s*-?\s*(?:"
    r"(?:bottle|can|keg|growler|crowler|butelka|puszka|beczka)\s*\d+(?:[.,]\d+)?\s*m?l"
    r"|"
    r"\d+(?:[.,]\d+)?\s*m?l\s*(?:bottle|can|keg|growler|crowler|butelka|puszka|beczka)"
    r"|"
    r"\d+(?:[.,]\d+)?\s*m?l\b"
    r")\s*$",
    re.IGNORECASE,
)

# "Polish Vintage:" - a collection/series label a shop prepends, never
# part of the beer's own catalog name - proven live to return zero
# results when left in (including just the words, without the colon).
# The optional trailing colon is consumed too, so it doesn't linger as a
# dangling punctuation mark once the words are gone.
_POLISH_VINTAGE_RE = re.compile(r"\bpolish\s+vintage\s*:?", re.IGNORECASE)

# "Kraft Roku <year>" (Polish "Craft of the Year <year>") - an award/
# marketing label a shop tacks onto a listing, never part of the beer's
# own catalog name - proven live to return zero results when left in,
# for any year.
_KRAFT_ROKU_RE = re.compile(r"\bkraft\s+roku\s+\d{4}\b", re.IGNORECASE)

# "IN&OUT" - a shop's own dine-in/takeaway program label, never part of
# the beer's own catalog name - proven live to return zero results when
# left in.
_IN_OUT_RE = re.compile(r"\bin\s*&\s*out\b", re.IGNORECASE)

# A dangling "&"/"/" left over once an adjacent noise word (e.g. "gluten"
# out of "Gluten & Alcohol Free") or non-alco marker has been stripped out
# from beside it - proven live: "Freeky Hazy IPA Gluten &" (the "&" left
# over once "Gluten" and the non-alco marker after it were both handled)
# still returned zero results until the stray "&" itself was gone too.
_DANGLING_CONNECTOR_RE = re.compile(r"(^|\s)[&/](\s|$)")

# A shop's own title joins a flavour list with the natural-language "and"
# ("Malina I Pigwa" - Polish "i" = "and"), but Untappd's catalog name for
# that exact beer doesn't use it at all ("Bestbir Piwo z Sokiem Malina -
# Pigwa") - proven live: leaving the bare "I" in returned ZERO results,
# dropping it found the one real match uniquely. Same role as the bare
# "and" scan_norm already drops for COMPARISON (see _NON_ALCO_RE's
# neighbor above) - this is the query-building-time equivalent, needed
# because here the untouched word breaks the search itself, not just the
# token comparison after.
_POLISH_AND_RE = re.compile(r"\bi\b", re.IGNORECASE)

# Polish "z" ("with") - a grammatical connector, not identifying content,
# same spirit as _POLISH_AND_RE's "i" above. Unlike "i" though, Untappd's
# OWN catalog name sometimes genuinely keeps a "z ..." phrase verbatim
# (this file's own earlier example: "Bestbir Piwo z Sokiem Malina -
# Pigwa") - so this is NOT stripped because "z" breaks the search the way
# "i" does. It's stripped because leaving it in the query can make a
# SPECIALTY/limited variant whose fuller catalog name happens to also
# start with the same "z ..." phrase look like a confident match ahead of
# the plain, far more common sibling beer that has no "z" in its name at
# all - proven live: a shop's plain "Porter Bałtycki z Amburaną" (0.5 L,
# a few zł) exact-name-matched Komes' own barrel-aged "Wymrażany
# Imperialny Porter Bałtycki Old Forester BA z Amburaną" (21% ABV, a
# completely different, far pricier product) instead of the correct plain
# "Porter Bałtycki Amburana" - dropping "z" turns that into an exact
# match against the RIGHT beer instead (Untappd's own search still finds
# a z-containing catalog name fine without "z" in the query - it's
# grammatically empty content, same as "i" - see MAX_SUPERSET_EXTRA_TOKENS
# below for the other half of this fix).
_POLISH_WITH_RE = re.compile(r"\bz\b", re.IGNORECASE)

# Same concept as _NON_ALCO_RE, but used to DETECT the concept in a shop's
# raw text (including a bare "0%"/"0.0%" ABV callout, another common way
# shops flag a non-alcoholic beer) rather than to canonicalize it - see
# _non_alco_variants below for why detection and query-text substitution
# are handled separately.
_NON_ALCO_TRIGGER_RE = re.compile(
    r"\bnon[\s-]?alco(?:holic)?\b|\balcohol[\s-]?free\b|\bbezalkoholowe\b|\bnealko\b|\b0(?:[.,]0)?\s*%",
    re.IGNORECASE,
)

# A shop's product title sometimes embeds the beer's STYLE category as
# actual words IN the name - not always trailing ("Czech Pilsner" at the
# end), sometimes stuck in the MIDDLE ("WILD Sour Saison Apricot & Palo
# Santo", "Aardbei-Schaarbeekse Kriek 23/24" - both confirmed live to
# return nothing until the style word was removed from wherever it sat).
# Untappd's own catalog name never includes it either way. NOT folded into
# _NOISE_WORDS_RE / _clean_beer_name_query - unlike those words, a style
# name is common enough as an actual beer-name substring elsewhere that
# stripping it unconditionally on every query is riskier, so this is only
# ever tried as a fallback variant (see _style_stripped_variant) after the
# untouched name already failed. "Grodziskie"/"Grätzer" (a historic Polish
# smoked-wheat style, named after the town Grodzisk) is the same pattern -
# proven live: a specialty brewery's own "Grodziskie Pils Bezalkoholowy"
# left the single real Untappd match ("Bezalkoholowy Pils") unmatched
# because "Grodziskie" isn't part of the catalog beerName at all (the
# catalog style field is plain "Lager", not "Grodziskie") and the word
# doesn't collapse into the brewery name ("Browar Grodzisk") either, so it
# sat as an unmatched extra token blocking both the exact and superset
# checks. "West Coast" (an IPA sub-style descriptor) is the same pattern
# again, and proven live to be actively HARMFUL rather than just inert
# noise: Pinta's own beerName for "IIPPAA" is literally just that one word
# (a pun, no style text baked in at all) - a shop's fuller "IIPPAA West
# Coast Double IPA" made search_beers return ZERO results outright (not
# just a worse-ranked one), where "Pinta IIPPAA" alone finds it instantly.
# Stripping "West Coast" here (alongside the already-handled "IPA") leaves
# "IIPPAA Double" as the safe-strip variant, which _drop_trailing_words
# then reduces the rest of the way down to the bare catalog name.
_STYLE_WORDS_RE = re.compile(
    r"\b(stout|porter|ipa|lager|pils(?:ner)?|ale|sour|gose|saison|wheat|kriek|lambic|"
    r"witbier|weisse|bock|barleywine|quad(?:rupel)?|tripel|dubbel|munich helles|helles|"
    r"grodziskie|gr[ae]tzer|west[\s-]coast)\b",
    re.IGNORECASE,
)

# German compound style words ending in "-bier" (German for "beer") - a
# shop lists the full compound ("Weizenbier"), but Untappd's own catalog
# name for that exact beer sometimes drops the "-bier" and just uses the
# base word ("Weizen") - proven live: "Primator Weizenbier" found nothing,
# "Primator Weizen" found the exact beer uniquely. Only ever tried as a
# fallback variant, same reasoning as _STYLE_WORDS_RE above - "-bier" as a
# word-ending is common enough elsewhere that stripping it unconditionally
# would be riskier than trying it as one more rewrite.
_BIER_SUFFIX_RE = re.compile(r"\b(\w+)bier\b", re.IGNORECASE)

# A shop sometimes appends the beer's own ABV to its title ("Svijany Rytir
# 12%") - proven live this is NOT noise to unconditionally strip like the
# packaging words above: "Svijany Maz 11%" found the single exact
# "Svijanský Máz" match, while "Svijany Maz" alone (no %) came back with 5
# ambiguous candidates (several real flavour variants sharing that base
# name) - the percentage was actively HELPING disambiguate. So this is only
# ever tried as a fallback (after the as-is, %-included name already
# failed), for the opposite (rarer) case where the % itself is what a shop
# adds but Untappd's own catalog name doesn't carry.
_ABV_PERCENT_RE = re.compile(r"\b\d{1,2}(?:[.,]\d+)?\s*%")

# A Polish shop's own title describes a beer with an adjective agreeing
# with the implicit noun "piwo" (beer, grammatically neuter - "...skie"),
# but Untappd's catalog name often agrees with a DIFFERENT noun instead
# (e.g. the loanword "Pils", grammatically masculine - "...ski") - proven
# live: "Raciborskie Pils" (shop's own neuter form) came back ambiguous (2
# candidates, neither an exact/superset token match), while "Raciborski
# Pils" (masculine, Untappd's actual name) found the one real match
# uniquely. Only ever tried as a fallback - most shop titles already use
# whichever gender happens to match.
_POLISH_SKIE_ADJECTIVE_RE = re.compile(r"\b(\w+)skie\b", re.IGNORECASE)

# A shop sometimes describes a beer's flavour/style with a POLISH word
# instead of whatever Untappd's own catalog name actually uses - proven
# live twice: a shop's "LITOVEL MIODOWY" (Polish "honey-flavoured") only
# matched Untappd's real "Litovel Medový speciál" (Czech "medový") once
# translated - the Polish spelling alone came back ambiguous even with the
# brewery name included; a shop's "AleBrowar Kwas Chlebowy JASNY" (Polish
# "light/pale") returned ZERO results, while Untappd's real name for that
# exact variant is the English "Kwas Chlebowy Light". A third case is a
# plain catalog SPELLING variant rather than a translation - proven live:
# Maryensztadt's "New Black: Bakalia w Czekoladzie" is Untappd's own name,
# but a shop's title says "Bakalie" (the ordinary Polish plural of
# "bakalia", "assorted dried fruit/nuts") - close enough for Untappd's own
# fuzzy search to still find it, but not an exact/superset token match
# either way. Not a general rule (no pattern could infer any of these), so
# a small hand-maintained pair list, same spirit as KNOWN_TERM_SUBSTITUTIONS
# - but tried as a fallback VARIANT rather than substituted unconditionally,
# since these words are also perfectly normal Polish words elsewhere and
# blindly rewriting every occurrence would break those.
_FLAVOR_TRANSLATION_RE = re.compile(
    r"\b(miodowy|miodowe|miodowa|jasny|jasne|jasna|bakalie)\b", re.IGNORECASE
)
_FLAVOR_TRANSLATIONS = {
    "miodowy": "Medový", "miodowe": "Medový", "miodowa": "Medový",
    "jasny": "Light", "jasne": "Light", "jasna": "Light",
    "bakalie": "Bakalia",
}


def _brewery_query_base(brewery_name: str) -> str:
    """Prefix/collab cleanup only (no trailing-generic-word stripping) -
    used where a single representative brewery string is needed (e.g.
    BREWERY_TERM_SUBSTITUTIONS' substring match), not for building a
    search query itself."""
    cleaned = _BREWERY_PREFIX_RE.sub("", brewery_name or "").strip()
    cleaned = _COLLAB_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned or (brewery_name or "").strip()


def _brewery_query_variants(brewery_name: str) -> list[str]:
    """Ordered brewery-name variants to try. For the trailing (suffix)
    position, least-aggressively-stripped first: stripping exactly one
    trailing generic word, then two, etc. - see _BREWERY_SUFFIX_WORD_RE for
    why this is progressive rather than one greedy strip; the unstripped
    SUFFIX form is deliberately never tried on its own - proven live (see
    _BREWERY_PREFIX_RE) that leaving a genuine generic word at the END
    reliably returns zero results, so it's never worth the search call.

    For the LEADING (prefix) position though, both the prefix-kept and
    prefix-stripped forms are tried (prefix-kept first) - unlike the
    suffix case, a leading "Browar"/"Brewery" etc. isn't reliably safe to
    drop: proven live both ways - "Browar Lubrow" needs it stripped
    (catalogued as plain "Lubrow"), but "Browar Jana" needs it KEPT
    (catalogued as "Browar Jana" - "Browar" is part of this one's actual
    name, not a generic descriptor, same idea as ontap.pl's "AleBrowar"
    suffix case). Trying prefix-kept first costs nothing when it's wrong
    (proven live: a genuinely generic kept prefix just returns zero
    results, same as the suffix case, so it's a harmless first attempt)."""
    collab_stripped = _COLLAB_SUFFIX_RE.sub("", brewery_name or "").strip() or (brewery_name or "").strip()
    prefix_stripped = _BREWERY_PREFIX_RE.sub("", collab_stripped).strip()

    prefix_forms = [collab_stripped]
    if prefix_stripped and prefix_stripped.lower() != collab_stripped.lower():
        prefix_forms.append(prefix_stripped)

    variants: list[str] = []
    for form in prefix_forms:
        current = form
        suffix_variants = []
        while True:
            stripped = _BREWERY_SUFFIX_WORD_RE.sub("", current).strip()
            if not stripped or stripped == current:
                break
            suffix_variants.append(stripped)
            current = stripped
        variants.extend(suffix_variants or [form])

    seen: set[str] = set()
    deduped = []
    for v in variants:
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            deduped.append(v)
    # Proven live (a genuine crash, not just a bad match): a shop card with
    # NO brewery text at all (brewery_name="") - e.g. a glassware/merch item
    # scraped without a "Brand: Name" colon to split on - makes every
    # variant above collapse to "" too, which the `if v and ...` guard then
    # drops entirely, leaving deduped EMPTY. _query_context unconditionally
    # indexes brewery_variants[0], so an empty list here crashed the whole
    # /api/lens/lookup batch with an uncaught IndexError. Callers always
    # need at least one (possibly blank) variant to index into.
    return deduped or [brewery_name or ""]


def _collapse_non_alco_markers(text: str, replacement: str | None = None) -> str:
    """Collapses every non-alco trigger match (see _NON_ALCO_TRIGGER_RE) in
    text down to a single occurrence - the first one, replaced with
    `replacement` if given, else left as its own original text - dropping
    every further one entirely. A shop's title sometimes carries the
    concept TWICE at once (a word AND a bare "0.0%" callout, e.g. "Zlaty
    Bazant Nealko 0.0%") - proven live this is actively harmful, not just
    redundant: leaving both in can coincidentally return exactly ONE WRONG
    search result (a flavoured "Radler 0.0" sibling, not the plain beer)
    that then gets blindly trusted (see pick_best_match's single-result
    rule), while collapsing to one marker ("Zlaty Bazant Nealko") finds the
    real beer via an exact token match instead. A no-op when there's
    nothing to change: zero matches, or exactly one and no replacement was
    requested."""
    matches = list(_NON_ALCO_TRIGGER_RE.finditer(text or ""))
    if not matches or (replacement is None and len(matches) < 2):
        return text or ""
    parts = []
    last_end = 0
    for i, m in enumerate(matches):
        parts.append(text[last_end:m.start()])
        if i == 0:
            parts.append(replacement if replacement is not None else m.group(0))
        last_end = m.end()
    parts.append(text[last_end:])
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _clean_beer_name_query(beer_name: str, brewery_name: str = "") -> str:
    cleaned = _apply_known_terms(beer_name or "", brewery_name)
    cleaned = _PACKAGING_SUFFIX_RE.sub("", cleaned)
    cleaned = _NOISE_WORDS_RE.sub("", cleaned)
    cleaned = _KRAFT_ROKU_RE.sub("", cleaned)
    cleaned = _IN_OUT_RE.sub("", cleaned)
    cleaned = _POLISH_VINTAGE_RE.sub("", cleaned)
    cleaned = _POLISH_AND_RE.sub("", cleaned)
    cleaned = _POLISH_WITH_RE.sub("", cleaned)
    cleaned = _collapse_non_alco_markers(cleaned)
    # Cleanup above can leave a dangling "&"/"/" behind (e.g. stripping
    # "Gluten" out of "Gluten & Alcohol Free" leaves "& Alcohol Free") -
    # proven live this stray connector alone still broke the search.
    cleaned = _DANGLING_CONNECTOR_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or (beer_name or "").strip()


def _brewery_prefix_stripped_variant(beer_name: str, brewery_core: str) -> str | None:
    """Some shops don't split brewery from beer name at all - the product
    TITLE itself is the brewery name immediately followed by the beer name,
    with no separator (proven live: hoptimaal.com's own listings are
    literally "<Vendor> <BeerName>", e.g. vendor "FrauGruber Brewing" +
    title "FrauGruber The Pretender"). The untouched name usually still
    finds the beer fine even with the brewery duplicated at the front (this
    search backend tolerates a repeated-but-correct word - proven live), so
    this is only tried as a fallback for when that duplication is what's
    making an otherwise-unique match ambiguous. Strips brewery_core's
    words from the FRONT of beer_name only if they match there exactly, in
    order, case-insensitively; returns None otherwise (most sites' beer
    names never start with the brewery's own name, so this is a no-op for
    them)."""
    core_words = (brewery_core or "").split()
    name_words = (beer_name or "").split()
    if not core_words or len(name_words) <= len(core_words):
        return None
    # A "Brewery: Beer name" title leaves the separator glued to the last
    # brewery word ("AleSmith: BA Speedway Stout") - without ignoring it that
    # never compared equal to the bare brewery name, so the duplicated
    # brewery stayed in the query (and in the superset comparison).
    if [w.lower().rstrip(":,;") for w in name_words[: len(core_words)]] != [w.lower() for w in core_words]:
        return None
    return " ".join(name_words[len(core_words):]).strip() or None


def _style_stripped_variant(text: str) -> str | None:
    stripped = _STYLE_WORDS_RE.sub("", text or "")
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return stripped if stripped and stripped.lower() != (text or "").strip().lower() else None


def _bier_suffix_variant(text: str) -> str | None:
    stripped = _BIER_SUFFIX_RE.sub(lambda m: m.group(1), text or "")
    return stripped if stripped and stripped.lower() != (text or "").strip().lower() else None


def _abv_percent_stripped_variant(text: str) -> str | None:
    stripped = _ABV_PERCENT_RE.sub("", text or "")
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return stripped if stripped and stripped.lower() != (text or "").strip().lower() else None


def _polish_skie_variant(text: str) -> str | None:
    stripped = _POLISH_SKIE_ADJECTIVE_RE.sub(lambda m: m.group(1) + "ski", text or "")
    return stripped if stripped and stripped.lower() != (text or "").strip().lower() else None


def _flavor_translation_variant(text: str) -> str | None:
    if not _FLAVOR_TRANSLATION_RE.search(text or ""):
        return None
    translated = _FLAVOR_TRANSLATION_RE.sub(
        lambda m: _FLAVOR_TRANSLATIONS.get(m.group(0).lower(), m.group(0)), text
    )
    return translated if translated.lower() != (text or "").strip().lower() else None


def _non_alco_variants(beer_name: str) -> list[str]:
    """If beer_name mentions "non-alcoholic" in any common spelling (or a
    bare "0%"), return rewritten variants substituting each spelling
    Untappd's own catalog is known to use for this concept, PLUS one more
    variant with the marker dropped entirely. Untappd isn't consistent
    about which form a given brewery's entry uses - proven live five
    different ways: one brewery's entry needed "Bezalko", another needed
    the full "Bezalkoholowe" (truncating it to "Bezalko" returned zero
    results for THAT one), an English-market one needed "Non-Alcoholic", a
    Czech one (Litovel) needed "Nealko", and two others ("Amber Bezalkoholowe
    Zanzi" -> catalogued as plain "Zanzi"; "Freeky Jasny Lager Bezalkoholowe"
    -> catalogued as plain "Freeky Jasny Lager") needed the marker gone
    entirely, no replacement word at all - so all five are tried rather
    than guessing which a given brewery/beer uses. Uses
    _collapse_non_alco_markers rather than a plain substitute-every-match,
    in case beer_name still has more than one trigger occurrence (normally
    it won't - _clean_beer_name_query already collapses that upstream - but
    this stays correct even if called directly on un-cleaned text)."""
    if not _NON_ALCO_TRIGGER_RE.search(beer_name or ""):
        return []
    return [
        v for v in (
            _collapse_non_alco_markers(beer_name, spelling)
            for spelling in ("Bezalko", "Bezalkoholowe", "Non-Alcoholic", "Nealko", "")
        )
        if v  # the "" (drop entirely) spelling can leave nothing at all if the marker was the whole name
    ]


# Trailing words that distinguish a different product rather than add noise
# (see _drop_trailing_words): barrel ageing and nitro serves are separate beers.
_NEVER_DROP_WORDS = frozenset({"ba", "barrel", "aged", "barrel-aged", "nitro"})


def _drop_trailing_words(text: str, max_drop: int = 2):
    """Yields `text` with its last 1..max_drop words progressively removed.
    A shop's own product title sometimes carries one or two extra trailing
    words a cleanup regex above has no specific rule for - a wrong flavor-
    descriptor word ("Coconut Cream" vs the catalog's "Coconut Milk"), a
    release year that isn't part of the real name, a style phrase ("Czech
    Pilsner") - each confirmed live to make this search backend return
    nothing at all when left in, and confirmed live to find the beer once
    dropped. Only ever removes from the END - a shop abbreviating/altering
    a SUFFIX is the observed pattern, not a prefix."""
    words = text.split()
    for n in range(1, max_drop + 1):
        if len(words) - n < 1:
            break
        # A word that changes WHICH beer this is must never be dropped: with
        # "BA" gone, "Imperial Baltic Porter BA" exactly matched the plain
        # (non-barrel-aged) "Imperial Baltic Porter" - a different beer, found
        # in the lens crawl's outcome log. Dropping a larger tail only
        # includes the same word again, so stop here.
        if any(re.sub(r"[^\w-]", "", w).lower() in _NEVER_DROP_WORDS for w in words[len(words) - n:]):
            break
        yield " ".join(words[: len(words) - n])


def _query_name_variants(clean_name: str, *brewery_cores: str) -> list[str]:
    """Ordered, deduplicated list of beer-name variants to try, most-
    confident-first - see the comment above `base_variants` below for the
    exact ordering and why it's split into "safe" vs "situational"
    reductions, each optionally combined with a brewery-prefix strip
    against every brewery_core given (see _brewery_prefix_stripped_variant
    - a no-op unless the name literally starts with that core's own
    words). Multiple brewery_cores matter when a BREWERY_OVERRIDE_BY_NAME_TERM
    is in play - proven live (Maryensztadt's "Freeky" line): the beer name
    still duplicates the ORIGINAL shop-provided brewery ("Maryensztadt"),
    not the override being searched under ("FREEKY non-alcoholic"), so the
    prefix-strip needs to check against BOTH to find the duplication no
    matter which one the current search attempt is using. Every base
    variant also gets progressively shorter (trailing-word-dropped)
    versions tried. Capped so one stubborn beer can't blow up a batch lens
    lookup into a dozen+ search_beers calls."""
    # Two categories, treated differently: "safe" reductions (style/-bier
    # words) are NEVER part of a real catalog name (see _STYLE_WORDS_RE),
    # so their brewery-compounded forms are trusted early. "Situational"
    # reductions (ABV%, Polish adjective gender, flavour translation, non-
    # alco substitution) each involve either keeping-vs-changing genuinely
    # meaningful info or a speculative word GUESS - proven live both can
    # misfire when compounded too eagerly, so the plain brewery-prefix
    # strip (keeping this info exactly as printed) is tried BEFORE any of
    # them get their turn.
    safe_strips = []
    for stripper in (_style_stripped_variant, _bier_suffix_variant):
        stripped = stripper(clean_name)
        if stripped:
            safe_strips.append(stripped)
    situational_strips = []
    for stripper in (_abv_percent_stripped_variant, _polish_skie_variant, _flavor_translation_variant):
        stripped = stripper(clean_name)
        if stripped:
            situational_strips.append(stripped)
    non_alco_strips = _non_alco_variants(clean_name)

    cores = [c for c in dict.fromkeys(brewery_cores) if c]

    def _compounds(strips: list[str]) -> list[str]:
        out = []
        for strip in strips:
            for core in cores:
                compound = _brewery_prefix_stripped_variant(strip, core)
                if compound:
                    out.append(compound)
        return out

    # Ordering, most-confident-first: the name as-is; every SAFE reduction
    # compounded with a brewery-prefix strip (proven live necessary -
    # onemorebeer.pl's "PRIMÁTOR PRIMÁTOR PREMIUM LAGER": stripping ONLY
    # the brewery prefix (keeping "LAGER") matched a real but WRONG sibling
    # beer via the superset rule, only stripping BOTH found the actual
    # exact match); the plain brewery-prefix strip alone, keeping every
    # situational word exactly as printed (proven live necessary -
    # "Staropolski Kultowe Prozdrowotne 0,0%" and "Svijany Maz 11%" both
    # needed the ABV kept literal, just the duplicated brewery gone); each
    # SAFE reduction uncompounded; then situational reductions and non-alco
    # substitutions, compounded and uncompounded, in that order - proven
    # live these can misfire when tried any earlier (a non-alco-
    # substituted-but-still-brewery-duplicated variant, "Browar Jana BROWAR
    # JANA Non-Alcoholic", confidently matched a totally unrelated beer).
    base_variants = [clean_name]
    base_variants.extend(_compounds(safe_strips))
    for core in cores:
        brewery_prefix_stripped = _brewery_prefix_stripped_variant(clean_name, core)
        if brewery_prefix_stripped:
            base_variants.append(brewery_prefix_stripped)
    base_variants.extend(safe_strips)
    base_variants.extend(_compounds(situational_strips))
    base_variants.extend(situational_strips)
    base_variants.extend(_compounds(non_alco_strips))
    base_variants.extend(non_alco_strips)

    variants = list(base_variants)
    for base in base_variants:
        variants.extend(_drop_trailing_words(base))

    # A degenerate variant - one that IS the brewery's own name and
    # nothing else - carries zero product-identifying information, but can
    # still coincidentally return a confident (exact/superset) match:
    # proven live, "Birbant" alone (the tail end of _drop_trailing_words
    # stripping "HERO%" entirely off "BIRBANT HERO%") uniquely superset-
    # matched "Collab PL: Birbant" - a totally unrelated PINTA collab that
    # only credits Birbant BY NAME inside its own beerName, not the actual
    # "Hero%" product on the shelf. Never worth trying - drop these rather
    # than let them roll the dice.
    core_keys = {c.lower() for c in cores}
    variants = [v for v in variants if v.lower() not in core_keys]

    seen: set[str] = set()
    deduped = []
    for v in variants:
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            deduped.append(v)
    return deduped[:10]


async def _search_and_match(token: str, brewery: str, beer_name: str) -> tuple[dict | None, str, list[dict]]:
    query = f"{brewery} {beer_name}".strip()
    results = await untappd_mcp.search_beers(token, query, limit=SEARCH_RESULT_LIMIT)
    if not results:
        logger.info(f"resolve_beer: no results for query={query!r}")
        return None, query, []
    match = pick_best_match(results, beer_name, brewery)
    if match is None:
        logger.info(
            "resolve_beer: ambiguous, no confident match: query=%r (candidates: %s)",
            query, [r.get("beerName") for r in results],
        )
    return match, query, results


def build_search_url(brewery_name: str, beer_name: str) -> str:
    """Untappd's own search page for the given brewery/name text - a
    fallback link for the user to search manually themselves when nothing
    here could confidently pick one beer. Callers should pass the CLEANED
    text (e.g. resolve_beer's clean_brewery/clean_name), not the raw shop
    text - proven live: the raw text (e.g. "Brovarnia Gdańsk Brewery IPA")
    often doesn't find anything on Untappd's own search page either (the
    same "Brewery"-suffix problem search_beers has), which would defeat
    the point of offering a search link at all."""
    return f"https://untappd.com/search?q={quote(f'{brewery_name} {beer_name}'.strip())}&type=beer"


def _as_candidate(r: dict) -> dict:
    bid = r.get("bid")
    return {
        "name": r.get("beerName"),
        "brewery": (r.get("brewery") or {}).get("name"),
        "bid": bid,
        "url": f"https://untappd.com/beer/{bid}" if bid is not None else None,
    }


def _query_context(beer_name: str, brewery_name: str) -> tuple[list[str], str, str]:
    """Pure string-derived inputs shared by the (cached) identity search
    and the (always-fresh) not-found search URL: the ordered brewery-name
    variants to try, the original un-overridden/un-aliased brewery core
    (still needed by _query_name_variants for name-prefix-duplicate
    detection even when an override is in play - the beer name duplicates
    what the shop actually printed, not whatever brewery ends up being
    queried under), and the brewery-duplication-stripped clean beer name.
    Cheap (no I/O) - never cached, always recomputed from the CURRENT
    call's exact text."""
    brewery_variants = _brewery_query_variants(brewery_name)
    original_brewery_core = brewery_variants[0]
    brewery_override = _brewery_override(brewery_name, beer_name)
    if brewery_override:
        brewery_variants = [brewery_override] + brewery_variants
    # Appended at the END, tried only once the shop-provided brewery
    # variants have all failed - see BREWERY_ALIASES for why (the shop's
    # own brewery name already works fine for MOST of that brewery's own
    # beers, this only helps the exceptions).
    for alias in _brewery_aliases(brewery_name):
        if alias not in brewery_variants:
            brewery_variants.append(alias)
    brewery_base = _brewery_query_base(brewery_name)
    clean_name = _clean_beer_name_query(beer_name, brewery_base)
    return brewery_variants, original_brewery_core, clean_name


def _not_found_search_url(beer_name: str, brewery_name: str) -> str:
    brewery_variants, original_brewery_core, clean_name = _query_context(beer_name, brewery_name)
    # Same "most reduced" name the search retry loop itself would end up
    # trying (see _query_name_variants) - proven live necessary
    # (onemorebeer.pl's "Litovel Litovel Černy Citron 4%"): the plain
    # clean_name still has the brewery duplicated AND a trailing ABV
    # percent, and Untappd's own search page finds nothing for that
    # either, same as the raw text this replaced earlier.
    _url_brewery_prefix_stripped = (
        _brewery_prefix_stripped_variant(clean_name, original_brewery_core)
        or _brewery_prefix_stripped_variant(clean_name, brewery_variants[-1])
    )
    _url_abv_stripped = _abv_percent_stripped_variant(clean_name)
    url_name = (
        (_url_brewery_prefix_stripped and _abv_percent_stripped_variant(_url_brewery_prefix_stripped))
        or _url_brewery_prefix_stripped
        or _url_abv_stripped
        or clean_name
    )
    return build_search_url(brewery_variants[-1], url_name)


async def _resolve_identity(token: str, beer_name: str, brewery_name: str) -> dict:
    """Which Untappd beer (if any) this (name, brewery) text resolves to -
    the expensive, impersonal part of resolve_beer, cached process-wide
    (see the module-level cache comment). Returns EITHER
    {"matched": False, "candidates": [...]} or {"matched": True, "bid",
    "name", "brewery", "style", "abv", "rating", "ratingCount"} - never
    query_name/query_brewery/searchUrl (those must reflect the CURRENT
    call's exact text, not whatever casing first populated the cache -
    see _not_found_search_url) and never country/hadIt (see resolve_beer:
    country is cached separately by bid, hadIt is genuinely personal and
    never cached at all)."""
    cache_key = (beer_name.strip().lower(), brewery_name.strip().lower())
    cached = _cache_get(_identity_cache, cache_key)
    if cached is not None:
        return cached

    brewery_variants, original_brewery_core, clean_name = _query_context(beer_name, brewery_name)

    match, query = None, ""
    # Prefer the FIRST attempt's candidate list for the "couldn't tell
    # which one" fallback shown to the user (see _as_candidate below) - the
    # least-truncated query is also the one closest to what was actually
    # on the shelf/page, so its candidates are the most relevant set to
    # offer as alternatives. Only fall back to a later attempt's results if
    # the first one found literally nothing to show.
    first_results: list[dict] | None = None
    last_nonempty_results: list[dict] = []
    for brewery_variant in brewery_variants:
        # Recomputed per brewery_variant (cheap - pure string ops, no I/O):
        # the brewery-prefix-stripped name variant (see
        # _brewery_prefix_stripped_variant) needs to match against WHICHEVER
        # brewery form is being tried this iteration, not just one fixed
        # form - proven live necessary (onemorebeer.pl's "Browar Jana"): the
        # beer name duplicates the FULL "Browar Jana", but the fully-
        # stripped brewery variant is just "Jana", which doesn't match that
        # duplicate at all, so the name-side strip silently never fired.
        # original_brewery_core is passed alongside it for when an override
        # is in play (see _query_name_variants' docstring) - the beer name
        # still duplicates what the shop actually printed, not the override.
        name_variants = _query_name_variants(clean_name, brewery_variant, original_brewery_core)
        for candidate_name in name_variants:
            match, query, results = await _search_and_match(token, brewery_variant, candidate_name)
            if first_results is None:
                first_results = results
            if results:
                last_nonempty_results = results
            if match is not None:
                break
        if match is not None:
            break

    if match is None:
        source = first_results if first_results else last_nonempty_results
        if source:
            # Last resort, no extra search_beers call (reuses results
            # already in hand): some shops don't split brewery from beer
            # name the way Untappd itself does - proven live, onemorebeer.pl's
            # "Miłosław: IPA" splits into brewery="Miłosław"/name="IPA", but
            # Untappd's real brewery for it is "Browar Fortuna" and the
            # catalog beerName is "Miłosław IPA" (the sub-brand folded INTO
            # the name, not a separate brewery at all) - every attempt above
            # excludes brewery_name's own tokens from the comparison by
            # design (see pick_best_match's docstring), so "Miłosław" never
            # once entered the comparison and the bare "IPA" leftover
            # superset-matched every other IPA from the same brewery too.
            # Retried here with brewery_name folded INTO the name text
            # instead of subtracted from it (brewery_name="" - nothing left
            # to exclude) - safe because it goes through the exact same
            # strict exact/superset rules, so it only ever succeeds when a
            # real catalog beerName happens to equal (or be a superset of)
            # that merged text, which a genuinely separate brewery/name pair
            # essentially never does.
            match = pick_best_match(source, f"{brewery_name} {clean_name}".strip(), "")
            if match is not None:
                query = f"{brewery_name} {clean_name}".strip()

    if match is None:
        source = first_results if first_results else last_nonempty_results
        identity = {"matched": False, "candidates": [_as_candidate(r) for r in source[:3]]}
    else:
        bid = match.get("bid")
        logger.info("resolve_beer: query=%r -> %r (bid=%s)", query, match.get("beerName"), bid)
        identity = {
            "matched": True,
            "bid": bid,
            "name": match.get("beerName") or beer_name,
            "brewery": (match.get("brewery") or {}).get("name") or brewery_name,
            "style": match.get("style") or "",
            "abv": match.get("abv"),
            "rating": match.get("globalRating"),
            "ratingCount": match.get("ratingCount"),
        }
    _cache_set(_identity_cache, cache_key, identity)
    return identity


async def resolve_beer(
    token: str, user_id: int, beer_name: str, brewery_name: str, *,
    need_country: bool = True, live_fallback: bool = True,
) -> dict:
    """Look up one beer by name/brewery on Untappd: clean up the query (see
    the module-level cleanup rules), search, accept only a confident match
    (see pick_best_match), then attach country + had-it status. Returns a
    plain dict (no text/HTML formatting, no badge computation - callers
    build their own presentation on top):

        {"matched": bool, "query_name", "query_brewery",
         "name", "brewery", "style", "abv", "bid", "url",
         "rating", "ratingCount", "country", "hadIt" (True/False/None)}

    When "matched" is False, only query_name/query_brewery plus
    "candidates" (list of up to 3 {"name", "brewery", "bid", "url"} dicts,
    possibly empty) are present - search found genuine lookalikes but
    pick_best_match couldn't confidently choose between them (e.g. two
    same-named catalog entries, or a shop's title too generic to tell two
    real variants apart). An empty candidates list means search found
    nothing at all, not just nothing confident. Callers that only need a
    yes/no can ignore this field entirely; a caller with room to show a
    couple of alternative links (e.g. the lens endpoint) can offer the
    user a pick instead of a flat "not found".

    Raises untappd_mcp.UntappdMCPError on a search failure so the caller
    decides how to surface it (e.g. without aborting sibling lookups from
    the same batch).

    need_country=False skips the get_beer call entirely (country is only
    ever used for badge matching, e.g. /scan - callers with no badge
    computation, e.g. the lens endpoint, have no use for it).

    live_fallback=False skips the check_i_had_beer call when the local
    index is ambiguous (mid-resync), falling back to a plain "is this
    beer_id already in had_it_index" membership check instead of asking
    Untappd live. For a caller resolving dozens of beers from one shop page
    at once (the lens endpoint), both of these together eliminate every
    PERSONAL quota-costing call, leaving only the identity search below -
    which also sidesteps the real, tight per-second burst limit that a
    handful of concurrent quota calls was hitting in practice (observed
    live: nearly every get_beer/check_i_had_beer call 429'd when several
    ran at once). That identity search (which beer this text even refers
    to) is itself cached process-wide across ALL callers and users - see
    _resolve_identity - so a repeat or concurrent lookup of the same
    (name, brewery) text spends no quota at all beyond the first time."""
    identity = await _resolve_identity(token, beer_name, brewery_name)

    if not identity["matched"]:
        return {
            "matched": False, "query_name": beer_name, "query_brewery": brewery_name,
            "candidates": identity["candidates"],
            "searchUrl": _not_found_search_url(beer_name, brewery_name),
        }

    bid = identity["bid"]

    country = ""
    if bid is not None and need_country:
        # Cached by bid, separately from the identity cache above - a
        # beer's brewery/country never changes, so this is safe to reuse
        # even for a DIFFERENT (name, brewery) query text that happens to
        # resolve to the same bid.
        cached_country = _cache_get(_country_cache, bid)
        if cached_country is not None:
            country = cached_country
        else:
            try:
                detail = await untappd_mcp.get_beer(token, bid)
                country = ((detail.get("beer") or {}).get("brewery") or {}).get("country_name") or ""
                _cache_set(_country_cache, bid, country)
            except untappd_mcp.UntappdMCPError as exc:
                logger.warning(f"resolve_beer: get_beer failed for bid={bid}: {exc}")

    had_it = None
    if bid is not None:
        had_it_result = await had_it_index.lookup_had_it(user_id, bid)
        if had_it_result is None:
            if live_fallback:
                try:
                    live = await untappd_mcp.check_i_had_beer(token, bid)
                    had_it_result = {"hadIt": bool(live.get("hadIt"))}
                except untappd_mcp.UntappdMCPError as exc:
                    logger.warning(f"resolve_beer: check_i_had_beer failed for bid={bid}: {exc}")
            else:
                # No live quota call allowed - fall back to a direct
                # membership check instead of leaving this "unknown".
                # Safe even mid-resync: had_it_index's beers dict only ever
                # grows (record_page never removes a previously-known
                # beer), so "not present" is still a solid negative signal,
                # just possibly a little stale for something tried very
                # recently, right before the current backfill pass reached
                # it.
                beers = await had_it_index.get_all_beers(user_id)
                had_it_result = {"hadIt": str(bid) in beers}
        if had_it_result is not None:
            had_it = had_it_result.get("hadIt")

    return {
        "matched": True,
        "query_name": beer_name,
        "query_brewery": brewery_name,
        "name": identity["name"],
        "brewery": identity["brewery"],
        "style": identity["style"],
        "abv": identity["abv"],
        "bid": bid,
        "url": f"https://untappd.com/beer/{bid}" if bid is not None else None,
        "rating": identity["rating"],
        "ratingCount": identity["ratingCount"],
        "country": country,
        "hadIt": had_it,
    }


def classify_match(query_name: str, query_brewery: str, matched_name: str) -> tuple[str, int]:
    """How closely a resolved beer's catalog name agrees with the shop title
    it was resolved from, as (kind, delta) - for the lens outcome log
    (lens_log.py), so reviewing it can focus on the matches most likely to
    be WRONG instead of reading all of them. Compares the cleaned query
    (packaging/noise stripped, brewery tokens dropped) with the catalog name
    as token sets:
      "exact"            - identical;
      "candidate_longer" - the catalog name has extra words (delta = how
                           many) - the superset case;
      "query_longer"     - the shop title has words the catalog name lacks
                           (delta = how many) - matched via a shortened
                           query variant;
      "different"        - neither contains the other (delta = size of the
                           symmetric difference) - the most suspicious."""
    _, _, clean_name = _query_context(query_name, query_brewery)
    q = _token_set(clean_name) - _token_set(query_brewery)
    c = _token_set(matched_name)
    if q == c:
        return "exact", 0
    if q < c:
        return "candidate_longer", len(c - q)
    if c < q:
        return "query_longer", len(q - c)
    return "different", len(q ^ c)
