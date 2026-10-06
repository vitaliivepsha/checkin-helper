import json
from pathlib import Path

import festival_map

ZONES = ["Area 1", "Area 2"]
TEMPLATE = {
    "version": 1,
    "zones": {
        "Area 1": {
            "left": ["Bednary", "Markowy", "Zakładowy", "Nepo"],
            "right": ["Nieczajna"],
            "islands": {"isl_1": {"label": "", "breweries": ["Alchemik", "Brokreacja"]}},
        },
        "Area 2": {"top": ["Mazurski", "Inne Beczki"], "left": ["Magic Road", "Kingpin"]},
    },
}
KNOWN = ["Browar Bednary", "Browar Zakładowy", "Browar Brokreacja", "Browar Nieczajna", "Magic Road", "Browar Kingpin", "Browar Sulewski"]
HINT = {b: "Area 1" for b in KNOWN}


async def layout(tmp_path, known=KNOWN, template=TEMPLATE):
    return await festival_map.get_layout("wfp", known, {b: "Area 1" for b in known}, ZONES, template=template)


def saved(tmp_path):
    return json.loads((tmp_path / "festival_map.json").read_text(encoding="utf-8"))["wfp"]


def test_resolve_label_matches_whole_words_ignoring_case_and_diacritics():
    names = ["Browar Bednary", "Browar Spółdzielczy", "PINTA", "Pintaz", "Magic Road", "Browar Monsters / Sick Boy Brewing"]
    assert festival_map.resolve_label("bednary", names) == "Browar Bednary"
    assert festival_map.resolve_label("Spoldzielczy", names) == "Browar Spółdzielczy"
    assert festival_map.resolve_label("Pinta", names) == "PINTA"  # not "Pintaz"
    assert festival_map.resolve_label("Monsters", names) == "Browar Monsters / Sick Boy Brewing"
    assert festival_map.resolve_label("Mag", names) is None
    assert festival_map.resolve_label("", names) is None


def test_resolve_label_prefers_the_candidate_with_fewest_extra_words():
    assert festival_map.resolve_label("Pinta", ["Pinta Collab Series Brewing", "PINTA"]) == "PINTA"


async def test_first_read_rebuilds_the_layout_in_plan_order(tmp_path):
    festival_map.init(str(tmp_path))
    result = await layout(tmp_path)
    assert result["Area 1"]["left"] == ["Browar Bednary", "Browar Zakładowy"]  # Markowy/Nepo aren't known yet - no gaps
    assert result["Area 1"]["right"] == ["Browar Nieczajna"]
    assert result["Area 1"]["islands"]["isl_1"]["breweries"] == ["Browar Brokreacja"]
    assert result["Area 2"]["left"] == ["Magic Road", "Browar Kingpin"]
    assert "Browar Sulewski" in result["Area 1"]["top"] + result["Area 1"]["left"] + result["Area 1"]["right"] + result["Area 1"]["bottom"]
    meta = saved(tmp_path)
    assert meta["templateVersion"] == 1 and "previousZones" in meta


async def test_rebuild_replaces_an_old_manual_layout_but_keeps_it_as_previous(tmp_path):
    festival_map.init(str(tmp_path))
    await festival_map.get_layout("wfp", KNOWN, HINT, ZONES)  # no template yet: seeded by hint
    await festival_map.move_brewery("wfp", "Browar Nieczajna", "Area 2", "top", 0, ZONES)
    result = await layout(tmp_path)  # the template arrives
    assert result["Area 1"]["right"] == ["Browar Nieczajna"] and "Browar Nieczajna" not in result["Area 2"]["top"]
    assert "Browar Nieczajna" in saved(tmp_path)["previousZones"]["Area 2"]["top"]


async def test_after_the_rebuild_user_moves_stick_and_the_template_is_not_reapplied(tmp_path):
    festival_map.init(str(tmp_path))
    await layout(tmp_path)
    assert await festival_map.move_brewery("wfp", "Browar Nieczajna", "Area 2", "top", 0, ZONES)
    again = await layout(tmp_path)
    assert again["Area 2"]["top"][0] == "Browar Nieczajna"
    assert saved(tmp_path)["templateVersion"] == 1  # move_brewery kept the metadata


async def test_island_changes_keep_the_template_metadata(tmp_path):
    festival_map.init(str(tmp_path))
    await layout(tmp_path)
    new_id = await festival_map.create_island("wfp", "Area 2", ZONES)
    assert saved(tmp_path)["templateVersion"] == 1
    assert await festival_map.delete_island("wfp", "Area 2", new_id, ZONES)
    assert saved(tmp_path)["templateVersion"] == 1


