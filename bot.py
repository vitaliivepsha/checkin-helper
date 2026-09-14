import os
from dotenv import load_dotenv
load_dotenv()  # must run before any local module that reads env vars at import time (e.g. untappd_mcp._MCP_URL)

import shutil
import json
import csv
import io
import re
import time
import unicodedata
import base64
import html
import logging
import asyncio
from collections import defaultdict
from datetime import datetime
import anthropic
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, WebAppInfo
from telegram import BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.error import TelegramError, TimedOut, NetworkError
from search import find_beers_in_db, find_beer_candidates, search_untappd_web
from i18n import t
import had_it_index
import untappd_mcp
import user_tokens
import auto_toast
import festival_watch
import comment_watch

# ── Persistent data directory ────────────────────────────────────────────────
# Most container hosts have an ephemeral root filesystem. Mount a persistent
# volume/disk at some path and set BOT_DATA_DIR (or DATA_DIR) to it so mutable
# JSON state survives redeploys.
DATA_DIR = os.path.abspath(os.getenv("BOT_DATA_DIR") or os.getenv("DATA_DIR") or ".")
os.makedirs(DATA_DIR, exist_ok=True)

# Auto-toast is still a personal test feature, not something every connected
# account should get - restricted to this single Telegram user_id (defaults
# to the actual owner so it works with zero extra config; override via env
# if the owner's account ever changes).
AUTO_TOAST_OWNER_ID = os.environ.get("AUTO_TOAST_OWNER_ID", "402733193")
user_tokens.init(DATA_DIR)
# Also (idempotently) init'd by webapp_server.start_webapp_server, but that
# only runs when the Mini App is enabled (UNTAPPD_MCP_URL + PUBLIC_BASE_URL
# set) - /import_history below needs had_it_index regardless of whether the
# Mini App itself is running.
had_it_index.init(DATA_DIR)
auto_toast.init(DATA_DIR)  # same reasoning - /auto_toast must work regardless of the Mini App
festival_watch.init(DATA_DIR)  # same - /festival_watch's config commands must work regardless of the Mini App
comment_watch.init(DATA_DIR)  # same - /comment_watch's on/off command must work regardless of the Mini App

# Public HTTPS base URL this bot is reachable at (the deployed app's own URL)
# - needed to build the Telegram Mini App link for the festival check-in webapp.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")
_webapp_server_task = None  # keeps the asyncio.create_task result alive (avoid GC)


def data_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


def migrate_legacy_data_file(filename: str):
    """Move/copy existing local state into DATA_DIR on first run with a volume."""
    old_path = os.path.abspath(filename)
    new_path = os.path.abspath(data_path(filename))
    if old_path == new_path or os.path.exists(new_path) or not os.path.exists(old_path):
        return
    try:
        shutil.copy2(old_path, new_path)
        print(f"Migrated {filename} -> {new_path}")
    except Exception as e:
        print(f"Could not migrate {filename} -> {new_path}: {e}")


