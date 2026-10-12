import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")  # bot.py reads it at import

import badge_stats
import had_it_index

CSV_HEADER = "beer_name,brewery_name,beer_type,beer_abv,beer_ibu,rating_score,bid,brewery_country,brewery_state\n"


def _bot():
    import bot
    return bot


def test_export_rows_become_rich_entries_with_clean_text():
    text = CSV_HEADER + 'Nostalgia,"Browar Zakładowy","Lager - Fruited  ",6.5,0,3.75,6986514,Poland,"Województwo lubelskie"\n' \
                        ',,,,,,,,\nBad,Row,x,1,1,4,notanumber,PL,\n'
    entries, skipped = _bot()._parse_import_entries("untappd-checkins.csv", text)
    assert skipped == 2  # the blank row has no bid, the bad row's bid isn't a number
    first = entries[0]
    assert first["bid"] == 6986514 and first["rating"] == 3.75
    assert first["style"] == "Lager - Fruited"          # the export's trailing spaces are gone
    assert first["brewery"] == "Browar Zakładowy" and first["country"] == "Poland" and first["abv"] == 6.5


async def test_seeding_updates_records_in_place_instead_of_wiping_them(tmp_path):
    had_it_index.init(str(tmp_path))
    uid = 1
    async with had_it_index._lock:
        had_it_index._entry(uid)["beers"]["10"] = {
            "rating": 4.0, "style": "Non-Alcoholic - Lager", "country": "Poland", "state": "Mazowieckie", "name": "Old name",
        }
        had_it_index._save()
    await had_it_index.seed_from_export(uid, "u", [
        {"bid": 10, "rating": 4.5, "style": "Lager - Pale", "country": None},   # the export's style is current
        {"bid": 10, "rating": None, "name": "New name"},                          # a later unrated check-in
        {"bid": 11, "rating": 3.0, "style": "IPA - American"},
        (12, 2.5),                                                                # the older tuple form still works
    ])
    beers = await had_it_index.get_all_beers(uid)
    assert beers["10"] == {"rating": 4.5, "style": "Lager - Pale", "country": "Poland", "state": "Mazowieckie", "name": "New name"}
    assert beers["11"]["style"] == "IPA - American" and beers["12"] == {"rating": 2.5}
    assert await had_it_index.is_fully_synced(uid)


def test_wheel_of_styles_counts_beer_styles_only_and_normalises():
    assert badge_stats._wheel_style("Lager - Fruited  ") == "Lager - Fruited"
    assert badge_stats._wheel_style("Other") is None                 # not a real style
    assert badge_stats._wheel_style("Lager - Euro Pale") == "Lager - Pale"
    for non_beer in ("Wine - Other", "Spirit - Liqueur - Cream", "Makgeolli - Fruited", "Sake - Junmai"):
        assert badge_stats._wheel_style(non_beer) is None
    assert badge_stats._wheel_style("Non-Alcoholic - IPA") == "Non-Alcoholic - IPA"   # still counted
    beers = {str(i): {"style": s} for i, s in enumerate(
        ["IPA - American", "IPA - American", "Lager - Pale", "Lager - Euro Pale", "Other", "Wine - Other", "Spirit - Liqueur - Other"])}
    wheel = next(r for r in badge_stats.compute_distinct_progress(beers) if r.get("name") == "Wheel of Styles")
    assert wheel["current"] == 2
