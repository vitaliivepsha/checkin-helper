import urllib.parse
import logging
import re
import unicodedata
import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)


STYLE_WORDS = {
    "ale", "lager", "ipa", "dipa", "tipa", "neipa", "pale", "stout", "porter",
    "sour", "gose", "lambic", "pilsner", "pils", "barleywine", "barley", "wine",
    "wheat", "wit", "saison", "farmhouse", "wild", "spontaneous", "spontan",
    "imperial", "double", "triple", "quadruple", "session", "american", "english",
    "west", "coast", "east", "new", "england", "hazy", "red", "black", "dark",
    "brown", "white", "blonde", "golden", "amber", "rye", "oat", "oats",
    "barrel", "barreled", "aged", "bourbon", "whiskey", "whisky", "rum", "wine",
    "pastry", "milk", "sweet", "dry", "hopped", "hoppy", "hop", "hops", "ddh",
    "fruit", "fruited", "smoothie", "blend", "blended", "wildflower",
    "vanilla", "chocolate", "cocoa", "coffee", "hazelnut", "hazelnuts", "marshmallow",
    "marshmallows", "coconut", "almond", "almonds", "pecan", "pecans", "cashew",
    "cashews", "peanut", "peanuts", "toffee", "caramel", "maple", "cinnamon",
    "banana", "berry", "berries", "raspberry", "strawberry", "blueberry", "cherry",
    "mango", "pineapple", "passionfruit", "lime", "lemon", "orange", "grapefruit",
    "with", "and", "the", "for", "from", "collab", "collaboration", "brew", "beer",
    "style", "abv", "w", "by", "x",
    # common short beer/style names; unsafe for global DB matching without brewery
    "helles", "kolsch", "koelsch", "kölsch", "bock", "weizen", "hefeweizen",
    "marzen", "märzen", "rauchbier", "altbier", "dubbel", "tripel",
    "quadrupel", "kriek", "gueuze", "geuze", "mead", "cider", "radler",
    "witbier", "grisette",
}


SPECIAL_CHAR_REPLACEMENTS = str.maketrans({
    "ø": "o", "Ø": "O",
    "œ": "oe", "Œ": "OE",
    "æ": "ae", "Æ": "AE",
    "å": "a", "Å": "A",
    "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "TH",
    "ł": "l", "Ł": "L",
})


