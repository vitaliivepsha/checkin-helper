"""The check-in routes: submitting a check-in, and the pending (parked) check-ins with their retry that first looks for a
check-in that may already exist (audit R2).

Moved out of webapp_server.py (audit A1) without changing behaviour. Shared names that stay in webapp_server
(configuration, state tests patch, helpers used elsewhere) are reached through `ws`, a late-bound view of that module:
attributes are looked up at call time, so monkeypatching webapp_server still reaches this code."""

from aiohttp import web
from email.utils import parsedate_to_datetime
import checkin_queue
import input_checks
import json
import pending_checkins
import untappd_mcp
import user_tokens

from web_shared import ws


async def handle_submit(request: web.Request) -> web.Response:
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    token = await ws._resolve_token(tg_user)
    if not token:
        return ws._json_error("not_connected", 403)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")

    beer_id = body.get("beerId")
    rating = body.get("rating", 0)
    shout = (body.get("shout") or "").strip()
    foursquare_id = body.get("foursquareId")
    geolat = body.get("geolat")
    geolng = body.get("geolng")
    venue_name = body.get("venueName")
    queue_item_id = body.get("queueItemId")

    if not isinstance(beer_id, int) or beer_id <= 0:
        return ws._json_error("invalid_beer_id")
    if not isinstance(rating, (int, float)) or not (0 <= rating <= 5):
        return ws._json_error("invalid_rating")
    if foursquare_id is not None and not ws._valid_foursquare_id(foursquare_id):
        return ws._json_error("invalid_foursquare_id")

    # Remembering the picked venue is pure local UX state (never touches
    # Untappd), so it's safe to do in both the dry-run and real branches -
    # the venue doesn't change for the whole festival, so this saves the
    # user from re-picking it on every check-in.
    if foursquare_id:
        await user_tokens.set_last_venue(user_id, {
            "foursquareId": foursquare_id, "name": venue_name,
            "lat": geolat, "lng": geolng,
        })

    if ws.CHECKIN_DRY_RUN:
        would_send = {
            "beerId": beer_id, "rating": rating, "shout": shout,
            "foursquareId": foursquare_id, "geolat": geolat, "geolng": geolng,
        }
        ws.logger.info("CHECKIN_DRY_RUN - would check in: %s", would_send)
        # Marking a queue item completed is purely our own local state - it
        # never touches Untappd - so it's safe (and useful) to exercise even
        # in dry-run, unlike the real check_in call below.
        if queue_item_id:
            await checkin_queue.mark_completed(queue_item_id, user_id)
        return web.json_response({"ok": True, "dryRun": True, "would_send": would_send})

    # display-only fields, sent by the client alongside the ones above
    # purely so a failed attempt's pending record (below) can render a
    # normal-looking row without an extra quota-costing beer lookup -
    # never used on the success path.
    pending_fields = {
        "beerId": beer_id, "rating": rating, "shout": shout,
        "foursquareId": foursquare_id, "geolat": geolat, "geolng": geolng,
        "venueName": venue_name, "queueItemId": queue_item_id,
        "beerName": body.get("beerName"), "brewery": body.get("brewery"),
        "style": body.get("style"), "abv": body.get("abv"), "labelUrl": body.get("labelUrl"),
    }

    try:
        checkin = await untappd_mcp.check_in(
            token, beer_id=beer_id, rating=rating, shout=shout,
            foursquare_id=foursquare_id, geolat=geolat, geolng=geolng,
        )
    except untappd_mcp.UntappdRateLimited:
        # Saved instead of just failing, so the attempt (rating/comment/
        # venue included) isn't lost - see pending_checkins.py and the
        # Mini App's "Відкладені чекіни" screen for the retry side of this.
        try:
            item = await pending_checkins.add_item(user_id, {**pending_fields, "failReason": "rate_limited"})
        except input_checks.StoreFull:
            return ws._json_error("rate_limited", 429)   # nowhere to park it: report the failure as before
        return web.json_response({"ok": True, "pending": True, "item": item})
    except untappd_mcp.UntappdMCPError as e:
        ws.logger.warning("check_in failed: %s", e)
        unknown = isinstance(e, untappd_mcp.UntappdOutcomeUnknown)
        try:
            item = await pending_checkins.add_item(user_id, {
                **pending_fields, "failReason": "checkin_unknown" if unknown else "checkin_failed", "maybeCreated": unknown,
            })
        except input_checks.StoreFull:
            return ws._json_error("checkin_failed", 502)
        return web.json_response({"ok": True, "pending": True, "item": item})

    await ws._apply_checkin_side_effects(user_id, beer_id, rating, foursquare_id, queue_item_id)
    return web.json_response({"ok": True, "checkin": checkin})