# Status module (comment out to disable)
from status import register_status_handlers, STATUS_COMMANDS
from limited import register_limited_handlers, start_limited_background_tasks, LIMITED_COMMANDS

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(data_path("bot_log.txt"), encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

BOT_BUILD = "todo-brewery-first-v16"

CHECKINS_FILE = data_path("checkins.json")
PINNED_FILE = data_path("pinned.json")
for _state_file in ("checkins.json", "pinned.json"):
    migrate_legacy_data_file(_state_file)
PAGE_SIZE = 10
MEDIA_GROUP_DELAY_SECONDS = 2.5


def h(text) -> str:
    return html.escape(str(text or ""), quote=False)


def expand_search_abbreviations(text: str) -> str:
    """Expand common beer abbreviations for manual DB search.

    OCR and users often type names like "Double BA Spelt Dads", while the
    festival DB may store the title as "Double Barrel Aged ...". Keep this
    limited to whole-word replacements so normal words are not changed.
    """
    if not text:
        return ""

    replacements = [
        (r"\bBBA\b", "Bourbon Barrel Aged"),
        (r"\bBA\b", "Barrel Aged"),
        (r"\bDBA\b", "Double Barrel Aged"),
        (r"\bTBA\b", "Triple Barrel Aged"),
    ]
    expanded = str(text)
    for pattern, repl in replacements:
        expanded = re.sub(pattern, repl, expanded, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", expanded).strip()


# ── Language ──────────────────────────────────────────────────────────────────

def lang(update: Update) -> str:
    code = getattr(update.effective_user, "language_code", None) or "en"
    return code[:2].lower()

def lang_from_code(code: str) -> str:
    return (code or "en")[:2].lower()


# ── Pending state — keyed by (user_id, chat_id) ───────────────────────────────

def _pk(user_id, chat_id) -> str:
    return f"{user_id}:{chat_id}"

def get_pending(context, user_id, chat_id) -> dict | None:
    return context.user_data.get(f"pending_search:{_pk(user_id, chat_id)}")

def set_pending(context, user_id, chat_id, data: dict):
    context.user_data[f"pending_search:{_pk(user_id, chat_id)}"] = data

def clear_pending(context, user_id, chat_id):
    context.user_data.pop(f"pending_search:{_pk(user_id, chat_id)}", None)


def get_pending_ocr_edit(context, user_id, chat_id) -> dict | None:
    return context.user_data.get(f"pending_ocr_edit:{_pk(user_id, chat_id)}")


def set_pending_ocr_edit(context, user_id, chat_id, data: dict):
    context.user_data[f"pending_ocr_edit:{_pk(user_id, chat_id)}"] = data


def clear_pending_ocr_edit(context, user_id, chat_id):
    context.user_data.pop(f"pending_ocr_edit:{_pk(user_id, chat_id)}", None)


def get_pending_untappd_token(context, user_id, chat_id) -> dict | None:
    return context.user_data.get(f"pending_untappd_token:{_pk(user_id, chat_id)}")


def set_pending_untappd_token(context, user_id, chat_id, data: dict):
    context.user_data[f"pending_untappd_token:{_pk(user_id, chat_id)}"] = data


def clear_pending_untappd_token(context, user_id, chat_id):
    context.user_data.pop(f"pending_untappd_token:{_pk(user_id, chat_id)}", None)


def get_pending_festival_watch_location(context, user_id, chat_id) -> dict | None:
    return context.user_data.get(f"pending_festival_watch_location:{_pk(user_id, chat_id)}")


def set_pending_festival_watch_location(context, user_id, chat_id, data: dict):
    context.user_data[f"pending_festival_watch_location:{_pk(user_id, chat_id)}"] = data


def clear_pending_festival_watch_location(context, user_id, chat_id):
    context.user_data.pop(f"pending_festival_watch_location:{_pk(user_id, chat_id)}", None)


def get_pending_comment_reply(context, user_id, chat_id) -> dict | None:
    return context.user_data.get(f"pending_comment_reply:{_pk(user_id, chat_id)}")


def set_pending_comment_reply(context, user_id, chat_id, data: dict):
    context.user_data[f"pending_comment_reply:{_pk(user_id, chat_id)}"] = data


def clear_pending_comment_reply(context, user_id, chat_id):
    context.user_data.pop(f"pending_comment_reply:{_pk(user_id, chat_id)}", None)


def get_pending_import_history(context, user_id, chat_id) -> dict | None:
    return context.user_data.get(f"pending_import_history:{_pk(user_id, chat_id)}")


def set_pending_import_history(context, user_id, chat_id, data: dict):
    context.user_data[f"pending_import_history:{_pk(user_id, chat_id)}"] = data


def clear_pending_import_history(context, user_id, chat_id):
    context.user_data.pop(f"pending_import_history:{_pk(user_id, chat_id)}", None)

def get_pending_find_data(context, user_id, chat_id) -> dict | None:
    """Return pending /find state.

    Older bot versions stored this as a plain boolean; keep supporting that
    shape so pending state from an already-running process does not break.
    """
    pending = context.user_data.get(f"pending_find:{_pk(user_id, chat_id)}")
    if not pending:
        return None
    if isinstance(pending, dict):
        return pending
    return {"active": True}

def get_pending_find(context, user_id, chat_id) -> bool:
    return bool(get_pending_find_data(context, user_id, chat_id))

def set_pending_find(context, user_id, chat_id, val: bool, prompt_msg_id: int | None = None):
    key = f"pending_find:{_pk(user_id, chat_id)}"
    if val:
        context.user_data[key] = {
            "active": True,
            "prompt_chat_id": chat_id,
            "prompt_msg_id": prompt_msg_id,
        }
    else:
        context.user_data.pop(key, None)


def _looks_like_find_prompt(text: str | None) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if not normalized:
        return False

    known_prompts = {
        t("en", "find_prompt_brewery").lower(),
        t("uk", "find_prompt_brewery").lower(),
    }

    return (
        normalized in known_prompts
        or "type brewery name" in normalized
        or "введи назву пивоварні" in normalized
    )


def is_stale_find_prompt_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle ForceReply prompts that were sent by an older bot version.

    Telegram clients can keep a ForceReply composer open even after the bot was
    redeployed. If the user sends that reply, process it as the /find answer
    even when in-memory pending state was lost.
    """
    msg = update.effective_message
    reply = getattr(msg, "reply_to_message", None)
    if not reply or not _looks_like_find_prompt(getattr(reply, "text", None)):
        return False

    reply_from = getattr(reply, "from_user", None)
    bot_id = getattr(context.bot, "id", None)

    if bot_id is not None and reply_from is not None:
        return reply_from.id == bot_id

    return bool(getattr(reply_from, "is_bot", False))


async def delete_message_if_possible(context, chat_id: int | None, message_id: int | None):
    if not chat_id or not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


async def cleanup_pending_search_messages(
    context,
    pending: dict | None,
    default_chat_id: int | None = None,
):
    """Delete temporary manual-search messages.

    The bot sends a prompt ("type beer/brewery name"), the user sends one
    or more manual queries, and the bot may send temporary "nothing found"
    hints. Once a beer is picked, those messages only clutter the chat.
    Delete what we can; Telegram may refuse deletion in groups where the bot
    is not admin, so all failures are intentionally ignored.
    """
    if not pending:
        return

    prompt_chat_id = pending.get("prompt_chat_id", default_chat_id)
    await delete_message_if_possible(
        context,
        prompt_chat_id,
        pending.get("prompt_msg_id"),
    )

    query_chat_id = pending.get("query_chat_id", prompt_chat_id or default_chat_id)
    for message_id in pending.get("user_query_msg_ids", []) or []:
        await delete_message_if_possible(context, query_chat_id, message_id)

    feedback_chat_id = pending.get("feedback_chat_id", query_chat_id)
    for message_id in pending.get("feedback_msg_ids", []) or []:
        await delete_message_if_possible(context, feedback_chat_id, message_id)

    pick_chat_id = pending.get("pick_chat_id", feedback_chat_id)
    for message_id in pending.get("pick_msg_ids", []) or []:
        await delete_message_if_possible(context, pick_chat_id, message_id)


async def begin_manual_db_search(
    context,
    user_id: int,
    chat_id: int,
    pending_data: dict,
    lng: str,
):
    """Start a manual DB search and remember messages to clean later.

    There is only one pending manual search per user/chat. If the user taps
    "Search in database" on a second card before finishing the previous one,
    clean the old prompt first so multiple stale prompts do not pile up.
    """
    old_pending = get_pending(context, user_id, chat_id)
    if old_pending:
        await cleanup_pending_search_messages(context, old_pending, chat_id)
        clear_pending(context, user_id, chat_id)

    prompt = await context.bot.send_message(
        chat_id=chat_id,
        text=t(lng, "search_prompt"),
        parse_mode="Markdown",
    )

    pending_data.update({
        "prompt_chat_id": chat_id,
        "prompt_msg_id": prompt.message_id,
        "query_chat_id": chat_id,
        "feedback_chat_id": chat_id,
        "user_query_msg_ids": [],
        "feedback_msg_ids": [],
        "pick_msg_ids": [],
    })
    set_pending(context, user_id, chat_id, pending_data)


async def begin_ocr_text_correction(
    context,
    user_id: int,
    chat_id: int,
    pending_data: dict,
    lng: str,
):
    """Ask the user for a corrected OCR title, without hard-coded beer fixes."""
    old_pending = get_pending_ocr_edit(context, user_id, chat_id)
    if old_pending:
        await delete_message_if_possible(
            context,
            old_pending.get("prompt_chat_id", chat_id),
            old_pending.get("prompt_msg_id"),
        )

    prompt = await context.bot.send_message(
        chat_id=chat_id,
        text=t(lng, "ocr_edit_prompt"),
        parse_mode="Markdown",
    )

    pending_data.update({
        "prompt_chat_id": chat_id,
        "prompt_msg_id": prompt.message_id,
        "query_chat_id": chat_id,
    })
    set_pending_ocr_edit(context, user_id, chat_id, pending_data)


# ── Pinned messages storage ───────────────────────────────────────────────────
# asyncio.Lock + write-to-.tmp-then-os.replace() + tolerate a corrupt/partial
# file - same convention every other data module in this project uses
# (checkin_queue.py, user_tokens.py, auto_toast.py, had_it_index.py, ...).
# checkins.json/pinned.json used to be the two holdouts still doing a plain
# open(path, "w"): a process kill mid-write (routine on most container hosts -
# redeploys, OOM) left a truncated file, and the bare `except FileNotFoundError`
# meant the very next read raised an uncaught JSONDecodeError - with no global
# PTB error handler registered, that silently broke every checkin-related
# command/button for every user until someone fixed the file by hand.

_pinned_lock = asyncio.Lock()


def _atomic_write_json(path: str, data: dict) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _load_json_tolerant(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


async def load_pinned() -> dict:
    async with _pinned_lock:
        return _load_json_tolerant(PINNED_FILE)


async def save_pinned(data: dict) -> None:
    async with _pinned_lock:
        _atomic_write_json(PINNED_FILE, data)


async def get_pinned(user_id, chat_id, kind: str) -> int | None:
    data = await load_pinned()
    return data.get(f"{user_id}:{chat_id}:{kind}")


async def set_pinned(user_id, chat_id, kind: str, msg_id: int) -> None:
    async with _pinned_lock:
        data = _load_json_tolerant(PINNED_FILE)
        data[f"{user_id}:{chat_id}:{kind}"] = msg_id
        _atomic_write_json(PINNED_FILE, data)


async def clear_pinned(user_id, chat_id, kind: str) -> None:
    async with _pinned_lock:
        data = _load_json_tolerant(PINNED_FILE)
        data.pop(f"{user_id}:{chat_id}:{kind}", None)
        _atomic_write_json(PINNED_FILE, data)


# ── Checkins ──────────────────────────────────────────────────────────────────

_checkins_lock = asyncio.Lock()


async def load_checkins() -> dict:
    async with _checkins_lock:
        return _load_json_tolerant(CHECKINS_FILE)


async def save_checkins(data: dict) -> None:
    async with _checkins_lock:
        _atomic_write_json(CHECKINS_FILE, data)


async def get_user_checkins(user_id) -> dict:
    data = await load_checkins()
    return data.get(str(user_id), {})


async def add_checkin(user_id, beer_id: str, name: str, brewery: str, url: str) -> None:
    async with _checkins_lock:
        data = _load_json_tolerant(CHECKINS_FILE)
        uid = str(user_id)
        if uid not in data:
            data[uid] = {}
        data[uid][beer_id] = {
            "name": name, "brewery": brewery, "url": url,
            "ts": datetime.now().strftime("%H:%M")
        }
        _atomic_write_json(CHECKINS_FILE, data)


async def remove_checkin(user_id, beer_id: str) -> None:
    async with _checkins_lock:
        data = _load_json_tolerant(CHECKINS_FILE)
        uid = str(user_id)
        if uid in data and beer_id in data[uid]:
            del data[uid][beer_id]
            _atomic_write_json(CHECKINS_FILE, data)


# ── Beer DB ───────────────────────────────────────────────────────────────────

# Which festival's beer list to load - lets a future festival switch to its
# own JSON (e.g. FESTIVAL_BEERS_FILE=other_festival.json) without touching
# code. Defaults to the one that's been hardcoded here from the start.
FESTIVAL_BEERS_FILE = os.environ.get("FESTIVAL_BEERS_FILE", "mbcc_beers.json")

def load_db():
    beer_db = {}
    raw = {}
    try:
        with open(FESTIVAL_BEERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for session, beers in raw.items():
                for beer in beers:
                    bid = beer["id"]
                    if bid not in beer_db:
                        beer_db[bid] = {
                            "id": bid,
                            "name": beer.get("name", ""),
                            "brewery": beer.get("brewery", ""),
                            "style": beer.get("style", ""),
                            "url": beer.get("url", ""),
                            "location": beer.get("location", ""),
                            "session": session,
                        }
        elif isinstance(raw, list):
            for beer in raw:
                bid = beer["id"]
                if bid not in beer_db:
                    beer_db[bid] = {
                        "id": bid,
                        "name": beer.get("name", ""),
                        "brewery": beer.get("brewery", ""),
                        "style": beer.get("style", ""),
                        "url": beer.get("url", ""),
                        "location": beer.get("location", ""),
                        "session": "",
                    }
        # Physical festival location is brewery-level, not beer-level.
        # Keep the first non-empty Area for each brewery and apply it to all
        # beers from that brewery. This also fills occasional beers with a
        # missing location when another beer by the same brewery has one.
        brewery_locations = {}
        for beer in beer_db.values():
            brewery = (beer.get("brewery") or "").strip()
            location = (beer.get("location") or "").strip()
            if brewery and location and brewery not in brewery_locations:
                brewery_locations[brewery] = location

        for beer in beer_db.values():
            brewery = (beer.get("brewery") or "").strip()
            if brewery in brewery_locations:
                beer["location"] = brewery_locations[brewery]

        logger.info(f"Loaded {FESTIVAL_BEERS_FILE}: {len(beer_db)} unique beers")
    except FileNotFoundError:
        logger.warning(f"{FESTIVAL_BEERS_FILE} not found")
    return list(beer_db.values()), raw if isinstance(raw, dict) else {}

ALL_BEERS, SESSIONS_RAW = load_db()
FESTIVAL_BEERS = ALL_BEERS

SESSION_EMOJI = {
    "yellow": "🟡",
    "blue":   "🔵",
    "red":    "🔴",
    "green":  "🟢",
}
SESSIONS = ["yellow", "blue", "red", "green"]

def session_emoji(beer: dict) -> str:
    return SESSION_EMOJI.get(beer.get("session", ""), "")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
VISION_MODEL = os.getenv("ANTHROPIC_VISION_MODEL", "claude-sonnet-4-5")
VISION_REQUEST_TIMEOUT_SECONDS = int(os.getenv("VISION_REQUEST_TIMEOUT_SECONDS", "75"))
VISION_CONNECT_TIMEOUT_SECONDS = int(os.getenv("VISION_CONNECT_TIMEOUT_SECONDS", "20"))
VISION_MAX_ATTEMPTS = int(os.getenv("VISION_MAX_ATTEMPTS", "4"))
VISION_RETRY_BASE_DELAY_SECONDS = float(os.getenv("VISION_RETRY_BASE_DELAY_SECONDS", "2"))
VISION_HTTP_TRUST_ENV = os.getenv("ANTHROPIC_HTTP_TRUST_ENV", "false").strip().lower() in {"1", "true", "yes", "on"}
VISION_TRANSPORT = os.getenv("ANTHROPIC_VISION_TRANSPORT", "requests").strip().lower()
VISION_IMAGE_MAX_SIDE = int(os.getenv("VISION_IMAGE_MAX_SIDE", "1400"))
VISION_IMAGE_JPEG_QUALITY = int(os.getenv("VISION_IMAGE_JPEG_QUALITY", "85"))
VISION_IMAGE_TARGET_KB = int(os.getenv("VISION_IMAGE_TARGET_KB", "300"))
ANTHROPIC_API_BASE = os.getenv("ANTHROPIC_API_BASE", "https://api.anthropic.com").rstrip("/")

VISION_PROMPT = """RESPOND WITH VALID JSON ARRAY ONLY. NO TEXT BEFORE OR AFTER. NO EXPLANATIONS.

You are analyzing a photo from Beer Geek Madness / MBCC craft beer festival.

CARD FORMATS:
TYPE A — Standard MBCC tap card: white background, red illustrated character, "BEER #1"/"BEER #2" labels
TYPE B — Brewery's own printed card with logo at top
TYPE C — Printed label (like a beer can/bottle label) attached above or beside a tap card

BREWERY NAME RULES:
- Prefer brewery name if printed/written ON THE TAP CARD itself
- Also use a brewery logo/name printed on the SAME TAP MACHINE directly under/beside the tap card
  (for example a sticker/logo on the dispenser below the white MBCC card).
- Do NOT use brewery names from: glasses, clothing, lanyards, distant signs, or unrelated hanging banners
- If brewery not visible on the card/machine → use ""
- Collaborations: "Brewery A x Brewery B"
- If BEER #2 has a different brewery written next to it → use that brewery for beer #2
- If a HINT is provided below → use it as the brewery for beers where brewery is not visible

FIELD ORDER for TYPE A cards:
1. "BEER #1" or "BEER #2" — SECTION LABEL, never the beer name
2. Large handwritten text → BEER NAME
3. "STYLE" label → ignore this word
4. Smaller text below STYLE → STYLE DESCRIPTION
5. "ABV" label → ignore
6. Number with % → ABV

NEVER confuse style description for beer name. Beer name comes BEFORE the "STYLE" label.
"BEER #1" and "BEER #2" are SECTION LABELS — never return them as beer name.

SOLD OUT: large X or crossed out lines on a beer slot → skip that slot entirely.

NEVER use as brewery: "BEERGEEKMADNESS", "BEERGEEK MADNESS INTERNATIONAL",
"COPENHAGEN BEER CELEBRATION", "CBC", "MBCC", "BALKAN BEER BASH", "MIKKELLER", "FREESTYLE HOPS"

EXPAND abbreviations:
IMP.=Imperial, BA=Barrel Aged, BBA=Bourbon Barrel Aged, WC=West Coast,
DDH=Double Dry Hopped, NEIPA=New England IPA, DIPA=Double IPA, TIPA=Triple IPA,
BCS/BCBS=Bourbon County Brand Stout, RIS=Russian Imperial Stout, SPON=Spontaneous

NOTE: Photos may be blurry or partial. Extract what you can see clearly.
Do NOT guess serial numbers, dates, vintage numbers, batch numbers, or other numeric parts of beer names from context.
For numeric beer-name parts such as "no. 01-2023", "#03", "2021", preserve the exact visible digits and leading zeroes.
Handwritten "1" may have a small angled top serif; do not read it as "4" unless a clear horizontal crossbar is visible.
If a digit is genuinely unclear, return the most conservative visible transcription rather than inventing a likely number.

CRITICAL MATCHING RULES:
- Do not invent an existing festival beer just because the style text is similar.
- Beer name must come from the large handwritten title area above STYLE.
- If a possible brewery is visible on the same tap machine, use it to interpret blurry handwriting.
- If a known-beers list is provided below, use it only to correct/complete visible handwriting, not as a replacement for reading the image.

IGNORE: "contains nuts", "contain nuts", "contains allergens", "contains milk" — allergen warnings, not beer/brewery names.

Return ONLY valid JSON array:
[{"brewery": "Lua Brewing", "beer": "Weathered", "style": "Imperial Stout Barrel Aged", "abv": "16.4"}]
If NO valid beers found: []"""


def _simple_norm_for_hint(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def build_festival_breweries_hint() -> str:
    """Give Claude a small whitelist of brewery names seen in the festival DB."""
    breweries = sorted({
        b.get("brewery", "").strip()
        for b in ALL_BEERS
        if b.get("brewery", "").strip()
    })
    if not breweries:
        return ""

    return (
        "\n\nPOSSIBLE FESTIVAL BREWERIES / LOGOS:\n"
        + ", ".join(breweries)
        + "\nIf one of these names/logos is visible on the same tap machine, use it as brewery."
    )


def build_known_beers_for_brewery_hint(brewery_hint: str, limit: int = 45) -> str:
    """When the user adds a caption like 'ology', pass likely beer names to OCR."""
    hint_norm = _simple_norm_for_hint(brewery_hint)
    if not hint_norm:
        return ""

    try:
        from rapidfuzz import fuzz
    except Exception:
        fuzz = None

    matches = []
    seen_ids = set()
    for beer in ALL_BEERS:
        brewery = beer.get("brewery", "")
        brewery_norm = _simple_norm_for_hint(brewery)
        is_match = (
            hint_norm in brewery_norm
            or brewery_norm in hint_norm
            or (fuzz and fuzz.partial_ratio(hint_norm, brewery_norm) >= 82)
        )
        if is_match and beer.get("id") not in seen_ids:
            seen_ids.add(beer.get("id"))
            matches.append(beer)

    if not matches:
        return ""

    matches.sort(key=lambda b: (b.get("session", ""), b.get("name", "")))
    lines = []
    for beer in matches[:limit]:
        session = beer.get("session", "")
        session_label = f" [{session}]" if session else ""
        lines.append(f"- {beer.get('name', '')} — {beer.get('brewery', '')}{session_label}")

    return (
        "\n\nKNOWN FESTIVAL BEERS FOR THE HINT BREWERY:\n"
        + "\n".join(lines)
        + "\nPrefer these names only when they match visible handwriting on the card."
    )



# ── Alphabetical sorting helpers ──────────────────────────────────────────────

_ALPHA_TRANSLATIONS = str.maketrans({
    "ø": "o", "Ø": "O",
    "œ": "oe", "Œ": "OE",
    "æ": "ae", "Æ": "AE",
    "å": "a", "Å": "A",
    "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "TH",
    "ł": "l", "Ł": "L",
})


def alpha_sort_key(text: str | None) -> str:
    """Case/diacritic-insensitive key for stable user-facing alphabetical lists."""
    value = str(text or "").translate(_ALPHA_TRANSLATIONS)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def beer_name_alpha_key(beer: dict) -> tuple[str, str, str, str]:
    """Sort primarily by beer title, then brewery, session and id for stability."""
    return (
        alpha_sort_key(beer.get("name", "")),
        alpha_sort_key(beer.get("brewery", "")),
        alpha_sort_key(beer.get("session", "")),
        str(beer.get("id", "")),
    )


def brewery_then_beer_alpha_key(beer: dict) -> tuple[str, str, str, str]:
    """Sort brewery groups alphabetically, with beer titles sorted inside."""
    return (
        alpha_sort_key(beer.get("brewery", "")),
        alpha_sort_key(beer.get("name", "")),
        alpha_sort_key(beer.get("session", "")),
        str(beer.get("id", "")),
    )


# ── Stats / Todo helpers ──────────────────────────────────────────────────────

async def build_stats_text(user_id, lng: str) -> str:
    checkins = await get_user_checkins(user_id)
    if not checkins:
        return t(lng, "stats_empty")
    lines = [t(lng, "stats_header", count=len(checkins))]
    for c in list(checkins.values()):
        url = c.get("url", "")
        name = c.get("name", "?")
        brewery = c.get("brewery", "")
        ts = c.get("ts", "")
        if url:
            lines.append(f"• [{name}]({url}) — _{brewery}_ {ts}")
        else:
            lines.append(f"• {name} — _{brewery}_ {ts}")
    return "\n".join(lines)

async def build_todo_page(user_id, page: int, lng: str, session_filter: str = "all") -> tuple[str, int]:
    checkins = await get_user_checkins(user_id)
    checked_ids = set(checkins.keys())
    todo = [b for b in FESTIVAL_BEERS if b["id"] not in checked_ids]
    if session_filter != "all":
        todo = [b for b in todo if b.get("session") == session_filter]
    todo.sort(key=brewery_then_beer_alpha_key)
    total = len(todo)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = todo[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    all_checked = len(checkins)
    filter_label = f" {SESSION_EMOJI.get(session_filter, '')} {session_filter}" if session_filter != "all" else ""
    header = t(lng, "todo_header",
               total=total, done=all_checked,
               page=page + 1, total_pages=total_pages) + filter_label + "\n"
    lines = [header]
    for b in chunk:
        url = b.get("url") or f"https://untappd.com/beer/{b['id']}"
        emoji = session_emoji(b)
        brewery = b.get("brewery", "")
        if brewery:
            lines.append(f"• {emoji} _{brewery}_ — [{b['name']}]({url})")
        else:
            lines.append(f"• {emoji} [{b['name']}]({url})")
    return "\n".join(lines), total_pages

def todo_keyboard(page: int, total_pages: int, user_id, session_filter: str = "all") -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"todo:{page-1}:{user_id}:{session_filter}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"todo:{page+1}:{user_id}:{session_filter}"))

    filters_row = []
    for s in SESSIONS:
        emoji = SESSION_EMOJI[s]
        label = f"[{emoji}]" if s == session_filter else emoji
        filters_row.append(InlineKeyboardButton(label, callback_data=f"todo:0:{user_id}:{s}"))
    all_label = "[All]" if session_filter == "all" else "All"
    filters_row.append(InlineKeyboardButton(all_label, callback_data=f"todo:0:{user_id}:all"))

    rows = []
    if nav:
        rows.append(nav)
    rows.append(filters_row)
    return InlineKeyboardMarkup(rows)

def _normalize(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\s+[xX×]\s+', ' ', text)
    text = re.sub(r'\s+by\s+', ' ', text, flags=re.IGNORECASE)
    return text.lower().strip()


# ── Pinned message helpers ────────────────────────────────────────────────────

def _is_not_modified(exc: Exception) -> bool:
    return "message is not modified" in str(exc).lower()


TELEGRAM_TIMEOUTS = {
    "connect_timeout": 30,
    "read_timeout": 30,
    "write_timeout": 30,
    "pool_timeout": 30,
}


async def resilient_send_message(context, **kwargs):
    """Send a Telegram message with longer timeouts and one retry for transient network issues."""
    for key, value in TELEGRAM_TIMEOUTS.items():
        kwargs.setdefault(key, value)

    last_exc = None
    for attempt in range(1, 3):
        try:
            return await context.bot.send_message(**kwargs)
        except (TimedOut, NetworkError) as exc:
            last_exc = exc
            logger.warning(
                "Telegram send_message failed transiently, attempt %s/2: %s",
                attempt,
                exc,
            )
            if attempt == 2:
                break
            await asyncio.sleep(1.5)

    raise last_exc

async def ensure_pinned(context, chat_id: int, message_id: int):
    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id, message_id=message_id, disable_notification=True
        )
    except TelegramError as e:
        logger.info(f"pin_chat_message: {e}")

async def send_or_update_pinned(
    context, user_id, chat_id: int, kind: str,
    text: str, keyboard=None, lng: str = "en"
) -> int:
    is_private = int(chat_id) == int(user_id)
    existing_id = await get_pinned(user_id, chat_id, kind)
    logger.info(f"send_or_update_pinned: user={user_id} chat={chat_id} kind={kind} existing={existing_id} is_private={is_private}")

    kwargs = dict(parse_mode="Markdown", disable_web_page_preview=True)
    if keyboard is not None:
        kwargs["reply_markup"] = keyboard

    if existing_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=existing_id, text=text, **kwargs
            )
            if is_private:
                await ensure_pinned(context, chat_id, existing_id)
            return existing_id
        except TelegramError as e:
            if _is_not_modified(e):
                logger.info(f"Not modified (id={existing_id})")
                if is_private:
                    await ensure_pinned(context, chat_id, existing_id)
                return existing_id
            logger.warning(f"Edit failed (id={existing_id}) chat={chat_id}: {type(e).__name__}: {e}")
            await clear_pinned(user_id, chat_id, kind)

    msg = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    await set_pinned(user_id, chat_id, kind, msg.message_id)
    if is_private:
        await ensure_pinned(context, chat_id, msg.message_id)
    return msg.message_id

async def refresh_pinned_todo(context, user_id: int, lng: str):
    existing_id = await get_pinned(user_id, user_id, "todo")
    if not existing_id:
        return
    try:
        text, total_pages = await build_todo_page(user_id, 0, lng, "all")
        keyboard = todo_keyboard(0, total_pages, user_id, "all")
        await context.bot.edit_message_text(
            chat_id=user_id, message_id=existing_id,
            text=text, parse_mode="Markdown",
            reply_markup=keyboard, disable_web_page_preview=True
        )
    except TelegramError as e:
        logger.info(f"refresh_pinned_todo: {e}")


# ── Keyboards ─────────────────────────────────────────────────────────────────

async def beer_keyboard(
    beer_id: str,
    untappd_url: str,
    user_id,
    lng: str,
    msg_id: int = 0,
    personal_state: bool = True,
) -> InlineKeyboardMarkup:
    """
    In private chats we can show personal check-in state.
    In group chats inline keyboards are shared, so keep the button neutral.
    """
    checked = personal_state and beer_id in await get_user_checkins(user_id)

    checkin_btn = (
        InlineKeyboardButton(t(lng, "btn_checked"), callback_data=f"uncheckin:{beer_id}")
        if checked else
        InlineKeyboardButton(t(lng, "btn_checkin"), callback_data=f"checkin:{beer_id}")
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(lng, "btn_untappd"), url=untappd_url),
            InlineKeyboardButton("✏️", callback_data=f"refsearch:{msg_id}"),
        ],
        [
            checkin_btn,
            InlineKeyboardButton("❌", callback_data="delete_msg"),
        ],
    ])

def nofound_keyboard(untappd_url: str, msg_id: int, lng: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lng, "btn_search_untappd"), url=untappd_url)],
        [
            InlineKeyboardButton(t(lng, "btn_search_db"), callback_data=f"search:{msg_id}"),
            InlineKeyboardButton(t(lng, "btn_fix_ocr"), callback_data=f"editocr:{msg_id}"),
            InlineKeyboardButton("❌", callback_data="delete_msg"),
        ],
    ])


# ── Command handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lng = lang(update)
    checkins = await get_user_checkins(update.effective_user.id)
    await update.message.reply_text(
        t(lng, "start", beer_count=len(ALL_BEERS), checkin_count=len(checkins)),
        parse_mode="Markdown"
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Always send stats to private chat."""
    lng = lang(update)
    user_id = update.effective_user.id
    is_group = update.effective_chat.id != user_id
    text = await build_stats_text(user_id, lng)
    try:
        # Always send to private (user_id == private chat_id)
        msg_id = await send_or_update_pinned(
            context=context, user_id=user_id, chat_id=user_id,
            kind="stats", text=text, lng=lng,
        )
        if is_group:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=t(lng, "pinned_updated"),
                )
            except TelegramError:
                pass
        else:
            await update.message.reply_text(t(lng, "pinned_updated"), reply_to_message_id=msg_id)
    except TelegramError as e:
        logger.warning(f"/stats failed: {e}")
        await update.message.reply_text(t(lng, "stats_private_error"))

async def todo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Always send todo to private chat."""
    lng = lang(update)
    user_id = update.effective_user.id
    is_group = update.effective_chat.id != user_id
    if not FESTIVAL_BEERS:
        await update.message.reply_text(t(lng, "todo_no_festival"))
        return
    text, total_pages = await build_todo_page(user_id, 0, lng, "all")
    keyboard = todo_keyboard(0, total_pages, user_id, "all")
    try:
        msg_id = await send_or_update_pinned(
            context, user_id, user_id, "todo", text, keyboard, lng
        )
        if is_group:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=t(lng, "pinned_updated"),
                )
            except TelegramError:
                pass
        else:
            await update.message.reply_text(t(lng, "pinned_updated"), reply_to_message_id=msg_id)
    except TelegramError:
        await update.message.reply_text(t(lng, "stats_private_error"))

async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send find results only to the user who asked (via private)."""
    lng = lang(update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    is_group = chat_id != user_id
    args = context.args

    if not args:
        # Do not use ForceReply here. Telegram clients can keep old ForceReply
        # prompts active when the user later opens the private chat. The bot
        # already tracks the next message via pending_find, so a normal message
        # is enough and avoids the stuck reply UI.
        prompt_text = t(lng, "find_prompt_brewery")

        if is_group:
            try:
                prompt_msg = await context.bot.send_message(chat_id=user_id, text=prompt_text)
                set_pending_find(context, user_id, user_id, True, prompt_msg.message_id)

                try:
                    await update.message.delete()
                except TelegramError:
                    pass

                return
            except TelegramError:
                # User has not started the bot in private yet. Fall back to the
                # current chat, still without ForceReply.
                pass

        prompt_msg = await context.bot.send_message(chat_id=chat_id, text=prompt_text)
        set_pending_find(context, user_id, chat_id, True, prompt_msg.message_id)
        return

    from rapidfuzz import fuzz
    query = " ".join(args).strip()
    query_norm = query.lower()
    checkins = await get_user_checkins(user_id)

    def field_match(value: str, strong_score: int = 90) -> bool:
        value_norm = (value or "").lower().strip()
        if not value_norm:
            return False
        return (
            query_norm == value_norm
            or (len(query_norm) >= 3 and query_norm in value_norm)
            or (len(value_norm) >= 4 and value_norm in query_norm)
            or (
                min(len(query_norm), len(value_norm)) >= 4
                and fuzz.partial_ratio(query_norm, value_norm) >= strong_score
            )
        )

    matches = sorted(
        (b for b in ALL_BEERS if field_match(b.get("brewery", ""))),
        key=brewery_then_beer_alpha_key,
    )

    if not matches:
        await update.message.reply_text(
            t(lng, "find_not_found_brewery", query=query), parse_mode="Markdown"
        )
        return

    by_brewery = defaultdict(list)
    for b in matches:
        by_brewery[b.get("brewery", "")].append(b)

    header = t(lng, "find_header", query=query, count=len(matches)).rstrip()
    lines = [header, ""]

    for brewery in sorted(by_brewery.keys(), key=alpha_sort_key):
        beers = sorted(by_brewery[brewery], key=beer_name_alpha_key)
        # A brewery stands in one physical place, so show a single Area once
        # on the brewery header instead of repeating it on every beer.
        brewery_location = next(
            (b.get("location", "").strip() for b in beers if b.get("location", "").strip()),
            "",
        )
        location_label = f" · 📍 {brewery_location}" if brewery_location else ""
        lines.append(f"*{brewery}*{location_label}")
        for b in beers:
            url = b.get("url") or f"https://untappd.com/beer/{b['id']}"
            emoji = session_emoji(b)
            done = "✅" if b["id"] in checkins else "▫️"
            lines.append(f"{done} {emoji} [{b['name']}]({url})")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "..."

    if is_group:
        # Delete command, send result to private
        try:
            await update.message.delete()
        except TelegramError:
            pass
        await context.bot.send_message(
            chat_id=user_id, text=text,
            parse_mode="Markdown", disable_web_page_preview=True
        )
    else:
        await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wiping check-in marks is permanent (no undo, unlike a webapp check-in
    which at least has a confirm screen before it ever touches Untappd) -
    show a confirmation prompt instead of clearing immediately, same
    "irreversible action needs a confirm step" reasoning the webapp's own
    confirm screen is built on (see README)."""
    lng = lang(update)
    data = await load_checkins()
    uid = str(update.effective_user.id)
    count = len(data.get(uid, {}))
    if count == 0:
        await update.message.reply_text(t(lng, "clear_confirm_empty"))
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(t(lng, "btn_clear_confirm"), callback_data=f"clear_confirm:{uid}"),
        InlineKeyboardButton(t(lng, "btn_clear_cancel"), callback_data="delete_msg"),
    ]])
    await update.message.reply_text(t(lng, "clear_confirm_prompt", count=count), reply_markup=keyboard)

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel pending prompts and keep the chat clean.

    `/cancel` is used as a utility command inside manual search flows. Sending a
    separate "search cancelled" message leaves one more temporary message to
    clean up, so we delete the prompt, search-query messages, pick list, and the
    `/cancel` command itself whenever Telegram allows it.
    """
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    pending = get_pending(context, user_id, chat_id)
    await cleanup_pending_search_messages(context, pending, chat_id)
    clear_pending(context, user_id, chat_id)

    pending_find = get_pending_find_data(context, user_id, chat_id)
    if pending_find:
        await delete_message_if_possible(
            context,
            pending_find.get("prompt_chat_id", chat_id),
            pending_find.get("prompt_msg_id"),
        )
    set_pending_find(context, user_id, chat_id, False)

    await delete_message_if_possible(
        context,
        chat_id,
        getattr(update.message, "message_id", None),
    )


# ── Photo handler ─────────────────────────────────────────────────────────────

MULTI_CARD_PROMPT = """

MULTI-CARD CHECK:
- Before replying, count visible MBCC beer slots/cards in the image.
- Return one JSON object per readable BEER #1 / BEER #2 slot.
- If 4 slots are visible, try hard to return 4 objects. Do not stop after 2 or 3.
- It is OK if a beer name is imperfectly transcribed; return the best visible transcription.
"""


def _build_vision_text(caption_hint: str = "", original_caption: str = "") -> str:
    vision_text = VISION_PROMPT + MULTI_CARD_PROMPT + build_festival_breweries_hint()
    if caption_hint:
        vision_text += f"\n\nHINT: The brewery for these beers may be: '{original_caption or caption_hint}'"
        vision_text += build_known_beers_for_brewery_hint(caption_hint)
    return vision_text


def _parse_claude_json(raw: str) -> list[dict]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    if raw and not raw.startswith("["):
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        raw = m.group(0) if m else "[]"
    data = json.loads(raw) if raw and raw != "[]" else []
    return data if isinstance(data, list) else []


def _dedupe_detected(detected: list[dict]) -> list[dict]:
    seen, unique = set(), []
    for e in detected:
        if not isinstance(e, dict):
            continue
        beer = (e.get("beer") or "").strip()
        brewery = (e.get("brewery") or "").strip()
        if not beer:
            continue
        key = (_simple_norm_for_hint(beer), _simple_norm_for_hint(brewery))
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    return unique


def _fill_missing_brewery_for_one_photo(detected: list[dict], caption_hint: str, original_caption: str) -> list[dict]:
    """Use v2-like consensus, but only inside one photo, not across whole album."""
    found_breweries = [
        (e.get("brewery") or "").strip()
        for e in detected
        if isinstance(e, dict) and (e.get("brewery") or "").strip()
    ]
    consensus_brewery = found_breweries[0] if found_breweries else (original_caption if caption_hint else "")
    if consensus_brewery:
        for e in detected:
            if isinstance(e, dict) and not (e.get("brewery") or "").strip():
                e["brewery"] = consensus_brewery
    return detected


def _create_anthropic_client():
    """Create an isolated Anthropic client for one vision attempt.

    The default SDK client can reuse pooled HTTP connections and also performs
    its own hidden retries. On some Windows/network setups that repeatedly ends
    with `RemoteProtocolError: Server disconnected without sending a response`.
    For vision we want our own explicit retry loop, so every attempt gets a
    fresh httpx client with keep-alive disabled.
    """
    timeout = httpx.Timeout(
        connect=VISION_CONNECT_TIMEOUT_SECONDS,
        read=VISION_REQUEST_TIMEOUT_SECONDS,
        write=VISION_CONNECT_TIMEOUT_SECONDS,
        pool=VISION_CONNECT_TIMEOUT_SECONDS,
    )
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=10)
    http_client = httpx.Client(
        timeout=timeout,
        limits=limits,
        http2=False,
        trust_env=VISION_HTTP_TRUST_ENV,
    )

    try:
        return anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            max_retries=0,
            timeout=timeout,
            http_client=http_client,
        )
    except TypeError:
        # Older anthropic SDKs may not support all constructor kwargs. Fall back
        # instead of crashing at startup; the outer retry loop still helps.
        try:
            http_client.close()
        except Exception:
            pass
        try:
            return anthropic.Anthropic(
                api_key=ANTHROPIC_API_KEY,
                max_retries=0,
                timeout=VISION_REQUEST_TIMEOUT_SECONDS,
            )
        except TypeError:
            return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class AnthropicVisionHTTPError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body or ""
        super().__init__(f"Anthropic HTTP {status_code}: {self.body[:500]}")


