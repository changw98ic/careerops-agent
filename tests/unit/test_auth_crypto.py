from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from careerops.auth.contracts import AuthAction
from careerops.auth.crypto import (
    Argon2idPasswordHasher,
    constant_time_token_match,
    generate_token,
    hash_token,
)
from careerops.auth.rate_limit import FixedWindowAuthRateLimiter


def test_argon2id_hash_records_parameters_and_never_echoes_password() -> None:
    hasher = Argon2idPasswordHasher(memory_cost=8192, time_cost=1, parallelism=1)
    password = "correct horse battery staple"

    stored = hasher.hash_password(password)

    assert stored.encoded_hash.startswith("$argon2id$")
    assert stored.parameters["type"] == "argon2id"
    assert stored.parameters["memory_cost"] == 8192
    assert hasher.verify_password(password, stored) is True
    assert hasher.verify_password("wrong password value", stored) is False
    assert password not in repr(stored)


def test_tokens_are_random_hashed_and_compared_in_constant_time_helper() -> None:
    first = generate_token()
    second = generate_token()

    assert first != second
    assert first not in hash_token(first)
    assert constant_time_token_match(first, hash_token(first)) is True
    assert constant_time_token_match(second, hash_token(first)) is False
    assert constant_time_token_match("", "not-a-hash") is False


@pytest.mark.parametrize("password", ["short", "x" * 1025])
def test_argon2id_rejects_unbounded_passwords(password: str) -> None:
    with pytest.raises(ValueError):
        Argon2idPasswordHasher(memory_cost=8192, time_cost=1).hash_password(password)


def test_fixed_window_rate_limit_is_action_and_subject_scoped() -> None:
    limiter = FixedWindowAuthRateLimiter()
    now = datetime(2026, 7, 17, tzinfo=UTC)
    subject = "a" * 64

    assert all(limiter.check(AuthAction.LOGIN, subject, now=now) for _ in range(5))
    assert limiter.check(AuthAction.LOGIN, subject, now=now) is False
    assert limiter.check(AuthAction.LOGOUT, subject, now=now) is True
    assert limiter.check(AuthAction.LOGIN, subject, now=now + timedelta(minutes=5)) is True
