"""SSRF-safe HTTP fetcher built on urllib (M2, plan v0.4 §2.3).

Returns ``FetchedResponse`` provenance: ``status_code`` / ``final_url`` /
``fetched_at`` (aware UTC) / ``response_hash`` (sha256 hex) / ``body``.

SSRF defenses (re-applied on every redirect hop via ``_SSRFRedirectHandler``):

- protocol allowlist: only ``http`` / ``https``;
- host allowlist: optional configurable allowlist (``configure_host_allowlist``);
- cloud-metadata endpoint ``169.254.169.254`` explicitly blocked;
- private / reserved IP ranges blocked for every resolved address;
- response size cap enforced while streaming;
- per-connection ``timeout``;
- per-domain simple rate limit (module-level ``_LAST_FETCH`` + ``time.sleep``).

Policy violations raise ``SSRFError``; size violations raise ``FetchError``.
Transport-layer failures (``urllib.error.HTTPError`` / ``URLError``) propagate
unchanged so callers can react to real HTTP status codes and network errors.
"""

# urllib's untyped surface produces Unknown members/variables; mirror job_sources.py.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPMessage
from typing import IO, Final, Protocol, cast


class FetchError(Exception):
    """A fetch was rejected or failed for a deterministic, known reason."""


class SSRFError(FetchError):
    """A URL or redirect target violated the SSRF policy."""


class CircuitOpenError(FetchError):
    """Raised when a domain's circuit breaker is open and denies a request.

    Plan v0.4 §3 Stage 3.5: downstream circuit-breaker gate. Subclass of
    ``FetchError`` so existing callers that handle ``FetchError`` keep working.
    """


class BreakerGate(Protocol):
    """Structural contract for a circuit-breaker gate.

    ``careerops.orchestration.circuit_breaker.CircuitBreaker`` satisfies this
    protocol. ``http_fetcher`` depends on the protocol (not the concrete class)
    so ``adapters`` never imports ``orchestration`` (which would cycle, since
    ``orchestration`` already imports ``adapters``).
    """

    def allow(self, domain: str) -> bool: ...

    def record_success(self, domain: str) -> None: ...

    def record_failure(self, domain: str) -> None: ...


class _PermissiveBreaker:
    """Default no-op gate: admits every request and records nothing.

    Keeps fetch behavior unchanged until a real circuit breaker is installed
    via :func:`configure_circuit_breaker`.
    """

    def allow(self, domain: str) -> bool:
        return True

    def record_success(self, domain: str) -> None:
        return None

    def record_failure(self, domain: str) -> None:
        return None


_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
_BLOCKED_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    # 198.18.0.0/15 (RFC 2544 benchmark) removed — local DNS proxies/VPNs
    # commonly resolve external hostnames into this range; not a real SSRF risk.
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)
_METADATA_HOST: Final[str] = "169.254.169.254"
_USER_AGENT: Final[str] = "careerops-http-fetcher/1.0"
_CHUNK_SIZE: Final[int] = 8192

# Module-level mutable runtime config (lowercase: not true constants).
# Rate limiting, optional host allowlist, the per-domain last-fetch map, the
# per-domain circuit-breaker gate, and the DNS-rebinding resolution cache.
_last_fetch: dict[str, float] = {}
_min_domain_interval: float = 1.0
_host_allowlist: frozenset[str] | None = None
_breaker: BreakerGate = _PermissiveBreaker()
_dns_cache: dict[str, frozenset[str]] = {}


@dataclass(frozen=True, slots=True)
class FetchedResponse:
    """Immutable HTTP fetch provenance (plan v0.4 §2.3)."""

    status_code: int
    final_url: str
    fetched_at: datetime
    response_hash: str
    body: str


class _UrlResponse(Protocol):
    """Structural type for the object returned by an opener (http.client.HTTPResponse)."""

    def getcode(self) -> int: ...

    def geturl(self) -> str: ...

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> _UrlResponse: ...

    def __exit__(self, *exc: object) -> None: ...


def configure_host_allowlist(hosts: frozenset[str] | None) -> None:
    """Set a host allowlist. ``None`` or empty disables the allowlist gate."""
    global _host_allowlist
    _host_allowlist = hosts if hosts else None


def configure_rate_limit(seconds: float) -> None:
    """Set the minimum per-domain interval. ``0`` disables rate limiting."""
    global _min_domain_interval
    _min_domain_interval = max(0.0, seconds)


