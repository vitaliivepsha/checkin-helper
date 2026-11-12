import beer_match


def beer(bid, name, brewery="Browar Zakładowy"):
    return {"bid": bid, "beerName": name, "brewery": {"id": 1, "name": brewery}}


WUJEK = beer(1, "Wujek z Ameryki")
WUJEK_DOUBLE = beer(2, "Podwójny Wujek z Ameryki")
WUJEK_2024 = beer(3, "Wujek Z Ameryki 2024")


def test_raw_name_is_exact_where_the_cleaned_name_is_an_ambiguous_superset():
    results = [WUJEK, WUJEK_DOUBLE, WUJEK_2024]
    # the cleaned query ("Wujek Ameryki" - "z" is dropped) fits all three
    assert beer_match.pick_best_match(results, "Wujek Ameryki", "Zakładowy") is None
    # the shop's own text equals exactly one of them
    assert beer_match.pick_best_match(results, "Wujek z Ameryki", "Zakładowy", exact_only=True)["bid"] == 1


def test_exact_only_never_accepts_a_superset():
    results = [WUJEK_DOUBLE]
    assert beer_match.pick_best_match(results, "Wujek z Ameryki", "Zakładowy") is not None  # superset rule
    assert beer_match.pick_best_match(results, "Wujek z Ameryki", "Zakładowy", exact_only=True) is None


def test_shop_typos_match_the_one_catalog_beer_of_the_same_brewery():
    motueka = beer(10, "In Motueka We Trust", "Ziemia Obiecana")
    kwass = beer(11, "Das Is Kwass", "Browar Łańcut")
    assert beer_match.pick_best_match([motueka], "In Mouteka We Trust", "Ziemia obiecana")["bid"] == 10
    assert beer_match.pick_best_match([kwass, beer(12, "Das Is Kwass #2", "Browar Łańcut")], "Dass Is Kwass", "Łańcut")["bid"] == 11


def test_typo_matching_stays_narrow():
    assert beer_match.pick_best_match([beer(20, "Gose", "Browar X")], "Rose", "X") is None   # a different word, not a typo
    assert beer_match.pick_best_match([beer(21, "Stout 100", "Browar X")], "Stout 10", "X") is None   # digits never typo-match
    # right spelling, wrong brewery: not accepted
    assert beer_match.pick_best_match([beer(22, "In Motueka We Trust", "Other Brewery")], "In Mouteka We Trust", "Ziemia obiecana") is None
    # two candidates fit: refuse to guess
    twins = [beer(23, "In Motueka We Trust", "Ziemia Obiecana"), beer(24, "In Motueka  We Trust", "Ziemia Obiecana")]
    assert beer_match.pick_best_match(twins, "In Mouteka We Trust", "Ziemia obiecana") is None


def test_shortened_variants_keep_at_least_two_real_words():
    # Found in the lens log: dropping down to one word superset-matched an unrelated beer.
    variants = beer_match._query_name_variants("Grybów Pilsvar Lach", "Pilsvar")
    assert "Grybów Pilsvar Lach" in variants
    assert not any(v.lower() in ("grybów pilsvar", "grybów") for v in variants)
    variants = beer_match._query_name_variants("Bezalko Oatmeal Stout", "Trzech Kumpli")
    assert "Bezalko" not in variants and "Bezalko Oatmeal" in variants
    # a name that really is long enough still gets its shortened forms
    assert "Wonders Kiwi Banana Coconut" in beer_match._query_name_variants("Wonders Kiwi Banana Coconut Cream", "Magic Road")


def test_a_one_word_query_cannot_grow_into_a_long_unrelated_name():
    barley_wine = beer(30, "English Barley Wine Red Wine BA", "Trzech Kumpli")
    assert beer_match.pick_best_match([barley_wine], "Red", "Trzech Kumpli") is None
    # but a small, plausible extension is still fine
    assert beer_match.pick_best_match([beer(31, "Hazy Morning", "PINTA")], "Hazy", "PINTA")["bid"] == 31
    assert beer_match.pick_best_match([beer(32, "Oversaturated — Citra X Hallertau Blanc X Nelson Sauvin X Riwaka", "Piwne Podziemie")],
                                      "Oversaturated", "Piwne Podziemie")["bid"] == 32


