import group_festivals


async def test_get_unknown_chat_returns_none(tmp_path):
    group_festivals.init(str(tmp_path))
    assert await group_festivals.get_group_festival(999) is None


async def test_set_get_roundtrip(tmp_path):
    group_festivals.init(str(tmp_path))
    await group_festivals.set_group_festival(123, "test_1928")
    assert await group_festivals.get_group_festival(123) == "test_1928"


async def test_set_overwrites_previous_binding(tmp_path):
    group_festivals.init(str(tmp_path))
    await group_festivals.set_group_festival(123, "test_1928")
    await group_festivals.set_group_festival(123, "mbcc2026")
    assert await group_festivals.get_group_festival(123) == "mbcc2026"


async def test_different_chats_are_independent(tmp_path):
    group_festivals.init(str(tmp_path))
    await group_festivals.set_group_festival(1, "test_1928")
    await group_festivals.set_group_festival(2, "mbcc2026")
    assert await group_festivals.get_group_festival(1) == "test_1928"
    assert await group_festivals.get_group_festival(2) == "mbcc2026"
