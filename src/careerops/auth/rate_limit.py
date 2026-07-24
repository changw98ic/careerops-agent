from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from threading import Lock

from careerops.auth.contracts import AuthAction

_LIMITS = {
    AuthAction.PREAUTH: (30, timedelta(minutes=1)),
    AuthAction.BOOTSTRAP: (5, timedelta(minutes=15)),
    AuthAction.LOGIN: (5, timedelta(minutes=5)),
    AuthAction.LOGOUT: (30, timedelta(minutes=1)),
    AuthAction.SESSION_ROTATE: (10, timedelta(minutes=5)),
    AuthAction.SESSION_REVOKE: (30, timedelta(minutes=1)),
    AuthAction.REVIEW: (30, timedelta(minutes=1)),
}


def limit_for(action: AuthAction) -> tuple[int, timedelta]:
    return _LIMITS[action]


class FixedWindowAuthRateLimiter:
    """Single-process M0 limiter; callers supply only a one-way subject hash."""

    def __init__(self) -> None:
        self._events: dict[tuple[AuthAction, str], deque[datetime]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, action: AuthAction, subject_hash: str, *, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("rate-limit time must be timezone-aware")
        if len(subject_hash) != 64:
            raise ValueError("rate-limit subject must be a SHA-256 digest")
        maximum, window = limit_for(action)
        cutoff = now - window
        key = (action, subject_hash)
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= maximum:
                return False
            events.append(now)
            return True


__all__ = ["FixedWindowAuthRateLimiter", "limit_for"]