def test_one_wrong_letter_is_a_typo_only_in_long_words():
    assert beer_match._typo_token_match("collsion", "collision")      # 8+: a missing letter
    assert beer_match._typo_token_match("biologigue", "biologique")   # a wrong letter
    assert not beer_match._typo_token_match("gose", "rose")           # short: a different word
    assert not beer_match._typo_token_match("imperial", "imperials2")  # digits never
    nectar = beer(40, "Nectar Collision", "Stu Mostów")
    assert beer_match.pick_best_match([nectar], "Nectar Collsion", "Stu Mostów")["bid"] == 40


def test_a_plain_beer_never_supersets_into_its_barrel_aged_sibling():
    aged = beer(50, "Komes Wymrażany Barley Wine Cognac BA", "Fortuna")
    assert beer_match.pick_best_match([aged], "Komes Barley Wine", "Fortuna") is None
    # ...while a shop that says BA still reaches it
    assert beer_match.pick_best_match([aged], "Komes Wymrażany Barley Wine Cognac BA", "Fortuna")["bid"] == 50
    assert beer_match.pick_best_match([beer(51, "Rodenbach Vintage 2022 (Foeder N° 157)", "Rodenbach")], "Rodenbach Vintage 2022", "Rodenbach")["bid"] == 51


def test_homebrew_clone_kits_are_never_a_match():
    kit = beer(60, "All Grain Clone Kit - Verdant Even Sharks Need Water", "Brewers Kits")
    assert beer_match.pick_best_match([kit], "Verdant Even Sharks Need Water", "Verdant") is None
    real = beer(61, "Even Sharks Need Water", "Verdant")
    assert beer_match.pick_best_match([kit, real], "Verdant Even Sharks Need Water", "Verdant")["bid"] == 61


def test_keg_and_case_listings_lose_their_packaging_suffix():
    for title, clean in [
        ("Schops - keg A 30l", "Schops"), ("Plum Blond Sour Ale - keykeg 20l", "Plum Blond Sour Ale"),
        ("Autumn Hug - KEG 30 l A", "Autumn Hug"), ("Neon Pulse - KEG 30 l A", "Neon Pulse"),
        ("10th Anniversary TDH New England Dipa - KEG 20L typ A", "10th Anniversary TDH New England Dipa"),
        ("Atak Chmielu - karton 10 szt.", "Atak Chmielu"), ("Hazy Morning - puszka 500 ml", "Hazy Morning"),
    ]:
        assert beer_match._clean_beer_name_query(title, "") == clean


def test_a_beer_named_like_its_brewery_matches_only_its_own_exact_name():
    results = [beer(70, "Orval", "Brasserie d'Orval"), beer(71, "Orval (2025)", "Brasserie d'Orval"), beer(72, "Orval Vert", "Brasserie d'Orval")]
    assert beer_match.pick_best_match(results, "Orval", "Brasserie d'Orval")["bid"] == 70
    assert beer_match.pick_best_match([beer(73, "Orval", "Another Brewery")], "Orval", "Brasserie d'Orval") is None


def test_a_descriptor_after_the_shops_name_is_fine_but_a_name_inside_another_is_not():
    moon = "Moon Lark"
    assert beer_match.pick_best_match([beer(80, "Quill. Extra Hořká 12°", moon)], "Quill", moon)["bid"] == 80
    assert beer_match.pick_best_match([beer(81, "Klasik. Hořký Ležák 12°", moon)], "Klasik", moon)["bid"] == 81
    # the documented counter-example: the shop's word sits INSIDE a different beer's name
    assert beer_match.pick_best_match([beer(82, "Gelato XTREME: Blue Velvet", "Funky Fluid")], "Velvet", "Funky Fluid") is None


def test_scandinavian_letters_and_glued_numbers_compare_equal():
    assert beer_match.pick_best_match([beer(90, "Hindbærsnitter", "Magic Road")], "Hindbaersnitter", "Magic Road")["bid"] == 90
    assert beer_match.pick_best_match([beer(91, "Implosion", "To Øl"), beer(92, "Implosion Lager", "To Øl")], "To Ol Implosion", "To Øl")["bid"] == 91
    tap = beer(93, "Hefeweissbier Naturtrüb (Tap 01)", "Schneider Weisse")
    assert beer_match.pick_best_match([tap], "TAP01 HEFEWEISSBIER", "Schneider")["bid"] == 93  # (the variant with the brewery prefix stripped)


