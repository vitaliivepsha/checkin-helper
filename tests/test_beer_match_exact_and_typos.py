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
