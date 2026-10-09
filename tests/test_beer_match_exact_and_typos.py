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
