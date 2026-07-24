"""Unit tests for ``careerops.adapters.http_fetcher``.

Covers the plan v0.4 §2.3 SSRF contract: private/loopback IP rejection,
redirect-target re-validation, non-http(s) scheme rejection, metadata-endpoint
block, size cap, and the provenance returned on a successful 200.

No network: ``urllib`` is monkeypatched via ``_resolve_host`` and ``_open``.
"""

from __future__ import annotations

import hashlib
import io
import urllib.request
from http.client import HTTPMessage

import pytest

from careerops.adapters import http_fetcher
from careerops.adapters.http_fetcher import (
    CircuitOpenError,
    FetchedResponse,
    FetchError,
    SSRFError,
    _SSRFRedirectHandler,
    fetch,
)


class _FakeResponse:
    """Minimal urllib response double respecting chunked reads."""

    def __init__(
        self, body: bytes, *, status: int = 200, url: str = "http://example.com/x"
    ) -> None:
        self._body = body
        self.status = status
        self._url = url

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._body
            self._body = b""
            return chunk
        chunk = self._body[:size]
        self._body = self._body[size:]
        return chunk

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _no_real_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable real sleeping/last-fetch state for every test in this module."""
    monkeypatch.setattr(http_fetcher, "_min_domain_interval", 0.0)
    monkeypatch.setattr(http_fetcher, "_last_fetch", {})
    monkeypatch.setattr(http_fetcher, "_sleep", lambda _s: None)
    monkeypatch.setattr(http_fetcher, "_dns_cache", {})


def _stub_open_with_fake(fake: _FakeResponse, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire ``_resolve_host`` to a public IP and ``_open`` to return ``fake``."""
    monkeypatch.setattr(http_fetcher, "_resolve_host", lambda _host: ("93.184.216.34",))
    monkeypatch.setattr(http_fetcher, "_open", lambda _req, *, timeout=0.0: fake)


