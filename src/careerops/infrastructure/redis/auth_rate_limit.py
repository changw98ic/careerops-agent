from __future__ import annotations

from datetime import datetime
from typing import Protocol

from redis.exceptions import RedisError

from careerops.auth.contracts import AuthAction
from careerops.auth.rate_limit import limit_for

_LUA_FIXED_WINDOW = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
if count <= tonumber(ARGV[1]) then
  return 1
end
return 0
"""


class RedisEvalClient(Protocol):
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...


class RedisAuthRateLimiter:
    """Shared Redis fixed-window limiter.

    Keys contain only the auth action plus a caller-supplied one-way subject hash.
    Any Redis or protocol failure is treated as deny-by-default.
    """

    def __init__(
        self,
        redis: RedisEvalClient,
        *,
        key_prefix: str = "careerops:auth-rate:v1",
    ) -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    def check(self, action: AuthAction, subject_hash: str, *, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("rate-limit time must be timezone-aware")
        if len(subject_hash) != 64:
            raise ValueError("rate-limit subject must be a SHA-256 digest")
        maximum, window = limit_for(action)
        key = f"{self._key_prefix}:{action.value}:{subject_hash}"
        try:
            result = self._redis.eval(
                _LUA_FIXED_WINDOW,
                1,
                key,
                str(maximum),
                str(max(1, int(window.total_seconds()))),
            )
        except RedisError:
            return False
        except Exception:
            return False
        return result == 1


__all__ = ["RedisAuthRateLimiter"]