def _anthropic_payload(photo_b64: str, vision_text: str) -> dict:
    return {
        "model": VISION_MODEL,
        "max_tokens": 3000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": photo_b64}},
                {"type": "text", "text": vision_text},
            ],
        }],
    }


def _extract_anthropic_text(data: dict) -> str:
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, list):
        raise RuntimeError(f"Unexpected Anthropic response: {repr(data)[:500]}")

    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    return "\n".join(t for t in texts if t).strip()


def _call_claude_vision_sdk(photo_b64: str, vision_text: str) -> str:
    client = _create_anthropic_client()
    try:
        response = client.messages.create(**_anthropic_payload(photo_b64, vision_text))
        return response.content[0].text.strip()
    finally:
        try:
            client.close()
        except Exception:
            pass


def _call_claude_vision_requests(photo_b64: str, vision_text: str) -> str:
    """Call Anthropic directly with requests instead of the SDK/httpx stack.

    On the affected Windows setup text-only SDK calls work, but image payloads
    fail in httpx with RemoteProtocolError before an HTTP response arrives. This
    direct path uses requests/urllib3 and `Connection: close`, which avoids that
    code path entirely.
    """
    import requests

    session = requests.Session()
    session.trust_env = VISION_HTTP_TRUST_ENV
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "application/json",
        "connection": "close",
        "user-agent": f"checkin-helper/{BOT_BUILD}",
    }

    response = session.post(
        f"{ANTHROPIC_API_BASE}/v1/messages",
        headers=headers,
        json=_anthropic_payload(photo_b64, vision_text),
        timeout=(VISION_CONNECT_TIMEOUT_SECONDS, VISION_REQUEST_TIMEOUT_SECONDS),
    )

    if response.status_code >= 400:
        raise AnthropicVisionHTTPError(response.status_code, response.text)

    return _extract_anthropic_text(response.json())


