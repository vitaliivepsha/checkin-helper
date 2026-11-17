"""The festival watch: its settings routes, the novelty notifications (a new beer near the chosen point or at the watched
venue) and the background polls that feed them - the venue check-ins poll (direct pool first, the owner's MCP token as a
fallback), the friends feed poll and the scrape of extra venues.

Moved out of webapp_server.py (audit A1) without changing behaviour. Shared names that stay in webapp_server
(configuration, state tests patch, helpers used elsewhere) are reached through `ws`, a late-bound view of that module:
attributes are looked up at call time, so monkeypatching webapp_server still reaches this code."""

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from urllib.parse import parse_qsl, quote, urljoin
import asyncio
import auto_toast
import event_log
import festival_mode
import festival_watch
import had_it_index
import html
import i18n
import json
import time
import untappd_direct
import untappd_mcp
import user_tokens
import venue_scrape

from web_shared import ws


_festival_watch_venue_task = None  # module-level, keeps the asyncio.create_task result alive (avoid GC)


_festival_watch_scrape_task = None  # same, for _festival_watch_scrape_loop


# Anti-spam cap for _notify_festival_novelty (see its own docstring) - a
# busy festival tap can get checked in by many different people in quick
# succession, which would otherwise flood the owner's feed with one
# near-identical "🆕 X just checked in Y" message per person. Deliberately
# in-memory, not persisted - a restart naturally clearing the window is
# fine for a soft anti-spam cap, unlike real state.
_FESTIVAL_NOVELTY_NOTIFY_MAX_PER_HOUR = 3


_festival_novelty_notify_counts: dict[tuple[int, int], tuple[int, float]] = {}


# Check-in ids already handled per owner - the friends+radius check and the
# venue-checkins loop can both see the same check-in (a friend at the main
# venue). In-memory only: both sources have persisted cursors, so a restart
# doesn't replay anything. Bounded per owner, oldest evicted first.
_FESTIVAL_NOVELTY_SEEN_MAX = 500


_festival_novelty_seen: dict[int, dict[int, None]] = {}


def _festival_novelty_first_sight(owner_id: int, checkin_id: int | None) -> bool:
    """True the first time this (owner, check-in) is seen, False after - and
    always True when the item carries no checkin_id (nothing to dedup on)."""
    if checkin_id is None:
        return True
    seen = _festival_novelty_seen.setdefault(owner_id, {})
    if checkin_id in seen:
        return False
    seen[checkin_id] = None
    if len(seen) > _FESTIVAL_NOVELTY_SEEN_MAX:
        del seen[next(iter(seen))]
    return True


def _festival_novelty_notify_allowed(owner_id: int, bid: int) -> bool:
    """False once this exact (owner, beer) pair has already been notified
    about _FESTIVAL_NOVELTY_NOTIFY_MAX_PER_HOUR times in the last rolling
    hour - counts every check-in of that beer toward the same cap
    regardless of who checked it in, per the whole point of this limiter.
    Recording a fresh hit is folded into the same call (not a separate
    "record" step) since every caller immediately wants to send on a True
    result - there is no legitimate reason to check without also counting."""
    now = time.time()
    key = (owner_id, bid)
    count, window_start = _festival_novelty_notify_counts.get(key, (0, now))
    if now - window_start >= 3600:
        count, window_start = 0, now
    if count >= _FESTIVAL_NOVELTY_NOTIFY_MAX_PER_HOUR:
        return False
    _festival_novelty_notify_counts[key] = (count + 1, window_start)
    return True


async def handle_festival_watch_get(request: web.Request) -> web.Response:
    """Current festival-watch config for the "🆕 Новинки" screen - never
    needs an Untappd token, festival_watch.py never calls Untappd itself."""
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    cfg = await festival_watch.get_config(user_id)
    return web.json_response(cfg)


