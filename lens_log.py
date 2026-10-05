"""Outcome log for the Untappd Lens userscript's lookups (webapp_server's
/api/lens/lookup): ONE aggregated record per distinct (brewery, shop title),
holding the latest FINAL result - matched / no results / ambiguous / error -
and how many times it was looked up.

Why it exists: bot_log.txt records every intermediate query variant the
resolver tries, so "misses" there are mostly the resolver working through
fallbacks (thousands of lines for far fewer real products), and a WRONG
match - the worst failure, another beer's rating shown with full confidence -
never appears in any log at all. This keeps the final outcome per product,
plus how the match relates to the shop title (beer_match.classify_match), so
a review can start from the two lists that matter: products that stayed
unmatched, and matches that look suspicious. See report() and
webapp_server.handle_lens_report.

Same module shape as the other JSON stores (module-level _path/_lock, atomic
tmp-file + os.replace writes), with an in-memory mirror since every shop page
view updates it."""

import asyncio
import json
import os
import time

import beer_match

_path: str | None = None
_lock = asyncio.Lock()
_mirror: dict | None = None
MAX_ENTRIES = 20000
STALE_AFTER_SECONDS = 14 * 24 * 3600  # re-check a product that matched this long ago
MAX_CANDIDATES_KEPT = 5
# Matches at least this many words apart from the shop title are worth a look.
SUSPICIOUS_DELTA = 3


def init(data_dir: str) -> None:
    global _path, _mirror
    _path = os.path.join(data_dir, "lens_log.json")
    _mirror = None


def _load() -> dict:
    global _mirror
    if _mirror is not None:
        return _mirror
    _mirror = {}
    if _path and os.path.exists(_path):
        try:
            with open(_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _mirror = loaded
        except (json.JSONDecodeError, OSError):
            pass
    return _mirror


def _save() -> None:
    tmp = _path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_mirror, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, _path)


def _key(brewery: str, name: str) -> str:
    return f"{beer_match.scan_norm(brewery)}|{beer_match.scan_norm(name)}"


def _outcome(result: dict) -> str:
    if result.get("error"):
        return "error"
    if result.get("matched"):
        return "matched"
    return "ambiguous" if result.get("candidates") else "no_results"


async def record(items: list, results: list[dict], source: str = "lens") -> None:
    """items[i] is the {name, brewery} the shop page sent, results[i] what the
    lookup returned for it (same order). source "lens" = a real page view
    (counts toward `count`, i.e. someone looked at it); "crawl" = the daily
    shop crawl (counts toward `crawlSeen` only), so products people actually
    open rank above ones only the crawler has seen."""
    now = int(time.time())
    async with _lock:
        data = _load()
        for item, result in zip(items, results):
            if not isinstance(item, dict) or not isinstance(result, dict):
                continue
            name = (item.get("name") or "").strip()
            brewery = (item.get("brewery") or "").strip()
            if not name:
                continue
            outcome = _outcome(result)
            match_kind, delta = (None, 0)
            if outcome == "matched":
                match_kind, delta = beer_match.classify_match(name, brewery, result.get("name") or "")
            entry = data.get(_key(brewery, name))
            if entry is None:
                entry = data[_key(brewery, name)] = {"count": 0, "crawlSeen": 0, "firstSeen": now}
            elif entry.get("outcome") != outcome or entry.get("bid") != result.get("bid"):
                entry["previous"] = {"outcome": entry.get("outcome"), "bid": entry.get("bid"), "until": now}
            entry.update({
                "queryName": name, "queryBrewery": brewery, "outcome": outcome,
                "matchKind": match_kind, "delta": delta,
                "bid": result.get("bid") if outcome == "matched" else None,
                "matchedName": result.get("name") if outcome == "matched" else None,
                "matchedBrewery": result.get("brewery") if outcome == "matched" else None,
                "candidates": [c.get("name") for c in (result.get("candidates") or [])[:MAX_CANDIDATES_KEPT]],
                "lastSeen": now, "resolvedAt": now,
            })
            if source == "crawl":
                entry["crawlSeen"] = entry.get("crawlSeen", 0) + 1
                entry["lastCrawl"] = now
            else:
                entry["count"] = entry.get("count", 0) + 1
            shop = item.get("shop")
            if shop and shop not in entry.setdefault("shops", []) and len(entry["shops"]) < 3:
                entry["shops"].append(shop)
        if len(data) > MAX_ENTRIES:
            for k in sorted(data, key=lambda k: data[k].get("lastSeen", 0))[: len(data) - MAX_ENTRIES]:
                del data[k]
        _save()