class TestSSRFDefense:
    def test_private_ipv4_literal_rejected(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://10.0.0.1/")

    def test_loopback_ipv4_rejected(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://127.0.0.1:8080/")

    def test_rfc1918_172_16_rejected(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://172.16.5.5/")

    def test_rfc1918_192_168_rejected(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://192.168.1.1/")

    def test_ipv6_loopback_rejected(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://[::1]/")

    def test_ipv6_unique_local_rejected(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://[fd00:dead:beef::1]/")

    def test_metadata_endpoint_rejected(self) -> None:
        with pytest.raises(SSRFError, match="metadata"):
            fetch("http://169.254.169.254/latest/meta-data/iam/")

    def test_hostname_resolving_to_private_ip_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(http_fetcher, "_resolve_host", lambda _host: ("10.5.5.5",))
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://internal.example.com/")

    def test_non_http_scheme_rejected(self) -> None:
        with pytest.raises(SSRFError, match="scheme"):
            fetch("file:///etc/passwd")

    def test_ftp_scheme_rejected(self) -> None:
        with pytest.raises(SSRFError, match="scheme"):
            fetch("ftp://example.com/file")

    def test_gopher_scheme_rejected(self) -> None:
        with pytest.raises(SSRFError, match="scheme"):
            fetch("gopher://example.com/x")

    def test_host_allowlist_rejects_unlisted_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(http_fetcher, "_host_allowlist", frozenset({"allowed.example.com"}))
        with pytest.raises(SSRFError, match="allowlist"):
            fetch("http://not-allowed.example.com/")


class TestRedirectValidation:
    def test_redirect_to_private_ipv4_rejected(self) -> None:
        handler = _SSRFRedirectHandler()
        req = urllib.request.Request("http://example.com/")
        with pytest.raises(SSRFError, match="private/reserved"):
            handler.redirect_request(
                req, io.BytesIO(), 302, "Found", HTTPMessage(), "http://10.0.0.5/"
            )

    def test_redirect_to_loopback_rejected(self) -> None:
        handler = _SSRFRedirectHandler()
        req = urllib.request.Request("http://example.com/")
        with pytest.raises(SSRFError, match="private/reserved"):
            handler.redirect_request(
                req, io.BytesIO(), 307, "Temporary Redirect", HTTPMessage(), "http://127.0.0.1/x"
            )

    def test_redirect_to_metadata_rejected(self) -> None:
        handler = _SSRFRedirectHandler()
        req = urllib.request.Request("http://example.com/")
        with pytest.raises(SSRFError, match="metadata"):
            handler.redirect_request(
                req, io.BytesIO(), 302, "Found", HTTPMessage(), "http://169.254.169.254/"
            )

    def test_redirect_to_non_http_scheme_rejected(self) -> None:
        handler = _SSRFRedirectHandler()
        req = urllib.request.Request("http://example.com/")
        with pytest.raises(SSRFError, match="scheme"):
            handler.redirect_request(
                req, io.BytesIO(), 302, "Found", HTTPMessage(), "file:///etc/passwd"
            )

    def test_redirect_to_public_target_is_validated_then_delegated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Validation must pass (public IP), then base builds a real Request.
        monkeypatch.setattr(http_fetcher, "_resolve_host", lambda _host: ("93.184.216.34",))
        handler = _SSRFRedirectHandler()
        req = urllib.request.Request("http://example.com/")
        result = handler.redirect_request(
            req, io.BytesIO(), 302, "Found", HTTPMessage(), "http://example.org/next"
        )
        assert isinstance(result, urllib.request.Request)
        assert result.full_url == "http://example.org/next"


class TestSizeLimit:
    def test_size_limit_exceeded_raises_fetch_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeResponse(b"x" * 2_000_000, url="http://example.com/big")
        _stub_open_with_fake(fake, monkeypatch)
        with pytest.raises(FetchError, match="size limit"):
            fetch("http://example.com/big", size_limit=1_000_000)

    def test_body_under_limit_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = b"y" * 4_096
        fake = _FakeResponse(body, url="http://example.com/ok")
        _stub_open_with_fake(fake, monkeypatch)
        result = fetch("http://example.com/ok", size_limit=1_000_000)
        assert result.body == body.decode()


class TestSuccessfulFetch:
    def test_normal_200_returns_fetched_response_with_correct_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body_text = "<html><body>Senior Platform Engineer</body></html>"
        body_bytes = body_text.encode("utf-8")
        expected_hash = hashlib.sha256(body_bytes).hexdigest()
        final = "https://example.com/jobs/123"
        fake = _FakeResponse(body_bytes, status=200, url=final)
        _stub_open_with_fake(fake, monkeypatch)

        result = fetch("http://example.com/jobs/123", timeout=10.0)

        assert isinstance(result, FetchedResponse)
        assert result.status_code == 200
        assert result.final_url == final
        assert result.response_hash == expected_hash
        assert result.body == body_text
        from datetime import UTC

        assert result.fetched_at.tzinfo is not None  # aware
        assert result.fetched_at.tzinfo == UTC  # UTC

    def test_fetched_response_is_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeResponse(b"payload", url="http://example.com/x")
        _stub_open_with_fake(fake, monkeypatch)
        result = fetch("http://example.com/x")
        with pytest.raises((AttributeError, TypeError)):
            result.status_code = 500  # type: ignore[misc]

    def test_response_hash_matches_sha256_of_raw_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body_bytes = "héllo → world".encode()
        fake = _FakeResponse(body_bytes, url="http://example.com/u")
        _stub_open_with_fake(fake, monkeypatch)
        result = fetch("http://example.com/u")
        assert result.response_hash == hashlib.sha256(body_bytes).hexdigest()


class TestRateLimit:
    def test_sleeps_when_domain_fetched_too_recently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(http_fetcher, "_min_domain_interval", 2.0)
        monkeypatch.setattr(http_fetcher, "_sleep", lambda s: sleeps.append(s))
        # _rate_limit calls _monotonic twice per fetch (now, then record).
        clock = iter([100.0, 100.5, 101.0, 101.0])
        monkeypatch.setattr(http_fetcher, "_monotonic", lambda: next(clock))
        fake = _FakeResponse(b"ok")
        _stub_open_with_fake(fake, monkeypatch)

        fetch("http://example.com/a")  # no prior -> no sleep, records 100.5
        assert sleeps == []
        fetch("http://example.com/b")  # now=101.0, last=100.5 -> wait 1.5
        assert sleeps == [1.5]

    def test_no_sleep_when_interval_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # autouse fixture already set interval 0; ensure _sleep untouched.
        called: list[float] = []
        monkeypatch.setattr(http_fetcher, "_sleep", lambda s: called.append(s))
        fake = _FakeResponse(b"ok")
        _stub_open_with_fake(fake, monkeypatch)
        fetch("http://example.com/a")
        fetch("http://example.com/a")
        assert called == []


class TestCircuitBreakerInFetch:
    """Circuit breaker integration: open circuit rejects, recovery works."""

    def test_open_circuit_rejects_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When breaker.allow returns False, fetch raises CircuitOpenError."""
        from careerops.orchestration.circuit_breaker import (
            CircuitBreaker,
            CircuitBreakerConfig,
        )

        # Use a breaker with threshold=1 so one failure opens the circuit.
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, recovery_timeout_seconds=30.0)
        )
        monkeypatch.setattr(http_fetcher, "_breaker", breaker)
        _stub_open_with_fake(_FakeResponse(b"ok"), monkeypatch)

        # Trigger a failure to open the circuit.
        monkeypatch.setattr(http_fetcher, "_resolve_host", lambda _host: ("10.0.0.1",))
        with pytest.raises(SSRFError):
            fetch("http://example.com/x")
        # SSRFError counts as nothing for breaker (no record_failure called).
        # Manually trip it.
        breaker.record_failure("example.com")
        assert breaker.state("example.com").value == "open"

        # Now restore valid resolution but circuit is open.
        monkeypatch.setattr(http_fetcher, "_resolve_host", lambda _host: ("93.184.216.34",))
        with pytest.raises(CircuitOpenError, match="circuit breaker open"):
            fetch("http://example.com/x")

    def test_circuit_breaker_recovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After recovery_timeout, half-open admits one probe; success closes it."""
        from careerops.orchestration.circuit_breaker import (
            CircuitBreaker,
            CircuitBreakerConfig,
        )

        clock_time = 1000.0
        nonlocal_clock: list[float] = [clock_time]

        def fake_clock() -> float:
            return nonlocal_clock[0]

        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, recovery_timeout_seconds=10.0),
            clock=fake_clock,
        )
        monkeypatch.setattr(http_fetcher, "_breaker", breaker)
        _stub_open_with_fake(_FakeResponse(b"ok"), monkeypatch)

        # Trip the circuit.
        breaker.record_failure("example.com")
        assert breaker.state("example.com").value == "open"

        # Still open before recovery timeout.
        nonlocal_clock[0] = 1005.0
        with pytest.raises(CircuitOpenError):
            fetch("http://example.com/x")

        # Advance past recovery timeout -> HALF_OPEN, probe allowed.
        nonlocal_clock[0] = 1011.0
        result = fetch("http://example.com/x")
        assert result.status_code == 200
        # Success recorded -> circuit closed.
        assert breaker.state("example.com").value == "closed"


class TestSSRFBlocksNewRanges:
    """SSRF blocks CGNAT (100.64.0.0/10) and benchmarking (198.18.0.0/15)."""

    def test_cgnat_100_64_rejected(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://100.64.0.1/")

    def test_cgnat_100_127_rejected(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://100.127.255.254/")

    def test_benchmarking_198_18_rejected(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://198.18.0.1/")

    def test_benchmarking_198_19_rejected(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://198.19.255.254/")

    def test_cgnat_host_resolving_to_range_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(http_fetcher, "_resolve_host", lambda _host: ("100.64.1.1",))
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://cgnat.example.com/")

    def test_benchmarking_host_resolving_to_range_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(http_fetcher, "_resolve_host", lambda _host: ("198.18.5.5",))
        with pytest.raises(SSRFError, match="private/reserved"):
            fetch("http://bench.example.com/")


class TestDNSRebinding:
    """DNS-rebinding detection: reject when resolved IPs change between calls."""

    def test_dns_rebinding_detected_on_ip_change(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """First resolve → public IP; second resolve → different public IP → blocked."""
        call_count = 0

        def resolving_host(host: str) -> tuple[str, ...]:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return ("93.184.216.34",)
            return ("93.184.216.99",)

        monkeypatch.setattr(http_fetcher, "_resolve_host", resolving_host)
        # First call succeeds and caches IPs.
        fake = _FakeResponse(b"ok")
        monkeypatch.setattr(http_fetcher, "_open", lambda _r, *, timeout=0.0: fake)
        fetch("http://rebind.example.com/")

        # Second call with different IPs triggers rebinding detection.
        with pytest.raises(SSRFError, match="DNS rebinding"):
            fetch("http://rebind.example.com/")

    def test_dns_consistent_resolution_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same IPs on subsequent calls → no rebinding error."""
        monkeypatch.setattr(http_fetcher, "_resolve_host", lambda _h: ("93.184.216.34",))
        fake = _FakeResponse(b"ok")
        monkeypatch.setattr(http_fetcher, "_open", lambda _r, *, timeout=0.0: fake)
        fetch("http://stable.example.com/")
        result = fetch("http://stable.example.com/")
        assert result.status_code == 200

    def test_dns_rebinding_multi_ip_set_change(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """IP set changes even if one IP overlaps → blocked."""
        call_count = 0

        def resolving_host(host: str) -> tuple[str, ...]:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return ("93.184.216.34", "93.184.216.35")
            return ("93.184.216.34", "93.184.216.99")

        monkeypatch.setattr(http_fetcher, "_resolve_host", resolving_host)
        fake = _FakeResponse(b"ok")
        monkeypatch.setattr(http_fetcher, "_open", lambda _r, *, timeout=0.0: fake)
        fetch("http://multi.example.com/")

        with pytest.raises(SSRFError, match="DNS rebinding"):
            fetch("http://multi.example.com/")
