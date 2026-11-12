import webapp_server


class _Req:
    def __init__(self, headers):
        self.headers = headers


def test_lens_token_check_accepts_only_the_exact_token(monkeypatch):
    monkeypatch.setattr(webapp_server, "LENS_API_TOKEN", "s3cret-token")
    assert webapp_server._lens_authorized(_Req({"X-Lens-Token": "s3cret-token"}))
    for header in ({}, {"X-Lens-Token": ""}, {"X-Lens-Token": "s3cret-tokeN"}, {"X-Lens-Token": "é"}, {"X-Lens-Token": "s3cret-token "}):
        assert not webapp_server._lens_authorized(_Req(header)), header   # non-ASCII is a 401, not a TypeError


def test_lens_is_closed_when_no_token_is_configured(monkeypatch):
    monkeypatch.setattr(webapp_server, "LENS_API_TOKEN", "")
    assert not webapp_server._lens_authorized(_Req({"X-Lens-Token": ""}))
    assert not webapp_server._lens_authorized(_Req({}))


def test_constant_time_equal_handles_non_ascii():
    assert webapp_server._constant_time_equal("sha256=é", "sha256=é")
    assert not webapp_server._constant_time_equal("sha256=é", "sha256=abc")