async def handle_pending_list(request: web.Request) -> web.Response:
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    return web.json_response({"items": await pending_checkins.list_items(user_id)})


RECONCILE_LOOKBACK_SECONDS = 120   # the attempt ran just before the record was saved; allow for clock skew


async def _find_existing_checkin(user_id: int, token: str, item: dict) -> dict | None:
    """The user's own recent check-in of the item's beer made since the attempt that timed out, if there is one.
    One feed call; if it fails the retry stops (UntappdMCPError) rather than guessing."""
    profile = await user_tokens.get_profile(user_id)
    username = (profile or {}).get("username")
    if not username:
        ws.logger.warning("pending retry: no username to look up an earlier attempt of beer %s", item.get("beerId"))
        return None
    page = await untappd_mcp.get_user_checkins(token, username, limit=10)
    since = (item.get("createdAt") or 0) - RECONCILE_LOOKBACK_SECONDS
    for found in (page.get("checkins") or {}).get("items", []):
        if (found.get("beer") or {}).get("bid") != item.get("beerId"):
            continue
        try:
            made = parsedate_to_datetime(found.get("created_at") or "").timestamp()
        except (TypeError, ValueError):
            continue
        if made >= since:
            ws.logger.info("pending retry: beer %s was already checked in (%s), not repeating", item.get("beerId"), found.get("checkin_id"))
            return found
    return None


async def handle_pending_retry(request: web.Request) -> web.Response:
    """Re-attempts a saved pending check-in with exactly the rating/comment/
    venue it originally captured - same two-exception handling as
    handle_submit's own real-check-in branch, just scoped to one item: a
    second failure leaves it in the list for another try later instead of
    losing it."""
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    token = await ws._resolve_token(tg_user)
    if not token:
        return ws._json_error("not_connected", 403)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")

    item_id = body.get("id")
    if not item_id:
        return ws._json_error("invalid_id")
    # Claimed first: a double tap, or the same record on a second device, must not start a second check-in.
    item, claim = await pending_checkins.claim_item(user_id, item_id)
    if claim == "missing":
        return ws._json_error("not_found", 404)
    if claim == "busy":
        return ws._json_error("in_progress", 409)

    try:
        checkin = None
        if item.get("maybeCreated"):
            # The first attempt timed out after the request was sent: look for the check-in before repeating it.
            checkin = await _find_existing_checkin(user_id, token, item)
        if checkin is None:
            checkin = await untappd_mcp.check_in(
                token, beer_id=item["beerId"], rating=item.get("rating") or 0, shout=item.get("shout") or "",
                foursquare_id=item.get("foursquareId"), geolat=item.get("geolat"), geolng=item.get("geolng"),
            )
    except untappd_mcp.UntappdRateLimited:
        await pending_checkins.release_item(user_id, item_id)
        return ws._json_error("rate_limited", 429)
    except untappd_mcp.UntappdMCPError as e:
        ws.logger.warning("pending check-in retry failed: %s", e)
        if isinstance(e, untappd_mcp.UntappdOutcomeUnknown):
            await pending_checkins.release_item(user_id, item_id, failReason="checkin_unknown", maybeCreated=True)
        else:
            await pending_checkins.release_item(user_id, item_id)
        return ws._json_error("checkin_failed", 502)

    await ws._apply_checkin_side_effects(
        user_id, item["beerId"], item.get("rating") or 0, item.get("foursquareId"), item.get("queueItemId"),
    )
    await pending_checkins.remove_item(user_id, item_id)
    return web.json_response({"ok": True, "checkin": checkin})


async def handle_pending_remove(request: web.Request) -> web.Response:
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")

    item_id = body.get("id")
    if not item_id:
        return ws._json_error("invalid_id")

    removed = await pending_checkins.remove_item(user_id, item_id)
    return web.json_response({"ok": True, "removed": removed})