def _normalize(text: str) -> str:
    if not text:
        return ""

    text = str(text).translate(SPECIAL_CHAR_REPLACEMENTS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = text.replace("×", " x ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return _normalize(text).split()


def _distinctive_tokens(text: str) -> set[str]:
    return {
        token
        for token in _tokens(text)
        if len(token) >= 4 and token not in STYLE_WORDS and not token.isdigit()
    }


BREWERY_NOISE_WORDS = {
    "brewery", "brewing", "company", "co", "bryggeri", "bryggeriet",
    "beer", "beers", "ales", "ale", "craft", "the", "and", "of",
}


def _brewery_search_tokens(text: str) -> set[str]:
    """Tokens that are useful when a user types brewery + beer together.

    Keep short numeric tokens so queries like "3 sons dope" can infer
    "3 Sons Brewing Co", but never let a number alone be enough to infer
    a brewery. Generic suffixes such as "Brewing Co" are ignored.
    """
    return {
        token
        for token in _tokens(text)
        if token and token not in BREWERY_NOISE_WORDS
    }


def _has_token_prefix_match(query_name: str, candidate_name: str) -> bool:
    """True when a typed token is a useful prefix/substring of a DB title.

    This is intentionally token-based, so manual searches like "dope" can
    include "Dopealicious" without opening the door to unrelated fuzzy
    matches.
    """
    query_tokens = [
        t for t in _tokens(query_name)
        if len(t) >= 4 and t not in STYLE_WORDS and not t.isdigit()
    ]
    candidate_tokens = [
        t for t in _tokens(candidate_name)
        if len(t) >= 4 and t not in STYLE_WORDS and not t.isdigit()
    ]
    for qtok in query_tokens:
        for ctok in candidate_tokens:
            if ctok.startswith(qtok) or qtok in ctok:
                return True
    return False


def _is_generic_style_query(query_norm: str) -> bool:
    tokens = [token for token in query_norm.split() if token]
    if not tokens or len(tokens) > 4:
        return False
    useful = [t for t in tokens if t not in {"w", "with", "and", "the"}]
    return bool(useful) and all(t in STYLE_WORDS or t.isdigit() for t in useful)


def _strip_brewery_tokens_from_query(query_beer: str, brewery_norm: str) -> str:
    if not query_beer or not brewery_norm:
        return query_beer
    brewery_tokens = _brewery_search_tokens(brewery_norm)
    if not brewery_tokens:
        return query_beer
    kept = [t for t in query_beer.split() if t not in brewery_tokens]
    return " ".join(kept).strip() or query_beer


def _candidate_brewery_hints_from_query(all_beers: list, query_beer: str, limit: int = 4) -> list[str]:
    """Infer brewery only when OCR likely put a brewery at the beginning of beer text.

    Good: "Svalbard Pale Ale", "Ology Seven Years".
    Bad:  "Irrefutable Logic" must not infer Bottle Logic;
          "Barrel Aged Stout" must not infer The Rare Barrel.
    """
    if not query_beer:
        return []

    tokens = query_beer.split()
    query_tokens = set(tokens)
    token_pos = {token: idx for idx, token in enumerate(tokens)}
    scored: list[tuple[int, str]] = []
    seen = set()

    for beer in all_beers:
        brewery = beer.get("brewery", "")
        brewery_norm = _normalize(brewery)
        if not brewery_norm or brewery_norm in seen:
            continue
        seen.add(brewery_norm)

        btokens = _brewery_search_tokens(brewery_norm)
        overlap = query_tokens & btokens
        if not overlap:
            continue

        # Do not infer a brewery from a number alone ("3" is too broad),
        # but allow it as part of a brewery prefix such as "3 sons ...".
        if not any(not token.isdigit() for token in overlap):
            continue

        earliest = min(token_pos.get(t, 99) for t in overlap)
        # Only trust brewery inference when the brewery-looking part is at the
        # start of the user's/OCR text. Including numeric brewery tokens means
        # "3 sons dope" starts at 0 and correctly maps to 3 Sons Brewing Co.
        if earliest > 1:
            continue

        # Prefer matches that cover more brewery tokens, especially when the
        # first token is also part of the brewery name.
        starts_with_brewery = tokens and tokens[0] in btokens
        scored.append((len(overlap) * 100 + (25 if starts_with_brewery else 0) - earliest, brewery))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [b for _, b in scored[:limit]]


def _is_exact_or_substring(query: str, candidate: str) -> bool:
    query_norm = _normalize(query)
    candidate_norm = _normalize(candidate)

    if not query_norm or not candidate_norm:
        return False

    if query_norm == candidate_norm:
        return True

    # Allow substring only for reasonably specific names.
    if len(query_norm) >= 6 and query_norm in candidate_norm:
        return True
    if len(candidate_norm) >= 6 and candidate_norm in query_norm:
        return True

    return False


def _safe_name_match(query_name: str, candidate_name: str, score: float, brewery_known: bool) -> bool:
    """
    Guard against matching a style/description to a random beer name.

    Without brewery context, fuzzy matching must be stricter because tap cards often expose
    style text such as "Bourbon Barrel Aged Stout w/ Vanilla". Those phrases can otherwise
    match unrelated beer names that share common style/flavour words.
    """
    if _is_exact_or_substring(query_name, candidate_name):
        return score >= 75 if brewery_known else score >= 80

    query_distinctive = _distinctive_tokens(query_name)
    candidate_distinctive = _distinctive_tokens(candidate_name)
    overlap = query_distinctive & candidate_distinctive

    prefix_overlap = _has_token_prefix_match(query_name, candidate_name)

    if brewery_known:
        # Brewery already narrowed the search, so tolerate OCR noise and typed
        # prefixes. Example: "3 sons dope" -> brewery=3 Sons, beer=dope ->
        # Dopealicious.
        return score >= 60 and (bool(overlap) or prefix_overlap or score >= 84)

    # Global search: require a real title overlap or a very high fuzzy score.
    if not query_distinctive:
        return False

    if overlap and score >= 86:
        return True

    # Manual/global search should include title-prefix matches like
    # "dope" -> "Dopealicious", but still require a strong fuzzy score.
    if prefix_overlap and score >= 82:
        return True

    # Very short beer names like "Seven Years" should match exactly/near-exactly,
    # but not via style-only token overlap.
    if score >= 93 and len(query_distinctive) >= 2:
        return True

    # One-token names with a tiny OCR typo: Kreutswine -> Kreutzswine.
    if score >= 94 and query_distinctive:
        for qtok in query_distinctive:
            for ctok in candidate_distinctive:
                if fuzz.ratio(qtok, ctok) >= 90:
                    return True

    return False


def _name_score(query_norm: str, candidate_name: str) -> float:
    candidate_norm = _normalize(candidate_name)
    if not query_norm or not candidate_norm:
        return 0.0
    return max(
        fuzz.WRatio(query_norm, candidate_norm),
        fuzz.partial_ratio(query_norm, candidate_norm),
    )


def _brewery_matches(query_brewery: str, db_brewery: str) -> bool:
    if not query_brewery or not db_brewery:
        return False

    if query_brewery == db_brewery:
        return True

    query_tokens = _brewery_search_tokens(query_brewery)
    db_tokens = _brewery_search_tokens(db_brewery)
    overlap = query_tokens & db_tokens
    has_non_numeric_overlap = any(not token.isdigit() for token in overlap)

    # Avoid matching every "... Brewing Co" to every other "... Brewing Co".
    # Fuzzy brewery matching is only safe when at least one meaningful brewery
    # token overlaps, e.g. "pleasanti" -> "Pleasanti Street" or
    # "3 sons" -> "3 Sons Brewing Co".
    if overlap and has_non_numeric_overlap:
        if query_tokens <= db_tokens or db_tokens <= query_tokens:
            return True
        return fuzz.partial_ratio(query_brewery, db_brewery) >= 72

    # Substring matching is useful for exact text fragments, but only when the
    # query is not a single ambiguous character/number.
    if len(query_brewery) >= 4 and (query_brewery in db_brewery or db_brewery in query_brewery):
        return True

    return False


def find_beer_candidates(
    all_beers: list,
    beer_name: str,
    brewery_name: str = "",
    limit: int = 5,
) -> list[dict]:
    """
    Return several safe database candidates, best first.

    This is intentionally a little more tolerant than the automatic photo match because
    it is used after a human presses "Search in database" and can choose from buttons.
    """
    if not beer_name:
        return []

    query_beer = _normalize(beer_name)
    query_brewery = _normalize(brewery_name)

    # Do not globally auto-suggest generic style-only names like "Helles" / "Pale Ale".
    # They are valid visible names, but without brewery context they are too ambiguous.
    if not query_brewery and _is_generic_style_query(query_beer):
        return []

    # If OCR put brewery into beer text, use it as context first: "Svalbard Pale Ale".
    if not query_brewery:
        for inferred_brewery in _candidate_brewery_hints_from_query(all_beers, query_beer):
            inferred = find_beer_candidates(all_beers, beer_name, inferred_brewery, limit=limit)
            if inferred:
                return inferred

    ranked: list[tuple[float, dict]] = []
    seen_ids: set[str] = set()

    def add_candidate(beer: dict, score: float, brewery_known: bool):
        beer_id = str(beer.get("id", ""))
        if not beer_id or beer_id in seen_ids:
            return
        if _safe_name_match(
            query_beer,
            beer.get("name", ""),
            score,
            brewery_known=brewery_known,
        ):
            seen_ids.add(beer_id)
            ranked.append((float(score), beer))

    # 1) If we have brewery context, search that brewery first.
    if query_brewery:
        brewery_subset = [
            beer for beer in all_beers
            if _brewery_matches(query_brewery, _normalize(beer.get("brewery", "")))
        ]

        scored_subset = []
        for beer in brewery_subset:
            db_brewery_norm = _normalize(beer.get("brewery", ""))
            candidate_query = _strip_brewery_tokens_from_query(query_beer, db_brewery_norm)
            # When the user typed brewery + beer together, score the title
            # against the stripped beer part only. Otherwise "3 sons dope"
            # can accidentally match 3 Sons beer "Three^3" via the brewery
            # tokens instead of the actual beer token "dope".
            score = _name_score(candidate_query, beer.get("name", ""))
            if candidate_query == query_beer:
                score = max(score, _name_score(query_beer, beer.get("name", "")))
            if _has_token_prefix_match(candidate_query, beer.get("name", "")):
                score += 12
            if score >= 55:
                scored_subset.append((score, beer))

        for score, beer in sorted(scored_subset, key=lambda item: item[0], reverse=True):
            add_candidate(beer, score, brewery_known=True)
            if len(ranked) >= limit:
                return [beer for _, beer in ranked[:limit]]

        if ranked:
            return [beer for _, beer in ranked[:limit]]

    # 2) Combined brewery + beer search helps when OCR captured both imperfectly.
    if query_brewery:
        combined_keys = [
            f"{_normalize(beer.get('brewery', ''))} {_normalize(beer.get('name', ''))}"
            for beer in all_beers
        ]
        combined_results = process.extract(
            f"{query_brewery} {query_beer}",
            combined_keys,
            scorer=fuzz.WRatio,
            limit=10,
            score_cutoff=86,
        )
        for _, combined_score, idx in combined_results:
            beer = all_beers[idx]
            db_brewery_norm = _normalize(beer.get("brewery", ""))
            if not _brewery_matches(query_brewery, db_brewery_norm):
                continue
            candidate_query = _strip_brewery_tokens_from_query(query_beer, db_brewery_norm)
            name_score = _name_score(candidate_query, beer.get("name", ""))
            # Use combined_score only to choose what to inspect. The final safety
            # check must be based on the beer name itself; otherwise a brewery-only
            # match can leak unrelated beers into the suggestions.
            add_candidate(beer, name_score, brewery_known=True)
            if len(ranked) >= limit:
                return [beer for _, beer in ranked[:limit]]

    # If brewery context already produced safe results, do not pollute the list
    # with unrelated global matches. Global search is mainly a fallback for bad OCR brewery.
    if ranked:
        return [beer for _, beer in ranked[:limit]]

    # 3) Global beer-name search. Uses normalized names, so partial searches like
    # "my honning" correctly find "My Honningkage..." instead of "Lightning".
    scored_global = []
    for beer in all_beers:
        score = _name_score(query_beer, beer.get("name", ""))
        if _has_token_prefix_match(query_beer, beer.get("name", "")):
            score += 12
        if score >= 78:
            scored_global.append((score, beer))

    for score, beer in sorted(scored_global, key=lambda item: item[0], reverse=True):
        add_candidate(beer, score, brewery_known=False)
        if len(ranked) >= limit:
            break

    return [beer for _, beer in ranked[:limit]]


def find_beers_in_db(all_beers: list, beer_name: str, brewery_name: str = "") -> dict | None:
    candidates = find_beer_candidates(all_beers, beer_name, brewery_name, limit=1)
    if candidates:
        match = candidates[0]
        logger.info(
            "Beer match: query=%r brewery=%r -> %r / %r",
            beer_name,
            brewery_name,
            match.get("brewery", ""),
            match.get("name", ""),
        )
        return match

    logger.info("No safe beer match: query=%r brewery=%r", beer_name, brewery_name)
    return None

UNTAPPD_STYLE_SUFFIXES = tuple(
    tuple(_normalize(suffix).split())
    for suffix in (
        # Most common OCR pattern on tap boards: beer name followed by style.
        "fruited sour ale",
        "smoothie sour ale",
        "sour ale",
        "wild ale",
        "farmhouse ale",
        "pale ale",
        "india pale ale",
        "new england ipa",
        "hazy ipa",
        "west coast ipa",
        "east coast ipa",
        "double ipa",
        "triple ipa",
        "imperial ipa",
        "imperial stout",
        "barrel aged stout",
        "barrel aged imperial stout",
        "pastry stout",
        "milk stout",
        "oatmeal stout",
        "brown ale",
        "red ale",
        "amber ale",
        "blonde ale",
        "golden ale",
        "wheat ale",
        "cream ale",
        "american barleywine",
        "english barleywine",
        "barley wine",
        "barleywine",
        "pilsner",
        "pils",
        "lager",
        "helles",
        "saison",
        "gose",
        "gueuze",
        "geuze",
        "lambic",
        "porter",
        "stout",
        "ipa",
        "dipa",
        "tipa",
        "neipa",
        "witbier",
        "wit",
        "hefeweizen",
        "weizen",
        "kolsch",
        "koelsch",
        "kölsch",
        "bock",
        "mead",
        "cider",
    )
)

# Strip longer phrases first, e.g. "Fruited Sour Ale" before "Ale".
UNTAPPD_STYLE_SUFFIXES = tuple(
    sorted(UNTAPPD_STYLE_SUFFIXES, key=len, reverse=True)
)


def _untappd_query_tokens(text: str) -> list[tuple[str, str]]:
    """Return (original, normalized) word tokens for the Untappd query cleaner."""
    if not text:
        return []
    originals = re.findall(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+", str(text))
    return [
        (token, _normalize(token))
        for token in originals
        if _normalize(token)
    ]


def _remove_untappd_style_suffix(beer_name: str) -> str:
    """Remove only a trailing beer-style phrase from an Untappd fallback search.

    OCR often returns one combined title like "Smith Moves Fruited Sour Ale".
    Untappd search works better with the beer title alone, but the cleaner is
    conservative: it only removes a known style suffix when at least two title
    tokens remain, so real short names like "Nut Brown Ale" stay intact.
    """
    raw = str(beer_name or "").strip()
    if not raw:
        return ""

    raw = re.sub(
        r"(?:\s*[-–—,:|/()]?\s*)?\b\d{1,2}(?:[.,]\d{1,2})?\s*(?:%|abv\b)\s*$",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip(" -–—,:|/()")

    token_pairs = _untappd_query_tokens(raw)
    normalized = [norm for _, norm in token_pairs]

    for suffix in UNTAPPD_STYLE_SUFFIXES:
        if len(normalized) <= len(suffix):
            continue
        if tuple(normalized[-len(suffix):]) != suffix:
            continue

        kept = token_pairs[:-len(suffix)]
        # Keep at least two words from the title. This avoids turning
        # legitimate short names such as "Nut Brown Ale" into just "Nut".
        if len(kept) < 2:
            continue

        cleaned = " ".join(original for original, _ in kept).strip()
        if cleaned:
            return cleaned

    return raw


def search_untappd_web(beer_name: str, brewery_name: str = "") -> str:
    query_parts = []
    if brewery_name:
        query_parts.append(brewery_name)

    cleaned_beer_name = _remove_untappd_style_suffix(beer_name)
    query_parts.append(cleaned_beer_name or beer_name)

    query = " ".join(part.strip() for part in query_parts if str(part).strip())
    encoded = urllib.parse.quote_plus(query)
    search_url = f"https://untappd.com/search?q={encoded}&type=beer&sort=all"

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Encoding": "identity",
        }
        r = httpx.get(
            f"https://www.google.com/search?q={encoded}+site:untappd.com/b",
            headers=headers,
            timeout=5,
            follow_redirects=True,
        )
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/url?q=" in href and "untappd.com/b/" in href:
                direct = href.split("/url?q=")[1].split("&")[0]
                direct = urllib.parse.unquote(direct)
                if direct.startswith("https://untappd.com/b/"):
                    logger.info(f"Google found: {direct}")
                    return direct
    except Exception as e:
        logger.error(f"Google search error: {e}")

    return search_url
