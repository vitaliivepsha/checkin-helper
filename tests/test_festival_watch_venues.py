import festival_watch
import webapp_server


async def _enable_with_main_venue(owner=1, venue_id=10):
    await festival_watch.set_location(owner, 55.0, 12.0, "Main")
    await festival_watch.set_venue(owner, venue_id, "Main Venue")
    await festival_watch.set_enabled(owner, True)


async def test_jobs_main_only(tmp_path):
    festival_watch.init(str(tmp_path))
    await _enable_with_main_venue()
    jobs = await festival_watch.list_venue_jobs()
    assert [(j["slot"], j["venueId"]) for j in jobs] == [("main", 10)]


async def test_alt_venue_adds_second_job_and_has_own_cursor(tmp_path):
    festival_watch.init(str(tmp_path))
    await _enable_with_main_venue()
    await festival_watch.set_alt_venue(1, 20, "Alt Venue")
    await festival_watch.record_venue_tick(1, 111, "main")
    await festival_watch.record_venue_tick(1, 222, "alt")
    jobs = {j["slot"]: j for j in await festival_watch.list_venue_jobs()}
    assert jobs["main"]["lastCheckinId"] == 111
    assert jobs["alt"]["venueId"] == 20
    assert jobs["alt"]["lastCheckinId"] == 222


async def test_set_location_keeps_alt_venue(tmp_path):
    festival_watch.init(str(tmp_path))
    await _enable_with_main_venue()
    await festival_watch.set_alt_venue(1, 20, "Alt Venue")
    await festival_watch.set_location(1, 56.0, 13.0, "Elsewhere")
    cfg = await festival_watch.get_config(1)
    assert cfg["venueId"] is None
    assert cfg["altVenueId"] == 20


async def test_clear_alt_venue(tmp_path):
    festival_watch.init(str(tmp_path))
    await _enable_with_main_venue()
    await festival_watch.set_alt_venue(1, 20, "Alt Venue")
    await festival_watch.clear_alt_venue(1)
    assert [j["slot"] for j in await festival_watch.list_venue_jobs()] == ["main"]


async def test_disabled_owner_has_no_jobs(tmp_path):
    festival_watch.init(str(tmp_path))
    await _enable_with_main_venue()
    await festival_watch.set_alt_venue(1, 20, "Alt Venue")
    await festival_watch.set_enabled(1, False)
    assert await festival_watch.list_venue_jobs() == []


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
