"""Unit tests for the per-domain circuit breaker (plan v0.4 §3 Stage 3.5).

Covers the :class:`CircuitBreaker` state machine (CLOSED / OPEN / HALF_OPEN),
its thresholds and timeouts, per-domain isolation, config validation, and the
integration contract with :mod:`careerops.adapters.http_fetcher` (the breaker
gate is injected as a structural :class:`BreakerGate`, never imported directly
to avoid an adapters<->orchestration cycle).
"""

from __future__ import annotations

import io
import urllib.error
from collections.abc import Iterator
from http.client import HTTPMessage

import pytest

from careerops.adapters import http_fetcher
from careerops.adapters.http_fetcher import (
    CircuitOpenError,
    configure_circuit_breaker,
    fetch,
)
from careerops.orchestration.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)


class _Clock:
    """Deterministic monotonic clock for time-travel without real sleeping."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Pure state-machine tests
# ---------------------------------------------------------------------------


class TestCircuitBreakerClosedState:
    def test_unknown_domain_starts_closed_and_allows(self) -> None:
        breaker = CircuitBreaker()
        assert breaker.state("api.example.com") is CircuitState.CLOSED
        assert breaker.allow("api.example.com") is True

    def test_failures_below_threshold_stay_closed(self) -> None:
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=5))
        for _ in range(4):
            breaker.record_failure("api.example.com")
        assert breaker.state("api.example.com") is CircuitState.CLOSED
        assert breaker.consecutive_failures("api.example.com") == 4
        assert breaker.allow("api.example.com") is True

    def test_success_resets_consecutive_failures(self) -> None:
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=5))
        for _ in range(4):
            breaker.record_failure("api.example.com")
        breaker.record_success("api.example.com")
        assert breaker.consecutive_failures("api.example.com") == 0
        assert breaker.state("api.example.com") is CircuitState.CLOSED


class TestCircuitBreakerTripping:
    def test_threshold_failure_opens_circuit(self) -> None:
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=5))
        for _ in range(5):
            breaker.record_failure("api.example.com")
        assert breaker.state("api.example.com") is CircuitState.OPEN

    def test_open_circuit_denies_requests_within_timeout(self) -> None:
        clock = _Clock()
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=5, recovery_timeout_seconds=30.0),
            clock=clock,
        )
        for _ in range(5):
            breaker.record_failure("api.example.com")
        assert breaker.state("api.example.com") is CircuitState.OPEN
        # Just before the recovery window elapses -> still denied.
        clock.advance(29.9)
        assert breaker.allow("api.example.com") is False
        assert breaker.state("api.example.com") is CircuitState.OPEN


class TestCircuitBreakerHalfOpen:
    def test_recovery_timeout_admits_exactly_one_probe(self) -> None:
        clock = _Clock()
        breaker = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout_seconds=30.0,
                half_open_max_requests=1,
            ),
            clock=clock,
        )
        for _ in range(5):
            breaker.record_failure("api.example.com")
        clock.advance(30.0)

        # First allow() consumes the single half-open probe slot.
        assert breaker.allow("api.example.com") is True
        assert breaker.state("api.example.com") is CircuitState.HALF_OPEN
        # A second concurrent probe is denied while the first is in flight.
        assert breaker.allow("api.example.com") is False

    def test_successful_probe_closes_circuit(self) -> None:
        clock = _Clock()
        breaker = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout_seconds=30.0,
                half_open_max_requests=1,
            ),
            clock=clock,
        )
        for _ in range(5):
            breaker.record_failure("api.example.com")
        clock.advance(30.0)
        assert breaker.allow("api.example.com") is True  # probe admitted

        breaker.record_success("api.example.com")
        assert breaker.state("api.example.com") is CircuitState.CLOSED
        assert breaker.consecutive_failures("api.example.com") == 0
        # Circuit is fully usable again.
        assert breaker.allow("api.example.com") is True

    def test_failed_probe_reopens_circuit(self) -> None:
        clock = _Clock()
        breaker = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout_seconds=30.0,
                half_open_max_requests=1,
            ),
            clock=clock,
        )
        for _ in range(5):
            breaker.record_failure("api.example.com")
        clock.advance(30.0)
        assert breaker.allow("api.example.com") is True  # probe admitted

        breaker.record_failure("api.example.com")
        assert breaker.state("api.example.com") is CircuitState.OPEN
        # Immediately re-opened: requests denied again until the next window.
        assert breaker.allow("api.example.com") is False

    def test_failed_probe_restarts_recovery_timer(self) -> None:
        clock = _Clock()
        breaker = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout_seconds=30.0,
                half_open_max_requests=1,
            ),
            clock=clock,
        )
        for _ in range(5):
            breaker.record_failure("api.example.com")
        clock.advance(30.0)
        assert breaker.allow("api.example.com") is True
        breaker.record_failure("api.example.com")  # probe fails, timer restarts

        # Less than a full window since the reopen -> still open.
        clock.advance(29.9)
        assert breaker.allow("api.example.com") is False
        # After a full window -> a new probe is admitted.
        clock.advance(0.1)
        assert breaker.allow("api.example.com") is True


class TestCircuitBreakerDomainIsolation:
    def test_domains_are_isolated(self) -> None:
        clock = _Clock()
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3), clock=clock)
        for _ in range(3):
            breaker.record_failure("a.example.com")

        assert breaker.state("a.example.com") is CircuitState.OPEN
        assert breaker.state("b.example.com") is CircuitState.CLOSED
        assert breaker.allow("b.example.com") is True
        # Opening one domain must not deny another.
        assert breaker.allow("a.example.com") is False


class TestCircuitBreakerReset:
    def test_reset_single_domain(self) -> None:
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2))
        for _ in range(2):
            breaker.record_failure("api.example.com")
        assert breaker.state("api.example.com") is CircuitState.OPEN
        breaker.reset("api.example.com")
        assert breaker.state("api.example.com") is CircuitState.CLOSED

    def test_reset_all_domains(self) -> None:
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2))
        for _ in range(2):
            breaker.record_failure("a.example.com")
        for _ in range(2):
            breaker.record_failure("b.example.com")
        breaker.reset()
        assert breaker.state("a.example.com") is CircuitState.CLOSED
        assert breaker.state("b.example.com") is CircuitState.CLOSED


class TestCircuitBreakerConfigValidation:
    @pytest.mark.parametrize(
        ("kwargs",),
        [
            ({"failure_threshold": 0},),
            ({"failure_threshold": -1},),
        ],
    )
    def test_invalid_failure_threshold_rejected(self, kwargs: dict[str, int]) -> None:
        with pytest.raises(ValueError, match="failure_threshold"):
            CircuitBreakerConfig(**kwargs)  # type: ignore[arg-type]

    def test_invalid_recovery_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match="recovery_timeout_seconds"):
            CircuitBreakerConfig(recovery_timeout_seconds=-1.0)

    def test_invalid_half_open_max_rejected(self) -> None:
        with pytest.raises(ValueError, match="half_open_max_requests"):
            CircuitBreakerConfig(half_open_max_requests=0)

    def test_defaults_match_plan(self) -> None:
        config = CircuitBreakerConfig()
        assert config.failure_threshold == 5
        assert config.recovery_timeout_seconds == 30.0
        assert config.half_open_max_requests == 1


# ---------------------------------------------------------------------------
# http_fetcher integration (BreakerGate contract)
# ---------------------------------------------------------------------------


class _SpyBreaker:
    """Recording BreakerGate double."""

    def __init__(self, *, allow: bool = True) -> None:
        self._allow = allow
        self.allowed: list[str] = []
        self.successes: list[str] = []
        self.failures: list[str] = []

    def allow(self, domain: str) -> bool:
        self.allowed.append(domain)
        return self._allow

    def record_success(self, domain: str) -> None:
        self.successes.append(domain)

    def record_failure(self, domain: str) -> None:
        self.failures.append(domain)


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        url: str = "http://example.com/x",
    ) -> None:
        self._body = body
        self.status = status
        self._url = url

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk, self._body = self._body, b""
            return chunk
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _make_http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://example.com/x",
        code,
        "error",
        HTTPMessage(),
        io.BytesIO(b"error body"),
    )


@pytest.fixture(autouse=True)
def _isolate_fetcher(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Disable real rate-limiting and reset the breaker gate around each test."""
    monkeypatch.setattr(http_fetcher, "_min_domain_interval", 0.0)
    monkeypatch.setattr(http_fetcher, "_last_fetch", {})
    yield
    configure_circuit_breaker(None)