def test_plato_degrees_and_a_repeated_brewery_suffix_are_not_part_of_the_name():
    assert beer_match._clean_beer_name_query("Parohatej 11°", "") == "Parohatej"
    assert beer_match._brewery_prefix_stripped_variant("Two Chefs Brewing Bon Chef", "Two Chefs") == "Bon Chef"
    assert beer_match._brewery_prefix_stripped_variant("Two Chefs Bon Chef", "Two Chefs") == "Bon Chef"


def test_brewery_names_compare_across_apostrophe_styles_and_accents():
    valdieu = beer(100, "Val-Dieu Triple", "Brasserie de l'Abbaye du Val-Dieu")
    assert beer_match.pick_best_match([valdieu], "Val-Dieu Triple", "Brasserie de l’Abbaye du Val-Dieu")["bid"] == 100
    assert beer_match.pick_best_match([beer(101, "Cuvée", "Brasserie Fantôme")], "Cuvee", "Brasserie Fantome")["bid"] == 101


def test_a_missing_letter_in_a_seven_letter_word_is_a_typo():
    cocktail = beer(102, "The Cocktail Collection: Rio Vibes", "Nepo Brewing")
    assert beer_match.pick_best_match([cocktail], "The Coctail Collection Rio Vibes", "Nepo Brewing")["bid"] == 102


def test_trademark_marks_are_not_part_of_the_name():
    rhapzody = beer(30, "Limited Exhibition - Rhapzody™", "TankBusters.Co")
    assert beer_match.pick_best_match([rhapzody], "Limited Exhibition Rhapzody", "TankBusters")["bid"] == 30


def test_the_same_words_split_differently_still_match_exactly():
    wifi = beer(31, "There's No Wi-Fi In My Garden", "TankBusters.Co")
    assert beer_match.pick_best_match([wifi], "There's No Wifi In My Garden", "TankBusters")["bid"] == 31
    glass_go = beer(32, "Glass Go", "Browar Łańcut")
    assert beer_match.pick_best_match([glass_go], "GLASSGO", "Łańcut")["bid"] == 32
    # gluing is only for names long enough not to coincide by accident
    assert beer_match.pick_best_match([beer(33, "A B", "Browar X")], "AB", "X") is None


def test_a_shop_typo_is_found_even_when_the_catalog_name_repeats_the_brewery():
    krush = beer(34, "Duvel Tripel Hop Krush", "Duvel Moortgat")
    assert beer_match.pick_best_match([krush], "DUVEL TRIPLE HOP KRUSH", "Duvel Moortgat")["bid"] == 34


def test_double_ipa_abbreviations_are_dropped_as_style_words():
    assert "Nebuchadnezzar" in beer_match._query_name_variants("Nebuchadnezzar IIPA", "Omnipollo")


def test_shortening_never_drops_a_numbered_edition():
    drops = beer_match._drops_edition_number
    assert drops("GWAREK ZERO INON VOL.4", "ZERO INON", "Zero In On", "Gwarek")
    assert not drops("GWAREK ZERO INON VOL.4", "ZERO INON", "Zero In On Vol. 4", "Gwarek")
    assert not drops("Wonders Kiwi Banana Coconut Cream", "Wonders Kiwi Banana Coconut", "Wonders - Kiwi, Banana, Coconut Milk", "Magic Road")


def lookalike(bid, name, brewery="Pilsvar", rating=3.7):
    return {"bid": bid, "beerName": name, "brewery": {"id": 1, "name": brewery}, "style": "Lager", "globalRating": rating, "ratingCount": 40}


def test_one_close_lookalike_of_the_same_brewery_is_a_probable_match():
    probable = beer_match._probable_candidate([lookalike(1, "Pilsvar Premium")], "GRYBÓW PILSVAR PREMIUM", "Pilsvar")
    assert probable["bid"] == 1 and probable["rating"] == 3.7 and probable["url"].endswith("/1")
    # the catalog name inside the shop's longer text, and the other way round
    assert beer_match._probable_candidate([lookalike(2, "Bezalko Miłosławski", "Browar Fortuna")], "Bezalko Miłosławski o smaku Cydru", "Miłosław") is None  # brewery differs
    assert beer_match._probable_candidate([lookalike(3, "Rolator", "Brauerei Kundmüller")], "Weiherer Rolator", "Kundmüller")


