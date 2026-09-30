"""Tests for festival_map.py's slot-based move_brewery/get_layout - the
per-side lists can hold `None` entries (real, addressable empty slots),
which move_brewery uses to let a lone item on one side be placed at any
row position the OTHER side has, without disturbing anything else (see
the module's own docstring for the full "claim a slot" vs "ordinary
reorder" distinction this covers)."""

import festival_map

ZONES = ["Area 1"]
HINT: dict = {}


async def test_claim_empty_slot_past_current_end(tmp_path):
    festival_map.init(str(tmp_path))
    await festival_map.move_brewery(None, "A", "Area 1", "left", 0, ZONES)
    # left = ["A"], claim slot 3 (past the end) - pads with None, no shift
    await festival_map.move_brewery(None, "A", "Area 1", "left", 3, ZONES)
    layout = await festival_map.get_layout(None, ["A"], HINT, ZONES)
    assert layout["Area 1"]["left"] == [None, None, None, "A"]


async def test_claiming_a_slot_does_not_shift_other_items(tmp_path):
    festival_map.init(str(tmp_path))
    await festival_map.move_brewery(None, "A", "Area 1", "right", 0, ZONES)
    await festival_map.move_brewery(None, "B", "Area 1", "right", 1, ZONES)
    await festival_map.move_brewery(None, "C", "Area 1", "right", 2, ZONES)
    # Move A (currently at right[0], a REAL occupied slot elsewhere) into
    # left slot 1, which is empty (left is empty entirely) - claims it,
    # right's A-slot becomes None instead of B/C shifting left.
    await festival_map.move_brewery(None, "A", "Area 1", "left", 1, ZONES)
    layout = await festival_map.get_layout(None, ["A", "B", "C"], HINT, ZONES)
    assert layout["Area 1"]["left"] == [None, "A"]
    assert layout["Area 1"]["right"] == [None, "B", "C"]


async def test_ordinary_reorder_onto_occupied_slot_shifts(tmp_path):
    festival_map.init(str(tmp_path))
    await festival_map.move_brewery(None, "A", "Area 1", "right", 0, ZONES)
    await festival_map.move_brewery(None, "B", "Area 1", "right", 1, ZONES)
    await festival_map.move_brewery(None, "C", "Area 1", "right", 2, ZONES)
    # right = [A, B, C] - move C onto index 0 (occupied by A) - ordinary
    # reorder: shifts, no None left behind.
    await festival_map.move_brewery(None, "C", "Area 1", "right", 0, ZONES)
    layout = await festival_map.get_layout(None, ["A", "B", "C"], HINT, ZONES)
    assert layout["Area 1"]["right"] == ["C", "A", "B"]


async def test_trailing_none_trimmed(tmp_path):
    festival_map.init(str(tmp_path))
    await festival_map.move_brewery(None, "A", "Area 1", "left", 0, ZONES)
    await festival_map.move_brewery(None, "A", "Area 1", "left", 4, ZONES)
    # left = [None, None, None, None, A] - move A back to slot 0 (empty at
    # this point) - claims it, and the now-fully-trailing Nones get
    # trimmed rather than lingering forever.
    await festival_map.move_brewery(None, "A", "Area 1", "left", 0, ZONES)
    layout = await festival_map.get_layout(None, ["A"], HINT, ZONES)
    assert layout["Area 1"]["left"] == ["A"]


async def test_none_entries_survive_pruning_and_dont_count_as_unknown(tmp_path):
    festival_map.init(str(tmp_path))
    await festival_map.move_brewery(None, "A", "Area 1", "left", 0, ZONES)
    await festival_map.move_brewery(None, "A", "Area 1", "left", 2, ZONES)
    # left = [None, None, A] - a get_layout call with a SMALLER known set
    # (as if a beer-list swap dropped some breweries) must not treat the
    # None slots as "unknown breweries" to prune.
    layout = await festival_map.get_layout(None, ["A"], HINT, ZONES)
    assert layout["Area 1"]["left"] == [None, None, "A"]


# ---- islands (interior clusters - see the module's own docstring) ----