def _stub_open(fn: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_fetcher, "_resolve_host", lambda _host: ("93.184.216.34",))
    monkeypatch.setattr(http_fetcher, "_open", fn)


class TestHttpFetcherCircuitWiring:
    def test_open_breaker_raises_circuit_open_error_without_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Network must never be touched when the breaker is open.
        touched = {"count": 0}

        def _should_not_open(_req: object, *, timeout: float = 0.0) -> object:
            touched["count"] += 1
            raise AssertionError("breaker should have denied before _open was called")

        _stub_open(_should_not_open, monkeypatch)
        spy = _SpyBreaker(allow=False)
        configure_circuit_breaker(spy)

        with pytest.raises(CircuitOpenError, match="circuit breaker open"):
            fetch("http://example.com/x")
        assert touched["count"] == 0
        assert spy.allowed == ["example.com"]

    def test_successful_fetch_records_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeResponse(b"ok", status=200)
        _stub_open(lambda _req, *, timeout=0.0: fake, monkeypatch)
        spy = _SpyBreaker(allow=True)
        configure_circuit_breaker(spy)

        result = fetch("http://example.com/x")
        assert result.body == "ok"
        assert spy.successes == ["example.com"]
        assert spy.failures == []

    def test_5xx_records_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _open(_req: object, *, timeout: float = 0.0) -> object:
            raise _make_http_error(503)

        _stub_open(_open, monkeypatch)
        spy = _SpyBreaker(allow=True)
        configure_circuit_breaker(spy)

        with pytest.raises(urllib.error.HTTPError):
            fetch("http://example.com/x")
        assert spy.failures == ["example.com"]
        assert spy.successes == []

    def test_4xx_records_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _open(_req: object, *, timeout: float = 0.0) -> object:
            raise _make_http_error(404)

        _stub_open(_open, monkeypatch)
        spy = _SpyBreaker(allow=True)
        configure_circuit_breaker(spy)

        with pytest.raises(urllib.error.HTTPError):
            fetch("http://example.com/x")
        # 4xx means the server responded (healthy) -> counts as success.
        assert spy.successes == ["example.com"]
        assert spy.failures == []

    def test_urlerror_records_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _open(_req: object, *, timeout: float = 0.0) -> object:
            raise urllib.error.URLError("connection refused")

        _stub_open(_open, monkeypatch)
        spy = _SpyBreaker(allow=True)
        configure_circuit_breaker(spy)

        with pytest.raises(urllib.error.URLError):
            fetch("http://example.com/x")
        assert spy.failures == ["example.com"]

    def test_real_breaker_end_to_end_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Five consecutive 5xx responses trip a real breaker; the sixth fetch is
        # denied at the gate before any network call.
        calls = {"n": 0}

        def _open(_req: object, *, timeout: float = 0.0) -> object:
            calls["n"] += 1
            raise _make_http_error(500)

        _stub_open(_open, monkeypatch)
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=5))
        configure_circuit_breaker(breaker)

        for _ in range(5):
            with pytest.raises(urllib.error.HTTPError):
                fetch("http://example.com/x")
        assert breaker.state("example.com") is CircuitState.OPEN
        assert calls["n"] == 5

        # Sixth call is denied by the open breaker (no further network use).
        with pytest.raises(CircuitOpenError):
            fetch("http://example.com/x")
        assert calls["n"] == 5
