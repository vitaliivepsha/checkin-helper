"""Shared read/write helpers for the JSON files the stores keep under DATA_DIR (audit R3).

Every store used to do `except (JSONDecodeError, OSError): return {}`. A half-written or hand-edited file, or one
read error, then looked like an empty store, and the next save replaced the file with the near-empty result: all
tokens, the 8 MB "had it" index or every festival map gone in one write. Now:

* a file that exists but is not valid JSON (or has the wrong top-level type) is renamed to `<name>.corrupt-<ts>`
  for inspection, the error is logged loudly, and the last good copy `<name>.bak` is used if there is one;
* a read error that is not "the file is damaged" (EIO, permissions) raises `StoreUnreadable` instead of pretending
  the store is empty, so nothing overwrites a file that was never read;
* every save first flushes the new file to disk and keeps the previous version as `<name>.bak` (a hard link, so
  it costs no copy)."""

import json
import logging
import os
import shutil
import time

logger = logging.getLogger(__name__)

quarantined: list[str] = []   # paths moved aside since start-up, for whoever wants to tell the owner


class StoreUnreadable(OSError):
    """The store file exists but could not be read; the caller must not treat it as empty."""


def _read(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _quarantine(path: str, reason: str) -> None:
    target = f"{path}.corrupt-{int(time.time())}"
    try:
        os.replace(path, target)
    except OSError:
        logger.exception("store %s is damaged (%s) and could not be moved aside", path, reason)
        return
    quarantined.append(target)
    logger.error("store %s is damaged (%s); moved to %s", path, reason, target)


def load_json(path: str | None, default, expect: type | None = None):
    """The parsed content of `path`, `default` when the file does not exist, the `.bak` copy (or `default`) when
    it is damaged. `expect` is the required top-level type (defaults to the type of `default`)."""
    if not path or not os.path.exists(path):
        return default
    expect = expect or type(default)
    try:
        data = _read(path)
    except FileNotFoundError:
        return default
    except ValueError as e:   # JSONDecodeError and UnicodeDecodeError
        _quarantine(path, str(e))
    except OSError as e:
        logger.error("store %s could not be read: %s", path, e)
        raise StoreUnreadable(f"{path}: {e}") from e
    else:
        if isinstance(data, expect):
            return data
        _quarantine(path, f"top level is {type(data).__name__}, expected {expect.__name__}")

    backup = path + ".bak"
    try:
        data = _read(backup)
    except (OSError, ValueError):
        return default
    if not isinstance(data, expect):
        return default
    try:
        shutil.copyfile(backup, path)
        logger.error("store %s restored from %s", path, backup)
    except OSError:
        pass
    return data


def replace_file(tmp_path: str, path: str) -> None:
    """Moves a fully written temp file over `path`, keeping the file it replaces as `path + '.bak'`."""
    try:
        with open(tmp_path, "r+b") as f:
            os.fsync(f.fileno())
    except OSError:
        pass
    backup = path + ".bak"
    if os.path.exists(path):
        try:
            if os.path.exists(backup):
                os.remove(backup)
            os.link(path, backup)
        except OSError:
            pass   # no hard links here: the save still goes ahead, just without a .bak
    os.replace(tmp_path, path)
