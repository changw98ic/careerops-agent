"""Per-domain circuit breaker state machine (plan v0.4 §3 Stage 3.5).

A pure policy object: it answers ``allow(domain)`` and records success/failure
outcomes. It never performs I/O and raises nothing itself; the integration layer
(http_fetcher / crawl layer) decides what to do when ``allow`` returns False.

State machine (config defaults mirror the plan):

- ``CLOSED``: requests pass. After ``failure_threshold`` consecutive failures
  the breaker transitions to ``OPEN``.
- ``OPEN``: requests are denied. After ``recovery_timeout_seconds`` elapse, the
  next ``allow`` transitions the breaker to ``HALF_OPEN`` and admits exactly one
  probe request.
- ``HALF_OPEN``: at most ``half_open_max_requests`` probe(s) may be in flight.
  A successful probe closes the breaker (failures reset to 0); a failed probe
  re-opens it and restarts the recovery timer.

The clock is injectable so tests drive time without real sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    """Tunables for :class:`CircuitBreaker` (plan v0.4 Stage 3.5 defaults)."""

    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    half_open_max_requests: int = 1

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.recovery_timeout_seconds < 0.0:
            raise ValueError("recovery_timeout_seconds must be >= 0")
        if self.half_open_max_requests < 1:
            raise ValueError("half_open_max_requests must be >= 1")


@dataclass(slots=True)
class _DomainState:
    """Mutable per-domain bookkeeping. Not part of the public API."""

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    half_open_in_flight: int = 0


class CircuitBreaker:
    """Per-domain circuit breaker.

    ``domain`` is an opaque string key (typically a hostname). Unknown domains
    start ``CLOSED`` and are tracked lazily on first contact.
    """

    def __init__(
        self,
        config: CircuitBreakerConfig | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config or CircuitBreakerConfig()
        self._clock = clock or time.monotonic
        self._domains: dict[str, _DomainState] = {}

    @property
    def config(self) -> CircuitBreakerConfig:
        return self._config

    def state(self, domain: str) -> CircuitState:
        """Return the current state for ``domain`` (defaults to CLOSED)."""
        entry = self._domains.get(domain)
        if entry is None:
            return CircuitState.CLOSED
        return entry.state

    def consecutive_failures(self, domain: str) -> int:
        """Return the current consecutive-failure count for ``domain``."""
        entry = self._domains.get(domain)
        return entry.consecutive_failures if entry is not None else 0

    def allow(self, domain: str) -> bool:
        """Gate a request. Returns True if the request may proceed."""
        entry = self._domains.setdefault(domain, _DomainState())
        now = self._clock()
        if entry.state is CircuitState.CLOSED:
            return True
        if entry.state is CircuitState.OPEN:
            if now - entry.opened_at >= self._config.recovery_timeout_seconds:
                # Recovery elapsed: admit a single probe in HALF_OPEN.
                entry.state = CircuitState.HALF_OPEN
                entry.half_open_in_flight = 1
                return True
            return False
        # HALF_OPEN: admit only up to the configured probe budget.
        if entry.half_open_in_flight < self._config.half_open_max_requests:
            entry.half_open_in_flight += 1
            return True
        return False

    def record_success(self, domain: str) -> None:
        """Record a successful request: reset failures and close the circuit."""
        entry = self._domains.setdefault(domain, _DomainState())
        entry.consecutive_failures = 0
        entry.half_open_in_flight = 0
        entry.state = CircuitState.CLOSED

    def record_failure(self, domain: str) -> None:
        """Record a failed request; may trip or re-open the circuit."""
        entry = self._domains.setdefault(domain, _DomainState())
        if entry.state is CircuitState.HALF_OPEN:
            # A failed probe re-opens the circuit and restarts the timer.
            entry.state = CircuitState.OPEN
            entry.opened_at = self._clock()
            entry.half_open_in_flight = 0
            return
        entry.consecutive_failures += 1
        if entry.consecutive_failures >= self._config.failure_threshold:
            entry.state = CircuitState.OPEN
            entry.opened_at = self._clock()

    def reset(self, domain: str | None = None) -> None:
        """Clear state for ``domain`` (or all domains when ``None``)."""
        if domain is None:
            self._domains.clear()
        else:
            self._domains.pop(domain, None)
