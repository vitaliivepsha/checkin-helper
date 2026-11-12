import json
import os

import pytest

import json_store
import user_tokens


def test_a_damaged_file_is_moved_aside_not_treated_as_empty(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"1": {"token": "a"}, "2": {"tok', encoding="utf-8")   # truncated mid-write
    assert json_store.load_json(str(path), {}) == {}
    assert not path.exists()
    assert len(list(tmp_path.glob("s.json.corrupt-*"))) == 1
    assert json_store.quarantined


def test_a_wrong_top_level_type_counts_as_damage(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("[1, 2]", encoding="utf-8")
    assert json_store.load_json(str(path), {}) == {}
    assert list(tmp_path.glob("s.json.corrupt-*"))


def test_the_last_good_copy_is_restored(tmp_path):
    path = tmp_path / "s.json"
    tmp = tmp_path / "s.json.tmp"
    tmp.write_text('{"v": 1}', encoding="utf-8")
    json_store.replace_file(str(tmp), str(path))
    tmp.write_text('{"v": 2}', encoding="utf-8")
    json_store.replace_file(str(tmp), str(path))                 # now s.json = v2, s.json.bak = v1
    assert json.loads((tmp_path / "s.json.bak").read_text(encoding="utf-8")) == {"v": 1}
    path.write_text("{broken", encoding="utf-8")
    assert json_store.load_json(str(path), {}) == {"v": 1}
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 1}   # and put back in place


def test_missing_file_gives_the_default_and_unreadable_one_raises(tmp_path, monkeypatch):
    assert json_store.load_json(str(tmp_path / "none.json"), []) == []
    path = tmp_path / "s.json"
    path.write_text("{}", encoding="utf-8")

    def denied(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(json_store, "_read", denied)
    with pytest.raises(json_store.StoreUnreadable):
        json_store.load_json(str(path), {})
    assert path.exists()   # nothing was renamed or replaced


async def test_a_truncated_token_file_is_not_silently_overwritten(tmp_path):
    # the audit's scenario: a truncated untappd_tokens.json, then a save for another user
    user_tokens.init(str(tmp_path))
    for uid in (1, 2, 3):
        await user_tokens.set_token(uid, f"tok-{uid}", f"u{uid}", f"User {uid}")
    path = tmp_path / "untappd_tokens.json"
    path.write_text(path.read_text(encoding="utf-8")[:-15], encoding="utf-8")
    await user_tokens.set_token(4, "tok-4", "u4", "User 4")
    assert list(tmp_path.glob("untappd_tokens.json.corrupt-*")), "the damaged file is kept for inspection"
    # restored from the last good copy (one save back), not replaced by a near-empty file
    assert await user_tokens.get_token(1) == "tok-1" and await user_tokens.get_token(2) == "tok-2"
    assert await user_tokens.get_token(4) == "tok-4"
