"""The lens's "this is not that beer": the rejection store, its effect on the
matcher, and the /api/lens/reject + /unreject endpoints."""

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import beer_match
import lens_log
import lens_rejections
import webapp_server as ws


def beer(bid, name, brewery="PINTA"):
    return {"bid": bid, "beerName": name, "brewery": {"id": 1, "name": brewery}}


async def test_store_normalizes_the_text_persists_and_undoes(tmp_path):
    lens_rejections.init(str(tmp_path))
    assert lens_rejections.rejected_bids("PINTA", "Hazy Morning") == frozenset()
    assert await lens_rejections.add("PINTA", "Hazy Morning", 7)
    assert await lens_rejections.add(" pinta ", "HAZY  morning", 8)   # same product, written differently
    assert lens_rejections.rejected_bids("Pinta", "hazy morning") == frozenset({7, 8})
    assert lens_rejections.rejected_bids("PINTA", "Other Beer") == frozenset()

    lens_rejections.init(str(tmp_path))   # a restart: loaded back from the file
    assert lens_rejections.rejected_bids("PINTA", "Hazy Morning") == frozenset({7, 8})

    assert await lens_rejections.remove("PINTA", "Hazy Morning", 7)
    assert not await lens_rejections.remove("PINTA", "Hazy Morning", 7)
    assert await lens_rejections.remove("PINTA", "Hazy Morning", 8)
    assert await lens_rejections.all_entries() == []


async def test_store_refuses_blank_names_and_stops_at_the_cap(tmp_path, monkeypatch):
    lens_rejections.init(str(tmp_path))
    monkeypatch.setattr(lens_rejections, "MAX_ENTRIES", 1)
    assert not await lens_rejections.add("PINTA", "  ", 1)
    assert await lens_rejections.add("PINTA", "One", 1)
    assert not await lens_rejections.add("PINTA", "Two", 2)
    assert await lens_rejections.add("PINTA", "One", 3)   # more bids for a product already stored still fit


async def test_matcher_never_returns_or_offers_a_rejected_beer(monkeypatch):
    async def fake_search(token, query, limit=10):
        return [beer(1, "Hazy Morning"), beer(2, "Hazy Morning Deluxe")]

    monkeypatch.setattr(beer_match.untappd_mcp, "search_beers", fake_search)
    beer_match._identity_cache.clear()
    first = await beer_match._resolve_identity("tok", "Hazy Morning", "PINTA")
    assert first["matched"] and first["bid"] == 1

    second = await beer_match._resolve_identity("tok", "Hazy Morning", "PINTA", frozenset({1}))
    assert not second["matched"] or second["bid"] != 1
    assert all(c["bid"] != 1 for c in second.get("candidates", []))

    # the cache keeps the two apart: the unrejected call still gets the original answer
    again = await beer_match._resolve_identity("tok", "Hazy Morning", "PINTA")
    assert again["matched"] and again["bid"] == 1


async def test_reject_and_unreject_endpoints(tmp_path, monkeypatch):
    lens_log.init(str(tmp_path))
    lens_rejections.init(str(tmp_path))

    async def fake_resolve(token, owner_id, name, brewery, rejected_bids=frozenset(), **kw):
        if 1 in rejected_bids:
            return {"matched": False, "candidates": [], "query_name": name, "query_brewery": brewery}
        return {"matched": True, "name": name, "brewery": brewery, "bid": 1, "candidates": [], "query_name": name, "query_brewery": brewery}

    async def fake_token(owner_id):
        return "tok"

    async def no_beers(*a, **kw):
        return []

    monkeypatch.setattr(ws, "LENS_API_TOKEN", "secret")
    monkeypatch.setattr(ws, "AUTO_TOAST_OWNER_ID", "1")
    monkeypatch.setattr(ws.user_tokens, "get_token", fake_token)
    monkeypatch.setattr(ws, "_get_wishlist_beers", no_beers)
    monkeypatch.setattr(ws, "_get_my_list_items", no_beers)
    monkeypatch.setattr(ws.beer_match, "resolve_beer", fake_resolve)

    app = web.Application()
    app.router.add_post("/api/lens/lookup", ws.handle_lens_lookup)
    app.router.add_post("/api/lens/reject", ws.handle_lens_reject)
    app.router.add_post("/api/lens/unreject", ws.handle_lens_unreject)
    app.router.add_get("/api/lens/report", ws.handle_lens_report)
    async with TestClient(TestServer(app)) as c:
        headers = {"X-Lens-Token": "secret"}
        item = {"brewery": "PINTA", "name": "Hazy Morning"}

        assert (await c.post("/api/lens/reject", json={**item, "bid": 1})).status == 401
        assert (await c.post("/api/lens/reject", json={**item, "bid": "1"}, headers=headers)).status == 400
        assert (await c.post("/api/lens/reject", json={**item, "name": " ", "bid": 1}, headers=headers)).status == 400
        assert lens_rejections.rejected_bids("PINTA", "Hazy Morning") == frozenset()

        r = await c.post("/api/lens/reject", json={**item, "bid": 1}, headers=headers)
        assert r.status == 200 and (await r.json())["result"]["matched"] is False
        assert lens_rejections.rejected_bids("PINTA", "Hazy Morning") == frozenset({1})

        # the next page view honours it too
        looked_up = await (await c.post("/api/lens/lookup", json={"items": [item]}, headers=headers)).json()
        assert looked_up["results"][0]["matched"] is False

        report = await (await c.get("/api/lens/report", headers=headers)).json()
        assert [e["bids"] for e in report["rejections"]] == [[1]]

        r = await c.post("/api/lens/unreject", json={**item, "bid": 1}, headers=headers)
        assert (await r.json())["result"]["matched"] is True
        assert lens_rejections.rejected_bids("PINTA", "Hazy Morning") == frozenset()
