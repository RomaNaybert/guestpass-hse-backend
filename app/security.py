"""Separate, domain-scoped HMACs for passes, sessions and Telegram auth."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl
from uuid import UUID


class InvalidToken(ValueError):
    pass


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise InvalidToken("Некорректный код")
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except ValueError as exc:
        raise InvalidToken("Некорректный код") from exc
    if b64(raw) != text:
        raise InvalidToken("Неканонический код")
    return raw


def mac(key: str, purpose: bytes, body: bytes) -> bytes:
    return hmac.digest(key.encode(), purpose + b"\0" + body, "sha256")


@dataclass(frozen=True)
class PassClaims:
    event_id: UUID
    guest_id: UUID
    nonce: UUID
    expires: int


def issue_pass(claims: PassClaims, key: str) -> str:
    payload = b"\x01" + claims.event_id.bytes + claims.guest_id.bytes + claims.nonce.bytes + struct.pack("!Q", claims.expires)
    return "GP1." + b64(payload + mac(key, b"pass", payload))


def verify_pass(token: str, key: str, now: int | None = None) -> PassClaims:
    if not isinstance(token, str) or len(token) != 123 or not token.startswith("GP1."):
        raise InvalidToken("Некорректный пропуск")
    raw = unb64(token[4:])
    if len(raw) != 89:
        raise InvalidToken("Некорректный пропуск")
    payload, signature = raw[:-32], raw[-32:]
    if not hmac.compare_digest(signature, mac(key, b"pass", payload)) or payload[0] != 1:
        raise InvalidToken("Подпись пропуска не прошла проверку")
    expires = struct.unpack("!Q", payload[49:57])[0]
    if expires <= (int(time.time()) if now is None else now):
        raise InvalidToken("Срок действия пропуска истёк")
    return PassClaims(UUID(bytes=payload[1:17]), UUID(bytes=payload[17:33]), UUID(bytes=payload[33:49]), expires)


def telegram_user(init_data: str, bot_token: str, now: int | None = None) -> int:
    if not bot_token or len(init_data) > 10000:
        raise InvalidToken("Откройте панель через Telegram-бота")
    pairs = parse_qsl(init_data, keep_blank_values=True)
    if len(dict(pairs)) != len(pairs):
        raise InvalidToken("Повторяющиеся поля Telegram")
    fields = dict(pairs)
    supplied = fields.pop("hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise InvalidToken("Некорректная подпись Telegram")
    # Telegram's bot-token validation includes all fields other than hash.
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise InvalidToken("Не удалось проверить вход через Telegram")
    try:
        timestamp = int(fields["auth_date"])
        current = int(time.time()) if now is None else now
        if timestamp > current + 30 or current - timestamp > 300:
            raise InvalidToken("Вход устарел. Откройте панель заново")
        user_id = json.loads(fields["user"])["id"]
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise ValueError("Invalid user ID")
        return user_id
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidToken("Некорректные данные Telegram") from exc


def issue_session(actor_id: int, key: str) -> str:
    body = f"{actor_id}:{int(time.time()) + 3600}:{secrets.token_urlsafe(16)}".encode()
    return b64(body) + "." + b64(mac(key, b"session", body))


def verify_session(token: str, key: str) -> int:
    try:
        body_text, signature = token.split(".")
        body = unb64(body_text)
        if not hmac.compare_digest(unb64(signature), mac(key, b"session", body)):
            raise InvalidToken("Сессия недействительна")
        actor, expires, _ = body.decode().split(":")
        if int(expires) <= int(time.time()):
            raise InvalidToken("Сессия истекла")
        return int(actor)
    except (ValueError, UnicodeError) as exc:
        raise InvalidToken("Войдите через Telegram-бота") from exc
