"""The Untappd Lens routes and background jobs: the page lookup and its feedback buttons (reject / confirm and their
undo), the report, the nightly shop crawl, and the Telegram review queue with its weekly digest.

Moved out of webapp_server.py (audit A1) without changing behaviour. Shared names that stay in webapp_server
(configuration, state tests patch, helpers used elsewhere) are reached through `ws`, a late-bound view of that module:
attributes are looked up at call time, so monkeypatching webapp_server still reaches this code."""

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import asyncio
import beer_match
import datetime
import httpx
import json
import lens_confirmations
import lens_log
import lens_rejections
import lens_review
import shop_crawl
import time
import untappd_mcp
import user_tokens
import zoneinfo

from web_shared import ws


async def _lens_list_flags(owner_id: int, token: str) -> tuple[set, set]:
    """(classic-wishlist bids, "Мій список" bids). Fetched ONCE per batch, not
    per item - it's the same account's lists for every card on the page, and
    _get_wishlist_beers already shares the 15-min-TTL _wishlist_cache with the
    checkin webapp's own wishlist-priority search, so this is usually a cache
    hit (0 extra quota) rather than a fresh get_my_wishlist call."""
    wishlist_beers = await ws._get_wishlist_beers(owner_id, token)
    wishlist_bids = {b.get("bid") for b in wishlist_beers if b.get("bid") is not None}
    my_list_bids = {it["beerId"] for it in await ws._get_my_list_items(owner_id) if it.get("beerId")}
    return wishlist_bids, my_list_bids


async def _lens_resolve_item(token: str, owner_id: int, item, wishlist_bids: set, my_list_bids: set) -> dict:
    """One shop product through the lens resolver (never raises: a failed
    lookup comes back as an `error` result), plus the list flags. Shared by the
    batch lookup and the "not that beer" re-resolve."""
    name = (item.get("name") or "").strip() if isinstance(item, dict) else ""
    brewery = (item.get("brewery") or "").strip() if isinstance(item, dict) else ""
    if not name:
        return {"matched": False, "query_name": name, "query_brewery": brewery, "candidates": []}
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
            rejected_bids=lens_rejections.rejected_bids(brewery, name), with_probable=True,
            confirmed_bid=lens_confirmations.confirmed_bid(brewery, name),
        )
        result["inWishlist"] = result.get("bid") in wishlist_bids
        result["inSheetList"] = result.get("bid") in my_list_bids
        return result
    except untappd_mcp.UntappdMCPError as exc:
        ws.logger.warning("lens lookup failed for %r %r: %s", brewery, name, exc)
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
        ws.logger.exception("lens lookup: unexpected error for %r %r", brewery, name)
        return {
            "matched": False, "query_name": name, "query_brewery": brewery,
            "candidates": [], "searchUrl": beer_match.build_search_url(brewery, name), "error": True,
        }


def _lens_authorized(request: web.Request) -> bool:
    """The one X-Lens-Token check every lens route uses (it used to be copied into each handler)."""
    return bool(ws.LENS_API_TOKEN) and ws._constant_time_equal(request.headers.get("X-Lens-Token", ""), ws.LENS_API_TOKEN)


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
    if not _lens_authorized(request):
        return ws._json_error("unauthorized", 401)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return ws._json_error("invalid_json")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return ws._json_error("no_items")
    items = items[:ws.LENS_MAX_ITEMS_PER_REQUEST]
    # Which client version asks ("userscript 2.9.4", "extension 1.14.4"; none = an old one): a stale copy
    # somewhere keeps sending titles without their source, and this is how /api/lens/report shows it.
    client = str(body.get("client") or "legacy (no version)")[:40]
    if client not in ws._lens_clients and len(ws._lens_clients) >= ws.MAX_LENS_CLIENTS:
        ws._lens_clients.pop(min(ws._lens_clients, key=lambda c: ws._lens_clients[c]["last"]))   # forget the longest-quiet label
    seen = ws._lens_clients.setdefault(client, {"lookups": 0, "items": 0, "last": 0})
    seen["lookups"] += 1
    seen["items"] += len(items)
    seen["last"] = int(time.time())
    # Which device it is (the browser's User-Agent and the address the proxy saw), to find the stale copy.
    seen["ua"] = request.headers.get("User-Agent", "")[:100]
    seen["ip"] = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote or "")[:45]

    owner_id = int(ws.AUTO_TOAST_OWNER_ID)
    token = await user_tokens.get_token(owner_id)
    if not token:
        return ws._json_error("not_connected")

    # Fetched ONCE per batch, not per item - it's the same account's
    # wishlist for every card on the page, and _get_wishlist_beers already
    # shares the 15-min-TTL _wishlist_cache with the checkin webapp's own
    # wishlist-priority search, so this is usually a cache hit (0 extra
    # quota) rather than a fresh get_my_wishlist call.
    wishlist_bids, my_list_bids = await _lens_list_flags(owner_id, token)

    semaphore = asyncio.Semaphore(ws.LENS_LOOKUP_CONCURRENCY)

    async def _resolve_one(item) -> dict:
        async with semaphore:
            return await _lens_resolve_item(token, owner_id, item, wishlist_bids, my_list_bids)

    results = await asyncio.gather(*(_resolve_one(it) for it in items))
    try:
        await lens_log.record(items, results)
    except Exception:
        ws.logger.exception("lens_log: failed to record a lookup batch")  # never fail the user's page over bookkeeping
    return web.json_response({"ok": True, "results": results})


