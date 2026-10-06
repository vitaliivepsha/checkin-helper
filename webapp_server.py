"""aiohttp server for the /checkin Telegram Mini App.

Started via asyncio.create_task from bot.py's post_init, alongside the
existing limited-mode background scheduler - see limited.py's
start_limited_background_tasks for the established pattern this mirrors.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import hmac
import html
import io
import datetime
import json
import logging
import os
import re
import time
from urllib.parse import parse_qsl, quote

import aiohttp
from aiohttp import web
from aiohttp.web_log import AccessLogger
from rapidfuzz import fuzz, process
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

import auto_toast
import badge_index
import badge_stats
import beer_match
import bjcp_styles
import checkin_queue
import comment_watch
import event_log
import festival_map
import festival_mode
import maintenance_mode
import festival_watch
import i18n
import foursquare
import feature_flags
import group_festivals
import group_membership
import user_festivals
import had_it_index
import untappd_direct
import public_map_util
import venue_scrape
import untappd_mcp
import user_tokens
import venue_index
import special_badge_dismissals
import pending_checkins
import wishlist_items
import httpx
import lens_log
import shop_crawl
import wishlist_sheets

logger = logging.getLogger(__name__)


class _DstAwareAccessLogger(AccessLogger):
    """aiohttp's own AccessLogger._format_t hardcodes `time.timezone` (the
    STANDARD/winter UTC offset) as the log line's timezone, regardless of
    whether DST is currently in effect - confirmed live this session: on a
    real September evening (CEST, UTC+2) every access-log line showed
    UTC+1 and a clock an hour behind actual wall-clock time (e.g. real
    22:06 logged as 21:06 +0100). This is a real bug in the aiohttp
    library itself (site-packages/aiohttp/web_log.py), not a misconfigured
    OS/BOT_TIMEZONE here - confirmed live too: Python's own
    datetime.now().astimezone() correctly reports +02:00 on this same
    machine at the same moment. Not worth pinning a 5-minor-version aiohttp
    upgrade (3.9.5 -> 3.14.x, unvetted against this whole project) over a
    cosmetic log timestamp - overriding just this one static method is a
    much smaller, local fix that survives an eventual real upgrade too
    (this override simply becomes a no-op once/if upstream ever fixes it,
    nothing here depends on the bug being present)."""

    @staticmethod
    def _format_t(request, response, time_taken: float) -> str:
        now = datetime.datetime.now().astimezone()  # DST-aware, unlike aiohttp's own version
        start_time = now - datetime.timedelta(seconds=time_taken)
        return start_time.strftime("[%d/%b/%Y:%H:%M:%S %z]")


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
# Real Untappd API v4 access_token - a second, completely independent quota
# pool for the owner only (see untappd_direct.py's own module docstring for
# why this can't be offered to regular connected users). Used by the
# background sync loops via _pick_untappd_backend() below as a fallback when
# the owner's own MCP-token quota is tapped out, not a general replacement.
DIRECT_TOKEN = os.environ.get("UNTAPPD_API_ACCESS_TOKEN", "")

# Per-process cache-busting token for static assets - see handle_index.
_BUILD_VERSION = str(int(time.time()))

# Background lifetime "had-it" backfill pacing - see _had_it_backfill_loop.
# Deliberately conservative: the shared 100/rolling-hour Untappd quota should
# go to live festival search/check-ins first, backfill trickles in the rest.
HAD_IT_BACKFILL_INTERVAL_SECONDS = float(os.environ.get("HAD_IT_BACKFILL_INTERVAL_SECONDS", "30"))
HAD_IT_BACKFILL_IDLE_SLEEP_SECONDS = float(os.environ.get("HAD_IT_BACKFILL_IDLE_SLEEP_SECONDS", "600"))
HAD_IT_BACKFILL_PAGE_SIZE = int(os.environ.get("HAD_IT_BACKFILL_PAGE_SIZE", "50"))
HAD_IT_BACKFILL_MIN_REMAINING = int(os.environ.get("HAD_IT_BACKFILL_MIN_REMAINING", "20"))
# Full walk only once per calendar month (resync_schedule.full_resync_due) - a
# cheap top-N "quick" recheck (below) handles day-to-day catch-up far more
# cheaply; see had_it_index.next_turn's own docstring for the priority order.
HAD_IT_QUICK_RECHECK_COOLDOWN_SECONDS = float(os.environ.get("HAD_IT_QUICK_RECHECK_COOLDOWN_SECONDS", str(24 * 60 * 60)))
HAD_IT_QUICK_RECHECK_LIMIT = int(os.environ.get("HAD_IT_QUICK_RECHECK_LIMIT", "400"))

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
# Full walk only once per calendar month - same reasoning as the had-it one
# above; a cheap daily "quick" recheck of the most recent check-ins (below)
# handles day-to-day catch-up, including keeping badge_index.py's ground-truth
# badge levels fresh.
VENUE_QUICK_RECHECK_COOLDOWN_SECONDS = float(os.environ.get("VENUE_QUICK_RECHECK_COOLDOWN_SECONDS", str(24 * 60 * 60)))
VENUE_QUICK_RECHECK_LIMIT = int(os.environ.get("VENUE_QUICK_RECHECK_LIMIT", "400"))

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

# /api/lens/lookup - a browser userscript's "mark beers I've had on a shop's
# own product page" (see README/session notes). No Telegram init_data is
# possible here (a userscript on a third-party site has no way to produce
# it), so this is gated by a personal long-lived shared-secret token instead
# - the owner pastes it into their own userscript. Always acts as
# AUTO_TOAST_OWNER_ID's connected account, same personal-test-feature
# scoping as /scan and /auto_toast - not a multi-user endpoint.
LENS_API_TOKEN = os.environ.get("LENS_API_TOKEN", "")
LENS_MAX_ITEMS_PER_REQUEST = 60  # generous for one shop category page; still a bound against a runaway/malformed batch
LENS_LOOKUP_CONCURRENCY = 6  # search_beers itself is quota-free, but no need to hammer its Algolia index at once

# A personal Google Sheet (Файл → Поділитися → Опублікувати в інтернеті →
# CSV), read-only, no Google credentials needed - a plain HTTP GET on the
# published CSV URL. Stands in for Untappd's own "Lists" feature, which
# has no API access at all through this app's Untappd MCP connection
# (confirmed live - only the classic single Wishlist is reachable, not
# custom named lists). Expected columns: Назва, Броварня, Посилання
# (an untappd.com beer URL - REQUIRED, the bid is parsed out of it),
# Стиль, ABV - only "Посилання" is actually read; the rest are for the
# user's own reference. A "Статус" column may be added later to
# distinguish rows (e.g. "хочу"/"уникати") - not yet present, so every row
# currently means the same single "on this list" marker.
#
# Each user registers their own sheet via /wishlist_sheet (see
# wishlist_sheets.py) - this env var is ONLY a fallback for
# AUTO_TOAST_OWNER_ID specifically, for whoever set it up before that
# command existed and hasn't re-registered the same URL through it yet.
WISHLIST_SHEET_CSV_URL = os.environ.get("WISHLIST_SHEET_CSV_URL", "")
_WISHLIST_SHEET_CACHE_TTL = 15 * 60  # seconds - same cadence as _WISHLIST_CACHE_TTL
_wishlist_sheet_cache: dict[int, dict] = {}  # keyed by Telegram user_id, like _wishlist_cache
_BEER_URL_ID_RE = re.compile(r"/(\d+)/?$")

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

# Festival-watch venue-checkins loop (see _festival_watch_venue_loop) - its
# OWN independent poll, unlike the old friends-only path which rides
# _auto_toast_loop's feed for free. Always spends DIRECT_TOKEN's own quota
# (the MCP server doesn't expose venue/checkins at all - see README), never
# untappd_mcp's - confirmed live at 100/hour (the per-token limit, not the
# older shared-per-app-key figure some earlier comments/estimates here used).
#
# FESTIVAL_WATCH_VENUE_INTERVAL_SECONDS is a FLOOR, not the actual interval -
# every enabled venue target costs its own call each pass, so a fixed
# interval alone silently overruns the hourly budget as more owners turn
# watch on (confirmed live: 2 concurrent targets at the old flat 90s already
# burned the pool down to 3/100 remaining, well below MIN_REMAINING's 5,
# which then auto-backs the whole loop off for the rest of the rolling hour
# - exactly the "almost no notifications" symptom that gave this away). The
# loop instead computes its own sleep each pass from
# FESTIVAL_WATCH_VENUE_BUDGET_PER_HOUR ÷ target count, and uses whichever of
# that or the floor is LARGER - so a single target still polls at the
# responsive 90s floor, while additional concurrent targets automatically
# spread the SAME total hourly budget across more calls instead of each one
# adding its own 40/hour on top.
FESTIVAL_WATCH_VENUE_INTERVAL_SECONDS = float(os.environ.get("FESTIVAL_WATCH_VENUE_INTERVAL_SECONDS", "90"))
FESTIVAL_WATCH_VENUE_BUDGET_PER_HOUR = float(os.environ.get("FESTIVAL_WATCH_VENUE_BUDGET_PER_HOUR", "70"))
FESTIVAL_WATCH_VENUE_IDLE_SLEEP_SECONDS = float(os.environ.get("FESTIVAL_WATCH_VENUE_IDLE_SLEEP_SECONDS", "300"))
FESTIVAL_WATCH_VENUE_MIN_REMAINING = int(os.environ.get("FESTIVAL_WATCH_VENUE_MIN_REMAINING", "5"))
FESTIVAL_WATCH_VENUE_CHECK_LIMIT = int(os.environ.get("FESTIVAL_WATCH_VENUE_CHECK_LIMIT", "25"))
# Extra venues (festival_watch.add_extra_venue) are scraped, not API-polled -
# see _festival_watch_scrape_loop. Pause between two venues' requests within
# one pass (politeness / not looking like a burst), the pass interval, and
# the cap on the backoff after a Cloudflare block.
FESTIVAL_WATCH_SCRAPE_INTERVAL_SECONDS = float(os.environ.get("FESTIVAL_WATCH_SCRAPE_INTERVAL_SECONDS", "150"))
FESTIVAL_WATCH_SCRAPE_GAP_SECONDS = float(os.environ.get("FESTIVAL_WATCH_SCRAPE_GAP_SECONDS", "3"))
FESTIVAL_WATCH_SCRAPE_MAX_BACKOFF_SECONDS = float(os.environ.get("FESTIVAL_WATCH_SCRAPE_MAX_BACKOFF_SECONDS", "1800"))

_festival_watch_venue_task = None  # module-level, keeps the asyncio.create_task result alive (avoid GC)
_festival_watch_scrape_task = None  # same, for _festival_watch_scrape_loop

# Public, unauthenticated read-only festival map page (/map, /api/public/*) -
# see handle_public_index. Data is all local (no Untappd quota behind any of
# it), so the only protections needed are a short cache and a per-IP limit.
PUBLIC_MAP_RATE_LIMIT_PER_MIN = int(os.environ.get("PUBLIC_MAP_RATE_LIMIT_PER_MIN", "180"))
PUBLIC_MAP_SEARCH_RATE_LIMIT_PER_MIN = int(os.environ.get("PUBLIC_MAP_SEARCH_RATE_LIMIT_PER_MIN", "60"))
_public_cache = public_map_util.TTLCache(ttl_seconds=30)
_public_limiter = public_map_util.RateLimiter()
# Fuzzy search is the one CPU-heavy public endpoint (~10 ms of pure CPU each,
# measured) and this process also runs the Telegram bot on the same event
# loop - so identical queries are cached and the matching itself runs in a
# small worker pool instead of on the loop (rapidfuzz releases the GIL while
# scoring), otherwise a burst of searches stalled EVERYTHING for hundreds of ms.
_public_search_cache = public_map_util.TTLCache(ttl_seconds=60, max_entries=1000)
_public_search_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="public-search")

# Full-badge-list sync loop (see _badge_index_sync_loop) - replaces
# badge_index.py's original "scavenge from whichever check-ins happen to
# carry a badges array" design with GET /v4/user/badges/{username}, a
# complete, always-current, directly-paginated list - confirmed live to
# work for ANY connected user via DIRECT_TOKEN alone (public, same as
# venue/checkins), not just the owner. A cheap full walk (see
# badge_index.next_sync_turn's own docstring), so a much shorter resync
# cooldown than had_it/venue's own 30-day one is fine.
BADGE_INDEX_SYNC_INTERVAL_SECONDS = float(os.environ.get("BADGE_INDEX_SYNC_INTERVAL_SECONDS", "60"))
BADGE_INDEX_SYNC_IDLE_SLEEP_SECONDS = float(os.environ.get("BADGE_INDEX_SYNC_IDLE_SLEEP_SECONDS", "300"))
BADGE_INDEX_SYNC_MIN_REMAINING = int(os.environ.get("BADGE_INDEX_SYNC_MIN_REMAINING", "5"))
BADGE_INDEX_SYNC_PAGE_SIZE = int(os.environ.get("BADGE_INDEX_SYNC_PAGE_SIZE", "50"))
BADGE_INDEX_SYNC_RESYNC_COOLDOWN_SECONDS = float(os.environ.get("BADGE_INDEX_SYNC_RESYNC_COOLDOWN_SECONDS", str(24 * 60 * 60)))

_badge_index_sync_task = None  # module-level, keeps the asyncio.create_task result alive (avoid GC)

# Pulls special_badges.json (and special_badges_pending_review.json, if
# present) from GitHub raw content once a day - the actual catalog upkeep
# (reading untappd.com/blog, which this server's own network calls can't
# reach through Cloudflare - confirmed live, 403 "Just a moment" even via
# plain aiohttp) is done by a separate cloud Claude Code routine that
# commits straight to this repo's master branch; this loop is just the
# local half of that bridge, picking up whatever it committed and telling
# the owner about it. See badge_stats.reload_special_badges for why only
# this one catalog needs a live-reload path.
SPECIAL_BADGES_SYNC_INTERVAL_SECONDS = float(os.environ.get("SPECIAL_BADGES_SYNC_INTERVAL_SECONDS", str(60 * 60)))
SPECIAL_BADGES_RAW_BASE_URL = os.environ.get(
    "SPECIAL_BADGES_RAW_BASE_URL",
    "https://raw.githubusercontent.com/vitaliivepsha/checkin-helper/master",
)

_special_badges_sync_task = None  # module-level, keeps the asyncio.create_task result alive (avoid GC)


@web.middleware
async def _no_cache_middleware(request: web.Request, handler):
    """Telegram's Mini App WebView has been observed to cache aggressively
    regardless of headers, but set this anyway - it's the correct behavior
    for a page whose content changes on every deploy, and some clients do
    honor it.

    Exempts everything under /static/checkin/ (app.js, style.css, festival
    tile images): that mount's own URLs are already version-busted with
    `_BUILD_VERSION` on every file name that can change (see handle_index
    and _festival_image_url) specifically so the CURRENT version is safe to
    cache indefinitely - a new deploy gets a new URL, not a cache-bust
    header. Forcing no-store on top of that defeated the whole point: it
    was confirmed live that opening the Mini App always re-fetched every
    festival tile image from scratch (visible as a brief blank/loading tile
    every single time, never just once) even though the exact same
    `?v=<token>` URL never changes between opens."""
    response = await handler(request)
    if not request.path.startswith("/static/checkin/"):
        response.headers["Cache-Control"] = "no-store"
    elif "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


# Pre-loaded festival beer list (bot.py's ALL_BEERS - id/name/brewery/style/session),
# set once by start_webapp_server. Searched before falling back to live Untappd search.
_festival_beers: list = []

# The python-telegram-bot Bot, set once by start_webapp_server - lets
# _auto_toast_loop send festival_watch notifications directly, without a
# second bot instance or a round-trip back into bot.py.
_ptb_bot = None

# bot.py's own PUBLIC_BASE_URL (the Mini App's public origin) - needed here
# specifically to build a deep-link "Відкрити на карті" button on a festival
# novelty notification (see _notify_festival_novelty), the same web_app
# button pattern bot.py's own /checkin uses. None when PUBLIC_BASE_URL isn't
# configured (e.g. local dev) - that button is just skipped then.
_public_base_url: str | None = None

# The full Application, kept alongside _ptb_bot specifically for
# .bot_data - _notify_new_comment stashes the commenter's username there
# keyed by checkin_id (mirrors bot.py's own f"beer:{bid}" caching
# convention) so the reply flow in bot.py's handle_callback can look it up
# without a second Untappd call. Not used for anything else here - the
# callback_data itself stays short (checkin_id only), since Telegram caps
# it at 64 bytes and a username could easily push that over.
_ptb_app = None

# Set once in start_webapp_server (same value as bot.py's DATA_DIR) - used
# by handle_deploy_webhook to run `git pull` in the right directory and to
# write restart_notify.json via the exact same path bot.py's own data_path()
# would produce.
_data_dir: str | None = None

# bot.py's reload_beer_db function reference, passed in by start_webapp_
# server - see its own docstring for why this indirection (no circular
# import) instead of `import bot`. None in dev_server.py's dev_mode.
_reload_beer_db_fn = None

# Which festivals.json entry is currently loaded - None means the active
# beer-list file doesn't match any registry entry (e.g. FESTIVAL_BEERS_FILE
# was set to something custom, or no switch has ever happened and the env
# default isn't registered either). Set once in start_webapp_server, kept
# in sync by handle_festival_switch on every successful switch.
_active_festival_key: str | None = None

# bot.py's get_festival_data function reference, passed in by
# start_webapp_server - same cross-module-callback reasoning as
# _reload_beer_db_fn (webapp_server.py can't import bot.py back). Resolves
# a festivals.json key (or None) to that festival's (beers, sessions_raw),
# with an in-process cache - see bot.py's own docstring. None in
# dev_server.py's dev_mode, where _festival_data_for falls back to the
# single dataset dev_server.py loaded directly.
_get_festival_data_fn = None

# bot.py's get_toggleable_commands function reference, passed in by
# start_webapp_server - same cross-module-callback reasoning as
# _get_festival_data_fn. Returns bot.py's own TOGGLEABLE_COMMANDS list
# ((command, label, description) tuples) - webapp_server.py has no
# business hardcoding a second copy of that registry. None in
# dev_server.py's dev_mode, where handle_command_flags_get/_set report
# unavailable (there's no real bot.py command menu to control there).
_get_toggleable_commands_fn = None

# bot.py's refresh_command_menus function reference, passed in by
# start_webapp_server - called (with _ptb_app) at the end of a successful
# handle_command_flags_set, so a toggle's effect on the Telegram command
# menu is immediate instead of waiting for the bot's next restart (see
# refresh_command_menus's own docstring for why this exists - the delay
# was confirmed live confusing). None in dev_server.py's dev_mode.
_refresh_command_menus_fn = None

DEPLOY_WEBHOOK_SECRET = os.environ.get("DEPLOY_WEBHOOK_SECRET", "")

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


def _sessions_for(raw_id, beer_sessions: dict | None = None, session_order: list | None = None) -> list[str]:
    """Defaults to the module-global default dataset's derived data when
    called with no override - `beer_sessions`/`session_order` are passed
    explicitly by handlers that resolved a specific group's bound festival
    (see _resolve_festival_key/_festival_data_for/_derive_session_data)."""
    beer_sessions = _beer_sessions if beer_sessions is None else beer_sessions
    session_order = _session_order if session_order is None else session_order
    found = beer_sessions.get(str(raw_id), [])
    return [s for s in session_order if s in found]

# All per-user caches below are keyed by Telegram user id - a shared global
# here would leak one friend's wishlist/had-it/venues into another's view.
_wishlist_cache: dict[int, dict] = {}
_WISHLIST_CACHE_TTL = 15 * 60  # seconds
_WISHLIST_MAX_PAGES = 12  # caps one refresh at 12 calls (600 beers) - see _fetch_all_wishlist

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

# Anti-spam cap for _notify_festival_novelty (see its own docstring) - a
# busy festival tap can get checked in by many different people in quick
# succession, which would otherwise flood the owner's feed with one
# near-identical "🆕 X just checked in Y" message per person. Deliberately
# in-memory, not persisted - a restart naturally clearing the window is
# fine for a soft anti-spam cap, unlike real state.
_FESTIVAL_NOVELTY_NOTIFY_MAX_PER_HOUR = 3
_festival_novelty_notify_counts: dict[tuple[int, int], tuple[int, float]] = {}

# Check-in ids already handled per owner - the friends+radius check and the
# venue-checkins loop can both see the same check-in (a friend at the main
# venue). In-memory only: both sources have persisted cursors, so a restart
# doesn't replay anything. Bounded per owner, oldest evicted first.
_FESTIVAL_NOVELTY_SEEN_MAX = 500
_festival_novelty_seen: dict[int, dict[int, None]] = {}


def _festival_novelty_first_sight(owner_id: int, checkin_id: int | None) -> bool:
    """True the first time this (owner, check-in) is seen, False after - and
    always True when the item carries no checkin_id (nothing to dedup on)."""
    if checkin_id is None:
        return True
    seen = _festival_novelty_seen.setdefault(owner_id, {})
    if checkin_id in seen:
        return False
    seen[checkin_id] = None
    if len(seen) > _FESTIVAL_NOVELTY_SEEN_MAX:
        del seen[next(iter(seen))]
    return True


def _festival_novelty_notify_allowed(owner_id: int, bid: int) -> bool:
    """False once this exact (owner, beer) pair has already been notified
    about _FESTIVAL_NOVELTY_NOTIFY_MAX_PER_HOUR times in the last rolling
    hour - counts every check-in of that beer toward the same cap
    regardless of who checked it in, per the whole point of this limiter.
    Recording a fresh hit is folded into the same call (not a separate
    "record" step) since every caller immediately wants to send on a True
    result - there is no legitimate reason to check without also counting."""
    now = time.time()
    key = (owner_id, bid)
    count, window_start = _festival_novelty_notify_counts.get(key, (0, now))
    if now - window_start >= 3600:
        count, window_start = 0, now
    if count >= _FESTIVAL_NOVELTY_NOTIFY_MAX_PER_HOUR:
        return False
    _festival_novelty_notify_counts[key] = (count + 1, window_start)
    return True


async def _beer_already_queued(owner_id: int, bid: int) -> bool:
    """Whether this beer is already sitting in the owner's active group's
    shared queue (checkin_queue.py) - if so, _notify_festival_novelty skips
    it entirely (see its own docstring): the group already knows about it,
    a novelty ping would just be redundant noise on top of what the queue
    screen already shows. An owner with no active group (or the beer
    genuinely absent from the queue) is NOT an error - just means this
    filter never applies, same as checkin_queue.list_items(None) itself
    already treats "no active group" as "nothing to show"."""
    group = await _active_group(owner_id)
    if not group:
        return False
    items = await checkin_queue.list_items(group["chatId"])
    return any(it.get("beerId") == bid for it in items)


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


async def _active_group(user_id: int) -> dict | None:
    """{"chatId", "chatTitle", "joinedAt"} for whichever Telegram group chat
    the caller last ran /join_group in, or None if they never have - see
    group_membership.py. Scopes the shared queue (checkin_queue.py) so two
    different festivals' crowds never see each other's beers."""
    return await group_membership.get_active_group(user_id)


async def _resolve_festival_key(user_id: int) -> str | None:
    """The effective festivals.json key for this user: their own personal
    override (user_festivals - set via /set_festival in a private chat, or
    the Mini App's "Мій фестиваль" screen) if they set one, else the
    festival their active group has bound via /set_festival (see
    group_festivals.py), else None - caller then falls back to the global
    default via _festival_data_for(None)."""
    personal = await user_festivals.get_user_festival(user_id)
    if personal:
        return personal
    group = await _active_group(user_id)
    if not group:
        return None
    return await group_festivals.get_group_festival(group["chatId"])


def _festival_data_for(key: str | None) -> tuple[list, dict]:
    """(beers, sessions_raw) for `key`, via bot.py's own cache (see
    _get_festival_data_fn) - falls back to the single dataset already
    loaded into _festival_beers/module globals when running under
    dev_server.py's dev_mode, where there's no bot.py process to call
    into."""
    if _get_festival_data_fn is None:
        return _festival_beers, {}
    return _get_festival_data_fn(key)


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
    except untappd_mcp.UntappdRateLimited:
        # DIRECT_TOKEN's own beer/info-based check_i_had_beer (see its
        # docstring) is a fallback ONLY for the recognized owner - its
        # per-viewer fields (auth_rating, stats.user_count) always answer
        # "has DIRECT_TOKEN's own account had this", which is only the
        # right answer when the CALLER is that same account. For every
        # other connected user this would silently show the OWNER's had-it
        # status as their own - never used for anyone else.
        if not (DIRECT_TOKEN and OWNER_TELEGRAM_ID and str(user_id) == OWNER_TELEGRAM_ID):
            return None
        try:
            result = await untappd_direct.check_i_had_beer(DIRECT_TOKEN, beer_id)
        except untappd_mcp.UntappdMCPError as e:
            logger.warning("check_i_had_beer(%s) direct-API fallback failed: %s", beer_id, e)
            return None
    except untappd_mcp.UntappdMCPError as e:
        logger.warning("check_i_had_beer(%s) failed: %s", beer_id, e)
        return None
    user_cache[beer_id] = (result, time.time(), bool(result.get("hadIt")))
    return result


async def _annotate_had_it(beers: list[dict], user_id: int, token: str | None, limit: int = 5) -> None:
    """Mutates EVERY beer dict in-place with hadIt/userRating - `limit` caps
    only the number of LIVE (quota-costing) check_i_had_beer calls, not the
    total beer count. had_it_index.lookup_had_it is a free, in-memory,
    already-synced answer for any beer the backfill has already reached
    (see had_it_index.py's own module docstring) - there's no reason to
    hide that free information from a result ranked 6th+ just because a
    single shared cap used to apply to every beer indiscriminately.
    Confirmed live: a genuinely already-had beer ranked past the old flat
    5-beer cutoff showed no had-it badge in search results (while its
    queue-status badge, which was never capped, showed fine) - looking
    "not had" in search and "had" in the queue screen for the exact same
    beer. Calls sequentially, not concurrently - a burst of parallel
    check_i_had_beer calls has been observed to trip Untappd's own rate
    limiting. No-ops without a token (viewer hasn't connected their own
    Untappd account yet)."""
    if not token:
        return
    live_calls_made = 0
    for beer in beers:
        bid = beer.get("beerId")
        if not bid:
            continue
        indexed = await had_it_index.lookup_had_it(user_id, bid)
        if indexed is not None:
            beer["hadIt"] = indexed.get("hadIt", False)
            beer["userRating"] = indexed.get("userRating")
            continue
        if live_calls_made >= limit:
            continue
        live_calls_made += 1
        result = await _get_had_it(user_id, bid, token)
        if result:
            beer["hadIt"] = result.get("hadIt", False)
            beer["userRating"] = result.get("userRating")


async def _annotate_my_list(beers: list[dict], user_id: int) -> None:
    """Mutates each beer dict in-place with wishlistItemId - the native
    wishlist_items.py entry's id, if this beer already has one - so the
    search screen's "Мій список" button can toggle add/remove instead of
    only ever adding (a second tap would otherwise just be a same-bid
    dedup no-op, per wishlist_items.add_item). A local dict lookup, not an
    Untappd call, so unlike _annotate_had_it's quota-conserving cap, every
    result gets annotated."""
    native_items = await wishlist_items.list_items(user_id)
    by_bid = {it.get("beerId"): it.get("id") for it in native_items if it.get("beerId") is not None}
    for beer in beers:
        beer["wishlistItemId"] = by_bid.get(beer.get("beerId"))


async def _annotate_queue_status(beers: list[dict], user_id: int, group_id: int | None) -> None:
    """Mutates each beer dict in-place with queueStatus - the small corner
    badge on every beer row (search/session/brewery/wishlist) that answers
    "have I already queued this?" before tapping "+" again:
    - "active": a shared checkin_queue item exists for this beerId *and*
      is currently visible in this user's own queue view.
    - "was_in_queue": an item exists but this user has since hidden or
      completed it (see checkin_queue's module docstring - neither is a
      delete, the item just drops out of their personal view).
    Beers nobody has ever queued get no field at all - including ones this
    user has wiped via checkin_queue.reset_user's "forget my test
    check-ins" (testForgottenBy): that button exists specifically to erase
    the impression a beer was queued during the festival, so the badge
    must stop claiming "was in queue" for it too, not just the completed-
    at-the-festival text elsewhere. A local dict lookup against the shared
    queue, not an Untappd call, so - unlike _annotate_had_it's quota-
    conserving cap - every result gets annotated.

    group_id scopes which group's queue is consulted - None (caller has no
    active group) means checkin_queue.list_items returns nothing, so every
    badge quietly disappears rather than leaking another group's items."""
    items = await checkin_queue.list_items(group_id)
    by_beer_id = {it.get("beerId"): it for it in items if it.get("beerId") is not None}
    for beer in beers:
        item = by_beer_id.get(beer.get("beerId"))
        if not item:
            continue
        if user_id in (item.get("testForgottenBy") or []):
            continue
        hidden = item.get("hiddenBy") or []
        completed = item.get("completedBy") or []
        beer["queueStatus"] = "was_in_queue" if (user_id in hidden or user_id in completed) else "active"


