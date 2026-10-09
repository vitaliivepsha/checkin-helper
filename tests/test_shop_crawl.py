import json
import time
from pathlib import Path

import lens_log
import shop_crawl
import untappd_mcp
import webapp_server as ws

FIX = Path(__file__).parent / "fixtures" / "shops"


def text(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_piwnemosty_parses_cards_and_pager():
    items, last = shop_crawl.parse_piwnemosty(text("piwnemosty.html"))
    assert last == 49  # pages are ?counter=0..49
    assert len(items) == 4
    assert items[0] == {"brewery": "Monsters", "name": "Lizard - puszka 500 ml"}


def test_piwnemosty_strips_ml_packaging_and_skips_bundles():
    html = """<div class="product"><a class="product__name">Brewery X: Hazy One - 473 ml can</a></div>
              <div class="product"><a class="product__name">Brewery X: Fan Box 6 beers</a></div>
              <div class="product"><a class="product__name">Plain Name Without Brewery</a></div>"""
    items, _ = shop_crawl.parse_piwnemosty(html)
    assert items == [{"brewery": "Brewery X", "name": "Hazy One"}, {"brewery": "", "name": "Plain Name Without Brewery"}]


def test_ontap_reads_the_name_between_the_br_tags():
    items = shop_crawl.parse_ontap(text("ontap.html"))
    assert {"brewery": "PINTA Brewery", "name": "Atak Chmielu"} in items
    assert all(it["name"] and not it["name"].endswith("°") for it in items)


def test_ontap_drops_a_leaked_plato_degree():
    html = '<div class="panel panel-default"><h4 class="cml_shadow"><span><b class="brewery">X</b><br>Atak Chmielu 15,1°</span></h4></div>'
    assert shop_crawl.parse_ontap(html) == [{"brewery": "X", "name": "Atak Chmielu"}]


def test_hopincraftbier_keeps_only_brewery_dash_beer_titles():
    items = shop_crawl.parse_hopincraftbier(text("hopincraftbier.html"))
    assert {"brewery": "Finback", "name": "Something Mosaic"} in items
    assert {"brewery": "Brett & Sauvage", "name": "Indigo Gem"} in items
    assert not any("Gift card" in it["name"] or "clip" in it["name"].lower() for it in items)


def test_onemorebeer_cleans_packaging_and_takes_the_producer_as_brewery():
    items = shop_crawl.parse_onemorebeer(json.loads(text("onemorebeer.json")))
    assert {"brewery": "PINTA", "name": "PINTA HOPZZ_ IMPACT"} in items  # "PUSZKA 0,5 L KAUCJA" stripped
    assert {"brewery": "Zakładowy", "name": "ZAKŁADOWY POLSKIE WAKACJE"} in items  # "BUT. 0,5 L" stripped


def test_onemorebeer_skips_bundles_and_falls_back_to_the_manufacturer_record():
    payload = {"items": [
        {"name": "ZESTAW PIW MIX", "characteristics": [], "manufacturer": {"name": "X"}},
        {"name": "ATAK CHMIELU BUT. 0,5 L", "characteristics": [], "manufacturer": {"name": "PINTA"}},
    ]}
    assert shop_crawl.parse_onemorebeer(payload) == [{"brewery": "PINTA", "name": "ATAK CHMIELU"}]


def test_hoptimaal_uses_vendor_and_skips_merch():
    payload = json.loads(text("hoptimaal.json"))
    payload["products"].append({"title": "Hop T-shirt", "vendor": "Hoptimaal", "product_type": "Merch"})
    items = shop_crawl.parse_hoptimaal(payload)
    assert {"brewery": "Hoppy Road", "name": "Hoppy Road BRUTAL MONKEY - Double NEIPA"} in items
    assert not any("T-shirt" in it["name"] for it in items)


async def test_crawl_paginates_and_isolates_a_failing_shop(monkeypatch):
    pages_requested = []

    async def get_text(url):
        pages_requested.append(url)
        if "ontap.pl" in url:
            raise RuntimeError("boom")
        if "hopincraftbier" in url:
            return text("hopincraftbier.html")
        return text("piwnemosty.html")

    seen_headers = []

    async def get_json(url, headers=None):
        if "onecommerce" in url:
            seen_headers.append(headers)
            page = int(url.rsplit("pageNumber=", 1)[1])
            return {**json.loads(text("onemorebeer.json")), "pageNumber": page, "totalPages": 2}
        page = int(url.rsplit("page=", 1)[1])
        return json.loads(text("hoptimaal.json")) if page <= 2 else {"products": []}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(shop_crawl, "MAX_PAGES", {"piwnemosty": 4, "hoptimaal": 20, "onemorebeer": 40})
    crawl = await shop_crawl.crawl_shops(get_text, get_json, sleep=no_sleep, gap=0)

    assert crawl["ontap"]["items"] == [] and "boom" in crawl["ontap"]["error"]
    assert crawl["piwnemosty"]["error"] is None and crawl["piwnemosty"]["pages"] == 4  # capped
    assert crawl["hoptimaal"]["pages"] == 3 and len(crawl["hoptimaal"]["items"]) == 12  # 2 full pages, then empty
    assert crawl["hopincraftbier"]["pages"] == 2
    assert crawl["onemorebeer"]["pages"] == 2 and len(crawl["onemorebeer"]["items"]) == 12  # stops at totalPages
    assert seen_headers == [{"one-tenant": "pinta"}] * 2
    assert all(it["shop"] == "hoptimaal" for it in crawl["hoptimaal"]["items"])
    summary = shop_crawl.summarize(crawl)
    assert summary["ontap"]["items"] == 0 and summary["piwnemosty"]["items"] > 0


def test_dedupe_is_case_insensitive():
    items = [{"brewery": "A", "name": "Beer"}, {"brewery": "a", "name": "BEER"}, {"brewery": "A", "name": "Other"}]
    assert len(shop_crawl.dedupe(items)) == 2


async def test_due_for_crawl_orders_unmatched_then_new_then_stale(tmp_path, monkeypatch):
    lens_log.init(str(tmp_path))
    found = {"matched": True, "name": "Hazy", "bid": 1, "candidates": []}
    await lens_log.record(
        [{"name": "Fresh Match", "brewery": "B"}, {"name": "Old Match", "brewery": "B"}, {"name": "Missing", "brewery": "B"}],
        [found, found, {"matched": False, "candidates": []}], source="crawl",
    )
    lens_log._load()[lens_log._key("B", "Old Match")]["resolvedAt"] = int(time.time()) - lens_log.STALE_AFTER_SECONDS - 10
    items = [{"name": n, "brewery": "B"} for n in ("Fresh Match", "Old Match", "Missing", "Brand New")]
    due = [it["name"] for it in await lens_log.due_for_crawl(items)]
    assert due == ["Missing", "Brand New", "Old Match"]  # "Fresh Match" needs no re-check


async def test_crawl_source_counts_crawl_seen_not_views(tmp_path):
    lens_log.init(str(tmp_path))
    item = [{"name": "Beer", "brewery": "B", "shop": "hoptimaal"}]
    unmatched = [{"matched": False, "candidates": []}]
    await lens_log.record(item, unmatched, source="crawl")
    await lens_log.record(item, unmatched, source="lens")
    await lens_log.mark_crawled(item)
    entry = next(iter(lens_log._load().values()))
    assert entry["count"] == 1 and entry["crawlSeen"] == 2 and entry["shops"] == ["hoptimaal"]


async def test_run_shop_crawl_records_outcomes_and_stops_on_a_rate_limit(tmp_path, monkeypatch):
    lens_log.init(str(tmp_path))
    shop_crawl.init(str(tmp_path))
    names = [f"Beer {i}" for i in range(6)]

    async def fake_crawl(get_text, get_json, **kw):
        return {"hoptimaal": {"items": [{"brewery": "B", "name": n, "shop": "hoptimaal"} for n in names], "pages": 1, "error": None},
                "ontap": {"items": [], "pages": 0, "error": "RuntimeError: down"}}

    calls = []

    async def fake_resolve(token, owner_id, name, brewery, **kw):
        calls.append(name)
        if len(calls) > 3:
            raise untappd_mcp.UntappdRateLimited("slow down")
        return {"matched": True, "name": name, "bid": len(calls), "candidates": []}

    async def fake_token(owner_id):
        return "tok"

    monkeypatch.setattr(shop_crawl, "crawl_shops", fake_crawl)
    monkeypatch.setattr(ws, "AUTO_TOAST_OWNER_ID", "1")
    monkeypatch.setattr(ws.user_tokens, "get_token", fake_token)
    monkeypatch.setattr(ws.beer_match, "resolve_beer", fake_resolve)
    monkeypatch.setattr(ws, "SHOP_CRAWL_BATCH_SIZE", 3)

    state = await ws._run_shop_crawl()
    assert state["products"] == 6 and state["resolved"] == 3 and state["stoppedEarly"] == "rate_limited"
    assert state["shops"]["ontap"]["error"] == "RuntimeError: down"
    assert ws._shop_crawl_running is False and shop_crawl.load_state()["running"] is False
    report = await lens_log.report()
    assert report["total"] == 3 and report["outcomes"] == {"matched": 3}


def test_non_beer_taplist_entries_are_recognised_without_touching_real_beers():
    for brewery, name in [("Frizzante Brewery", "Frizzante"), ("FRIZZANTE MACCARI Brewery", "GLERA VENETO"),
                          ("Graciarnia Brewery", "Whisky z Colą"), ("Cuba Libre Brewery", "Cuba Libre")]:
        assert shop_crawl.is_non_beer({"brewery": brewery, "name": name})
    for brewery, name in [("Browar X", "Whisky Barrel Aged Stout"), ("Y", "Barley Wine"), ("PINTA", "Spritzer Sour"),
                          ("PINTA Brewery", "Pierwsza Pomoc")]:
        assert not shop_crawl.is_non_beer({"brewery": brewery, "name": name})


def test_merch_slips_are_recognised_but_beers_are_not():
    assert shop_crawl.is_not_a_beer({"brewery": "WRCLW", "name": "Pszeniczny T-Shirt"})
    assert shop_crawl.is_not_a_beer({"brewery": "Arpus", "name": "Tumbler 0.4"})
    assert shop_crawl.is_not_a_beer({"brewery": "Frizzante Brewery", "name": "Frizzante"})
    assert not shop_crawl.is_not_a_beer({"brewery": "WRCLW", "name": "Pszeniczny"})
    assert not shop_crawl.is_not_a_beer({"brewery": "Browar X", "name": "Szklanka Stout"} | {"name": "Czarny Stout"})
