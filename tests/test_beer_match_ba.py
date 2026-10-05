import beer_match


def beer(bid, name, brewery="AleSmith Brewing Company"):
    return {"bid": bid, "beerName": name, "brewery": {"id": 2471, "name": brewery}}


# Real search_beers answers (Untappd, Oct 2026) for the shop listings that
# the lens got wrong: "BA" in the shop title, "Barrel-Aged" in the catalog.
COCONUT_2023 = beer(5313378, "Barrel-Aged Speedway Stout: Coconut Vanilla Edition (2023)")
PLAIN_2023 = beer(5628687, "Barrel-Aged Speedway Stout (2023)")
MEXICAN_2023 = beer(5603825, "BA Speedway Stout: Mexican Hot Chocolate Edition (2023)")
MONSTERS_PARK = beer(6197916, "Barrel Aged Speedway Stout: Monster's Park Chocolate Espresso Edition (2025)")


def test_shop_ba_matches_the_spelled_out_catalog_name_not_a_literal_ba_sibling():
    # Before the fix this returned MEXICAN_2023 (the only candidate literally
    # containing "BA") - a wrong beer, with another beer's rating.
    results = [COCONUT_2023, PLAIN_2023, MEXICAN_2023]
    match = beer_match.pick_best_match(results, "AleSmith: BA Speedway Stout 2023", "AleSmith")
    assert match["bid"] == 5628687


def test_shop_ba_with_edition_suffix_resolves():
    match = beer_match.pick_best_match([COCONUT_2023], "AleSmith: BA Speedway Stout Coconut Vanilla Edition 2023", "AleSmith")
    assert match["bid"] == 5313378


def test_shop_ba_resolves_via_the_superset_rule_too():
    # "Speedway Stout ... Espresso Edition" are extra words in the catalog
    # name, still within MAX_SUPERSET_EXTRA_TOKENS - but only once the
    # duplicated "AleSmith:" prefix is gone from the query (see the variants
    # test below), since the superset check keeps brewery tokens in place.
    match = beer_match.pick_best_match([MONSTERS_PARK], "BA Monster's Park Chocolate 2025", "AleSmith")
    assert match["bid"] == 6197916


def test_brewery_prefix_with_a_colon_is_stripped_into_a_variant():
    for title, expected in [
        ("AleSmith: BA Monster's Park Chocolate 2025", "BA Monster's Park Chocolate 2025"),
        ("AleSmith: BA Speedway Stout 2023", "BA Speedway Stout 2023"),
    ]:
        cores, original, clean = beer_match._query_context(title, "AleSmith")
        assert expected in beer_match._query_name_variants(clean, original)


def test_brewery_prefix_strip_still_ignores_a_name_that_merely_starts_similarly():
    assert beer_match._brewery_prefix_stripped_variant("AleSmithy Stout", "AleSmith") is None
    assert beer_match._brewery_prefix_stripped_variant("Ale Smith Stout", "AleSmith") is None


def test_a_catalog_entry_that_literally_says_ba_still_matches_itself():
    results = [COCONUT_2023, PLAIN_2023, MEXICAN_2023]
    match = beer_match.pick_best_match(results, "BA Speedway Stout: Mexican Hot Chocolate Edition (2023)", "AleSmith")
    assert match["bid"] == 5603825


def test_the_komes_wrong_sibling_is_refused():
    # The case MAX_SUPERSET_EXTRA_TOKENS exists for. It sat exactly AT the cap
    # (5 extra words, "BA" counted once); "BA" now counts as the two words it
    # stands for, so this refusal is stricter, never looser.
    results = [beer(1, "Wymrażany Imperialny Porter Bałtycki Old Forester BA z Amburaną", "Komes")]
    assert beer_match.pick_best_match(results, "Porter Bałtycki z Amburaną", "Komes") is None


def test_scan_norm_expands_ba_only_as_a_standalone_word():
    assert beer_match.scan_norm("BA Imperial Stout") == "barrel aged imperial stout"
    assert beer_match.scan_norm("Barrel-Aged Stout") == "barrel aged stout"
    assert beer_match.scan_norm("Bałtycki Banana") == "baltycki banana"  # "ba" fragments don't count
