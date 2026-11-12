"""The deploy webhook must not restart into a checkout that cannot start (audit R1)."""

import hashlib
import hmac
import json
import subprocess

import pytest
from aiohttp.test_utils import TestClient, TestServer

import deploy_checks
import webapp_server as ws


@pytest.fixture(autouse=True)
def _no_import_smoke(monkeypatch):
    monkeypatch.setattr(deploy_checks, "IMPORT_SMOKE_CODE", "pass")   # the temp repos below are not the project


async def test_broken_python_and_json_are_reported(tmp_path):
    (tmp_path / "good.py").write_text("x = 1\n")
    (tmp_path / "bad.py").write_text("def broken(:\n")
    (tmp_path / "bad.json").write_text("{not json")
    (tmp_path / "ok.json").write_text('{"a": 1}')
    problems = await deploy_checks.validate(str(tmp_path), ["good.py", "bad.py", "bad.json", "ok.json", "deleted.py"])
    assert len(problems) == 2
    assert any(p.startswith("bad.py:") for p in problems) and any(p.startswith("bad.json: invalid JSON") for p in problems)
    assert await deploy_checks.validate(str(tmp_path), ["good.py", "ok.json"]) == []


async def test_a_festival_beer_list_without_ids_is_refused(tmp_path):
    (tmp_path / "festivals.json").write_text(json.dumps({"festivals": [{"key": "f", "file": "beers.json"}]}))
    (tmp_path / "beers.json").write_text(json.dumps({"yellow": [{"id": "1", "name": "A"}, {"name": "no id"}]}))
    problems = await deploy_checks.validate(str(tmp_path), ["beers.json"])
    assert problems == ["beers.json: entry 1 has no id or name"]
    (tmp_path / "beers.json").write_text(json.dumps({"yellow": [{"id": "1", "name": "A"}], "red": []}))
    assert await deploy_checks.validate(str(tmp_path), ["beers.json"]) == []
    assert deploy_checks.check_beer_file("x", "not a list") is not None


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


def _head(cwd):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


async def test_the_webhook_rolls_back_a_broken_push_and_deploys_the_fix(tmp_path, monkeypatch):
    origin, work, live = tmp_path / "origin.git", tmp_path / "work", tmp_path / "live"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    (work / "app.py").write_text("x = 1\n")
    _git(work, "add", "."); _git(work, "commit", "-qm", "init"); _git(work, "push", "-q", "origin", "HEAD:master")
    subprocess.run(["git", "clone", "-q", str(origin), str(live)], check=True, capture_output=True)
    good = _head(live)

    told = []

    async def fake_notify(previous, problems):
        told.append((previous, problems))

    monkeypatch.setattr(ws, "DEPLOY_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(ws, "_data_dir", str(live))
    monkeypatch.setattr(ws, "_notify_deploy_rejected", fake_notify)
    monkeypatch.setattr(ws, "_ptb_app", None)
    monkeypatch.setattr(ws, "AUTO_TOAST_OWNER_ID", "1")

    body = json.dumps({"ref": "refs/heads/master"}).encode()
    headers = {"X-Hub-Signature-256": "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest(), "Content-Type": "application/json"}
    app = ws._build_app()
    async with TestClient(TestServer(app)) as c:
        (work / "app.py").write_text("def broken(:\n")
        _git(work, "commit", "-qam", "bad"); _git(work, "push", "-q", "origin", "HEAD:master")
        r = await c.post("/api/deploy/webhook", data=body, headers=headers)
        assert r.status == 422 and (await r.json())["error"] == "deploy_checks_failed"
        assert _head(live) == good and (live / "app.py").read_text() == "x = 1\n"      # rolled back
        assert told and told[0][0] == good[:7] and told[0][1][0].startswith("app.py:")

        (work / "app.py").write_text("x = 2\n")
        _git(work, "commit", "-qam", "fix"); _git(work, "push", "-q", "origin", "HEAD:master")
        r = await c.post("/api/deploy/webhook", data=body, headers=headers)
        assert r.status == 200 and (await r.json())["deployed"] is True
        assert (live / "app.py").read_text() == "x = 2\n" and _head(live) != good       # the fix passes through the earlier bad commit
