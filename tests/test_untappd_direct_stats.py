import asyncio

import pytest

import untappd_direct


class _FakeResponse:
    status_code = 200
    headers = {"X-Ratelimit-Remaining": "90", "X-Ratelimit-Limit": "100"}

    def json(self):
        return {"meta": {"code": 200}, "response": {}}


class _FakeClient:
    async def request(self, method, url, params=None):
        return _FakeResponse()


@pytest.fixture(autouse=True)
def fresh_stats(monkeypatch):
    monkeypatch.setattr(untappd_direct, "_get_client", lambda: _FakeClient())
    untappd_direct._call_times.clear()
    untappd_direct._call_totals.clear()
    untappd_direct._caller_var.set("other")
    yield


async def _call():
    await untappd_direct._call_api("GET", "venue/checkins/1", "tok")


async def test_calls_are_counted_per_caller_label():
    await _call()                                  # unlabelled
    with untappd_direct.caller("badge_sync"):
        await _call()
        await _call()
    await _call()                                  # the label is gone again
    stats = untappd_direct.get_call_stats()
    assert stats["other"] == {"lastHour": 2, "total": 2}
    assert stats["badge_sync"] == {"lastHour": 2, "total": 2}
    assert isinstance(stats["_since"], int)


async def test_a_loop_labels_its_own_task_without_touching_the_others():
    async def loop(label, n):
        untappd_direct.set_caller(label)
        for _ in range(n):
            await _call()
            await asyncio.sleep(0)

    await asyncio.gather(asyncio.create_task(loop("festival_venue", 3)), asyncio.create_task(loop("auto_toast", 2)))
    stats = untappd_direct.get_call_stats()
    assert stats["festival_venue"]["total"] == 3 and stats["auto_toast"]["total"] == 2
    assert "other" not in stats


async def test_a_rate_limited_call_still_counts_and_old_calls_age_out(monkeypatch):
    class _Limited(_FakeResponse):
        status_code = 429

    class _LimitedClient:
        async def request(self, method, url, params=None):
            return _Limited()

    monkeypatch.setattr(untappd_direct, "_get_client", lambda: _LimitedClient())
    with untappd_direct.caller("comment_watch"):
        with pytest.raises(untappd_direct.UntappdRateLimited):
            await _call()
    assert untappd_direct.get_call_stats()["comment_watch"]["lastHour"] == 1
    untappd_direct._call_times["comment_watch"][0] -= 3700   # an hour and a bit ago
    stats = untappd_direct.get_call_stats()
    assert stats["comment_watch"] == {"lastHour": 0, "total": 1}
