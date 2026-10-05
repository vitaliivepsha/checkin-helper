from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import beer_match
import lens_log
import webapp_server as ws


def matched(name, brewery, bid):
    return {"matched": True, "name": name, "brewery": brewery, "bid": bid, "candidates": []}


def unmatched(candidates=()):
    return {"matched": False, "candidates": [{"name": c} for c in candidates]}


async def test_records_final_outcomes_and_counts(tmp_path):
    lens_log.init(str(tmp_path))
    items = [
        {"name": "AleSmith: BA Speedway Stout 2023 - 473 ml can", "brewery": "AleSmith"},
        {"name": "Hazy Morning", "brewery": "PINTA"},
        {"name": "Unknown Thing", "brewery": "Nobody"},
        {"name": "Ambiguous Ale", "brewery": "Someone"},
        {"name": "Broken", "brewery": "X"},
        {"name": "  ", "brewery": "Skipped"},
    ]
    results = [
        matched("Barrel-Aged Speedway Stout (2023)", "AleSmith Brewing Company", 5628687),
        matched("Hazy Morning", "PINTA", 1),
        unmatched(),
        unmatched(["Ambiguous Ale A", "Ambiguous Ale B"]),
        {"matched": False, "error": True, "candidates": []},
        unmatched(),
    ]
    await lens_log.record(items, results)
    await lens_log.record(items[:3], results[:3])  # same products again

    report = await lens_log.report()
    assert report["total"] == 5  # the blank-name item is ignored
    assert report["outcomes"] == {"matched": 2, "no_results": 1, "ambiguous": 1, "error": 1}
    by_name = {e["queryName"]: e for e in report["unmatched"]}
    assert by_name["Unknown Thing"]["count"] == 2 and by_name["Unknown Thing"]["outcome"] == "no_results"
    assert by_name["Ambiguous Ale"]["candidates"] == ["Ambiguous Ale A", "Ambiguous Ale B"]
    assert report["matchKinds"]["exact"] >= 1


async def test_suspicious_lists_far_apart_matches_but_not_exact_ones(tmp_path):
    lens_log.init(str(tmp_path))
    await lens_log.record(
        [{"name": "Totally Different", "brewery": "X"}, {"name": "PINTA Hazy Morning", "brewery": "PINTA"}],
        [matched("Some Other Beer", "Y", 7), matched("Hazy Morning", "PINTA", 1)],
    )
    suspicious = (await lens_log.report())["suspicious"]
    assert [e["queryName"] for e in suspicious] == ["Totally Different"]
    assert suspicious[0]["matchKind"] == "different"


async def test_outcome_change_is_remembered_and_listed(tmp_path):
    lens_log.init(str(tmp_path))
    item = [{"name": "BA Monster's Park Chocolate 2025", "brewery": "AleSmith"}]
    await lens_log.record(item, [unmatched()])
    await lens_log.record(item, [matched("Barrel Aged Speedway Stout: Monster's Park Chocolate Espresso Edition (2025)", "AleSmith", 6197916)])
    changed = (await lens_log.report())["changed"]
    assert len(changed) == 1
    assert changed[0]["outcome"] == "matched" and changed[0]["previous"]["outcome"] == "no_results"


async def test_persists_and_evicts_oldest_beyond_the_cap(tmp_path, monkeypatch):
    lens_log.init(str(tmp_path))
    await lens_log.record([{"name": "First", "brewery": "B"}], [unmatched()])
    lens_log.init(str(tmp_path))  # fresh load from disk
    assert (await lens_log.report())["total"] == 1

    monkeypatch.setattr(lens_log, "MAX_ENTRIES", 3)
    for i in range(5):
        await lens_log.record([{"name": f"Beer {i}", "brewery": "B"}], [unmatched()])
    names = {e["queryName"] for e in (await lens_log.report())["unmatched"]}
    assert len(names) == 3 and "Beer 4" in names


def test_classify_match():
    assert beer_match.classify_match("PINTA Hazy Morning", "PINTA", "Hazy Morning") == ("exact", 0)
    kind, delta = beer_match.classify_match("AleSmith: BA Monster's Park Chocolate 2025", "AleSmith",
                                            "Barrel Aged Speedway Stout: Monster's Park Chocolate Espresso Edition (2025)")
    assert kind == "candidate_longer" and delta == 4
    assert beer_match.classify_match("Totally Different", "X", "Some Other Beer")[0] == "different"


async def test_lookup_handler_logs_and_report_endpoint_needs_the_token(tmp_path, monkeypatch):
    lens_log.init(str(tmp_path))

    async def fake_resolve(token, owner_id, name, brewery, **kw):
        return matched("Barrel-Aged Speedway Stout (2023)", "AleSmith Brewing Company", 5628687) if "Speedway" in name \
            else unmatched()

    async def fake_token(owner_id):
        return "tok"

    async def no_beers(*a, **kw):
        return []

    monkeypatch.setattr(ws, "LENS_API_TOKEN", "secret")
    monkeypatch.setattr(ws, "AUTO_TOAST_OWNER_ID", "1")
    monkeypatch.setattr(ws.user_tokens, "get_token", fake_token)
    monkeypatch.setattr(ws, "_get_wishlist_beers", no_beers)
    monkeypatch.setattr(ws, "_get_my_list_items", no_beers)
    monkeypatch.setattr(ws.beer_match, "resolve_beer", fake_resolve)

    app = web.Application()
    app.router.add_post("/api/lens/lookup", ws.handle_lens_lookup)
    app.router.add_get("/api/lens/report", ws.handle_lens_report)
    async with TestClient(TestServer(app)) as c:
        headers = {"X-Lens-Token": "secret"}
        body = {"items": [{"name": "AleSmith: BA Speedway Stout 2023", "brewery": "AleSmith"},
                          {"name": "Mystery Beer", "brewery": "Nobody"}]}
        r = await c.post("/api/lens/lookup", json=body, headers=headers)
        assert r.status == 200 and len((await r.json())["results"]) == 2

        assert (await c.get("/api/lens/report")).status == 401
        assert (await c.get("/api/lens/report", headers={"X-Lens-Token": "wrong"})).status == 401
        report = await (await c.get("/api/lens/report?limit=5", headers=headers)).json()
        assert report["total"] == 2 and report["outcomes"] == {"matched": 1, "no_results": 1}
        assert [e["queryName"] for e in report["unmatched"]] == ["Mystery Beer"]
