"""The lens's feedback loop: confirmations, the matcher route ("via"), and the Telegram review queue."""

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import beer_match
import i18n
import lens_confirmations
import lens_log
import lens_rejections
import lens_review
import webapp_server as ws


def _init_all(tmp_path):
    for module in (lens_log, lens_rejections, lens_confirmations, lens_review):
        module.init(str(tmp_path))


def beer(bid, name, brewery="Pilsvar"):
    return {"bid": bid, "beerName": name, "brewery": {"id": 1, "name": brewery}, "style": "Lager", "globalRating": 3.5, "ratingCount": 10}


def matched(name, brewery, bid, via="exact"):
    return {"matched": True, "name": name, "brewery": brewery, "bid": bid, "candidates": [], "via": via}


def probable_result(bid, name, brewery="Pilsvar"):
    return {"matched": False, "candidates": [{"name": name}], "probable": {"bid": bid, "name": name, "brewery": brewery, "style": "Lager", "rating": 3.5}}


async def test_confirmations_store_persists_and_undoes(tmp_path):
    lens_confirmations.init(str(tmp_path))
    assert lens_confirmations.confirmed_bid("Pilsvar", "Grybów Pilsvar Lach") is None
    assert await lens_confirmations.add(" pilsvar", "GRYBÓW  pilsvar lach", 7)
    assert lens_confirmations.confirmed_bid("Pilsvar", "Grybów Pilsvar Lach") == 7
    lens_confirmations.init(str(tmp_path))   # restart
    assert lens_confirmations.confirmed_bid("Pilsvar", "Grybów Pilsvar Lach") == 7
    assert not await lens_confirmations.remove("Pilsvar", "Grybów Pilsvar Lach", 8)   # a different beer
    assert await lens_confirmations.remove("Pilsvar", "Grybów Pilsvar Lach", 7)
    assert lens_confirmations.confirmed_bid("Pilsvar", "Grybów Pilsvar Lach") is None


async def test_matcher_takes_the_confirmed_beer_and_names_its_route(monkeypatch):
    async def fake_search(token, query, limit=10):
        return [beer(1, "Pilsvar Premium"), beer(2, "Pilsvar Premium Special Edition 2019 Reserve Blend")]

    monkeypatch.setattr(beer_match.untappd_mcp, "search_beers", fake_search)
    beer_match._identity_cache.clear()
    plain = await beer_match._resolve_identity("t", "Pilsvar Premium", "Pilsvar")
    assert plain["matched"] and plain["bid"] == 1 and plain["via"] == "exact"
    # a confirmation wins even where the rules would never pick that beer
    confirmed = await beer_match._resolve_identity("t", "Pilsvar Premium", "Pilsvar", confirmed_bid=2)
    assert confirmed["bid"] == 2 and confirmed["via"] == "confirmed"


def test_match_route_names_the_kind_of_agreement():
    route = beer_match._match_route
    assert route("Hazy Morning", "Hazy Morning", "Hazy Morning", "PINTA") == "exact"
    assert route("There's No Wifi", "There's No Wifi", "There's No Wi-Fi", "TankBusters") == "glued"
    assert route("Hazy Morning Pinta", "Hazy Morning Pinta", "Hazy Morning Extra Words", "PINTA") == "superset"
    assert route("Triple Hop Krush", "Triple Hop Krush", "Tripel Hop Krush", "Duvel") == "typo"
    assert route("Grybów Pilsvar Lach", "Grybów Pilsvar", "Grybów Pilsvar", "Pilsvar").endswith("+short")


async def test_log_keeps_via_and_probable_and_flags_rechecks(tmp_path):
    _init_all(tmp_path)
    item = {"name": "Pilsvar Premium", "brewery": "Pilsvar"}
    await lens_log.record([item], [probable_result(5, "Pilsvar Premium")])
    entry = (await lens_log.review_candidates(5))[0]
    assert entry["probable"]["bid"] == 5 and entry["queryName"] == item["name"]

    await lens_log.record([item], [matched("Pilsvar Premium", "Pilsvar", 5, via="superset+short")])
    assert await lens_log.entry_via("Pilsvar", item["name"]) == "superset+short"
    assert await lens_log.review_candidates(5) == []   # matched, close names: nothing to review

    await lens_log.mark_recheck("Pilsvar", item["name"])
    assert [i["name"] for i in await lens_log.entries_to_recheck([], 10)] == [item["name"]]
    await lens_log.record([item], [matched("Pilsvar Premium", "Pilsvar", 5)], source="recheck")
    assert await lens_log.entries_to_recheck([], 10) == []   # the fresh result cleared the flag