def _fuzzy_match(query: str, keys: list[str], limit: int, score_cutoff: int = 60):
    """rapidfuzz process.extract with case-insensitive matching, with one
    correction on top: a key that literally CONTAINS `query` as a
    substring is always ranked ahead of one that doesn't, regardless of
    fuzzy score - proven live necessary: WRatio blends several
    sub-scorers and can score a short query against a completely
    unrelated key (e.g. "garag" against "PINTA Bawarka", scoring 60) at
    or above a key that's an obvious literal substring match ("Garage
    Project ... ", also 60) - a tie an unrelated result has no business
    winning. Substring hits keep their own fuzzy-score order among
    themselves (a tighter substring match still outranks a looser one),
    non-substring hits keep their normal fuzzy order after all of them."""
    if not keys:
        return []
    hits = process.extract(
        query, keys, scorer=fuzz.WRatio, limit=limit,
        score_cutoff=score_cutoff, processor=lambda s: s.lower(),
    )
    query_lower = query.lower()
    return sorted(hits, key=lambda h: (query_lower not in h[0].lower(), -h[1]))


def _int_beer_id(b: dict) -> int | None:
    try:
        return int(b.get("id"))
    except (TypeError, ValueError):
        return None


def _search_festival_beers(
    query: str, beers: list | None = None, beer_sessions: dict | None = None,
    session_order: list | None = None, limit: int = 10,
) -> list[dict]:
    """Defaults to the module-global default dataset when called with no
    override - see _sessions_for's own note."""
    beers = _festival_beers if beers is None else beers
    if not beers:
        return []
    keys = [f"{b.get('brewery', '')} {b.get('name', '')}" for b in beers]
    hits = _fuzzy_match(query, keys, limit)
    results = []
    for _, _score, idx in hits:
        b = beers[idx]
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
            "sessions": _sessions_for(b.get("id"), beer_sessions, session_order),
            "source": "festival",
        })
    return results


_ZONE_NAME_RE = re.compile(r"^Area (\d+)$")


def _festival_editable_zone_names(beers: list | None = None) -> list[str]:
    """Every distinct "Area N" location present in the given festival's beer
    data (defaults to the module-global default dataset when called with no
    override - see _sessions_for's own note), sorted numerically (not
    alphabetically - "Area 10" must sort after "Area 2", not before it).
    However many of these exist (MBCC has 4; a different festival might
    have just one, or a dozen) are the main, user-editable zones on the
    festival map - see festival_map.py's own docstring for why it doesn't
    hardcode this itself."""
    beers = _festival_beers if beers is None else beers
    names = {
        (b.get("location") or "").strip()
        for b in beers
        if _ZONE_NAME_RE.match((b.get("location") or "").strip())
    }
    return sorted(names, key=lambda n: int(_ZONE_NAME_RE.match(n).group(1)))


def _stand_brewery(b: dict) -> str:
    """Which brewery's STAND (physical presence on the map) a beer counts
    toward - almost always just its own `brewery`, except a collab beer
    whose OTHER named brewery has no stand of its own (e.g. WFP's
    "Beskidy", credited to Verdant Brewing Co on Untappd, but actually
    poured at PINTA's stand - Verdant isn't physically at the festival at
    all). `standBrewery` is an optional per-beer override in the source
    JSON for exactly that case - see also _festival_brewery_aliases,
    which is how a map search for the CREDITED brewery still finds the
    right stand."""
    return (b.get("standBrewery") or b.get("brewery") or "").strip()


def _festival_brewery_zone_map(beers: list | None = None) -> dict[str, str]:
    """{brewery: "Area N"} for every festival brewery whose location is one
    of the editable zones - location is already forward-filled to brewery
    level by bot.py's load_db(), so every beer of a brewery agrees, and the
    first one seen is enough. Keyed by each beer's STAND brewery (see
    _stand_brewery), not necessarily its own credited `brewery` - a
    collab beer with no stand of its own never gets counted as one."""
    beers = _festival_beers if beers is None else beers
    zone_names = set(_festival_editable_zone_names(beers))
    zones: dict[str, str] = {}
    for b in beers:
        brewery = _stand_brewery(b)
        location = (b.get("location") or "").strip()
        if brewery and location in zone_names and brewery not in zones:
            zones[brewery] = location
    return zones


def _festival_bonus_categories(beers: list | None = None) -> dict[str, list[str]]:
    """{category_name: [breweries]} for every festival location that ISN'T
    one of the "Area N" editable zones - shown read-only, at the end of the
    map (MBCC's "Lagerland" is just one example of this, not a special
    case - a different festival's own bonus category is picked up the same
    way, by name, with no code change needed). Same STAND-brewery keying
    as _festival_brewery_zone_map."""
    beers = _festival_beers if beers is None else beers
    editable = set(_festival_editable_zone_names(beers))
    by_category: dict[str, set[str]] = {}
    for b in beers:
        brewery = _stand_brewery(b)
        location = (b.get("location") or "").strip()
        if brewery and location and location not in editable:
            by_category.setdefault(location, set()).add(brewery)
    return {name: sorted(breweries) for name, breweries in sorted(by_category.items())}


def _festival_brewery_aliases(beers: list | None = None) -> dict[str, str]:
    """{credited_brewery: stand_brewery} for every beer whose own `brewery`
    (how it's credited/searchable, e.g. in the beer list and search
    results) differs from its STAND brewery (see _stand_brewery) - lets
    the Mini App's map search still find a collab beer's OTHER named
    brewery (no stand of its own) and jump straight to/highlight whichever
    stand it's actually poured at, instead of coming up empty or a
    phantom "stand" that was never really on the map."""
    beers = _festival_beers if beers is None else beers
    aliases: dict[str, str] = {}
    for b in beers:
        credited = (b.get("brewery") or "").strip()
        stand = _stand_brewery(b)
        if credited and stand and credited != stand:
            aliases[credited] = stand
    return aliases


async def _fetch_all_wishlist(token: str) -> list[dict]:
    """Pages through get_my_wishlist up to _WISHLIST_MAX_PAGES (same
    "short page = done" pattern as _fetch_all_friends) - proven live
    necessary: a single 50-item page silently missed a real wishlist beer
    that turned out to be past position 50, showing no wishlist marker for
    it at all despite genuinely being on the list."""
    beers: list[dict] = []
    offset = 0
    for _ in range(_WISHLIST_MAX_PAGES):
        page = await untappd_mcp.get_my_wishlist(token, limit=50, offset=offset)
        if not page:
            break
        beers.extend(page)
        if len(page) < 50:
            break
        offset += 50
    return beers


async def _get_wishlist_beers(user_id: int, token: str) -> list[dict]:
    """Shared cache (see _wishlist_cache/_WISHLIST_CACHE_TTL) behind both
    the checkin webapp's own wishlist-priority search and the lens
    endpoint's "already on my wishlist" marker - one full get_my_wishlist
    walk (see _fetch_all_wishlist) serves both for up to 15 minutes, not
    one per beer."""
    cache = _wishlist_cache.get(user_id)
    now = time.time()
    if cache is None or now - cache["fetched_at"] > _WISHLIST_CACHE_TTL:
        try:
            cache = {"data": await _fetch_all_wishlist(token), "fetched_at": now}
        except untappd_mcp.UntappdMCPError as e:
            logger.warning("get_my_wishlist failed: %s", e)
            cache = {"data": (cache or {}).get("data") or [], "fetched_at": now}
        _wishlist_cache[user_id] = cache
    return cache["data"] or []


