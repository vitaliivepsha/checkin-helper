"""aiohttp server for the /checkin Telegram Mini App.

Started via asyncio.create_task from bot.py's post_init, alongside the
existing limited-mode background scheduler - see limited.py's
start_limited_background_tasks for the established pattern this mirrors.
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import time
from urllib.parse import parse_qsl

from aiohttp import web
from rapidfuzz import fuzz, process
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import auto_toast
import badge_index
import badge_stats
import checkin_queue
import comment_watch
import event_log
import festival_watch
import foursquare
import had_it_index
import untappd_mcp
import user_tokens
import venue_index

logger = logging.getLogger(__name__)

WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
CHECKIN_DRY_RUN = os.environ.get("CHECKIN_DRY_RUN", "").lower() in ("1", "true", "yes")

# badge_venue_categories.json is a static, bundled-with-the-repo reference
# file (scraped once from Untappd's public badge catalog, not user data), so
# it's loaded eagerly at import time rather than lazily like DATA_DIR-backed
# state. Builds category(lowercased) -> [badge names] for the "badge only"
# filter in handle_venues_nearby, so a matching venue can also say which
# badge(s) it counts toward - plus a badge-name -> icon URL lookup so the
# Mini App can show the actual badge thumbnail, not just its name.
_BADGE_CATEGORY_TO_BADGES: dict[str, list[str]] = {}
_BADGE_ICONS: dict[str, str] = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "badge_venue_categories.json"), encoding="utf-8") as _f:
        for _entry in json.load(_f)["badges"]:
            for _cat in _entry["categories"]:
                _BADGE_CATEGORY_TO_BADGES.setdefault(_cat.lower(), []).append(_entry["badge"])
            if _entry.get("icon"):
                _BADGE_ICONS[_entry["badge"]] = _entry["icon"]
except (OSError, json.JSONDecodeError, KeyError) as _e:
    logger.warning("Could not load badge_venue_categories.json: %s", _e)


def _matched_badges(categories: list[str]) -> list[dict]:
    """Which Untappd venue badges (if any) a place's Foursquare categories
    count toward, each as {"name", "icon"} (icon may be None for the one
    badge whose thumbnail URL couldn't be found this session). Empty list =
    not a badge-qualifying venue (or the venue simply has no categories
    Untappd cares about)."""
    names: list[str] = []
    for cat in categories or []:
        for badge in _BADGE_CATEGORY_TO_BADGES.get(cat.lower(), []):
            if badge not in names:
                names.append(badge)
    return [{"name": n, "icon": _BADGE_ICONS.get(n)} for n in names]

# Owner's fallback token: only used for this exact Telegram id, so an
# unregistered friend never silently inherits the owner's account.
OWNER_TELEGRAM_ID = os.environ.get("OWNER_TELEGRAM_ID", "")
FALLBACK_TOKEN = os.environ.get("UNTAPPD_MCP_TOKEN", "")

# Per-process cache-busting token for static assets - see handle_index.
_BUILD_VERSION = str(int(time.time()))

# Background lifetime "had-it" backfill pacing - see _had_it_backfill_loop.
# Deliberately conservative: the shared 100/rolling-hour Untappd quota should
# go to live festival search/check-ins first, backfill trickles in the rest.
HAD_IT_BACKFILL_INTERVAL_SECONDS = float(os.environ.get("HAD_IT_BACKFILL_INTERVAL_SECONDS", "30"))
HAD_IT_BACKFILL_IDLE_SLEEP_SECONDS = float(os.environ.get("HAD_IT_BACKFILL_IDLE_SLEEP_SECONDS", "600"))
HAD_IT_BACKFILL_PAGE_SIZE = int(os.environ.get("HAD_IT_BACKFILL_PAGE_SIZE", "50"))
HAD_IT_BACKFILL_MIN_REMAINING = int(os.environ.get("HAD_IT_BACKFILL_MIN_REMAINING", "20"))
HAD_IT_BACKFILL_RESYNC_COOLDOWN_SECONDS = float(os.environ.get("HAD_IT_BACKFILL_RESYNC_COOLDOWN_SECONDS", str(24 * 60 * 60)))

_backfill_task = None  # module-level, keeps the asyncio.create_task result alive (avoid GC)

# Background lifetime "visited venue" backfill pacing - see
# _venue_backfill_loop. Separate from HAD_IT_BACKFILL_* (two independent
# quota consumers, each backing off independently rather than sharing one
# threaded-through budget) - MIN_REMAINING is a bit higher since both now
# share the same quota headroom.
VENUE_BACKFILL_INTERVAL_SECONDS = float(os.environ.get("VENUE_BACKFILL_INTERVAL_SECONDS", "45"))
VENUE_BACKFILL_IDLE_SLEEP_SECONDS = float(os.environ.get("VENUE_BACKFILL_IDLE_SLEEP_SECONDS", "600"))
VENUE_BACKFILL_PAGE_SIZE = int(os.environ.get("VENUE_BACKFILL_PAGE_SIZE", "25"))
VENUE_BACKFILL_MIN_REMAINING = int(os.environ.get("VENUE_BACKFILL_MIN_REMAINING", "25"))
VENUE_BACKFILL_RESYNC_COOLDOWN_SECONDS = float(os.environ.get("VENUE_BACKFILL_RESYNC_COOLDOWN_SECONDS", str(24 * 60 * 60)))

_venue_backfill_task = None  # module-level, keeps the asyncio.create_task result alive (avoid GC)

# Auto-toast pacing - see _auto_toast_loop. Its own independent quota
# consumer, same reasoning as VENUE_BACKFILL_* above. Polls more eagerly
# than the two backfills (a toast is only useful while it's still fresh -
# unlike had-it/venue indexing, there's no value in it "eventually" landing
# hours later). Each tick costs one get_my_friend_feed call (covering every
# watched target at once, not one call per target) plus one toast_checkin
# per check-in actually toasted that tick.
AUTO_TOAST_INTERVAL_SECONDS = float(os.environ.get("AUTO_TOAST_INTERVAL_SECONDS", "60"))
AUTO_TOAST_IDLE_SLEEP_SECONDS = float(os.environ.get("AUTO_TOAST_IDLE_SLEEP_SECONDS", "300"))
AUTO_TOAST_MIN_REMAINING = int(os.environ.get("AUTO_TOAST_MIN_REMAINING", "25"))

# Same restriction as bot.py's AUTO_TOAST_OWNER_ID (kept as a separate env
# read, not a cross-import, matching this file's existing pattern of owning
# its own env-driven constants) - the Mini App tab/toggle stay hidden for
# everyone else, and the API routes below double-check it server-side too.
AUTO_TOAST_OWNER_ID = os.environ.get("AUTO_TOAST_OWNER_ID", "402733193")


def _is_auto_toast_owner(user_id) -> bool:
    return str(user_id) == AUTO_TOAST_OWNER_ID

_auto_toast_task = None  # module-level, keeps the asyncio.create_task result alive (avoid GC)

# Comment-watch pacing - see _comment_watch_loop. Can't ride along on
# auto-toast's shared feed poll (see comment_watch.py's docstring for why -
# a comment lands well after its check-in has scrolled past that poll's
# cursor), so this is its own independent, modest quota consumer: one
# get_user_checkins call per *enabled owner* per tick (not per target -
# there's only ever one "target," the owner's own check-ins), regardless
# of how many owners there are.
COMMENT_WATCH_INTERVAL_SECONDS = float(os.environ.get("COMMENT_WATCH_INTERVAL_SECONDS", "120"))
COMMENT_WATCH_IDLE_SLEEP_SECONDS = float(os.environ.get("COMMENT_WATCH_IDLE_SLEEP_SECONDS", "300"))
COMMENT_WATCH_MIN_REMAINING = int(os.environ.get("COMMENT_WATCH_MIN_REMAINING", "10"))
COMMENT_WATCH_CHECK_LIMIT = int(os.environ.get("COMMENT_WATCH_CHECK_LIMIT", "10"))

_comment_watch_task = None  # module-level, keeps the asyncio.create_task result alive (avoid GC)


@web.middleware
async def _no_cache_middleware(request: web.Request, handler):
    """Telegram's Mini App WebView has been observed to cache aggressively
    regardless of headers, but set this anyway - it's the correct behavior
    for a page whose content changes on every deploy, and some clients do
    honor it."""
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    return response


# Pre-loaded festival beer list (bot.py's ALL_BEERS - id/name/brewery/style/session),
# set once by start_webapp_server. Searched before falling back to live Untappd search.
_festival_beers: list = []

# The python-telegram-bot Bot, set once by start_webapp_server - lets
# _auto_toast_loop send festival_watch notifications directly, without a
# second bot instance or a round-trip back into bot.py.
_ptb_bot = None

# The full Application, kept alongside _ptb_bot specifically for
# .bot_data - _notify_new_comment stashes the commenter's username there
# keyed by checkin_id (mirrors bot.py's own f"beer:{bid}" caching
# convention) so the reply flow in bot.py's handle_callback can look it up
# without a second Untappd call. Not used for anything else here - the
# callback_data itself stays short (checkin_id only), since Telegram caps
# it at 64 bytes and a username could easily push that over.
_ptb_app = None

# bot.py's load_db() dedupes ALL_BEERS by beer id, keeping only the *first*
# session it saw a beer under - a beer poured across multiple sessions (e.g.
# 4 festival days) silently loses the rest. Rebuilt from SESSIONS_RAW (the
# undeduped {session: [beers]} dict bot.py already parses) so we can show
# every session a beer actually appears in. Keyed by the beer's raw string id
# (as it appears in mbcc_beers.json), not the int Untappd bid. Values are the
# session's *raw* key from the source JSON (e.g. "yellow" or "friday") - the
# real identity of a session; see _session_colors below for the cosmetic-only
# color assigned to each for display.
_beer_sessions: dict[str, list[str]] = {}

# Per-session set of int Untappd beer ids - the denominator for
# handle_festival_stats's "X of Y still un-tried per session" breakdown.
# Keyed by raw session key, same as _beer_sessions.
_session_beer_ids: dict[str, set] = {}

# The real, ordered list of session identities from the currently-loaded
# festival JSON (first-appearance order) - what handle_festival_stats/
# handle_festival_session actually iterate/validate against. A raw session
# key (e.g. "friday") is never renamed or merged with another one, however
# many sessions the source file has.
_session_order: list[str] = []

# raw session key -> cosmetic display color, purely for the UI dot/emoji.
_session_colors: dict[str, str] = {}

_SESSION_COLOR_PALETTE = ["yellow", "blue", "red", "green"]


def _assign_session_colors(raw_keys: list) -> dict:
    """Maps each raw session key from the source festival JSON to a display
    *color* for the UI (session dot/emoji) - purely cosmetic. A key that's
    already one of the 4 known color names keeps it unchanged (matches
    mbcc_beers.json's own "yellow"/"blue"/"red"/"green" keys); anything else
    (e.g. "friday"/"saturday") claims the next unclaimed color in the order
    it first appears, cycling through the same 4 once there are more than 4
    distinct sessions. A color can end up shared by two sessions this way -
    that's fine, since a session's real identity is always its raw key
    (_session_order/_session_beer_ids), never the color. Colors used to
    double as identity, which silently merged unrelated sessions into one
    bucket once a festival had more than 4 of them - see _session_order."""
    mapping: dict = {}
    taken = set()
    for key in raw_keys:
        if key in _SESSION_COLOR_PALETTE:
            mapping[key] = key
            taken.add(key)
    remaining = [c for c in _SESSION_COLOR_PALETTE if c not in taken]
    i = 0
    for key in raw_keys:
        if key in mapping:
            continue
        mapping[key] = remaining[i % len(remaining)] if remaining else _SESSION_COLOR_PALETTE[i % len(_SESSION_COLOR_PALETTE)]
        i += 1
    return mapping


def _sessions_for(raw_id) -> list[str]:
    found = _beer_sessions.get(str(raw_id), [])
    return [s for s in _session_order if s in found]

# All per-user caches below are keyed by Telegram user id - a shared global
# here would leak one friend's wishlist/had-it/venues into another's view.
_wishlist_cache: dict[int, dict] = {}
_WISHLIST_CACHE_TTL = 15 * 60  # seconds

_venue_cache: dict[int, dict] = {}
_VENUE_CACHE_TTL = 15 * 60  # seconds

# check_i_had_beer costs real Untappd quota per beer. A bulk alternative
# (get_user_beers with a date range) was tried and reverted - verified
# unreliable: two beers confirmed hadIt=true via check_i_had_beer did not
# appear even in a 12-day/166-result window, for reasons not evident from
# the API's documented behavior. Trust check_i_had_beer's direct answer;
# save quota by calling it for far fewer results per search instead
# (see _annotate_had_it's `limit`), not by trying to batch it.
#
# Cache per (user_id, beerId), TTL'd rather than indefinite - a "false"
# answer is only true until the person actually drinks it, which at a live
# festival can be minutes later (searching before drinking is the normal
# flow). handle_submit updates this cache immediately on a real check-in
# made *through this app*; a check-in via the real Untappd app directly is
# only picked up once the TTL expires and we ask again.
_had_it_cache: dict[int, dict[int, dict]] = {}
_HAD_IT_CACHE_TTL = 3 * 60  # seconds

# get_user_friends has no single-friend lookup and pages at 25/call - a full
# list (a heavy account here has 243 friends, ~10 calls) is comparatively
# expensive, but a friend list itself changes rarely, unlike had-it/venue
# state - a long TTL is appropriate (unlike the 15-min caches above).
_autotoast_friends_cache: dict[int, dict] = {}
_AUTOTOAST_FRIENDS_CACHE_TTL = 60 * 60  # seconds
_AUTOTOAST_FRIENDS_MAX_PAGES = 12  # caps one refresh at 12 calls (300 friends)


async def _resolve_token(tg_user: dict) -> str | None:
    """The calling Telegram user's own Untappd token, or the owner's
    fallback if they are the recognized owner and never registered one."""
    user_id = tg_user.get("id")
    token = await user_tokens.get_token(user_id)
    if token:
        return token
    if FALLBACK_TOKEN and OWNER_TELEGRAM_ID and str(user_id) == OWNER_TELEGRAM_ID:
        return FALLBACK_TOKEN
    return None


async def _get_had_it(user_id: int, beer_id, token: str) -> dict | None:
    # Consult the slowly-backfilled lifetime index first - free (no
    # network call) and, once fully synced for this user, authoritative
    # even for a "no" (not just "yes"). Falls through to the live per-beer
    # cache/check only while that index doesn't yet cover this beer.
    indexed = await had_it_index.lookup_had_it(user_id, beer_id)
    if indexed is not None:
        return indexed

    user_cache = _had_it_cache.setdefault(user_id, {})
    cached = user_cache.get(beer_id)
    if cached is not None:
        result, fetched_at, is_confirmed_true = cached
        # A confirmed "yes" never needs re-checking; a "no" is only true
        # until the next drink, so it expires after _HAD_IT_CACHE_TTL.
        if is_confirmed_true or time.time() - fetched_at < _HAD_IT_CACHE_TTL:
            return result
    try:
        result = await untappd_mcp.check_i_had_beer(token, beer_id)
    except untappd_mcp.UntappdMCPError as e:
        logger.warning("check_i_had_beer(%s) failed: %s", beer_id, e)
        return None
    user_cache[beer_id] = (result, time.time(), bool(result.get("hadIt")))
    return result


async def _annotate_had_it(beers: list[dict], user_id: int, token: str | None, limit: int = 5) -> None:
    """Mutates each beer dict in-place with hadIt/userRating, for the first
    `limit` results only - kept small (not 15) specifically to conserve the
    shared Untappd quota for actual check-ins during a live festival. Calls
    sequentially, not concurrently - a burst of parallel check_i_had_beer
    calls has been observed to trip Untappd's own rate limiting. No-ops
    without a token (viewer hasn't connected their own Untappd account
    yet)."""
    if not token:
        return
    for beer in beers[:limit]:
        bid = beer.get("beerId")
        if not bid:
            continue
        result = await _get_had_it(user_id, bid, token)
        if result:
            beer["hadIt"] = result.get("hadIt", False)
            beer["userRating"] = result.get("userRating")


def _fuzzy_match(query: str, keys: list[str], limit: int, score_cutoff: int = 60):
    """rapidfuzz process.extract with case-insensitive matching."""
    if not keys:
        return []
    return process.extract(
        query, keys, scorer=fuzz.WRatio, limit=limit,
        score_cutoff=score_cutoff, processor=lambda s: s.lower(),
    )


def _int_beer_id(b: dict) -> int | None:
    try:
        return int(b.get("id"))
    except (TypeError, ValueError):
        return None


def _search_festival_beers(query: str, limit: int = 10) -> list[dict]:
    if not _festival_beers:
        return []
    keys = [f"{b.get('brewery', '')} {b.get('name', '')}" for b in _festival_beers]
    hits = _fuzzy_match(query, keys, limit)
    results = []
    for _, _score, idx in hits:
        b = _festival_beers[idx]
        try:
            beer_id = int(b.get("id"))
        except (TypeError, ValueError):
            continue  # not a real Untappd bid - can't check-in, skip
        results.append({
            "beerId": beer_id,
            "name": b.get("name"),
            "brewery": b.get("brewery"),
            "style": b.get("style"),
            "abv": None, "ibu": None, "rating": None, "ratingCount": None,
            "labelUrl": None,
            "sessions": _sessions_for(b.get("id")),
            "source": "festival",
        })
    return results


async def _search_wishlist(query: str, user_id: int, token: str | None, limit: int = 10) -> list[dict]:
    if not token:
        return []
    cache = _wishlist_cache.get(user_id)
    now = time.time()
    if cache is None or now - cache["fetched_at"] > _WISHLIST_CACHE_TTL:
        try:
            cache = {"data": await untappd_mcp.get_my_wishlist(token), "fetched_at": now}
        except untappd_mcp.UntappdMCPError as e:
            logger.warning("get_my_wishlist failed: %s", e)
            cache = {"data": (cache or {}).get("data") or [], "fetched_at": now}
        _wishlist_cache[user_id] = cache

    beers = cache["data"] or []
    if not beers:
        return []
    keys = [f"{(b.get('brewery') or {}).get('name', '')} {b.get('beerName', '')}" for b in beers]
    hits = _fuzzy_match(query, keys, limit)
    return [
        {
            "beerId": (b := beers[idx]).get("bid"),
            "name": b.get("beerName"),
            "brewery": (b.get("brewery") or {}).get("name"),
            "style": b.get("style"),
            "abv": b.get("abv"), "ibu": b.get("ibu"),
            "rating": b.get("globalRating"), "ratingCount": b.get("ratingCount"),
            "labelUrl": b.get("labelUrl"),
            "sessions": [],
            "source": "wishlist",
        }
        for _, _score, idx in hits
    ]


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict | None:
    """Verify Telegram Mini App initData per Telegram's documented algorithm.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Returns the parsed field dict (with "user" json-decoded) on success, else None.
    """
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = pairs.get("auth_date")
    if auth_date:
        try:
            if time.time() - int(auth_date) > max_age_seconds:
                return None
        except ValueError:
            return None

    if "user" in pairs:
        try:
            pairs["user"] = json.loads(pairs["user"])
        except json.JSONDecodeError:
            pass
    return pairs


def _bot_token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


async def _require_valid_init_data(request: web.Request) -> dict | None:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return validate_init_data(init_data, _bot_token())


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def handle_index(request: web.Request) -> web.Response:
    # Telegram's Mini App WebView caches static assets aggressively (observed:
    # a phone kept showing a stale app.js after a server-side update while a
    # plain browser fetched the fresh file fine). Version-bust the script/style
    # URLs with a per-process token so every restart forces a fresh fetch.
    with open(os.path.join(WEBAPP_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/static/checkin/app.js", f"/static/checkin/app.js?v={_BUILD_VERSION}")
    html = html.replace("/static/checkin/style.css", f"/static/checkin/style.css?v={_BUILD_VERSION}")
    return web.Response(text=html, content_type="text/html")


async def handle_search(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    token = await _resolve_token(tg_user)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    query = (body.get("query") or "").strip()
    if len(query) < 2:
        return web.json_response({"beers": []})

    # festivalPriority/wishlistPriority control ORDER, not exclusivity - a
    # live global search (search_beers - Algolia-backed, free, doesn't spend
    # the shared quota) always still runs to fill out the rest, unlike the
    # earlier design where any local hit fully replaced the global search.
    # The two are independent toggles: festival (if on) always goes first,
    # wishlist (if on) always goes right after - regardless of whether the
    # other one is also on.
    festival_priority = bool(body.get("festivalPriority"))
    wishlist_priority = bool(body.get("wishlistPriority"))

    ordered: list[dict] = []
    seen_ids: set = set()

    def _extend(hits) -> None:
        for h in hits:
            bid = h.get("beerId")
            if bid is not None and bid not in seen_ids:
                seen_ids.add(bid)
                ordered.append(h)

    if festival_priority:
        _extend(_search_festival_beers(query))
    if wishlist_priority:
        _extend(await _search_wishlist(query, user_id, token))

    if token:
        global_results = []
        try:
            global_results = await untappd_mcp.search_beers(token, query)
        except untappd_mcp.UntappdRateLimited:
            if not ordered:
                return _json_error("rate_limited", 429)
        except untappd_mcp.UntappdMCPError as e:
            logger.warning("search_beers failed: %s", e)
            if not ordered:
                return _json_error("search_failed", 502)

        if global_results:
            # The MCP's raw results are ranked by popularity (ratingCount), not
            # text relevance - a query can surface unrelated, highly-rated beers
            # whose only connection is a collab mentioned in an alias (e.g.
            # "Zinnebir" outranking actual "Hoppy People" beers for the query
            # "Hoppy People"). Boost beers whose real name/brewery actually
            # contains the query text; stable sort keeps the original
            # popularity order within each group.
            query_lower = query.lower()

            def _relevance(b: dict) -> int:
                name = (b.get("beerName") or "").lower()
                brewery = ((b.get("brewery") or {}).get("name") or "").lower()
                return 0 if (query_lower in name or query_lower in brewery) else 1

            global_results.sort(key=_relevance)
            _extend(
                {
                    "beerId": b.get("bid"),
                    "name": b.get("beerName"),
                    "brewery": (b.get("brewery") or {}).get("name"),
                    "style": b.get("style"),
                    "abv": b.get("abv"),
                    "ibu": b.get("ibu"),
                    "rating": b.get("globalRating"),
                    "ratingCount": b.get("ratingCount"),
                    "labelUrl": b.get("labelUrl"),
                    "sessions": [],
                    "source": "untappd",
                }
                for b in global_results
            )

    beers = ordered[:20]
    await _annotate_had_it(beers, user_id, token)
    return web.json_response({"beers": beers})


async def handle_venues(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    token = await _resolve_token(tg_user)
    if not token:
        return _json_error("not_connected", 403)

    now = time.time()
    cache = _venue_cache.get(user_id)
    if cache is not None and now - cache["fetched_at"] < _VENUE_CACHE_TTL:
        return web.json_response({"venues": cache["data"]})

    try:
        venues_raw = await untappd_mcp.get_my_recent_venues(token)
    except untappd_mcp.UntappdRateLimited:
        return _json_error("rate_limited", 429)
    except untappd_mcp.UntappdMCPError as e:
        logger.warning("get_my_recent_venues failed: %s", e)
        return _json_error("venues_failed", 502)

    venues = [
        {
            "foursquareId": v.get("foursquareId"),
            "name": v.get("name"),
            "lat": v.get("lat"),
            "lng": v.get("lng"),
        }
        for v in venues_raw
    ]
    _venue_cache[user_id] = {"data": venues, "fetched_at": now}
    return web.json_response({"venues": venues})


async def handle_venues_nearby(request: web.Request) -> web.Response:
    """Venues near an arbitrary lat/lng and/or matching a text query, via
    Foursquare's Places API - not limited to venues already used on Untappd
    (unlike handle_venues above, Untappd itself has no venue search). At
    least one of lat/lng or query is required; both together give the most
    relevant results (geo-biased text search). No Untappd token is needed
    for the Foursquare call itself; a token is only needed to resolve
    user_id for the optional uniqueOnly filter, which degrades to a no-op
    without one - same "search still works, personalization doesn't"
    pattern as handle_search without a token."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    lat = body.get("lat")
    lng = body.get("lng")
    has_location = isinstance(lat, (int, float)) and isinstance(lng, (int, float))
    query = (body.get("query") or "").strip() or None
    if not has_location and not query:
        return _json_error("invalid_location")
    # A name search implies "find this place somewhere in the area", not
    # "what's right next to me" - confirmed for real: searching "Zoo" near
    # Wrocław with the tight 500m nearby-default missed the actual Zoo
    # (~2.9km away) entirely, leaving only closer but irrelevant text
    # matches (pet supply shops). Use Foursquare's real maximum (100000m -
    # confirmed live: 100000 works, 200000 gets a 400 Bad Request) so a name
    # search never misses a real match. Nearby-browsing (no query) keeps
    # the tighter default - "poruch" should mean nearby, not the whole region.
    radius = body.get("radius") or (100000 if query else 500)

    try:
        venues = await foursquare.search_nearby(
            lat if has_location else None, lng if has_location else None,
            query=query, radius=radius,
        )
    except foursquare.FoursquareRateLimited:
        return _json_error("rate_limited", 429)
    except foursquare.FoursquareError as e:
        logger.warning("foursquare search_nearby failed: %s", e)
        return _json_error("nearby_failed", 502)

    if body.get("uniqueOnly") and user_id:
        kept = []
        for v in venues:
            visited = await venue_index.lookup_visited(user_id, v["foursquareId"])
            if visited is not True:  # hide only on a confirmed visit
                kept.append(v)
        venues = kept

    if body.get("badgeOnly"):
        kept = []
        for v in venues:
            matched = _matched_badges(v.get("categories"))
            if matched:
                v["matchedBadges"] = matched
                kept.append(v)
        venues = kept

    return web.json_response({"venues": venues})


async def handle_usage(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    token = await _resolve_token(tg_user)
    if not token:
        return _json_error("not_connected", 403)

    try:
        usage = await untappd_mcp.get_untappd_api_usage(token)
    except untappd_mcp.UntappdMCPError as e:
        logger.warning("get_untappd_api_usage failed: %s", e)
        return _json_error("usage_failed", 502)

    last_seen = usage.get("lastSeen", {})
    instance = usage.get("instance", {})
    profile = await user_tokens.get_profile(tg_user.get("id"))
    return web.json_response({
        "limit": last_seen.get("limit"),
        "remaining": last_seen.get("remaining"),
        "callsLastHour": instance.get("callsLastHour"),
        "lastVenue": (profile or {}).get("last_venue"),
        "isAutoToastOwner": str(tg_user.get("id")) == AUTO_TOAST_OWNER_ID,
    })


async def handle_submit(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    token = await _resolve_token(tg_user)
    if not token:
        return _json_error("not_connected", 403)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    beer_id = body.get("beerId")
    rating = body.get("rating", 0)
    shout = (body.get("shout") or "").strip()
    foursquare_id = body.get("foursquareId")
    geolat = body.get("geolat")
    geolng = body.get("geolng")
    venue_name = body.get("venueName")
    queue_item_id = body.get("queueItemId")

    if not isinstance(beer_id, int) or beer_id <= 0:
        return _json_error("invalid_beer_id")
    if not isinstance(rating, (int, float)) or not (0 <= rating <= 5):
        return _json_error("invalid_rating")

    # Remembering the picked venue is pure local UX state (never touches
    # Untappd), so it's safe to do in both the dry-run and real branches -
    # the venue doesn't change for the whole festival, so this saves the
    # user from re-picking it on every check-in.
    if foursquare_id:
        await user_tokens.set_last_venue(user_id, {
            "foursquareId": foursquare_id, "name": venue_name,
            "lat": geolat, "lng": geolng,
        })

    if CHECKIN_DRY_RUN:
        would_send = {
            "beerId": beer_id, "rating": rating, "shout": shout,
            "foursquareId": foursquare_id, "geolat": geolat, "geolng": geolng,
        }
        logger.info("CHECKIN_DRY_RUN - would check in: %s", would_send)
        # Marking a queue item completed is purely our own local state - it
        # never touches Untappd - so it's safe (and useful) to exercise even
        # in dry-run, unlike the real check_in call below.
        if queue_item_id:
            await checkin_queue.mark_completed(queue_item_id, user_id)
        return web.json_response({"ok": True, "dryRun": True, "would_send": would_send})

    try:
        checkin = await untappd_mcp.check_in(
            token, beer_id=beer_id, rating=rating, shout=shout,
            foursquare_id=foursquare_id, geolat=geolat, geolng=geolng,
        )
    except untappd_mcp.UntappdRateLimited:
        return _json_error("rate_limited", 429)
    except untappd_mcp.UntappdMCPError as e:
        logger.warning("check_in failed: %s", e)
        return _json_error("checkin_failed", 502)

    # Reflect the fresh check-in immediately, without another quota-costing call.
    # Real check-in path only - never from the CHECKIN_DRY_RUN branch above,
    # which returns before reaching here, so a dry run never poisons the
    # lifetime index with a beer that was never actually drunk.
    _had_it_cache.setdefault(user_id, {})[beer_id] = ({"hadIt": True, "userRating": rating}, time.time(), True)
    await had_it_index.record_checkin(user_id, beer_id, rating)
    await venue_index.record_checkin(user_id, foursquare_id)

    if queue_item_id:
        await checkin_queue.mark_completed(queue_item_id, user_id)

    return web.json_response({"ok": True, "checkin": checkin})


def _all_festival_beer_ids() -> set:
    all_ids: set = set()
    for ids in _session_beer_ids.values():
        all_ids |= ids
    return all_ids


async def handle_festival_stats(request: web.Request) -> web.Response:
    """Personal "how much beer have I still not tried" - overall and per
    session. Per-viewer, like the rest of the app's personalized features
    (had-it badges, wishlist priority) - built entirely from had_it_index's
    already-synced personal history (no live per-beer Untappd calls here;
    with ~824 festival beers, a live check_i_had_beer fallback for every
    unknown one would demolish the shared quota in a single screen open).
    Without a connected account there's simply no personal history to draw
    on, so everything shows as not-yet-tried - same degrade as the had-it
    badge elsewhere."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    all_ids = _all_festival_beer_ids()

    tried_ids: set = set()
    for bid in all_ids:
        result = await had_it_index.lookup_had_it(user_id, bid)
        if result and result.get("hadIt"):
            tried_ids.add(bid)

    sessions = [
        {"session": s, "color": _session_colors.get(s, "yellow"), "total": len(ids), "checked": len(ids & tried_ids)}
        for s in _session_order
        for ids in [_session_beer_ids.get(s, set())]
    ]
    return web.json_response({
        "total": len(all_ids),
        "checked": len(all_ids & tried_ids),
        "sessions": sessions,
    })


async def handle_festival_meta(request: web.Request) -> web.Response:
    """Static per-session display info - the color assigned to each real
    session key, so the frontend can draw the right dot for a beer's session
    badges (search results, queue) even when the source JSON's session names
    aren't literally "yellow"/"blue"/etc (e.g. "friday"/"saturday"). No
    token needed - this is app config, not personal data - so it's fetched
    once at page load regardless of whether the viewer is connected."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    return web.json_response({
        "sessions": [{"session": s, "color": _session_colors.get(s, "yellow")} for s in _session_order],
    })


async def handle_festival_session(request: web.Request) -> web.Response:
    """Full beer list for one session, personally annotated with had-it -
    the "drill in" view from the stats screen. Optional `query` narrows it
    with the same fuzzy matcher search uses; an empty query returns the
    whole session. Untried (or unknown - had_it_index hasn't reached it
    yet) beers sort first, so what's actually left to try surfaces without
    scrolling past everything already tried."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    session = (body.get("session") or "").strip()
    if session not in _session_order:
        return _json_error("invalid_session")
    query = (body.get("query") or "").strip()

    session_ids = _session_beer_ids.get(session, set())
    candidates = [b for b in _festival_beers if _int_beer_id(b) in session_ids]

    if query:
        keys = [f"{b.get('brewery', '')} {b.get('name', '')}" for b in candidates]
        hits = _fuzzy_match(query, keys, limit=len(candidates))
        candidates = [candidates[idx] for _, _score, idx in hits]

    beers = []
    for b in candidates:
        bid = _int_beer_id(b)
        if bid is None:
            continue
        beers.append({
            "beerId": bid,
            "name": b.get("name"),
            "brewery": b.get("brewery"),
            "style": b.get("style"),
            "sessions": _sessions_for(b.get("id")),
        })

    for beer in beers:
        result = await had_it_index.lookup_had_it(user_id, beer["beerId"])
        if result:
            beer["hadIt"] = result.get("hadIt", False)
            beer["userRating"] = result.get("userRating")

    beers.sort(key=lambda b: 1 if b.get("hadIt") else 0)
    return web.json_response({"beers": beers})


async def handle_badges_get(request: web.Request) -> web.Response:
    """Real Untappd style/country badge progress (badge_stats.py), computed
    entirely from had_it_index's already-synced beer history - no live
    Untappd calls, no extra quota. Sorted so the most actionable badges (done
    first, then closest to the next level) surface at the top of a ~130-entry
    list instead of the reader having to hunt for them."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    beers = await had_it_index.get_all_beers(user_id)
    visited_categories = await venue_index.get_visited_venue_categories(user_id)
    profile = await user_tokens.get_profile(user_id)
    is_supporter = bool((profile or {}).get("is_supporter"))
    rows = (
        badge_stats.compute_progress(beers, is_supporter=is_supporter)
        + badge_stats.compute_venue_progress(visited_categories)
    )
    # personalUrl (untappd.com/user/{username}/badges/{user_badge_id}) is
    # the real per-earned-instance page - only known once badge_index.py has
    # actually seen this exact badge in the owner's check-in history (see
    # its own docstring for why there's no way to look this up on demand).
    # Falls back to the catalog's generic badges.untappd.com page client-side
    # when absent - see app.js's renderBadgeDetail.
    username = (profile or {}).get("username")
    if username:
        earned = await badge_index.get_all(user_id)
        for row in rows:
            user_badge_id = earned.get(row["name"])
            if user_badge_id:
                row["personalUrl"] = f"https://untappd.com/user/{username}/badges/{user_badge_id}"
    # Default order only - the Mini App re-sorts/filters this same fetched
    # list client-side (level ascending, alphabetical, search), so this is
    # just the initial "most actionable first" view, not the only one.
    rows.sort(key=lambda r: (-r["level"], -r["pct"]))
    return web.json_response({"badges": rows})


async def handle_queue_list(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    token = await _resolve_token(tg_user)

    all_items = await checkin_queue.list_items()
    # Per-viewer had-it: reuses the same mechanism as search results, keyed
    # by beerId under "beerId" so _annotate_had_it can mutate in place.
    await _annotate_had_it(all_items, user_id, token)
    if not token:
        for it in all_items:
            it.setdefault("hadIt", None)

    # Once *you* have checked a beer in *through this queue*, or personally
    # dismissed it with "✕", it drops off your own view - other people (or
    # you, on a beer you haven't completed/hidden) still see it. Deliberately
    # NOT based on lifetime hadIt - someone may have had a beer years ago and
    # still want to queue it up and check in again today. `total` (before
    # this filter) is returned separately so the UI can tell "empty for me"
    # apart from "genuinely empty" - hiding the queue button on the former
    # would make an add you just made look like it silently failed.
    items = [
        it for it in all_items
        if user_id not in (it.get("completedBy") or [])
        and user_id not in (it.get("hiddenBy") or [])
    ]
    return web.json_response({"items": items, "total": len(all_items)})


async def handle_queue_add(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    beer_id = body.get("beerId")
    if not isinstance(beer_id, int) or beer_id <= 0:
        return _json_error("invalid_beer_id")

    added_by = {
        "userId": tg_user.get("id"),
        "name": tg_user.get("first_name") or tg_user.get("username") or "?",
    }
    item, added = await checkin_queue.add_item(body, added_by)
    return web.json_response({"ok": True, "item": item, "added": added})


async def handle_queue_remove(request: web.Request) -> web.Response:
    # Despite the route name (kept for API stability), this is a *personal*
    # dismissal, not a delete - see checkin_queue.hide_item's docstring for
    # why "✕ removes it for everyone" was the wrong default.
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    item_id = body.get("id")
    if not item_id:
        return _json_error("invalid_id")

    hidden = await checkin_queue.hide_item(item_id, user_id)
    return web.json_response({"ok": True, "removed": hidden})


async def handle_queue_clear(request: web.Request) -> web.Response:
    """Personal "clear all" - empties *this viewer's* queue in one tap
    (see checkin_queue.hide_all). The shared queue itself is untouched for
    everyone else."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    count = await checkin_queue.hide_all(user_id)
    return web.json_response({"ok": True, "cleared": count})


async def _fetch_all_friends(token: str) -> list[dict]:
    """Pages through get_user_friends up to _AUTOTOAST_FRIENDS_MAX_PAGES,
    returning a flat [{"username","name","avatar"}, ...] list. Stops early
    on a short/empty page (the real end), same "short page = done" signal
    already used by had_it_index/venue_index."""
    friends: list[dict] = []
    offset = 0
    for _ in range(_AUTOTOAST_FRIENDS_MAX_PAGES):
        page = await untappd_mcp.get_user_friends(token, limit=25, offset=offset)
        items = page.get("items", []) if isinstance(page, dict) else []
        if not items:
            break
        for it in items:
            u = it.get("user") or {}
            username = u.get("user_name")
            if not username:
                continue
            name = " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or username
            friends.append({"username": username, "name": name, "avatar": u.get("user_avatar")})
        if len(items) < 25:
            break
        offset += 25
    return friends


async def handle_autotoast_friends(request: web.Request) -> web.Response:
    """Personal friends list for the "🍻 Авто-тост" screen's checkbox UI,
    merged with which ones are currently auto-toast targets. Cached per
    viewer for _AUTOTOAST_FRIENDS_CACHE_TTL - a full fetch can be many
    calls (see _fetch_all_friends), and a friend list rarely changes."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    if not _is_auto_toast_owner(user_id):
        return _json_error("owner_only", 403)
    token = await _resolve_token(tg_user)
    if not token:
        return _json_error("not_connected", 403)

    now = time.time()
    cached = _autotoast_friends_cache.get(user_id)
    if cached and now - cached["fetchedAt"] < _AUTOTOAST_FRIENDS_CACHE_TTL:
        friends = cached["friends"]
    else:
        try:
            friends = await _fetch_all_friends(token)
        except untappd_mcp.UntappdRateLimited:
            return _json_error("rate_limited", 429)
        except untappd_mcp.UntappdMCPError as e:
            logger.warning("autotoast friends fetch failed: %s", e)
            return _json_error("friends_failed", 502)
        _autotoast_friends_cache[user_id] = {"friends": friends, "fetchedAt": now}

    config = await auto_toast.get_config(user_id)
    target_lower = {u.lower() for u in config["targets"]}
    result = [{**f, "enabled": f["username"].lower() in target_lower} for f in friends]

    # A target added earlier (e.g. via /auto_toast add) that isn't a mutual
    # Untappd friend, or just isn't in this (possibly stale) cached page,
    # must still show up checked - otherwise the UI would silently drop
    # them the moment its owner saves the checkbox state back.
    friend_lower = {f["username"].lower() for f in friends}
    for username in config["targets"]:
        if username.lower() not in friend_lower:
            result.append({"username": username, "name": username, "avatar": None, "enabled": True})

    return web.json_response({"friends": result, "enabled": config["enabled"]})


async def handle_autotoast_toggle(request: web.Request) -> web.Response:
    """Global pause/resume for the caller's own auto-toast - e.g. during a
    festival, to keep the token's quota for real check-ins."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    if not _is_auto_toast_owner(user_id):
        return _json_error("owner_only", 403)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    await auto_toast.set_enabled(user_id, bool(body.get("enabled")))
    return web.json_response({"ok": True})


async def handle_autotoast_status(request: web.Request) -> web.Response:
    """Just the on/off flag - unlike /autotoast/friends, this never touches
    Untappd (no get_user_friends call), so the "🔔" quick-settings screen
    can cheaply show all three watch features' state in one screen open.
    Non-owners get a fixed "off, unavailable" shape rather than an error -
    the settings screen renders this alongside two features everyone gets,
    so it degrades quietly instead of showing an error state."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    if not _is_auto_toast_owner(user_id):
        return web.json_response({"enabled": False, "available": False})
    cfg = await auto_toast.get_config(user_id)
    return web.json_response({"enabled": cfg["enabled"], "available": True})


async def handle_comment_watch_get(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    cfg = await comment_watch.get_config(user_id)
    return web.json_response(cfg)


async def handle_comment_watch_toggle(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    await comment_watch.set_enabled(user_id, bool(body.get("enabled")))
    return web.json_response({"ok": True})


async def handle_events_get(request: web.Request) -> web.Response:
    """Recent events (auto-toasted check-ins, new comments, festival
    novelties) for the "🔔" screen - a glance-back convenience, not a full
    history (see event_log.py). Local read only, no Untappd/quota cost."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    events = await event_log.get_events(user_id)
    return web.json_response({"events": events})


async def handle_events_reply(request: web.Request) -> web.Response:
    """Posts a reply straight from the "🔔" screen to a "comment" event's
    check-in - the Mini App equivalent of bot.py's "💬 Відповісти" button
    flow, same "@username, <text>" convention (see comment_watch.py)."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    token = await _resolve_token(tg_user)
    if not token:
        return _json_error("not_connected", 403)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    checkin_id = body.get("checkinId")
    reply_text = (body.get("text") or "").strip()
    username = body.get("username")
    if not checkin_id or not reply_text:
        return _json_error("invalid_reply")
    comment_text = f"@{username}, {reply_text}" if username else reply_text
    try:
        await untappd_mcp.comment_checkin(token, int(checkin_id), comment_text)
    except untappd_mcp.UntappdRateLimited:
        return _json_error("rate_limited", 429)
    except untappd_mcp.UntappdMCPError as e:
        logger.warning("events reply failed: %s", e)
        return _json_error("reply_failed", 502)
    return web.json_response({"ok": True})


async def handle_autotoast_set_targets(request: web.Request) -> web.Response:
    """Full replace of the caller's auto-toast target list, driven by the
    checkbox screen - see auto_toast.set_targets."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    if not _is_auto_toast_owner(user_id):
        return _json_error("owner_only", 403)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    targets = body.get("targets")
    if not isinstance(targets, list):
        return _json_error("invalid_targets")
    await auto_toast.set_targets(user_id, [str(t) for t in targets])
    return web.json_response({"ok": True})


async def handle_autotoast_remove_target(request: web.Request) -> web.Response:
    """Removes a single target - the "✕" quick-action on a "toast" event in
    the "🔔" screen (see event_log.py), for "oh, I didn't mean to keep
    watching them" without having to open the full checkbox screen."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    if not _is_auto_toast_owner(user_id):
        return _json_error("owner_only", 403)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    username = (body.get("username") or "").strip()
    if not username:
        return _json_error("invalid_username")
    removed = await auto_toast.remove_target(user_id, username)
    return web.json_response({"ok": True, "removed": removed})


async def handle_festival_watch_get(request: web.Request) -> web.Response:
    """Current festival-watch config for the "🆕 Новинки" screen - never
    needs an Untappd token, festival_watch.py never calls Untappd itself."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    cfg = await festival_watch.get_config(user_id)
    return web.json_response(cfg)


async def handle_festival_watch_set_location(request: web.Request) -> web.Response:
    """Sets the watch point - from either the Mini App's GPS capture
    (ensureLocationManager, same mechanism "🧭 Локації поруч" already uses)
    or picking a named place from the existing Foursquare venue search."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    lat, lng = body.get("lat"), body.get("lng")
    if lat is None or lng is None:
        return _json_error("invalid_location")
    await festival_watch.set_location(user_id, float(lat), float(lng), body.get("label"))
    return web.json_response({"ok": True})


async def handle_festival_watch_set_radius(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    meters = body.get("radiusMeters")
    if not isinstance(meters, (int, float)) or meters <= 0:
        return _json_error("invalid_radius")
    await festival_watch.set_radius(user_id, int(meters))
    return web.json_response({"ok": True})


async def handle_festival_watch_toggle(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    await festival_watch.set_enabled(user_id, bool(body.get("enabled")))
    return web.json_response({"ok": True})


def _build_app() -> web.Application:
    app = web.Application(middlewares=[_no_cache_middleware])
    app.router.add_get("/checkin", handle_index)
    app.router.add_static("/static/checkin/", path=WEBAPP_DIR, name="checkin_static")
    app.router.add_post("/api/checkin/search", handle_search)
    app.router.add_post("/api/checkin/venues", handle_venues)
    app.router.add_post("/api/checkin/venues/nearby", handle_venues_nearby)
    app.router.add_post("/api/checkin/usage", handle_usage)
    app.router.add_post("/api/checkin/submit", handle_submit)
    app.router.add_post("/api/checkin/festival/stats", handle_festival_stats)
    app.router.add_post("/api/checkin/festival/session", handle_festival_session)
    app.router.add_post("/api/checkin/festival/meta", handle_festival_meta)
    app.router.add_post("/api/checkin/queue/list", handle_queue_list)
    app.router.add_post("/api/checkin/queue/add", handle_queue_add)
    app.router.add_post("/api/checkin/queue/remove", handle_queue_remove)
    app.router.add_post("/api/checkin/queue/clear", handle_queue_clear)
    app.router.add_post("/api/checkin/autotoast/friends", handle_autotoast_friends)
    app.router.add_post("/api/checkin/autotoast/toggle", handle_autotoast_toggle)
    app.router.add_post("/api/checkin/autotoast/set_targets", handle_autotoast_set_targets)
    app.router.add_post("/api/checkin/autotoast/remove_target", handle_autotoast_remove_target)
    app.router.add_post("/api/checkin/festival_watch/get", handle_festival_watch_get)
    app.router.add_post("/api/checkin/festival_watch/set_location", handle_festival_watch_set_location)
    app.router.add_post("/api/checkin/festival_watch/set_radius", handle_festival_watch_set_radius)
    app.router.add_post("/api/checkin/festival_watch/toggle", handle_festival_watch_toggle)
    app.router.add_post("/api/checkin/autotoast/status", handle_autotoast_status)
    app.router.add_post("/api/checkin/comment_watch/get", handle_comment_watch_get)
    app.router.add_post("/api/checkin/comment_watch/toggle", handle_comment_watch_toggle)
    app.router.add_post("/api/checkin/events/get", handle_events_get)
    app.router.add_post("/api/checkin/events/reply", handle_events_reply)
    app.router.add_post("/api/checkin/badges/get", handle_badges_get)
    return app


async def start_webapp_server(
    ptb_app,
    festival_beers: list | None = None,
    data_dir: str | None = None,
    sessions_raw: dict | None = None,
) -> None:
    """Bind the aiohttp app on the port Fly's http_service expects (8080).

    Fire-and-forget from post_init via asyncio.create_task - returns once
    bound, the server keeps serving on the same event loop afterward.
    `ptb_app` is bot.py's python-telegram-bot Application - kept (as
    `_ptb_bot`) so _auto_toast_loop can send festival_watch notifications
    via `_ptb_bot.send_message`; nothing else here needs it. `festival_beers`
    is bot.py's ALL_BEERS (id/name/brewery/style/session) - searched before
    falling back to a live Untappd search. `data_dir` is bot.py's DATA_DIR
    (the persistent Fly volume) for per-user tokens and the shared queue.
    `sessions_raw` is bot.py's SESSIONS_RAW (the undeduped {session: [beers]}
    dict) - used to recover every session a beer appears in, since
    ALL_BEERS itself only keeps the first.
    """
    global _festival_beers, _beer_sessions, _session_beer_ids, _session_order, _session_colors, _ptb_bot, _ptb_app
    _ptb_bot = ptb_app.bot
    _ptb_app = ptb_app
    _festival_beers = festival_beers or []
    _beer_sessions = {}
    _session_beer_ids = {}
    if sessions_raw:
        _session_order = list(sessions_raw.keys())
        _session_colors = _assign_session_colors(_session_order)
        for session, beers in sessions_raw.items():
            ids = _session_beer_ids.setdefault(session, set())
            for beer in beers:
                raw_id = str(beer.get("id"))
                _beer_sessions.setdefault(raw_id, [])
                if session not in _beer_sessions[raw_id]:
                    _beer_sessions[raw_id].append(session)
                bid = _int_beer_id(beer)
                if bid is not None:
                    ids.add(bid)
    else:
        # No session grouping at all in the source JSON (bot.py's load_db()
        # returns an empty sessions_raw for a flat beer list) - every beer
        # gets the same single synthetic session (identified by its assigned
        # color, since there's no real name to preserve), rather than
        # _session_beer_ids staying empty and the whole festival-progress
        # feature silently showing 0/0 for everything.
        session = _assign_session_colors([""])[""]
        _session_order = [session]
        _session_colors = {session: session}
        ids = set()
        for beer in _festival_beers:
            raw_id = str(beer.get("id"))
            _beer_sessions[raw_id] = [session]
            bid = _int_beer_id(beer)
            if bid is not None:
                ids.add(bid)
        _session_beer_ids[session] = ids
    if data_dir:
        user_tokens.init(data_dir)
        checkin_queue.init(data_dir)
        had_it_index.init(data_dir)
        venue_index.init(data_dir)
        auto_toast.init(data_dir)
        festival_watch.init(data_dir)
        comment_watch.init(data_dir)
        event_log.init(data_dir)
        badge_index.init(data_dir)
    port = int(os.environ.get("PORT", 8080))
    aio_app = _build_app()
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Festival check-in Mini App server listening on :%s", port)
    _start_had_it_backfill()
    _start_venue_backfill()
    _start_auto_toast()
    _start_comment_watch()


# How old a get_untappd_api_usage reading has to be before _quota_allows
# stops trusting a low `remaining` number - see that function's docstring.
# Originally hard-coded at 3600 (the full rolling window - "guaranteed
# stale"), which turned out too conservative in practice: observed live
# twice now (once mid-session, once again after the per-token quota change)
# getting stuck for the last several hundred seconds before the 3600s mark,
# with a low-but-already-mostly-expired reading blocking all three
# background loops (had-it/venue/auto-toast) simultaneously right up until
# the exact moment it flips. Lowered to a third of the window - old enough
# that a meaningful chunk of whatever calls produced that low reading have
# already rolled out of Untappd's rolling hour, without reacting to every
# brief lull the way a much shorter threshold would.
QUOTA_STALENESS_SECONDS = float(os.environ.get("QUOTA_STALENESS_SECONDS", "1200"))


def _quota_allows(usage: dict, min_remaining: int) -> bool:
    """Whether a backfill tick should spend quota, given a fresh (free)
    get_untappd_api_usage reading.

    lastSeen.remaining is a passive observation, not a live counter - it
    only updates when *some* real quota-costing call happens on this
    token, by this account. If nothing has spent quota in a while, the
    figure just sits there getting staler, and can badly understate what's
    actually available now (Untappd's limit is a *rolling* hour, so an old
    low reading is progressively more obsolete - every request behind it
    is closer to rolling out of the window). Without accounting for that, a
    background loop that only ever *reads* usage and never itself spends
    quota can deadlock forever: it keeps seeing the same stale low number
    and never makes the one real call that would refresh it. Past
    QUOTA_STALENESS_SECONDS old, trust comes from staleness, not the
    number."""
    last_seen = usage.get("lastSeen") or {}
    remaining = last_seen.get("remaining")
    if remaining is None:
        return True
    age_seconds = last_seen.get("ageSeconds")
    stale = age_seconds is not None and age_seconds >= QUOTA_STALENESS_SECONDS
    return remaining >= min_remaining or stale


def _start_had_it_backfill() -> None:
    global _backfill_task
    if _backfill_task and not _backfill_task.done():
        return
    _backfill_task = asyncio.create_task(_had_it_backfill_loop())


async def _had_it_backfill_loop() -> None:
    """Slowly paginates each connected user's full Untappd check-in history
    into had_it_index.json, a handful of pages at a time, so the had-it
    badge eventually covers a user's entire history - not just what a live
    per-search check happens to ask about. Round-robins fairly across
    multiple connected users (see had_it_index.next_turn) and backs off
    whenever the shared quota is getting tight, so live festival search/
    check-in traffic is never starved by this background job."""
    await asyncio.sleep(5)  # let the server finish binding first
    while True:
        try:
            user_ids = await user_tokens.list_user_ids()
            turn = await had_it_index.next_turn(user_ids, HAD_IT_BACKFILL_RESYNC_COOLDOWN_SECONDS) if user_ids else None
            if turn is None:
                await asyncio.sleep(HAD_IT_BACKFILL_IDLE_SLEEP_SECONDS)
                continue
            user_id, offset = turn
            profile = await user_tokens.get_profile(user_id)
            if not profile or not profile.get("token") or not profile.get("username"):
                await asyncio.sleep(HAD_IT_BACKFILL_INTERVAL_SECONDS)
                continue
            token = profile["token"]

            usage = await untappd_mcp.get_untappd_api_usage(token)  # free, no quota cost
            if not _quota_allows(usage, HAD_IT_BACKFILL_MIN_REMAINING):
                await asyncio.sleep(HAD_IT_BACKFILL_INTERVAL_SECONDS)
                continue

            page = await untappd_mcp.get_user_beers(
                token, profile["username"],
                limit=HAD_IT_BACKFILL_PAGE_SIZE, offset=offset,
            )  # no start/end date - the unfiltered walk verified stable, unlike date-filtering
            items = (page.get("beers") or {}).get("items", [])
            await had_it_index.record_page(
                user_id, profile["username"], items,
                offset + len(items), page.get("total_count", 0),
            )
        except asyncio.CancelledError:
            raise
        except untappd_mcp.UntappdRateLimited:
            pass  # skip to next tick - never auto-retry, per house rule
        except untappd_mcp.UntappdMalformedResponse as e:
            # A specific beer/brewery name the upstream server itself
            # serializes into broken JSON - retrying the identical offset
            # would fail identically forever and wedge this user's backfill
            # permanently. Skip past it (best-effort - up to one page's
            # worth of beers may be missed) rather than get stuck.
            logger.warning("had_it backfill: skipping unparseable page for user %s at offset %s: %s", user_id, offset, e)
            await had_it_index.skip_page(user_id, offset + HAD_IT_BACKFILL_PAGE_SIZE, str(e))
        except untappd_mcp.UntappdMCPError as e:
            logger.warning("had_it backfill tick failed: %s", e)
        except Exception:
            logger.exception("had_it backfill tick failed")
        await asyncio.sleep(HAD_IT_BACKFILL_INTERVAL_SECONDS)


def _start_venue_backfill() -> None:
    global _venue_backfill_task
    if _venue_backfill_task and not _venue_backfill_task.done():
        return
    _venue_backfill_task = asyncio.create_task(_venue_backfill_loop())


async def _venue_backfill_loop() -> None:
    """Slowly paginates each connected user's full Untappd check-in history
    into venue_index.json (checkin_id-based paging via get_user_checkins),
    so "unique venue mode" can confidently tell a brand-new venue from one
    already visited, not just the ~100-250-checkin approximation
    get_my_recent_venues gives. Mirrors _had_it_backfill_loop's pacing/
    resilience shape exactly, as its own independent quota consumer."""
    await asyncio.sleep(5)  # let the server finish binding first
    while True:
        try:
            user_ids = await user_tokens.list_user_ids()
            turn = await venue_index.next_turn(user_ids, VENUE_BACKFILL_RESYNC_COOLDOWN_SECONDS) if user_ids else None
            if turn is None:
                await asyncio.sleep(VENUE_BACKFILL_IDLE_SLEEP_SECONDS)
                continue
            user_id, max_id = turn
            profile = await user_tokens.get_profile(user_id)
            if not profile or not profile.get("token") or not profile.get("username"):
                await asyncio.sleep(VENUE_BACKFILL_INTERVAL_SECONDS)
                continue
            token = profile["token"]

            usage = await untappd_mcp.get_untappd_api_usage(token)  # free, no quota cost
            if not _quota_allows(usage, VENUE_BACKFILL_MIN_REMAINING):
                await asyncio.sleep(VENUE_BACKFILL_INTERVAL_SECONDS)
                continue

            page = await untappd_mcp.get_user_checkins(
                token, profile["username"],
                limit=VENUE_BACKFILL_PAGE_SIZE, max_id=max_id,
            )
            items = (page.get("checkins") or {}).get("items", [])
            next_max_id = (page.get("pagination") or {}).get("max_id")
            await venue_index.record_page(
                user_id, profile["username"], items,
                next_max_id, len(items), VENUE_BACKFILL_PAGE_SIZE,
            )
            # Backfills style/brewery/country for beers had_it_index's own
            # offset-paginated walk keeps missing on active accounts (see
            # had_it_index.enrich_from_checkin's docstring for the confirmed
            # reordering bug) - free, from this same already-paid page, via
            # the checkin_id cursor's reordering-immune walk.
            for item in items:
                beer = item.get("beer") or {}
                bid = beer.get("bid")
                if bid is None:
                    continue
                brewery = item.get("brewery") or {}
                await had_it_index.enrich_from_checkin(
                    user_id, bid, beer.get("beer_style"),
                    brewery.get("brewery_name"), brewery.get("country_name"),
                )
            # Same free-ride principle for badge_index.py: a checkin's own
            # "badges" array only ever appears at the moment that badge was
            # earned, so this walk is the only way to ever discover a given
            # badge's personal user_badge_id (no dedicated "my badges"
            # endpoint exists) - covers the user's ENTIRE history over time,
            # not just badges earned going forward.
            for item in items:
                for badge_item in (item.get("badges") or {}).get("items", []):
                    await badge_index.record(
                        user_id, badge_item.get("badge_name"), badge_item.get("user_badge_id"),
                    )
            # Refreshes user_tokens' cached is_supporter (badge_stats.py's
            # Super Style badges) for free - each check-in's own "user"
            # object already carries the CURRENT subscription status of the
            # account it belongs to (this user's own checkins), so no
            # dedicated get_my_profile call is needed just to keep it fresh.
            if items:
                is_supporter = bool((items[0].get("user") or {}).get("is_supporter"))
                await user_tokens.set_is_supporter(user_id, is_supporter)
        except asyncio.CancelledError:
            raise
        except untappd_mcp.UntappdRateLimited:
            pass  # skip to next tick - never auto-retry, per house rule
        except untappd_mcp.UntappdMCPError as e:
            logger.warning("venue backfill tick failed: %s", e)
        except Exception:
            logger.exception("venue backfill tick failed")
        await asyncio.sleep(VENUE_BACKFILL_INTERVAL_SECONDS)


def _html_link(url: str, text: str) -> str:
    """One escaped <a> tag - `text` is untrusted external content (a beer/
    brewery/venue/user name), `url` is always one we built ourselves from a
    numeric Untappd id, never from external text, so it doesn't need
    escaping itself."""
    return f'<a href="{url}">{html.escape(text)}</a>'


async def _check_festival_novelty(owner_id: int, items: list[dict]) -> None:
    """Sends a Telegram message for any item in `items` that's within the
    owner's saved festival_watch radius AND whose beer isn't on the current
    festival beer list - a tap change or surprise release, the thing the
    static list can't know about. Not about "have I had this" (that's
    auto_toast/had_it_index's question) - deliberately fires regardless of
    whether the owner personally cares about that specific beer, since the
    point is noticing something changed at a specific physical place.

    No Untappd quota cost - runs against the same feed page
    _auto_toast_loop already fetched for auto-toast, so this is naturally
    only checked as often as that loop polls, and only while the owner has
    it enabled with an owner-picked center point (see festival_watch.py).

    Note this only ever sees the owner's own Untappd *friends* -
    get_my_friend_feed is "checkin/recent," which is friends-only by
    Untappd's own definition. Seeing everyone checking in at a place
    (strangers included) would need a venue-specific feed, which isn't
    exposed by this MCP server yet - see README.md."""
    if not items:
        return
    watch = await festival_watch.get_config(owner_id)
    if not watch["enabled"] or watch["lat"] is None or watch["lng"] is None:
        return
    profile = await user_tokens.get_profile(owner_id)
    own_username_lower = (profile or {}).get("username", "").lower()
    known_beer_ids = _all_festival_beer_ids()
    for item in items:
        username = (item.get("user") or {}).get("user_name") or "?"
        if username.lower() == own_username_lower:
            continue  # the feed includes the owner's own check-ins too - not interesting to notify about
        venue = (item.get("venue") or {}).get("location") or {}
        lat, lng = venue.get("lat"), venue.get("lng")
        if lat is None or lng is None:
            continue
        if not festival_watch.is_within(lat, lng, watch["lat"], watch["lng"], watch["radiusMeters"]):
            continue
        bid = (item.get("beer") or {}).get("bid")
        if bid is not None and int(bid) in known_beer_ids:
            continue  # already on our list - not a novelty
        beer_name = (item.get("beer") or {}).get("beer_name") or "?"
        brewery_id = (item.get("brewery") or {}).get("brewery_id")
        brewery_name = (item.get("brewery") or {}).get("brewery_name") or "?"
        venue_id = (item.get("venue") or {}).get("venue_id")
        venue_name = (item.get("venue") or {}).get("venue_name") or watch.get("label") or "локації"

        # HTML with escaping, not plain text: a bare "@username" in a plain
        # Telegram message gets auto-linkified by the client into whatever
        # *Telegram* account happens to have that username - completely
        # unrelated to the real Untappd profile, and confusing/misleading
        # (confirmed live: it pointed at a stranger's Telegram, not
        # Untappd). Every name below links to its own real Untappd page
        # instead (beer/brewery/venue ids are all present on the same
        # already-fetched feed item, no extra lookup); everything is
        # untrusted external content, escaped.
        profile_link = _html_link(f"https://untappd.com/user/{html.escape(username)}", f"@{username}")
        beer_link = _html_link(f"https://untappd.com/beer/{bid}", beer_name) if bid is not None else html.escape(beer_name)
        brewery_link = (
            _html_link(f"https://untappd.com/brewery/{brewery_id}", brewery_name)
            if brewery_id is not None else html.escape(brewery_name)
        )
        venue_link = (
            _html_link(f"https://untappd.com/venue/{venue_id}", venue_name)
            if venue_id is not None else html.escape(venue_name)
        )
        text = (
            f"🆕 {profile_link} щойно зачекінив(-ла) {beer_link} ({brewery_link}) "
            f"на {venue_link} — цього пива нема в базі фестивалю!"
        )
        await event_log.add_event(
            owner_id, "novelty", f"🆕 {username}: {beer_name} на {venue_name}",
            beer_id=bid, checkin_id=item.get("checkin_id"), username=username,
        )
        try:
            await _ptb_bot.send_message(chat_id=owner_id, text=text, parse_mode="HTML")
        except Exception:
            logger.exception("festival_watch: failed to notify owner %s", owner_id)


def _start_auto_toast() -> None:
    global _auto_toast_task
    if _auto_toast_task and not _auto_toast_task.done():
        return
    _auto_toast_task = asyncio.create_task(_auto_toast_loop())


async def _auto_toast_loop() -> None:
    """Round-robins across every owner with auto-toast enabled (bot.py's
    /auto_toast command / the Mini App's checkbox screen), polling their
    *combined* friend feed (get_my_friend_feed - Untappd's checkin/recent,
    added to the MCP server specifically for this) and toasting whatever is
    new from a watched target, unless its venue's country is on that
    owner's exclusion list.

    This replaced an earlier per-target design (one get_user_checkins call
    per watched username - 75+ calls per lap for a heavy list) once
    get_my_friend_feed became available: one call now covers every friend
    at once, filtered down to just the watched subset locally. Still
    paginates backward via max_id rather than relying on minId alone, for
    the same reason as before - a burst bigger than one page (get_my_friend_
    feed's ceiling is 50) must be walked across multiple ticks (see
    catchup_max_id below), not silently truncated.

    The very first poll of a freshly-enabled owner never toasts anything -
    it only records the current newest check-in id as a baseline (see
    auto_toast.peek_owner_turn's docstring) - so turning this on doesn't
    retroactively toast years of everyone's history in one burst."""
    await asyncio.sleep(5)  # let the server finish binding first
    while True:
        try:
            # peek, not a combined next-and-advance: don't consume this
            # owner's rotation slot until we've actually managed to poll
            # them. See auto_toast.peek_owner_turn's docstring.
            turn = await auto_toast.peek_owner_turn()
            if turn is None:
                await asyncio.sleep(AUTO_TOAST_IDLE_SLEEP_SECONDS)
                continue
            profile = await user_tokens.get_profile(turn.owner_id)
            if not profile or not profile.get("token"):
                # A genuinely unusable owner (not a transient condition) -
                # move on so it doesn't block anyone else behind it forever.
                await auto_toast.advance_owner_turn()
                await asyncio.sleep(AUTO_TOAST_INTERVAL_SECONDS)
                continue
            token = profile["token"]

            usage = await untappd_mcp.get_untappd_api_usage(token)  # free, no quota cost
            if not _quota_allows(usage, AUTO_TOAST_MIN_REMAINING):
                # Transient - deliberately do NOT advance. The next loop
                # iteration re-peeks this same owner once quota allows it.
                await asyncio.sleep(AUTO_TOAST_INTERVAL_SECONDS)
                continue

            if turn.last_checkin_id is None:
                # Bootstrap: just note today's newest feed item as the
                # baseline, don't toast anything from before now.
                page = await untappd_mcp.get_my_friend_feed(token, limit=1)
                items = (page.get("checkins") or {}).get("items", [])
                newest_id = (items[0].get("checkin_id") or 0) if items else 0
                await auto_toast.record_feed_tick(turn.owner_id, last_checkin_id=newest_id)
                await auto_toast.advance_owner_turn()
                await asyncio.sleep(AUTO_TOAST_INTERVAL_SECONDS)
                continue

            # catchup_max_id set means we're mid multi-tick walk from a
            # previous tick that hit the 50-item page cap before reaching
            # last_checkin_id; None means start a fresh walk from the very
            # newest feed item. catchup_target_id is "what last_checkin_id
            # should become once this whole walk finishes" - fixed at the
            # newest id seen when the walk *started*, since by the time the
            # walk reaches the old boundary, the page in hand is full of
            # much older ids.
            page = await untappd_mcp.get_my_friend_feed(
                token, limit=50, max_id=turn.catchup_max_id,
            )
            items = (page.get("checkins") or {}).get("items", [])
            catchup_target_id = turn.catchup_target_id
            if catchup_target_id is None and items:
                catchup_target_id = items[0].get("checkin_id") or 0

            config = await auto_toast.get_config(turn.owner_id)
            excluded = config["excludedCountries"]
            legacy_only = config["legacyOnly"]
            watched_lower = {u.lower() for u in config["targets"]}

            # Only items newer than the confirmed boundary are actually new;
            # max_id already bounds the top of this page from above, so
            # this filters the bottom.
            new_items = [it for it in items if (it.get("checkin_id") or 0) > turn.last_checkin_id]

            # festival_watch runs over every new item, not just the
            # auto-toast watch list - it's about a physical place, not
            # specific people. Zero extra quota: same feed page already
            # fetched for auto-toast above.
            await _check_festival_novelty(turn.owner_id, new_items)

            # Auto-toast itself only cares about the watched subset - the
            # feed carries every friend's activity, not only the ones on
            # this owner's auto-toast list.
            relevant = [
                it for it in new_items
                if ((it.get("user") or {}).get("user_name") or "").lower() in watched_lower
            ]

            toasted_by_username: dict[str, int] = {}
            rate_limited = False
            for item in relevant:
                checkin_id = item["checkin_id"]
                username = (item.get("user") or {}).get("user_name") or "?"
                if (item.get("toasts") or {}).get("auth_toast"):
                    continue  # already toasted (e.g. manually, or a prior tick)
                if legacy_only and not auto_toast.is_legacy_style((item.get("beer") or {}).get("beer_style")):
                    continue  # Non-Alcoholic/RTD/Spirit/Wine - not a "real" beer check-in
                venue_country = ((item.get("venue") or {}).get("location") or {}).get("venue_country")
                if auto_toast.is_country_excluded(venue_country, excluded):
                    continue
                try:
                    await untappd_mcp.toast_checkin(token, checkin_id)
                    toasted_by_username[username] = toasted_by_username.get(username, 0) + 1
                    beer_name = (item.get("beer") or {}).get("beer_name") or "?"
                    beer_id = (item.get("beer") or {}).get("bid")
                    await event_log.add_event(
                        turn.owner_id, "toast", f"🍻 {username}: {beer_name}",
                        beer_id=beer_id, checkin_id=checkin_id, username=username,
                    )
                except untappd_mcp.UntappdRateLimited:
                    rate_limited = True
                    break  # abandon the rest of this page, retry it next tick (see below)
                except untappd_mcp.UntappdMCPError as e:
                    # A single permanently-broken check-in would otherwise
                    # wedge this walk forever if we insisted on retrying it -
                    # accept skipping it for good, same trade-off
                    # had_it_index.skip_page makes. Safe here specifically
                    # because we still finish the page (no break).
                    logger.warning("auto_toast: toast_checkin %s failed: %s", checkin_id, e)

            if rate_limited:
                # Leave every cursor exactly where it is - next tick
                # refetches this identical page (same max_id). Safe to
                # repeat: toasting re-checks toasts.auth_toast fresh from
                # the API first, so anything toasted just now is recognized
                # and skipped, never re-toggled off by a retry.
                await auto_toast.record_feed_tick(turn.owner_id, toasted=toasted_by_username, error="rate_limited")
            elif not items or len(items) < 50 or (items[-1].get("checkin_id") or 0) <= turn.last_checkin_id:
                # This page reached the true end of the feed, or walked back
                # down to (or past) the known boundary - the whole catch-up
                # walk (however many ticks it took) is done.
                new_boundary = max(turn.last_checkin_id, catchup_target_id or turn.last_checkin_id)
                await auto_toast.record_feed_tick(
                    turn.owner_id,
                    last_checkin_id=new_boundary, catchup_max_id=None, catchup_target_id=None,
                    toasted=toasted_by_username,
                )
            else:
                # Full 50-item page, still hasn't reached last_checkin_id -
                # more to walk. Continue from the oldest id seen so far.
                await auto_toast.record_feed_tick(
                    turn.owner_id,
                    catchup_max_id=items[-1].get("checkin_id") or 0, catchup_target_id=catchup_target_id,
                    toasted=toasted_by_username,
                )

            # All three branches above genuinely attempted this owner this
            # tick (even the rate-limited one) - move on regardless of
            # outcome. They get their next natural turn later in the
            # rotation - not camped on just because of a rate limit.
            await auto_toast.advance_owner_turn()
        except asyncio.CancelledError:
            raise
        except untappd_mcp.UntappdRateLimited:
            pass  # skip to next tick - never auto-retry, per house rule
        except untappd_mcp.UntappdMCPError as e:
            logger.warning("auto_toast tick failed: %s", e)
        except Exception:
            logger.exception("auto_toast tick failed")
        await asyncio.sleep(AUTO_TOAST_INTERVAL_SECONDS)


async def _notify_new_comment(owner_id: int, checkin_item: dict, comment: dict) -> None:
    beer_name = (checkin_item.get("beer") or {}).get("beer_name") or "?"
    bid = (checkin_item.get("beer") or {}).get("bid")
    checkin_id = checkin_item.get("checkin_id")
    commenter = (comment.get("user") or {}).get("user_name") or "?"
    comment_text = comment.get("comment") or ""
    # HTML with escaping, not plain text: a bare "@username" in a plain
    # Telegram message gets auto-linkified by the client into whatever
    # *Telegram* account happens to have that username - unrelated to the
    # real Untappd profile, confirmed live to point at a stranger's
    # Telegram account. Link deliberately to the real Untappd profile and
    # beer page instead; the comment text is untrusted external content,
    # escaped (not linked - it's free text, not a name).
    profile_link = _html_link(f"https://untappd.com/user/{html.escape(commenter)}", f"@{commenter}")
    beer_link = _html_link(f"https://untappd.com/beer/{bid}", beer_name) if bid is not None else html.escape(beer_name)
    text = (
        f"💬 {profile_link} прокоментував(-ла) твій чекін {beer_link}:\n"
        f"“{html.escape(comment_text)}”"
    )
    # callback_data is capped at 64 bytes by Telegram - a username could
    # easily push "commentreply:<id>:<username>" over that, so the
    # commenter goes in bot_data instead (same f"beer:{bid}" caching
    # convention bot.py already uses elsewhere), keyed by checkin_id.
    if _ptb_app is not None:
        _ptb_app.bot_data[f"comment_reply_to:{checkin_id}"] = commenter
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Відповісти", callback_data=f"commentreply:{checkin_id}")
    ]])
    await event_log.add_event(
        owner_id, "comment", f"💬 {commenter}: {beer_name}",
        beer_id=bid, checkin_id=checkin_id, username=commenter,
    )
    try:
        await _ptb_bot.send_message(chat_id=owner_id, text=text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        logger.exception("comment_watch: failed to notify owner %s", owner_id)


def _start_comment_watch() -> None:
    global _comment_watch_task
    if _comment_watch_task and not _comment_watch_task.done():
        return
    _comment_watch_task = asyncio.create_task(_comment_watch_loop())


async def _comment_watch_loop() -> None:
    """Separately polls each enabled owner's own recent check-ins
    (get_user_checkins on their own username) for new comments - can't
    reuse the shared friend-feed poll (see comment_watch.py's docstring for
    why: a comment usually lands after its check-in has already scrolled
    past that poll's cursor, so it would never be re-examined there). One
    real quota-costing call per enabled owner per tick - modest, since
    there's only ever one "target" per owner (their own check-ins), unlike
    auto-toast's many watched friends."""
    await asyncio.sleep(5)  # let the server finish binding first
    while True:
        try:
            owners = await comment_watch.list_enabled_owners()
            if not owners:
                await asyncio.sleep(COMMENT_WATCH_IDLE_SLEEP_SECONDS)
                continue
            for owner_id in owners:
                try:
                    profile = await user_tokens.get_profile(owner_id)
                    if not profile or not profile.get("token") or not profile.get("username"):
                        continue
                    token = profile["token"]
                    own_username_lower = profile["username"].lower()

                    usage = await untappd_mcp.get_untappd_api_usage(token)  # free, no quota cost
                    if not _quota_allows(usage, COMMENT_WATCH_MIN_REMAINING):
                        continue

                    page = await untappd_mcp.get_user_checkins(
                        token, profile["username"], limit=COMMENT_WATCH_CHECK_LIMIT,
                    )
                    items = (page.get("checkins") or {}).get("items", [])

                    all_comment_ids: list[int] = []
                    comment_by_id: dict[int, tuple[dict, dict]] = {}
                    for item in items:
                        for c in (item.get("comments") or {}).get("items", []):
                            cid = c.get("comment_id")
                            if cid is None:
                                continue
                            commenter = (c.get("user") or {}).get("user_name") or ""
                            if commenter.lower() == own_username_lower:
                                continue  # don't notify about your own comment on your own check-in
                            all_comment_ids.append(cid)
                            comment_by_id[cid] = (item, c)

                    new_ids = await comment_watch.record_tick(owner_id, all_comment_ids)
                    for cid in new_ids:
                        item, c = comment_by_id[cid]
                        await _notify_new_comment(owner_id, item, c)
                except untappd_mcp.UntappdRateLimited:
                    pass  # skip this owner this tick - never auto-retry, per house rule
                except untappd_mcp.UntappdMCPError as e:
                    logger.warning("comment_watch tick failed for owner %s: %s", owner_id, e)
                except Exception:
                    logger.exception("comment_watch tick failed for owner %s", owner_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("comment_watch loop tick failed")
        await asyncio.sleep(COMMENT_WATCH_INTERVAL_SECONDS)
