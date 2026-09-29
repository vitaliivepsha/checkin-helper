"""Owner-controlled per-command / per-feature visibility for regular users.

Lets the bot owner turn individual commands (and the direct-photo-to-chat
recognition flow) on/off for everyone EXCEPT themselves, from the Mini
App's owner-only "Керування функціями" screen (see webapp_server.py's
handle_command_flags_get/handle_command_flags_set) - a lighter-weight
alternative to actually removing a feature from the codebase when it's
not ready, noisy, or just not wanted for regular users right now. The
owner (AUTO_TOAST_OWNER_ID) is never affected by any of these flags - see
bot.py's _is_owner/_require_command_enabled, which always let the owner
through regardless of what's toggled off here.

A command missing from the stored file defaults to enabled (True) - a
command added to bot.py's own TOGGLEABLE_COMMANDS list with no flag saved
yet is available until the owner deliberately turns it off, not silently
disabled by omission. Disabled commands are also dropped from the
Telegram command menu (see bot.py's private_commands_for/
status_commands_for) - a belt-and-suspenders pairing with the handler-side
check, same "not shown AND not just cosmetic" pattern as every owner-only
gate elsewhere in this project.

Also stores a custom display ORDER for the same command list (drag-to-
reorder in the Mini App screen - see set_order, and get_all's own "order"
field), applied to both that screen's own row order and the real
Telegram command menu (see bot.py's _apply_order).

Same shape as maintenance_mode.py/festival_mode.py: module-level `_path`,
`asyncio.Lock`, `init(data_dir)`, atomic tmp-file + os.replace() writes,
reads also async+locked for the same consistency-under-concurrent-write
reason those modules are.
"""

import asyncio
import json
import os

# Not real command names (Telegram commands can't contain double
# underscores meaningfully like this) - reserved keys for the one
# non-command feature this module also gates (the direct-photo-to-chat
# beer recognition flow - see bot.py's handle_photo/_process_photo_messages)
# and the Mini App's drag-to-reorder command list (see get_order/set_order).
PHOTO_RECOGNITION_KEY = "__photo_recognition__"
ORDER_KEY = "__order__"

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "feature_flags.json")


def _load() -> dict:
    if not _path or not os.path.exists(_path):
        return {}
    try:
        with open(_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    tmp_path = _path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _path)


async def is_enabled(key: str) -> bool:
    """True unless the owner has explicitly toggled `key` (a command name,
    or PHOTO_RECOGNITION_KEY) off."""
    async with _lock:
        return bool(_load().get(key, True))


async def set_enabled(key: str, enabled: bool) -> None:
    async with _lock:
        data = _load()
        data[key] = enabled
        _save(data)


async def set_order(order: list[str]) -> None:
    async with _lock:
        data = _load()
        data[ORDER_KEY] = order
        _save(data)


async def get_all(known_commands: list[str]) -> dict:
    """{"commands": {cmd: bool, ...}, "photoRecognition": bool, "order":
    [cmd, ...]} for every command in `known_commands` - the Mini App
    settings screen's one-call snapshot of current state, so it doesn't
    need is_enabled() once per row. `order` always lists every command in
    `known_commands` exactly once: a saved order (from the Mini App's
    drag-to-reorder) has any command that's no longer registered dropped,
    and any newly-registered command not yet in it appended at the end -
    reconciled here rather than stored stale, so a command added to
    bot.py's own TOGGLEABLE_COMMANDS list later doesn't silently vanish
    from a saved custom order, or crash sorting against it."""
    async with _lock:
        data = _load()
        known_set = set(known_commands)
        saved_order = data.get(ORDER_KEY)
        if isinstance(saved_order, list):
            order = [c for c in saved_order if c in known_set]
            order += [c for c in known_commands if c not in order]
        else:
            order = list(known_commands)
        return {
            "commands": {cmd: bool(data.get(cmd, True)) for cmd in known_commands},
            "photoRecognition": bool(data.get(PHOTO_RECOGNITION_KEY, True)),
            "order": order,
        }