async def test_a_later_arrival_is_inserted_at_its_plan_position(tmp_path):
    festival_map.init(str(tmp_path))
    await layout(tmp_path)
    known = KNOWN + ["Browar Markowy", "Browar Nepo"]
    result = await layout(tmp_path, known=known)
    # plan order is Bednary, Markowy, Zakładowy, Nepo
    assert result["Area 1"]["left"] == ["Browar Bednary", "Browar Markowy", "Browar Zakładowy", "Browar Nepo"]


async def test_a_later_arrival_creates_its_template_island_when_needed(tmp_path):
    festival_map.init(str(tmp_path))
    await layout(tmp_path, known=["Browar Bednary"])
    assert "isl_1" not in (await layout(tmp_path, known=["Browar Bednary"]))["Area 1"]["islands"]
    result = await layout(tmp_path, known=["Browar Bednary", "Alchemik"])
    assert result["Area 1"]["islands"]["isl_1"]["breweries"] == ["Alchemik"]


async def test_unmatched_breweries_still_fall_back_to_the_hint_seeding(tmp_path):
    festival_map.init(str(tmp_path))
    result = await layout(tmp_path, known=["Totally Unlisted Brewing"])
    placed = [b for side in ("top", "left", "right", "bottom") for b in result["Area 1"][side]]
    assert placed == ["Totally Unlisted Brewing"]


async def test_a_version_bump_rebuilds_again(tmp_path):
    festival_map.init(str(tmp_path))
    await layout(tmp_path)
    await festival_map.move_brewery("wfp", "Browar Nieczajna", "Area 2", "top", 0, ZONES)
    result = await layout(tmp_path, template={**TEMPLATE, "version": 2})
    assert result["Area 1"]["right"] == ["Browar Nieczajna"] and saved(tmp_path)["templateVersion"] == 2


def test_the_shipped_wfp_template_is_well_formed():
    template = json.loads((Path(__file__).parent.parent / "festival_layouts" / "wfp2026.json").read_text(encoding="utf-8"))
    assert template["version"] >= 1 and set(template["zones"]) <= {"Area 1", "Area 2", "Area 3"}
    labels = [lab for spec in template["zones"].values()
              for key, v in spec.items() if key in ("top", "left", "right", "bottom") for lab in v]
    labels += [lab for spec in template["zones"].values() for isl in spec.get("islands", {}).values() for lab in isl["breweries"]]
    assert len(labels) == len({lab.lower() for lab in labels})  # a stand appears once


def test_water_stands_resolves_labels_to_known_stands_and_skips_unknown_ones():
    template = {"water": ["Bednary", "Sulewski", "Not Here Yet", "bednary"]}
    assert festival_map.water_stands(template, KNOWN) == ["Browar Bednary", "Browar Sulewski"]
    assert festival_map.water_stands({}, KNOWN) == []
    assert festival_map.water_stands(None, KNOWN) == []


def test_planned_stands_are_the_plan_labels_no_known_brewery_matches():
    planned = festival_map.planned_stands(TEMPLATE, KNOWN)
    assert planned == ["Markowy", "Nepo", "Alchemik", "Mazurski", "Inne Beczki"]
    assert festival_map.planned_stands(None, KNOWN) == []


async def test_placeholders_fill_the_plan_and_a_real_arrival_takes_over_the_slot(tmp_path):
    festival_map.init(str(tmp_path))
    planned = festival_map.planned_stands(TEMPLATE, KNOWN)
    result = await layout(tmp_path, known=KNOWN + planned)
    assert result["Area 1"]["left"] == ["Browar Bednary", "Markowy", "Browar Zakładowy", "Nepo"]
    assert result["Area 1"]["islands"]["isl_1"]["breweries"] == ["Alchemik", "Browar Brokreacja"]
    assert result["Area 2"]["top"] == ["Mazurski", "Inne Beczki"]
    # an editor moves the placeholder, then its real brewery shows up in the data
    await festival_map.move_brewery("wfp", "Markowy", "Area 2", "top", 0, ZONES)
    known = KNOWN + ["Browar Markowy"]
    result = await layout(tmp_path, known=known + festival_map.planned_stands(TEMPLATE, known))
    assert result["Area 2"]["top"][0] == "Browar Markowy"  # same slot, no duplicate
    assert "Markowy" not in [b for z in result.values() for s in ("top", "left", "right", "bottom") for b in z[s]]
    assert sum("Markowy" in (b or "") for z in result.values() for s in ("top", "left", "right", "bottom") for b in z[s]) == 1