async def due_for_crawl(items: list[dict]) -> list[dict]:
    """The subset of crawled products worth resolving now, most useful first:
    products still unmatched (they may have been fixed since), then new ones,
    then matched ones not re-checked for STALE_AFTER_SECONDS."""
    now = int(time.time())
    async with _lock:
        data = _load()
        buckets: tuple[list, list, list] = ([], [], [])
        for item in items:
            entry = data.get(_key(item.get("brewery") or "", item.get("name") or ""))
            if entry is None:
                buckets[1].append(item)
            elif entry.get("outcome") != "matched":
                buckets[0].append(item)
            elif now - entry.get("resolvedAt", 0) > STALE_AFTER_SECONDS:
                buckets[2].append(item)
    return buckets[0] + buckets[1] + buckets[2]


async def mark_crawled(items: list[dict]) -> None:
    """Products the crawl saw again but didn't need to re-resolve."""
    now = int(time.time())
    async with _lock:
        data = _load()
        touched = False
        for item in items:
            entry = data.get(_key(item.get("brewery") or "", item.get("name") or ""))
            if entry is not None:
                entry["crawlSeen"] = entry.get("crawlSeen", 0) + 1
                entry["lastCrawl"] = entry["lastSeen"] = now
                touched = True
        if touched:
            _save()


async def report(limit: int = 50) -> dict:
    """The review view: overall counts, unmatched products by how often they
    were looked up, matches whose catalog name is far from the shop title
    (the likely-wrong ones), and products whose outcome changed since the
    last time (e.g. after a matcher fix)."""
    async with _lock:
        entries = list(_load().values())
    outcomes: dict = {}
    kinds: dict = {}
    for e in entries:
        outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
        if e.get("matchKind"):
            kinds[e["matchKind"]] = kinds.get(e["matchKind"], 0) + 1

    def brief(e: dict) -> dict:
        return {k: e.get(k) for k in (
            "queryBrewery", "queryName", "outcome", "matchKind", "delta", "bid", "matchedName",
            "candidates", "count", "crawlSeen", "shops", "lastSeen", "previous",
        ) if e.get(k) not in (None, [], 0) or k in ("queryName", "queryBrewery")}

    unmatched = sorted(
        (e for e in entries if e["outcome"] in ("no_results", "ambiguous")),
        key=lambda e: (-e.get("count", 0), -e.get("crawlSeen", 0), -e.get("lastSeen", 0)),
    )
    suspicious = sorted(
        (e for e in entries if e["outcome"] == "matched" and (
            e.get("matchKind") == "different"
            or (e.get("matchKind") in ("candidate_longer", "query_longer") and e.get("delta", 0) >= SUSPICIOUS_DELTA)
        )),
        key=lambda e: (-e.get("delta", 0), -e.get("count", 0)),
    )
    changed = sorted((e for e in entries if e.get("previous")), key=lambda e: -e.get("lastSeen", 0))
    return {
        "total": len(entries), "outcomes": outcomes, "matchKinds": kinds,
        "unmatched": [brief(e) for e in unmatched[:limit]],
        "suspicious": [brief(e) for e in suspicious[:limit]],
        "changed": [brief(e) for e in changed[:limit]],
    }
