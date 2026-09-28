"""Tells the owner on Telegram when the bot was unreachable for a while.

Two cases, both detected from a wall-clock heartbeat (time.time(), not
monotonic - the point is to notice the machine sleeping or the process
being down, which a monotonic clock can hide):

- frozen: the process itself kept running but its own loop woke up far
  later than scheduled (laptop sleep, VM pause). Caught by the in-process
  loop comparing consecutive ticks.
- down: the process was not running at all (crash, reboot, PC off). Caught
  once at startup by comparing "now" with the last heartbeat left on disk.

A normal /restart (a few seconds) stays well under GAP_THRESHOLD_SECONDS
and is never reported.
"""

import asyncio
import datetime
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 30
GAP_THRESHOLD_SECONDS = 300
SEND_RETRIES = 6
SEND_RETRY_DELAY_SECONDS = 10

_task = None  # keeps the asyncio.create_task result alive (avoid GC)


def format_duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} г {minutes} хв"
    if hours:
        return f"{hours} г"
    return f"{minutes} хв"


def _clock(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def build_message(kind: str, start_ts: float, end_ts: float) -> str:
    duration = format_duration(end_ts - start_ts)
    span = f"з {_clock(start_ts)} по {_clock(end_ts)}"
    if kind == "frozen":
        return (
            f"💤 Бот не працював ~{duration} ({span}): процес був заморожений, "
            f"найімовірніше ПК/ноутбук ішов у сон. Зараз усе працює."
        )
    return (
        f"⚠️ Бот не працював ~{duration} ({span}): процес був зупинений "
        f"(перезавантаження, збій або вимкнений ПК). Зараз він запущений."
    )


def detect_gap(last_ts: float | None, now_ts: float, expected_interval: float = 0.0) -> bool:
    return last_ts is not None and (now_ts - last_ts) > expected_interval + GAP_THRESHOLD_SECONDS


def _read_heartbeat(path: str) -> float | None:
    try:
        with open(path, encoding="utf-8") as f:
            return float(json.load(f)["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_heartbeat(path: str, ts: float) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"ts": ts}, f)
    os.replace(tmp_path, path)


async def _notify(bot, owner_id: int, text: str) -> None:
    # Right after a wake-up the network (Wi-Fi) is often not back yet.
    for attempt in range(SEND_RETRIES):
        try:
            await bot.send_message(chat_id=owner_id, text=text)
            return
        except Exception:
            if attempt == SEND_RETRIES - 1:
                logger.warning("downtime_watch: could not notify owner", exc_info=True)
                return
            await asyncio.sleep(SEND_RETRY_DELAY_SECONDS)


async def run(bot, owner_id: int, heartbeat_path: str) -> None:
    now = time.time()
    previous = _read_heartbeat(heartbeat_path)
    _write_heartbeat(heartbeat_path, now)
    if detect_gap(previous, now):
        await _notify(bot, owner_id, build_message("down", previous, now))

    last = now
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        now = time.time()
        try:
            _write_heartbeat(heartbeat_path, now)
        except OSError:
            logger.warning("downtime_watch: could not write heartbeat", exc_info=True)
        if detect_gap(last, now, HEARTBEAT_INTERVAL_SECONDS):
            await _notify(bot, owner_id, build_message("frozen", last, now))
            now = time.time()
        last = now


def start(bot, owner_id: int, heartbeat_path: str) -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(run(bot, owner_id, heartbeat_path))
