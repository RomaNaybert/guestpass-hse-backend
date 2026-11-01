import hashlib
import hmac
import json
import time
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from app.security import InvalidToken, PassClaims, b64, issue_pass, issue_session, telegram_user, unb64, verify_pass, verify_session

KEY = "unit-test-key-with-at-least-32-characters"


def claims(expires=2000):
    return PassClaims(uuid4(), uuid4(), uuid4(), expires)


def test_pass_roundtrip():
    expected = claims()
    token = issue_pass(expected, KEY)
    assert len(token) == 123
    assert verify_pass(token, KEY, now=1000) == expected


def test_changing_guest_id_invalidates_signature():
    token = issue_pass(claims(), KEY)
    raw = bytearray(unb64(token[4:]))
    raw[17] ^= 1
    with pytest.raises(InvalidToken):
        verify_pass("GP1." + b64(bytes(raw)), KEY, now=1000)


@pytest.mark.parametrize("token", ["", "123", "GP1.", "GP1." + "!" * 119, "GP1." + "A" * 120])
def test_malformed_pass(token):
    with pytest.raises(InvalidToken):
        verify_pass(token, KEY, now=1000)


def test_expiry_boundary():
    token = issue_pass(claims(1000), KEY)
    with pytest.raises(InvalidToken):
        verify_pass(token, KEY, now=1000)


def test_wrong_signing_key():
    with pytest.raises(InvalidToken):
        verify_pass(issue_pass(claims(), KEY), "other-key", now=1000)


def telegram_data(user_id=123, timestamp=1000, token="test-bot-token"):
    fields = {"auth_date": str(timestamp), "user": json.dumps({"id": user_id}), "query_id": "test-query"}
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
    fields["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_telegram_auth_verified():
    assert telegram_user(telegram_data(), "test-bot-token", now=1010) == 123


def test_cannot_forge_telegram_actor():
    with pytest.raises(InvalidToken):
        telegram_user(telegram_data().replace("123", "999"), "test-bot-token", now=1010)


@pytest.mark.parametrize("timestamp", [699, 1031])
def test_stale_or_future_telegram_auth(timestamp):
    with pytest.raises(InvalidToken):
        telegram_user(telegram_data(timestamp=timestamp), "test-bot-token", now=1000)


def test_duplicate_telegram_fields_rejected():
    with pytest.raises(InvalidToken):
        telegram_user(telegram_data() + "&auth_date=1000", "test-bot-token", now=1000)


def test_non_ascii_telegram_hash_is_rejected_cleanly():
    with pytest.raises(InvalidToken):
        telegram_user("hash=подпись&auth_date=1000&user=%7B%22id%22%3A123%7D", "test-bot-token", now=1000)


def test_session_roundtrip_and_tampering():
    session = issue_session(123, KEY)
    assert verify_session(session, KEY) == 123
    with pytest.raises(InvalidToken):
        verify_session(session, "different-key")


def test_expired_session(monkeypatch):
    session = issue_session(123, KEY)
    future = time.time() + 3601
    monkeypatch.setattr(time, "time", lambda: future)
    with pytest.raises(InvalidToken):
        verify_session(session, KEY)
