"""Runs the lens clients (userscript and extension) in jsdom against a stubbed server: lookup, the "not that beer" and
"yes, this one" buttons, undo. Needs node and `npm install` (jsdom, dev-only, see package.json); skipped without them."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HAS_JSDOM = (ROOT / "node_modules" / "jsdom" / "package.json").exists()


@pytest.mark.skipif(shutil.which("node") is None or not HAS_JSDOM, reason="node or jsdom (npm install) is missing")
def test_both_lens_clients_behave_in_a_browser_like_dom():
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "lens_clients.test.js")],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok userscript" in result.stdout and "ok extension" in result.stdout


def test_the_userscript_reports_its_own_version_to_the_bot():
    # the lens report lists client versions; a stale label hid the 2.9.5 update behind "userscript 2.9.4"
    source = (ROOT / "userscripts" / "untappd-lens.user.js").read_text(encoding="utf-8")
    header = re.search(r"@version\s+([0-9.]+)", source).group(1)
    label = re.search(r'client:\s*"userscript ([0-9.]+)"', source).group(1)
    assert label == header