async def test_move_into_nonexistent_island_is_rejected(tmp_path):
    festival_map.init(str(tmp_path))
    moved = await festival_map.move_brewery(None, "A", "Area 1", "top", 0, ZONES, island_id="isl_1")
    assert moved is False


async def test_create_island_then_drop_brewery_into_it(tmp_path):
    festival_map.init(str(tmp_path))
    island_id = await festival_map.create_island(None, "Area 1", ZONES)
    assert island_id == "isl_1"
    moved = await festival_map.move_brewery(None, "A", "Area 1", "top", 0, ZONES, island_id=island_id)
    assert moved is True
    layout = await festival_map.get_layout(None, ["A"], HINT, ZONES)
    assert layout["Area 1"]["islands"][island_id]["breweries"] == ["A"]
    assert layout["Area 1"]["top"] == []  # never seeded/placed there


async def test_second_island_gets_next_numeric_id(tmp_path):
    festival_map.init(str(tmp_path))
    first = await festival_map.create_island(None, "Area 1", ZONES)
    second = await festival_map.create_island(None, "Area 1", ZONES)
    assert first == "isl_1"
    assert second == "isl_2"


async def test_moving_brewery_between_two_islands(tmp_path):
    festival_map.init(str(tmp_path))
    isl1 = await festival_map.create_island(None, "Area 1", ZONES)
    isl2 = await festival_map.create_island(None, "Area 1", ZONES)
    await festival_map.move_brewery(None, "A", "Area 1", "top", 0, ZONES, island_id=isl1)
    await festival_map.move_brewery(None, "A", "Area 1", "top", 0, ZONES, island_id=isl2)
    layout = await festival_map.get_layout(None, ["A"], HINT, ZONES)
    assert layout["Area 1"]["islands"][isl1]["breweries"] == []
    assert layout["Area 1"]["islands"][isl2]["breweries"] == ["A"]


async def test_second_drop_onto_occupied_island_slot_reorders(tmp_path):
    festival_map.init(str(tmp_path))
    island_id = await festival_map.create_island(None, "Area 1", ZONES)
    await festival_map.move_brewery(None, "A", "Area 1", "top", 0, ZONES, island_id=island_id)
    # Claim slot 0 again while it's occupied by A -> ordinary reorder (B
    # first, A shifted after), same as the perimeter's own claim-vs-reorder
    # split.
    await festival_map.move_brewery(None, "B", "Area 1", "top", 0, ZONES, island_id=island_id)
    layout = await festival_map.get_layout(None, ["A", "B"], HINT, ZONES)
    assert layout["Area 1"]["islands"][island_id]["breweries"] == ["B", "A"]


async def test_delete_island_returns_its_breweries_to_top(tmp_path):
    festival_map.init(str(tmp_path))
    island_id = await festival_map.create_island(None, "Area 1", ZONES)
    await festival_map.move_brewery(None, "A", "Area 1", "top", 0, ZONES, island_id=island_id)
    deleted = await festival_map.delete_island(None, "Area 1", island_id, ZONES)
    assert deleted is True
    layout = await festival_map.get_layout(None, ["A"], HINT, ZONES)
    assert island_id not in layout["Area 1"]["islands"]
    assert layout["Area 1"]["top"] == ["A"]


async def test_delete_unknown_island_returns_false(tmp_path):
    festival_map.init(str(tmp_path))
    deleted = await festival_map.delete_island(None, "Area 1", "isl_1", ZONES)
    assert deleted is False


async def test_get_layout_prunes_unknown_brewery_from_island(tmp_path):
    festival_map.init(str(tmp_path))
    island_id = await festival_map.create_island(None, "Area 1", ZONES)
    await festival_map.move_brewery(None, "A", "Area 1", "top", 0, ZONES, island_id=island_id)
    # A festival-data swap no longer knows "A" - it should be pruned from
    # the island exactly like it would be from a perimeter side.
    layout = await festival_map.get_layout(None, [], HINT, ZONES)
    assert layout["Area 1"]["islands"][island_id]["breweries"] == []
