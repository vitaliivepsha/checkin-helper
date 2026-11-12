"""Checks run on a freshly pulled checkout BEFORE the bot restarts into it (audit R1).

The deploy webhook used to `git pull` and restart straight away. A broken file in `master` - a syntax error in a
`.py`, a half-written beer list, a new import without its package - then crashed the bot at start-up in a restart
loop, and since the webhook lives in the same process, the fixing push could not even be deployed. Now the pulled
files are validated first; on any failure the webhook rolls the checkout back to the previous commit and tells the
owner, and the running process is left alone."""

import asyncio
import json
import os
import sys

IMPORT_SMOKE_SECONDS = 120
IMPORT_SMOKE_CODE = "import bot, webapp_server"   # what must import cleanly in the new checkout
PIP_SECONDS = 600


async def _run(*args: str, cwd: str, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode, out.decode(errors="replace")


def _festival_beer_files(cwd: str) -> set[str]:
    try:
        with open(os.path.join(cwd, "festivals.json"), encoding="utf-8") as f:
            return {fest["file"] for fest in json.load(f).get("festivals", []) if isinstance(fest, dict) and fest.get("file")}
    except (OSError, ValueError):
        return set()


def check_beer_file(name: str, data) -> str | None:
    """A festival beer list is {session: [beer, ...]} or a flat [beer, ...]; every beer needs an id and a name
    (bot.load_db indexes beer["id"]; a record without it crashed the import)."""
    groups = list(data.values()) if isinstance(data, dict) else [data] if isinstance(data, list) else None
    if groups is None or not all(isinstance(g, list) for g in groups):
        return f"{name}: not a beer list"
    for beers in groups:
        for index, beer in enumerate(beers):
            if not isinstance(beer, dict) or beer.get("id") in (None, "") or not beer.get("name"):
                return f"{name}: entry {index} has no id or name"
    return None


async def validate(cwd: str, changed: list[str]) -> list[str]:
    """The problems found in the checkout at `cwd`, given the files the pull changed (empty list = safe to restart)."""
    errors: list[str] = []
    beer_files = _festival_beer_files(cwd)
    for name in changed:
        path = os.path.join(cwd, name)
        if not os.path.exists(path):
            continue   # a deleted file
        if name.endswith(".py"):
            code, out = await _run(sys.executable, "-m", "py_compile", path, cwd=cwd, timeout=60)
            if code != 0:
                errors.append(f"{name}: {out.strip().splitlines()[-1] if out.strip() else 'does not compile'}")
        elif name.endswith(".json"):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                errors.append(f"{name}: invalid JSON ({e})")
                continue
            if name in beer_files:
                problem = check_beer_file(name, data)
                if problem:
                    errors.append(problem)
    if errors:
        return errors   # no point installing packages or importing a tree that is already known to be broken

    if "requirements.txt" in changed:
        code, out = await _run(sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt", cwd=cwd, timeout=PIP_SECONDS)
        if code != 0:
            return [f"requirements.txt: pip install failed: {out.strip()[-300:]}"]

    if any(n.endswith((".py", ".txt")) for n in changed):
        code, out = await _run(sys.executable, "-c", IMPORT_SMOKE_CODE, cwd=cwd, timeout=IMPORT_SMOKE_SECONDS)
        if code != 0:
            return [f"import check failed: {out.strip().splitlines()[-1] if out.strip() else 'no output'}"]
    return []
