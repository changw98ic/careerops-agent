from __future__ import annotations

import hashlib
import hmac
import secrets
from types import MappingProxyType

from argon2 import PasswordHasher as Argon2LibraryHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from careerops.auth.contracts import PasswordRecord

TOKEN_BYTES = 32
TOKEN_HASH_LENGTH = 64
HASH_SCHEME = "argon2id"


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    if not token or len(token) > 512:
        raise ValueError("token must be non-empty and bounded")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_token_match(token: str, expected_hash: str) -> bool:
    try:
        actual_hash = hash_token(token)
    except ValueError:
        actual_hash = "0" * TOKEN_HASH_LENGTH
    if len(expected_hash) != TOKEN_HASH_LENGTH:
        expected_hash = "f" * TOKEN_HASH_LENGTH
    return hmac.compare_digest(actual_hash, expected_hash)


def hash_subject(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


class Argon2idPasswordHasher:
    """Bounded Argon2id password hashing with parameters stored beside the hash."""

    def __init__(
        self,
        *,
        time_cost: int = 3,
        memory_cost: int = 65_536,
        parallelism: int = 4,
        hash_len: int = 32,
        salt_len: int = 16,
    ) -> None:
        self._parameters = MappingProxyType(
            {
                "type": HASH_SCHEME,
                "version": 19,
                "time_cost": time_cost,
                "memory_cost": memory_cost,
                "parallelism": parallelism,
                "hash_len": hash_len,
                "salt_len": salt_len,
            }
        )
        self._hasher = Argon2LibraryHasher(
            time_cost=time_cost,
            memory_cost=memory_cost,
            parallelism=parallelism,
            hash_len=hash_len,
            salt_len=salt_len,
            type=Type.ID,
        )

    def hash_password(self, password: str) -> PasswordRecord:
        self._validate_password(password)
        return PasswordRecord(
            encoded_hash=self._hasher.hash(password),
            parameters=self._parameters,
        )

    def verify_password(self, password: str, stored: PasswordRecord) -> bool:
        try:
            self._validate_password(password)
            return bool(self._hasher.verify(stored.encoded_hash, password))
        except (InvalidHashError, VerificationError, VerifyMismatchError, ValueError):
            return False

    def needs_rehash(self, stored: PasswordRecord) -> bool:
        try:
            return bool(self._hasher.check_needs_rehash(stored.encoded_hash))
        except (InvalidHashError, VerificationError):
            return True

    @staticmethod
    def _validate_password(password: str) -> None:
        encoded_length = len(password.encode("utf-8"))
        if encoded_length < 12:
            raise ValueError("password must contain at least 12 UTF-8 bytes")
        if encoded_length > 1024:
            raise ValueError("password must not exceed 1024 UTF-8 bytes")


__all__ = [
    "HASH_SCHEME",
    "Argon2idPasswordHasher",
    "constant_time_token_match",
    "generate_token",
    "hash_subject",
    "hash_token",
]