async def test_review_queue_asks_once_and_applies_the_decisions(tmp_path):
    _init_all(tmp_path)
    shop = [{"name": "GRYBÓW PILSVAR PREMIUM", "brewery": "Pilsvar"}, {"name": "GRYBÓW PILSVAR LACH", "brewery": "Pilsvar"},
            {"name": "GRYBÓW PILSVAR EXCLUSIVE", "brewery": "Pilsvar"}]
    await lens_log.record(shop, [probable_result(1, "Pilsvar Premium"), probable_result(2, "Pilsvar Lach"), probable_result(3, "Pilsvar Exclusive")])

    items = await lens_review.pick()
    assert sorted(i["bid"] for i in items) == [1, 2, 3] and all(i["kind"] == "probable" for i in items)
    await lens_review.register(items)
    assert await lens_review.pick() == []   # asked just now: not again

    by_bid = {i["bid"]: i for i in items}
    assert (await lens_review.decide(by_bid[1]["id"], "ok"))["bid"] == 1
    assert lens_confirmations.confirmed_bid("Pilsvar", "GRYBÓW PILSVAR PREMIUM") == 1
    assert (await lens_review.decide(by_bid[2]["id"], "no"))["bid"] == 2
    assert lens_rejections.rejected_bids("Pilsvar", "GRYBÓW PILSVAR LACH") == frozenset({2})
    assert (await lens_review.decide(by_bid[3]["id"], "skip"))["bid"] == 3
    assert lens_confirmations.confirmed_bid("Pilsvar", "GRYBÓW PILSVAR EXCLUSIVE") is None
    assert await lens_review.decide(by_bid[1]["id"], "ok") is None   # a button answers once
    assert await lens_review.decide("unknown", "ok") is None

    flagged = sorted(e["queryName"] for e in lens_log._load().values() if e.get("recheck"))
    assert flagged == ["GRYBÓW PILSVAR LACH", "GRYBÓW PILSVAR PREMIUM"]   # only the decided ones


async def test_review_question_text_and_buttons_in_both_languages(tmp_path):
    _init_all(tmp_path)
    await lens_log.record([{"name": "Grybów <Lach> & Co", "brewery": "Pilsvar"}], [probable_result(9, "Pilsvar Lach")])
    item = (await lens_review.pick())[0]
    for lang in ("en", "uk"):
        text = lens_review.render(item, lang)
        assert "&lt;Lach&gt; &amp; Co" in text and "https://untappd.com/beer/9" in text and "⭐ 3.50" in text
        rows = lens_review.keyboard_rows(item["id"], lang)
        assert [data.split(":")[1] for _, data in rows[0]] == ["ok", "no", "skip"]
        assert all(len(data) <= 64 for _, data in rows[0])
    assert lens_review.render(item, "en") != lens_review.render(item, "uk")


def test_review_strings_exist_in_both_languages_with_the_same_placeholders():
    import string
    keys = [k for k in i18n.STRINGS["en"] if k.startswith("lens_review_")]
    assert keys
    for key in keys:
        assert key in i18n.STRINGS["uk"], key
        fields = lambda s: {f for _, f, _, _ in string.Formatter().parse(s) if f}
        assert fields(i18n.STRINGS["en"][key]) == fields(i18n.STRINGS["uk"][key]), key