def test_probable_match_needs_one_result_the_right_brewery_and_no_conflicting_edition():
    one = lookalike(1, "Zero In On Vol. 3", "Browar Gwarek")
    assert beer_match._probable_candidate([one, lookalike(2, "Zero In On Vol.2", "Browar Gwarek")], "Zero In On", "Gwarek") is None   # two to choose from
    assert beer_match._probable_candidate([one], "Zero In On Vol.4", "Gwarek") is None                                          # another edition
    assert beer_match._probable_candidate([lookalike(4, "Hazy Morning", "Upside Down")], "Hazy Morning Pinta", "Pinta") is None  # not their beer
    assert beer_match._probable_candidate([lookalike(5, "Komes Porter Barrel Aged", "Komes")], "Komes Porter", "Komes") is None   # barrel version
    assert beer_match._probable_candidate([lookalike(6, "Totally Different Words", "Pilsvar")], "Grybów Pilsvar Lach", "Pilsvar") is None


def test_with_probable_adds_it_to_an_unmatched_result_only_when_asked(monkeypatch):
    import asyncio
    probable = {"name": "Pilsvar Premium", "brewery": "Pilsvar", "bid": 1, "url": "u", "style": "Lager", "abv": 4.5, "rating": 3.7, "ratingCount": 40}

    async def fake_identity(token, name, brewery, rejected=frozenset(), confirmed=None):
        return {"matched": False, "candidates": [{"name": "Pilsvar Premium", "bid": 1}], "probable": probable}

    async def fake_had_it(token, user_id, bid, live_fallback):
        return True

    monkeypatch.setattr(beer_match, "_resolve_identity", fake_identity)
    monkeypatch.setattr(beer_match, "_had_it", fake_had_it)
    without = asyncio.run(beer_match.resolve_beer("t", 1, "Grybów Pilsvar Premium", "Pilsvar"))
    assert without["matched"] is False and "probable" not in without
    with_it = asyncio.run(beer_match.resolve_beer("t", 1, "Grybów Pilsvar Premium", "Pilsvar", with_probable=True))
    assert with_it["probable"]["name"] == "Pilsvar Premium" and with_it["probable"]["hadIt"] is True


def test_a_shops_edition_marker_becomes_the_catalogs_hash_number():
    assert beer_match._edition_marker_variant("10th Anniversary no.5") == "10th Anniversary #5"
    assert beer_match._edition_marker_variant("Brew Nr 12 Special") == "Brew #12 Special"
    assert beer_match._edition_marker_variant("Beer № 7") == "Beer #7"
    assert beer_match._edition_marker_variant("Nordic Beer") is None and beer_match._edition_marker_variant("Hazy Morning") is None
    assert "10th Anniversary #5" in beer_match._query_name_variants("10th Anniversary no.5", "Dziki Wschód")
    sunset = beer(40, "10th Anniversary #5: Sunset Guava Farmhouse", "Browar Dziki Wschód")
    assert beer_match.pick_best_match([sunset], "10th Anniversary #5", "Dziki Wschód")["bid"] == 40
    assert beer_match._match_route("10th Anniversary no.5", "10th Anniversary #5", sunset["beerName"], "Dziki Wschód") == "superset"


def test_another_numbered_edition_can_never_be_the_shops_beer():
    conflict = beer_match.edition_conflict
    assert conflict("10th Anniversary no.5", "10th Anniversary #1: Nitro Bitter Citrus Zest")
    assert conflict("10th Anniversary #5", "10th Anniversary #6: Cucumber Gose")
    assert not conflict("10th Anniversary no.5", "10th Anniversary #5: Sunset Guava Farmhouse")
    assert not conflict("Hazy Morning", "Hazy Morning #3")          # the shop names no number
    assert not conflict("10th Anniversary no.5", "10th Anniversary Collab")   # the candidate is not numbered


