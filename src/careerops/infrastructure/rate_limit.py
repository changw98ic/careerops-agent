"""Rate-limit policy primitives shared by API routes and services.

The console-login auth subsystem (sessions, CSRF, passwords, bootstrap
tokens) was deleted with the login (auth-rm Task 11). What survives here is
the generic rate-limit kernel that smart intake and the review endpoint
still use: a fixed-window policy keyed by action plus a caller-supplied
one-way subject hash.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol


class RateLimitAction(StrEnum):
    REVIEW = "review"
    SMART_INTAKE = "smart_intake"


_LIMITS: dict[RateLimitAction, tuple[int, timedelta]] = {
    RateLimitAction.REVIEW: (30, timedelta(minutes=1)),
    RateLimitAction.SMART_INTAKE: (10, timedelta(minutes=10)),
}


def limit_for(action: RateLimitAction) -> tuple[int, timedelta]:
    return _LIMITS[action]


def hash_subject(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


class RateLimiter(Protocol):
    def check(self, action: RateLimitAction, subject_hash: str, *, now: datetime) -> bool: ...


__all__ = ["RateLimitAction", "RateLimiter", "hash_subject", "limit_for"]