async def _fetch_wishlist_sheet_rows(csv_url: str) -> list[dict]:
    """Fetches and parses a published Google Sheet CSV URL (see
    WISHLIST_SHEET_CSV_URL's own comment) - a plain GET, no Google auth.
    "Посилання" (parsed into a bid) is required; "Назва"/"Броварня"/"Стиль"/
    "ABV" are carried along for display in the webapp's merged wishlist tab
    but otherwise unused. Rows with no parseable beer link are skipped
    rather than failing the whole fetch, since this is someone's
    manually-maintained spreadsheet, not a validated data source."""
    if not csv_url:
        return []
    async with aiohttp.ClientSession() as session:
        async with session.get(csv_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            text = await resp.text()
    rows: list[dict] = []
    for row in csv.DictReader(io.StringIO(text)):
        url = (row.get("Посилання") or "").strip()
        m = _BEER_URL_ID_RE.search(url)
        if not m:
            continue
        rows.append({
            "bid": int(m.group(1)),
            "name": (row.get("Назва") or "").strip() or None,
            "brewery": (row.get("Броварня") or "").strip() or None,
            "style": (row.get("Стиль") or "").strip() or None,
            "abv": (row.get("ABV") or "").strip() or None,
        })
    return rows


async def _get_wishlist_sheet_rows(user_id: int) -> list[dict]:
    """Cached per-user (see _WISHLIST_SHEET_CACHE_TTL), same pattern as
    _wishlist_cache - each user's own registered sheet (see
    wishlist_sheets.py, set via /wishlist_sheet), falling back to
    WISHLIST_SHEET_CSV_URL only for AUTO_TOAST_OWNER_ID and only if they
    haven't registered their own yet (see that env var's own comment)."""
    cache = _wishlist_sheet_cache.get(user_id)
    now = time.time()
    if cache is None or now - cache["fetched_at"] > _WISHLIST_SHEET_CACHE_TTL:
        csv_url = await wishlist_sheets.get_csv_url(user_id)
        if not csv_url and str(user_id) == AUTO_TOAST_OWNER_ID:
            csv_url = WISHLIST_SHEET_CSV_URL
        try:
            rows = await _fetch_wishlist_sheet_rows(csv_url)
            cache = {"rows": rows, "fetched_at": now}
        except (aiohttp.ClientError, asyncio.TimeoutError, csv.Error) as e:
            logger.warning("wishlist sheet fetch failed for user %s: %s", user_id, e)
            cache = {"rows": (cache or {}).get("rows") or [], "fetched_at": now}
        _wishlist_sheet_cache[user_id] = cache
    return cache["rows"]


async def _search_wishlist(query: str, user_id: int, limit: int = 10) -> list[dict]:
    """Powers the search screen's "Вішліст" priority checkbox - the user's
    own list (native items + Google Sheet rows, see _get_my_list_items),
    not Untappd's classic Wishlist (that one still backs the lens's 🔖
    marker via _get_wishlist_beers, untouched here). No `token` needed:
    this list lives entirely in our own storage."""
    beers = await _get_my_list_items(user_id)
    if not beers:
        return []
    keys = [f"{b.get('brewery') or ''} {b.get('name') or ''}" for b in beers]
    hits = _fuzzy_match(query, keys, limit)
    return [
        {
            "beerId": (b := beers[idx]).get("beerId"),
            "name": b.get("name"),
            "brewery": b.get("brewery"),
            "style": b.get("style"),
            "abv": b.get("abv"), "ibu": None,
            "rating": None, "ratingCount": None,
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


def _public_client_ip(request: web.Request) -> str:
    # Last hop, not first: behind ngrok -> gateway the closest proxy's own
    # entry is the one that can't have been supplied by the client.
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
    return forwarded or request.remote or "?"


def _public_rate_limited(request: web.Request, *, search: bool = False) -> web.Response | None:
    ip = _public_client_ip(request)
    if not _public_limiter.allow(f"all:{ip}", PUBLIC_MAP_RATE_LIMIT_PER_MIN) or (
        search and not _public_limiter.allow(f"search:{ip}", PUBLIC_MAP_SEARCH_RATE_LIMIT_PER_MIN)
    ):
        return _json_error("rate_limited", 429)
    return None


async def _public_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _public_festival_key(body: dict) -> str | None:
    """`fest` must be a real festivals.json key, otherwise the default
    festival - never trust a free-form value into a dataset lookup."""
    fest = body.get("fest")
    if isinstance(fest, str) and any(f.get("key") == fest for f in _load_festivals_registry()):
        return fest
    return None


async def handle_public_index(request: web.Request) -> web.Response:
    """The festival map as a public, read-only web page (no Telegram, no
    login): the same index.html/app.js, flagged with window.PUBLIC_MAP so
    app.js shows only the map + brewery lists + a festival-database search
    (with Untappd links), talks to /api/public/* instead of the
    initData-authenticated endpoints, and hides everything personal (queue,
    check-ins, wishlist, settings). Optional ?fest=<festivals.json key>,
    ?mapZone=&mapBrewery= deep link."""
    if _public_rate_limited(request):
        return web.Response(status=429, text="Too many requests")
    with open(os.path.join(WEBAPP_DIR, "index.html"), encoding="utf-8") as f:
        page = f.read()
    page = page.replace("/static/checkin/app.js", f"/static/checkin/app.js?v={_BUILD_VERSION}")
    page = page.replace("/static/checkin/style.css", f"/static/checkin/style.css?v={_BUILD_VERSION}")
    page = page.replace("<body", '<body class="public-map"', 1)
    page = page.replace("</head>", '<meta name="robots" content="noindex">\n<script>window.PUBLIC_MAP = true;</script>\n</head>', 1)
    return web.Response(text=page, content_type="text/html")


async def handle_public_i18n(request: web.Request) -> web.Response:
    if (limited := _public_rate_limited(request)):
        return limited
    body = await _public_body(request)
    code = body.get("lang") if isinstance(body.get("lang"), str) else request.headers.get("Accept-Language", "en")
    return web.json_response({"lang": (code or "en")[:2].lower(), "strings": i18n.app_strings(code)})


async def handle_public_meta(request: web.Request) -> web.Response:
    if (limited := _public_rate_limited(request)):
        return limited
    return web.json_response({
        "sessions": [{"session": s, "color": _session_colors.get(s, "yellow")} for s in _session_order],
    })


async def handle_public_map_get(request: web.Request) -> web.Response:
    if (limited := _public_rate_limited(request)):
        return limited
    festival_key = _public_festival_key(await _public_body(request))
    payload = _public_cache.get(("map", festival_key))
    if payload is None:
        payload = await _festival_map_payload(festival_key)
        _public_cache.set(("map", festival_key), payload)
    return web.json_response(payload)


async def handle_public_brewery(request: web.Request) -> web.Response:
    if (limited := _public_rate_limited(request)):
        return limited
    body = await _public_body(request)
    brewery = (body.get("brewery") or "").strip() if isinstance(body.get("brewery"), str) else ""
    if not brewery:
        return _json_error("invalid_brewery")
    festival_key = _public_festival_key(body)
    cache_key = ("brewery", festival_key, brewery)
    beers = _public_cache.get(cache_key)
    if beers is None:
        beers = _stand_beers(festival_key, brewery)
        _public_cache.set(cache_key, beers)
    return web.json_response({"brewery": brewery, "beers": beers})


def _public_search_index(festival_key: str | None) -> tuple:
    """Per-festival lookup tables the search needs, built once per cache
    window instead of per query."""
    festival_beers, sessions_raw = _festival_data_for(festival_key)
    beer_sessions, _, session_order, _ = _derive_session_data(festival_beers, sessions_raw)
    by_id = {_int_beer_id(b): b for b in festival_beers}
    return festival_beers, beer_sessions, session_order, by_id, _festival_brewery_zone_map(festival_beers)


def _public_search_compute(index: tuple, query: str) -> list[dict]:
    """Pure CPU work over immutable-by-convention data - safe to run in the
    search worker pool (nothing here touches shared mutable state)."""
    festival_beers, beer_sessions, session_order, by_id, zone_hint = index
    hits = _search_festival_beers(query, festival_beers, beer_sessions, session_order, limit=25)
    results = []
    for hit in hits:
        raw = by_id.get(hit["beerId"]) or {}
        stand = _stand_brewery(raw) or hit["brewery"]
        results.append({
            "beerId": hit["beerId"], "name": hit["name"], "brewery": hit["brewery"],
            "style": hit["style"], "stand": stand, "zone": zone_hint.get(stand),
        })
    return results


async def handle_public_search(request: web.Request) -> web.Response:
    """Search over the loaded festival beer list only (local fuzzy match,
    zero Untappd quota) - each hit carries the stand/zone it's poured at so
    the page can jump to it on the map, plus the beerId for the Untappd
    link."""
    if (limited := _public_rate_limited(request, search=True)):
        return limited
    body = await _public_body(request)
    query = (body.get("query") or "").strip() if isinstance(body.get("query"), str) else ""
    if len(query) < 2 or len(query) > 80:
        return web.json_response({"results": []})
    festival_key = _public_festival_key(body)
    normalized = " ".join(query.lower().split())
    cache_key = (festival_key, normalized)
    results = _public_search_cache.get(cache_key)
    if results is None:
        index = _public_cache.get(("search_index", festival_key))
        if index is None:
            index = _public_search_index(festival_key)
            _public_cache.set(("search_index", festival_key), index)
        results = await asyncio.get_running_loop().run_in_executor(
            _public_search_pool, _public_search_compute, index, normalized,
        )
        _public_search_cache.set(cache_key, results)
    return web.json_response({"results": results})


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
        festival_key = await _resolve_festival_key(user_id)
        beers, sessions_raw = _festival_data_for(festival_key)
        beer_sessions, _, session_order, _ = _derive_session_data(beers, sessions_raw)
        _extend(_search_festival_beers(query, beers, beer_sessions, session_order))
    if wishlist_priority:
        _extend(await _search_wishlist(query, user_id))

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
    await _annotate_my_list(beers, user_id)
    group = await _active_group(user_id)
    await _annotate_queue_status(beers, user_id, group["chatId"] if group else None)
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
    is_owner = str(tg_user.get("id")) == AUTO_TOAST_OWNER_ID
    # The direct Untappd API (untappd_direct.py) is a completely separate
    # credential/quota pool from the MCP one above - only the recognized
    # owner's background sync loops ever use it (see that module's own
    # docstring), so surfacing it to anyone else would just be a confusing
    # "0/0" for a pool they never touch. get_api_usage() is free (reads
    # module state captured from the real API's own response headers, no
    # network call), so this costs nothing even when there's nothing to show.
    direct_usage = None
    if is_owner:
        direct_last_seen = untappd_direct.get_api_usage().get("lastSeen", {})
        if direct_last_seen.get("remaining") is not None:
            direct_usage = {"limit": direct_last_seen.get("limit"), "remaining": direct_last_seen.get("remaining")}
    return web.json_response({
        "limit": last_seen.get("limit"),
        "remaining": last_seen.get("remaining"),
        "callsLastHour": instance.get("callsLastHour"),
        "lastVenue": (profile or {}).get("last_venue"),
        "isAutoToastOwner": is_owner,
        "directUsage": direct_usage,
    })


async def _apply_checkin_side_effects(
    user_id: int, beer_id: int, rating: float, foursquare_id: str | None, queue_item_id: str | None,
) -> None:
    """Everything that follows a REAL, successful check-in besides the
    Untappd call itself - shared by handle_submit's own success path and
    handle_pending_retry's (a retried pending item gets exactly the same
    bookkeeping a first-try success would have)."""
    # Reflect the fresh check-in immediately, without another quota-costing call.
    _had_it_cache.setdefault(user_id, {})[beer_id] = ({"hadIt": True, "userRating": rating}, time.time(), True)
    await had_it_index.record_checkin(user_id, beer_id, rating)
    await venue_index.record_checkin(user_id, foursquare_id)
    if queue_item_id:
        await checkin_queue.mark_completed(queue_item_id, user_id)


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

    # display-only fields, sent by the client alongside the ones above
    # purely so a failed attempt's pending record (below) can render a
    # normal-looking row without an extra quota-costing beer lookup -
    # never used on the success path.
    pending_fields = {
        "beerId": beer_id, "rating": rating, "shout": shout,
        "foursquareId": foursquare_id, "geolat": geolat, "geolng": geolng,
        "venueName": venue_name, "queueItemId": queue_item_id,
        "beerName": body.get("beerName"), "brewery": body.get("brewery"),
        "style": body.get("style"), "abv": body.get("abv"), "labelUrl": body.get("labelUrl"),
    }

    try:
        checkin = await untappd_mcp.check_in(
            token, beer_id=beer_id, rating=rating, shout=shout,
            foursquare_id=foursquare_id, geolat=geolat, geolng=geolng,
        )
    except untappd_mcp.UntappdRateLimited:
        # Saved instead of just failing, so the attempt (rating/comment/
        # venue included) isn't lost - see pending_checkins.py and the
        # Mini App's "Відкладені чекіни" screen for the retry side of this.
        item = await pending_checkins.add_item(user_id, {**pending_fields, "failReason": "rate_limited"})
        return web.json_response({"ok": True, "pending": True, "item": item})
    except untappd_mcp.UntappdMCPError as e:
        logger.warning("check_in failed: %s", e)
        item = await pending_checkins.add_item(user_id, {**pending_fields, "failReason": "checkin_failed"})
        return web.json_response({"ok": True, "pending": True, "item": item})

    await _apply_checkin_side_effects(user_id, beer_id, rating, foursquare_id, queue_item_id)
    return web.json_response({"ok": True, "checkin": checkin})


async def handle_pending_list(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    return web.json_response({"items": await pending_checkins.list_items(user_id)})


async def handle_pending_retry(request: web.Request) -> web.Response:
    """Re-attempts a saved pending check-in with exactly the rating/comment/
    venue it originally captured - same two-exception handling as
    handle_submit's own real-check-in branch, just scoped to one item: a
    second failure leaves it in the list for another try later instead of
    losing it."""
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

    item_id = body.get("id")
    if not item_id:
        return _json_error("invalid_id")
    item = await pending_checkins.get_item(user_id, item_id)
    if not item:
        return _json_error("not_found", 404)

    try:
        checkin = await untappd_mcp.check_in(
            token, beer_id=item["beerId"], rating=item.get("rating") or 0, shout=item.get("shout") or "",
            foursquare_id=item.get("foursquareId"), geolat=item.get("geolat"), geolng=item.get("geolng"),
        )
    except untappd_mcp.UntappdRateLimited:
        return _json_error("rate_limited", 429)
    except untappd_mcp.UntappdMCPError as e:
        logger.warning("pending check-in retry failed: %s", e)
        return _json_error("checkin_failed", 502)

    await _apply_checkin_side_effects(
        user_id, item["beerId"], item.get("rating") or 0, item.get("foursquareId"), item.get("queueItemId"),
    )
    await pending_checkins.remove_item(user_id, item_id)
    return web.json_response({"ok": True, "checkin": checkin})


async def handle_pending_remove(request: web.Request) -> web.Response:
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

    removed = await pending_checkins.remove_item(user_id, item_id)
    return web.json_response({"ok": True, "removed": removed})


def _all_festival_beer_ids(session_beer_ids: dict | None = None) -> set:
    all_ids: set = set()
    for ids in (_session_beer_ids if session_beer_ids is None else session_beer_ids).values():
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

    festival_key = await _resolve_festival_key(user_id)
    beers, sessions_raw = _festival_data_for(festival_key)
    _, session_beer_ids, session_order, session_colors = _derive_session_data(beers, sessions_raw)

    all_ids = _all_festival_beer_ids(session_beer_ids)

    tried_ids: set = set()
    for bid in all_ids:
        result = await had_it_index.lookup_had_it(user_id, bid)
        if result and result.get("hadIt"):
            tried_ids.add(bid)

    sessions = [
        {"session": s, "color": session_colors.get(s, "yellow"), "total": len(ids), "checked": len(ids & tried_ids)}
        for s in session_order
        for ids in [session_beer_ids.get(s, set())]
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


def _stand_beers(festival_key: str | None, brewery: str) -> list[dict]:
    festival_beers, sessions_raw = _festival_data_for(festival_key)
    beer_sessions, _, session_order, _ = _derive_session_data(festival_beers, sessions_raw)

    # Match by STAND, not the beer's own credited brewery - a map pill is
    # always a stand (see _stand_brewery's own docstring), and a pure stand
    # name like a collab host that holds no beers credited to itself
    # directly (e.g. WFP's "OneMoreBeer") would otherwise match nothing at
    # all, even though it visibly has beers on the map.
    beers = []
    for b in festival_beers:
        if _stand_brewery(b) != brewery:
            continue
        bid = _int_beer_id(b)
        if bid is None:
            continue
        beers.append({
            "beerId": bid,
            "name": b.get("name"),
            "brewery": b.get("brewery"),
            "style": b.get("style"),
            "sessions": _sessions_for(b.get("id"), beer_sessions, session_order),
        })
    return beers


async def handle_festival_brewery(request: web.Request) -> web.Response:
    """Full beer list for one brewery, personally annotated with had-it -
    the "drill in" view opened by tapping a brewery pill on the festival
    map. Uses had_it_index's already-synced local data (like
    handle_festival_session) rather than _annotate_had_it's live,
    quota-capped Untappd lookups - a brewery can have plenty of beers and
    this screen has no reason to cost quota just to show its own history."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    brewery = (body.get("brewery") or "").strip()
    if not brewery:
        return _json_error("invalid_brewery")

    festival_key = await _resolve_festival_key(user_id)
    beers = _stand_beers(festival_key, brewery)

    for beer in beers:
        result = await had_it_index.lookup_had_it(user_id, beer["beerId"])
        if result:
            beer["hadIt"] = result.get("hadIt", False)
            beer["userRating"] = result.get("userRating")

    group = await _active_group(user_id)
    await _annotate_queue_status(beers, user_id, group["chatId"] if group else None)
    return web.json_response({"brewery": brewery, "beers": beers})


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

    festival_key = await _resolve_festival_key(user_id)
    festival_beers, sessions_raw = _festival_data_for(festival_key)
    beer_sessions, session_beer_ids, session_order, _ = _derive_session_data(festival_beers, sessions_raw)

    session = (body.get("session") or "").strip()
    if session not in session_order:
        return _json_error("invalid_session")
    query = (body.get("query") or "").strip()

    session_ids = session_beer_ids.get(session, set())
    candidates = [b for b in festival_beers if _int_beer_id(b) in session_ids]

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
            "sessions": _sessions_for(b.get("id"), beer_sessions, session_order),
        })

    for beer in beers:
        result = await had_it_index.lookup_had_it(user_id, beer["beerId"])
        if result:
            beer["hadIt"] = result.get("hadIt", False)
            beer["userRating"] = result.get("userRating")

    group = await _active_group(user_id)
    await _annotate_queue_status(beers, user_id, group["chatId"] if group else None)
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
    visited_regions = await venue_index.get_visited_regions(user_id)
    had_it_synced = await had_it_index.is_fully_synced(user_id)
    venue_synced = await venue_index.is_fully_synced(user_id)
    profile = await user_tokens.get_profile(user_id)
    is_supporter = bool((profile or {}).get("is_supporter"))
    rows = (
        badge_stats.compute_progress(beers, is_supporter=is_supporter)
        + badge_stats.compute_distinct_progress(beers)
        + badge_stats.compute_range_progress(beers)
        + badge_stats.compute_venue_progress(visited_categories, visited_regions)
    )
    # personalUrl (untappd.com/user/{username}/badges/{user_badge_id}) is
    # the real per-earned-instance page - only known once badge_index.py has
    # actually seen this exact badge in the owner's check-in history (see
    # its own docstring for why there's no way to look this up on demand),
    # and only used when it matches the level actually shown (see below,
    # right after the level correction it depends on). Falls back to the
    # catalog's generic badges.untappd.com page client-side when absent -
    # see app.js's renderBadgeDetail.
    #
    # badge_index.py's level is also the source of GROUND TRUTH for the
    # level itself: compute_progress only ever *estimates* a level from our
    # own style/country catalog counting had_it_index's beers, which can
    # undercount if that catalog's matching is ever imperfect (confirmed to
    # happen at least once this session already). badge_index.py's level
    # comes straight from Untappd's own check-in "badges" array, so when
    # it's HIGHER than our estimate, it wins - see badge_stats.level_floor's
    # own comment on why the corrected row shows pct=0 rather than a
    # fabricated fraction. The reverse can also happen though: badge_index's
    # capture depends on having actually fetched a check-in carrying that
    # exact "(Level N)" badge, so it can lag BEHIND compute_progress's own
    # estimate too - that's exactly when personalUrl must NOT be used.
    username = (profile or {}).get("username")
    if username:
        earned = await badge_index.get_all(user_id)
        for row in rows:
            entry = earned.get(row["name"])
            if not entry:
                continue
            confirmed_level = entry.get("level")
            if row["levels"] <= 1:
                # Single-tier badge - any recorded instance at all confirms
                # it's earned, regardless of confirmed_level (always None
                # for these - there's no "(Level N)" suffix to parse).
                if not row["done"]:
                    row["level"] = 1
                    row["pct"] = 100
                    row["done"] = True
            elif confirmed_level is not None and confirmed_level > row["level"]:
                level_start, next_threshold = badge_stats.level_floor(
                    row["countPerLevel"], row["levels"], confirmed_level, row["firstLevelCount"],
                )
                row["level"] = confirmed_level
                row["current"] = level_start
                row["nextThreshold"] = next_threshold
                # pct=0 ("don't fabricate a fraction we can't support" - see
                # badge_stats.level_floor's own comment) only makes sense
                # for a non-final corrected level, where there genuinely IS
                # an unknown fraction hiding somewhere inside it. Once the
                # corrected level reaches the catalog's own cap
                # (next_threshold is None - nothing left to level up
                # toward), there's no fraction left to be honest or
                # dishonest about - the badge is unambiguously done, same
                # as badge_stats._row's own pct=100 for that case. Proven
                # live: without this, a maxed-out badge showed "500 / 500"
                # in text (app.js falls back to `current` when
                # nextThreshold is null) right next to a visually EMPTY
                # progress ring, since the ring's fill directly uses pct.
                row["pct"] = 100 if next_threshold is None else 0
                row["done"] = next_threshold is None
            # personalUrl is ideally only used when badge_index.py actually
            # captured the SAME level being shown - it only records a level
            # when it happens to see that exact "(Level N)" badge attached to
            # a fetched check-in, which can lag behind compute_progress's own
            # independent beer-count estimate (confirmed to happen: a badge
            # whose count/level is already correct on-screen still linked out
            # to an old, long-superseded level's page - each personalUrl is a
            # FROZEN snapshot of one specific award moment, not a live page,
            # so a stale one really does show a lower number when clicked).
            #
            # Confirmed live (2026-09-22, user's own real Untappd profile):
            # compute_progress's count can be exactly right - 56/284/285,
            # matching Untappd's own display digit for digit - while
            # badge_index still sits on a stale level 53 catch, because
            # Untappd itself never tagged the check-ins that crossed 54-56
            # with a "(Level N)" badge at all; no amount of re-walking finds
            # what was never tagged. Refusing personalUrl forever in that
            # case throws away a real, working link for no benefit once
            # there's nothing left to passively discover - so a stale link is
            # still offered once the RELEVANT index has completed at least
            # one full walk (had_it_index for style/country badges,
            # venue_index for venue badges - see each one's own
            # is_fully_synced), just labeled with the level it actually shows
            # (personalUrlStaleLevel) so the Mini App can caption it honestly
            # instead of presenting a lower number as if it were current.
            user_badge_id = entry.get("userBadgeId")
            if user_badge_id:
                if row["levels"] <= 1 or confirmed_level == row["level"]:
                    row["personalUrl"] = f"https://untappd.com/user/{username}/badges/{user_badge_id}"
                elif confirmed_level is not None and confirmed_level < row["level"]:
                    source_synced = had_it_synced if row["kind"] in ("style", "country", "distinct", "range") else venue_synced
                    if source_synced:
                        row["personalUrl"] = f"https://untappd.com/user/{username}/badges/{user_badge_id}"
                        row["personalUrlStaleLevel"] = confirmed_level
    # Default order only - the Mini App re-sorts/filters this same fetched
    # list client-side (level ascending, alphabetical, search), so this is
    # just the initial "most actionable first" view, not the only one.
    rows.sort(key=lambda r: (-r["level"], -r["pct"]))
    return web.json_response({"badges": rows})


async def handle_special_badges_get(request: web.Request) -> web.Response:
    """Untappd's own time-limited promotional badges currently active
    (special_badges.json, hand-curated from untappd.com/blog - see
    badge_stats.compute_special_badges' own docstring for why this can
    only ever recommend what to check in, never confirm it's already
    done). No live Untappd call, no extra quota - had_it_index's
    already-synced beer history is enough to suggest matches."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    beers = await had_it_index.get_all_beers(user_id)
    dismissed = await special_badge_dismissals.get_dismissed(user_id)
    rows = [r for r in badge_stats.compute_special_badges(beers) if r["badge"] not in dismissed]
    rows.sort(key=lambda r: r["daysRemaining"])
    return web.json_response({"badges": rows})


async def handle_special_badges_dismiss(request: web.Request) -> web.Response:
    """"Вже отримав" - hides one special-badge card for this user (see
    special_badge_dismissals.py's own docstring: purely a display filter,
    not a claim to Untappd that anything was actually earned)."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    badge_name = (body.get("badge") or "").strip()
    if not badge_name:
        return _json_error("invalid_badge")

    await special_badge_dismissals.dismiss(user_id, badge_name)
    return web.json_response({"ok": True})


BJCP_GENERAL_GUIDE_URL = "https://www.bjcp.org/beer-styles/beer-style-guidelines/"


async def handle_style_info(request: web.Request) -> web.Response:
    """The badge-detail screen's clickable style tags - "what actually IS
    this style" for one of a style badge's own catalog tags (e.g. "Stout
    - Pastry"). See bjcp_styles.py's own module docstring for why BJCP
    (not Untappd, which has no such page/API of its own) is the
    description source, and why a fuzzy but ungrounded guess is refused
    rather than risking a wrong style's description shown as fact."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    style = (body.get("style") or "").strip()
    if not style:
        return _json_error("invalid_style")

    match = bjcp_styles.find_style(style)
    if not match:
        return web.json_response({"matched": False, "guideUrl": BJCP_GENERAL_GUIDE_URL})
    if match.get("note"):
        lang_code = (init_data.get("user") or {}).get("language_code") or "en"
        match = {**match, "note": i18n.t(lang_code, match["note"])}
    return web.json_response({"matched": True, **match})


async def handle_lens_lookup(request: web.Request) -> web.Response:
    """Batch beer lookup for a browser userscript (see LENS_API_TOKEN's own
    comment above): given raw {name, brewery} pairs scraped from a shop's
    product listing, resolve each on Untappd via beer_match.resolve_beer
    (same exact-match discipline /scan uses) and report had-it/rating/link
    for each, plus inWishlist (the classic Untappd Wishlist) and inSheetList
    (this app's own "Мій список" - native items added via the webapp's tab
    PLUS a personal Google Sheet, both merged by _get_my_list_items; kept
    the inSheetList name for backward compat with the already-shipped
    extension/userscript, even though "sheet" is now only one of its two
    sources) - deliberately no badge computation here, out of scope for
    "should I buy this" browsing (unlike /scan's own use of the same
    resolver)."""
    if not LENS_API_TOKEN or request.headers.get("X-Lens-Token", "") != LENS_API_TOKEN:
        return _json_error("unauthorized", 401)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("invalid_json")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return _json_error("no_items")
    items = items[:LENS_MAX_ITEMS_PER_REQUEST]

    owner_id = int(AUTO_TOAST_OWNER_ID)
    token = await user_tokens.get_token(owner_id)
    if not token:
        return _json_error("not_connected")

    # Fetched ONCE per batch, not per item - it's the same account's
    # wishlist for every card on the page, and _get_wishlist_beers already
    # shares the 15-min-TTL _wishlist_cache with the checkin webapp's own
    # wishlist-priority search, so this is usually a cache hit (0 extra
    # quota) rather than a fresh get_my_wishlist call.
    wishlist_beers = await _get_wishlist_beers(owner_id, token)
    wishlist_bids = {b.get("bid") for b in wishlist_beers if b.get("bid") is not None}
    my_list_bids = {it["beerId"] for it in await _get_my_list_items(owner_id) if it.get("beerId")}

    semaphore = asyncio.Semaphore(LENS_LOOKUP_CONCURRENCY)

    async def _resolve_one(item) -> dict:
        name = (item.get("name") or "").strip() if isinstance(item, dict) else ""
        brewery = (item.get("brewery") or "").strip() if isinstance(item, dict) else ""
        if not name:
            return {"matched": False, "query_name": name, "query_brewery": brewery, "candidates": []}
        async with semaphore:
            try:
                # need_country=False: no badge computation here, no use for
                # country. live_fallback=False: never call check_i_had_beer
                # either - trust had_it_index's own (monotonically-growing)
                # data instead. Together these make this endpoint spend NO
                # Untappd quota at all beyond the free search_beers lookup -
                # a handful of concurrent get_beer/check_i_had_beer calls
                # was observed live to 429 almost every one of them, which
                # is what was actually causing most of the "невідомо, чи
                # пив" results, not genuinely unknown data.
                result = await beer_match.resolve_beer(
                    token, owner_id, name, brewery, need_country=False, live_fallback=False,
                )
                result["inWishlist"] = result.get("bid") in wishlist_bids
                result["inSheetList"] = result.get("bid") in my_list_bids
                return result
            except untappd_mcp.UntappdMCPError as exc:
                logger.warning("lens lookup failed for %r %r: %s", brewery, name, exc)
                return {
                    "matched": False, "query_name": name, "query_brewery": brewery,
                    "candidates": [], "searchUrl": beer_match.build_search_url(brewery, name), "error": True,
                }
            except Exception:
                # ANY other exception here (a beer_match bug tripped by one
                # unlucky name/brewery string, a transient local-index read
                # issue, anything not already an UntappdMCPError) must NOT
                # propagate past this one item - proven live: asyncio.gather
                # has no return_exceptions=True, so a single unhandled
                # exception for ONE item on a shop's page previously killed
                # the entire batch response (a generic 500, "Server got
                # itself in trouble"), leaving EVERY OTHER beer on that same
                # page - unrelated to whatever actually failed - with no
                # overlay at all. logger.exception (not .warning) captures
                # the full traceback, since this is by definition an
                # unexpected code path worth investigating, unlike the
                # already-understood UntappdMCPError case above.
                logger.exception("lens lookup: unexpected error for %r %r", brewery, name)
                return {
                    "matched": False, "query_name": name, "query_brewery": brewery,
                    "candidates": [], "searchUrl": beer_match.build_search_url(brewery, name), "error": True,
                }

    results = await asyncio.gather(*(_resolve_one(it) for it in items))
    try:
        await lens_log.record(items, results)
    except Exception:
        logger.exception("lens_log: failed to record a lookup batch")  # never fail the user's page over bookkeeping
    return web.json_response({"ok": True, "results": results})


async def handle_lens_report(request: web.Request) -> web.Response:
    """Review view of lens_log.py (same X-Lens-Token as /api/lens/lookup):
    unmatched products, suspicious matches and outcome changes - e.g.
    `curl -H "X-Lens-Token: $LENS_API_TOKEN" <origin>/api/lens/report?limit=30`."""
    if not LENS_API_TOKEN or request.headers.get("X-Lens-Token", "") != LENS_API_TOKEN:
        return _json_error("unauthorized", 401)
    try:
        limit = max(1, min(200, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50
    report = await lens_log.report(limit)
    report["crawl"] = {**shop_crawl.load_state(), "running": _shop_crawl_running}
    return web.json_response(report)


async def handle_lens_crawl(request: web.Request) -> web.Response:
    """Starts the shop crawl now (same X-Lens-Token) instead of waiting for
    the daily run - e.g. right after a matcher fix, to re-resolve what was
    unmatched. Runs in the background; progress shows up in /api/lens/report
    under "crawl"."""
    if not LENS_API_TOKEN or request.headers.get("X-Lens-Token", "") != LENS_API_TOKEN:
        return _json_error("unauthorized", 401)
    if _shop_crawl_running:
        return _json_error("already_running", 409)
    global _shop_crawl_manual_task
    _shop_crawl_manual_task = asyncio.create_task(_run_shop_crawl())
    return web.json_response({"ok": True, "started": True}, status=202)


async def handle_deploy_webhook(request: web.Request) -> web.Response:
    """GitHub push webhook -> `git pull` + restart, so a code change reaches
    the always-on VM without a manual SSH session every time (unlike
    special_badges.json's own hourly pull loop, this is push-triggered and
    covers the WHOLE repo - .py files, webapp/* static assets, everything).

    Verifies GitHub's HMAC-SHA256 body signature (X-Hub-Signature-256)
    against DEPLOY_WEBHOOK_SECRET before doing anything - this endpoint runs
    `git pull` in a real directory and can restart the whole process, so an
    unauthenticated version of it would be a remote-code-deploy hole. Only
    reacts to a push actually landing on refs/heads/master; every other
    ref (a branch, a tag) is acknowledged with 200 (so GitHub doesn't retry
    it as a delivery failure) but does nothing.

    Restart reuses bot.py's own restart_notify.json + stop_running()
    mechanism (see post_init) - same clean handoff to systemd's
    `Restart=always` that /restart already relies on, just with reason:
    "deploy" so the owner's notification text says a deploy happened
    instead of a manual restart."""
    if not DEPLOY_WEBHOOK_SECRET:
        return _json_error("webhook_not_configured", 501)

    raw_body = await request.read()
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(DEPLOY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return _json_error("bad_signature", 401)

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        return _json_error("invalid_json")
    if payload.get("ref") != "refs/heads/master":
        return web.json_response({"ok": True, "deployed": False, "reason": "not master"})

    proc = await asyncio.create_subprocess_exec(
        "git", "pull", "--ff-only", cwd=_data_dir,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
    if proc.returncode != 0:
        logger.error("deploy webhook: git pull failed: %s", output)
        return web.json_response({"ok": False, "error": "git_pull_failed", "output": output}, status=500)

    if "Already up to date" in output:
        return web.json_response({"ok": True, "deployed": False, "reason": "no changes"})

    logger.info("deploy webhook: pulled new code, restarting: %s", output.strip())
    try:
        with open(os.path.join(_data_dir, "restart_notify.json"), "w", encoding="utf-8") as f:
            json.dump({"chatId": int(AUTO_TOAST_OWNER_ID), "lang": "uk", "reason": "deploy"}, f)
    except OSError:
        logger.warning("deploy webhook: could not write restart_notify.json", exc_info=True)
    response = web.json_response({"ok": True, "deployed": True})
    if _ptb_app is not None:
        # Respond first, then stop - GitHub's webhook delivery has its own
        # timeout and shouldn't be left hanging on the process tearing
        # itself down.
        asyncio.get_running_loop().call_later(1, _ptb_app.stop_running)
    return response


async def handle_queue_list(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    token = await _resolve_token(tg_user)

    group = await _active_group(user_id)
    if not group:
        # Distinct from a genuinely empty queue (total == 0 below) - the
        # Mini App shows a "run /join_group in your festival's chat" hint
        # instead of "черга порожня", which would wrongly imply adding a
        # beer is possible right now.
        return web.json_response({"items": [], "total": 0, "noGroup": True})

    all_items = await checkin_queue.list_items(group["chatId"])

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
    # Annotate AFTER filtering - _annotate_had_it only touches the first 5
    # (quota-conserving), and those must be the first 5 the viewer will
    # actually see, not 5 that might include beers already filtered out of
    # their own view (which would both waste quota-costing check_i_had_beer
    # calls on beers this response never returns, and leave later, genuinely
    # visible beers with no had-it badge at all).
    await _annotate_had_it(items, user_id, token)
    if not token:
        for it in items:
            it.setdefault("hadIt", None)

    # Mark queue items the viewer already attempted and that got saved to
    # their own "Відкладені" list instead of completing the queue item (see
    # handle_submit) - without this, a queue row that's actually already
    # been tried looks identical to one nobody has touched yet.
    pending_queue_ids = {
        it.get("queueItemId")
        for it in await pending_checkins.list_items(user_id)
        if it.get("queueItemId")
    }
    for it in items:
        it["pendingForMe"] = it.get("id") in pending_queue_ids

    return web.json_response({"items": items, "total": len(all_items), "groupTitle": group["chatTitle"]})


async def handle_queue_add(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    beer_id = body.get("beerId")
    if not isinstance(beer_id, int) or beer_id <= 0:
        return _json_error("invalid_beer_id")

    # Defense in depth - the Mini App also hides/disables the "+" button
    # when it knows there's no active group, but this endpoint must refuse
    # on its own too (e.g. a stale page that hasn't re-fetched queue state
    # yet). There's no group to file the item under otherwise.
    group = await _active_group(user_id)
    if not group:
        return _json_error("no_active_group")

    added_by = {
        "userId": user_id,
        "name": tg_user.get("first_name") or tg_user.get("username") or "?",
    }
    item, status = await checkin_queue.add_item(body, added_by, group["chatId"])
    return web.json_response({"ok": True, "item": item, "status": status})


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

    group = await _active_group(user_id)
    count = await checkin_queue.hide_all(user_id, group["chatId"]) if group else 0
    return web.json_response({"ok": True, "cleared": count})


async def handle_queue_reset_personal(request: web.Request) -> web.Response:
    """Settings-screen "forget my test check-ins" action - clears the
    caller's own completedBy marker (see checkin_queue.reset_user) so a
    pre-festival test check-in through the queue stops being reported as
    "already had this at the festival", without dumping it back into their
    active queue view. Beers they'd separately hidden on purpose are left
    untouched."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    group = await _active_group(user_id)
    count = await checkin_queue.reset_user(user_id, group["chatId"]) if group else 0
    return web.json_response({"ok": True, "reset": count})


async def handle_festival_map_get(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    festival_key = await _resolve_festival_key(user_id)
    return web.json_response(await _festival_map_payload(festival_key))


async def _festival_map_payload(festival_key: str | None) -> dict:
    beers, _ = _festival_data_for(festival_key)
    map_key = festival_key or _active_festival_key

    zone_names = _festival_editable_zone_names(beers)
    zone_hint = _festival_brewery_zone_map(beers)
    known_breweries = list(zone_hint.keys())
    template = _festival_layout_template(map_key)
    # The whole floor plan shows up from day one: plan stands with no beers
    # in the data yet are placeholders (see festival_map.planned_stands).
    planned = festival_map.planned_stands(template, known_breweries)
    zones = await festival_map.get_layout(
        map_key, known_breweries + planned, zone_hint, zone_names, template=template,
    )
    return {
        "zones": zones,
        "plannedStands": planned,
        "waterStands": festival_map.water_stands(template, known_breweries + planned),
        "zoneOrder": zone_names,
        "zoneLabels": _zone_labels_for(map_key),
        "bonusCategories": _festival_bonus_categories(beers),
        "breweryAliases": _festival_brewery_aliases(beers),
    }


async def handle_festival_map_move(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    festival_key = await _resolve_festival_key(user_id)
    beers, _ = _festival_data_for(festival_key)
    map_key = festival_key or _active_festival_key

    brewery = body.get("brewery")
    zone = body.get("zone")
    side = body.get("side")
    index = body.get("index")
    island_id = body.get("islandId")
    zone_map = _festival_brewery_zone_map(beers)
    if not isinstance(brewery, str) or (
        brewery not in zone_map
        and brewery not in festival_map.planned_stands(_festival_layout_template(map_key), list(zone_map))
    ):
        return _json_error("invalid_brewery")
    if not isinstance(index, int) or index < 0:
        return _json_error("invalid_index")
    if island_id is not None and not isinstance(island_id, str):
        return _json_error("invalid_island")

    moved = await festival_map.move_brewery(
        map_key, brewery, zone, side, index, _festival_editable_zone_names(beers), island_id=island_id
    )
    if not moved:
        return _json_error("invalid_zone")
    return web.json_response({"ok": True})


async def _handle_festival_map_gap(request: web.Request, edit) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    user_id = (init_data.get("user") or {}).get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    index = body.get("index")
    island_id = body.get("islandId")
    if not isinstance(index, int) or index < 0:
        return _json_error("invalid_index")
    if island_id is not None and not isinstance(island_id, str):
        return _json_error("invalid_island")
    festival_key = await _resolve_festival_key(user_id)
    beers, _ = _festival_data_for(festival_key)
    map_key = festival_key or _active_festival_key
    done = await edit(
        map_key, body.get("zone"), body.get("side"), index, _festival_editable_zone_names(beers), island_id=island_id
    )
    if not done:
        return _json_error("invalid_gap")
    return web.json_response({"ok": True})


async def handle_festival_map_gap_insert(request: web.Request) -> web.Response:
    return await _handle_festival_map_gap(request, festival_map.insert_gap)


async def handle_festival_map_gap_remove(request: web.Request) -> web.Response:
    return await _handle_festival_map_gap(request, festival_map.remove_gap)


async def handle_festival_map_island_create(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    festival_key = await _resolve_festival_key(user_id)
    beers, _ = _festival_data_for(festival_key)
    map_key = festival_key or _active_festival_key

    zone = body.get("zone")
    island_id = await festival_map.create_island(map_key, zone, _festival_editable_zone_names(beers))
    if island_id is None:
        return _json_error("invalid_zone")
    return web.json_response({"ok": True, "islandId": island_id})


async def handle_festival_map_island_delete(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    festival_key = await _resolve_festival_key(user_id)
    beers, _ = _festival_data_for(festival_key)
    map_key = festival_key or _active_festival_key

    zone = body.get("zone")
    island_id = body.get("islandId")
    if not isinstance(island_id, str):
        return _json_error("invalid_island")
    deleted = await festival_map.delete_island(map_key, zone, island_id, _festival_editable_zone_names(beers))
    return web.json_response({"ok": deleted})


async def _get_my_list_items(user_id: int) -> list[dict]:
    """Merges the user's own live-editable items (wishlist_items.py) with
    their registered Google Sheet's rows (wishlist_sheets.py/
    _get_wishlist_sheet_rows), unioned by beer id. A sheet row is dropped
    when the same beer is already a native item - native wins on
    duplicates, since it's the one the user can actually manage from the
    webapp (see wishlist_items.py's own module docstring). Backs both the
    "Мій список" tab (handle_wishlist_list) and the search screen's
    "Вішліст" priority checkbox (_search_wishlist) - the latter switched
    from Untappd's own classic Wishlist to this app's own list, since it's
    the one the user actually curates here."""
    native_items = await wishlist_items.list_items(user_id)
    for it in native_items:
        it["source"] = "native"
    native_bids = {it.get("beerId") for it in native_items if it.get("beerId") is not None}

    sheet_rows = await _get_wishlist_sheet_rows(user_id)
    sheet_items = [
        {
            "id": None,
            "beerId": r.get("bid"),
            "name": r.get("name"),
            "brewery": r.get("brewery"),
            "style": r.get("style"),
            "abv": r.get("abv"),
            "labelUrl": None,
            "source": "sheet",
        }
        for r in sheet_rows
        if r.get("bid") not in native_bids
    ]

    return native_items + sheet_items


async def handle_wishlist_list(request: web.Request) -> web.Response:
    """The webapp's "Мій список" tab - see _get_my_list_items. Annotated
    with hadIt/userRating (same as search - see _annotate_had_it) so an
    already-tried beer shows its checkmark here too, and so selectBeer's
    rating pre-fill (app.js) kicks in when checking it in again."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    token = await _resolve_token(tg_user)

    items = await _get_my_list_items(user_id)
    # Most of this list resolves for free via the already-synced had_it_index
    # (see _get_had_it) rather than a live call, so a much higher cap than
    # search's quota-conscious default(5) is fine for a personal list this
    # size - but still capped, not len(items), in case someone's imported
    # Sheet is huge and largely outside the synced index.
    await _annotate_had_it(items, user_id, token, limit=30)
    group = await _active_group(user_id)
    await _annotate_queue_status(items, user_id, group["chatId"] if group else None)
    return web.json_response({"items": items})


async def handle_wishlist_add(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")

    beer_id = body.get("beerId")
    if not isinstance(beer_id, int) or beer_id <= 0:
        return _json_error("invalid_beer_id")

    item, added = await wishlist_items.add_item(user_id, body)
    return web.json_response({"ok": True, "item": item, "added": added})


async def handle_wishlist_remove(request: web.Request) -> web.Response:
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

    removed = await wishlist_items.remove_item(user_id, item_id)
    return web.json_response({"ok": True, "removed": removed})


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


async def handle_festival_mode_get(request: web.Request) -> web.Response:
    """Pauses had_it/venue backfill + auto-toast for the caller (see
    festival_mode.py's own module docstring) - e.g. for the duration of an
    actual festival, to keep quota entirely for live search/check-ins."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    enabled = await festival_mode.is_enabled(user_id)
    return web.json_response({"enabled": enabled})


async def handle_festival_mode_toggle(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    await festival_mode.set_enabled(user_id, bool(body.get("enabled")))
    return web.json_response({"ok": True})


async def handle_maintenance_get(request: web.Request) -> web.Response:
    """Global maintenance-mode status for the Mini App's own splash gate
    (app.js checks this before rendering anything else - see maintenance_
    mode.py). Deliberately requires NO init-data validation, unlike every
    other handler here - the whole point is to still answer honestly even
    if something else about the session/auth path is degraded, so the
    splash can show instead of a confusing blank/broken screen. Toggled
    only via /maintenance in Telegram (bot.py, owner-only), never from the
    Mini App itself."""
    status = await maintenance_mode.get_status()
    return web.json_response(status)


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


async def handle_i18n_get(request: web.Request) -> web.Response:
    """The Mini App's translation table in the viewer's own language (their
    Telegram client's language_code - the one signal that's per-user rather
    than per-deployment). Also remembers it for server-pushed messages."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    code = tg_user.get("language_code")
    if tg_user.get("id") and code:
        await user_tokens.set_language(tg_user["id"], code[:2].lower())
    return web.json_response({"lang": (code or "en")[:2].lower(), "strings": i18n.app_strings(code)})


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
    (ensureLocationManager, same mechanism "🧭 Локації поруч" already uses,
    no foursquareId available for a bare GPS point) or picking a named
    place from the existing Foursquare venue search (which does carry one).

    When a foursquareId is given AND DIRECT_TOKEN is configured, tries to
    resolve it to a real Untappd venue_id (untappd_direct.
    lookup_venue_by_foursquare) and attaches it via festival_watch.
    set_venue - this is what upgrades the watch from the old friends-only
    GPS+radius novelty check to the venue-checkins one that sees everyone
    (see _festival_watch_venue_loop). Resolution failing for any reason
    (no DIRECT_TOKEN, this place has no known Untappd venue yet, a
    transient API error) is NOT a request failure - the watch point is
    still set and still works via the plain GPS+radius path, just without
    the upgrade; never worth failing the whole "set my location" action
    over."""
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

    foursquare_id = body.get("foursquareId")
    if foursquare_id and DIRECT_TOKEN:
        try:
            resolved = await untappd_direct.lookup_venue_by_foursquare(DIRECT_TOKEN, foursquare_id)
        except untappd_mcp.UntappdMCPError as e:
            logger.warning("festival_watch venue resolution failed for %s: %s", foursquare_id, e)
            resolved = None
        if resolved:
            await festival_watch.set_venue(user_id, resolved["venueId"], resolved["venueName"])

    return web.json_response({"ok": True})


async def handle_festival_watch_add_extra_venue(request: web.Request) -> web.Response:
    """Adds a picked Foursquare place to the owner's extra-venues list
    (festival_watch.add_extra_venue, scraped by _festival_watch_scrape_loop).
    Unlike set_location, a failed Untappd venue resolution IS an error here -
    an extra venue has no GPS+radius fallback, it only exists as a venue_id.
    Costs one DIRECT_TOKEN call (the foursquare lookup), once, at add time."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    foursquare_id = body.get("foursquareId")
    if not foursquare_id:
        return _json_error("invalid_venue")
    if not DIRECT_TOKEN:
        return _json_error("no_direct_token", 503)
    try:
        resolved = await untappd_direct.lookup_venue_by_foursquare(DIRECT_TOKEN, foursquare_id)
    except untappd_mcp.UntappdRateLimited:
        return _json_error("rate_limited", 429)
    except untappd_mcp.UntappdMCPError as e:
        logger.warning("festival_watch extra venue resolution failed for %s: %s", foursquare_id, e)
        return _json_error("lookup_failed", 502)
    if not resolved:
        return _json_error("venue_not_found", 404)
    added = await festival_watch.add_extra_venue(user_id, resolved["venueId"], resolved["venueName"] or body.get("name"))
    if not added:
        return _json_error("already_listed_or_full", 409)
    return web.json_response({"ok": True})


async def handle_festival_watch_remove_extra_venue(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    user_id = (init_data.get("user") or {}).get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    removed = await festival_watch.remove_extra_venue(user_id, body.get("venueId"))
    return web.json_response({"ok": True, "removed": removed})


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


async def handle_festival_watch_set_notify_listed(request: web.Request) -> web.Response:
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_error("invalid_json")
    await festival_watch.set_notify_listed(user_id, bool(body.get("enabled")))
    return web.json_response({"ok": True})


def _build_app() -> web.Application:
    app = web.Application(middlewares=[_no_cache_middleware])
    app.router.add_get("/checkin", handle_index)
    app.router.add_get("/map", handle_public_index)
    app.router.add_post("/api/public/i18n", handle_public_i18n)
    app.router.add_post("/api/public/festival/meta", handle_public_meta)
    app.router.add_post("/api/public/festival_map/get", handle_public_map_get)
    app.router.add_post("/api/public/festival/brewery", handle_public_brewery)
    app.router.add_post("/api/public/search", handle_public_search)
    app.router.add_static("/static/checkin/", path=WEBAPP_DIR, name="checkin_static")
    app.router.add_post("/api/checkin/search", handle_search)
    app.router.add_post("/api/checkin/venues", handle_venues)
    app.router.add_post("/api/checkin/venues/nearby", handle_venues_nearby)
    app.router.add_post("/api/checkin/usage", handle_usage)
    app.router.add_post("/api/checkin/submit", handle_submit)
    app.router.add_post("/api/checkin/festival/stats", handle_festival_stats)
    app.router.add_post("/api/checkin/festival/session", handle_festival_session)
    app.router.add_post("/api/checkin/festival/brewery", handle_festival_brewery)
    app.router.add_post("/api/checkin/festival/meta", handle_festival_meta)
    app.router.add_post("/api/checkin/queue/list", handle_queue_list)
    app.router.add_post("/api/checkin/queue/add", handle_queue_add)
    app.router.add_post("/api/checkin/queue/remove", handle_queue_remove)
    app.router.add_post("/api/checkin/queue/clear", handle_queue_clear)
    app.router.add_post("/api/checkin/queue/reset_personal", handle_queue_reset_personal)
    app.router.add_post("/api/checkin/festival_map/get", handle_festival_map_get)
    app.router.add_post("/api/checkin/festival_map/move", handle_festival_map_move)
    app.router.add_post("/api/checkin/festival_map/gap_insert", handle_festival_map_gap_insert)
    app.router.add_post("/api/checkin/festival_map/gap_remove", handle_festival_map_gap_remove)
    app.router.add_post("/api/checkin/festival_map/island_create", handle_festival_map_island_create)
    app.router.add_post("/api/checkin/festival_map/island_delete", handle_festival_map_island_delete)
    app.router.add_post("/api/checkin/wishlist/list", handle_wishlist_list)
    app.router.add_post("/api/checkin/wishlist/add", handle_wishlist_add)
    app.router.add_post("/api/checkin/wishlist/remove", handle_wishlist_remove)
    app.router.add_post("/api/checkin/pending/list", handle_pending_list)
    app.router.add_post("/api/checkin/pending/retry", handle_pending_retry)
    app.router.add_post("/api/checkin/pending/remove", handle_pending_remove)
    app.router.add_post("/api/checkin/autotoast/friends", handle_autotoast_friends)
    app.router.add_post("/api/checkin/autotoast/toggle", handle_autotoast_toggle)
    app.router.add_post("/api/checkin/autotoast/set_targets", handle_autotoast_set_targets)
    app.router.add_post("/api/checkin/autotoast/remove_target", handle_autotoast_remove_target)
    app.router.add_post("/api/checkin/i18n", handle_i18n_get)
    app.router.add_post("/api/checkin/festival_watch/get", handle_festival_watch_get)
    app.router.add_post("/api/checkin/festival_watch/set_location", handle_festival_watch_set_location)
    app.router.add_post("/api/checkin/festival_watch/add_extra_venue", handle_festival_watch_add_extra_venue)
    app.router.add_post("/api/checkin/festival_watch/remove_extra_venue", handle_festival_watch_remove_extra_venue)
    app.router.add_post("/api/checkin/festival_watch/set_radius", handle_festival_watch_set_radius)
    app.router.add_post("/api/checkin/festival_watch/toggle", handle_festival_watch_toggle)
    app.router.add_post("/api/checkin/festival_watch/set_notify_listed", handle_festival_watch_set_notify_listed)
    app.router.add_post("/api/checkin/maintenance/get", handle_maintenance_get)
    app.router.add_post("/api/checkin/autotoast/status", handle_autotoast_status)
    app.router.add_post("/api/checkin/comment_watch/get", handle_comment_watch_get)
    app.router.add_post("/api/checkin/comment_watch/toggle", handle_comment_watch_toggle)
    app.router.add_post("/api/checkin/festival_mode/get", handle_festival_mode_get)
    app.router.add_post("/api/checkin/festival_mode/toggle", handle_festival_mode_toggle)
    app.router.add_post("/api/checkin/events/get", handle_events_get)
    app.router.add_post("/api/checkin/events/reply", handle_events_reply)
    app.router.add_post("/api/checkin/badges/get", handle_badges_get)
    app.router.add_post("/api/checkin/special_badges/get", handle_special_badges_get)
    app.router.add_post("/api/checkin/special_badges/dismiss", handle_special_badges_dismiss)
    app.router.add_post("/api/checkin/style_info", handle_style_info)
    app.router.add_post("/api/lens/lookup", handle_lens_lookup)
    app.router.add_get("/api/lens/report", handle_lens_report)
    app.router.add_post("/api/lens/crawl", handle_lens_crawl)
    app.router.add_post("/api/deploy/webhook", handle_deploy_webhook)
    app.router.add_post("/api/checkin/festival/list", handle_festival_list)
    app.router.add_post("/api/checkin/festival/switch", handle_festival_switch)
    app.router.add_post("/api/checkin/festival/my/get", handle_my_festival_get)
    app.router.add_post("/api/checkin/festival/my/set", handle_my_festival_set)
    app.router.add_post("/api/checkin/command_flags/get", handle_command_flags_get)
    app.router.add_post("/api/checkin/command_flags/set", handle_command_flags_set)
    app.router.add_post("/api/checkin/command_flags/reorder", handle_command_flags_reorder)
    return app


def _derive_session_data(festival_beers: list | None, sessions_raw: dict | None):
    """Pure version of the derivation _set_festival_data used to do inline
    via module globals - returns (beer_sessions, session_beer_ids,
    session_order, session_colors) for the given (beers, sessions_raw) pair
    instead of assigning globals, so it's safe to call per-request with a
    DIFFERENT dataset on every call (one group's bound festival can differ
    from another's - see _resolve_festival_key/_festival_data_for). Sharing
    this via module globals the way _set_festival_data still does for the
    single default dataset would race: one group's request could overwrite
    the globals mid-read of another group's concurrent request."""
    festival_beers = festival_beers or []
    beer_sessions: dict[str, list] = {}
    session_beer_ids: dict[str, set] = {}
    if sessions_raw:
        session_order = list(sessions_raw.keys())
        session_colors = _assign_session_colors(session_order)
        for session, beers in sessions_raw.items():
            ids = session_beer_ids.setdefault(session, set())
            for beer in beers:
                raw_id = str(beer.get("id"))
                beer_sessions.setdefault(raw_id, [])
                if session not in beer_sessions[raw_id]:
                    beer_sessions[raw_id].append(session)
                bid = _int_beer_id(beer)
                if bid is not None:
                    ids.add(bid)
    else:
        # No session grouping at all in the source JSON (bot.py's load_db()
        # returns an empty sessions_raw for a flat beer list) - every beer
        # gets the same single synthetic session (identified by its assigned
        # color, since there's no real name to preserve), rather than
        # session_beer_ids staying empty and the whole festival-progress
        # feature silently showing 0/0 for everything.
        session = _assign_session_colors([""])[""]
        session_order = [session]
        session_colors = {session: session}
        ids = set()
        for beer in festival_beers:
            raw_id = str(beer.get("id"))
            beer_sessions[raw_id] = [session]
            bid = _int_beer_id(beer)
            if bid is not None:
                ids.add(bid)
        session_beer_ids[session] = ids
    return beer_sessions, session_beer_ids, session_order, session_colors


def _set_festival_data(festival_beers: list | None, sessions_raw: dict | None) -> None:
    """(Re)derives every festival-dataset global from a fresh (ALL_BEERS,
    SESSIONS_RAW) pair - the same assignment block start_webapp_server ran
    inline before this was factored out, now also reused by
    handle_festival_switch so an owner-triggered festival change takes
    effect immediately, with no restart (mirrors badge_stats.
    reload_special_badges's live-reload pattern). Only for the single
    process-wide DEFAULT dataset (boot time, and handle_festival_switch's
    default-changing path) - a request scoped to a specific group's bound
    festival calls _derive_session_data directly instead, see
    _resolve_festival_key/_festival_data_for."""
    global _festival_beers, _beer_sessions, _session_beer_ids, _session_order, _session_colors
    _festival_beers = festival_beers or []
    _beer_sessions, _session_beer_ids, _session_order, _session_colors = _derive_session_data(
        _festival_beers, sessions_raw
    )


async def start_webapp_server(
    ptb_app,
    festival_beers: list | None = None,
    data_dir: str | None = None,
    sessions_raw: dict | None = None,
    dev_mode: bool = False,
    reload_beer_db=None,
    active_festival_key: str | None = None,
    get_festival_data=None,
    get_toggleable_commands=None,
    refresh_command_menus=None,
    public_base_url: str | None = None,
) -> None:
    """Bind the aiohttp app on the port Fly's http_service expects (8080).

    Fire-and-forget from post_init via asyncio.create_task - returns once
    bound, the server keeps serving on the same event loop afterward.
    `ptb_app` is bot.py's python-telegram-bot Application - kept (as
    `_ptb_bot`) so _auto_toast_loop can send festival_watch notifications
    via `_ptb_bot.send_message`; nothing else here needs it. `festival_beers`
    is bot.py's ALL_BEERS (id/name/brewery/style/session) - searched before
    falling back to a live Untappd search. `data_dir` is bot.py's DATA_DIR
    (the persistent volume) for per-user tokens and the shared queue.
    `sessions_raw` is bot.py's SESSIONS_RAW (the undeduped {session: [beers]}
    dict) - used to recover every session a beer appears in, since
    ALL_BEERS itself only keeps the first. `dev_mode` (see dev_server.py)
    skips every `_start_*` background loop below - those hit the SAME
    Untappd DIRECT_TOKEN and GitHub repo the production VM already polls,
    so running them a second time from a laptop would just double the
    quota usage and the deploy/sync noise for zero benefit in a build
    that's only ever open in one person's own browser for a few minutes.
    `reload_beer_db` is bot.py's own function of the same name (a plain
    function reference, not called here) - handle_festival_switch calls it
    later to re-read a different festival's beer list into bot.py's own
    ALL_BEERS/SESSIONS_RAW globals (webapp_server.py can't import bot.py
    directly - bot.py already imports this module, so that would be
    circular), then feeds the result back into `_set_festival_data`."""
    global _ptb_bot, _ptb_app, _data_dir, _reload_beer_db_fn, _active_festival_key, _get_festival_data_fn, _get_toggleable_commands_fn, _refresh_command_menus_fn, _public_base_url
    _ptb_bot = ptb_app.bot
    _ptb_app = ptb_app
    _data_dir = os.path.abspath(data_dir or ".")
    _reload_beer_db_fn = reload_beer_db
    _active_festival_key = active_festival_key
    _get_festival_data_fn = get_festival_data
    _get_toggleable_commands_fn = get_toggleable_commands
    _refresh_command_menus_fn = refresh_command_menus
    _public_base_url = public_base_url
    _set_festival_data(festival_beers, sessions_raw)
    if data_dir:
        user_tokens.init(data_dir)
        checkin_queue.init(data_dir)
        group_membership.init(data_dir)
        group_festivals.init(data_dir)
        user_festivals.init(data_dir)
        feature_flags.init(data_dir)
        festival_map.init(data_dir)
        await festival_map.migrate_legacy_default(active_festival_key)
        had_it_index.init(data_dir)
        venue_index.init(data_dir)
        auto_toast.init(data_dir)
        festival_watch.init(data_dir)
        comment_watch.init(data_dir)
        festival_mode.init(data_dir)
        maintenance_mode.init(data_dir)
        event_log.init(data_dir)
        badge_index.init(data_dir)
        wishlist_sheets.init(data_dir)
        wishlist_items.init(data_dir)
        lens_log.init(data_dir)
        shop_crawl.init(data_dir)
        pending_checkins.init(data_dir)
        special_badge_dismissals.init(data_dir)
    port = int(os.environ.get("PORT", 8080))
    aio_app = _build_app()
    runner = web.AppRunner(aio_app, access_log_class=_DstAwareAccessLogger)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Festival check-in Mini App server listening on :%s", port)
    if not dev_mode:
        _start_had_it_backfill()
        _start_venue_backfill()
        _start_auto_toast()
        _start_comment_watch()
        _start_festival_watch_venue()
        _start_festival_watch_scrape()
        _start_shop_crawl()
        _start_badge_index_sync()
        _start_special_badges_sync()


def _load_festivals_registry() -> list[dict]:
    """Same file/shape as bot.py's own load_festivals_registry - duplicated
    rather than imported (see start_webapp_server's `reload_beer_db`
    docstring on why webapp_server.py can't import bot.py) since it's a
    single cheap file read, not worth threading through as another
    callback param."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "festivals.json"), encoding="utf-8") as f:
            return json.load(f).get("festivals", [])
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("Could not load festivals.json: %s", e)
        return []


_layout_template_cache: dict[str, tuple[float, dict | None]] = {}


def _festival_layout_template(festival_key: str | None) -> dict | None:
    """festival_layouts/<key>.json - the festival's official floor plan as a
    layout template (see festival_map.py's docstring), or None. Re-read only
    when the file changes; a malformed file is treated as absent rather than
    breaking the map."""
    if not festival_key or not re.fullmatch(r"[A-Za-z0-9_-]+", festival_key):
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "festival_layouts", f"{festival_key}.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _layout_template_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as f:
            template = json.load(f)
        if not isinstance(template, dict) or not isinstance(template.get("zones"), dict):
            template = None
    except (OSError, json.JSONDecodeError):
        logger.warning("festival layout template %s is unreadable - ignoring it", path)
        template = None
    _layout_template_cache[path] = (mtime, template)
    return template


def _zone_labels_for(festival_key: str | None) -> dict[str, str]:
    """{"Area N": "custom display label", ...} for the given festival - see
    festivals.json's optional "zoneLabels" field (a festival's physical
    zones aren't always called "Area N" - WFP's are floors, for instance).
    The stored zone identity (used for drag/drop, festival_map.py's own
    persistence, and _ZONE_NAME_RE matching) never changes - this is
    display-only, so the Mini App can show "1-й поверх" for "Area 1"
    without touching how zones are detected or persisted at all. Empty
    when the festival has none, or `festival_key` doesn't match a
    registry entry - the Mini App then just shows the raw zone name."""
    if not festival_key:
        return {}
    match = next((f for f in _load_festivals_registry() if f.get("key") == festival_key), None)
    return (match or {}).get("zoneLabels") or {}


def _festival_image_url(entry: dict) -> str | None:
    """Static URL of a festival's tile image for the Mini App's switcher, or
    None when it has none (the tile then falls back to a plain lettered
    square - see app.js's renderFestivalSwitchList). The registry's "image"
    is a bare file name inside webapp/festivals/, which the existing
    /static/checkin/ mount already serves; anything with a path separator is
    ignored rather than trusted, so a registry edit can't reach outside that
    folder.

    Version-busted with the same `_BUILD_VERSION` token as app.js/style.css
    (see handle_index) so _no_cache_middleware can safely cache this exact
    URL indefinitely - confirmed live that without it, the Mini App's "Мій
    фестиваль"/switcher tiles re-fetched every festival's picture from
    scratch on every single open (a visible blank tile each time), not just
    once after a deploy."""
    name = (entry.get("image") or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    if not os.path.exists(os.path.join(WEBAPP_DIR, "festivals", name)):
        return None
    return f"/static/checkin/festivals/{name}?v={_BUILD_VERSION}"


async def handle_festival_list(request: web.Request) -> web.Response:
    """Owner-only: the Mini App's festival-switcher screen reads this to
    show every festivals.json entry plus which one is currently loaded.
    Non-owners get a fixed empty/unavailable shape (same convention as
    handle_autotoast_status) - the settings row that would open this
    screen is never shown to them in the first place, this is defense in
    depth, not the primary access control."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    if not _is_auto_toast_owner(tg_user.get("id")):
        return web.json_response({"available": False, "festivals": [], "activeKey": None})
    festivals = [
        {"key": f["key"], "label": f.get("label", f["key"]), "imageUrl": _festival_image_url(f)}
        for f in _load_festivals_registry() if f.get("key")
    ]
    return web.json_response({"available": True, "festivals": festivals, "activeKey": _active_festival_key})


async def handle_festival_switch(request: web.Request) -> web.Response:
    """Owner-only: switches the DEFAULT active festival beer list - used by
    anyone with no active group, or whose group never bound its own
    festival via /set_festival (see group_festivals.py). A group that HAS
    bound one keeps seeing its own festival regardless of this switch.
    Live-reloads via bot.py's reload_beer_db (passed in as
    `_reload_beer_db_fn` - see start_webapp_server's own docstring) plus
    _set_festival_data, no process restart needed (mirrors badge_
    stats.reload_special_badges's pattern) - unavailable entirely in
    dev_server.py's dev_mode, where `_reload_beer_db_fn` stays None since
    there's no real bot.py module loaded to reload from."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    if not _is_auto_toast_owner(tg_user.get("id")):
        return _json_error("forbidden", 403)
    if _reload_beer_db_fn is None:
        return _json_error("not_available_in_dev_mode", 501)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("invalid_json")
    key = body.get("key")
    if not key:
        return _json_error("missing_key")
    result = _reload_beer_db_fn(key)
    if result is None:
        return _json_error("unknown_festival")
    new_beers, new_sessions_raw = result
    global _active_festival_key
    _active_festival_key = key
    _set_festival_data(new_beers, new_sessions_raw)
    logger.info("festival switch: now serving %r (%d beers)", key, len(new_beers))
    return web.json_response({"ok": True, "activeKey": key, "beerCount": len(new_beers)})


async def handle_my_festival_get(request: web.Request) -> web.Response:
    """Open to any authenticated user (NOT owner-gated, unlike
    handle_festival_list) - the Mini App's personal "Мій фестиваль" screen
    reads this to show every festivals.json entry, which one (if any) this
    user has personally overridden (user_festivals.py), and which one
    they'd actually see right now after the full resolution chain
    (personal override -> their group's binding -> the global default -
    see _resolve_festival_key/_festival_data_for)."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    personal_key = await user_festivals.get_user_festival(user_id)
    effective_key = await _resolve_festival_key(user_id) or _active_festival_key
    festivals = [
        {"key": f["key"], "label": f.get("label", f["key"]), "imageUrl": _festival_image_url(f)}
        for f in _load_festivals_registry() if f.get("key")
    ]
    return web.json_response({
        "festivals": festivals,
        "personalKey": personal_key,
        "effectiveKey": effective_key,
    })


async def handle_my_festival_set(request: web.Request) -> web.Response:
    """Sets (with a `key` from festivals.json) or clears (with `key: null`)
    the CALLER's own personal festival override - open to any authenticated
    user, no owner gate (unlike handle_festival_switch, which changes the
    shared default for everyone without their own override/group). Never
    touches group_festivals or the shared default."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("invalid_json")
    key = body.get("key")
    if key is None:
        await user_festivals.clear_user_festival(user_id)
        return web.json_response({"ok": True, "personalKey": None})
    if not isinstance(key, str) or not any(f.get("key") == key for f in _load_festivals_registry()):
        return _json_error("unknown_festival")
    await user_festivals.set_user_festival(user_id, key)
    return web.json_response({"ok": True, "personalKey": key})


async def handle_command_flags_get(request: web.Request) -> web.Response:
    """Owner-only: the Mini App's "Керування функціями" screen reads this
    to show every command a regular user could otherwise reach (see bot.py's
    TOGGLEABLE_COMMANDS - owner-only commands like /scan or /restart are
    deliberately not in that list, they need no separate toggle) plus the
    direct-photo-to-chat recognition flow, each with its current on/off
    state (feature_flags.py). Non-owners get a fixed empty/unavailable
    shape (same convention as handle_autotoast_status) - defense in depth,
    not the primary access control. Unavailable in dev_server.py's
    dev_mode too, where `_get_toggleable_commands_fn` stays None since
    there's no real bot.py command menu to control from there."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    if not _is_auto_toast_owner(tg_user.get("id")):
        return web.json_response({"available": False, "commands": [], "photoRecognition": True})
    if _get_toggleable_commands_fn is None:
        return web.json_response({"available": False, "commands": [], "photoRecognition": True})
    registry = _get_toggleable_commands_fn()
    flags = await feature_flags.get_all([cmd for cmd, _, _ in registry])
    by_command = {cmd: (label, desc) for cmd, label, desc in registry}
    commands = [
        {"command": cmd, "label": by_command[cmd][0], "description": by_command[cmd][1], "enabled": flags["commands"].get(cmd, True)}
        for cmd in flags["order"]
    ]
    return web.json_response({
        "available": True,
        "commands": commands,
        "photoRecognition": flags["photoRecognition"],
    })


async def handle_command_flags_set(request: web.Request) -> web.Response:
    """Owner-only: toggles one command (by name, must be in bot.py's
    TOGGLEABLE_COMMANDS) or the photo-recognition flow (`photoRecognition`
    instead of `command`) on/off for everyone except the owner themselves -
    see feature_flags.py and bot.py's _require_command_enabled/_is_owner.
    Takes effect on the very next message that command's handler
    processes; also re-applies the Telegram command-menu SUGGESTION list
    immediately (via `_refresh_command_menus_fn`, bot.py's own
    refresh_command_menus) for a `command` toggle, so a disabled command
    stops showing up in "/" autocomplete for other accounts right away
    instead of only after the bot's next restart - confirmed live
    confusing when this only updated the enforcement side."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    if not _is_auto_toast_owner(tg_user.get("id")):
        return _json_error("forbidden", 403)
    if _get_toggleable_commands_fn is None:
        return _json_error("not_available_in_dev_mode", 501)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("invalid_json")
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return _json_error("invalid_enabled")
    if "photoRecognition" in body:
        await feature_flags.set_enabled(feature_flags.PHOTO_RECOGNITION_KEY, enabled)
        return web.json_response({"ok": True, "photoRecognition": enabled})
    command = body.get("command")
    valid_commands = {cmd for cmd, _, _ in _get_toggleable_commands_fn()}
    if not isinstance(command, str) or command not in valid_commands:
        return _json_error("unknown_command")
    await feature_flags.set_enabled(command, enabled)
    if _refresh_command_menus_fn is not None:
        await _refresh_command_menus_fn(_ptb_app)
    return web.json_response({"ok": True, "command": command, "enabled": enabled})


async def handle_command_flags_reorder(request: web.Request) -> web.Response:
    """Owner-only: persists a custom display order for the "Керування
    функціями" screen's command list (drag-to-reorder), applied to both
    that screen's own row order AND the real Telegram command menu (see
    feature_flags.py's set_order, bot.py's _apply_order) - refreshed
    immediately via `_refresh_command_menus_fn`, same as
    handle_command_flags_set. `order` doesn't need to list every
    toggleable command - get_all() appends any missing one at the end
    next time it's read, so a client can send just what it has without
    needing to know the full registry."""
    init_data = await _require_valid_init_data(request)
    if not init_data:
        return _json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    if not _is_auto_toast_owner(tg_user.get("id")):
        return _json_error("forbidden", 403)
    if _get_toggleable_commands_fn is None:
        return _json_error("not_available_in_dev_mode", 501)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("invalid_json")
    order = body.get("order")
    valid_commands = {cmd for cmd, _, _ in _get_toggleable_commands_fn()}
    if not isinstance(order, list) or not order or not all(isinstance(c, str) and c in valid_commands for c in order):
        return _json_error("invalid_order")
    await feature_flags.set_order(order)
    if _refresh_command_menus_fn is not None:
        await _refresh_command_menus_fn(_ptb_app)
    return web.json_response({"ok": True, "order": order})


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


async def _pick_untappd_backend(user_id: int, token: str, min_remaining: int):
    """(client_module, token_to_use) to make the NEXT quota-costing call
    with this tick, or None if nothing currently has enough headroom (the
    caller should sleep and retry next tick, same as before this existed).

    Every connected user's own MCP token is tried first via the normal
    quota gate. Only when that's tapped out AND the caller is the
    recognized owner AND DIRECT_TOKEN is configured does this reach for
    untappd_direct's completely separate quota pool - never for any other
    user, per the explicit scoping decision behind this feature (a real
    Untappd API access_token requires an approved Untappd API application,
    which most connected users have no realistic way to obtain - see
    untappd_direct.py's own docstring).

    Deliberately a same-tick DECISION, not a retry-after-429: the codebase
    has a standing house rule (see every loop's own `except
    UntappdRateLimited: pass` - never auto-retry a rate limit) because
    Untappd's shared quota has been observed to stay throttled well past a
    few seconds of backoff. This sidesteps that entirely by checking
    headroom on BOTH pools *before* spending anything, exactly like the
    existing MCP-only gate already did - it just now has a second pool to
    check when the first is dry."""
    usage = await untappd_mcp.get_untappd_api_usage(token)  # free, no quota cost
    if _quota_allows(usage, min_remaining):
        return untappd_mcp, token
    if DIRECT_TOKEN and OWNER_TELEGRAM_ID and str(user_id) == OWNER_TELEGRAM_ID:
        direct_usage = untappd_direct.get_api_usage()  # free, no network call
        if _quota_allows(direct_usage, min_remaining):
            return untappd_direct, DIRECT_TOKEN
    return None


def _start_had_it_backfill() -> None:
    global _backfill_task
    if _backfill_task and not _backfill_task.done():
        return
    _backfill_task = asyncio.create_task(_had_it_backfill_loop())


async def _had_it_backfill_loop() -> None:
    """Paginates each connected user's Untappd check-in history into
    had_it_index.json, a handful of pages at a time, so the had-it badge
    eventually covers a user's entire history - not just what a live
    per-search check happens to ask about. Two kinds of pass, picked by
    had_it_index.next_turn: a full walk (initial, then once per calendar month,
    see resync_schedule) and a much cheaper daily
    "quick" recheck of just the first HAD_IT_QUICK_RECHECK_LIMIT beers
    (catches new check-ins/rating edits made in the real Untappd app,
    which always land at the front of get_user_beers' recency-sorted
    list - see next_turn's own docstring for why both passes still exist).
    Round-robins fairly across multiple connected users and backs off
    whenever the shared quota is getting tight, so live festival search/
    check-in traffic is never starved by this background job. Used to also
    be confined to a fixed 1h/day clock window ("quiet hours") on top of
    that - removed once it became clear the two protections weren't
    equivalent: the quota gate reacts to REAL headroom in real time, while
    the clock window blocked all progress for the other 23h/day regardless
    of how much quota sat unused (see _venue_backfill_loop's own note - same
    change, same reasoning, its own independent quota consumer)."""
    await asyncio.sleep(5)  # let the server finish binding first
    while True:
        try:
            user_ids = await user_tokens.list_user_ids()
            turn = (
                await had_it_index.next_turn(
                    user_ids, HAD_IT_QUICK_RECHECK_COOLDOWN_SECONDS
                )
                if user_ids else None
            )
            if turn is None:
                await asyncio.sleep(HAD_IT_BACKFILL_IDLE_SLEEP_SECONDS)
                continue
            user_id, offset, kind = turn
            if await festival_mode.is_enabled(user_id):
                await asyncio.sleep(HAD_IT_BACKFILL_INTERVAL_SECONDS)
                continue
            profile = await user_tokens.get_profile(user_id)
            if not profile or not profile.get("token") or not profile.get("username"):
                await asyncio.sleep(HAD_IT_BACKFILL_INTERVAL_SECONDS)
                continue
            token = profile["token"]

            backend = await _pick_untappd_backend(user_id, token, HAD_IT_BACKFILL_MIN_REMAINING)
            if backend is None:
                await asyncio.sleep(HAD_IT_BACKFILL_INTERVAL_SECONDS)
                continue
            client, active_token = backend

            page = await client.get_user_beers(
                active_token, profile["username"],
                limit=HAD_IT_BACKFILL_PAGE_SIZE, offset=offset,
            )  # no start/end date - the unfiltered walk verified stable, unlike date-filtering
            items = (page.get("beers") or {}).get("items", [])
            if kind == "quick":
                await had_it_index.record_quick_page(
                    user_id, profile["username"], items,
                    offset + len(items), HAD_IT_QUICK_RECHECK_LIMIT,
                )
            else:
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
            # would fail identically forever and wedge this pass for this
            # user. Skip past it (best-effort - up to one page's worth of
            # beers may be missed) rather than get stuck.
            logger.warning("had_it backfill: skipping unparseable %s page for user %s at offset %s: %s", kind, user_id, offset, e)
            await had_it_index.skip_page(user_id, offset + HAD_IT_BACKFILL_PAGE_SIZE, str(e), kind=kind)
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
    """Paginates each connected user's Untappd check-in history into
    venue_index.json (checkin_id-based paging via get_user_checkins), so
    "unique venue mode" can confidently tell a brand-new venue from one
    already visited, not just the ~100-250-checkin approximation
    get_my_recent_venues gives - and so badge_index.py's ground-truth badge
    levels (see handle_badges_get) stay reasonably fresh. Same two-kind
    full/quick split as _had_it_backfill_loop (see venue_index.next_turn),
    as its own independent quota consumer.

    Used to also only run inside a fixed 1h/day clock window ("quiet
    hours", BACKFILL_WINDOW_START/END) on top of the _quota_allows gate
    below - removed: proven live the two aren't equivalent safeguards. The
    quota gate already backs off in real time whenever headroom is
    actually tight, which is the thing that matters for not starving live
    festival traffic; the clock window on top of that just blocked ALL
    progress for the other 23h/day even when quota sat completely unused
    (e.g. overnight) - confirmed live to stretch a single full walk over a
    large, heavily-checked-in account (53k+ check-ins between two users)
    across many days, with badge discovery (badge_index.record, called
    from this same walk below) stalled the entire time."""
    await asyncio.sleep(5)  # let the server finish binding first
    while True:
        try:
            user_ids = await user_tokens.list_user_ids()
            turn = (
                await venue_index.next_turn(
                    user_ids, VENUE_QUICK_RECHECK_COOLDOWN_SECONDS
                )
                if user_ids else None
            )
            if turn is None:
                await asyncio.sleep(VENUE_BACKFILL_IDLE_SLEEP_SECONDS)
                continue
            user_id, max_id, kind = turn
            if await festival_mode.is_enabled(user_id):
                await asyncio.sleep(VENUE_BACKFILL_INTERVAL_SECONDS)
                continue
            profile = await user_tokens.get_profile(user_id)
            if not profile or not profile.get("token") or not profile.get("username"):
                await asyncio.sleep(VENUE_BACKFILL_INTERVAL_SECONDS)
                continue
            token = profile["token"]

            backend = await _pick_untappd_backend(user_id, token, VENUE_BACKFILL_MIN_REMAINING)
            if backend is None:
                await asyncio.sleep(VENUE_BACKFILL_INTERVAL_SECONDS)
                continue
            client, active_token = backend

            page = await client.get_user_checkins(
                active_token, profile["username"],
                limit=VENUE_BACKFILL_PAGE_SIZE, max_id=max_id,
            )
            items = (page.get("checkins") or {}).get("items", [])
            next_max_id = (page.get("pagination") or {}).get("max_id")
            if kind == "quick":
                await venue_index.record_quick_page(
                    user_id, profile["username"], items,
                    next_max_id, len(items), VENUE_QUICK_RECHECK_LIMIT,
                )
            else:
                await venue_index.record_page(
                    user_id, profile["username"], items,
                    next_max_id, len(items), VENUE_BACKFILL_PAGE_SIZE,
                )
            # Backfills name/style/brewery/country for beers had_it_index's
            # own offset-paginated walk keeps missing on active accounts
            # (see had_it_index.enrich_from_checkin's docstring for the
            # confirmed reordering bug) - free, from this same already-paid
            # page, via the checkin_id cursor's reordering-immune walk.
            for item in items:
                beer = item.get("beer") or {}
                bid = beer.get("bid")
                if bid is None:
                    continue
                brewery = item.get("brewery") or {}
                await had_it_index.enrich_from_checkin(
                    user_id, bid, beer.get("beer_style"),
                    brewery.get("brewery_name"), brewery.get("country_name"),
                    name=beer.get("beer_name"),
                    state=(brewery.get("location") or {}).get("brewery_state"),
                    abv=beer.get("beer_abv"), ibu=beer.get("beer_ibu"),
                    brewery_type=brewery.get("brewery_type"),
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
    Untappd's own definition. Runs even when the owner's watch point has
    resolved to a real venue_id and _festival_watch_venue_loop is polling it
    (which sees EVERYONE at that exact venue): venue/checkins only returns
    check-ins attributed to that one venue_id, so a friend logging the same
    pour at a neighbouring venue is invisible there but still caught here by
    radius. The two overlap on friends at the main venue - dedup by
    checkin_id lives in _notify_festival_novelty."""
    if not items:
        return
    watch = await festival_watch.get_config(owner_id)
    if not watch["enabled"] or watch["lat"] is None or watch["lng"] is None:
        return
    profile = await user_tokens.get_profile(owner_id)
    own_username_lower = (profile or {}).get("username", "").lower()
    candidates = []
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
        candidates.append(item)
    await _notify_festival_novelty(owner_id, candidates, watch.get("label"))


async def _notify_festival_novelty(owner_id: int, items: list[dict], venue_label_fallback: str | None) -> None:
    """Shared tail for both novelty sources (_check_festival_novelty's
    friends-only/radius path and _festival_watch_venue_loop's venue-checkins
    path) - `items` must already be exactly the candidates worth notifying
    about (own-username and radius/venue filtering both already done by the
    caller). A check-in both sources see is handled once (see
    _festival_novelty_first_sight). Per beer, in order:

    1. Already in the owner's active group's shared queue
       (_beer_already_queued) - skip entirely, the group already knows.
    2. Already pinged _FESTIVAL_NOVELTY_NOTIFY_MAX_PER_HOUR times this hour
       for this exact beer (_festival_novelty_notify_allowed) - skip, a
       busy tap shouldn't flood the feed just because many different
       people are checking the same thing in.
    3. On the festival's own list but not yet queued - a DIFFERENT message
       ("📋 ... in the festival's list, not in the queue yet") than genuine
       novelty, since it's not actually new to the static list, just to the
       group's tracked queue.
    4. Off the list entirely - the original "🆕 novelty" message, now also
       noting when had_it_index confirms the OWNER has already had this
       beer before (free, local lookup - no extra quota)."""
    if not items:
        return
    known_beer_ids = _all_festival_beer_ids()
    notify_listed = (await festival_watch.get_config(owner_id))["notifyListedBeers"]
    # The owner's last-seen language (set when they open the Mini App);
    # "uk" until they ever have, which is what these messages always were.
    lang = await user_tokens.get_language(owner_id) or "uk"
    venue_label_fallback = venue_label_fallback or i18n.t(lang, "nov_venue_fallback")
    for item in items:
        if not _festival_novelty_first_sight(owner_id, item.get("checkin_id")):
            continue  # already handled via the other novelty source
        username = (item.get("user") or {}).get("user_name") or "?"
        raw_bid = (item.get("beer") or {}).get("bid")
        bid = int(raw_bid) if raw_bid is not None else None

        if bid is not None and await _beer_already_queued(owner_id, bid):
            continue  # rule 1: already tracked in the shared queue
        if bid is not None and not _festival_novelty_notify_allowed(owner_id, bid):
            continue  # rule 4 (spam cap): already pinged enough this hour

        beer_name = (item.get("beer") or {}).get("beer_name") or "?"
        brewery_id = (item.get("brewery") or {}).get("brewery_id")
        brewery_name = (item.get("brewery") or {}).get("brewery_name") or "?"
        venue_id = (item.get("venue") or {}).get("venue_id")
        venue_name = (item.get("venue") or {}).get("venue_name") or venue_label_fallback

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

        on_list = bid is not None and bid in known_beer_ids
        reply_markup = None
        if on_list:
            if not notify_listed:
                continue  # opted out of "on the list, not queued yet" pings (see set_notify_listed)
            # rule 2: known festival beer, just not queued yet.
            text = i18n.t(
                lang, "nov_listed",
                profile=profile_link, beer=beer_link, brewery=brewery_link, venue=venue_link,
            )
            event_text = i18n.t(lang, "nov_event_listed", user=username, beer=beer_name, venue=venue_name)
        else:
            # rule 3: genuine novelty, off the static list entirely - note
            # when the OWNER (the one receiving this DM) has already had
            # this exact beer before, so they know not to rush for it.
            already_had = False
            if bid is not None:
                had = await had_it_index.lookup_had_it(owner_id, bid)
                already_had = bool(had and had.get("hadIt"))
            had_note = i18n.t(lang, "nov_had_yes" if already_had else "nov_had_no")
            text = i18n.t(
                lang, "nov_new",
                profile=profile_link, beer=beer_link, brewery=brewery_link, venue=venue_link, had=had_note,
            )
            event_text = i18n.t(lang, "nov_event_new", user=username, beer=beer_name, venue=venue_name) + (
                i18n.t(lang, "nov_event_had_suffix") if already_had else ""
            )

            # Even though THIS beer isn't in the festival's list, its credited
            # brewery (or whichever real stand it aliases to - see
            # _festival_brewery_aliases, e.g. a collab poured at a host's
            # stand) might already have a pin on the map from its OTHER
            # beers - worth a direct "Відкрити на карті" deep-link then,
            # same web_app button pattern as bot.py's /checkin (the only
            # one confirmed to carry real initData into the WebView).
            if _public_base_url:
                zone_hint = _festival_brewery_zone_map(_festival_beers)
                stand_brewery = _festival_brewery_aliases(_festival_beers).get(brewery_name, brewery_name)
                zone = zone_hint.get(stand_brewery)
                if zone:
                    map_url = (
                        f"{_public_base_url}/checkin"
                        f"?mapZone={quote(zone)}&mapBrewery={quote(stand_brewery)}"
                    )
                    reply_markup = InlineKeyboardMarkup(
                        [[InlineKeyboardButton(i18n.t(lang, "nov_open_on_map"), web_app=WebAppInfo(url=map_url))]]
                    )

        await event_log.add_event(
            owner_id, "novelty", event_text,
            beer_id=bid, checkin_id=item.get("checkin_id"), username=username,
        )
        try:
            await _ptb_bot.send_message(chat_id=owner_id, text=text, parse_mode="HTML", reply_markup=reply_markup)
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
    new from a watched target, unless its venue's country OR its brewery's
    own country is on that owner's exclusion list (checked separately - a
    virtual check-in like "Untappd at Home" has no venue country at all,
    so the brewery's own origin is often the only signal there is to
    exclude by).

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
            if await festival_mode.is_enabled(turn.owner_id):
                # peek doesn't auto-advance (see the comment above) - a
                # paused owner left un-advanced would just be re-peeked
                # forever, wedging every other owner behind them.
                await auto_toast.advance_owner_turn()
                await asyncio.sleep(AUTO_TOAST_INTERVAL_SECONDS)
                continue
            profile = await user_tokens.get_profile(turn.owner_id)
            if not profile or not profile.get("token"):
                # A genuinely unusable owner (not a transient condition) -
                # move on so it doesn't block anyone else behind it forever.
                await auto_toast.advance_owner_turn()
                await asyncio.sleep(AUTO_TOAST_INTERVAL_SECONDS)
                continue
            token = profile["token"]

            backend = await _pick_untappd_backend(turn.owner_id, token, AUTO_TOAST_MIN_REMAINING)
            if backend is None:
                # Transient - deliberately do NOT advance. The next loop
                # iteration re-peeks this same owner once quota allows it.
                await asyncio.sleep(AUTO_TOAST_INTERVAL_SECONDS)
                continue
            client, active_token = backend

            if turn.last_checkin_id is None:
                # Bootstrap: just note today's newest feed item as the
                # baseline, don't toast anything from before now.
                page = await client.get_my_friend_feed(active_token, limit=1)
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
            page = await client.get_my_friend_feed(
                active_token, limit=50, max_id=turn.catchup_max_id,
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
                # A virtual check-in ("Untappd at Home", or no venue at all)
                # has no venue_country to exclude by - but the BEER itself
                # still came from somewhere, and a country exclusion is
                # meant to catch that beer regardless of where it was
                # physically drunk (confirmed live: a Russian brewery's
                # check-in at "Untappd at Home" sailed straight through a
                # Russia exclusion that only ever looked at venue_country).
                # item["brewery"] sits alongside item["beer"], same shape
                # webapp_server's venue-backfill loop already reads
                # brewery.country_name from.
                brewery_country = (item.get("brewery") or {}).get("country_name")
                if auto_toast.is_country_excluded(venue_country, excluded):
                    continue
                if auto_toast.is_country_excluded(brewery_country, excluded):
                    continue
                try:
                    await client.toast_checkin(active_token, checkin_id)
                    toasted_by_username[username] = toasted_by_username.get(username, 0) + 1
                    # Deliberately NOT logged to event_log - a routine
                    # auto-toasted check-in is exactly the "just a friend's
                    # regular check-in" noise the "Останні події" screen is
                    # meant to rise above (per owner feedback); "comment"
                    # and "novelty" events stay logged since those are
                    # actually worth surfacing.
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
    lang = await user_tokens.get_language(owner_id) or "uk"
    text = i18n.t(lang, "cmt_notify", profile=profile_link, beer=beer_link, text=html.escape(comment_text))
    # callback_data is capped at 64 bytes by Telegram - a username could
    # easily push "commentreply:<id>:<username>" over that, so the
    # commenter goes in bot_data instead (same f"beer:{bid}" caching
    # convention bot.py already uses elsewhere), keyed by checkin_id.
    if _ptb_app is not None:
        _ptb_app.bot_data[f"comment_reply_to:{checkin_id}"] = commenter
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(i18n.t(lang, "cmt_reply_btn"), callback_data=f"commentreply:{checkin_id}")
    ]])
    await event_log.add_event(
        owner_id, "comment", f"{commenter}: {beer_name}",
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

                    backend = await _pick_untappd_backend(owner_id, token, COMMENT_WATCH_MIN_REMAINING)
                    if backend is None:
                        continue
                    client, active_token = backend

                    page = await client.get_user_checkins(
                        active_token, profile["username"], limit=COMMENT_WATCH_CHECK_LIMIT,
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


def _start_festival_watch_venue() -> None:
    global _festival_watch_venue_task
    if _festival_watch_venue_task and not _festival_watch_venue_task.done():
        return
    _festival_watch_venue_task = asyncio.create_task(_festival_watch_venue_loop())


async def _festival_watch_friends_owner() -> int | None:
    """The owner whose friends feed _festival_watch_venue_loop must poll
    itself (via DIRECT_TOKEN) because _check_festival_novelty's usual ride on
    _auto_toast_loop's feed isn't running for them - festival mode skips that
    loop entirely, and it also needs auto-toast enabled with a target. Only
    ever the bot owner: checkin/recent returns the TOKEN's OWN friends, and
    DIRECT_TOKEN is the owner's account (same restriction as _get_had_it's
    direct fallback). Also resets the poll's cursor whenever it isn't
    needed, so the first poll after it becomes needed again re-baselines
    instead of replaying whatever happened in between."""
    if not (DIRECT_TOKEN and OWNER_TELEGRAM_ID):
        return None
    owner_id = int(OWNER_TELEGRAM_ID)
    watch = await festival_watch.get_config(owner_id)
    if not watch["enabled"] or watch["lat"] is None or watch["lng"] is None:
        await festival_watch.record_friends_tick(owner_id, None)
        return None
    toast = await auto_toast.get_config(owner_id)
    auto_toast_covers_it = (
        toast["enabled"] and toast["targets"]
        and not await festival_mode.is_enabled(owner_id)
    )
    if auto_toast_covers_it:
        await festival_watch.record_friends_tick(owner_id, None)
        return None
    return owner_id


async def _poll_festival_friends(owner_id: int) -> None:
    """One direct friends-feed pass for _check_festival_novelty - same
    cursor/bootstrap/stale-min_id handling as the venue polls above."""
    min_id = (await festival_watch.get_config(owner_id))["friendsLastCheckinId"]
    try:
        page = await untappd_direct.get_my_friend_feed(DIRECT_TOKEN, limit=50, min_id=min_id)
    except untappd_mcp.UntappdMCPError as e:
        if min_id is None or "min_id" not in str(e).lower():
            raise
        page = await untappd_direct.get_my_friend_feed(DIRECT_TOKEN, limit=50)
        min_id = None
    items = (page.get("checkins") or {}).get("items", [])
    if not items:
        return
    newest_id = max((it.get("checkin_id") or 0) for it in items)
    if min_id is not None:
        await _check_festival_novelty(
            owner_id, [it for it in items if (it.get("checkin_id") or 0) > min_id],
        )
    await festival_watch.record_friends_tick(owner_id, newest_id)


async def _festival_watch_venue_loop() -> None:
    """Independent poll of venue/checkins/{venue_id} for every owner whose
    festival_watch point has resolved to a real Untappd venue (see
    festival_watch.set_venue) - sees EVERYONE checking in there, unlike
    _check_festival_novelty's friends-only feed piggyback (the whole reason
    untappd_direct.py and this loop exist - see README.md).

    Always uses DIRECT_TOKEN (untappd_direct's own separate quota pool) -
    never untappd_mcp, since the MCP server doesn't expose this endpoint at
    all. A no-op forever if DIRECT_TOKEN isn't configured (no owner's watch
    point can ever have resolved to a venueId in that case either - see
    handle_festival_watch_set_location - so list_venue_jobs() would just
    always come back empty).

    Only the MAIN venue is polled here (API quota); extra venues are scraped
    by _festival_watch_scrape_loop. The bot owner's friends
    feed is also polled here whenever _auto_toast_loop isn't covering it
    (see _festival_watch_friends_owner), so a friend logging a new beer at a
    different venue within the radius is still caught during festival mode.

    Cursor-based like auto_toast's own feed walk, but deliberately simpler:
    no multi-tick catchup walk for a page-cap overrun - a single venue's
    check-in volume between ticks is expected to be small (a handful of
    people at one place), unlike a whole friend feed. A tick that
    genuinely maxes out FESTIVAL_WATCH_VENUE_CHECK_LIMIT just catches the
    rest on the next tick from the same cursor, same as a normal
    lower-than-limit page would leave nothing missed - min_id already
    bounds the query so nothing in between is skipped either way."""
    await asyncio.sleep(5)  # let the server finish binding first
    while True:
        # Default floor - overwritten below once `targets` is known; stays
        # at the floor if an exception hits before that (the final sleep at
        # the bottom of the loop always needs a defined value to fall back
        # to, exception or not).
        tick_interval = FESTIVAL_WATCH_VENUE_INTERVAL_SECONDS
        try:
            if not DIRECT_TOKEN:
                await asyncio.sleep(FESTIVAL_WATCH_VENUE_IDLE_SLEEP_SECONDS)
                continue
            targets = await festival_watch.list_venue_jobs()
            friends_owner = await _festival_watch_friends_owner()
            if not targets and friends_owner is None:
                await asyncio.sleep(FESTIVAL_WATCH_VENUE_IDLE_SLEEP_SECONDS)
                continue
            # See FESTIVAL_WATCH_VENUE_BUDGET_PER_HOUR's own comment - spreads
            # the same total hourly call budget across however many targets
            # are currently enabled, instead of every target adding its own
            # fixed-interval load on top of the others. The direct friends-
            # feed poll costs one more call per pass.
            call_count = len(targets) + (1 if friends_owner is not None else 0)
            tick_interval = max(
                FESTIVAL_WATCH_VENUE_INTERVAL_SECONDS,
                3600 * call_count / FESTIVAL_WATCH_VENUE_BUDGET_PER_HOUR,
            )

            usage = untappd_direct.get_api_usage()  # free, no network call
            if not _quota_allows(usage, FESTIVAL_WATCH_VENUE_MIN_REMAINING):
                await asyncio.sleep(tick_interval)
                continue

            for target in targets:
                owner_id = target["ownerId"]
                try:
                    # Deliberately NOT gated on festival_mode.is_enabled, unlike
                    # every other DIRECT_TOKEN consumer (had_it/venue backfill,
                    # auto_toast) - festival mode being ON is exactly when this
                    # venue-novelty monitoring should be running, not paused.
                    # It always spends DIRECT_TOKEN's own separate quota pool
                    # (see this loop's own docstring), never the owner's own
                    # MCP-token quota, so it never competes with their live
                    # search/check-in traffic either way. Pausing it here used
                    # to mean the one loop festival mode should be FUNDING
                    # (freeing up DIRECT_TOKEN headroom by pausing the other
                    # consumers) was itself the first thing turned off.
                    min_id = target["lastCheckinId"]
                    try:
                        page = await untappd_direct.get_venue_checkins(
                            DIRECT_TOKEN, target["venueId"],
                            limit=FESTIVAL_WATCH_VENUE_CHECK_LIMIT, min_id=min_id,
                        )
                    except untappd_mcp.UntappdMCPError as e:
                        # Confirmed live: Untappd rejects a min_id pointing
                        # further back than ~10 days for this endpoint
                        # ("Your 'min_id' ... must be greater than 10 days
                        # from now") - happens once this venue goes 10+
                        # days without a new check-in, and would otherwise
                        # repeat this exact error on EVERY tick forever
                        # (the cursor never advances on a raised exception).
                        # Nothing in that >10-day gap is worth notifying
                        # about by the time we'd ever see it anyway, so
                        # treat it exactly like a fresh bootstrap instead:
                        # re-baseline the cursor to "now" rather than
                        # retrying the same doomed min_id forever.
                        if min_id is None or "min_id" not in str(e).lower():
                            raise
                        page = await untappd_direct.get_venue_checkins(
                            DIRECT_TOKEN, target["venueId"], limit=FESTIVAL_WATCH_VENUE_CHECK_LIMIT,
                        )
                        min_id = None
                    items = (page.get("checkins") or {}).get("items", [])
                    if not items:
                        continue
                    newest_id = max((it.get("checkin_id") or 0) for it in items)

                    if min_id is None:
                        # Bootstrap (first-ever watch, or a just-reset
                        # stale cursor): note the newest id as the new
                        # baseline, don't notify about anything older.
                        await festival_watch.record_venue_tick(owner_id, newest_id)
                        continue

                    profile = await user_tokens.get_profile(owner_id)
                    own_username_lower = (profile or {}).get("username", "").lower()
                    candidates = [
                        it for it in items
                        if ((it.get("user") or {}).get("user_name") or "").lower() != own_username_lower
                    ]
                    await _notify_festival_novelty(owner_id, candidates, target["venueName"])
                    await festival_watch.record_venue_tick(owner_id, newest_id)
                except untappd_mcp.UntappdRateLimited:
                    break  # this tick's shared DIRECT_TOKEN quota is spent - skip remaining targets, retry next tick
                except untappd_mcp.UntappdMCPError as e:
                    logger.warning("festival_watch venue tick failed for owner %s: %s", owner_id, e)
                except Exception:
                    logger.exception("festival_watch venue tick failed for owner %s", owner_id)
            if friends_owner is not None:
                try:
                    await _poll_festival_friends(friends_owner)
                except untappd_mcp.UntappdRateLimited:
                    pass  # retry next pass, same as the venue polls
                except untappd_mcp.UntappdMCPError as e:
                    logger.warning("festival_watch friends poll failed for owner %s: %s", friends_owner, e)
                except Exception:
                    logger.exception("festival_watch friends poll failed for owner %s", friends_owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("festival_watch venue loop tick failed")
        await asyncio.sleep(tick_interval)


def _start_festival_watch_scrape() -> None:
    global _festival_watch_scrape_task
    if _festival_watch_scrape_task and not _festival_watch_scrape_task.done():
        return
    _festival_watch_scrape_task = asyncio.create_task(_festival_watch_scrape_loop())


async def _festival_watch_scrape_loop() -> None:
    """Scrapes every owner's extra venues (festival_watch.add_extra_venue)
    via venue_scrape - logged-out public activity pages, so no Untappd API
    quota and no DIRECT_TOKEN needed, which is why the list can be long
    (unlike the quota-bound main venue in _festival_watch_venue_loop).
    Same cursor/bootstrap idea as that loop: the first pass over a venue only
    records the newest check-in id, later passes notify about newer ones via
    the shared _notify_festival_novelty (deduped by checkin_id).

    Fragile by nature - Cloudflare can start challenging this server at any
    time. A block backs the whole loop off exponentially (up to
    FESTIVAL_WATCH_SCRAPE_MAX_BACKOFF_SECONDS) instead of retrying hard, and
    resets after the next clean pass. A single venue failing otherwise
    (layout change, venue has no stream) is just logged and skipped."""
    await asyncio.sleep(8)  # let the server finish binding first
    backoff = 0.0
    while True:
        interval = FESTIVAL_WATCH_SCRAPE_INTERVAL_SECONDS
        try:
            jobs = await festival_watch.list_extra_venue_jobs()
            if not jobs:
                await asyncio.sleep(FESTIVAL_WATCH_VENUE_IDLE_SLEEP_SECONDS)
                continue
            blocked = False
            fetched: dict[int, list[dict]] = {}  # two owners sharing a venue = one request
            for job in jobs:
                try:
                    await _scrape_extra_venue(job, fetched)
                except venue_scrape.ScrapeBlocked as e:
                    logger.warning("festival_watch scrape blocked (%s) - backing off", e)
                    blocked = True
                    break
                except venue_scrape.ScrapeError as e:
                    logger.warning("festival_watch scrape of venue %s failed: %s", job["venueId"], e)
                except Exception:
                    logger.exception("festival_watch scrape of venue %s failed", job["venueId"])
                await asyncio.sleep(FESTIVAL_WATCH_SCRAPE_GAP_SECONDS)
            if blocked:
                backoff = min(max(backoff * 2, interval), FESTIVAL_WATCH_SCRAPE_MAX_BACKOFF_SECONDS)
                interval = backoff
            else:
                backoff = 0.0
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("festival_watch scrape loop tick failed")
        await asyncio.sleep(interval)


async def _scrape_extra_venue(job: dict, fetched: dict[int, list[dict]]) -> None:
    owner_id, venue_id, cursor = job["ownerId"], job["venueId"], job["lastCheckinId"]
    if venue_id not in fetched:
        fetched[venue_id] = await venue_scrape.fetch_venue_activity(venue_id)
    items = fetched[venue_id]
    if not items:
        return
    newest_id = max(it["checkin_id"] for it in items)
    if cursor is not None:
        profile = await user_tokens.get_profile(owner_id)
        own_username_lower = (profile or {}).get("username", "").lower()
        candidates = [
            it for it in items
            if it["checkin_id"] > cursor and it["user"]["user_name"].lower() != own_username_lower
        ]
        await _notify_festival_novelty(owner_id, candidates, job["venueName"])
    await festival_watch.record_extra_venue_tick(owner_id, venue_id, max(newest_id, cursor or 0))


# Daily crawl of the lens-supported shops into lens_log (see shop_crawl.py).
SHOP_CRAWL_INTERVAL_SECONDS = float(os.environ.get("SHOP_CRAWL_INTERVAL_SECONDS", str(24 * 3600)))
SHOP_CRAWL_STARTUP_DELAY_SECONDS = float(os.environ.get("SHOP_CRAWL_STARTUP_DELAY_SECONDS", "600"))
SHOP_CRAWL_MAX_RESOLVES = int(os.environ.get("SHOP_CRAWL_MAX_RESOLVES", "600"))  # per run; the rest waits for the next one
SHOP_CRAWL_BATCH_SIZE = 50
_shop_crawl_task = None
_shop_crawl_manual_task = None  # keeps the create_task result alive (avoid GC)
_shop_crawl_running = False


def _start_shop_crawl() -> None:
    global _shop_crawl_task
    if _shop_crawl_task and not _shop_crawl_task.done():
        return
    _shop_crawl_task = asyncio.create_task(_shop_crawl_loop())


async def _shop_crawl_loop() -> None:
    await asyncio.sleep(SHOP_CRAWL_STARTUP_DELAY_SECONDS)
    while True:
        try:
            wait = shop_crawl.load_state().get("lastRun", 0) + SHOP_CRAWL_INTERVAL_SECONDS - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            await _run_shop_crawl()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("shop crawl failed")
            await asyncio.sleep(3600)  # retry in an hour, never hot-loop on a persistent failure


async def _crawl_resolve_one(token: str, owner_id: int, item: dict, sem: asyncio.Semaphore, stop: asyncio.Event):
    """One crawled product through the same resolver the lens uses; None when
    the run already hit a rate limit (left for the next run)."""
    if stop.is_set():
        return None
    async with sem:
        try:
            return await beer_match.resolve_beer(
                token, owner_id, item["name"], item["brewery"], need_country=False, live_fallback=False,
            )
        except untappd_mcp.UntappdRateLimited:
            stop.set()
            return None
        except untappd_mcp.UntappdMCPError as exc:
            logger.warning("shop crawl: lookup failed for %r %r: %s", item["brewery"], item["name"], exc)
        except Exception:
            logger.exception("shop crawl: unexpected error for %r %r", item["brewery"], item["name"])
        return {"matched": False, "candidates": [], "error": True}


async def _run_shop_crawl() -> dict:
    """Crawls every supported shop, then resolves the products worth a look
    (unmatched, new, or stale - see lens_log.due_for_crawl), at most
    SHOP_CRAWL_MAX_RESOLVES per run, recording each outcome in lens_log.
    Costs no tokens and no Untappd API quota (search_beers is the free search
    index, same as the lens)."""
    global _shop_crawl_running
    if _shop_crawl_running:
        return {}
    _shop_crawl_running = True
    state = {"lastRun": int(time.time()), "running": True, "shops": {}}
    shop_crawl.save_state(state)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
            get_text, get_json = shop_crawl.make_http_getters(client)
            crawl = await shop_crawl.crawl_shops(get_text, get_json)
        state["shops"] = shop_crawl.summarize(crawl)
        items = shop_crawl.dedupe([it for r in crawl.values() for it in r["items"]])
        state["products"] = len(items)

        owner_id = int(AUTO_TOAST_OWNER_ID) if AUTO_TOAST_OWNER_ID else None
        token = await user_tokens.get_token(owner_id) if owner_id else None
        resolved = 0
        todo: list[dict] = []
        if token:
            due = await lens_log.due_for_crawl(items)
            todo = due[:SHOP_CRAWL_MAX_RESOLVES]
            state["deferred"] = len(due) - len(todo)
            sem, stop = asyncio.Semaphore(2), asyncio.Event()
            for i in range(0, len(todo), SHOP_CRAWL_BATCH_SIZE):
                batch = todo[i:i + SHOP_CRAWL_BATCH_SIZE]
                results = await asyncio.gather(*(_crawl_resolve_one(token, owner_id, it, sem, stop) for it in batch))
                done = [(it, r) for it, r in zip(batch, results) if r is not None]
                if done:
                    await lens_log.record([d[0] for d in done], [d[1] for d in done], source="crawl")
                    resolved += len(done)
                if stop.is_set():
                    state["stoppedEarly"] = "rate_limited"
                    break
                await asyncio.sleep(1)
        else:
            state["skipped"] = "no owner Untappd token - products listed but not resolved"
        todo_keys = {(it["brewery"].lower(), it["name"].lower()) for it in todo}
        await lens_log.mark_crawled([it for it in items if (it["brewery"].lower(), it["name"].lower()) not in todo_keys])
        state["resolved"] = resolved
        logger.info("shop crawl: %d products, %d resolved, shops=%s", len(items), resolved, state["shops"])
    finally:
        state["running"] = False
        state["finishedAt"] = int(time.time())
        shop_crawl.save_state(state)
        _shop_crawl_running = False
    return state


def _start_badge_index_sync() -> None:
    global _badge_index_sync_task
    if _badge_index_sync_task and not _badge_index_sync_task.done():
        return
    _badge_index_sync_task = asyncio.create_task(_badge_index_sync_loop())


async def _badge_index_sync_loop() -> None:
    """Paginates GET /v4/user/badges/{username} (untappd_direct.
    get_user_badges) for every connected user into badge_index.json - see
    that function's own docstring for why this is a strictly better ground
    truth than badge_index.py's original check-in-scavenging design (no lag,
    no missed badges, always-current user_badge_id for personalUrl).

    Always DIRECT_TOKEN (never a user's own MCP token) - confirmed live
    this is a public endpoint like venue/checkins, so one token serves
    every connected user's badge list, not just the owner's. A no-op
    forever if DIRECT_TOKEN isn't configured, same guard as
    _festival_watch_venue_loop."""
    await asyncio.sleep(5)  # let the server finish binding first
    while True:
        try:
            if not DIRECT_TOKEN:
                await asyncio.sleep(BADGE_INDEX_SYNC_IDLE_SLEEP_SECONDS)
                continue
            user_ids = await user_tokens.list_user_ids()
            turn = (
                await badge_index.next_sync_turn(user_ids, BADGE_INDEX_SYNC_RESYNC_COOLDOWN_SECONDS)
                if user_ids else None
            )
            if turn is None:
                await asyncio.sleep(BADGE_INDEX_SYNC_IDLE_SLEEP_SECONDS)
                continue
            user_id, offset = turn
            profile = await user_tokens.get_profile(user_id)
            if not profile or not profile.get("username"):
                await asyncio.sleep(BADGE_INDEX_SYNC_INTERVAL_SECONDS)
                continue

            usage = untappd_direct.get_api_usage()  # free, no network call
            if not _quota_allows(usage, BADGE_INDEX_SYNC_MIN_REMAINING):
                await asyncio.sleep(BADGE_INDEX_SYNC_INTERVAL_SECONDS)
                continue

            page = await untappd_direct.get_user_badges(
                DIRECT_TOKEN, profile["username"],
                limit=BADGE_INDEX_SYNC_PAGE_SIZE, offset=offset,
            )
            items = page.get("items", [])
            for item in items:
                badge_name = item.get("badge_name")
                user_badge_id = item.get("user_badge_id")
                if badge_name and user_badge_id:
                    await badge_index.record(user_id, badge_name, user_badge_id)
            await badge_index.record_sync_page(
                user_id, offset + len(items), len(items), BADGE_INDEX_SYNC_PAGE_SIZE,
            )
        except asyncio.CancelledError:
            raise
        except untappd_mcp.UntappdRateLimited:
            pass  # skip to next tick - never auto-retry, per house rule
        except untappd_mcp.UntappdMCPError as e:
            logger.warning("badge_index sync tick failed: %s", e)
        except Exception:
            logger.exception("badge_index sync loop tick failed")
        await asyncio.sleep(BADGE_INDEX_SYNC_INTERVAL_SECONDS)


_SPECIAL_BADGES_LOCAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "special_badges.json")
_SPECIAL_BADGES_PENDING_LOCAL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "special_badges_pending_review.json",
)


def _start_special_badges_sync() -> None:
    global _special_badges_sync_task
    if _special_badges_sync_task and not _special_badges_sync_task.done():
        return
    _special_badges_sync_task = asyncio.create_task(_special_badges_sync_loop())


def _read_local_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: str, data) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


async def _fetch_github_raw_json(session: aiohttp.ClientSession, filename: str):
    """None if the file is missing on GitHub (special_badges_pending_review.
    json may not exist until the cloud routine first creates one) or the
    fetch/parse fails for any other reason - callers treat None as "no
    change to report", never as "delete the local file"."""
    url = f"{SPECIAL_BADGES_RAW_BASE_URL}/{filename}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
        return json.loads(text)
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("special_badges sync: failed to fetch %s: %s", filename, e)
        return None


async def _special_badges_sync_loop() -> None:
    """Local half of the special-badges bridge (see the comment above
    SPECIAL_BADGES_SYNC_INTERVAL_SECONDS): a separate cloud Claude Code
    routine reads untappd.com/blog daily and commits catalog updates
    straight to this repo's GitHub master branch (this server's own
    network calls can't reach the blog - Cloudflare-blocked even via plain
    aiohttp, confirmed live) - this loop just pulls whatever it committed,
    writes it locally, live-reloads badge_stats' in-memory copy, and tells
    the owner what changed. Silent on a no-op tick, per the owner's own
    "don't ping me for nothing" preference used elsewhere in this file."""
    await asyncio.sleep(5)  # let the server finish binding first
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                remote_catalog = await _fetch_github_raw_json(session, "special_badges.json")
                remote_pending = await _fetch_github_raw_json(session, "special_badges_pending_review.json")

            lines: list[str] = []

            if remote_catalog is not None and isinstance(remote_catalog.get("special_badges"), list):
                local_catalog = _read_local_json(_SPECIAL_BADGES_LOCAL_PATH) or {}
                if remote_catalog != local_catalog:
                    old_names = {b.get("badge") for b in local_catalog.get("special_badges", [])}
                    new_badges = remote_catalog.get("special_badges", [])
                    new_names = {b.get("badge") for b in new_badges}
                    added = sorted(n for n in (new_names - old_names) if n)
                    removed = sorted(n for n in (old_names - new_names) if n)
                    _write_json_atomic(_SPECIAL_BADGES_LOCAL_PATH, remote_catalog)
                    badge_stats.reload_special_badges()
                    if added:
                        lines.append("🆕 Нові спеціальні бейджі:")
                        by_name = {b.get("badge"): b for b in new_badges}
                        for name in added:
                            src = (by_name.get(name) or {}).get("sourceUrl")
                            label = html.escape(name)
                            lines.append(f"• <a href=\"{html.escape(src)}\">{label}</a>" if src else f"• {label}")
                    if removed:
                        lines.append("🗑 Прибрано (термін дії сплив):")
                        lines.extend(f"• {html.escape(n)}" for n in removed)

            if remote_pending is not None and isinstance(remote_pending, list):
                local_pending = _read_local_json(_SPECIAL_BADGES_PENDING_LOCAL_PATH) or []
                if remote_pending != local_pending:
                    old_urls = {e.get("url") for e in local_pending}
                    added_pending = [e for e in remote_pending if e.get("url") not in old_urls]
                    _write_json_atomic(_SPECIAL_BADGES_PENDING_LOCAL_PATH, remote_pending)
                    if added_pending:
                        lines.append("❓ Знайдено, потребує ручної перевірки:")
                        for entry in added_pending:
                            url = entry.get("url") or ""
                            note = html.escape(entry.get("note") or "")
                            link = f"<a href=\"{html.escape(url)}\">{html.escape(url)}</a>" if url else "?"
                            lines.append(f"• {link} — {note}")

            if lines and _ptb_bot:
                text = "Оновлення каталогу спеціальних бейджів (синхронізовано з GitHub):\n\n" + "\n".join(lines)
                try:
                    await _ptb_bot.send_message(
                        chat_id=int(AUTO_TOAST_OWNER_ID), text=text,
                        parse_mode="HTML", disable_web_page_preview=True,
                    )
                except Exception:
                    logger.exception("special_badges sync: failed to notify owner")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("special_badges sync loop tick failed")
        await asyncio.sleep(SPECIAL_BADGES_SYNC_INTERVAL_SECONDS)