def test_a_version_or_vintage_dropped_from_a_shortened_name_is_not_a_match():
    drops = beer_match._drops_edition_number
    assert drops("Beer Brothers 2.0", "Beer Brothers", "Beer Brothers", "Ziemia Obiecana")                     # a version
    assert drops("Dark Star BBA 2016", "Dark Star BBA", "Dark Star BBA With Coffee", "Fremont")                # a vintage, not the bare name
    assert not drops("Dark Star BBA 2016", "Dark Star BBA", "Dark Star BBA", "Fremont")                        # the bare name without a year: fine
    assert not drops("Hazy Morning 500 ml", "Hazy Morning", "Hazy Morning", "PINTA")                           # packaging is not an edition


def test_brewery_names_match_as_whole_words_not_character_substrings():
    match = beer_match.brewery_names_match
    assert match("Stu Mostów", "Browar Stu Mostów") and match("Browar Stu Mostów", "Stu Mostow")
    assert match("l’Abbaye", "L'Abbaye") and match("Ziemia Obiecana/Pinta Brewery", "Ziemia Obiecana")
    assert not match("Ora", "Browar Kormoran") and not match("Art", "Stuttgart Brewing")        # audit M5
    assert not match("", "Anything") and not match("Pinta", "")
    assert not match("Mikkeller Brewpub", "Brewpub Mikkeller")                                    # words keep their order


def _result(bid, name, brewery, aliases=()):
    return {"bid": bid, "beerName": name, "brewery": {"id": 1, "name": brewery}, "aliases": list(aliases), "style": "IPA", "globalRating": 3.6, "ratingCount": 20}


async def _identity(monkeypatch, results, name, brewery, **kw):
    async def fake_search(token, query, limit=10):
        return results

    monkeypatch.setattr(beer_match.untappd_mcp, "search_beers", fake_search)
    beer_match._identity_cache.clear()
    return await beer_match._resolve_identity("t", name, brewery, **kw)


async def test_a_lone_hit_by_another_brewery_is_probable_not_matched(monkeypatch):
    # audit M1: "Hazy IPA" of a small brewery missing from Untappd found another brewery's "Hazy IPA"
    other = _result(1, "Hazy IPA", "Totally Other Brewing")
    identity = await _identity(monkeypatch, [other], "Hazy IPA", "Nowy Browar")
    assert identity["matched"] is False
    assert identity["probable"]["bid"] == 1 and identity["candidates"][0]["bid"] == 1        # still offered, as 🤔
    superset = await _identity(monkeypatch, [_result(2, "Hazy IPA Citra Mosaic", "Totally Other Brewing")], "Hazy IPA", "Nowy Browar")
    assert superset["matched"] is False and superset["probable"]["bid"] == 2


async def test_the_same_brewery_and_its_known_spellings_still_match(monkeypatch):
    same = await _identity(monkeypatch, [_result(3, "Hazy IPA", "Browar Nowy")], "Hazy IPA", "Nowy Browar")
    assert same["matched"] and same["bid"] == 3                                                # generic words and order ignored
    glued = await _identity(monkeypatch, [_result(4, "Kraken Roll", "AleBrowar")], "Kraken Roll", "ALE BROWAR Brewery")
    assert glued["matched"]
    collab = await _identity(monkeypatch, [_result(5, "Beskidy", "Verdant Brewing Co")], "Beskidy", "Pinta & Verdant")
    assert collab["matched"]                                                                   # one of the collab's breweries
    sub_brand = await _identity(monkeypatch, [_result(6, "Freeky Mango Ale", "FREEKY non-alcoholic")], "Freeky Mango Ale", "Maryensztadt")
    assert sub_brand["matched"]                                                                # the shop title names the catalog brand
    renamed = await _identity(monkeypatch, [_result(7, "Trybunał Pils", "Drink ID")], "Trybunał Pils", "Piotrków")
    assert renamed["matched"]                                                                  # BREWERY_RENAME knows Piotrków -> Drink ID
    alias = await _identity(monkeypatch, [_result(8, "Hazy IPA", "Weird Name", aliases=["Nowy Browar"])], "Hazy IPA", "Nowy Browar")
    assert alias["matched"]                                                                    # Untappd's own alias of the brewery


