import untappd_mcp
import webapp_server as ws


def _patch(monkeypatch, direct_remaining, mcp_remaining, token="tok"):
    calls = []

    async def direct(tok, venue_id, limit=25, max_id=None, min_id=None):
        calls.append(("direct", venue_id, min_id))
        return {"checkins": {"items": []}}

    async def mcp(tok, venue_id, limit=25, max_id=None, min_id=None):
        calls.append(("mcp", venue_id, min_id))
        return {"checkins": {"items": []}}

    async def usage(tok):
        return {"lastSeen": {"remaining": mcp_remaining, "ageSeconds": 5}}

    async def get_token(owner_id):
        return token

    monkeypatch.setattr(ws, "DIRECT_TOKEN", "direct-token")
    monkeypatch.setattr(ws.untappd_direct, "get_api_usage", lambda: {"lastSeen": {"remaining": direct_remaining, "ageSeconds": 5}})
    monkeypatch.setattr(ws.untappd_direct, "get_venue_checkins", direct)
    monkeypatch.setattr(ws.untappd_mcp, "get_venue_checkins", mcp)
    monkeypatch.setattr(ws.untappd_mcp, "get_untappd_api_usage", usage)
    monkeypatch.setattr(ws.user_tokens, "get_token", get_token)
    return calls


async def test_the_direct_pool_is_used_first(monkeypatch):
    calls = _patch(monkeypatch, direct_remaining=50, mcp_remaining=100)
    assert await ws._venue_checkins_page(1, 77, 25, 5) is not None
    assert calls == [("direct", 77, 5)]


async def test_the_owners_mcp_token_is_used_only_when_direct_is_spent_and_it_has_room(monkeypatch):
    calls = _patch(monkeypatch, direct_remaining=0, mcp_remaining=100)
    assert await ws._venue_checkins_page(1, 77, 25, None) is not None
    assert calls == [("mcp", 77, None)]

    calls = _patch(monkeypatch, direct_remaining=0, mcp_remaining=20)       # not enough left: leave it for their own use
    assert await ws._venue_checkins_page(1, 77, 25, None) is None and calls == []

    calls = _patch(monkeypatch, direct_remaining=0, mcp_remaining=100, token=None)
    assert await ws._venue_checkins_page(1, 77, 25, None) is None and calls == []


async def test_the_mcp_wrapper_sends_the_documented_arguments(monkeypatch):
    sent = {}

    async def fake_call(name, arguments, token):
        sent.update(name=name, arguments=arguments)
        return {"count": 1, "items": [{"checkin_id": 5}], "pagination": {}}

    monkeypatch.setattr(untappd_mcp, "_call_tool", fake_call)
    page = await untappd_mcp.get_venue_checkins("tok", 123, limit=60, min_id=9)
    assert sent == {"name": "get_venue_checkins", "arguments": {"venueId": 123, "limit": 25, "minId": 9}}
    assert page["checkins"]["items"] == [{"checkin_id": 5}]          # flat envelope normalized like get_user_checkins
