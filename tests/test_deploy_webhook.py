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
