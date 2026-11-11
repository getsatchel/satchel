"""Encryption of secrets stored in Postgres (LLM API keys, later connection credentials).

The scheme follows Langfuse's self-hosted ``ENCRYPTION_KEY``:

- ``SATCHEL_ENCRYPTION_KEY`` is 256 bits as 64 hex characters (``openssl rand -hex 32``). It
  lives only in the deployment's secret store (Secrets Manager / Secret Manager / a Kubernetes
  Secret), never in the database, so a database dump or backup holds only ciphertext.
- Values are sealed with AES-256-GCM, a fresh 96-bit nonce per value. The associated data binds
  each ciphertext to the row and column it belongs to, so a ciphertext copied into another row
  or workspace fails to decrypt instead of leaking there.
- A stored value is ``enc:v1:<key id>:<base64url(nonce || ciphertext || tag)>``. The key id is a
  short hash of the key, so rotation works: put the old key in
  ``SATCHEL_ENCRYPTION_KEY_PREVIOUS`` (comma-separated), deploy the new one, run
  ``satchel secrets rotate``, then drop the old key.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from satchel.settings import Settings

PREFIX = "enc:v1:"
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
GENERATE_HINT = "generate one with `openssl rand -hex 32` (or `satchel secrets generate-key`)"


class EncryptionUnavailable(RuntimeError):
    """No (valid) SATCHEL_ENCRYPTION_KEY: secrets cannot be stored or read."""


class DecryptionError(RuntimeError):
    """The value was sealed with a key that is not configured, or it was tampered with."""


def generate_key() -> str:
    return os.urandom(32).hex()


def _parse(raw: str, name: str) -> bytes:
    raw = raw.strip()
    if not _HEX64.match(raw):
        raise EncryptionUnavailable(
            f"{name} must be 256 bits as 64 hex characters; {GENERATE_HINT}"
        )
    return bytes.fromhex(raw)


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"satchel-kid\0" + key).hexdigest()[:12]


@dataclass(frozen=True)
class Keyring:
    current: bytes
    previous: tuple[bytes, ...] = ()

    @classmethod
    def from_settings(cls, st: Settings) -> Keyring:
        """The configured keys; raises :class:`EncryptionUnavailable` when there are none."""
        if st.encryption_key is None or not st.encryption_key.get_secret_value().strip():
            raise EncryptionUnavailable(
                f"SATCHEL_ENCRYPTION_KEY is not set, so secrets cannot be stored in the "
                f"database; {GENERATE_HINT}"
            )
        current = _parse(st.encryption_key.get_secret_value(), "SATCHEL_ENCRYPTION_KEY")
        prev: list[bytes] = []
        if st.encryption_key_previous is not None:
            for part in st.encryption_key_previous.get_secret_value().split(","):
                if part.strip():
                    prev.append(_parse(part, "SATCHEL_ENCRYPTION_KEY_PREVIOUS"))
        return cls(current, tuple(prev))

    @property
    def current_id(self) -> str:
        return key_id(self.current)

    def encrypt(self, plaintext: str, aad: str) -> str:
        nonce = os.urandom(12)
        sealed = AESGCM(self.current).encrypt(nonce, plaintext.encode(), aad.encode())
        blob = base64.urlsafe_b64encode(nonce + sealed).decode().rstrip("=")
        return f"{PREFIX}{self.current_id}:{blob}"

    def decrypt(self, token: str, aad: str) -> str:
        if not token.startswith(PREFIX):
            raise DecryptionError("not an encrypted value")
        kid, _, blob = token[len(PREFIX):].partition(":")
        key = next((k for k in (self.current, *self.previous) if key_id(k) == kid), None)
        if key is None:
            raise DecryptionError(
                f"sealed with key {kid}, which is neither SATCHEL_ENCRYPTION_KEY nor in "
                "SATCHEL_ENCRYPTION_KEY_PREVIOUS"
            )
        try:
            raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
            return AESGCM(key).decrypt(raw[:12], raw[12:], aad.encode()).decode()
        except (InvalidTag, ValueError) as e:
            raise DecryptionError("ciphertext does not match its row (tampered or moved)") from e

    def is_current(self, token: str) -> bool:
        return token.startswith(f"{PREFIX}{self.current_id}:")


def configured(st: Settings) -> bool:
    try:
        Keyring.from_settings(st)
    except EncryptionUnavailable:
        return False
    return True


def secret_hint(value: str) -> str:
    """What the UI shows for a stored secret: the last four characters (like Langfuse)."""
    return f"…{value[-4:]}" if len(value) >= 12 else "set"
