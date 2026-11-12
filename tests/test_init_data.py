import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import webapp_server as ws

TOKEN = "123456:TEST-TOKEN"


def signed(fields: dict, token: str = TOKEN) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def fields(age=10, user_id=7):
    return {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": user_id, "first_name": "A"})}


def test_a_correctly_signed_init_data_is_accepted():
    parsed = ws.validate_init_data(signed(fields()), TOKEN)
    assert parsed["user"]["id"] == 7


def test_forged_expired_and_incomplete_init_data_are_refused():
    good = signed(fields())
    assert ws.validate_init_data(good, "999:OTHER") is None                      # signed with another bot's token
    edited = good.replace("%22id%22%3A+7", "%22id%22%3A+8")
    assert edited != good and ws.validate_init_data(edited, TOKEN) is None      # edited after signing
    assert ws.validate_init_data(signed(fields(age=90000)), TOKEN) is None       # older than a day
    no_hash = urlencode(fields())
    assert ws.validate_init_data(no_hash, TOKEN) is None
    assert ws.validate_init_data(signed({"user": fields()["user"]}), TOKEN) is None   # no auth_date
    assert ws.validate_init_data("", TOKEN) is None
    assert ws.validate_init_data(urlencode({**fields(), "hash": "é"}), TOKEN) is None   # non-ASCII hash: refused, not a TypeError
