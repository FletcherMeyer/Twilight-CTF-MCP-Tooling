"""Encryption at rest and credential hashing.

* Sensitive columns (flags, descriptions, uploaded files, agent.md) are encrypted
  with Fernet (AES-128-CBC + HMAC-SHA256). MultiFernet allows key rotation.
* Passwords and join codes are hashed with Argon2id.
* Session cookies and API tokens are 256-bit random values; only their
  SHA-256 digests are stored.
"""
from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .config import settings

_ph = PasswordHasher()  # argon2id with library-recommended parameters


class DataBox:
    def __init__(self, keys: tuple[str, ...]):
        if not keys:
            raise RuntimeError(
                "CTFSHEET_DATA_KEYS is not set. Generate one with: "
                "python -m ctfsheet_server.manage gen-key"
            )
        self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])

    def enc(self, value: bytes | str | None) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.encode("utf-8")
        return self._fernet.encrypt(value)

    def dec(self, token: bytes | None) -> bytes | None:
        if token is None:
            return None
        try:
            return self._fernet.decrypt(token)
        except InvalidToken as exc:  # wrong key or tampered row
            raise RuntimeError("Unable to decrypt stored data (wrong CTFSHEET_DATA_KEYS?)") from exc

    def dec_str(self, token: bytes | None) -> str | None:
        raw = self.dec(token)
        return raw.decode("utf-8") if raw is not None else None

    def rotate(self, token: bytes | None) -> bytes | None:
        return None if token is None else self._fernet.rotate(token)


_box: DataBox | None = None


def box() -> DataBox:
    global _box
    if _box is None:
        _box = DataBox(settings.data_keys)
    return _box


def hash_secret(secret: str) -> str:
    return _ph.hash(secret)


def verify_secret(stored_hash: str, secret: str) -> bool:
    try:
        return _ph.verify(stored_hash, secret)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# A pre-computed hash so failed username lookups take as long as real ones.
_DUMMY_HASH = _ph.hash("not-a-real-password-" + secrets.token_hex(8))


def burn_time() -> None:
    verify_secret(_DUMMY_HASH, "x")


def new_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_join_code() -> str:
    # Human-shareable: 4 groups of 5 chars from an unambiguous alphabet (~100 bits).
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(20))
    return "-".join(raw[i : i + 5] for i in range(0, 20, 5))
