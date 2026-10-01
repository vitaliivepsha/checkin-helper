import pending_checkins

SAMPLE = {
    "beerId": 123, "beerName": "Test Beer", "brewery": "Test Brewery",
    "style": "IPA", "abv": "6%", "labelUrl": None,
    "rating": 4, "shout": "", "foursquareId": "abc", "geolat": 1.0, "geolng": 2.0,
    "venueName": "Test Venue", "queueItemId": None, "failReason": "rate_limited",
}


async def test_add_then_list(tmp_path):
    pending_checkins.init(str(tmp_path))
    item = await pending_checkins.add_item(1, SAMPLE)
    assert item["beerId"] == 123
    assert item["failReason"] == "rate_limited"
    assert "id" in item and "createdAt" in item
    items = await pending_checkins.list_items(1)
    assert len(items) == 1
    assert items[0]["id"] == item["id"]


async def test_no_dedupe_same_beer_twice(tmp_path):
    pending_checkins.init(str(tmp_path))
    await pending_checkins.add_item(1, SAMPLE)
    await pending_checkins.add_item(1, SAMPLE)
    items = await pending_checkins.list_items(1)
    assert len(items) == 2
    assert items[0]["id"] != items[1]["id"]


async def test_different_users_are_independent(tmp_path):
    pending_checkins.init(str(tmp_path))
    await pending_checkins.add_item(1, SAMPLE)
    await pending_checkins.add_item(2, SAMPLE)
    assert len(await pending_checkins.list_items(1)) == 1
    assert len(await pending_checkins.list_items(2)) == 1


async def test_get_item(tmp_path):
    pending_checkins.init(str(tmp_path))
    item = await pending_checkins.add_item(1, SAMPLE)
    found = await pending_checkins.get_item(1, item["id"])
    assert found == item
    assert await pending_checkins.get_item(1, "nonexistent") is None
    assert await pending_checkins.get_item(2, item["id"]) is None  # wrong user


async def test_remove_item(tmp_path):
    pending_checkins.init(str(tmp_path))
    item = await pending_checkins.add_item(1, SAMPLE)
    removed = await pending_checkins.remove_item(1, item["id"])
    assert removed is True
    assert await pending_checkins.list_items(1) == []


async def test_remove_unknown_item_returns_false(tmp_path):
    pending_checkins.init(str(tmp_path))
    assert await pending_checkins.remove_item(1, "nonexistent") is False
