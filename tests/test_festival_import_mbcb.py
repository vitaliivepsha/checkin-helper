"""Tests for festival_import_mbcb.build_festival_beers - the mbcb.jonpacker.com
latest.json -> {session: [beer, ...]} festival file conversion."""

import json
import os

import festival_import_mbcb


def _row(session, bid, name="Beer", brewery="Brew"):
    return {"session": session, "ut_bid": bid, "name": name, "brewery": brewery}


def test_groups_by_session_and_builds_untappd_url():
    sessions, skipped = festival_import_mbcb.build_festival_beers({"beers": [
        _row("yellow", 6496029, "Dark Heart", "Engkanto"),
        _row("red", 1, "Other"),
    ]})
    assert list(sessions) == ["yellow", "red"]
    assert sessions["yellow"] == [{
        "id": "6496029", "name": "Dark Heart", "brewery": "Engkanto",
        "url": "https://untappd.com/beer/6496029",
    }]
    assert skipped == []


def test_same_beer_listed_in_each_session_but_deduped_within_one():
    sessions, _ = festival_import_mbcb.build_festival_beers({"beers": [
        _row("yellow", 5), _row("yellow", 5), _row("red", 5),
    ]})
    assert [len(v) for v in sessions.values()] == [1, 1]


def test_rows_without_untappd_id_or_name_are_returned_as_skipped():
    no_id = _row("green", None, "Ice Tea")
    no_name = _row("green", 7, "  ")
    sessions, skipped = festival_import_mbcb.build_festival_beers({"beers": [no_id, no_name, _row("green", 8)]})
    assert skipped == [no_id, no_name]
    assert [b["id"] for b in sessions["green"]] == ["8"]


def test_empty_payload():
    assert festival_import_mbcb.build_festival_beers({}) == ({}, [])


def test_registered_festival_file_loads():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    festivals = json.load(open(os.path.join(root, "festivals.json"), encoding="utf-8"))["festivals"]
    entry = next(f for f in festivals if f["key"] == "mbcb2026")
    data = json.load(open(os.path.join(root, entry["file"]), encoding="utf-8"))
    assert set(data) == {"yellow", "red", "green"}
    assert all(b["id"] and b["name"] and b["brewery"] for beers in data.values() for b in beers)
