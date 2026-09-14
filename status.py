"""
Beer run status module — experimental.
Commands: /go [brewery], /back, /status, /clearstatus
To disable: comment out register_status_handlers(app) in bot.py
"""

import os
import shutil
import json
import logging
import re
import time
import asyncio
from datetime import datetime

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.error import TelegramError
from i18n import t

from telegram.ext import (
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)


# ── Persistent data directory ────────────────────────────────────────────────
# Keep status JSON on the same persistent volume as checkins.json.
DATA_DIR = os.path.abspath(os.getenv("BOT_DATA_DIR") or os.getenv("DATA_DIR") or ".")
os.makedirs(DATA_DIR, exist_ok=True)


def data_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


def migrate_legacy_data_file(filename: str):
    old_path = os.path.abspath(filename)
    new_path = os.path.abspath(data_path(filename))
    if old_path == new_path or os.path.exists(new_path) or not os.path.exists(old_path):
        return
    try:
        shutil.copy2(old_path, new_path)
        logger.info("Migrated %s -> %s", filename, new_path)
    except Exception as e:
        logger.warning("Could not migrate %s -> %s: %s", filename, new_path, e)


def lang(update: Update) -> str:
    code = getattr(update.effective_user, "language_code", None) or "en"
    return code[:2].lower()


STATUS_FILE = data_path("status.json")
PINNED_STATUS_FILE = data_path("pinned_status.json")
for _state_file in ("status.json", "pinned_status.json"):
    migrate_legacy_data_file(_state_file)

PENDING_GO_TTL_SECONDS = 5 * 60
TEMP_MESSAGE_DELETE_DELAY_SECONDS = 10


# ── MarkdownV2 helpers ────────────────────────────────────────────────────────

def escape_md(text: str | None) -> str:
    """
    Escape dynamic text for Telegram MarkdownV2.

    Keep Markdown syntax itself outside this function, for example:
    f"*{escape_md(title)}*"
    """
    if text is None:
        return ""

    return re.sub(r'([\\_*\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))


def strip_bot_command_tail(text: str) -> str:
    """
    Convert Telegram group commands like:
    /go@UntappdCheckinHelperBot lervig -> /go lervig
    /back@UntappdCheckinHelperBot -> /back
    """
    if not text:
        return ""

    return re.sub(
        r"^(/[\w]+)@[A-Za-z0-9_]+(?=\s|$)",
        r"\1",
        text,
    ).strip()


async def delete_message_later(
    context,
    chat_id: int,
    message_id: int | None,
    delay_seconds: int = TEMP_MESSAGE_DELETE_DELAY_SECONDS,
):
    if not message_id:
        return

    try:
        await asyncio.sleep(delay_seconds)
        await try_delete_message(context, chat_id, message_id)
    except Exception as e:
        logger.debug(f"Could not auto-delete message {message_id}: {e}")


async def send_temp_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    delay_seconds: int = TEMP_MESSAGE_DELETE_DELAY_SECONDS,
):
    msg = await update.message.reply_text(text)

    asyncio.create_task(
        delete_message_later(
            context=context,
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            delay_seconds=delay_seconds,
        )
    )

    return msg


# ── Storage ───────────────────────────────────────────────────────────────────

