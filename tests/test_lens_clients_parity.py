"""The userscript and the Chrome extension are two copies of one client (the extension swaps the transport only).
They drifted before: the "not that beer" button, the "probable" chip and the not-a-beer filter existed in one and not
the other. This fails when a function, constant, CSS class or shop adapter appears on one side only."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USERSCRIPT = (ROOT / "userscripts" / "untappd-lens.user.js").read_text(encoding="utf-8").replace("\r\n", "\n")
EXTENSION = (ROOT / "extension" / "content.js").read_text(encoding="utf-8").replace("\r\n", "\n")

# Differences that are the transport itself (GM_xmlhttpRequest/GM_setValue vs messages to background.js).
TRANSPORT_ONLY = {"apiPost", "apiPostOnce", "getApiToken", "apiFeedback", "extensionContextValid", "API_BASE", "API_TOKEN",
                  "lens-wrap"}


def names(source: str) -> set[str]:
    found = set(re.findall(r"function\s+(\w+)", source))
    found |= set(re.findall(r"^\s*const\s+([A-Z][A-Z_]{3,})\s*=", source, re.M))
    found |= set(re.findall(r"lens-[a-z-]+", source))
    return found


def block(source: str, start: str, end: str) -> str:
    i = source.index(start)
    return source[i:source.index(end, i)]


def test_both_clients_have_the_same_functions_constants_and_classes():
    only_userscript = names(USERSCRIPT) - names(EXTENSION) - TRANSPORT_ONLY
    only_extension = names(EXTENSION) - names(USERSCRIPT) - TRANSPORT_ONLY
    assert not only_userscript, f"in the userscript only (port it to extension/content.js): {sorted(only_userscript)}"
    assert not only_extension, f"in the extension only (port it to the userscript): {sorted(only_extension)}"


def test_the_shop_adapters_are_identical():
    start, end = "const ADAPTERS = [", "\n  ];\n"
    assert block(USERSCRIPT, start, end) == block(EXTENSION, start, end)


def test_the_not_a_beer_filter_is_identical():
    start, end = "const NOT_A_BEER_RE", '"i"\n  );'
    assert block(USERSCRIPT, start, end) == block(EXTENSION, start, end)


def test_the_extension_may_only_call_the_feedback_paths_it_whitelists():
    background = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
    sent = set(re.findall(r'"(/api/lens/(?:un)?(?:reject|confirm))"', EXTENSION))
    allowed = set(re.findall(r'"(/api/lens/(?:un)?(?:reject|confirm))"', background))
    assert sent and sent <= allowed
