"""Request-body hardening (audit S2/S6): what a client can store, and the shape of what it can send."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import checkin_queue
import input_checks
import pending_checkins
import webapp_server as ws
import wishlist_items


def test_a_label_address_must_be_a_plain_https_url():
    ok = "https://assets.untappd.com/site/beer_logos/beer-1_abc.jpeg"
    assert input_checks.https_url(ok) == ok
    for bad in ('x" onerror="alert(1)', "javascript:alert(1)", "http://assets.untappd.com/a.png", "https://a.b/x y",
                "https://a.b/<script>", "https://a.b/x'y", "https://a.b/" + "a" * 400, None, 5, ["https://a.b/"]):
        assert input_checks.https_url(bad) is None, bad


def test_text_fields_are_cut_and_typed():
    assert input_checks.text("  Hazy  ") == "Hazy"
    assert len(input_checks.text("x" * 5000)) == input_checks.MAX_TEXT
    assert input_checks.text({"a": 1}) is None and input_checks.text(True) is None and input_checks.text(None) is None
    assert input_checks.text(6.5) == 6.5


async def test_the_shared_queue_keeps_only_clean_fields_and_has_a_cap(tmp_path, monkeypatch):
    checkin_queue.init(str(tmp_path))
    evil = {"beerId": 1, "name": "N" * 9999, "brewery": {"x": 1}, "labelUrl": 'x" onerror="steal()', "sessions": ["yellow", 5, "z" * 99],
            "extra": "dropped"}
    item, status = await checkin_queue.add_item(evil, {"userId": 7, "name": "Mallory" * 30}, 100)
    assert status == "added" and len(item["name"]) == input_checks.MAX_TEXT and item["brewery"] is None
    assert item["labelUrl"] is None and item["sessions"] == ["yellow", "z" * 30] and len(item["addedBy"]["name"]) == 64
    assert "extra" not in item

    monkeypatch.setattr(input_checks, "MAX_QUEUE_PER_GROUP", 2)
    await checkin_queue.add_item({"beerId": 2, "name": "b"}, {"userId": 7, "name": "m"}, 100)
    with pytest.raises(input_checks.StoreFull):
        await checkin_queue.add_item({"beerId": 3, "name": "c"}, {"userId": 7, "name": "m"}, 100)
    other_group, _ = await checkin_queue.add_item({"beerId": 3, "name": "c"}, {"userId": 7, "name": "m"}, 200)
    assert other_group["groupId"] == 200                                      # the cap is per group


async def test_wishlist_and_pending_lists_are_capped_and_clean(tmp_path, monkeypatch):
    wishlist_items.init(str(tmp_path))
    pending_checkins.init(str(tmp_path))
    monkeypatch.setattr(input_checks, "MAX_WISHLIST", 1)
    monkeypatch.setattr(input_checks, "MAX_PENDING", 1)
    item, added = await wishlist_items.add_item(1, {"beerId": 1, "name": "A", "labelUrl": "javascript:alert(1)"})
    assert added and item["labelUrl"] is None
    with pytest.raises(input_checks.StoreFull):
        await wishlist_items.add_item(1, {"beerId": 2, "name": "B"})
    pending = await pending_checkins.add_item(1, {"beerId": 1, "beerName": "A", "geolat": "x", "shout": "s" * 900, "failReason": "rate_limited"})
    assert pending["geolat"] is None and len(pending["shout"]) == 500
    with pytest.raises(input_checks.StoreFull):
        await pending_checkins.add_item(1, {"beerId": 2})


async def test_a_body_that_is_not_an_object_is_a_400_not_a_500():
    app = web.Application(middlewares=[ws._json_object_middleware])

    async def echo(request):
        body = await request.json()
        return web.json_response({"keys": sorted(body)})

    app.router.add_post("/x", echo)
    async with TestClient(TestServer(app)) as c:
        for raw in ("[1,2]", '"text"', "5"):
            r = await c.post("/x", data=raw, headers={"Content-Type": "application/json"})
            assert r.status == 400 and (await r.json())["error"] == "invalid_json", raw
        r = await c.post("/x", json={"a": 1})
        assert r.status == 200 and (await r.json())["keys"] == ["a"]


async def test_known_users_are_the_owner_token_holders_and_group_members(monkeypatch):
    async def token(user_id):
        return "tok" if user_id == 2 else None

    async def group(user_id):
        return {"chatId": 1} if user_id == 3 else None

    monkeypatch.setattr(ws, "AUTO_TOAST_OWNER_ID", "1")
    monkeypatch.setattr(ws, "OWNER_TELEGRAM_ID", "")
    monkeypatch.setattr(ws.user_tokens, "get_token", token)
    monkeypatch.setattr(ws.group_membership, "get_active_group", group)
    assert [await ws._is_known_user(u) for u in (1, 2, 3, 4, None)] == [True, True, True, False, False]


async def test_a_stranger_cannot_edit_the_map_or_spend_the_direct_quota(monkeypatch):
    async def stranger(request):
        return {"user": {"id": 99}}

    async def nobody(user_id):
        return None

    monkeypatch.setattr(ws, "_require_valid_init_data", stranger)
    monkeypatch.setattr(ws, "AUTO_TOAST_OWNER_ID", "1")
    monkeypatch.setattr(ws, "OWNER_TELEGRAM_ID", "")
    monkeypatch.setattr(ws.user_tokens, "get_token", nobody)
    monkeypatch.setattr(ws.group_membership, "get_active_group", nobody)

    app = web.Application(middlewares=[ws._json_object_middleware])
    for path, handler in (
        ("/move", ws.handle_festival_map_move), ("/gap", ws.handle_festival_map_gap_insert), ("/order", ws.handle_festival_map_zone_order),
        ("/island", ws.handle_festival_map_island_create), ("/delete", ws.handle_festival_map_island_delete),
        ("/loc", ws.handle_festival_watch_set_location), ("/extra", ws.handle_festival_watch_add_extra_venue),
        ("/nearby", ws.handle_venues_nearby),
    ):
        app.router.add_post(path, handler)
    async with TestClient(TestServer(app)) as c:
        for path in ("/move", "/gap", "/order", "/island", "/delete", "/loc", "/extra", "/nearby"):
            r = await c.post(path, json={"zone": "Area 1", "lat": 1, "lng": 2, "foursquareId": "4b5bc1e0f964a520e6e925e3"})
            assert r.status == 403 and (await r.json())["error"] == "not_allowed", path


async def test_a_huge_index_cannot_grow_a_side_list_or_the_map_file(tmp_path):
    import festival_map

    festival_map.init(str(tmp_path))
    zones = ["Area 1"]
    assert await festival_map.move_brewery("fest", "A", "Area 1", "left", 300_000, zones)
    layout = await festival_map.get_layout("fest", ["A"], {"A": "Area 1"}, zones)
    assert len(layout["Area 1"]["left"]) <= festival_map.MAX_PADDING + 1
    assert not await festival_map.insert_gap("fest", "Area 1", "left", 10**8, zones)        # out of range: refused
    assert (tmp_path / "festival_map.json").stat().st_size < 10_000
