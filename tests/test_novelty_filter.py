import festival_watch
import webapp_server

HAZY = "IPA - New England / Hazy"
STOUT = "Stout - Imperial / Double"


def test_taxonomy_is_beer_like_and_unique():
    styles = festival_watch.styles_taxonomy()
    assert HAZY in styles and STOUT in styles and "Kölsch" in styles
    assert len(styles) == len(set(styles))
    assert not any(s.startswith(("Wine", "Spirit", "Sake")) for s in styles)


async def test_defaults_mean_every_style_and_every_rating(tmp_path):
    festival_watch.init(str(tmp_path))
    cfg = await festival_watch.get_config(1)
    assert cfg["noveltyStyles"] == [] and cfg["noveltyRatingMin"] == 0.0 and cfg["noveltyRatingMax"] == 5.0
    assert not festival_watch.novelty_filter_active(cfg)


async def test_set_filter_saves_normalizes_and_survives_other_updates(tmp_path):
    festival_watch.init(str(tmp_path))
    assert await festival_watch.set_novelty_filter(1, [STOUT, HAZY, "Not A Style"], 3.449, 4.5)
    cfg = await festival_watch.get_config(1)
    assert cfg["noveltyStyles"] == [HAZY, STOUT]  # taxonomy order, unknown names dropped
    assert (cfg["noveltyRatingMin"], cfg["noveltyRatingMax"]) == (3.4, 4.5)
    await festival_watch.set_radius(1, 300)  # an unrelated setting doesn't reset the filter
    assert (await festival_watch.get_config(1))["noveltyStyles"] == [HAZY, STOUT]
    # every style ticked is the same as none: back to "all"
    assert await festival_watch.set_novelty_filter(1, festival_watch.styles_taxonomy(), 0, 5)
    assert (await festival_watch.get_config(1))["noveltyStyles"] == []


async def test_set_filter_rejects_bad_input(tmp_path):
    festival_watch.init(str(tmp_path))
    assert not await festival_watch.set_novelty_filter(1, "IPA", 0, 5)
    assert not await festival_watch.set_novelty_filter(1, [1, 2], 0, 5)
    assert not await festival_watch.set_novelty_filter(1, [], 4, 3)       # inverted
    assert not await festival_watch.set_novelty_filter(1, [], -1, 5)
    assert not await festival_watch.set_novelty_filter(1, [], 0, 5.5)
    assert not await festival_watch.set_novelty_filter(1, [], "x", 5)
    assert (await festival_watch.get_config(1))["noveltyRatingMin"] == 0.0


def test_filter_predicate():
    cfg = {"noveltyStyles": [HAZY], "noveltyRatingMin": 3.5, "noveltyRatingMax": 4.5}
    assert festival_watch.novelty_passes_filter(cfg, HAZY, 4.0)
    assert not festival_watch.novelty_passes_filter(cfg, STOUT, 4.0)   # wrong style
    assert not festival_watch.novelty_passes_filter(cfg, HAZY, 3.2)    # below the range
    assert not festival_watch.novelty_passes_filter(cfg, HAZY, 4.7)    # above the range
    assert festival_watch.novelty_passes_filter(cfg, HAZY, 3.5) and festival_watch.novelty_passes_filter(cfg, HAZY, 4.5)
    # unknowns never suppress: no style, no rating yet
    assert festival_watch.novelty_passes_filter(cfg, None, 4.0)
    assert festival_watch.novelty_passes_filter(cfg, HAZY, None)
    assert festival_watch.novelty_passes_filter(cfg, HAZY, 0)
    # defaults let everything through
    assert festival_watch.novelty_passes_filter({}, STOUT, 1.2)


async def _item_passes(monkeypatch, cfg, item, facts, bid=7):
    calls = []

    async def fake_facts(owner_id, beer_id):
        calls.append(beer_id)
        return facts

    monkeypatch.setattr(webapp_server, "_novelty_beer_facts", fake_facts)
    ok = await webapp_server._novelty_item_passes_filter(1, cfg, item, bid)
    return ok, calls


async def test_default_filter_never_looks_anything_up(monkeypatch):
    ok, calls = await _item_passes(monkeypatch, {}, {"beer": {}}, None)
    assert ok and calls == []


async def test_style_filter_uses_the_items_own_style_without_a_lookup(monkeypatch):
    cfg = {"noveltyStyles": [HAZY], "noveltyRatingMin": 0.0, "noveltyRatingMax": 5.0}
    ok, calls = await _item_passes(monkeypatch, cfg, {"beer": {"beer_style": HAZY}}, None)
    assert ok and calls == []
    ok, calls = await _item_passes(monkeypatch, cfg, {"beer": {"beer_style": STOUT}}, None)
    assert not ok and calls == []


async def test_scraped_items_without_a_style_are_looked_up(monkeypatch):
    cfg = {"noveltyStyles": [HAZY], "noveltyRatingMin": 0.0, "noveltyRatingMax": 5.0}
    ok, calls = await _item_passes(monkeypatch, cfg, {"beer": {}}, {"style": STOUT, "rating": 4.0})
    assert not ok and calls == [7]
    ok, _ = await _item_passes(monkeypatch, cfg, {"beer": {}}, {"style": HAZY, "rating": 4.0})
    assert ok


async def test_rating_filter_looks_up_the_average_rating(monkeypatch):
    cfg = {"noveltyStyles": [], "noveltyRatingMin": 3.8, "noveltyRatingMax": 5.0}
    ok, calls = await _item_passes(monkeypatch, cfg, {"beer": {"beer_style": HAZY}}, {"style": HAZY, "rating": 3.5})
    assert not ok and calls == [7]
    ok, _ = await _item_passes(monkeypatch, cfg, {"beer": {}}, {"style": HAZY, "rating": 4.1})
    assert ok
    ok, _ = await _item_passes(monkeypatch, cfg, {"beer": {}}, {"style": HAZY, "rating": None})  # no ratings yet
    assert ok


async def test_a_failed_lookup_lets_the_beer_through(monkeypatch):
    cfg = {"noveltyStyles": [HAZY], "noveltyRatingMin": 3.8, "noveltyRatingMax": 5.0}
    ok, calls = await _item_passes(monkeypatch, cfg, {"beer": {}}, None)
    assert ok and calls == [7]