async def handle_lens_report(request: web.Request) -> web.Response:
    """Review view of lens_log.py (same X-Lens-Token as /api/lens/lookup):
    unmatched products, suspicious matches and outcome changes - e.g.
    `curl -H "X-Lens-Token: $LENS_API_TOKEN" <origin>/api/lens/report?limit=30`."""
    if not _lens_authorized(request):
        return ws._json_error("unauthorized", 401)
    try:
        limit = max(1, min(1000, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50
    report = await lens_log.report(limit)
    rejections = await lens_rejections.all_entries()
    report["rejections"] = rejections[:limit]
    report["rejectionsByVia"] = {}
    for entry in rejections:
        for via in (entry.get("vias") or {}).values():
            report["rejectionsByVia"][via] = report["rejectionsByVia"].get(via, 0) + 1
    report["confirmations"] = await lens_confirmations.count()
    report["clients"] = {k: dict(v) for k, v in ws._lens_clients.items()}
    report["crawl"] = {**shop_crawl.load_state(), "running": _shop_crawl_running}
    return web.json_response(report)


async def _lens_rejection_request(request: web.Request) -> tuple[dict, None] | tuple[None, web.Response]:
    """Shared validation of /api/lens/reject and /unreject: (fields, None) or
    (None, error response). Same X-Lens-Token gate as the lookup."""
    if not _lens_authorized(request):
        return None, ws._json_error("unauthorized", 401)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return None, ws._json_error("invalid_json")
    if not isinstance(body, dict):
        return None, ws._json_error("invalid_json")
    name, brewery, bid = body.get("name"), body.get("brewery") or "", body.get("bid")
    if not isinstance(name, str) or not name.strip() or not isinstance(brewery, str) or len(name) > 300 or len(brewery) > 300:
        return None, ws._json_error("invalid_item")
    if isinstance(bid, bool) or not isinstance(bid, int):
        return None, ws._json_error("invalid_bid")
    return {"name": name.strip(), "brewery": brewery.strip(), "bid": bid}, None


async def handle_lens_reject(request: web.Request) -> web.Response:
    """The userscript's "this is not that beer": remembers `bid` as wrong for
    this shop text (lens_rejections) and returns the item re-resolved without
    it, so the page can redraw the card at once. No Untappd quota - the same
    quota-free search the lookup does, and the rejection itself is a local file
    write. The new outcome is logged like a recheck (a correction is not
    another view of the product)."""
    fields, err = await _lens_rejection_request(request)
    if err:
        return err
    via = await lens_log.entry_via(fields["brewery"], fields["name"])
    if not await lens_rejections.add(fields["brewery"], fields["name"], fields["bid"], via):
        return ws._json_error("rejections_full", 507)
    await lens_confirmations.remove(fields["brewery"], fields["name"], fields["bid"])  # a rejected beer is not confirmed
    return await _lens_reresolved_response(fields)


async def handle_lens_unreject(request: web.Request) -> web.Response:
    """Undo of /api/lens/reject; returns the item re-resolved with the beer
    allowed again."""
    fields, err = await _lens_rejection_request(request)
    if err:
        return err
    await lens_rejections.remove(fields["brewery"], fields["name"], fields["bid"])
    return await _lens_reresolved_response(fields)


async def handle_lens_confirm(request: web.Request) -> web.Response:
    """The userscript's ✓ on a probable (🤔) match: the beer is the right one for this shop text
    (lens_confirmations). Returns the item re-resolved, now matched."""
    fields, err = await _lens_rejection_request(request)
    if err:
        return err
    if not await lens_confirmations.add(fields["brewery"], fields["name"], fields["bid"]):
        return ws._json_error("confirmations_full", 507)
    await lens_rejections.remove(fields["brewery"], fields["name"], fields["bid"])  # confirmed beats an earlier ✕
    return await _lens_reresolved_response(fields)


async def handle_lens_unconfirm(request: web.Request) -> web.Response:
    """Undo of /api/lens/confirm."""
    fields, err = await _lens_rejection_request(request)
    if err:
        return err
    await lens_confirmations.remove(fields["brewery"], fields["name"], fields["bid"])
    return await _lens_reresolved_response(fields)


async def _lens_reresolved_response(fields: dict) -> web.Response:
    owner_id = int(ws.AUTO_TOAST_OWNER_ID)
    token = await user_tokens.get_token(owner_id)
    if not token:
        return ws._json_error("not_connected")
    wishlist_bids, my_list_bids = await _lens_list_flags(owner_id, token)
    item = {"name": fields["name"], "brewery": fields["brewery"]}
    result = await _lens_resolve_item(token, owner_id, item, wishlist_bids, my_list_bids)
    try:
        await lens_log.record([item], [result], source="recheck")
    except Exception:
        ws.logger.exception("lens_log: failed to record a re-resolved item")
    return web.json_response({"ok": True, "result": result})


async def handle_lens_crawl(request: web.Request) -> web.Response:
    """Starts the shop crawl now (same X-Lens-Token) instead of waiting for
    the daily run - e.g. right after a matcher fix, to re-resolve what was
    unmatched. Runs in the background; progress shows up in /api/lens/report
    under "crawl"."""
    if not _lens_authorized(request):
        return ws._json_error("unauthorized", 401)
    if _shop_crawl_running:
        return ws._json_error("already_running", 409)
    global _shop_crawl_manual_task
    _shop_crawl_manual_task = asyncio.create_task(_run_shop_crawl())
    return web.json_response({"ok": True, "started": True}, status=202)


_shop_crawl_task = None


_shop_crawl_manual_task = None  # keeps the create_task result alive (avoid GC)


_shop_crawl_running = False


def _start_shop_crawl() -> None:
    global _shop_crawl_task, _lens_review_task
    if _shop_crawl_task and not _shop_crawl_task.done():
        return
    _shop_crawl_task = asyncio.create_task(_shop_crawl_loop())
    _lens_review_task = asyncio.create_task(_lens_review_loop())


_lens_review_task = None


LENS_REVIEW_STARTUP_DELAY_SECONDS = 600


LENS_REVIEW_HOURS = range(9, 21)  # Warsaw local time: never wake the owner at night


async def _send_lens_review_batch() -> int:
    """Asks the owner about the lens matches most in doubt (see lens_review.py); returns how many
    questions were sent."""
    if ws._ptb_bot is None:
        return 0
    owner_id = int(ws.AUTO_TOAST_OWNER_ID)
    lang = await user_tokens.get_language(owner_id) or "uk"
    items = await lens_review.pick()
    sent = []
    for item in items:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in lens_review.keyboard_rows(item["id"], lang, len(item.get("options") or []))
        ])
        try:
            await ws._ptb_bot.send_message(
                chat_id=owner_id, text=lens_review.render(item, lang), parse_mode="HTML",
                reply_markup=keyboard, disable_web_page_preview=True,
            )
            sent.append(item)
        except Exception:
            ws.logger.exception("lens review: could not send a question")
            break
    await lens_review.register(sent)
    return len(sent)


