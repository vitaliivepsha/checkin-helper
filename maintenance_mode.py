"""Global "we're doing planned work" switch, checked by the Mini App's own
front page before it renders anything else - shows a splash screen instead
of the normal UI for every viewer while enabled (see webapp_server.py's
handle_maintenance_get and app.js's maintenanceGate). GLOBAL, not per-user
(unlike festival_mode.py's own pause switch) - maintenance is about the
service itself, not any one connected account's quota.

Only ever helps while the bot process is actually alive and serving
requests - it cannot show anything once the process has genuinely crashed
(there's no code left running to render a splash then). For that case, see
watchdog.ps1's own docstring instead.

Same shape as festival_mode.py: module-level `_path`, `asyncio.Lock`,
`init(data_dir)`, atomic tmp-file + os.replace() writes.
"""

import asyncio
import json
import os

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "maintenance_mode.json")


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


async def get_status() -> dict:
    async with _lock:
        data = _load()
        return {"enabled": bool(data.get("enabled", False)), "message": data.get("message")}


async def set_enabled(enabled: bool, message: str | None = None) -> None:
    async with _lock:
        data = _load()
        data["enabled"] = enabled
        if message is not None:
            data["message"] = message
        _save(data)
