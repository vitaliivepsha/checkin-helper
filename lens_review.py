"""The lens's review queue: once a day the bot asks the owner, in Telegram, about the few shop
products whose match is most in doubt - questionable matches (lens_log's "suspicious") and probable
(🤔) ones - with ✅ right / ❌ wrong / ⏭ skip under each. A decision lands in lens_confirmations or
lens_rejections (the same stores the userscript's ✓/✕ write), and the product is flagged for the next
crawl to re-resolve, so a minute on a phone a day replaces browsing shops for wrong matches.

State (lens_review.json, same JSON-store shape as the others): which items were asked about
recently (not asked again for RESEND_AFTER), and the pending items behind each button - the button's
callback data carries only a short id, Telegram limits it to 64 bytes."""

import asyncio
import hashlib
import html
import json
import os
import time

import lens_confirmations
import lens_log
import lens_rejections
from i18n import t

_path: str | None = None
_lock = asyncio.Lock()
_mirror: dict | None = None

REVIEW_BATCH = 5                    # questions per day
REVIEW_INTERVAL_SECONDS = 22 * 3600
RESEND_AFTER_SECONDS = 14 * 24 * 3600
ITEM_TTL_SECONDS = 30 * 24 * 3600   # a button older than this answers "too old"


def init(data_dir: str) -> None:
    global _path, _mirror
    _path = os.path.join(data_dir, "lens_review.json")
    _mirror = None


def _load() -> dict:
    global _mirror
    if _mirror is not None:
        return _mirror
    _mirror = {"items": {}, "sent": {}, "lastRun": 0}
    if _path and os.path.exists(_path):
        try:
            with open(_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _mirror.update({k: loaded[k] for k in ("items", "sent", "lastRun") if k in loaded})
        except (json.JSONDecodeError, OSError):
            pass
    return _mirror


def _save() -> None:
    tmp = _path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_mirror, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, _path)


def _item_id(key: str, bid: int) -> str:
    return hashlib.sha1(f"{key}#{bid}".encode("utf-8")).hexdigest()[:10]


def _target(entry: dict) -> dict | None:
    """The beer the question is about: the logged match, or the probable candidate."""
    if entry.get("outcome") == "matched":
        if entry.get("bid") is None:
            return None
        return {"bid": entry["bid"], "name": entry.get("matchedName") or "", "brewery": entry.get("matchedBrewery") or "",
                "style": "", "rating": None, "kind": "suspicious"}
    probable = entry.get("probable")
    if not probable or probable.get("bid") is None:
        return None
    return {"bid": probable["bid"], "name": probable.get("name") or "", "brewery": probable.get("brewery") or "",
            "style": probable.get("style") or "", "rating": probable.get("rating"), "kind": "probable"}


def last_run() -> float:
    return _load().get("lastRun", 0)


async def pick(limit: int = REVIEW_BATCH) -> list[dict]:
    """Up to `limit` questions, most-looked-at first, skipping products already decided or asked
    about recently. Each: {"id", "queryBrewery", "queryName", "bid", "found", "foundBrewery", "style",
    "rating", "kind", "via"}."""
    now = time.time()
    sent = _load()["sent"]

    def skip(entry: dict) -> bool:
        target = _target(entry)
        if target is None:
            return True
        brewery, name = entry.get("queryBrewery") or "", entry.get("queryName") or ""
        if target["bid"] in lens_rejections.rejected_bids(brewery, name):
            return True
        if lens_confirmations.confirmed_bid(brewery, name) == target["bid"]:
            return True
        return now - sent.get(_item_id(entry.get("key", ""), target["bid"]), 0) < RESEND_AFTER_SECONDS

    entries = await lens_log.review_candidates(limit, skip)
    out = []
    for entry in entries:
        target = _target(entry)
        out.append({
            "id": _item_id(entry["key"], target["bid"]), "queryBrewery": entry.get("queryBrewery") or "",
            "queryName": entry.get("queryName") or "", "bid": target["bid"], "found": target["name"],
            "foundBrewery": target["brewery"], "style": target["style"], "rating": target["rating"],
            "kind": target["kind"], "via": entry.get("via") or "",
        })
    return out


async def register(items: list[dict]) -> None:
    """Remembers what each button id stands for, and that these were asked about now."""
    now = int(time.time())
    async with _lock:
        state = _load()
        for item in items:
            state["items"][item["id"]] = {**{k: item[k] for k in ("queryBrewery", "queryName", "bid", "found")}, "at": now}
            state["sent"][item["id"]] = now
        state["lastRun"] = now
        for old in [k for k, v in state["items"].items() if now - v.get("at", 0) > ITEM_TTL_SECONDS]:
            del state["items"][old]
        for old in [k for k, ts in state["sent"].items() if now - ts > RESEND_AFTER_SECONDS * 2]:
            del state["sent"][old]
        _save()


def render(item: dict, lang: str) -> str:
    """The question's HTML text."""
    shop = " - ".join(p for p in (item["queryBrewery"], item["queryName"]) if p)
    found = item["found"] + (f" ({item['foundBrewery']})" if item["foundBrewery"] else "")
    details = ""
    if item["style"]:
        details += f"\n{html.escape(item['style'])}"
    if item["rating"]:
        details += f" ⭐ {item['rating']:.2f}"
    reason = t(lang, "lens_review_reason_probable") if item["kind"] == "probable" \
        else t(lang, "lens_review_reason_suspicious", via=html.escape(item["via"] or "?"))
    return t(
        lang, "lens_review_item", shop=html.escape(shop),
        url=f"https://untappd.com/beer/{item['bid']}", found=html.escape(found), details=details, reason=reason,
    )


def keyboard_rows(item_id: str, lang: str) -> list[list[tuple[str, str]]]:
    """[[(label, callback_data), ...]] for the three buttons."""
    return [[
        (t(lang, "lens_review_btn_ok"), f"lr:ok:{item_id}"),
        (t(lang, "lens_review_btn_no"), f"lr:no:{item_id}"),
        (t(lang, "lens_review_btn_skip"), f"lr:skip:{item_id}"),
    ]]


async def decide(item_id: str, action: str) -> dict | None:
    """Applies ✅ ("ok") / ❌ ("no") / ⏭ ("skip") to the question behind `item_id`; returns the item
    (or None when it is unknown or too old). ok/no flag the product for the next crawl to re-resolve."""
    async with _lock:
        state = _load()
        item = state["items"].get(item_id)
        if item is None:
            return None
        if action in ("ok", "no", "skip"):
            del state["items"][item_id]
            _save()
    if action == "ok":
        await lens_confirmations.add(item["queryBrewery"], item["queryName"], item["bid"])
        await lens_rejections.remove(item["queryBrewery"], item["queryName"], item["bid"])
    elif action == "no":
        via = await lens_log.entry_via(item["queryBrewery"], item["queryName"])
        await lens_rejections.add(item["queryBrewery"], item["queryName"], item["bid"], via)
        await lens_confirmations.remove(item["queryBrewery"], item["queryName"], item["bid"])
    if action in ("ok", "no"):
        await lens_log.mark_recheck(item["queryBrewery"], item["queryName"])
    return item