async def handle_festival_watch_set_location(request: web.Request) -> web.Response:
    """Sets the watch point - from either the Mini App's GPS capture
    (ensureLocationManager, same mechanism "🧭 Локації поруч" already uses,
    no foursquareId available for a bare GPS point) or picking a named
    place from the existing Foursquare venue search (which does carry one).

    When a foursquareId is given AND DIRECT_TOKEN is configured, tries to
    resolve it to a real Untappd venue_id (untappd_direct.
    lookup_venue_by_foursquare) and attaches it via festival_watch.
    set_venue - this is what upgrades the watch from the old friends-only
    GPS+radius novelty check to the venue-checkins one that sees everyone
    (see _festival_watch_venue_loop). Resolution failing for any reason
    (no DIRECT_TOKEN, this place has no known Untappd venue yet, a
    transient API error) is NOT a request failure - the watch point is
    still set and still works via the plain GPS+radius path, just without
    the upgrade; never worth failing the whole "set my location" action
    over."""
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    if not await ws._is_known_user(user_id):
        return ws._json_error("not_allowed", 403)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")
    lat, lng = body.get("lat"), body.get("lng")
    if lat is None or lng is None:
        return ws._json_error("invalid_location")
    await festival_watch.set_location(user_id, float(lat), float(lng), body.get("label"))

    foursquare_id = body.get("foursquareId")
    if foursquare_id is not None and not ws._valid_foursquare_id(foursquare_id):
        return ws._json_error("invalid_foursquare_id")
    if foursquare_id and ws.DIRECT_TOKEN:
        try:
            with untappd_direct.caller("venue_lookup"):
                resolved = await untappd_direct.lookup_venue_by_foursquare(ws.DIRECT_TOKEN, foursquare_id)
        except untappd_mcp.UntappdMCPError as e:
            ws.logger.warning("festival_watch venue resolution failed for %s: %s", foursquare_id, e)
            resolved = None
        if resolved:
            await festival_watch.set_venue(user_id, resolved["venueId"], resolved["venueName"])

    return web.json_response({"ok": True})


async def handle_festival_watch_add_extra_venue(request: web.Request) -> web.Response:
    """Adds a picked Foursquare place to the owner's extra-venues list
    (festival_watch.add_extra_venue, scraped by _festival_watch_scrape_loop).
    Unlike set_location, a failed Untappd venue resolution IS an error here -
    an extra venue has no GPS+radius fallback, it only exists as a venue_id.
    Costs one DIRECT_TOKEN call (the foursquare lookup), once, at add time."""
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    if not await ws._is_known_user(user_id):
        return ws._json_error("not_allowed", 403)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")
    foursquare_id = body.get("foursquareId")
    if not foursquare_id or not ws._valid_foursquare_id(foursquare_id):
        return ws._json_error("invalid_venue")
    if not ws.DIRECT_TOKEN:
        return ws._json_error("no_direct_token", 503)
    try:
        with untappd_direct.caller("venue_lookup"):
            resolved = await untappd_direct.lookup_venue_by_foursquare(ws.DIRECT_TOKEN, foursquare_id)
    except untappd_mcp.UntappdRateLimited:
        return ws._json_error("rate_limited", 429)
    except untappd_mcp.UntappdMCPError as e:
        ws.logger.warning("festival_watch extra venue resolution failed for %s: %s", foursquare_id, e)
        return ws._json_error("lookup_failed", 502)
    if not resolved:
        return ws._json_error("venue_not_found", 404)
    added = await festival_watch.add_extra_venue(user_id, resolved["venueId"], resolved["venueName"] or body.get("name"))
    if not added:
        return ws._json_error("already_listed_or_full", 409)
    return web.json_response({"ok": True})


async def handle_festival_watch_remove_extra_venue(request: web.Request) -> web.Response:
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    user_id = (init_data.get("user") or {}).get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")
    removed = await festival_watch.remove_extra_venue(user_id, body.get("venueId"))
    return web.json_response({"ok": True, "removed": removed})


async def handle_festival_watch_set_radius(request: web.Request) -> web.Response:
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")
    meters = body.get("radiusMeters")
    if not isinstance(meters, (int, float)) or meters <= 0:
        return ws._json_error("invalid_radius")
    await festival_watch.set_radius(user_id, int(meters))
    return web.json_response({"ok": True})


async def handle_festival_watch_toggle(request: web.Request) -> web.Response:
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")
    await festival_watch.set_enabled(user_id, bool(body.get("enabled")))
    return web.json_response({"ok": True})


