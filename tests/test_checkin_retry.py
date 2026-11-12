"""A check-in that timed out may exist already; retrying must look first and never run twice at once (audit R2)."""

import asyncio
import time
from email.utils import format_datetime
from datetime import datetime, timezone

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import pending_checkins
import untappd_mcp
import webapp_server as ws


@pytest.fixture
async def client(tmp_path, monkeypatch):
    pending_checkins.init(str(tmp_path))
    state = {"check_ins": 0, "feed": [], "check_in_error": None, "gate": None}

    async def valid(request):
        return {"user": {"id": 7}}

    async def token(tg_user):
        return "tok"

    async def check_in(tok, **kwargs):
        state["check_ins"] += 1
        if state["gate"]:
            await state["gate"].wait()
        if state["check_in_error"]:
            raise state["check_in_error"]
        return {"checkin_id": 99}

    async def feed(tok, username, limit=25, **kw):
        return {"checkins": {"items": state["feed"]}}

    async def profile(user_id):
        return {"username": "drinker", "token": "tok"}

    async def effects(*a, **k):
        state["effects"] = state.get("effects", 0) + 1

    monkeypatch.setattr(ws, "CHECKIN_DRY_RUN", False)
    monkeypatch.setattr(ws, "_require_valid_init_data", valid)
    monkeypatch.setattr(ws, "_resolve_token", token)
    monkeypatch.setattr(ws, "_apply_checkin_side_effects", effects)
    monkeypatch.setattr(ws.user_tokens, "get_profile", profile)
    monkeypatch.setattr(ws.user_tokens, "set_last_venue", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(untappd_mcp, "check_in", check_in)
    monkeypatch.setattr(untappd_mcp, "get_user_checkins", feed)
    app = web.Application()
    app.router.add_post("/submit", ws.handle_submit)
    app.router.add_post("/retry", ws.handle_pending_retry)
    async with TestClient(TestServer(app)) as c:
        c.state = state
        yield c


def _feed_item(bid, seconds_ago):
    when = datetime.fromtimestamp(time.time() - seconds_ago, tz=timezone.utc)
    return {"checkin_id": 5, "beer": {"bid": bid}, "created_at": format_datetime(when)}


async def test_an_answer_that_never_came_is_saved_as_maybe_created(client):
    client.state["check_in_error"] = untappd_mcp.UntappdOutcomeUnknown("read timeout")
    r = await client.post("/submit", json={"beerId": 5, "rating": 4})
    item = (await r.json())["item"]
    assert item["failReason"] == "checkin_unknown" and item["maybeCreated"] is True

    client.state["check_in_error"] = untappd_mcp.UntappdMCPError("bad request")
    item = (await (await client.post("/submit", json={"beerId": 6, "rating": 4})).json())["item"]
    assert item["failReason"] == "checkin_failed" and item["maybeCreated"] is False


async def test_retry_finds_the_check_in_that_already_exists_instead_of_repeating_it(client):
    client.state["check_in_error"] = untappd_mcp.UntappdOutcomeUnknown("read timeout")
    item = (await (await client.post("/submit", json={"beerId": 5, "rating": 4})).json())["item"]
    assert client.state["check_ins"] == 1

    client.state["feed"] = [_feed_item(5, 20)]                     # the "lost" check-in is on Untappd
    r = await client.post("/retry", json={"id": item["id"]})
    assert r.status == 200 and (await r.json())["ok"]
    assert client.state["check_ins"] == 1                          # no second check-in
    assert await pending_checkins.list_items(7) == []              # and the record is gone


async def test_retry_checks_in_when_the_feed_has_nothing_new(client):
    client.state["check_in_error"] = untappd_mcp.UntappdOutcomeUnknown("read timeout")
    item = (await (await client.post("/submit", json={"beerId": 5, "rating": 4})).json())["item"]
    client.state["check_in_error"] = None
    client.state["feed"] = [_feed_item(5, 86400), _feed_item(6, 10)]   # an old check-in of it, and a new one of another beer
    r = await client.post("/retry", json={"id": item["id"]})
    assert r.status == 200
    assert client.state["check_ins"] == 2 and await pending_checkins.list_items(7) == []


async def test_a_failed_lookup_stops_the_retry_and_keeps_the_record(client, monkeypatch):
    client.state["check_in_error"] = untappd_mcp.UntappdOutcomeUnknown("read timeout")
    item = (await (await client.post("/submit", json={"beerId": 5, "rating": 4})).json())["item"]

    async def broken_feed(*a, **k):
        raise untappd_mcp.UntappdMCPError("feed down")

    monkeypatch.setattr(untappd_mcp, "get_user_checkins", broken_feed)
    r = await client.post("/retry", json={"id": item["id"]})
    assert r.status == 502 and client.state["check_ins"] == 1       # nothing blindly repeated
    kept = await pending_checkins.list_items(7)
    assert len(kept) == 1 and "inProgressAt" not in kept[0]         # released, so the user can try again


async def test_two_taps_at_once_make_one_check_in(client):
    client.state["check_in_error"] = untappd_mcp.UntappdMCPError("rate")   # parked as an ordinary failure
    item = (await (await client.post("/submit", json={"beerId": 5, "rating": 4})).json())["item"]
    client.state["check_in_error"] = None
    client.state["gate"] = asyncio.Event()

    first = asyncio.create_task(client.post("/retry", json={"id": item["id"]}))
    await asyncio.sleep(0.1)                                        # the first retry is inside check_in
    second = await client.post("/retry", json={"id": item["id"]})
    assert second.status == 409 and (await second.json())["error"] == "in_progress"
    client.state["gate"].set()
    assert (await first).status == 200
    assert client.state["check_ins"] == 2                           # the submit's failed try + exactly one retry


@pytest.mark.parametrize("error, expected", [
    (httpx.ReadTimeout("slow"), untappd_mcp.UntappdOutcomeUnknown),
    (httpx.RemoteProtocolError("dropped"), untappd_mcp.UntappdOutcomeUnknown),
    (httpx.ConnectError("no route"), untappd_mcp.UntappdMCPError),
    (httpx.ConnectTimeout("no route"), untappd_mcp.UntappdMCPError),
])
async def test_only_errors_after_the_request_left_are_unknown_outcomes(monkeypatch, error, expected):
    class Client:
        async def post(self, *a, **k):
            raise error

    monkeypatch.setenv("UNTAPPD_MCP_URL", "https://mcp.invalid/")
    monkeypatch.setattr(untappd_mcp, "_get_client", lambda: Client())
    with pytest.raises(untappd_mcp.UntappdMCPError) as raised:
        await untappd_mcp.check_in("tok", beer_id=1, rating=3, shout="")
    assert isinstance(raised.value, untappd_mcp.UntappdOutcomeUnknown) is (expected is untappd_mcp.UntappdOutcomeUnknown)
