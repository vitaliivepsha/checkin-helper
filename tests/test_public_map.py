import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import public_map_util
import webapp_server as ws

BEERS = [
    {"id": "111", "name": "Hazy Dream", "brewery": "Alpha Brewing", "style": "IPA - New England / Hazy", "location": "Area 1"},
    {"id": "222", "name": "Dark Matter", "brewery": "Beta Brewing", "style": "Stout - Imperial", "location": "Area 2"},
    {"id": "333", "name": "Collab Special", "brewery": "Credited Only Co", "style": "Sour", "location": "Area 2",
     "standBrewery": "Beta Brewing"},
    {"id": "not-a-bid", "name": "Hazy Ghost", "brewery": "Alpha Brewing", "style": "IPA", "location": "Area 1"},
]


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(ws, "_festival_data_for", lambda key: (BEERS, {}))
    monkeypatch.setattr(ws, "_load_festivals_registry", lambda: [{"key": "wfp2026"}])
    monkeypatch.setattr(ws, "_public_limiter", public_map_util.RateLimiter())
    monkeypatch.setattr(ws, "_public_cache", public_map_util.TTLCache(ttl_seconds=30))
    monkeypatch.setattr(ws, "_public_search_cache", public_map_util.TTLCache(ttl_seconds=60))
    app = web.Application()
    app.router.add_get("/map", ws.handle_public_index)
    app.router.add_post("/api/public/i18n", ws.handle_public_i18n)
    app.router.add_post("/api/public/festival/brewery", ws.handle_public_brewery)
    app.router.add_post("/api/public/search", ws.handle_public_search)
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_map_page_is_public_and_flagged(client):
    r = await client.get("/map")
    assert r.status == 200
    page = await r.text()
    assert "window.PUBLIC_MAP = true" in page
    assert '<body class="public-map"' in page
    assert 'name="robots" content="noindex"' in page


async def test_search_needs_no_auth_and_returns_stand_zone_and_bid(client):
    r = await client.post("/api/public/search", json={"query": "hazy dream"})
    assert r.status == 200
    results = (await r.json())["results"]
    assert results and results[0]["beerId"] == 111
    assert results[0]["stand"] == "Alpha Brewing" and results[0]["zone"] == "Area 1"
    assert all(isinstance(x["beerId"], int) for x in results)  # non-bid entries skipped


async def test_search_collab_resolves_to_the_stand_not_the_credited_brewery(client):
    results = (await (await client.post("/api/public/search", json={"query": "collab special"})).json())["results"]
    assert results[0]["brewery"] == "Credited Only Co"
    assert results[0]["stand"] == "Beta Brewing" and results[0]["zone"] == "Area 2"


async def test_search_rejects_short_and_overlong_queries(client):
    assert (await (await client.post("/api/public/search", json={"query": "a"})).json())["results"] == []
    assert (await (await client.post("/api/public/search", json={"query": "x" * 200})).json())["results"] == []
    assert (await (await client.post("/api/public/search", data=b"not json")).json())["results"] == []


async def test_brewery_beers_by_stand_without_personal_fields(client):
    r = await client.post("/api/public/festival/brewery", json={"brewery": "Beta Brewing"})
    beers = (await r.json())["beers"]
    assert {b["beerId"] for b in beers} == {222, 333}
    assert all("hadIt" not in b and "queueStatus" not in b for b in beers)
    assert (await client.post("/api/public/festival/brewery", json={})).status == 400


async def test_unknown_festival_key_falls_back_to_default(client):
    assert ws._public_festival_key({"fest": "nope"}) is None
    assert ws._public_festival_key({"fest": "wfp2026"}) == "wfp2026"
    assert ws._public_festival_key({"fest": 5}) is None


