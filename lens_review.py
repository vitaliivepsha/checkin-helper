"""The lens's review queue: once a day the bot asks the owner, in Telegram, about the few shop
products whose match is most in doubt - questionable matches (lens_log's "suspicious") and probable
(🤔) ones - with ✅ right / ❌ wrong / ⏭ skip under each. A decision lands in lens_confirmations or
lens_rejections (the same stores the userscript's ✓/✕ write), and the product is flagged for the next
crawl to re-resolve, so a minute on a phone a day replaces browsing shops for wrong matches.

State (lens_review.json, same JSON-store shape as the others): which items were asked about
recently (not asked again for RESEND_AFTER), and the pending items behind each button - the button's
callback data carries only a short id, Telegram limits it to 64 bytes.

Two kinds of question: "choose" - an unmatched product the search found lookalikes for, most-looked-at
first (one answer fixes every later view of it; buttons 1/2/3 pick the beer, ❌ marks it as not on
Untappd), and "check" - a doubtful match or a probable one (✅ / ❌). Choose questions come first: a
doubtful match has turned out right 97% of the time, an unmatched popular product is a sure miss."""

import asyncio
import hashlib
import html
import json
import os
import time

import lens_confirmations
import lens_log
import lens_rejections
import beer_match
import shop_crawl
from i18n import t

_path: str | None = None
_lock = asyncio.Lock()
_mirror: dict | None = None

REVIEW_BATCH = 5                    # questions per day
CHOOSE_PER_BATCH = 3                # of them "which beer is this" questions
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
    """The beer a "check" question is about: the logged match, or the probable candidate."""
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


def _source(entry: dict) -> tuple[str, str]:
    return shop_crawl.shop_link((entry.get("shops") or [""])[0], entry.get("shopUrl"))


async def pick(limit: int = REVIEW_BATCH) -> list[dict]:
    """Up to `limit` questions: first the "choose" ones (CHOOSE_PER_BATCH), then "check" ones, skipping
    products already decided or asked about recently. A "check" item: {"id", "kind", "queryBrewery",
    "queryName", "bid", "found", "foundBrewery", "style", "rating", "via", "source"}; a "choose" item:
    {"id", "kind": "choose", "queryBrewery", "queryName", "options": [{"bid", "name", "brewery"}],
    "views", "source"}."""
    now = time.time()
    sent = _load()["sent"]

    def asked_recently(item_id: str) -> bool:
        return now - sent.get(item_id, 0) < RESEND_AFTER_SECONDS

    def skip_choose(entry: dict) -> bool:
        brewery, name = entry.get("queryBrewery") or "", entry.get("queryName") or ""
        return lens_confirmations.confirmed_bid(brewery, name) is not None or asked_recently(_item_id(entry["key"], 0))

    def skip_check(entry: dict) -> bool:
        target = _target(entry)
        if target is None:
            return True
        brewery, name = entry.get("queryBrewery") or "", entry.get("queryName") or ""
        if target["bid"] in lens_rejections.rejected_bids(brewery, name):
            return True
        if lens_confirmations.confirmed_bid(brewery, name) == target["bid"]:
            return True
        return asked_recently(_item_id(entry.get("key", ""), target["bid"]))

    out = []
    for entry in await lens_log.choose_candidates(min(CHOOSE_PER_BATCH, limit), skip_choose):
        rejected = lens_rejections.rejected_bids(entry.get("queryBrewery") or "", entry.get("queryName") or "")
        options = [
            o for o in entry["candidateList"]
            if o["bid"] not in rejected and not beer_match.edition_conflict(entry.get("queryName") or "", o["name"] or "")
        ]
        if not options:
            continue
        out.append({
            "id": _item_id(entry["key"], 0), "kind": "choose", "queryBrewery": entry.get("queryBrewery") or "",
            "queryName": entry.get("queryName") or "", "options": options, "views": entry.get("count", 0),
            "source": _source(entry),
        })
    for entry in await lens_log.review_candidates(limit - len(out), skip_check):
        target = _target(entry)
        out.append({
            "id": _item_id(entry["key"], target["bid"]), "queryBrewery": entry.get("queryBrewery") or "",
            "queryName": entry.get("queryName") or "", "bid": target["bid"], "found": target["name"],
            "foundBrewery": target["brewery"], "style": target["style"], "rating": target["rating"],
            "kind": target["kind"], "via": entry.get("via") or "", "source": _source(entry),
        })
    return out


async def register(items: list[dict]) -> None:
    """Remembers what each button id stands for, and that these were asked about now."""
    now = int(time.time())
    async with _lock:
        state = _load()
        for item in items:
            record = {"queryBrewery": item["queryBrewery"], "queryName": item["queryName"], "at": now}
            if item["kind"] == "choose":
                record["options"] = [{"bid": o["bid"], "name": o["name"]} for o in item["options"]]
            else:
                record.update({"bid": item["bid"], "found": item["found"]})
            state["items"][item["id"]] = record
            state["sent"][item["id"]] = now
        state["lastRun"] = now
        for old in [k for k, v in state["items"].items() if now - v.get("at", 0) > ITEM_TTL_SECONDS]:
            del state["items"][old]
        for old in [k for k, ts in state["sent"].items() if now - ts > RESEND_AFTER_SECONDS * 2]:
            del state["sent"][old]
        _save()