async def _send_lens_weekly() -> bool:
    """The Monday digest (lens_review.weekly_digest); False when the bot is not up."""
    if ws._ptb_bot is None:
        return False
    owner_id = int(ws.AUTO_TOAST_OWNER_ID)
    lang = await user_tokens.get_language(owner_id) or "uk"
    text = await lens_review.weekly_digest(await lens_log.report(100000), await lens_confirmations.count(), lang)
    await ws._ptb_bot.send_message(chat_id=owner_id, text=text, parse_mode="HTML")
    return True


async def _lens_review_loop() -> None:
    """Once a day, inside LENS_REVIEW_HOURS, sends the review queue."""
    await asyncio.sleep(LENS_REVIEW_STARTUP_DELAY_SECONDS)
    while True:
        try:
            now = datetime.datetime.now(zoneinfo.ZoneInfo("Europe/Warsaw"))
            due = time.time() - lens_review.last_run() >= lens_review.REVIEW_INTERVAL_SECONDS
            if due and now.hour in LENS_REVIEW_HOURS:
                await _send_lens_review_batch()
            if now.weekday() == 0 and now.hour in LENS_REVIEW_HOURS and lens_review.weekly_due():
                await _send_lens_weekly()
        except asyncio.CancelledError:
            raise
        except Exception:
            ws.logger.exception("lens review loop failed")
        await asyncio.sleep(1800)


