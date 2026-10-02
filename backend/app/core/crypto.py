"""Symmetric encryption for secrets at rest (Shopify tokens, carrier credentials).

Uses Fernet (AES-128-CBC + HMAC-SHA256) via MultiFernet so keys can be rotated: put the new key
first in ENCRYPTION_KEYS, keep old keys after it, and re-encrypt with `rotate()`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings
from app.core.errors import ConfigurationError


class Encryptor:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ConfigurationError(
                "ENCRYPTION_KEYS is not set. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(bytes(token)).decode()
        except InvalidToken as exc:
            raise ConfigurationError("Unable to decrypt secret: wrong ENCRYPTION_KEYS?") from exc

    def encrypt_json(self, data: dict[str, Any]) -> bytes:
        return self.encrypt(json.dumps(data, separators=(",", ":"), sort_keys=True))

    def decrypt_json(self, token: bytes) -> dict[str, Any]:
        value = json.loads(self.decrypt(token))
        if not isinstance(value, dict):
            raise ConfigurationError("Encrypted payload is not a JSON object")
        return value

    def rotate(self, token: bytes) -> bytes:
        return self._fernet.rotate(bytes(token))


@lru_cache
def get_encryptor() -> Encryptor:
    return Encryptor(get_settings().fernet_keys)


def mask_secret(value: str | None) -> str | None:
    """Display form of a secret: never reveals more than the last 4 characters."""
    if not value:
        return None
    if len(value) <= 8:
        return "••••"
    return f"••••{value[-4:]}"
