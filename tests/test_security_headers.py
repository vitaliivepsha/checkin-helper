from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webapp_server as ws


async def test_security_headers_are_set_and_the_public_map_stays_embeddable():
    app = web.Application(middlewares=[ws._no_cache_middleware])

    async def ok(request):
        return web.Response(text="x")

    for path in ("/checkin", "/map", "/api/checkin/usage", "/api/public/i18n"):
        app.router.add_get(path, ok)
    async with TestClient(TestServer(app)) as c:
        for path in ("/checkin", "/api/checkin/usage"):
            r = await c.get(path)
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
            assert "frame-ancestors" in r.headers["Content-Security-Policy"] and "web.telegram.org" in r.headers["Content-Security-Policy"]
        for path in ("/map", "/api/public/i18n"):
            r = await c.get(path)
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert "Content-Security-Policy" not in r.headers
