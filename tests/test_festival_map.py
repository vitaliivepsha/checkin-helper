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
