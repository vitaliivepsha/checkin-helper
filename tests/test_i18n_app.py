import re
from pathlib import Path

import i18n

ROOT = Path(__file__).resolve().parent.parent
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_CYRILLIC = re.compile(r"[А-Яа-яІіЇїЄєҐґ]")

# The Mini App (app.js / index.html) must contain NO user-facing Cyrillic
# outside comments: text lives in i18n.py (APP_STRINGS), read via
# T("app_...") / TP() in app.js or data-i18n* attributes in index.html, so
# every user sees their own language.
MINI_APP_FILES = ["webapp/app.js", "webapp/index.html"]


def _keys(prefixes):
    return {k for k in i18n.STRINGS["en"] if k.startswith(prefixes)}


def test_app_and_novelty_keys_exist_in_every_language_with_same_placeholders():
    for key in _keys(("app_", "nov_", "cmt_", "bjcp_note_")):
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


def _without_html_comments(text):
    # Blank them out (keeping newlines) so reported line numbers stay right.
    return re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def test_no_hardcoded_cyrillic_in_mini_app():
    for rel in MINI_APP_FILES:
        text = _without_html_comments((ROOT / rel).read_text(encoding="utf-8"))
        offenders = [
            f"{n}: {line.strip()[:80]}"
            for n, line in enumerate(text.splitlines(), 1)
            if _CYRILLIC.search(line) and not line.strip().startswith(("//", "/*", "*"))
        ]
        assert not offenders, (
            f"{rel} has hardcoded Cyrillic - put user-facing text in i18n.py (APP_STRINGS) "
            "and use T()/data-i18n:\n" + "\n".join(offenders[:10])
        )


def test_bjcp_notes_are_translatable_keys():
    import bjcp_styles
    for note in (bjcp_styles.BEST_EFFORT_NOTE, bjcp_styles.MIXED_STYLE_NOTE, bjcp_styles.FRUIT_BEER_NOTE):
        assert note in i18n.STRINGS["en"] and note in i18n.STRINGS["uk"], note


def test_every_key_used_by_the_mini_app_exists():
    used = set()
    for rel in MINI_APP_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        used |= set(re.findall(r'"(app_[a-z0-9_]+)"', text))
        used |= set(re.findall(r'data-i18n[a-z-]*="(app_[a-z0-9_]+)"', text))
        for base in re.findall(r'\bTP\("(app_[a-z0-9_]+)"', text):
            used.discard(base)  # a plural base isn't a key itself, only base_one/few/many/other
            used |= {f"{base}_{cat}" for cat in ("one", "few", "many", "other")}
    missing = sorted(k for k in used if k not in i18n.STRINGS["en"] or k not in i18n.STRINGS["uk"])
    assert not missing, missing