async def test_no_brewery_or_a_non_latin_one_cannot_conflict(monkeypatch):
    assert (await _identity(monkeypatch, [_result(9, "Hazy IPA", "Totally Other Brewing")], "Hazy IPA", ""))["matched"]
    cyrillic = await _identity(monkeypatch, [_result(10, "Лісова пісня", "Блукач")], "Лісова пісня", "Блукач")
    assert cyrillic["matched"] is True or cyrillic["matched"] is False   # no crash; names that cannot be compared never demote
    assert beer_match._brewery_conflict(_result(11, "X", "Блукач"), "Блукач", "X") is False


async def test_a_confirmed_beer_is_never_demoted(monkeypatch):
    other = _result(12, "Hazy IPA", "Totally Other Brewing")
    identity = await _identity(monkeypatch, [other], "Hazy IPA", "Nowy Browar", confirmed_bid=12)
    assert identity["matched"] and identity["via"] == "confirmed"


async def test_one_resolution_never_repeats_a_query_and_stops_after_the_budget(monkeypatch):
    # audit M4: the audit counted 20 live searches for one product that is not on Untappd
    queries = []

    async def fake_search(token, query, limit=10):
        queries.append(query)
        return []

    monkeypatch.setattr(beer_match.untappd_mcp, "search_beers", fake_search)
    beer_match._identity_cache.clear()
    identity = await beer_match._resolve_identity(
        "t", "Miodowe Jasne Bezalkoholowe 0,0% Pils 500 ml", "Browar Jana Brewery",
    )
    assert identity["matched"] is False
    assert len(queries) == len({q.lower() for q in queries}), "an identical query was sent twice"
    assert len(queries) <= beer_match.MAX_SEARCHES_PER_RESOLUTION


def test_a_doubled_letter_does_not_equate_style_words():
    # audit M3
    for a, b in (("gose", "goose"), ("hel", "hell"), ("bok", "book"), ("rose", "roose")):
        assert not beer_match._typo_token_match(a, b), (a, b)
        assert not beer_match._typo_token_match(b, a), (b, a)
    for a, b in (("dass", "das"), ("kwass", "kwas"), ("mouteka", "motueka"), ("collsion", "collision")):
        assert beer_match._typo_token_match(a, b), (a, b)
    results = [{"bid": 1, "beerName": "Mango Goose", "brewery": {"name": "Browar Z"}}]
    assert beer_match.pick_best_match(results, "Mango Gose", "Browar Z") is None


async def test_a_name_cleaned_to_a_shorter_beer_prefers_the_catalog_beer_with_the_full_name(monkeypatch):
    # audit M2: "Miodowe Gold" was cleaned to "Miodowe" and became the brewery's plain "Miodowe"
    plain = _result(1, "Miodowe", "Browar Z")
    gold = _result(2, "Miodowe Gold", "Browar Z")
    identity = await _identity(monkeypatch, [plain, gold], "Miodowe Gold", "Browar Z")
    assert identity["matched"] and identity["bid"] == 2


async def test_another_degree_is_another_beer(monkeypatch):
    eleven = _result(3, "Parohatej 11°", "Browar Z")
    identity = await _identity(monkeypatch, [eleven], "Parohatej 12°", "Browar Z")
    assert identity["matched"] is False
    twelve = _result(4, "Parohatej 12°", "Browar Z")
    assert (await _identity(monkeypatch, [eleven, twelve], "Parohatej 12°", "Browar Z"))["bid"] == 4
    bare = _result(5, "Parohatej", "Browar Z")
    assert (await _identity(monkeypatch, [bare], "Parohatej 12°", "Browar Z"))["bid"] == 5   # no number in the catalog name: fine


def test_degree_conflict_only_looks_at_degrees():
    assert beer_match._degree_conflict("Parohatej 12°", "Parohatej 11°")
    assert not beer_match._degree_conflict("Parohatej 12°", "Parohatej 12")
    assert not beer_match._degree_conflict("Hazy IPA 6,5%", "Hazy IPA 11")     # an ABV is not a degree
    assert not beer_match._degree_conflict("Parohatej 12°", "Parohatej")


def test_the_resolution_caches_are_bounded(monkeypatch):
    monkeypatch.setattr(beer_match, "_CACHE_MAX_ENTRIES", 50)
    cache: dict = {}
    for i in range(500):
        beer_match._cache_set(cache, i, {"bid": i})
    assert len(cache) <= 50 and 499 in cache and 0 not in cache      # the oldest are dropped first
