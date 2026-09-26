"""Per-user "already got this special badge" dismissals for the badges
screen's special-badges section (see badge_stats.compute_special_badges) -
purely a display filter, has NOTHING to do with whether the badge was
actually earned (this app has no way to confirm that against Untappd -
see that function's own docstring on why). Tapping "Вже отримав" just
hides that badge's card for this user until it naturally drops off the
active list on its own (activeUntil passes) - a badge NAME reused in a
future year (e.g. "Sour Beer Day (2027)") is a different string, so a
stale dismissal never accidentally hides next year's edition.

Same shape as wishlist_items.py: flat dict keyed by str(user_id) -> list
of dismissed badge names, module-level `_path`/`asyncio.Lock`,
init(data_dir), atomic tmp-file + os.replace() writes.
"""

import asyncio
import json
import os

_path: str | None = None
_lock = asyncio.Lock()


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "special_badge_dismissals.json")


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


async def get_dismissed(user_id: int) -> set:
    async with _lock:
        return set(_load().get(str(user_id), []))


async def dismiss(user_id: int, badge_name: str) -> None:
    async with _lock:
        data = _load()
        names = set(data.get(str(user_id), []))
        names.add(badge_name)
        data[str(user_id)] = sorted(names)
        _save(data)