def _call_claude_vision(photo_b64: str, vision_text: str) -> str:
    transport = VISION_TRANSPORT or "requests"

    if transport == "sdk":
        return _call_claude_vision_sdk(photo_b64, vision_text)

    if transport == "auto":
        try:
            return _call_claude_vision_requests(photo_b64, vision_text)
        except Exception as req_exc:
            logger.warning("requests vision transport failed; trying sdk/httpx fallback: %s", req_exc)
            return _call_claude_vision_sdk(photo_b64, vision_text)

    # Default: requests. This is intentional because this machine showed stable
    # text-only SDK calls but repeated httpx RemoteProtocolError for image calls.
    return _call_claude_vision_requests(photo_b64, vision_text)

def _is_retryable_vision_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()

    if isinstance(exc, AnthropicVisionHTTPError):
        return exc.status_code in {408, 409, 425, 429, 500, 502, 503, 504, 529}

    retryable_names = (
        "apiconnectionerror",
        "apitimeouterror",
        "timeout",
        "ratelimiterror",
        "internalservererror",
        "connectionerror",
        "remoteprotocolerror",
        "sslerror",
        "chunkedencodingerror",
    )
    retryable_text = (
        "connection error",
        "server disconnected",
        "remoteprotocolerror",
        "remote protocol",
        "connection reset",
        "connection aborted",
        "read error",
        "write error",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "rate limit",
    )

    return any(part in name for part in retryable_names) or any(part in text for part in retryable_text)


async def _recognize_one_photo(photo_b64: str, caption_hint: str, original_caption: str = "") -> list[dict]:
    vision_text = _build_vision_text(caption_hint, original_caption)
    last_exc: Exception | None = None

    for attempt in range(1, VISION_MAX_ATTEMPTS + 1):
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(_call_claude_vision, photo_b64, vision_text),
                timeout=VISION_REQUEST_TIMEOUT_SECONDS,
            )
            raw = (raw or "").strip()
            logger.info(f"Claude raw ({len(raw)}): {repr(raw[:300])}")
            detected = _parse_claude_json(raw)
            return _dedupe_detected(detected)

        except Exception as exc:
            last_exc = exc
            retryable = _is_retryable_vision_error(exc)

            if attempt >= VISION_MAX_ATTEMPTS or not retryable:
                logger.error(
                    "Claude vision failed after %s/%s attempt(s): %s",
                    attempt,
                    VISION_MAX_ATTEMPTS,
                    exc,
                )
                raise

            delay = VISION_RETRY_BASE_DELAY_SECONDS * attempt
            logger.warning(
                "Claude vision transient error on attempt %s/%s; retrying in %.1fs: %s",
                attempt,
                VISION_MAX_ATTEMPTS,
                delay,
                exc,
            )
            await asyncio.sleep(delay)

    raise last_exc or RuntimeError("Vision recognition failed")


def _prepare_photo_bytes_for_vision(photo_bytes: bytes | bytearray) -> bytes:
    """Resize/re-encode Telegram photos when Pillow is available.

    The affected Windows/network setup accepts small text requests to Anthropic
    but can break TLS on very large image JSON payloads. Keep the image payload
    reasonably small, but not so compressed that handwritten digits become OCR
    artifacts. The defaults favor accuracy for small tap-card text, with env
    knobs for tuning:
      VISION_IMAGE_MAX_SIDE=1400
      VISION_IMAGE_JPEG_QUALITY=85
      VISION_IMAGE_TARGET_KB=300
    """
    original = bytes(photo_bytes)

    try:
        from io import BytesIO
        from PIL import Image

        image = Image.open(BytesIO(original))
        image = image.convert("RGB")
        image.thumbnail((VISION_IMAGE_MAX_SIDE, VISION_IMAGE_MAX_SIDE))

        # Try progressively lower JPEG quality until the payload is small enough.
        target_bytes = max(20, VISION_IMAGE_TARGET_KB) * 1024
        qualities = []
        start_quality = max(35, min(95, VISION_IMAGE_JPEG_QUALITY))
        for q in (start_quality, 65, 55, 45, 35):
            if q not in qualities:
                qualities.append(q)

        best = None
        for quality in qualities:
            out = BytesIO()
            image.save(out, format="JPEG", quality=quality, optimize=True, progressive=False)
            prepared = out.getvalue()
            if prepared:
                best = prepared
                if len(prepared) <= target_bytes:
                    break

        if best and len(best) < len(original):
            logger.info(
                "Prepared photo for vision: %.1fKB -> %.1fKB (max_side=%s target=%sKB)",
                len(original) / 1024,
                len(best) / 1024,
                VISION_IMAGE_MAX_SIDE,
                VISION_IMAGE_TARGET_KB,
            )
            return best

    except Exception as exc:
        logger.debug("Photo prepare skipped: %s", exc)

    return original


