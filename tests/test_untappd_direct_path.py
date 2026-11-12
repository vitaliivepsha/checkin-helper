"""A request body must never be able to steer an Untappd API call to another path (audit S1)."""

import pytest

import untappd_direct
import untappd_mcp
import webapp_server as ws


@pytest.mark.parametrize("path", [
    "venue/foursquare_lookup/../../friend/remove/123",
    "venue/foursquare_lookup/..",
    "beer/info/%2e%2e",
    "user/checkins/a?b=c",
    "user/badges/x#y",
    "user/beers/a\b",
    "venue/checkins/",
    "/venue/checkins/1",
])
def test_unsafe_paths_are_refused(path):
    with pytest.raises(untappd_mcp.UntappdMCPError):
        untappd_direct._check_path(path)


@pytest.mark.parametrize("path", [
    "user/info", "beer/info/12345", "user/badges/some_user-1", "user/checkins/Jan.Kowalski",
    "venue/foursquare_lookup/4b5bc1e0f964a520e6e925e3", "checkin/addcomment/987654321",
])
def test_the_paths_the_client_builds_are_accepted(path):
    untappd_direct._check_path(path)


async def test_call_api_refuses_before_any_request(monkeypatch):
    def boom():
        raise AssertionError("no HTTP client may be created for an unsafe path")

    monkeypatch.setattr(untappd_direct, "_get_client", boom)
    with pytest.raises(untappd_mcp.UntappdMCPError):
        await untappd_direct.lookup_venue_by_foursquare("tok", "../../friend/remove/123")


def test_foursquare_ids_from_request_bodies_are_validated():
    assert ws._valid_foursquare_id("4b5bc1e0f964a520e6e925e3")
    for bad in ("../../friend/remove/1", "abc", "x" * 41, "a/b/c/d/e/f/g", None, 123, ["4b5bc1e0f964a520e6e925e3"], ""):
        assert not ws._valid_foursquare_id(bad), bad


def test_rate_limit_text_is_matched_by_whole_words():
    # audit R4: "rate" inside "generated"/"separate" must not read as a rate limit
    from untappd_mcp import is_rate_limit_message
    for text in ("HTTP 429", "Rate limit exceeded", "rate_limit", "Too Many Requests", "request throttled"):
        assert is_rate_limit_message(text), text
    for text in ("Beer separate from brewery", "Could not generate report", "Inaccurate id 14290", "error 14290", ""):
        assert not is_rate_limit_message(text), text
