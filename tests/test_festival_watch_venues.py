import festival_watch
import webapp_server


async def _enable_with_main_venue(owner=1, venue_id=10):
    await festival_watch.set_location(owner, 55.0, 12.0, "Main")
    await festival_watch.set_venue(owner, venue_id, "Main Venue")
    await festival_watch.set_enabled(owner, True)


async def test_jobs_main_only(tmp_path):
    festival_watch.init(str(tmp_path))
    await _enable_with_main_venue()
    await festival_watch.add_extra_venue(1, 20, "Extra")
    jobs = await festival_watch.list_venue_jobs()
    assert [j["venueId"] for j in jobs] == [10]


async def test_extra_venues_add_list_remove_and_cursor(tmp_path):
    festival_watch.init(str(tmp_path))
    await _enable_with_main_venue()
    assert await festival_watch.add_extra_venue(1, 20, "A") is True
    assert await festival_watch.add_extra_venue(1, 21, "B") is True
    assert await festival_watch.add_extra_venue(1, 20, "A again") is False
    await festival_watch.record_extra_venue_tick(1, 21, 555)
    jobs = {j["venueId"]: j for j in await festival_watch.list_extra_venue_jobs()}
    assert jobs[20]["lastCheckinId"] is None and jobs[21]["lastCheckinId"] == 555
    assert [v["venueId"] for v in (await festival_watch.get_config(1))["extraVenues"]] == [20, 21]
    assert await festival_watch.remove_extra_venue(1, 20) is True
    assert await festival_watch.remove_extra_venue(1, 20) is False
    assert [j["venueId"] for j in await festival_watch.list_extra_venue_jobs()] == [21]


async def test_extra_venue_cap(tmp_path):
    festival_watch.init(str(tmp_path))
    for i in range(festival_watch.MAX_EXTRA_VENUES):
        assert await festival_watch.add_extra_venue(1, 1000 + i, "v") is True
    assert await festival_watch.add_extra_venue(1, 9999, "one too many") is False


async def test_set_location_keeps_extra_venues(tmp_path):
    festival_watch.init(str(tmp_path))
    await _enable_with_main_venue()
    await festival_watch.add_extra_venue(1, 20, "Extra")
    await festival_watch.set_location(1, 56.0, 13.0, "Elsewhere")
    cfg = await festival_watch.get_config(1)
    assert cfg["venueId"] is None
    assert [v["venueId"] for v in cfg["extraVenues"]] == [20]


async def test_disabled_owner_has_no_jobs(tmp_path):
    festival_watch.init(str(tmp_path))
    await _enable_with_main_venue()
    await festival_watch.add_extra_venue(1, 20, "Extra")
    await festival_watch.set_enabled(1, False)
    assert await festival_watch.list_venue_jobs() == []
    assert await festival_watch.list_extra_venue_jobs() == []


def test_first_sight_dedupes_per_owner():
    webapp_server._festival_novelty_seen.clear()
    assert webapp_server._festival_novelty_first_sight(1, 500) is True
    assert webapp_server._festival_novelty_first_sight(1, 500) is False
    assert webapp_server._festival_novelty_first_sight(2, 500) is True
    assert webapp_server._festival_novelty_first_sight(1, None) is True
    assert webapp_server._festival_novelty_first_sight(1, None) is True


def test_first_sight_is_bounded():
    webapp_server._festival_novelty_seen.clear()
    for i in range(webapp_server._FESTIVAL_NOVELTY_SEEN_MAX + 10):
        webapp_server._festival_novelty_first_sight(1, i)
    seen = webapp_server._festival_novelty_seen[1]
    assert len(seen) == webapp_server._FESTIVAL_NOVELTY_SEEN_MAX
    assert 0 not in seen


async def test_friends_cursor_roundtrip_and_reset(tmp_path):
    festival_watch.init(str(tmp_path))
    await _enable_with_main_venue()
    assert (await festival_watch.get_config(1))["friendsLastCheckinId"] is None
    await festival_watch.record_friends_tick(1, 42)
    assert (await festival_watch.get_config(1))["friendsLastCheckinId"] == 42
    await festival_watch.record_friends_tick(1, None)
    assert (await festival_watch.get_config(1))["friendsLastCheckinId"] is None