async def _download_photo_b64(context: ContextTypes.DEFAULT_TYPE, message) -> str:
    photo = message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()
    photo_bytes = _prepare_photo_bytes_for_vision(photo_bytes)
    return base64.standard_b64encode(photo_bytes).decode("utf-8")


async def _process_photo_messages(anchor_message, messages: list, context: ContextTypes.DEFAULT_TYPE, lng: str):
    captions = [(m.caption or "").strip() for m in messages if (m.caption or "").strip()]
    original_caption = " | ".join(captions)
    caption_hint = original_caption.lower().strip()

    progress_text = t(lng, "recognizing")
    if len(messages) > 1:
        progress_text += f" ({len(messages)} фото)"
    progress_msg = await anchor_message.reply_text(progress_text)

    all_detected: list[dict] = []
    had_vision_error = False

    for idx, message in enumerate(messages, start=1):
        try:
            if len(messages) > 1:
                await progress_msg.edit_text(f"{t(lng, 'recognizing')} ({idx}/{len(messages)})")

            photo_b64 = await _download_photo_b64(context, message)
            detected = await _recognize_one_photo(photo_b64, caption_hint, original_caption)
            detected = _fill_missing_brewery_for_one_photo(detected, caption_hint, original_caption)
            all_detected.extend(detected)

        except Exception as exc:
            import traceback
            had_vision_error = True
            logger.error(f"Vision error on photo {idx}/{len(messages)}: {exc}\n{traceback.format_exc()}")

    detected = _dedupe_detected(all_detected)

    if not detected:
        if had_vision_error:
            await progress_msg.edit_text(t(lng, "error_vision"))
        else:
            await progress_msg.edit_text(t(lng, "not_found_photo"))
        return

    await _send_detected_results(
        anchor_message=anchor_message,
        context=context,
        progress_msg=progress_msg,
        detected=detected,
        caption_hint=caption_hint,
        lng=lng,
    )


async def _process_media_group_later(context: ContextTypes.DEFAULT_TYPE, key: str):
    try:
        await asyncio.sleep(MEDIA_GROUP_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    data = context.chat_data.pop(key, None)
    if not data:
        return

    messages = sorted(data.get("messages", []), key=lambda m: m.message_id)
    if not messages:
        return

    lng = data.get("lng", "en")
    await _process_photo_messages(messages[0], messages, context, lng)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    lng = lang(update)

    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        key = f"media_group:{message.chat_id}:{media_group_id}"
        data = context.chat_data.setdefault(key, {"messages": [], "task": None, "lng": lng})
        data["messages"].append(message)
        data["lng"] = lng

        # Debounce: every new album message restarts the timer, so we wait for the whole album.
        task = data.get("task")
        if task is not None and not task.done():
            task.cancel()
        data["task"] = asyncio.create_task(_process_media_group_later(context, key))
        return

    await _process_photo_messages(message, [message], context, lng)


async def _send_detected_results(
    anchor_message,
    context: ContextTypes.DEFAULT_TYPE,
    progress_msg,
    detected: list[dict],
    caption_hint: str,
    lng: str,
):
    total = len(detected)
    await progress_msg.edit_text(t(lng, "found_beers", count=total))
    user_id = anchor_message.from_user.id
    chat_id = anchor_message.chat_id
    personal_keyboard = chat_id == user_id
    first_result_message_used = False
    sent_count = 0
    failed_count = 0

    logger.info("Detected beers to send (%s): %s", total, detected)

    for idx, entry in enumerate(detected, start=1):
        try:
            beer_name = (entry.get("beer") or "").strip()
            brewery_name = (entry.get("brewery") or "").strip()
            style = (entry.get("style") or "").strip()
            abv = entry.get("abv")
            abv_str = f" • {abv}%" if abv else ""
            if not beer_name:
                logger.info("Skipping detected entry without beer name: %s", entry)
                failed_count += 1
                continue

            match = find_beers_in_db(ALL_BEERS, beer_name, brewery_name)
            if not match and caption_hint:
                match = find_beers_in_db(ALL_BEERS, beer_name, caption_hint)
            if not match:
                expanded = beer_name.replace("BCS", "Bourbon County Stout").replace("BCBS", "Bourbon County Brand Stout")
                if expanded != beer_name:
                    match = find_beers_in_db(ALL_BEERS, expanded, brewery_name)

            if match:
                untappd_url = (
                    match.get("url") if match.get("url") and "/b/" in match.get("url", "")
                    else f"https://untappd.com/beer/{match['id']}"
                )
                display_style = match.get("style") or style
                beer_id = match["id"]
                sess = session_emoji(match)
                text = f"🍺 <code>{h(match['name'])}</code>\n🏭 {h(match.get('brewery', ''))}"
                if display_style:
                    text += f"\n🏷 {h(display_style)}{h(abv_str)}"
                if sess:
                    text += f"\n{h(sess)} {h(match.get('session', ''))}"
                if match.get("location"):
                    text += f"\n📍 {h(match.get('location'))}"
                context.bot_data[f"beer:{beer_id}"] = {
                    "name": match["name"],
                    "brewery": match.get("brewery", ""),
                    "url": untappd_url,
                    "location": match.get("location", ""),
                }
                if not first_result_message_used:
                    await progress_msg.edit_text(
                        text,
                        reply_markup=await beer_keyboard(
                            beer_id,
                            untappd_url,
                            user_id,
                            lng,
                            0,
                            personal_state=personal_keyboard,
                        ),
                        disable_web_page_preview=True,
                        parse_mode="HTML",
                    )
                    sent_beer = progress_msg
                    first_result_message_used = True
                else:
                    sent_beer = await resilient_send_message(context,
                        chat_id=chat_id,
                        text=text,
                        reply_markup=await beer_keyboard(
                            beer_id,
                            untappd_url,
                            user_id,
                            lng,
                            0,
                            personal_state=personal_keyboard,
                        ),
                        disable_web_page_preview=True,
                        parse_mode="HTML",
                    )
                context.bot_data[f"search_ctx:{sent_beer.message_id}"] = {
                    "beer_name": beer_name, "brewery_name": brewery_name,
                    "style": display_style, "abv_str": abv_str, "web_url": "",
                    "chat_id": chat_id, "user_id": user_id, "lng": lng,
                }
                await sent_beer.edit_reply_markup(
                    reply_markup=await beer_keyboard(
                        beer_id,
                        untappd_url,
                        user_id,
                        lng,
                        sent_beer.message_id,
                        personal_state=personal_keyboard,
                    )
                )
            else:
                web_url = await asyncio.to_thread(search_untappd_web, beer_name, brewery_name)
                text = f"🍺 <code>{h(beer_name)}</code>"
                if brewery_name:
                    text += f"\n🏭 {h(brewery_name)}"
                if style:
                    text += f"\n🏷 {h(style)}{h(abv_str)}"
                if not first_result_message_used:
                    await progress_msg.edit_text(
                        text,
                        reply_markup=nofound_keyboard(web_url, 0, lng),
                        disable_web_page_preview=True,
                        parse_mode="HTML",
                    )
                    sent = progress_msg
                    first_result_message_used = True
                else:
                    sent = await resilient_send_message(context,
                        chat_id=chat_id,
                        text=text,
                        reply_markup=nofound_keyboard(web_url, 0, lng),
                        disable_web_page_preview=True,
                        parse_mode="HTML",
                    )
                context.bot_data[f"search_ctx:{sent.message_id}"] = {
                    "beer_name": beer_name, "brewery_name": brewery_name,
                    "style": style, "abv_str": abv_str, "web_url": web_url,
                    "chat_id": chat_id, "user_id": user_id, "lng": lng,
                }
                await sent.edit_reply_markup(reply_markup=nofound_keyboard(web_url, sent.message_id, lng))

            sent_count += 1

        except Exception as exc:
            failed_count += 1
            logger.exception(
                "Failed to send detected result %s/%s: %r; error=%s",
                idx,
                total,
                entry,
                exc,
            )
            continue

    logger.info("Sent detected beer results: %s/%s, failed=%s", sent_count, total, failed_count)

    if failed_count:
        try:
            await resilient_send_message(context,
                chat_id=chat_id,
                text=f"⚠️ Розпізнав {total}, але зміг показати {sent_count}. Подивись bot_log.txt — там буде причина для пропущених.",
            )
        except Exception:
            logger.exception("Could not send failed-count notice")


# ── Callback handler ──────────────────────────────────────────────────────────

async def send_private_checkin_notice(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
) -> bool:
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            disable_web_page_preview=True,
        )
        return True
    except TelegramError as e:
        logger.info(f"Could not send private checkin notice to {user_id}: {e}")
        return False

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Don't answer yet — will answer below based on action
    user_id = query.from_user.id
    lng = lang_from_code(getattr(query.from_user, "language_code", "en"))
    data = query.data

    if data == "noop":
        await query.answer()
        return

    elif data.startswith("clear_confirm:"):
        # Only the person who ran /clear can confirm it - a bare "clear_confirm"
        # with no owner check would let anyone else in a group chat tap this
        # button and end up wiping their OWN check-ins (not the requester's,
        # since the wipe always targets the tapper - still confusing/unwanted
        # for someone who never typed /clear themselves).
        owner_id = data.split(":", 1)[1]
        if str(user_id) != owner_id:
            await query.answer(t(lng, "clear_not_yours"), show_alert=True)
            return
        data_store = await load_checkins()
        count = len(data_store.get(owner_id, {}))
        data_store[owner_id] = {}
        await save_checkins(data_store)
        await query.answer()
        await query.edit_message_text(t(lng, "cleared", count=count))
        return

    elif data == "delete_msg":
        await query.answer()
        try:
            await query.message.delete()
        except TelegramError as e:
            logger.info(f"delete_msg: {e}")
        return

    elif data.startswith("checkin:"):
        beer_id = data.split(":", 1)[1]
        is_private = int(query.message.chat_id) == int(user_id)

        already_checked = beer_id in await get_user_checkins(user_id)
        beer_info = context.bot_data.get(f"beer:{beer_id}", {})

        if not beer_info.get("name") or beer_info.get("name") == "?":
            lines = (query.message.text or "").split("\n")
            name = lines[0].replace("🍺", "").strip() if lines else "?"
            brewery = lines[1].replace("🏭", "").strip() if len(lines) > 1 else ""
            beer_info = {
                "name": name,
                "brewery": brewery,
                "url": f"https://untappd.com/beer/{beer_id}",
            }

        beer_name = beer_info.get("name", "?")
        brewery = beer_info.get("brewery", "")
        url = beer_info.get("url", "")

        if already_checked:
            await query.answer(
                t(lng, "checkin_already", beer_name=beer_name),
                show_alert=True,
            )
        else:
            await add_checkin(user_id, beer_id, beer_name, brewery, url)
            await refresh_pinned_todo(context, user_id, lng)

            private_text = t(lng, "checkin_private_notice", beer_name=beer_name)
            if brewery:
                private_text += f"\n🏭 {brewery}"
            if url:
                private_text += f"\n🔗 {url}"

            private_sent = False

            if not is_private:
                private_sent = await send_private_checkin_notice(
                    context=context,
                    user_id=user_id,
                    text=private_text,
                )

            if is_private:
                await query.answer(t(lng, "checkin_added_alert"), show_alert=True)
            elif private_sent:
                await query.answer(
                    t(lng, "checkin_added_private_alert"),
                    show_alert=True,
                )
            else:
                await query.answer(
                    t(lng, "checkin_added_no_private_alert"),
                    show_alert=True,
                )

        # In groups, do NOT edit the shared keyboard.
        # Otherwise everyone sees this user's personal state.
        if is_private:
            row0 = query.message.reply_markup.inline_keyboard[0]
            untappd_url = row0[0].url or ""
            msg_id = query.message.message_id

            await query.edit_message_reply_markup(
                reply_markup=await beer_keyboard(
                    beer_id,
                    untappd_url,
                    user_id,
                    lng,
                    msg_id,
                    personal_state=True,
                )
            )

    elif data.startswith("uncheckin:"):
        beer_id = data.split(":", 1)[1]
        is_private = int(query.message.chat_id) == int(user_id)

        was_checked = beer_id in await get_user_checkins(user_id)
        beer_info = context.bot_data.get(f"beer:{beer_id}", {})
        beer_name = beer_info.get("name", "?")
        brewery = beer_info.get("brewery", "")

        if was_checked:
            await remove_checkin(user_id, beer_id)
            await refresh_pinned_todo(context, user_id, lng)

            private_text = t(lng, "uncheckin_private_notice", beer_name=beer_name)
            if brewery:
                private_text += f"\n🏭 {brewery}"

            private_sent = False

            if not is_private:
                private_sent = await send_private_checkin_notice(
                    context=context,
                    user_id=user_id,
                    text=private_text,
                )

            if is_private:
                await query.answer(t(lng, "uncheckin_removed_alert"), show_alert=True)
            elif private_sent:
                await query.answer(
                    t(lng, "uncheckin_removed_private_alert"),
                    show_alert=True,
                )
            else:
                await query.answer(
                    t(lng, "uncheckin_removed_no_private_alert"),
                    show_alert=True,
                )
        else:
            await query.answer(
                t(lng, "uncheckin_not_checked"),
                show_alert=True,
            )

        # In groups, do NOT edit the shared keyboard.
        if is_private:
            row0 = query.message.reply_markup.inline_keyboard[0]
            untappd_url = row0[0].url or ""
            msg_id = query.message.message_id

            await query.edit_message_reply_markup(
                reply_markup=await beer_keyboard(
                    beer_id,
                    untappd_url,
                    user_id,
                    lng,
                    msg_id,
                    personal_state=True,
                )
            )

    elif data.startswith("todo:"):
        await query.answer()
        parts = data.split(":")
        page = int(parts[1])
        owner_id = int(parts[2])
        session_filter = parts[3] if len(parts) > 3 else "all"
        text, total_pages = await build_todo_page(owner_id, page, lng, session_filter)
        keyboard = todo_keyboard(page, total_pages, owner_id, session_filter)
        await query.edit_message_text(
            text=text, parse_mode="Markdown",
            reply_markup=keyboard, disable_web_page_preview=True
        )

    elif data.startswith("refsearch:"):
        await query.answer()
        msg_id = int(data.split(":", 1)[1])
        ctx = context.bot_data.get(f"search_ctx:{msg_id}", {})
        chat_id = query.message.chat_id
        await begin_manual_db_search(context, user_id, chat_id, {
            "msg_id": msg_id, "chat_id": chat_id,
            "beer_name": ctx.get("beer_name", ""),
            "brewery_name": ctx.get("brewery_name", ""),
            "style": ctx.get("style", ""), "abv_str": ctx.get("abv_str", ""),
            "web_url": ctx.get("web_url", ""), "user_id": ctx.get("user_id", user_id),
            "lng": ctx.get("lng", lng),
        }, lng)

    elif data.startswith("editocr:"):
        await query.answer()
        msg_id = int(data.split(":", 1)[1])
        ctx = context.bot_data.get(f"search_ctx:{msg_id}")
        if not ctx:
            await query.answer(t(lng, "context_lost"), show_alert=True)
            return
        chat_id = query.message.chat_id
        await begin_ocr_text_correction(context, user_id, chat_id, {
            "msg_id": msg_id, "chat_id": ctx["chat_id"],
            "beer_name": ctx.get("beer_name", ""),
            "brewery_name": ctx.get("brewery_name", ""),
            "style": ctx.get("style", ""),
            "abv_str": ctx.get("abv_str", ""),
            "web_url": ctx.get("web_url", ""),
            "user_id": ctx.get("user_id", user_id),
            "lng": ctx.get("lng", lng),
        }, lng)

    elif data.startswith("search:"):
        await query.answer()
        msg_id = int(data.split(":", 1)[1])
        ctx = context.bot_data.get(f"search_ctx:{msg_id}")
        if not ctx:
            await query.answer(t(lng, "context_lost"), show_alert=True)
            return
        chat_id = query.message.chat_id
        await begin_manual_db_search(context, user_id, chat_id, {
            "msg_id": msg_id, "chat_id": ctx["chat_id"],
            "beer_name": ctx.get("beer_name", ""),
            "brewery_name": ctx.get("brewery_name", ""),
            "style": ctx["style"], "abv_str": ctx["abv_str"],
            "web_url": ctx["web_url"], "user_id": ctx["user_id"],
            "lng": ctx.get("lng", lng),
        }, lng)

    elif data.startswith("pick:"):
        await query.answer()
        parts = data.split(":", 2)
        beer_id = parts[1]
        msg_id = int(parts[2])
        chat_id = query.message.chat_id
        pending = get_pending(context, user_id, chat_id) or {
            "msg_id": msg_id, "chat_id": chat_id,
            "style": "", "abv_str": "", "user_id": user_id, "lng": lng
        }
        if beer_id == "cancel":
            await query.message.delete()
            await cleanup_pending_search_messages(context, pending, chat_id)
            clear_pending(context, user_id, chat_id)
            return
        match = next((b for b in ALL_BEERS if b["id"] == beer_id), None)
        if match:
            await _apply_search_result(context, pending, match, user_id)
            await cleanup_pending_search_messages(context, pending, chat_id)
        await query.message.delete()
        clear_pending(context, user_id, chat_id)

    elif data.startswith("commentreply:"):
        await query.answer()
        checkin_id = int(data.split(":", 1)[1])
        chat_id = query.message.chat_id
        set_pending_comment_reply(context, user_id, chat_id, {"checkin_id": checkin_id})
        commenter = context.bot_data.get(f"comment_reply_to:{checkin_id}")
        prompt = t(lng, "comment_reply_prompt_named", username=commenter) if commenter else t(lng, "comment_reply_prompt")
        await context.bot.send_message(chat_id=chat_id, text=prompt)