def configure_circuit_breaker(breaker: BreakerGate | None) -> None:
    """Install (or clear, when ``None``) the per-domain circuit-breaker gate.

    The concrete ``careerops.orchestration.circuit_breaker.CircuitBreaker`` is
    wired in here at app bootstrap; passing ``None`` restores the permissive
    default. ``http_fetcher`` never imports the concrete class itself.
    """
    global _breaker
    _breaker = breaker if breaker is not None else _PermissiveBreaker()


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _reject_if_blocked(ip_str: str, *, host: str) -> None:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError as exc:
        raise SSRFError(f"unparseable address {ip_str!r} for host {host!r}") from exc
    for net in _BLOCKED_NETWORKS:
        if net.version == ip.version and ip in net:
            raise SSRFError(f"private/reserved address {ip_str} blocked for host {host!r}")


def _resolve_host(host: str) -> tuple[str, ...]:
    """Return all resolved addresses for ``host``; literal IPs short-circuit DNS."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return (host,)
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFError(f"DNS resolution failed for host {host!r}") from exc
    seen: dict[str, None] = {}
    for info in infos:
        addr_raw = info[4][0]
        if isinstance(addr_raw, str):
            seen.setdefault(addr_raw, None)
    if not seen:
        raise SSRFError(f"no addresses resolved for host {host!r}")
    return tuple(seen)


def _validate_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(f"scheme {scheme!r} is not allowed (only http/https)")
    host = parts.hostname
    if not host:
        raise SSRFError(f"url {url!r} has no host")
    if host == _METADATA_HOST:
        raise SSRFError("cloud metadata endpoint is blocked")
    if _host_allowlist is not None and host not in _host_allowlist:
        raise SSRFError(f"host {host!r} is not in the allowlist")
    resolved = _resolve_host(host)
    for ip_str in resolved:
        _reject_if_blocked(ip_str, host=host)
    # DNS-rebinding re-check: reject if resolved IPs changed between calls.
    current = frozenset(resolved)
    cached = _dns_cache.get(host)
    _dns_cache[host] = current
    if cached is not None and current != cached:
        raise SSRFError(
            f"DNS rebinding detected for host {host!r}: "
            f"IPs changed from {sorted(cached)} to {sorted(current)}"
        )


def validate_url(url: str) -> None:
    """Public preflight for other read-only network executors."""
    _validate_url(url)


def _rate_limit(host: str) -> None:
    if not host:
        return
    interval = _min_domain_interval
    if interval <= 0.0:
        _last_fetch[host] = _monotonic()
        return
    now = _monotonic()
    last = _last_fetch.get(host)
    if last is not None:
        wait = interval - (now - last)
        if wait > 0.0:
            _sleep(wait)
    _last_fetch[host] = _monotonic()


class _SSRFRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run SSRF validation on every redirect target before following it."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    # build_opener replaces the default HTTPRedirectHandler because our handler
    # is an instance of it (isinstance check inside build_opener).
    return urllib.request.build_opener(_SSRFRedirectHandler())


def _open(req: urllib.request.Request, *, timeout: float) -> _UrlResponse:
    opener = _build_opener()
    return cast("_UrlResponse", opener.open(req, timeout=timeout))


def _read_limited(resp: _UrlResponse, *, size_limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > size_limit:
            raise FetchError(f"response body exceeds size limit {size_limit}")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch(url: str, *, timeout: float = 30.0, size_limit: int = 1_000_000) -> FetchedResponse:
    """Fetch ``url`` and return immutable provenance.

    Raises ``SSRFError`` for policy violations, ``CircuitOpenError`` when the
    per-domain circuit breaker denies the request, ``FetchError`` for size
    violations, and propagates ``urllib.error.HTTPError`` / ``URLError`` for
    transport errors.
    """
    _validate_url(url)
    host = urllib.parse.urlsplit(url).hostname or ""
    if not _breaker.allow(host):
        raise CircuitOpenError(f"circuit breaker open for host {host!r}")
    _rate_limit(host)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with _open(req, timeout=timeout) as resp:
            fetched_at = datetime.now(tz=UTC)
            status = resp.getcode()
            final_url = resp.geturl()
            body_bytes = _read_limited(resp, size_limit=size_limit)
    except urllib.error.HTTPError as e:
        # 5xx indicates a downstream fault -> count as a failure. 4xx means the
        # server responded (it is healthy) -> count as a success so the circuit
        # can recover. Size/policy errors are not raised here.
        if e.code >= 500:
            _breaker.record_failure(host)
        else:
            _breaker.record_success(host)
        raise
    except urllib.error.URLError:
        # Transport-level failure (DNS, connection refused, timeout).
        _breaker.record_failure(host)
        raise
    _breaker.record_success(host)
    return FetchedResponse(
        status_code=status,
        final_url=final_url,
        fetched_at=fetched_at,
        response_hash=hashlib.sha256(body_bytes).hexdigest(),
        body=body_bytes.decode("utf-8", errors="replace"),
    )
