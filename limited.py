"""
Limited pour reminders for group chats.

Commands:
- /limited <beer/brewery text> HH:MM
- /limited, then type <beer/brewery text> HH:MM

Stores data in BOT_DATA_DIR/DATA_DIR so Fly.io deploys do not wipe it.
"""

import os
import shutil
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from i18n import t
from status import escape_md, send_temp_reply, strip_bot_command_tail, try_delete_message

logger = logging.getLogger(__name__)


# ── Persistent data directory ────────────────────────────────────────────────

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


LIMITED_FILE = data_path("limited.json")
PINNED_LIMITED_FILE = data_path("pinned_limited.json")
for _state_file in ("limited.json", "pinned_limited.json"):
    migrate_legacy_data_file(_state_file)

PENDING_LIMITED_TTL_SECONDS = 5 * 60
REMINDER_MINUTES = (30, 15)
EXPIRE_STRIKE_DELAY_SECONDS = 60
SCHEDULER_POLL_SECONDS = 10
# Allow a small delay from polling/network jitter, but do not send reminders
# many minutes late after a deploy/restart. Missed reminders are marked as
# handled silently so the bot does not ping the chat at T-4 with a T-15 reminder.
MISSED_REMINDER_GRACE_SECONDS = 90
_limited_scheduler_task = None



def _timezone() -> ZoneInfo:
    tz_name = os.getenv("BOT_TIMEZONE") or os.getenv("TZ") or "Europe/Warsaw"
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown BOT_TIMEZONE/TZ=%r; falling back to UTC", tz_name)
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(_timezone())


def lang(update: Update) -> str:
    code = getattr(update.effective_user, "language_code", None) or "en"
    return code[:2].lower()


# ── Storage ───────────────────────────────────────────────────────────────────