async def test_gaps_can_be_inserted_removed_and_survive_a_reload(tmp_path):
    festival_map.init(str(tmp_path))
    await layout(tmp_path)  # Area 1 left: Bednary, Zakładowy
    assert await festival_map.insert_gap("wfp", "Area 1", "left", 1, ZONES)
    assert (await layout(tmp_path))["Area 1"]["left"] == ["Browar Bednary", None, "Browar Zakładowy"]
    # a gap past the end separates nothing: accepted, nothing stored
    assert await festival_map.insert_gap("wfp", "Area 1", "left", 9, ZONES)
    assert (await layout(tmp_path))["Area 1"]["left"] == ["Browar Bednary", None, "Browar Zakładowy"]
    assert not await festival_map.remove_gap("wfp", "Area 1", "left", 0, ZONES)  # a real stand, not a gap
    assert await festival_map.remove_gap("wfp", "Area 1", "left", 1, ZONES)
    assert (await layout(tmp_path))["Area 1"]["left"] == ["Browar Bednary", "Browar Zakładowy"]


async def test_gaps_work_inside_islands_and_reject_bad_targets(tmp_path):
    festival_map.init(str(tmp_path))
    await layout(tmp_path, known=KNOWN + ["Alchemik"])
    assert await festival_map.insert_gap("wfp", "Area 1", "island", 1, ZONES, island_id="isl_1")
    assert (await layout(tmp_path, known=KNOWN + ["Alchemik"]))["Area 1"]["islands"]["isl_1"]["breweries"] == [
        "Alchemik", None, "Browar Brokreacja"]
    assert not await festival_map.insert_gap("wfp", "Area 1", "island", 0, ZONES, island_id="isl_9")
    assert not await festival_map.insert_gap("wfp", "Area 9", "left", 0, ZONES)
    assert not await festival_map.insert_gap("wfp", "Area 1", "middle", 0, ZONES)
    assert not await festival_map.insert_gap("wfp", "Area 1", "left", -1, ZONES)


async def test_an_island_keeps_a_gap_below_its_last_stand_but_a_side_does_not(tmp_path):
    festival_map.init(str(tmp_path))
    known = KNOWN + ["Alchemik"]
    await layout(tmp_path, known=known)  # island isl_1: Alchemik, Brokreacja
    assert await festival_map.insert_gap("wfp", "Area 1", "island", 2, ZONES, island_id="isl_1")
    assert (await layout(tmp_path, known=known))["Area 1"]["islands"]["isl_1"]["breweries"] == [
        "Alchemik", "Browar Brokreacja", None]
    assert await festival_map.insert_gap("wfp", "Area 1", "island", 0, ZONES, island_id="isl_1")  # and above the first
    assert (await layout(tmp_path, known=known))["Area 1"]["islands"]["isl_1"]["breweries"] == [
        None, "Alchemik", "Browar Brokreacja", None]
    assert await festival_map.remove_gap("wfp", "Area 1", "island", 3, ZONES, island_id="isl_1")
    assert await festival_map.insert_gap("wfp", "Area 1", "left", 9, ZONES)  # a side: accepted, nothing stored
    assert (await layout(tmp_path, known=known))["Area 1"]["left"] == ["Browar Bednary", "Browar Zakładowy"]


async def test_an_island_shrinks_when_its_last_stand_leaves_but_keeps_explicit_bottom_gaps(tmp_path):
    festival_map.init(str(tmp_path))
    known = KNOWN + ["Alchemik"]
    await layout(tmp_path, known=known)  # isl_1: Alchemik, Brokreacja
    await festival_map.move_brewery("wfp", "Browar Brokreacja", "Area 2", "top", 0, ZONES)
    assert (await layout(tmp_path, known=known))["Area 1"]["islands"]["isl_1"]["breweries"] == ["Alchemik"]
    await festival_map.insert_gap("wfp", "Area 1", "island", 1, ZONES, island_id="isl_1")
    await festival_map.move_brewery("wfp", "Browar Nieczajna", "Area 2", "top", 0, ZONES)  # unrelated move
    assert (await layout(tmp_path, known=known))["Area 1"]["islands"]["isl_1"]["breweries"] == ["Alchemik", None]