async def handle_festival_watch_styles(request: web.Request) -> web.Response:
    """The style names the novelty filter's picker offers."""
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    return web.json_response({"styles": festival_watch.styles_taxonomy()})


async def handle_festival_watch_set_filter(request: web.Request) -> web.Response:
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    user_id = (init_data.get("user") or {}).get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")
    done = await festival_watch.set_novelty_filter(
        user_id, body.get("styles"), body.get("ratingMin"), body.get("ratingMax"),
    )
    if not done:
        return ws._json_error("invalid_filter")
    return web.json_response({"ok": True})


async def handle_festival_watch_set_notify_listed(request: web.Request) -> web.Response:
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")
    await festival_watch.set_notify_listed(user_id, bool(body.get("enabled")))
    return web.json_response({"ok": True})


async def _novelty_item_passes_filter(owner_id: int, watch: dict, item: dict, bid: int | None) -> bool:
    """The owner's style/average-rating filter (festival_watch.
    set_novelty_filter) applied to one candidate check-in. Cheap when the
    filter is at its defaults (nothing is looked up at all)."""
    if not festival_watch.novelty_filter_active(watch):
        return True
    style = (item.get("beer") or {}).get("beer_style")
    rating = None
    if bid is not None and festival_watch.novelty_filter_needs_facts(watch, style):
        facts = await ws._novelty_beer_facts(owner_id, bid)
        if facts is not None:
            style = style or facts.get("style")
            rating = facts.get("rating")
    return festival_watch.novelty_passes_filter(watch, style, rating)


async def _check_festival_novelty(owner_id: int, items: list[dict]) -> None:
    """Sends a Telegram message for any item in `items` that's within the
    owner's saved festival_watch radius AND whose beer isn't on the current
    festival beer list - a tap change or surprise release, the thing the
    static list can't know about. Not about "have I had this" (that's
    auto_toast/had_it_index's question) - deliberately fires regardless of
    whether the owner personally cares about that specific beer, since the
    point is noticing something changed at a specific physical place.

    No Untappd quota cost - runs against the same feed page
    _auto_toast_loop already fetched for auto-toast, so this is naturally
    only checked as often as that loop polls, and only while the owner has
    it enabled with an owner-picked center point (see festival_watch.py).

    Note this only ever sees the owner's own Untappd *friends* -
    get_my_friend_feed is "checkin/recent," which is friends-only by
    Untappd's own definition. Runs even when the owner's watch point has
    resolved to a real venue_id and _festival_watch_venue_loop is polling it
    (which sees EVERYONE at that exact venue): venue/checkins only returns
    check-ins attributed to that one venue_id, so a friend logging the same
    pour at a neighbouring venue is invisible there but still caught here by
    radius. The two overlap on friends at the main venue - dedup by
    checkin_id lives in _notify_festival_novelty."""
    if not items:
        return
    watch = await festival_watch.get_config(owner_id)
    if not watch["enabled"] or watch["lat"] is None or watch["lng"] is None:
        return
    profile = await user_tokens.get_profile(owner_id)
    own_username_lower = (profile or {}).get("username", "").lower()
    candidates = []
    for item in items:
        username = (item.get("user") or {}).get("user_name") or "?"
        if username.lower() == own_username_lower:
            continue  # the feed includes the owner's own check-ins too - not interesting to notify about
        venue = (item.get("venue") or {}).get("location") or {}
        lat, lng = venue.get("lat"), venue.get("lng")
        if lat is None or lng is None:
            continue
        if not festival_watch.is_within(lat, lng, watch["lat"], watch["lng"], watch["radiusMeters"]):
            continue
        candidates.append(item)
    await _notify_festival_novelty(owner_id, candidates, watch.get("label"))


