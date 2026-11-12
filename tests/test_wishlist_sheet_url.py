import pytest

import wishlist_sheets
import webapp_server


def test_only_published_google_sheet_csv_links_are_accepted():
    good = "https://docs.google.com/spreadsheets/d/e/2PACX-abc/pub?gid=0&single=true&output=csv"
    assert wishlist_sheets.is_allowed_sheet_url(good)
    for bad in (
        "http://docs.google.com/spreadsheets/d/e/x/pub?output=csv",           # not https
        "https://attacker.example/x?output=csv",                               # not Google
        "https://docs.google.com.attacker.example/x?output=csv",               # look-alike host
        "https://docs.google.com@attacker.example/x?output=csv",               # userinfo trick
        "https://user:pw@docs.google.com/x?output=csv",
        "https://docs.google.com/spreadsheets/d/e/x/pub",                      # not csv
        "", "not a url",
    ):
        assert not wishlist_sheets.is_allowed_sheet_url(bad), bad


def test_redirects_may_only_go_to_google_hosts():
    assert wishlist_sheets.is_allowed_redirect_url("https://doc-0c-8s-sheets.googleusercontent.com/pub/abc/x")
    assert wishlist_sheets.is_allowed_redirect_url("https://docs.google.com/a")
    for bad in ("http://127.0.0.1:8080/x", "https://evil.example/", "https://googleusercontent.com.evil.example/", "http://doc.googleusercontent.com/x"):
        assert not wishlist_sheets.is_allowed_redirect_url(bad), bad


async def test_a_disallowed_link_is_not_fetched(monkeypatch):
    async def boom(url):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(webapp_server, "_get_sheet_text", boom)
    assert await webapp_server._fetch_wishlist_sheet_rows("https://attacker.example/x?output=csv") == []


async def test_redirect_away_from_google_and_oversized_bodies_are_refused():
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def redirect(request):
        raise web.HTTPFound("http://127.0.0.1:1/secret")

    async def big(request):
        return web.Response(body=b"x" * (wishlist_sheets.MAX_SHEET_BYTES + 10))

    async def small(request):
        return web.Response(text="Назва,Посилання\nA,https://untappd.com/b/x/1\n")

    app = web.Application()
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/big", big)
    app.router.add_get("/small", small)
    async with TestServer(app) as server:
        base = f"http://127.0.0.1:{server.port}"
        with pytest.raises(ValueError):
            await webapp_server._get_sheet_text(base + "/redirect")
        with pytest.raises(ValueError):
            await webapp_server._get_sheet_text(base + "/big")
        assert "Посилання" in await webapp_server._get_sheet_text(base + "/small")
