"""`ws`: a late-bound view of the webapp_server module for the modules split out of it (audit A1).

Moved code reads shared configuration, state and helpers as `ws.NAME`. The attribute is looked up on webapp_server at
call time - never copied at import time - so tests that monkeypatch webapp_server (`monkeypatch.setattr(ws, "X", ...)`)
still reach the moved code, and the circular import (webapp_server re-exports what it moved) cannot bite."""


class _WebappServer:
    def __getattr__(self, name):
        import webapp_server

        return getattr(webapp_server, name)


ws = _WebappServer()
