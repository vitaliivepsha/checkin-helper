import re
from pathlib import Path

import i18n

ROOT = Path(__file__).resolve().parent.parent
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_CYRILLIC = re.compile(r"[А-Яа-яІіЇїЄєҐґ]")

# Ratchet, not a goal: Mini App text that was hardcoded before the
# translation mechanism existed. It may only go DOWN - new user-facing text
# belongs in i18n.py (APP_STRINGS), read via T("app_...") in app.js or
# data-i18n in index.html, so every user sees their own language. Lower
# these numbers whenever strings get migrated.
LEGACY_HARDCODED_LINES = {"webapp/app.js": 160, "webapp/index.html": 106}


def _keys(prefixes):
    return {k for k in i18n.STRINGS["en"] if k.startswith(prefixes)}


def test_app_and_novelty_keys_exist_in_every_language_with_same_placeholders():
    for key in _keys(("app_", "nov_")):
        en, uk = i18n.STRINGS["en"][key], i18n.STRINGS["uk"].get(key)
        assert uk is not None, f"{key} missing in uk"
        assert set(_PLACEHOLDER.findall(en)) == set(_PLACEHOLDER.findall(uk)), key


def test_app_strings_falls_back_to_english_and_ru_follows_uk():
    assert i18n.app_strings("de") == i18n.app_strings("en")
    assert i18n.app_strings(None) == i18n.app_strings("en")
    assert i18n.app_strings("ru") == i18n.app_strings("uk")
    assert i18n.app_strings("uk")["app_back"] != i18n.app_strings("en")["app_back"]
    assert all(k.startswith("app_") for k in i18n.app_strings("uk"))


def test_novelty_templates_format():
    for lang in ("en", "uk"):
        text = i18n.t(lang, "nov_new", profile="P", beer="B", brewery="X", venue="V", had="")
        assert "P" in text and "B" in text and "X" in text and "V" in text


def test_no_new_hardcoded_cyrillic_in_mini_app():
    for rel, baseline in LEGACY_HARDCODED_LINES.items():
        count = sum(
            1 for line in (ROOT / rel).read_text(encoding="utf-8").splitlines()
            if _CYRILLIC.search(line) and not line.strip().startswith(("//", "/*", "*", "<!--"))
        )
        assert count <= baseline, (
            f"{rel} has {count} hardcoded Cyrillic lines (baseline {baseline}) - "
            "put user-facing text in i18n.py (APP_STRINGS) and use T()/data-i18n"
        )