async def test_i18n_follows_requested_language(client):
    en = await (await client.post("/api/public/i18n", json={"lang": "en-GB"})).json()
    uk = await (await client.post("/api/public/i18n", json={"lang": "uk"})).json()
    assert en["lang"] == "en" and uk["lang"] == "uk"
    assert en["strings"]["app_public_search_ph"] != uk["strings"]["app_public_search_ph"]


async def test_per_ip_rate_limit(client, monkeypatch):
    monkeypatch.setattr(ws, "PUBLIC_MAP_SEARCH_RATE_LIMIT_PER_MIN", 2)
    codes = [(await client.post("/api/public/search", json={"query": "hazy"})).status for _ in range(4)]
    assert codes == [200, 200, 429, 429]


def test_ttl_cache_expires_and_evicts(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(public_map_util.time, "monotonic", lambda: now[0])
    cache = public_map_util.TTLCache(ttl_seconds=10, max_entries=2)
    cache.set("a", 1)
    assert cache.get("a") == 1
    now[0] += 11
    assert cache.get("a") is None
    cache.set("x", 1); now[0] += 1; cache.set("y", 2); now[0] += 1; cache.set("z", 3)
    assert cache.get("x") is None and cache.get("y") == 2 and cache.get("z") == 3


def test_rate_limiter_window_and_keys(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(public_map_util.time, "monotonic", lambda: now[0])
    rl = public_map_util.RateLimiter(window_seconds=60)
    assert rl.allow("ip1", 2) and rl.allow("ip1", 2) and not rl.allow("ip1", 2)
    assert rl.allow("ip2", 2)
    now[0] += 61
    assert rl.allow("ip1", 2)


async def test_private_brewery_handler_still_works_after_sharing_stand_beers(monkeypatch):
    async def fake_auth(request):
        return {"user": {"id": 1}}

    async def no_festival(user_id):
        return None

    async def no_group(user_id):
        return None

    async def had_it(user_id, bid):
        return {"hadIt": bid == 222, "userRating": 4.0 if bid == 222 else None}

    async def annotate(beers, user_id, chat_id):
        for b in beers:
            b["queueStatus"] = None

    monkeypatch.setattr(ws, "_festival_data_for", lambda key: (BEERS, {}))
    monkeypatch.setattr(ws, "_require_valid_init_data", fake_auth)
    monkeypatch.setattr(ws, "_resolve_festival_key", no_festival)
    monkeypatch.setattr(ws, "_active_group", no_group)
    monkeypatch.setattr(ws.had_it_index, "lookup_had_it", had_it)
    monkeypatch.setattr(ws, "_annotate_queue_status", annotate)
    app = web.Application()
    app.router.add_post("/api/checkin/festival/brewery", ws.handle_festival_brewery)
    async with TestClient(TestServer(app)) as c:
        beers = (await (await c.post("/api/checkin/festival/brewery", json={"brewery": "Beta Brewing"})).json())["beers"]
    by_id = {b["beerId"]: b for b in beers}
    assert set(by_id) == {222, 333}
    assert by_id[222]["hadIt"] is True and by_id[222]["userRating"] == 4.0


async def test_repeated_search_is_computed_once_and_ignores_case_and_spacing(client, monkeypatch):
    calls = []
    real = ws._search_festival_beers

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(ws, "_search_festival_beers", counting)
    first = await (await client.post("/api/public/search", json={"query": "Hazy  Dream"})).json()
    again = await (await client.post("/api/public/search", json={"query": "hazy dream"})).json()
    assert first == again and first["results"]
    assert calls == ["hazy dream"]
    await client.post("/api/public/search", json={"query": "dark matter"})
    assert calls == ["hazy dream", "dark matter"]


async def test_search_runs_off_the_event_loop_thread(client, monkeypatch):
    import threading
    seen = []
    real = ws._search_festival_beers

    def recording(*args, **kwargs):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(*args, **kwargs)

    monkeypatch.setattr(ws, "_search_festival_beers", recording)
    await client.post("/api/public/search", json={"query": "stout"})
    assert seen == [False]