def load_status() -> dict:
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_status(data: dict):
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_pinned_status() -> dict:
    try:
        with open(PINNED_STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_pinned_status(data: dict):
    with open(PINNED_STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_pinned_status_msg(chat_id: int) -> int | None:
    return load_pinned_status().get(str(chat_id))


def set_pinned_status_msg(chat_id: int, msg_id: int):
    data = load_pinned_status()
    data[str(chat_id)] = msg_id
    save_pinned_status(data)


def clear_pinned_status_msg(chat_id: int):
    data = load_pinned_status()
    data.pop(str(chat_id), None)
    save_pinned_status(data)


# ── Pending go state — keyed by user_id + chat_id ─────────────────────────────

def _pending_go_key(user_id: int, chat_id: int) -> str:
    return f"pending_go:{user_id}:{chat_id}"


def get_pending_go(context, user_id: int, chat_id: int) -> dict | None:
    key = _pending_go_key(user_id, chat_id)
    pending = context.user_data.get(key)

    if not pending:
        return None

    created_at = pending.get("created_at", 0)

    if time.time() - created_at > PENDING_GO_TTL_SECONDS:
        context.user_data.pop(key, None)
        return None

    return pending


def set_pending_go(
    context,
    user_id: int,
    chat_id: int,
    prompt_msg_id: int | None = None,
    command_msg_id: int | None = None,
):
    key = _pending_go_key(user_id, chat_id)

    context.user_data[key] = {
        "created_at": time.time(),
        "prompt_msg_id": prompt_msg_id,
        "command_msg_id": command_msg_id,
    }


def clear_pending_go(context, user_id: int, chat_id: int):
    key = _pending_go_key(user_id, chat_id)
    context.user_data.pop(key, None)


# ── Build status text ─────────────────────────────────────────────────────────

def build_status_text(chat_id: int, lng: str = "en") -> str:
    data = load_status()
    chat_data = data.get(str(chat_id), {})

    if not chat_data:
        return f"{t(lng, 'status_title')}\n\n{t(lng, 'status_empty')}"

    lines = [f"{t(lng, 'status_title')}\n"]

    active = {uid: v for uid, v in chat_data.items() if v.get("active")}
    returned = {uid: v for uid, v in chat_data.items() if not v.get("active")}

    if active:
        for uid, v in active.items():
            name = escape_md(v.get("name", ""))
            brewery_name = escape_md(v.get("brewery", ""))
            ts = escape_md(v.get("ts", ""))

            brewery = f" → *{brewery_name}*" if brewery_name else ""
            lines.append(f"🙋 {name}{brewery} _{ts}_")

    if returned:
        for uid, v in returned.items():
            name = escape_md(v.get("name", ""))
            ts = escape_md(v.get("ts", ""))

            lines.append(f"✅ {name} — {escape_md(t(lng, 'status_returned'))} _{ts}_")

    return "\n".join(lines)


# ── Update pinned status message ──────────────────────────────────────────────

async def update_status_message(context, chat_id: int, lng: str = "en"):
    text = build_status_text(chat_id, lng)
    existing_id = get_pinned_status_msg(chat_id)

    if existing_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        except TelegramError as e:
            if "message is not modified" in str(e).lower():
                return

            logger.warning(f"Status edit failed: {e}")
            clear_pinned_status_msg(chat_id)

    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        set_pinned_status_msg(chat_id, msg.message_id)

        try:
            await context.bot.pin_chat_message(
                chat_id=chat_id,
                message_id=msg.message_id,
                disable_notification=True,
            )
        except TelegramError as e:
            logger.info(f"Could not pin status: {e}")

    except TelegramError as e:
        logger.error(f"Could not send status: {e}")


# ── Optional cleanup ──────────────────────────────────────────────────────────

async def try_delete_message(context, chat_id: int, message_id: int | None):
    if not message_id:
        return

    try:
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
    except TelegramError as e:
        logger.debug(f"Could not delete message {message_id}: {e}")


# ── Core go logic ─────────────────────────────────────────────────────────────

async def _do_go(update: Update, context: ContextTypes.DEFAULT_TYPE, brewery: str):
    user = update.effective_user
    chat_id = update.effective_chat.id

    brewery = brewery.strip()

    data = load_status()
    cid = str(chat_id)

    if cid not in data:
        data[cid] = {}

    name = f"@{user.username}" if user.username else user.first_name

    data[cid][str(user.id)] = {
        "name": name,
        "brewery": brewery,
        "ts": datetime.now().strftime("%H:%M"),
        "active": True,
    }

    save_status(data)

    await update_status_message(context, chat_id, lang(update))


# ── Handlers ──────────────────────────────────────────────────────────────────

async def go_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id

    clear_pending_go(context, user_id, chat_id)

    # Case 1:
    # /go lervig
    # /go@UntappdCheckinHelperBot lervig
    #
    # Directly update pinned status.
    if context.args:
        brewery = " ".join(context.args).strip()
        brewery = strip_bot_command_tail(brewery)

        await _do_go(update, context, brewery)

        # Remove noisy command message like:
        # /go@UntappdCheckinHelperBot lervig
        await try_delete_message(
            context=context,
            chat_id=chat_id,
            message_id=update.message.message_id,
        )
        return

    # Case 2:
    # /go
    # /go@UntappdCheckinHelperBot
    #
    # Called from Telegram menu. Ask without ForceReply.
    name = f"@{user.username}" if user.username else user.first_name

    prompt = await update.message.reply_text(
        t(lang(update), "status_go_prompt", name=name)
    )

    set_pending_go(
        context=context,
        user_id=user_id,
        chat_id=chat_id,
        prompt_msg_id=prompt.message_id,
        command_msg_id=update.message.message_id,
    )

    # Remove noisy command message like:
    # /go@UntappdCheckinHelperBot
    await try_delete_message(
        context=context,
        chat_id=chat_id,
        message_id=update.message.message_id,
    )


async def back_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    clear_pending_go(context, user.id, chat_id)

    data = load_status()
    cid = str(chat_id)
    uid = str(user.id)

    if cid not in data or uid not in data[cid]:
        await update.message.reply_text(t(lang(update), "status_not_gone"))

        await try_delete_message(
            context=context,
            chat_id=chat_id,
            message_id=update.message.message_id,
        )
        return

    data[cid][uid]["active"] = False
    data[cid][uid]["ts"] = datetime.now().strftime("%H:%M")

    save_status(data)

    await update_status_message(context, chat_id, lang(update))

    # Remove noisy command message like:
    # /back@UntappdCheckinHelperBot
    await try_delete_message(
        context=context,
        chat_id=chat_id,
        message_id=update.message.message_id,
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    clear_pending_go(context, user.id, chat_id)

    existing_id = get_pinned_status_msg(chat_id)

    await update_status_message(context, chat_id, lang(update))

    # If status message already existed, do not create extra status copies.
    # Just tell the user to check the pinned message.
    if existing_id:
        await send_temp_reply(
            update=update,
            context=context,
            text=t(lang(update), "status_see_pinned"),
        )

    # Remove noisy command message like:
    # /status@UntappdCheckinHelperBot
    await try_delete_message(
        context=context,
        chat_id=chat_id,
        message_id=update.message.message_id,
    )


async def clear_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    clear_pending_go(context, user.id, chat_id)

    data = load_status()
    data[str(chat_id)] = {}

    save_status(data)

    await update_status_message(context, chat_id, lang(update))

    # Remove noisy command message like:
    # /clearstatus@UntappdCheckinHelperBot
    await try_delete_message(
        context=context,
        chat_id=chat_id,
        message_id=update.message.message_id,
    )


# ── Text handler for /go without brewery ──────────────────────────────────────

async def handle_status_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Handle the next text message after /go without arguments.

    Returns True if handled.
    """
    if not update.message or not update.message.text:
        return False

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    pending = get_pending_go(context, user_id, chat_id)

    if not pending:
        return False

    text = update.message.text.strip()
    text = strip_bot_command_tail(text)

    # Do not consume commands as brewery names.
    # Examples:
    # /back
    # /back@UntappdCheckinHelperBot
    # /go
    # /go@UntappdCheckinHelperBot
    if text.startswith("/"):
        clear_pending_go(context, user_id, chat_id)
        return False

    brewery = text

    clear_pending_go(context, user_id, chat_id)

    await _do_go(update, context, brewery)

    # Optional cleanup to reduce chat noise.
    # Works only if the bot has permission to delete messages in the chat.
    await try_delete_message(
        context=context,
        chat_id=chat_id,
        message_id=pending.get("prompt_msg_id"),
    )

    await try_delete_message(
        context=context,
        chat_id=chat_id,
        message_id=pending.get("command_msg_id"),
    )

    await try_delete_message(
        context=context,
        chat_id=chat_id,
        message_id=update.message.message_id,
    )

    return True


async def status_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Wrapper for PTB MessageHandler.

    If this module consumed the message, stop other handlers from processing it.
    """
    handled = await handle_status_text(update, context)

    if handled:
        raise ApplicationHandlerStop


# ── Register ──────────────────────────────────────────────────────────────────

def register_status_handlers(app):
    group_only = filters.ChatType.GROUPS

    app.add_handler(CommandHandler("go", go_cmd, filters=group_only))
    app.add_handler(CommandHandler("back", back_cmd, filters=group_only))
    app.add_handler(CommandHandler("status", status_cmd, filters=group_only))
    app.add_handler(CommandHandler("clearstatus", clear_status_cmd, filters=group_only))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & group_only, status_text_handler),
        group=-1,
    )

    logger.info("Status handlers registered for groups only")


STATUS_COMMANDS = [
    BotCommand("go", t("en", "cmd_go")),
    BotCommand("back", t("en", "cmd_back")),
    BotCommand("status", t("en", "cmd_status")),
]