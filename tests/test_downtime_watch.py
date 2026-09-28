import asyncio
import time

import downtime_watch


def test_format_duration():
    assert downtime_watch.format_duration(6 * 3600 + 21 * 60) == "6 г 21 хв"
    assert downtime_watch.format_duration(2 * 3600) == "2 г"
    assert downtime_watch.format_duration(7 * 60) == "7 хв"


def test_detect_gap_ignores_normal_restart_and_missing_heartbeat():
    assert not downtime_watch.detect_gap(None, 1000.0)
    assert not downtime_watch.detect_gap(1000.0, 1010.0)  # /restart takes seconds
    assert not downtime_watch.detect_gap(1000.0, 1000.0 + 30 + 60, expected_interval=30)
    assert downtime_watch.detect_gap(1000.0, 1000.0 + 3600)
    assert downtime_watch.detect_gap(1000.0, 1000.0 + 30 + 400, expected_interval=30)


def test_build_message_kinds():
    frozen = downtime_watch.build_message("frozen", 0, 6 * 3600)
    down = downtime_watch.build_message("down", 0, 6 * 3600)
    assert "сон" in frozen and "6 г" in frozen
    assert "зупинений" in down and "6 г" in down


def test_heartbeat_roundtrip_and_missing_file(tmp_path):
    path = str(tmp_path / "heartbeat.json")
    assert downtime_watch._read_heartbeat(path) is None
    downtime_watch._write_heartbeat(path, 123.5)
    assert downtime_watch._read_heartbeat(path) == 123.5


def test_run_reports_down_when_stale_heartbeat_on_startup(tmp_path, monkeypatch):
    path = str(tmp_path / "heartbeat.json")
    downtime_watch._write_heartbeat(path, time.time() - 3 * 3600)
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    async def scenario():
        task = asyncio.create_task(downtime_watch.run(FakeBot(), 42, path))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(scenario())
    assert len(sent) == 1
    assert sent[0][0] == 42
    assert "зупинений" in sent[0][1]


def test_run_silent_on_fresh_heartbeat(tmp_path):
    path = str(tmp_path / "heartbeat.json")
    downtime_watch._write_heartbeat(path, time.time() - 5)
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            sent.append(text)

    async def scenario():
        task = asyncio.create_task(downtime_watch.run(FakeBot(), 42, path))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(scenario())
    assert sent == []


def test_run_reports_frozen_when_loop_wakes_late(tmp_path, monkeypatch):
    path = str(tmp_path / "heartbeat.json")
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            sent.append(text)

    real_time = time.time
    offset = {"v": 0.0}
    monkeypatch.setattr(downtime_watch.time, "time", lambda: real_time() + offset["v"])
    monkeypatch.setattr(downtime_watch, "HEARTBEAT_INTERVAL_SECONDS", 0.05)

    async def scenario():
        task = asyncio.create_task(downtime_watch.run(FakeBot(), 42, path))
        await asyncio.sleep(0.02)
        offset["v"] = 6 * 3600  # simulate the clock jumping across a sleep
        await asyncio.sleep(0.15)
        task.cancel()

    asyncio.run(scenario())
    assert len(sent) == 1
    assert "сон" in sent[0]
