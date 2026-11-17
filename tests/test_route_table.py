"""Refactor guard (audit A1): the set of routes - method, path and handler name - must not change while webapp_server.py
is split into modules. Regenerate tests/fixtures/route_table.txt deliberately when a route is added or removed."""

from pathlib import Path

import webapp_server as ws

FIXTURE = Path(__file__).parent / "fixtures" / "route_table.txt"


def _table() -> list[str]:
    rows = set()
    for route in ws._build_app().router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or str(info)
        rows.add(f"{route.method} {path} {getattr(route.handler, '__name__', route.handler)}")
    return sorted(rows)


def test_the_route_table_is_unchanged():
    expected = FIXTURE.read_text(encoding="utf-8").splitlines()
    assert _table() == expected