# ── Text search handler ───────────────────────────────────────────────────────

def _parse_corrected_ocr_input(text: str, fallback_brewery: str = "") -> tuple[str, str]:
    """Parse a user correction.

    Supported forms:
      Beer Name
      Brewery | Beer Name

    We intentionally avoid guessing from hyphens/dashes because beer names often
    contain them. The pipe form is explicit and works for arbitrary tap-board text.
    """
    raw = re.sub(r"\s+", " ", text or "").strip()
    if "|" in raw:
        brewery, beer = raw.split("|", 1)
        return beer.strip(), brewery.strip()
    return raw, (fallback_brewery or "").strip()


async def _apply_ocr_text_correction(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    lng = pending.get("lng", lang(update))
    corrected_beer, corrected_brewery = _parse_corrected_ocr_input(
        update.message.text,
        pending.get("brewery_name", ""),
    )

    await delete_message_if_possible(
        context,
        pending.get("prompt_chat_id", chat_id),
        pending.get("prompt_msg_id"),
    )
    await delete_message_if_possible(context, chat_id, update.message.message_id)

    if not corrected_beer:
        return

    msg_id = pending.get("msg_id", 0)
    match = find_beers_in_db(ALL_BEERS, corrected_beer, corrected_brewery)
    if not match and corrected_brewery:
        match = find_beers_in_db(ALL_BEERS, corrected_beer, "")

    if match:
        pending = dict(pending)
        pending["beer_name"] = corrected_beer
        pending["brewery_name"] = corrected_brewery
        await _apply_search_result(context, pending, match, user_id)
        return

    style = pending.get("style", "")
    abv_str = pending.get("abv_str", "")
    web_url = await asyncio.to_thread(search_untappd_web, corrected_beer, corrected_brewery)

    text = f"🍺 <code>{h(corrected_beer)}</code>"
    if corrected_brewery:
        text += f"\n🏭 {h(corrected_brewery)}"
    if style:
        text += f"\n🏷 {h(style)}{h(abv_str)}"

    context.bot_data[f"search_ctx:{msg_id}"] = {
        "beer_name": corrected_beer,
        "brewery_name": corrected_brewery,
        "style": style,
        "abv_str": abv_str,
        "web_url": web_url,
        "chat_id": pending.get("chat_id", chat_id),
        "user_id": pending.get("user_id", user_id),
        "lng": lng,
    }

    try:
        await context.bot.edit_message_text(
            chat_id=pending.get("chat_id", chat_id),
            message_id=msg_id,
            text=text,
            parse_mode="HTML",
            reply_markup=nofound_keyboard(web_url, msg_id, lng),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"OCR correction edit error: {e}")
        await context.bot.send_message(
            chat_id=pending.get("chat_id", chat_id),
            text=text,
            parse_mode="HTML",
            reply_markup=nofound_keyboard(web_url, msg_id, lng),
            disable_web_page_preview=True,
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Handle /find reply. Also accept replies to old ForceReply prompts that
    # Telegram may keep open after a redeploy.
    pending_find = get_pending_find_data(context, user_id, chat_id)
    stale_find_reply = is_stale_find_prompt_reply(update, context)
    if pending_find or stale_find_reply:
        set_pending_find(context, user_id, chat_id, False)

        prompt_chat_id = (pending_find or {}).get("prompt_chat_id", chat_id)
        prompt_msg_id = (pending_find or {}).get("prompt_msg_id")

        reply = getattr(update.message, "reply_to_message", None)
        if stale_find_reply and reply:
            prompt_chat_id = chat_id
            prompt_msg_id = reply.message_id

        await delete_message_if_possible(context, prompt_chat_id, prompt_msg_id)

        context.args = update.message.text.strip().split()
        await find_cmd(update, context)
        return

    # Handle Untappd token paste reply (/connect_untappd)
    pending_untappd = get_pending_untappd_token(context, user_id, chat_id)
    if pending_untappd is not None:
        lng = lang(update)
        text = update.message.text.strip()
        if text.startswith("/cancel"):
            clear_pending_untappd_token(context, user_id, chat_id)
            await update.message.reply_text(t(lng, "connect_untappd_cancelled"))
            return
        if text.startswith("/"):
            return
        clear_pending_untappd_token(context, user_id, chat_id)
        try:
            profile = await untappd_mcp.get_my_profile(text)
        except untappd_mcp.UntappdRateLimited:
            await update.message.reply_text(t(lng, "untappd_token_rate_limited"))
            return
        except untappd_mcp.UntappdMCPError as e:
            logger.warning(f"/connect_untappd validation failed: {e}")
            await update.message.reply_text(t(lng, "untappd_token_invalid"))
            return
        profile_user = (profile or {}).get("user") or {}
        username = profile_user.get("user_name") or ""
        first_name = profile_user.get("first_name") or ""
        is_supporter = bool(profile_user.get("is_supporter"))
        await user_tokens.set_token(user_id, text, username, first_name, is_supporter=is_supporter)
        await update.message.reply_text(
            t(lng, "untappd_token_connected", username=username or first_name or "?")
        )
        return

    # Handle a reply to a "💬 Відповісти" comment-watch notification
    pending_comment_reply = get_pending_comment_reply(context, user_id, chat_id)
    if pending_comment_reply is not None:
        lng = lang(update)
        text = update.message.text.strip()
        if text.startswith("/cancel"):
            clear_pending_comment_reply(context, user_id, chat_id)
            await update.message.reply_text(t(lng, "comment_reply_cancelled"))
            return
        if text.startswith("/"):
            return
        clear_pending_comment_reply(context, user_id, chat_id)
        checkin_id = pending_comment_reply["checkin_id"]
        token = await user_tokens.get_token(user_id)
        if not token:
            await update.message.reply_text(t(lng, "auto_toast_not_connected"))
            return
        # "@username, ..." convention - Untappd comments have no native
        # threading, so this is the closest thing to a visible reply.
        commenter = context.bot_data.get(f"comment_reply_to:{checkin_id}")
        comment_text = f"@{commenter}, {text}" if commenter else text
        try:
            await untappd_mcp.comment_checkin(token, checkin_id, comment_text)
        except untappd_mcp.UntappdRateLimited:
            await update.message.reply_text(t(lng, "untappd_token_rate_limited"))
            return
        except untappd_mcp.UntappdMCPError as e:
            logger.warning(f"comment reply failed: {e}")
            await update.message.reply_text(t(lng, "comment_reply_failed"))
            return
        await update.message.reply_text(t(lng, "comment_reply_sent"))
        return

    # Handle OCR text correction reply
    pending_ocr_edit = get_pending_ocr_edit(context, user_id, chat_id)
    if pending_ocr_edit:
        query_text = update.message.text.strip()
        if query_text.startswith("/cancel"):
            await delete_message_if_possible(
                context,
                pending_ocr_edit.get("prompt_chat_id", chat_id),
                pending_ocr_edit.get("prompt_msg_id"),
            )
            clear_pending_ocr_edit(context, user_id, chat_id)
            await update.message.reply_text(t(lang(update), "search_cancelled"))
            return
        if query_text.startswith("/"):
            return
        await _apply_ocr_text_correction(update, context, pending_ocr_edit)
        clear_pending_ocr_edit(context, user_id, chat_id)
        return

    # Handle beer search reply
    pending = get_pending(context, user_id, chat_id)
    if not pending:
        return

    query_text = update.message.text.strip()
    if query_text.startswith("/"):
        return

    pending.setdefault("user_query_msg_ids", []).append(update.message.message_id)
    pending["query_chat_id"] = chat_id

    lng = pending.get("lng", lang(update))
    query_lower = query_text.lower().strip()
    brewery_hint = pending.get("brewery_name", "")

    def make_buttons(results):
        buttons = []
        for beer in results[:5]:
            bid = beer["id"]
            context.bot_data[f"beer:{bid}"] = {
                "name": beer["name"],
                "brewery": beer.get("brewery", ""),
                "url": beer.get("url", ""),
                "location": beer.get("location", ""),
            }
            emoji = session_emoji(beer)
            label = f"{emoji} {beer['name']} — {beer.get('brewery', '')}"[:52]
            buttons.append([InlineKeyboardButton(label, callback_data=f"pick:{bid}:{pending['msg_id']}")])
        buttons.append([InlineKeyboardButton(t(lng, "btn_cancel"), callback_data=f"pick:cancel:{pending['msg_id']}")])
        return buttons

    from rapidfuzz import fuzz

    def find_brewery_matches(query: str):
        q = query.lower().strip()
        if not q:
            return []

        matches = []
        for beer in ALL_BEERS:
            brewery = beer.get("brewery", "")
            brewery_lower = brewery.lower().strip()
            if not brewery_lower:
                continue

            score = fuzz.partial_ratio(q, brewery_lower)
            if (
                q == brewery_lower
                or q in brewery_lower
                or brewery_lower in q
                or score >= 90
            ):
                matches.append((score, beer))

        matches.sort(
            key=lambda item: (
                item[1].get("brewery", "").lower().strip() != q,
                -item[0],
                item[1].get("name", "").lower(),
            )
        )
        return matches

    results = []
    brewery_matches = find_brewery_matches(query_text)

    # If the user typed a brewery name after pressing "Search in database",
    # keep the original detected beer title and use the user's text as the
    # brewery hint BEFORE treating the text as a beer name. Otherwise
    # "pleasanti" incorrectly returns a beer named "Pleasant Pils" instead of
    # correcting the current Pleasanti Street card.
    original_beer_name = pending.get("beer_name", "").strip()
    original_queries = []
    if original_beer_name:
        original_queries.append(original_beer_name)
        expanded_original = expand_search_abbreviations(original_beer_name)
        if expanded_original != original_beer_name:
            original_queries.append(expanded_original)

    if original_queries and brewery_matches:
        for original_query in original_queries:
            results = find_beer_candidates(ALL_BEERS, original_query, query_text, limit=5)
            if results:
                break

    # Prefer the safe matcher from search.py. It normalizes names and avoids
    # bad token_set_ratio matches like "my honning" -> "Lightning".
    if not results:
        results = find_beer_candidates(ALL_BEERS, query_text, brewery_hint, limit=5)

    # If the OCR brewery was wrong, retry globally by beer name only.
    if not results and brewery_hint:
        results = find_beer_candidates(ALL_BEERS, query_text, "", limit=5)

    expanded_query_text = expand_search_abbreviations(query_text)
    if not results and expanded_query_text != query_text:
        results = find_beer_candidates(ALL_BEERS, expanded_query_text, brewery_hint, limit=5)
        if not results and brewery_hint:
            results = find_beer_candidates(ALL_BEERS, expanded_query_text, "", limit=5)

    # If typed text was not recognized as a brewery at first, still try it as a
    # brewery hint after beer-name search fails. This keeps obscure/partial
    # brewery names useful without hijacking normal beer-name corrections.
    if not results and original_queries:
        for original_query in original_queries:
            results = find_beer_candidates(ALL_BEERS, original_query, query_text, limit=5)
            if results:
                break

    # As a final manual-search fallback, if the user's text looks like a
    # brewery, show beers from that brewery. This makes the prompt genuinely
    # accept either a beer name or a brewery name.
    if not results and brewery_matches:
        seen_ids = set()
        for _, beer in brewery_matches:
            beer_id = str(beer.get("id", ""))
            if beer_id and beer_id not in seen_ids:
                seen_ids.add(beer_id)
                results.append(beer)
            if len(results) >= 5:
                break

    # Keep a simple substring fallback for very short/special queries.
    if not results and (len(query_lower) < 6 or re.search(r'[#-]', query_lower)):
        substring_matches = [
            b for b in ALL_BEERS
            if query_lower in b.get("name", "").lower()
        ]
        results = substring_matches[:5]

    if not results:
        feedback = await update.message.reply_text(t(lng, "search_not_found"))
        pending.setdefault("feedback_msg_ids", []).append(feedback.message_id)
        pending["feedback_chat_id"] = chat_id
        return

    buttons = make_buttons(results)
    pick_msg = await update.message.reply_text(
        t(lng, "search_pick", count=len(results)),
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    pending.setdefault("pick_msg_ids", []).append(pick_msg.message_id)
    pending["pick_chat_id"] = chat_id


async def _apply_search_result(context, pending: dict, match: dict, user_id):
    beer_id = match["id"]
    untappd_url = (
        match.get("url") if match.get("url") and "/b/" in match.get("url", "")
        else f"https://untappd.com/beer/{match['id']}"
    )
    display_style = match.get("style") or pending.get("style", "")
    abv_str = pending.get("abv_str", "")
    lng = pending.get("lng", "en")
    msg_id = pending.get("msg_id", 0)

    sess = session_emoji(match)
    # HTML + h() (escape), not raw Markdown - a DB beer name containing a
    # backtick/underscore/asterisk would otherwise make edit_message_text
    # raise "can't parse entities"; with no global PTB error handler, that
    # update is silently dropped and this manual-search pick appears to do
    # nothing. Matches the pattern already used for live-search results
    # elsewhere in this same flow.
    text = f"🍺 <code>{h(match['name'])}</code>\n🏭 {h(match.get('brewery', ''))}"
    if display_style:
        text += f"\n🏷 {h(display_style)}{h(abv_str)}"
    if sess:
        text += f"\n{h(sess)} {h(match.get('session', ''))}"
    if match.get("location"):
        text += f"\n📍 {h(match.get('location'))}"

    context.bot_data[f"beer:{beer_id}"] = {
        "name": match["name"], "brewery": match.get("brewery", ""),
        "url": untappd_url, "location": match.get("location", "")
    }
    personal_keyboard = int(pending["chat_id"]) == int(user_id)

    keyboard = await beer_keyboard(
        beer_id,
        untappd_url,
        user_id,
        lng,
        msg_id,
        personal_state=personal_keyboard,
    )

    try:
        await context.bot.edit_message_text(
            chat_id=pending["chat_id"], message_id=msg_id,
            text=text, parse_mode="HTML",
            reply_markup=keyboard, disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Edit message error: {e}")
        await context.bot.send_message(
            chat_id=pending["chat_id"], text=text, parse_mode="HTML",
            reply_markup=keyboard, disable_web_page_preview=True
        )


# ── Main ──────────────────────────────────────────────────────────────────────

async def checkin_webapp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open the festival check-in Mini App (private chats only)."""
    lng = lang(update)
    if update.effective_chat.type != "private":
        await update.message.reply_text(t(lng, "checkin_webapp_group_hint"))
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
        t(lng, "btn_open_checkin_webapp"),
        web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/checkin"),
    )]])
    try:
        await update.message.reply_text(t(lng, "checkin_webapp_intro"), reply_markup=keyboard)
    except TelegramError as e:
        logger.warning(f"/checkin failed: {e}")


async def connect_untappd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start pairing the user's own Untappd account (private chats only)."""
    lng = lang(update)
    if update.effective_chat.type != "private":
        await update.message.reply_text(t(lng, "connect_untappd_group_hint"))
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    set_pending_untappd_token(context, user_id, chat_id, {})
    await update.message.reply_text(t(lng, "connect_untappd_prompt"))


async def import_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Seed had_it_index instantly from an official Untappd data export
    (CSV/JSON, Insider-only "Export Your Data" feature) instead of waiting
    for the slow paced background backfill. Private chats only."""
    lng = lang(update)
    if update.effective_chat.type != "private":
        await update.message.reply_text(t(lng, "import_history_group_hint"))
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not await user_tokens.get_token(user_id):
        await update.message.reply_text(t(lng, "import_history_not_connected"))
        return
    set_pending_import_history(context, user_id, chat_id, {})
    await update.message.reply_text(t(lng, "import_history_prompt"))


IMPORT_HISTORY_MAX_FILE_SIZE = 20 * 1024 * 1024  # Telegram's own bot-download ceiling


def _parse_import_entries(file_name: str, text: str) -> tuple[list[tuple[int, float | None]], int]:
    """Returns (entries, skipped_count). entries are (beerId, rating) pairs
    pulled from the export's `bid`/`rating_score` fields - same field names
    for both formats, since it's the same underlying Untappd export.
    Malformed/unrecognized rows are skipped and counted, never raised -
    a parsing surprise should degrade to "fewer rows imported than
    expected", not a crash."""
    def _row_entry(row: dict) -> tuple[int, float | None] | None:
        try:
            bid = int(row.get("bid"))
        except (TypeError, ValueError):
            return None
        rating_raw = row.get("rating_score")
        try:
            rating = float(rating_raw) if rating_raw not in (None, "") else None
        except (TypeError, ValueError):
            rating = None
        return bid, rating

    entries: list[tuple[int, float | None]] = []
    skipped = 0

    if file_name.endswith(".csv"):
        for row in csv.DictReader(io.StringIO(text)):
            parsed = _row_entry(row)
            if parsed is None:
                skipped += 1
            else:
                entries.append(parsed)
        return entries, skipped

    # .json - export's exact top-level shape wasn't confirmed against a real
    # sample this session (only the CSV's column names were), so accept
    # either a bare list of records or a dict with the list under a common
    # key - degrades to "0 imported" rather than crashing if this guess
    # about the shape is wrong.
    try:
        parsed_json = json.loads(text)
    except json.JSONDecodeError:
        return [], 0
    if isinstance(parsed_json, list):
        records = parsed_json
    elif isinstance(parsed_json, dict):
        records = next(
            (parsed_json[k] for k in ("checkins", "items", "data") if isinstance(parsed_json.get(k), list)),
            [],
        )
    else:
        records = []
    for row in records:
        if not isinstance(row, dict):
            skipped += 1
            continue
        parsed = _row_entry(row)
        if parsed is None:
            skipped += 1
        else:
            entries.append(parsed)
    return entries, skipped


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only acts while /import_history has set a pending state for this
    user - otherwise a random file upload is silently ignored, same
    not-pending-so-ignore convention handle_text's checks already use."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if get_pending_import_history(context, user_id, chat_id) is None:
        return
    lng = lang(update)
    document = update.message.document
    file_name = (document.file_name or "").lower()
    if not (file_name.endswith(".csv") or file_name.endswith(".json")):
        await update.message.reply_text(t(lng, "import_history_invalid_file"))
        return  # keep pending state so they can just try again
    if document.file_size and document.file_size > IMPORT_HISTORY_MAX_FILE_SIZE:
        await update.message.reply_text(t(lng, "import_history_invalid_file"))
        return

    clear_pending_import_history(context, user_id, chat_id)
    file = await context.bot.get_file(document.file_id)
    raw_bytes = await file.download_as_bytearray()
    text = bytes(raw_bytes).decode("utf-8", errors="replace")

    entries, skipped = _parse_import_entries(file_name, text)
    if not entries:
        await update.message.reply_text(t(lng, "import_history_invalid_file"))
        return

    profile = await user_tokens.get_profile(user_id)
    username = (profile or {}).get("username") or ""
    count = await had_it_index.seed_from_export(user_id, username, entries)
    skipped_note = t(lng, "import_history_skipped_note", skipped=skipped) if skipped else ""
    await update.message.reply_text(t(lng, "import_history_done", count=count, skipped_note=skipped_note))


async def auto_toast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage a personal "auto-toast" watch list - see auto_toast.py and
    webapp_server.py's _auto_toast_loop. Private chats only, same gating as
    the other Untappd-account commands. No args shows current status;
    subcommands add/remove targets, manage the country exclusion list, and
    pause/resume the whole thing (e.g. during a festival, when quota should
    go to real check-ins instead - pausing skips the owner's pairs out of
    the rotation entirely, without touching their target list or stats).
    A freshly-added target's existing history is never toasted - only
    check-ins made from now on (see auto_toast.peek_owner_turn)."""
    lng = lang(update)
    if update.effective_chat.type != "private":
        await update.message.reply_text(t(lng, "auto_toast_group_hint"))
        return
    user_id = update.effective_user.id
    if str(user_id) != AUTO_TOAST_OWNER_ID:
        await update.message.reply_text(t(lng, "auto_toast_owner_only"))
        return
    if not await user_tokens.get_token(user_id):
        await update.message.reply_text(t(lng, "auto_toast_not_connected"))
        return

    args = context.args
    if not args:
        config = await auto_toast.get_config(user_id)
        targets = config["targets"]
        excluded = config["excludedCountries"]
        if not targets:
            await update.message.reply_text(t(lng, "auto_toast_status_empty"))
            return
        stats = config["stats"]
        feed = config["feed"]
        lines = []
        grand_total = 0
        for username in targets:
            total = stats.get(username, {}).get("total_toasted", 0)
            grand_total += total
            lines.append(f"• {username} — {t(lng, 'auto_toast_status_line', total=total)}")
        excluded_text = ", ".join(excluded) if excluded else t(lng, "auto_toast_none")
        legacy_text = t(lng, "auto_toast_legacy_yes") if config["legacyOnly"] else t(lng, "auto_toast_legacy_no")

        # The feed (get_my_friend_feed) is polled once per owner, not once
        # per target - so "last checked"/"catching up" is one shared line
        # for the whole list, not per friend.
        polled_at = feed.get("last_polled_at")
        if polled_at:
            mins_ago = int((time.time() - polled_at) / 60)
            feed_line = t(lng, "auto_toast_feed_checked", mins=mins_ago)
            if feed.get("catchup_max_id") is not None:
                feed_line += f" {t(lng, 'auto_toast_catching_up')}"
        else:
            feed_line = t(lng, "auto_toast_status_pending")

        status_key = "auto_toast_status" if config["enabled"] else "auto_toast_status_paused"
        await update.message.reply_text(
            t(lng, status_key, targets="\n".join(lines), excluded=excluded_text,
              total=grand_total, feed=feed_line, legacy=legacy_text)
        )
        return

    sub = args[0].lower()
    rest = args[1:]

    if sub == "add":
        if not rest:
            await update.message.reply_text(t(lng, "auto_toast_usage"))
            return
        added = await auto_toast.add_targets(user_id, rest)
        if added:
            await update.message.reply_text(t(lng, "auto_toast_added", usernames=", ".join(added)))
        else:
            await update.message.reply_text(t(lng, "auto_toast_add_nothing"))
    elif sub == "remove":
        if not rest:
            await update.message.reply_text(t(lng, "auto_toast_usage"))
            return
        removed = await auto_toast.remove_target(user_id, rest[0])
        await update.message.reply_text(
            t(lng, "auto_toast_removed", username=rest[0]) if removed
            else t(lng, "auto_toast_remove_not_found", username=rest[0])
        )
    elif sub == "exclude":
        if not rest:
            await update.message.reply_text(t(lng, "auto_toast_usage"))
            return
        key = await auto_toast.exclude_country(user_id, " ".join(rest))
        await update.message.reply_text(t(lng, "auto_toast_excluded", country=key))
    elif sub == "include":
        if not rest:
            await update.message.reply_text(t(lng, "auto_toast_usage"))
            return
        removed = await auto_toast.include_country(user_id, " ".join(rest))
        await update.message.reply_text(
            t(lng, "auto_toast_included", country=" ".join(rest)) if removed
            else t(lng, "auto_toast_include_not_found", country=" ".join(rest))
        )
    elif sub == "pause":
        await auto_toast.set_enabled(user_id, False)
        await update.message.reply_text(t(lng, "auto_toast_paused"))
    elif sub == "resume":
        await auto_toast.set_enabled(user_id, True)
        await update.message.reply_text(t(lng, "auto_toast_resumed"))
    elif sub == "legacy_only":
        await auto_toast.set_legacy_only(user_id, True)
        await update.message.reply_text(t(lng, "auto_toast_legacy_only_on"))
    elif sub == "all_categories":
        await auto_toast.set_legacy_only(user_id, False)
        await update.message.reply_text(t(lng, "auto_toast_legacy_only_off"))
    else:
        await update.message.reply_text(t(lng, "auto_toast_usage"))


async def festival_watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Personal "festival novelty watch" - see festival_watch.py and
    webapp_server.py's _check_festival_novelty. Private chats only. No args
    shows status; `here` starts a native-Telegram location share (handled
    by handle_location below) to pin the watch point; `radius`/`on`/`off`
    manage the rest. Deliberately does NOT require a connected Untappd
    account - unlike auto_toast, this only ever reads the shared friend
    feed already being fetched for auto_toast, it never calls anything on
    its own, so there's nothing here that needs the caller's own token."""
    lng = lang(update)
    if update.effective_chat.type != "private":
        await update.message.reply_text(t(lng, "festival_watch_group_hint"))
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    args = context.args
    if not args:
        watch = await festival_watch.get_config(user_id)
        if watch["lat"] is None:
            await update.message.reply_text(t(lng, "festival_watch_status_no_location"))
            return
        status_key = "festival_watch_status_on" if watch["enabled"] else "festival_watch_status_off"
        label = watch["label"] or f"{watch['lat']:.5f}, {watch['lng']:.5f}"
        await update.message.reply_text(t(lng, status_key, label=label, radius=watch["radiusMeters"]))
        return

    sub = args[0].lower()
    rest = args[1:]

    if sub == "here":
        set_pending_festival_watch_location(context, user_id, chat_id, {})
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(t(lng, "btn_share_location"), request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True,
        )
        await update.message.reply_text(t(lng, "festival_watch_prompt_location"), reply_markup=keyboard)
    elif sub == "radius":
        if not rest or not rest[0].isdigit():
            await update.message.reply_text(t(lng, "festival_watch_usage"))
            return
        meters = int(rest[0])
        await festival_watch.set_radius(user_id, meters)
        await update.message.reply_text(t(lng, "festival_watch_radius_set", radius=meters))
    elif sub == "on":
        watch = await festival_watch.get_config(user_id)
        if watch["lat"] is None:
            await update.message.reply_text(t(lng, "festival_watch_no_location_yet"))
            return
        await festival_watch.set_enabled(user_id, True)
        await update.message.reply_text(t(lng, "festival_watch_on"))
    elif sub == "off":
        await festival_watch.set_enabled(user_id, False)
        await update.message.reply_text(t(lng, "festival_watch_off"))
    else:
        await update.message.reply_text(t(lng, "festival_watch_usage"))


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only acts on a shared location when /festival_watch here is pending
    (mirrors handle_document/handle_photo's pending-state-gated pattern) -
    sharing location for any other reason does nothing here."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    pending = get_pending_festival_watch_location(context, user_id, chat_id)
    if pending is None:
        return
    lng = lang(update)
    loc = update.message.location
    clear_pending_festival_watch_location(context, user_id, chat_id)
    await festival_watch.set_location(user_id, loc.latitude, loc.longitude)
    await update.message.reply_text(t(lng, "festival_watch_location_set"), reply_markup=ReplyKeyboardRemove())


async def comment_watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Personal "notify me when someone comments on my check-in, let me
    reply from the bot" - see comment_watch.py and webapp_server.py's
    _comment_watch_loop. Private chats only. Just on/off - no target list
    or location to configure, unlike auto_toast/festival_watch."""
    lng = lang(update)
    if update.effective_chat.type != "private":
        await update.message.reply_text(t(lng, "comment_watch_group_hint"))
        return
    user_id = update.effective_user.id
    if not await user_tokens.get_token(user_id):
        await update.message.reply_text(t(lng, "auto_toast_not_connected"))
        return

    args = context.args
    if not args:
        cfg = await comment_watch.get_config(user_id)
        status_key = "comment_watch_status_on" if cfg["enabled"] else "comment_watch_status_off"
        await update.message.reply_text(t(lng, status_key))
        return

    sub = args[0].lower()
    if sub == "on":
        await comment_watch.set_enabled(user_id, True)
        await update.message.reply_text(t(lng, "comment_watch_on"))
    elif sub == "off":
        await comment_watch.set_enabled(user_id, False)
        await update.message.reply_text(t(lng, "comment_watch_off"))
    else:
        await update.message.reply_text(t(lng, "comment_watch_usage"))


def private_commands_for(lng: str, *, include_auto_toast: bool = False):
    commands = [
        BotCommand("stats", t(lng, "cmd_stats")),
        BotCommand("todo", t(lng, "cmd_todo")),
        BotCommand("find", t(lng, "cmd_find")),
        BotCommand("clear", t(lng, "cmd_clear")),
        BotCommand("cancel", t(lng, "cmd_cancel")),
        BotCommand("checkin", t(lng, "cmd_checkin")),
        BotCommand("connect_untappd", t(lng, "cmd_connect_untappd")),
        BotCommand("import_history", t(lng, "cmd_import_history")),
    ]
    # Auto-toast is a personal test feature (see AUTO_TOAST_OWNER_ID) - only
    # listed in the owner's own command menu (BotCommandScopeChat in
    # post_init), not the default menu every private chat gets.
    if include_auto_toast:
        commands.append(BotCommand("auto_toast", t(lng, "cmd_auto_toast")))
    commands += [
        BotCommand("festival_watch", t(lng, "cmd_festival_watch")),
        BotCommand("comment_watch", t(lng, "cmd_comment_watch")),
    ]
    return commands


def status_commands_for(lng: str):
    return [
        BotCommand("go", t(lng, "cmd_go")),
        BotCommand("back", t(lng, "cmd_back")),
        BotCommand("status", t(lng, "cmd_status")),
        BotCommand("limited", t(lng, "cmd_limited")),
    ]


async def post_init(app):
    for lng in ("en", "uk", "ru"):
        await app.bot.set_my_commands(
            private_commands_for(lng),
            scope=BotCommandScopeAllPrivateChats(),
            language_code=None if lng == "en" else lng,
        )
        try:
            await app.bot.set_my_commands(
                private_commands_for(lng, include_auto_toast=True),
                scope=BotCommandScopeChat(chat_id=int(AUTO_TOAST_OWNER_ID)),
                language_code=None if lng == "en" else lng,
            )
        except Exception:
            # Owner hasn't started a private chat with the bot yet (or the
            # id is stale) - Telegram rejects BotCommandScopeChat for a
            # chat_id it has no record of. Not fatal: falls back to the
            # default menu (without auto_toast) until they do.
            logger.warning("Could not set owner-scoped commands for chat_id=%s", AUTO_TOAST_OWNER_ID, exc_info=True)
        await app.bot.set_my_commands(
            status_commands_for(lng),
            scope=BotCommandScopeAllGroupChats(),
            language_code=None if lng == "en" else lng,
        )

    start_limited_background_tasks(app)

    if os.environ.get("UNTAPPD_MCP_URL") and PUBLIC_BASE_URL:
        global _webapp_server_task
        from webapp_server import start_webapp_server
        _webapp_server_task = asyncio.create_task(start_webapp_server(app, ALL_BEERS, DATA_DIR, SESSIONS_RAW))
    else:
        logger.info("Festival check-in webapp disabled (UNTAPPD_MCP_URL/PUBLIC_BASE_URL not set)")

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("todo", todo_cmd))
    app.add_handler(CommandHandler("find", find_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("checkin", checkin_webapp_cmd))
    app.add_handler(CommandHandler("connect_untappd", connect_untappd_cmd))
    app.add_handler(CommandHandler("import_history", import_history_cmd))
    app.add_handler(CommandHandler("auto_toast", auto_toast_cmd))
    app.add_handler(CommandHandler("festival_watch", festival_watch_cmd))
    app.add_handler(CommandHandler("comment_watch", comment_watch_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    register_status_handlers(app)  # comment out to disable
    register_limited_handlers(app)  # comment out to disable
    logger.info(
        "Bot build %s started with %s beers in DB; vision_model=%s; "
        "vision_attempts=%s; trust_env=%s; vision_transport=%s; "
        "image_max_side=%s; image_target_kb=%s; data_dir=%s",
        BOT_BUILD,
        len(ALL_BEERS),
        VISION_MODEL,
        VISION_MAX_ATTEMPTS,
        VISION_HTTP_TRUST_ENV,
        VISION_TRANSPORT,
        VISION_IMAGE_MAX_SIDE,
        VISION_IMAGE_TARGET_KB,
        DATA_DIR,
    )
    app.run_polling()


if __name__ == "__main__":
    main()