def load_limited() -> dict:
    try:
        with open(LIMITED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.warning("Could not parse %s: %s", LIMITED_FILE, e)
        return {}


def save_limited(data: dict):
    with open(LIMITED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_pinned_limited() -> dict:
    try:
        with open(PINNED_LIMITED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.warning("Could not parse %s: %s", PINNED_LIMITED_FILE, e)
        return {}


def save_pinned_limited(data: dict):
    with open(PINNED_LIMITED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_pinned_limited_msg(chat_id: int) -> int | None:
    return load_pinned_limited().get(str(chat_id))


def set_pinned_limited_msg(chat_id: int, msg_id: int):
    data = load_pinned_limited()
    data[str(chat_id)] = msg_id
    save_pinned_limited(data)


def clear_pinned_limited_msg(chat_id: int):
    data = load_pinned_limited()
    data.pop(str(chat_id), None)
    save_pinned_limited(data)


def _chat_record(data: dict, chat_id: int, lng: str = "en") -> dict:
    cid = str(chat_id)
    record = data.get(cid)
    if not isinstance(record, dict) or "items" not in record:
        # Migration-friendly shape in case an older/manual file used a plain item dict.
        old_items = record if isinstance(record, dict) else {}
        record = {"lng": lng, "items": old_items if isinstance(old_items, dict) else {}}
        data[cid] = record
    record.setdefault("lng", lng)
    record.setdefault("items", {})
    return record


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_timezone())
    return dt.astimezone(_timezone())


# ── Pending /limited without args ─────────────────────────────────────────────


def _pending_limited_key(user_id: int, chat_id: int) -> str:
    return f"pending_limited:{user_id}:{chat_id}"


def get_pending_limited(context, user_id: int, chat_id: int) -> dict | None:
    key = _pending_limited_key(user_id, chat_id)
    pending = context.user_data.get(key)
    if not pending:
        return None
    if time.time() - pending.get("created_at", 0) > PENDING_LIMITED_TTL_SECONDS:
        context.user_data.pop(key, None)
        return None
    return pending


def set_pending_limited(
    context,
    user_id: int,
    chat_id: int,
    prompt_msg_id: int | None = None,
    command_msg_id: int | None = None,
):
    context.user_data[_pending_limited_key(user_id, chat_id)] = {
        "created_at": time.time(),
        "prompt_msg_id": prompt_msg_id,
        "command_msg_id": command_msg_id,
    }


def clear_pending_limited(context, user_id: int, chat_id: int):
    context.user_data.pop(_pending_limited_key(user_id, chat_id), None)


# ── Parsing / formatting ──────────────────────────────────────────────────────


TIME_AT_END_RE = re.compile(r"(?:^|\s)(?P<hour>[01]?\d|2[0-3])[:.](?P<minute>[0-5]\d)\s*$")


def parse_limited_text(text: str) -> tuple[str | None, datetime | None, str | None]:
    """Return (title, dt, error_key). Time is mandatory and must be at the end."""
    text = strip_bot_command_tail(text or "").strip()
    match = TIME_AT_END_RE.search(text)
    if not match:
        return None, None, "limited_missing_time"

    title = text[:match.start()].strip(" —-\t\n")
    if not title:
        return None, None, "limited_missing_title"

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    tz = _timezone()
    today = now_local().date()
    pour_time = datetime(today.year, today.month, today.day, hour, minute, tzinfo=tz)

    if pour_time <= now_local():
        return None, None, "limited_time_in_past"

    return re.sub(r"\s+", " ", title).strip(), pour_time, None


def _item_sort_key(item: dict):
    dt = _parse_dt(item.get("time")) or datetime.max.replace(tzinfo=_timezone())
    return (dt, str(item.get("title", "")).casefold())


def _is_expired(item: dict, now: datetime | None = None) -> bool:
    dt = _parse_dt(item.get("time"))
    if not dt:
        return True
    now = now or now_local()
    return now >= dt + timedelta(seconds=EXPIRE_STRIKE_DELAY_SECONDS)


def build_limited_text(chat_id: int, lng: str = "en") -> str:
    data = load_limited()
    record = _chat_record(data, chat_id, lng)
    items = list(record.get("items", {}).values())
    items.sort(key=_item_sort_key)

    lines = [t(lng, "limited_title")]
    if not items:
        lines.append("")
        lines.append(t(lng, "limited_empty"))
        return "\n".join(lines)

    now = now_local()
    for item in items:
        dt = _parse_dt(item.get("time"))
        time_label = escape_md(dt.strftime("%H:%M") if dt else item.get("time_label", "??:??"))
        title = escape_md(item.get("title", ""))
        creator = escape_md(item.get("creator", ""))
        expired = _is_expired(item, now)
        base = f"• {time_label} — {title}"
        if creator:
            base += f" {creator}" if expired else f" _{creator}_"
        if expired:
            base = f"~{base}~"
        lines.append(base)

    return "\n".join(lines)


async def _pin_limited_message(context, chat_id: int, message_id: int, lng: str = "en") -> bool:
    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=True,
        )
        return True
    except TelegramError as e:
        logger.info("Could not pin limited message: %s", e)
        return False


async def _notify_pin_failure_once(context, chat_id: int, lng: str):
    """Tell the chat once per process if the bot cannot pin the limited list."""
    key = f"limited_pin_failed_notice:{chat_id}"
    bot_data = getattr(context, "bot_data", None)
    if bot_data is not None and bot_data.get(key):
        return
    if bot_data is not None:
        bot_data[key] = True
    try:
        await context.bot.send_message(chat_id=chat_id, text=t(lng, "limited_pin_failed"))
    except TelegramError:
        pass


async def update_limited_message(
    context,
    chat_id: int,
    lng: str = "en",
    notify_pin_failure: bool = False,
) -> bool:
    """Create/update the pinned limited list. Returns True if pinning succeeded."""
    text = build_limited_text(chat_id, lng)
    existing_id = get_pinned_limited_msg(chat_id)

    if existing_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            pinned = await _pin_limited_message(context, chat_id, existing_id, lng)
            if notify_pin_failure and not pinned:
                await _notify_pin_failure_once(context, chat_id, lng)
            return pinned
        except TelegramError as e:
            if "message is not modified" in str(e).lower():
                pinned = await _pin_limited_message(context, chat_id, existing_id, lng)
                if notify_pin_failure and not pinned:
                    await _notify_pin_failure_once(context, chat_id, lng)
                return pinned
            logger.warning("Limited message edit failed: %s", e)
            clear_pinned_limited_msg(chat_id)

    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        set_pinned_limited_msg(chat_id, msg.message_id)
        pinned = await _pin_limited_message(context, chat_id, msg.message_id, lng)
        if notify_pin_failure and not pinned:
            await _notify_pin_failure_once(context, chat_id, lng)
        return pinned
    except TelegramError as e:
        logger.error("Could not send limited message: %s", e)
        return False

# ── Jobs ──────────────────────────────────────────────────────────────────────


def _job_name(chat_id: int, item_id: str, kind: str) -> str:
    return f"limited:{chat_id}:{item_id}:{kind}"


def schedule_item_jobs(context_or_app, chat_id: int, item_id: str, item: dict):
    """Compatibility no-op. Reminders are handled by the background scheduler loop."""
    return


async def limited_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data_obj = context.job.data or {}
    chat_id = int(data_obj["chat_id"])
    item_id = str(data_obj["item_id"])
    minutes = int(data_obj["minutes"])
    lng = data_obj.get("lng") or "en"

    data = load_limited()
    record = _chat_record(data, chat_id, lng)
    item = record.get("items", {}).get(item_id)
    if not item:
        return

    dt = _parse_dt(item.get("time"))
    if not dt or now_local() >= dt:
        return

    item[f"reminded_{minutes}"] = True
    save_limited(data)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=t(
                lng,
                "limited_reminder",
                minutes=minutes,
                title=item.get("title", ""),
                time=dt.strftime("%H:%M"),
            ),
        )
    except TelegramError as e:
        logger.warning("Could not send limited reminder: %s", e)


async def limited_expire_job(context: ContextTypes.DEFAULT_TYPE):
    data_obj = context.job.data or {}
    chat_id = int(data_obj["chat_id"])
    lng = data_obj.get("lng") or "en"
    await update_limited_message(context, chat_id, lng)


async def limited_startup_refresh_job(context: ContextTypes.DEFAULT_TYPE):
    data = load_limited()
    for cid, record in data.items():
        try:
            chat_id = int(cid)
        except (TypeError, ValueError):
            continue
        lng = record.get("lng") if isinstance(record, dict) else "en"
        await update_limited_message(context, chat_id, lng or "en")


def schedule_existing_limited_jobs(app):
    """Compatibility no-op. Existing limited pours are picked up by the background scheduler."""
    return


def _item_created_at(item: dict) -> datetime | None:
    return _parse_dt(item.get("created_at"))


def _reminder_state(item: dict, minutes: int, now: datetime) -> str:
    """Return pending | fire | missed | skip for a reminder.

    The scheduler polls every few seconds, so sending shortly after the exact
    reminder moment is fine. But after a deploy/restart we must not send stale
    reminders many minutes late. For example, if a 20:36 pour missed its 20:21
    reminder, do not send that 15-min reminder at 20:32.
    """
    if item.get(f"reminded_{minutes}"):
        return "skip"

    dt = _parse_dt(item.get("time"))
    if not dt or now >= dt:
        return "missed"

    remind_at = dt - timedelta(minutes=minutes)
    created_at = _item_created_at(item)

    # If the item was created after this reminder moment, skip it.
    # Example: a 20:36 limited pour added at 20:22 should not emit the 15-min reminder.
    if created_at and created_at > remind_at:
        return "missed"

    if now < remind_at:
        return "pending"

    if now <= remind_at + timedelta(seconds=MISSED_REMINDER_GRACE_SECONDS):
        return "fire"

    return "missed"


async def _send_due_limited_reminders(app, data: dict) -> bool:
    changed = False
    now = now_local()

    for cid, record in list(data.items()):
        try:
            chat_id = int(cid)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        lng = record.get("lng") or "en"
        items = record.get("items", {}) if isinstance(record.get("items", {}), dict) else {}

        for item_id, item in list(items.items()):
            if not isinstance(item, dict):
                continue
            dt = _parse_dt(item.get("time"))
            if not dt:
                continue

            for minutes in REMINDER_MINUTES:
                state = _reminder_state(item, minutes, now)

                if state in {"skip", "pending"}:
                    continue

                if state == "missed":
                    item[f"reminded_{minutes}"] = True
                    changed = True
                    logger.info(
                        "Skipped stale/missed limited %s-min reminder for chat=%s item=%s",
                        minutes,
                        chat_id,
                        item_id,
                    )
                    continue

                if state == "fire":
                    item[f"reminded_{minutes}"] = True
                    changed = True
                    try:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=t(
                                lng,
                                "limited_reminder",
                                minutes=minutes,
                                title=item.get("title", ""),
                                time=dt.strftime("%H:%M"),
                            ),
                        )
                        logger.info("Sent limited %s-min reminder for chat=%s item=%s", minutes, chat_id, item_id)
                    except TelegramError as e:
                        logger.warning("Could not send limited reminder: %s", e)

            if _is_expired(item, now) and not item.get("expired_marked"):
                item["expired_marked"] = True
                changed = True
                try:
                    await update_limited_message(app, chat_id, lng)
                    logger.info("Marked limited item expired for chat=%s item=%s", chat_id, item_id)
                except Exception as e:
                    logger.warning("Could not update limited list after expiry: %s", e)

    return changed