def _source_line(item: dict, lang: str) -> str:
    label, link = item.get("source") or ("", "")
    return t(lang, "lens_review_source", url=html.escape(link, quote=True), label=html.escape(label)) if link else ""


def render(item: dict, lang: str) -> str:
    """The question's HTML text."""
    shop = " - ".join(p for p in (item["queryBrewery"], item["queryName"]) if p)
    if item["kind"] == "choose":
        options = "\n".join(
            t(lang, "lens_review_option", n=n, url=f"https://untappd.com/beer/{o['bid']}", name=html.escape(o["name"] or ""),
              brewery=f" ({html.escape(o['brewery'])})" if o.get("brewery") else "")
            for n, o in enumerate(item["options"], 1)
        )
        return t(lang, "lens_review_choose", shop=html.escape(shop), source=_source_line(item, lang),
                 views=item.get("views", 0), options=options)
    found = item["found"] + (f" ({item['foundBrewery']})" if item["foundBrewery"] else "")
    details = ""
    if item["style"]:
        details += f"\n{html.escape(item['style'])}"
    if item["rating"]:
        details += f" ⭐ {item['rating']:.2f}"
    if item["kind"] == "probable":
        reason = t(lang, "lens_review_reason_probable")
    elif item["via"]:
        reason = t(lang, "lens_review_reason_suspicious", via=html.escape(item["via"]))
    else:
        reason = t(lang, "lens_review_reason_suspicious_plain")   # logged before the route was recorded
    return t(
        lang, "lens_review_item", shop=html.escape(shop),
        url=f"https://untappd.com/beer/{item['bid']}", found=html.escape(found), details=details, reason=reason,
        source=_source_line(item, lang),
    )


def keyboard_rows(item_id: str, lang: str, options: int = 0) -> list[list[tuple[str, str]]]:
    """[[(label, callback_data), ...]]: ✅/❌/⏭ for a "check" question; for a "choose" one (`options` > 0)
    a button per option, then ❌ (none / not on Untappd) and ⏭."""
    if options:
        return [
            [(str(n), f"lr:c{n}:{item_id}") for n in range(1, options + 1)],
            [(t(lang, "lens_review_btn_none"), f"lr:none:{item_id}"), (t(lang, "lens_review_btn_skip"), f"lr:skip:{item_id}")],
        ]
    return [[
        (t(lang, "lens_review_btn_ok"), f"lr:ok:{item_id}"),
        (t(lang, "lens_review_btn_no"), f"lr:no:{item_id}"),
        (t(lang, "lens_review_btn_skip"), f"lr:skip:{item_id}"),
    ]]


ACTIONS = ("ok", "no", "skip", "none", "c1", "c2", "c3")


async def decide(item_id: str, action: str) -> dict | None:
    """Applies a button to the question behind `item_id`; returns the item (None when it is unknown, too old
    or the action does not fit it). ✅ ok / ❌ no / ⏭ skip for a "check" question; c1-c3 (the chosen option) /
    none (not on Untappd) / skip for a "choose" one. Decisions flag the product for the next crawl to
    re-resolve; a choose item comes back with "chosen" (the picked beer's name)."""
    async with _lock:
        state = _load()
        item = state["items"].get(item_id)
        if item is None or action not in ACTIONS:
            return None
        is_choose = "options" in item
        chosen = None
        if action in ("c1", "c2", "c3"):
            index = int(action[1]) - 1
            if not is_choose or index >= len(item["options"]):
                return None
            chosen = item["options"][index]
        elif action in ("ok", "no") and is_choose or action == "none" and not is_choose:
            return None
        del state["items"][item_id]
        _save()
    brewery, name = item["queryBrewery"], item["queryName"]
    if chosen is not None:
        await lens_confirmations.add(brewery, name, chosen["bid"])
        await lens_rejections.remove(brewery, name, chosen["bid"])
        await lens_log.mark_recheck(brewery, name)
        return {**item, "chosen": chosen["name"]}
    if action == "none":
        await lens_log.mark_absent(brewery, name)
    elif action == "ok":
        await lens_confirmations.add(brewery, name, item["bid"])
        await lens_rejections.remove(brewery, name, item["bid"])
        await lens_log.mark_recheck(brewery, name)
    elif action == "no":
        via = await lens_log.entry_via(brewery, name)
        await lens_rejections.add(brewery, name, item["bid"], via)
        await lens_confirmations.remove(brewery, name, item["bid"])
        await lens_log.mark_recheck(brewery, name)
    return item
