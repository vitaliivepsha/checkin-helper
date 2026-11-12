"""Every route of the web app answers a caller with no credentials by refusing it, unless it is on the short list of
public ones (audit T1). A new route that forgets its auth check fails here."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

import webapp_server as ws

PUBLIC_PREFIXES = ("/api/public/", "/static/")
# the maintenance flag is read by the splash screen before anyone is signed in (it holds one boolean and a message)
PUBLIC_PATHS = {"/checkin", "/map", "/api/checkin/maintenance/get"}


def _routes():
    app = ws._build_app()
    seen = set()
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter")
        if route.method in ("*", "HEAD") or not path or "{" in path:
            continue
        seen.add((route.method, path))
    return app, sorted(seen)


async def test_routes_without_credentials_are_refused(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setattr(ws, "LENS_API_TOKEN", "secret")
    monkeypatch.setattr(ws, "DEPLOY_WEBHOOK_SECRET", "secret")
    app, routes = _routes()
    assert len(routes) > 40
    open_routes = []
    async with TestClient(TestServer(app)) as c:
        for method, path in routes:
            if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
                continue
            r = await c.request(method, path, data=b"{}", headers={"Content-Type": "application/json"})
            if r.status not in (401, 403):
                open_routes.append((method, path, r.status))
    assert open_routes == [], f"routes that answered an anonymous caller: {open_routes}"
