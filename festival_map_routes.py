"""The festival map editor routes: reading the layout, moving breweries between zones, gaps and spacers, islands, zone order
and alignment. Editing is limited to known users (audit S3/S4); the layout itself lives in festival_map.py.

Moved out of webapp_server.py (audit A1) without changing behaviour. Shared names that stay in webapp_server
(configuration, state tests patch, helpers used elsewhere) are reached through `ws`, a late-bound view of that module:
attributes are looked up at call time, so monkeypatching webapp_server still reaches this code."""

from aiohttp import web
import festival_map
import json

from web_shared import ws


async def handle_festival_map_get(request: web.Request) -> web.Response:
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    tg_user = init_data.get("user") or {}
    user_id = tg_user.get("id")

    festival_key = await ws._resolve_festival_key(user_id)
    return web.json_response(await _festival_map_payload(festival_key))


async def _festival_map_payload(festival_key: str | None) -> dict:
    beers, _ = ws._festival_data_for(festival_key)
    map_key = festival_key or ws._active_festival_key

    template = ws._festival_layout_template(map_key)
    zone_names = ws._festival_editable_zone_names(beers, template)
    zone_hint = ws._festival_brewery_zone_map(beers)
    known_breweries = list(zone_hint.keys())
    # The whole floor plan shows up from day one: plan stands with no beers
    # in the data yet are placeholders (see festival_map.planned_stands).
    planned = festival_map.planned_stands(template, known_breweries)
    zones = await festival_map.get_layout(
        map_key, known_breweries + planned, zone_hint, zone_names, template=template,
    )
    return {
        "zones": zones,
        "plannedStands": planned,
        "waterStands": festival_map.water_stands(template, known_breweries + planned),
        "zoneOrder": festival_map.apply_zone_order(zone_names, await festival_map.get_zone_order(map_key)),
        "zoneLabels": ws._zone_labels_for(map_key),
        "bonusCategories": ws._festival_bonus_categories(beers),
        "breweryAliases": ws._festival_brewery_aliases(beers),
    }


async def handle_festival_map_move(request: web.Request) -> web.Response:
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

    festival_key = await ws._resolve_festival_key(user_id)
    beers, _ = ws._festival_data_for(festival_key)
    map_key = festival_key or ws._active_festival_key

    brewery = body.get("brewery")
    zone = body.get("zone")
    side = body.get("side")
    index = body.get("index")
    island_id = body.get("islandId")
    zone_map = ws._festival_brewery_zone_map(beers)
    if not isinstance(brewery, str) or (
        brewery not in zone_map
        and brewery not in festival_map.planned_stands(ws._festival_layout_template(map_key), list(zone_map))
    ):
        return ws._json_error("invalid_brewery")
    if not isinstance(index, int) or index < 0:
        return ws._json_error("invalid_index")
    if island_id is not None and not isinstance(island_id, str):
        return ws._json_error("invalid_island")

    moved = await festival_map.move_brewery(
        map_key, brewery, zone, side, index, ws._festival_editable_zone_names(beers, ws._festival_layout_template(map_key)), island_id=island_id
    )
    if not moved:
        return ws._json_error("invalid_zone")
    return web.json_response({"ok": True})


async def _handle_festival_map_gap(request: web.Request, edit) -> web.Response:
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    user_id = (init_data.get("user") or {}).get("id")
    if not await ws._is_known_user(user_id):
        return ws._json_error("not_allowed", 403)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")
    index = body.get("index")
    island_id = body.get("islandId")
    if not isinstance(index, int) or index < 0:
        return ws._json_error("invalid_index")
    if island_id is not None and not isinstance(island_id, str):
        return ws._json_error("invalid_island")
    festival_key = await ws._resolve_festival_key(user_id)
    beers, _ = ws._festival_data_for(festival_key)
    map_key = festival_key or ws._active_festival_key
    done = await edit(
        map_key, body.get("zone"), body.get("side"), index, ws._festival_editable_zone_names(beers, ws._festival_layout_template(map_key)), island_id=island_id
    )
    if not done:
        return ws._json_error("invalid_gap")
    return web.json_response({"ok": True})


async def handle_festival_map_gap_insert(request: web.Request) -> web.Response:
    return await _handle_festival_map_gap(request, festival_map.insert_gap)


async def handle_festival_map_gap_remove(request: web.Request) -> web.Response:
    return await _handle_festival_map_gap(request, festival_map.remove_gap)


async def _festival_map_island_call(request: web.Request, call) -> web.Response:
    """Shared auth/body/zone-list plumbing of the island handlers below: `call`
    gets (map_key, body, zone_names) and returns the response payload dict, or
    None for an invalid request."""
    init_data = await ws._require_valid_init_data(request)
    if not init_data:
        return ws._json_error("invalid_init_data", 401)
    user_id = (init_data.get("user") or {}).get("id")
    if not await ws._is_known_user(user_id):
        return ws._json_error("not_allowed", 403)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return ws._json_error("invalid_json")
    festival_key = await ws._resolve_festival_key(user_id)
    beers, _ = ws._festival_data_for(festival_key)
    map_key = festival_key or ws._active_festival_key
    result = await call(map_key, body, ws._festival_editable_zone_names(beers, ws._festival_layout_template(map_key)))
    if result is None:
        return ws._json_error("invalid_request")
    return web.json_response({"ok": True, **result})


async def handle_festival_map_island_spacer(request: web.Request) -> web.Response:
    async def call(map_key, body, zone_names):
        index = body.get("index")
        if not isinstance(index, int):
            return None
        island_id = await festival_map.insert_island_spacer(map_key, body.get("zone"), index, zone_names)
        return None if island_id is None else {"islandId": island_id}
    return await _festival_map_island_call(request, call)


async def handle_festival_map_zone_order(request: web.Request) -> web.Response:
    async def call(map_key, body, zone_names):
        done = await festival_map.set_zone_order(map_key, body.get("order"), zone_names)
        return {} if done else None
    return await _festival_map_island_call(request, call)


async def handle_festival_map_island_align(request: web.Request) -> web.Response:
    async def call(map_key, body, zone_names):
        brewery, island_id, align = body.get("brewery"), body.get("islandId"), body.get("align")
        if not isinstance(brewery, str) or not isinstance(island_id, str) or not isinstance(align, str):
            return None
        done = await festival_map.set_island_align(map_key, body.get("zone"), island_id, brewery, align, zone_names)
        return {} if done else None
    return await _festival_map_island_call(request, call)


async def handle_festival_map_island_create(request: web.Request) -> web.Response:
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

    festival_key = await ws._resolve_festival_key(user_id)
    beers, _ = ws._festival_data_for(festival_key)
    map_key = festival_key or ws._active_festival_key

    zone = body.get("zone")
    island_id = await festival_map.create_island(map_key, zone, ws._festival_editable_zone_names(beers, ws._festival_layout_template(map_key)))
    if island_id is None:
        return ws._json_error("invalid_zone")
    return web.json_response({"ok": True, "islandId": island_id})


async def handle_festival_map_island_delete(request: web.Request) -> web.Response:
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

    festival_key = await ws._resolve_festival_key(user_id)
    beers, _ = ws._festival_data_for(festival_key)
    map_key = festival_key or ws._active_festival_key

    zone = body.get("zone")
    island_id = body.get("islandId")
    if not isinstance(island_id, str):
        return ws._json_error("invalid_island")
    deleted = await festival_map.delete_island(map_key, zone, island_id, ws._festival_editable_zone_names(beers, ws._festival_layout_template(map_key)))
    return web.json_response({"ok": deleted})