async def limited_background_scheduler_loop(app):
    await asyncio.sleep(2)
    logger.info("Limited background scheduler started; timezone=%s", _timezone().key)

    # Refresh the pinned lists once after startup so already-expired items are struck through.
    try:
        data = load_limited()
        for cid, record in data.items():
            try:
                chat_id = int(cid)
            except (TypeError, ValueError):
                continue
            lng = record.get("lng") if isinstance(record, dict) else "en"
            await update_limited_message(app, chat_id, lng or "en")
    except Exception as e:
        logger.warning("Limited startup refresh failed: %s", e)

    while True:
        try:
            data = load_limited()
            changed = await _send_due_limited_reminders(app, data)
            if changed:
                save_limited(data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Limited scheduler loop error: %s", e)
        await asyncio.sleep(SCHEDULER_POLL_SECONDS)


def start_limited_background_tasks(app):
    global _limited_scheduler_task
    if _limited_scheduler_task and not _limited_scheduler_task.done():
        return
    _limited_scheduler_task = asyncio.create_task(limited_background_scheduler_loop(app))


# ── Core logic ────────────────────────────────────────────────────────────────


async def _do_limited(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> bool:
    user = update.effective_user
    chat_id = update.effective_chat.id
    lng = lang(update)

    title, pour_time, error_key = parse_limited_text(raw_text)
    if error_key:
        await send_temp_reply(update, context, t(lng, error_key))
        return False

    data = load_limited()
    record = _chat_record(data, chat_id, lng)
    record["lng"] = lng
    item_id = f"{int(time.time() * 1000)}:{user.id}"
    creator = f"@{user.username}" if user.username else user.first_name

    item = {
        "id": item_id,
        "title": title,
        "time": pour_time.isoformat(),
        "time_label": pour_time.strftime("%H:%M"),
        "creator": creator,
        "created_at": now_local().isoformat(),
        "lng": lng,
        "reminded_30": False,
        "reminded_15": False,
    }
    record["items"][item_id] = item
    save_limited(data)

    await update_limited_message(context, chat_id, lng, notify_pin_failure=True)
    return True


# ── Handlers ──────────────────────────────────────────────────────────────────


async def limited_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    lng = lang(update)

    clear_pending_limited(context, user.id, chat_id)

    if context.args:
        raw_text = " ".join(context.args).strip()
        handled = await _do_limited(update, context, raw_text)
        if handled:
            await try_delete_message(context, chat_id, update.message.message_id)
        return

    prompt = await update.message.reply_text(t(lng, "limited_prompt"))
    set_pending_limited(
        context=context,
        user_id=user.id,
        chat_id=chat_id,
        prompt_msg_id=prompt.message_id,
        command_msg_id=update.message.message_id,
    )
    await try_delete_message(context, chat_id, update.message.message_id)


async def handle_limited_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.text:
        return False

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    pending = get_pending_limited(context, user_id, chat_id)
    if not pending:
        return False

    text = strip_bot_command_tail(update.message.text.strip())
    if text.startswith("/"):
        clear_pending_limited(context, user_id, chat_id)
        return False

    handled = await _do_limited(update, context, text)
    if handled:
        clear_pending_limited(context, user_id, chat_id)
        await try_delete_message(context, chat_id, pending.get("prompt_msg_id"))
        await try_delete_message(context, chat_id, pending.get("command_msg_id"))
        await try_delete_message(context, chat_id, update.message.message_id)
    return handled


async def limited_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    handled = await handle_limited_text(update, context)
    if handled:
        raise ApplicationHandlerStop


# ── Register ──────────────────────────────────────────────────────────────────


def register_limited_handlers(app):
    group_only = filters.ChatType.GROUPS
    app.add_handler(CommandHandler("limited", limited_cmd, filters=group_only))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & group_only, limited_text_handler),
        group=-2,
    )
    schedule_existing_limited_jobs(app)
    logger.info("Limited handlers registered for groups only; timezone=%s; background scheduler starts in post_init", _timezone().key)


LIMITED_COMMANDS = [
    BotCommand("limited", t("en", "cmd_limited")),
]
