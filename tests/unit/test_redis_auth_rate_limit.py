from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

import pytest
from redis.exceptions import RedisError

from careerops.infrastructure.rate_limit import RateLimitAction
from careerops.infrastructure.redis import RedisAuthRateLimiter
from careerops.infrastructure.redis.auth_rate_limit import RedisEvalClient

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


class FakeRedisBackend:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.last_key = ""

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        assert script and numkeys == 1
        key = str(keys_and_args[0])
        self.last_key = key
        maximum = int(str(keys_and_args[1]))
        self.counts[key] = self.counts.get(key, 0) + 1
        return 1 if self.counts[key] <= maximum else 0


class FailingRedisBackend:
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        assert script and numkeys == 1 and keys_and_args
        raise RedisError("backend unavailable")


class MalformedRedisBackend:
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        assert script and numkeys == 1 and keys_and_args
        return b"1"


class RedisBackendFactory(Protocol):
    def __call__(self) -> RedisEvalClient: ...


def test_redis_rate_limit_is_shared_between_instances_until_limit_exceeded() -> None:
    backend = FakeRedisBackend()
    first = RedisAuthRateLimiter(backend)
    second = RedisAuthRateLimiter(backend)
    subject = "a" * 64

    assert all(first.check(RateLimitAction.SMART_INTAKE, subject, now=NOW) for _ in range(10))
    assert second.check(RateLimitAction.SMART_INTAKE, subject, now=NOW) is False


@pytest.mark.parametrize("backend_factory", [FailingRedisBackend, MalformedRedisBackend])
def test_redis_rate_limit_fails_closed_on_backend_or_protocol_errors(
    backend_factory: RedisBackendFactory,
) -> None:
    limiter = RedisAuthRateLimiter(backend_factory())

    assert limiter.check(RateLimitAction.SMART_INTAKE, "a" * 64, now=NOW) is False


def test_redis_rate_limit_key_contains_only_action_and_one_way_subject_hash() -> None:
    backend = FakeRedisBackend()
    limiter = RedisAuthRateLimiter(backend)
    subject_hash = "f" * 64

    assert limiter.check(RateLimitAction.SMART_INTAKE, subject_hash, now=NOW) is True

    assert "smart_intake" in backend.last_key
    assert subject_hash in backend.last_key
    assert "127.0.0.1" not in backend.last_key
    assert "Mozilla" not in backend.last_key
    assert "\0" not in backend.last_key


def test_redis_rate_limit_validates_time_and_subject_before_backend_use() -> None:
    backend = FakeRedisBackend()
    limiter = RedisAuthRateLimiter(backend)

    with pytest.raises(ValueError, match="timezone-aware"):
        limiter.check(RateLimitAction.SMART_INTAKE, "a" * 64, now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="SHA-256"):
        limiter.check(RateLimitAction.SMART_INTAKE, "raw-client", now=NOW)
    assert backend.counts == {}

    later = NOW + timedelta(minutes=1)
    assert limiter.check(RateLimitAction.SMART_INTAKE, "a" * 64, now=later) is True