async def _notify_festival_novelty(owner_id: int, items: list[dict], venue_label_fallback: str | None) -> None:
    """Shared tail for both novelty sources (_check_festival_novelty's
    friends-only/radius path and _festival_watch_venue_loop's venue-checkins
    path) - `items` must already be exactly the candidates worth notifying
    about (own-username and radius/venue filtering both already done by the
    caller). A check-in both sources see is handled once (see
    _festival_novelty_first_sight). Per beer, in order:

    1. Already in the owner's active group's shared queue
       (_beer_already_queued) - skip entirely, the group already knows.
    2. Already pinged _FESTIVAL_NOVELTY_NOTIFY_MAX_PER_HOUR times this hour
       for this exact beer (_festival_novelty_notify_allowed) - skip, a
       busy tap shouldn't flood the feed just because many different
       people are checking the same thing in.
    3. On the festival's own list but not yet queued - a DIFFERENT message
       ("📋 ... in the festival's list, not in the queue yet") than genuine
       novelty, since it's not actually new to the static list, just to the
       group's tracked queue.
    4. Off the list entirely - the original "🆕 novelty" message, now also
       noting when had_it_index confirms the OWNER has already had this
       beer before (free, local lookup - no extra quota)."""
    if not items:
        return
    # The owner's OWN festival (personal or group-bound), not the global default one (audit R9): otherwise a beer
    # on their list is reported as new, and one that is new to the default list but on theirs is missed.
    festival_key = await ws._resolve_festival_key(owner_id)
    owner_beers, owner_sessions_raw = ws._festival_data_for(festival_key)
    _, owner_session_beer_ids, _, _ = ws._derive_session_data(owner_beers, owner_sessions_raw)
    known_beer_ids = ws._all_festival_beer_ids(owner_session_beer_ids)
    watch_config = await festival_watch.get_config(owner_id)
    notify_listed = watch_config["notifyListedBeers"]
    # The owner's last-seen language (set when they open the Mini App);
    # "uk" until they ever have, which is what these messages always were.
    lang = await user_tokens.get_language(owner_id) or "uk"
    venue_label_fallback = venue_label_fallback or i18n.t(lang, "nov_venue_fallback")
    for item in items:
        if not _festival_novelty_first_sight(owner_id, item.get("checkin_id")):
            continue  # already handled via the other novelty source
        username = (item.get("user") or {}).get("user_name") or "?"
        raw_bid = (item.get("beer") or {}).get("bid")
        bid = int(raw_bid) if raw_bid is not None else None

        if bid is not None and await ws._beer_already_queued(owner_id, bid):
            continue  # rule 1: already tracked in the shared queue
        if not await _novelty_item_passes_filter(owner_id, watch_config, item, bid):
            continue  # not one of the styles / outside the average-rating range the owner asked for
        if bid is not None and not _festival_novelty_notify_allowed(owner_id, bid):
            continue  # rule 4 (spam cap): already pinged enough this hour

        beer_name = (item.get("beer") or {}).get("beer_name") or "?"
        brewery_id = (item.get("brewery") or {}).get("brewery_id")
        brewery_name = (item.get("brewery") or {}).get("brewery_name") or "?"
        venue_id = (item.get("venue") or {}).get("venue_id")
        venue_name = (item.get("venue") or {}).get("venue_name") or venue_label_fallback

        # HTML with escaping, not plain text: a bare "@username" in a plain
        # Telegram message gets auto-linkified by the client into whatever
        # *Telegram* account happens to have that username - completely
        # unrelated to the real Untappd profile, and confusing/misleading
        # (confirmed live: it pointed at a stranger's Telegram, not
        # Untappd). Every name below links to its own real Untappd page
        # instead (beer/brewery/venue ids are all present on the same
        # already-fetched feed item, no extra lookup); everything is
        # untrusted external content, escaped.
        profile_link = ws._html_link(f"https://untappd.com/user/{html.escape(username)}", f"@{username}")
        beer_link = ws._html_link(f"https://untappd.com/beer/{bid}", beer_name) if bid is not None else html.escape(beer_name)
        brewery_link = (
            ws._html_link(f"https://untappd.com/brewery/{brewery_id}", brewery_name)
            if brewery_id is not None else html.escape(brewery_name)
        )
        venue_link = (
            ws._html_link(f"https://untappd.com/venue/{venue_id}", venue_name)
            if venue_id is not None else html.escape(venue_name)
        )

        on_list = bid is not None and bid in known_beer_ids
        reply_markup = None
        if on_list:
            if not notify_listed:
                continue  # opted out of "on the list, not queued yet" pings (see set_notify_listed)
            # rule 2: known festival beer, just not queued yet.
            text = i18n.t(
                lang, "nov_listed",
                profile=profile_link, beer=beer_link, brewery=brewery_link, venue=venue_link,
            )
            event_text = i18n.t(lang, "nov_event_listed", user=username, beer=beer_name, venue=venue_name)
        else:
            # rule 3: genuine novelty, off the static list entirely - note
            # when the OWNER (the one receiving this DM) has already had
            # this exact beer before, so they know not to rush for it.
            already_had = False
            if bid is not None:
                had = await had_it_index.lookup_had_it(owner_id, bid)
                already_had = bool(had and had.get("hadIt"))
            had_note = i18n.t(lang, "nov_had_yes" if already_had else "nov_had_no")
            text = i18n.t(
                lang, "nov_new",
                profile=profile_link, beer=beer_link, brewery=brewery_link, venue=venue_link, had=had_note,
            )
            event_text = i18n.t(lang, "nov_event_new", user=username, beer=beer_name, venue=venue_name) + (
                i18n.t(lang, "nov_event_had_suffix") if already_had else ""
            )

            # Even though THIS beer isn't in the festival's list, its credited
            # brewery (or whichever real stand it aliases to - see
            # _festival_brewery_aliases, e.g. a collab poured at a host's
            # stand) might already have a pin on the map from its OTHER
            # beers - worth a direct "Відкрити на карті" deep-link then,
            # same web_app button pattern as bot.py's /checkin (the only
            # one confirmed to carry real initData into the WebView).
            if ws._public_base_url:
                zone_hint = ws._festival_brewery_zone_map(owner_beers)
                stand_brewery = ws._festival_brewery_aliases(owner_beers).get(brewery_name, brewery_name)
                zone = zone_hint.get(stand_brewery)
                if zone:
                    map_url = (
                        f"{ws._public_base_url}/checkin"
                        f"?mapZone={quote(zone)}&mapBrewery={quote(stand_brewery)}"
                    )
                    reply_markup = InlineKeyboardMarkup(
                        [[InlineKeyboardButton(i18n.t(lang, "nov_open_on_map"), web_app=WebAppInfo(url=map_url))]]
                    )

        await event_log.add_event(
            owner_id, "novelty", event_text,
            beer_id=bid, checkin_id=item.get("checkin_id"), username=username,
        )
        try:
            await ws._ptb_bot.send_message(chat_id=owner_id, text=text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            ws.logger.exception("festival_watch: failed to notify owner %s", owner_id)


def _start_festival_watch_venue() -> None:
    global _festival_watch_venue_task
    if _festival_watch_venue_task and not _festival_watch_venue_task.done():
        return
    _festival_watch_venue_task = asyncio.create_task(_festival_watch_venue_loop())


async def _festival_watch_friends_owner() -> int | None:
    """The owner whose friends feed _festival_watch_venue_loop must poll
    itself (via DIRECT_TOKEN) because _check_festival_novelty's usual ride on
    _auto_toast_loop's feed isn't running for them - festival mode skips that
    loop entirely, and it also needs auto-toast enabled with a target. Only
    ever the bot owner: checkin/recent returns the TOKEN's OWN friends, and
    DIRECT_TOKEN is the owner's account (same restriction as _get_had_it's
    direct fallback). Also resets the poll's cursor whenever it isn't
    needed, so the first poll after it becomes needed again re-baselines
    instead of replaying whatever happened in between."""
    if not (ws.DIRECT_TOKEN and ws.OWNER_TELEGRAM_ID):
        return None
    owner_id = int(ws.OWNER_TELEGRAM_ID)
    watch = await festival_watch.get_config(owner_id)
    if not watch["enabled"] or watch["lat"] is None or watch["lng"] is None:
        await festival_watch.record_friends_tick(owner_id, None)
        return None
    toast = await auto_toast.get_config(owner_id)
    auto_toast_covers_it = (
        toast["enabled"] and toast["targets"]
        and not await festival_mode.is_enabled(owner_id)
    )
    if auto_toast_covers_it:
        await festival_watch.record_friends_tick(owner_id, None)
        return None
    return owner_id


async def _poll_festival_friends(owner_id: int) -> None:
    """One direct friends-feed pass for _check_festival_novelty - same
    cursor/bootstrap/stale-min_id handling as the venue polls above."""
    min_id = (await festival_watch.get_config(owner_id))["friendsLastCheckinId"]
    try:
        with untappd_direct.caller("festival_friends"):
            page = await untappd_direct.get_my_friend_feed(ws.DIRECT_TOKEN, limit=50, min_id=min_id)
    except untappd_mcp.UntappdMCPError as e:
        if min_id is None or "min_id" not in str(e).lower():
            raise
        with untappd_direct.caller("festival_friends"):
            page = await untappd_direct.get_my_friend_feed(ws.DIRECT_TOKEN, limit=50)
        min_id = None
    items = (page.get("checkins") or {}).get("items", [])
    if not items:
        return
    newest_id = max((it.get("checkin_id") or 0) for it in items)
    if min_id is not None:
        await _check_festival_novelty(
            owner_id, [it for it in items if (it.get("checkin_id") or 0) > min_id],
        )
    await festival_watch.record_friends_tick(owner_id, newest_id)


async def _venue_checkins_page(owner_id: int, venue_id: int, limit: int, min_id: int | None) -> dict | None:
    """One page of a venue's public check-ins from the best pool with headroom: DIRECT_TOKEN's own pool first (as
    always), the owner's MCP token (get_venue_checkins on the MCP server) only when that is spent. None when
    neither has room - the tick is skipped, never retried."""
    if ws.DIRECT_TOKEN and ws._quota_allows(untappd_direct.get_api_usage(), ws.FESTIVAL_WATCH_VENUE_MIN_REMAINING):
        return await untappd_direct.get_venue_checkins(ws.DIRECT_TOKEN, venue_id, limit=limit, min_id=min_id)
    token = await user_tokens.get_token(owner_id)
    if not token:
        return None
    usage = await untappd_mcp.get_untappd_api_usage(token)   # free
    if not ws._quota_allows(usage, ws.FESTIVAL_WATCH_VENUE_MCP_FALLBACK_MIN_REMAINING):
        return None
    ws.logger.info("festival_watch venue poll: direct pool spent, using the owner's MCP token")
    return await untappd_mcp.get_venue_checkins(token, venue_id, limit=limit, min_id=min_id)


async def _festival_watch_venue_loop() -> None:
    """Independent poll of venue/checkins/{venue_id} for every owner whose
    festival_watch point has resolved to a real Untappd venue (see
    festival_watch.set_venue) - sees EVERYONE checking in there, unlike
    _check_festival_novelty's friends-only feed piggyback (the whole reason
    untappd_direct.py and this loop exist - see README.md).

    Uses DIRECT_TOKEN (untappd_direct's own separate quota pool) first; the
    MCP server's get_venue_checkins is only a fallback for when that pool is
    spent (see _venue_checkins_page). A no-op forever if DIRECT_TOKEN isn't
    configured (no owner's watch
    point can ever have resolved to a venueId in that case either - see
    handle_festival_watch_set_location - so list_venue_jobs() would just
    always come back empty).

    Only the MAIN venue is polled here (API quota); extra venues are scraped
    by _festival_watch_scrape_loop. The bot owner's friends
    feed is also polled here whenever _auto_toast_loop isn't covering it
    (see _festival_watch_friends_owner), so a friend logging a new beer at a
    different venue within the radius is still caught during festival mode.

    Cursor-based like auto_toast's own feed walk, but deliberately simpler:
    no multi-tick catchup walk for a page-cap overrun - a single venue's
    check-in volume between ticks is expected to be small (a handful of
    people at one place), unlike a whole friend feed. A tick that
    genuinely maxes out FESTIVAL_WATCH_VENUE_CHECK_LIMIT just catches the
    rest on the next tick from the same cursor, same as a normal
    lower-than-limit page would leave nothing missed - min_id already
    bounds the query so nothing in between is skipped either way."""
    untappd_direct.set_caller("festival_venue")
    await asyncio.sleep(5)  # let the server finish binding first
    while True:
        # Default floor - overwritten below once `targets` is known; stays
        # at the floor if an exception hits before that (the final sleep at
        # the bottom of the loop always needs a defined value to fall back
        # to, exception or not).
        tick_interval = ws.FESTIVAL_WATCH_VENUE_INTERVAL_SECONDS
        try:
            if not ws.DIRECT_TOKEN:
                await asyncio.sleep(ws.FESTIVAL_WATCH_VENUE_IDLE_SLEEP_SECONDS)
                continue
            targets = await festival_watch.list_venue_jobs()
            friends_owner = await _festival_watch_friends_owner()
            if not targets and friends_owner is None:
                await asyncio.sleep(ws.FESTIVAL_WATCH_VENUE_IDLE_SLEEP_SECONDS)
                continue
            # See FESTIVAL_WATCH_VENUE_BUDGET_PER_HOUR's own comment - spreads
            # the same total hourly call budget across however many targets
            # are currently enabled, instead of every target adding its own
            # fixed-interval load on top of the others. The direct friends-
            # feed poll costs one more call per pass.
            call_count = len(targets) + (1 if friends_owner is not None else 0)
            tick_interval = max(
                ws.FESTIVAL_WATCH_VENUE_INTERVAL_SECONDS,
                3600 * call_count / ws.FESTIVAL_WATCH_VENUE_BUDGET_PER_HOUR,
            )

            usage = untappd_direct.get_api_usage()  # free, no network call
            direct_ok = ws._quota_allows(usage, ws.FESTIVAL_WATCH_VENUE_MIN_REMAINING)
            if not direct_ok and not targets:
                await asyncio.sleep(tick_interval)   # only the friends poll is due, and it needs the direct pool
                continue

            for target in targets:
                owner_id = target["ownerId"]
                try:
                    # Deliberately NOT gated on festival_mode.is_enabled, unlike
                    # every other DIRECT_TOKEN consumer (had_it/venue backfill,
                    # auto_toast) - festival mode being ON is exactly when this
                    # venue-novelty monitoring should be running, not paused.
                    # It always spends DIRECT_TOKEN's own separate quota pool
                    # (see this loop's own docstring), never the owner's own
                    # MCP-token quota, so it never competes with their live
                    # search/check-in traffic either way. Pausing it here used
                    # to mean the one loop festival mode should be FUNDING
                    # (freeing up DIRECT_TOKEN headroom by pausing the other
                    # consumers) was itself the first thing turned off.
                    min_id = target["lastCheckinId"]
                    try:
                        page = await _venue_checkins_page(
                            owner_id, target["venueId"], ws.FESTIVAL_WATCH_VENUE_CHECK_LIMIT, min_id,
                        )
                        if page is None:
                            break   # neither pool has room this tick
                    except untappd_mcp.UntappdMCPError as e:
                        # Confirmed live: Untappd rejects a min_id pointing
                        # further back than ~10 days for this endpoint
                        # ("Your 'min_id' ... must be greater than 10 days
                        # from now") - happens once this venue goes 10+
                        # days without a new check-in, and would otherwise
                        # repeat this exact error on EVERY tick forever
                        # (the cursor never advances on a raised exception).
                        # Nothing in that >10-day gap is worth notifying
                        # about by the time we'd ever see it anyway, so
                        # treat it exactly like a fresh bootstrap instead:
                        # re-baseline the cursor to "now" rather than
                        # retrying the same doomed min_id forever.
                        if min_id is None or "min_id" not in str(e).lower():
                            raise
                        page = await _venue_checkins_page(
                            owner_id, target["venueId"], ws.FESTIVAL_WATCH_VENUE_CHECK_LIMIT, None,
                        )
                        if page is None:
                            break
                        min_id = None
                    items = (page.get("checkins") or {}).get("items", [])
                    if not items:
                        continue
                    newest_id = max((it.get("checkin_id") or 0) for it in items)

                    if min_id is None:
                        # Bootstrap (first-ever watch, or a just-reset
                        # stale cursor): note the newest id as the new
                        # baseline, don't notify about anything older.
                        await festival_watch.record_venue_tick(owner_id, newest_id)
                        continue

                    profile = await user_tokens.get_profile(owner_id)
                    own_username_lower = (profile or {}).get("username", "").lower()
                    candidates = [
                        it for it in items
                        if ((it.get("user") or {}).get("user_name") or "").lower() != own_username_lower
                    ]
                    await _notify_festival_novelty(owner_id, candidates, target["venueName"])
                    await festival_watch.record_venue_tick(owner_id, newest_id)
                except untappd_mcp.UntappdRateLimited:
                    break  # this tick's shared DIRECT_TOKEN quota is spent - skip remaining targets, retry next tick
                except untappd_mcp.UntappdMCPError as e:
                    ws.logger.warning("festival_watch venue tick failed for owner %s: %s", owner_id, e)
                except Exception:
                    ws.logger.exception("festival_watch venue tick failed for owner %s", owner_id)
            if friends_owner is not None and direct_ok:
                try:
                    await _poll_festival_friends(friends_owner)
                except untappd_mcp.UntappdRateLimited:
                    pass  # retry next pass, same as the venue polls
                except untappd_mcp.UntappdMCPError as e:
                    ws.logger.warning("festival_watch friends poll failed for owner %s: %s", friends_owner, e)
                except Exception:
                    ws.logger.exception("festival_watch friends poll failed for owner %s", friends_owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            ws.logger.exception("festival_watch venue loop tick failed")
        await asyncio.sleep(tick_interval)


def _start_festival_watch_scrape() -> None:
    global _festival_watch_scrape_task
    if _festival_watch_scrape_task and not _festival_watch_scrape_task.done():
        return
    _festival_watch_scrape_task = asyncio.create_task(_festival_watch_scrape_loop())


async def _festival_watch_scrape_loop() -> None:
    """Scrapes every owner's extra venues (festival_watch.add_extra_venue)
    via venue_scrape - logged-out public activity pages, so no Untappd API
    quota and no DIRECT_TOKEN needed, which is why the list can be long
    (unlike the quota-bound main venue in _festival_watch_venue_loop).
    Same cursor/bootstrap idea as that loop: the first pass over a venue only
    records the newest check-in id, later passes notify about newer ones via
    the shared _notify_festival_novelty (deduped by checkin_id).

    Fragile by nature - Cloudflare can start challenging this server at any
    time. A block backs the whole loop off exponentially (up to
    FESTIVAL_WATCH_SCRAPE_MAX_BACKOFF_SECONDS) instead of retrying hard, and
    resets after the next clean pass. A single venue failing otherwise
    (layout change, venue has no stream) is just logged and skipped."""
    await asyncio.sleep(8)  # let the server finish binding first
    backoff = 0.0
    while True:
        interval = ws.FESTIVAL_WATCH_SCRAPE_INTERVAL_SECONDS
        try:
            jobs = await festival_watch.list_extra_venue_jobs()
            if not jobs:
                await asyncio.sleep(ws.FESTIVAL_WATCH_VENUE_IDLE_SLEEP_SECONDS)
                continue
            blocked = False
            fetched: dict[int, list[dict]] = {}  # two owners sharing a venue = one request
            for job in jobs:
                try:
                    await ws._scrape_extra_venue(job, fetched)
                except venue_scrape.ScrapeBlocked as e:
                    ws.logger.warning("festival_watch scrape blocked (%s) - backing off", e)
                    blocked = True
                    break
                except venue_scrape.ScrapeError as e:
                    ws.logger.warning("festival_watch scrape of venue %s failed: %s", job["venueId"], e)
                except Exception:
                    ws.logger.exception("festival_watch scrape of venue %s failed", job["venueId"])
                await asyncio.sleep(ws.FESTIVAL_WATCH_SCRAPE_GAP_SECONDS)
            if blocked:
                backoff = min(max(backoff * 2, interval), ws.FESTIVAL_WATCH_SCRAPE_MAX_BACKOFF_SECONDS)
                interval = backoff
            else:
                backoff = 0.0
        except asyncio.CancelledError:
            raise
        except Exception:
            ws.logger.exception("festival_watch scrape loop tick failed")
        await asyncio.sleep(interval)
