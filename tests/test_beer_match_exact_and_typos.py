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