async def handle_lens_review_send(request: web.Request) -> web.Response:
    """Sends the review queue now (same X-Lens-Token), e.g. to try it or catch up."""
    if not _lens_authorized(request):
        return ws._json_error("unauthorized", 401)
    return web.json_response({"ok": True, "sent": await _send_lens_review_batch()})


def _shop_crawl_due(now: float | None = None) -> bool:
    """Whether the daily crawl should start now (see SHOP_CRAWL_HOURS_UTC)."""
    now = time.time() if now is None else now
    state = shop_crawl.load_state()
    finished = state.get("finishedAt") or state.get("lastFinished") or 0
    age = now - finished
    if age < ws.SHOP_CRAWL_INTERVAL_SECONDS - ws.SHOP_CRAWL_SLACK_SECONDS:
        return False
    if state.get("lastRun", 0) > finished and now - state["lastRun"] > 1800:
        return True   # the last run started but never finished: it was killed, run it again
    hour = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).hour
    return hour in ws.SHOP_CRAWL_HOURS_UTC or age >= ws.SHOP_CRAWL_INTERVAL_SECONDS * 1.5


async def _shop_crawl_loop() -> None:
    await asyncio.sleep(ws.SHOP_CRAWL_STARTUP_DELAY_SECONDS)
    while True:
        try:
            if _shop_crawl_due():
                await _run_shop_crawl()
        except asyncio.CancelledError:
            raise
        except Exception:
            ws.logger.exception("shop crawl failed")
            await asyncio.sleep(3600)  # retry in an hour, never hot-loop on a persistent failure
        await asyncio.sleep(900)


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
    previous = shop_crawl.load_state()
    state = {
        "lastRun": int(time.time()), "running": True, "shops": {},
        # the last COMPLETED run, kept through this one so an interrupted run does not erase it
        "lastFinished": previous.get("finishedAt") or previous.get("lastFinished") or 0,
    }
    shop_crawl.save_state(state)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
            get_text, get_json = shop_crawl.make_http_getters(client)
            crawl = await shop_crawl.crawl_shops(get_text, get_json)
        state["shops"] = shop_crawl.summarize(crawl)
        items = shop_crawl.dedupe([it for r in crawl.values() for it in r["items"] if not shop_crawl.is_not_a_beer(it)])
        state["products"] = len(items)

        owner_id = int(ws.AUTO_TOAST_OWNER_ID) if ws.AUTO_TOAST_OWNER_ID else None
        token = await user_tokens.get_token(owner_id) if owner_id else None
        resolved = 0
        todo: list[dict] = []
        if token:
            due = await lens_log.due_for_crawl(items)
            todo = due[:ws.SHOP_CRAWL_MAX_RESOLVES]
            state["deferred"] = len(due) - len(todo)
            resolved = await ws._resolve_and_record(token, owner_id, todo, "crawl", state)
            if "stoppedEarly" not in state:
                # Then the unmatched entries the crawl itself didn't list (e.g. only
                # the userscript ever saw them) - so a matcher fix shows up without
                # anyone reopening that page. Not-a-beer entries are dropped here.
                recheck = await lens_log.entries_to_recheck(items, ws.SHOP_CRAWL_RECHECK_MAX, shop_crawl.is_not_a_beer)
                state["rechecked"] = await ws._resolve_and_record(token, owner_id, recheck, "recheck", state)
        else:
            state["skipped"] = "no owner Untappd token - products listed but not resolved"
        todo_keys = {(it["brewery"].lower(), it["name"].lower()) for it in todo}
        await lens_log.mark_crawled([it for it in items if (it["brewery"].lower(), it["name"].lower()) not in todo_keys])
        state["resolved"] = resolved
        ws.logger.info("shop crawl: %d products, %d resolved, shops=%s", len(items), resolved, state["shops"])
        state["finishedAt"] = state["lastFinished"] = int(time.time())   # only a run that got here counts as done
    finally:
        state["running"] = False
        shop_crawl.save_state(state)
        _shop_crawl_running = False
    return state
