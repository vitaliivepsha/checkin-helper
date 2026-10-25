"""Tests for webapp_server.py's handle_deploy_webhook - the GitHub push
webhook that triggers `git pull` + restart on the VM. Only covers the
auth/routing logic (HMAC signature check, master-branch filter), not the
actual `git pull` + restart path, which would touch the real filesystem/
process - that's exercised manually instead (see the commit that added
this feature for the ad hoc verification session)."""

import hashlib
import hmac
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

import webapp_server


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(webapp_server, "DEPLOY_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(webapp_server, "_data_dir", ".")
    app = webapp_server._build_app()
    server = TestServer(app)
    c = TestClient(server)
    await c.start_server()
    yield c
    await c.close()


def _sign(body: bytes, secret: str = "test-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_rejects_missing_signature(client):
    body = json.dumps({"ref": "refs/heads/master"}).encode()
    resp = await client.post("/api/deploy/webhook", data=body, headers={"Content-Type": "application/json"})
    assert resp.status == 401
    assert (await resp.json())["error"] == "bad_signature"


@pytest.mark.asyncio
async def test_rejects_wrong_signature(client):
    body = json.dumps({"ref": "refs/heads/master"}).encode()
    resp = await client.post(
        "/api/deploy/webhook", data=body,
        headers={"X-Hub-Signature-256": _sign(body, "wrong-secret"), "Content-Type": "application/json"},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_ignores_non_master_ref(client):
    body = json.dumps({"ref": "refs/heads/some-feature-branch"}).encode()
    resp = await client.post(
        "/api/deploy/webhook", data=body,
        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data == {"ok": True, "deployed": False, "reason": "not master"}


@pytest.mark.asyncio
async def test_disabled_when_no_secret_configured(client, monkeypatch):
    monkeypatch.setattr(webapp_server, "DEPLOY_WEBHOOK_SECRET", "")
    body = json.dumps({"ref": "refs/heads/master"}).encode()
    resp = await client.post(
        "/api/deploy/webhook", data=body,
        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status == 501


@pytest.mark.asyncio
async def test_local_edits_of_tracked_files_are_stashed_so_the_pull_can_go_ahead(tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "special_badges.json").write_text("[1]")
    (tmp_path / "other.py").write_text("x = 1")
    git("add", ".")
    git("commit", "-qm", "init")
    (tmp_path / "special_badges.json").write_text("[1, 2]")    # edited on the VM
    (tmp_path / "runtime.json").write_text("{}")                 # untracked: left alone

    stashed = await webapp_server._stash_local_changes(str(tmp_path))
    assert stashed == ["special_badges.json"]
    assert (tmp_path / "special_badges.json").read_text() == "[1]"
    assert (tmp_path / "runtime.json").exists()
    assert "deploy-autostash" in subprocess.run(
        ["git", "stash", "list"], cwd=tmp_path, capture_output=True, text=True,
    ).stdout
    assert await webapp_server._stash_local_changes(str(tmp_path)) == []   # clean tree: nothing to do
