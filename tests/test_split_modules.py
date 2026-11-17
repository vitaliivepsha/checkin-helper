"""Guards for the modules split out of webapp_server.py (audit A1)."""

import builtins
import symtable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPLIT_MODULES = ["lens_routes", "checkin_routes"]


def _module_level_names(table) -> set[str]:
    return {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}


def _undefined_globals(source: str) -> set[str]:
    top = symtable.symtable(source, "module", "exec")
    defined = _module_level_names(top) | set(dir(builtins))
    undefined: set[str] = set()

    def walk(table):
        if table.get_type() == "function":
            for symbol in table.get_symbols():
                if symbol.is_global() and symbol.is_referenced() and symbol.get_name() not in defined:
                    undefined.add(symbol.get_name())
        for child in table.get_children():
            walk(child)

    walk(top)
    return undefined


@pytest.mark.parametrize("module", SPLIT_MODULES)
def test_a_split_module_refers_only_to_names_it_defines_or_reaches_through_ws(module):
    # a function moved out of webapp_server that still names something left behind would crash only when called
    source = (ROOT / f"{module}.py").read_text(encoding="utf-8")
    assert _undefined_globals(source) == set()


@pytest.mark.parametrize("module", SPLIT_MODULES)
def test_a_split_module_does_not_import_webapp_server_at_load_time(module):
    source = (ROOT / f"{module}.py").read_text(encoding="utf-8")
    assert "import webapp_server" not in source and "from webapp_server" not in source


def test_moved_code_reads_shared_settings_from_webapp_server_at_call_time(monkeypatch):
    import lens_routes
    import webapp_server as ws

    class Req:
        headers = {"X-Lens-Token": "abc"}

    monkeypatch.setattr(ws, "LENS_API_TOKEN", "abc")
    assert lens_routes._lens_authorized(Req())
    monkeypatch.setattr(ws, "LENS_API_TOKEN", "other")
    assert not lens_routes._lens_authorized(Req())
    assert ws._lens_authorized is lens_routes._lens_authorized      # re-exported under the old name