async def test_confirm_endpoints_and_daily_review_send(tmp_path, monkeypatch):
    _init_all(tmp_path)

    async def fake_resolve(token, owner_id, name, brewery, rejected_bids=frozenset(), confirmed_bid=None, **kw):
        if confirmed_bid == 1:
            return {"matched": True, "name": name, "brewery": brewery, "bid": 1, "candidates": [], "via": "confirmed"}
        return {"matched": False, "candidates": [], "query_name": name, "query_brewery": brewery}

    async def fake_token(owner_id):
        return "tok"

    async def no_beers(*a, **kw):
        return []

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kw):
            self.sent.append(kw)

    bot = FakeBot()
    monkeypatch.setattr(ws, "LENS_API_TOKEN", "secret")
    monkeypatch.setattr(ws, "AUTO_TOAST_OWNER_ID", "1")
    monkeypatch.setattr(ws, "_ptb_bot", bot)
    monkeypatch.setattr(ws.user_tokens, "get_token", fake_token)
    monkeypatch.setattr(ws.user_tokens, "get_language", lambda uid: _async("en"))
    monkeypatch.setattr(ws, "_get_wishlist_beers", no_beers)
    monkeypatch.setattr(ws, "_get_my_list_items", no_beers)
    monkeypatch.setattr(ws.beer_match, "resolve_beer", fake_resolve)

    app = web.Application()
    app.router.add_post("/api/lens/confirm", ws.handle_lens_confirm)
    app.router.add_post("/api/lens/unconfirm", ws.handle_lens_unconfirm)
    app.router.add_post("/api/lens/review", ws.handle_lens_review_send)
    async with TestClient(TestServer(app)) as c:
        headers = {"X-Lens-Token": "secret"}
        item = {"brewery": "Pilsvar", "name": "GRYBÓW PILSVAR PREMIUM"}
        assert (await c.post("/api/lens/confirm", json={**item, "bid": 1})).status == 401

        r = await c.post("/api/lens/confirm", json={**item, "bid": 1}, headers=headers)
        assert (await r.json())["result"]["matched"] is True
        assert lens_confirmations.confirmed_bid("Pilsvar", item["name"]) == 1
        r = await c.post("/api/lens/unconfirm", json={**item, "bid": 1}, headers=headers)
        assert (await r.json())["result"]["matched"] is False
        assert lens_confirmations.confirmed_bid("Pilsvar", item["name"]) is None

        await lens_log.record([item], [probable_result(1, "Pilsvar Premium")])
        r = await c.post("/api/lens/review", headers=headers)
        assert (await r.json())["sent"] == 1
        message = bot.sent[0]
        assert message["chat_id"] == 1 and "Pilsvar Premium" in message["text"] and message["parse_mode"] == "HTML"
        assert len(message["reply_markup"].inline_keyboard[0]) == 3
        r = await c.post("/api/lens/review", headers=headers)
        assert (await r.json())["sent"] == 0   # not asked twice


async def _async(value):
    return value


async def test_review_question_names_the_shop_as_the_source_with_a_link(tmp_path):
    _init_all(tmp_path)
    shop_item = {"name": "Pilsvar Premium", "brewery": "Pilsvar", "shop": "piwnemosty",
                 "url": "https://www.piwnemosty.pl/product-pol-1-Pilsvar.html"}
    await lens_log.record([shop_item], [probable_result(5, "Pilsvar Premium")], source="crawl")
    user_item = {"name": "Pilsvar Lach", "brewery": "Pilsvar", "shop": "hoptimaal.com", "page": "https://hoptimaal.com/products/lach"}
    await lens_log.record([user_item], [probable_result(6, "Pilsvar Lach")])
    junk = {"name": "Pilsvar Exclusive", "brewery": "Pilsvar", "page": "javascript:alert(1)"}
    await lens_log.record([junk], [probable_result(7, "Pilsvar Exclusive")])

    by_bid = {i["bid"]: i for i in await lens_review.pick()}
    text = lens_review.render(by_bid[5], "en")
    assert 'Source: <a href="https://www.piwnemosty.pl/product-pol-1-Pilsvar.html">piwnemosty.pl</a>' in text
    assert "https://hoptimaal.com/products/lach" in lens_review.render(by_bid[6], "uk")
    assert "Джерело" in lens_review.render(by_bid[6], "uk")
    assert "Source" not in lens_review.render(by_bid[7], "en")   # a non-https link is never kept
    suspicious_old = {**by_bid[5], "kind": "suspicious", "via": ""}
    assert "(matched by" not in lens_review.render(suspicious_old, "en")   # an entry without a route reads plainly


async def test_crawl_notes_the_source_even_for_products_it_does_not_re_resolve(tmp_path):
    _init_all(tmp_path)
    item = {"name": "Pilsvar Premium", "brewery": "Pilsvar"}
    await lens_log.record([item], [matched("Pilsvar Premium", "Pilsvar", 5)])          # logged before sources existed
    await lens_log.mark_crawled([{**item, "shop": "hoptimaal", "url": "https://hoptimaal.com/products/premium"}])
    entry = next(iter(lens_log._load().values()))
    assert entry["shops"] == ["hoptimaal"] and entry["shopUrl"] == "https://hoptimaal.com/products/premium"
