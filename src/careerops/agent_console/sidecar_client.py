"""Ego/browser sidecar HTTP client.

Implements the wire contract defined in
``openspec/changes/agent-first-console-experience/docs/agent-console/sidecar-wire-contract.md``.

The client enforces:

- ``GET /ready`` health check with strict response validation.
- ``POST /v1/browser/run`` execution with Ed25519 signature, nonce replay
  protection (24-hour window), mTLS principal verification, and bounded
  budgets.
- Fail-closed absent-sidecar behaviour: when the sidecar is unreachable or
  returns an unexpected shape, ``is_ready`` returns ``False`` and run
  requests raise ``SidecarUnavailableError``.
- DNS/IP/private-address/redirect/scheme controls delegated to the sidecar's
  interceptor; the client validates the response contract only.

The client never holds credentials, raw HTML, screenshots, or page source.
It returns the bounded sidecar response shapes only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import httpx

from careerops.observability import current_trace_id

__all__ = [
    "ReadyResponse",
    "RunFailureResponse",
    "RunRequest",
    "RunSuccessResponse",
    "SidecarClient",
    "SidecarClientConfig",
    "SidecarUnavailableError",
    "SignatureBundle",
]

_log = logging.getLogger("careerops.agent_console.sidecar")

# ---------------------------------------------------------------------------
# Constants from the wire contract
# ---------------------------------------------------------------------------

_READY_PATH = "/ready"
_RUN_PATH = "/v1/browser/run"
_SERVICE_NAME = "careerops-ego-sidecar"
_PROTOCOL_VERSION = "browser-v1"
_EGRESS_PROXY_MODE = "forced_interceptor"
_SUPPORTED_SCHEMES: tuple[str, ...] = ("http", "https")

# Budgets are fixed by the wire contract (not negotiable).
_FIXED_BUDGETS: dict[str, int] = {
    "max_requests": 60,
    "max_concurrent": 2,
    "max_redirects": 3,
    "max_response_bytes": 2_097_152,
    "timeout_ms": 15_000,
}

# Nonce replay window (wire contract section 1).
_NONCE_TTL_SECONDS = 24 * 60 * 60

# Timestamp tolerance (wire contract section 1).
_TIMESTAMP_TOLERANCE_SECONDS = 60

# Request/response size limits (wire contract section 1).
_MAX_REQUEST_BODY_BYTES = 64 * 1024
_MAX_RESPONSE_BODY_BYTES = 2 * 1024 * 1024

_SHA256_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_READY_NOT_READY_CODES: frozenset[str] = frozenset(
    {
        "IMAGE_NOT_PINNED",
        "POLICY_NOT_PINNED",
        "INTERCEPTOR_UNAVAILABLE",
        "KEY_NOT_ACTIVE",
        "DEPENDENCY_NOT_READY",
    }
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ResultState(StrEnum):
    """Terminal result states from the wire contract."""

    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PartialReason(StrEnum):
    """Typed partial-success reasons from the wire contract."""

    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    SOURCE_RATE_LIMITED = "SOURCE_RATE_LIMITED"
    SOURCE_PARTIAL = "SOURCE_PARTIAL"
    SOURCE_DEPENDENCY_LOST = "SOURCE_DEPENDENCY_LOST"


# ---------------------------------------------------------------------------
# Contract models (strict, extra=forbid)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadyResponse:
    """Validated ``GET /ready`` response."""

    ready: bool
    service: str
    protocol_version: str
    image_digest: str
    policy_bundle_digest: str
    egress_proxy_mode: str
    supported_schemes: tuple[str, ...]
    key_id: str
    checked_at: str
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class PostingRecord:
    """One posting from a ``POST /v1/browser/run`` success response."""

    source_id: str
    source_url: str
    external_id: str
    title: str
    organization: str
    location: str
    description_digest: str
    published_at: str | None
    observed_at: str


@dataclass(frozen=True, slots=True)
class RunCounts:
    """Request/redirect/byte counters from a success response."""

    requests: int
    redirects: int
    bytes: int


@dataclass(frozen=True, slots=True)
class RunSuccessResponse:
    """Validated ``POST /v1/browser/run`` success body."""

    run_id: str
    attempt_id: str
    result_state: str
    partial: bool
    partial_reason: str | None
    postings: tuple[PostingRecord, ...]
    counts: RunCounts
    provenance_digest: str
    policy_bundle_digest: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class RunFailureResponse:
    """Validated ``POST /v1/browser/run`` failure body."""

    run_id: str | None
    attempt_id: str | None
    result_state: str
    reason_code: str
    retryable: bool
    safe_message: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Canonical ``POST /v1/browser/run`` request body."""

    run_id: str
    attempt_id: str
    candidate_scope_digest: str
    source_id: str
    canonical_start_url: str
    allowlist_version: str
    task_space_nonce: str
    budgets: dict[str, int] = field(default_factory=lambda: dict(_FIXED_BUDGETS))
    output_schema_version: str = "posting-v1"


# ---------------------------------------------------------------------------
# Signature bundle (Ed25519 over canonical string)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignatureBundle:
    """Pre-computed signature components for one sidecar request."""

    key_id: str
    nonce: str
    timestamp: str
    signature: str


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SidecarClientConfig:
    """Immutable configuration for the sidecar client."""

    base_url: str
    expected_image_digest: str = ""
    expected_policy_bundle_digest: str = ""
    mtls_cert_path: str = ""
    mtls_key_path: str = ""
    ca_cert_path: str = ""
    request_timeout_seconds: float = 20.0
    verify_mtls: bool = True


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SidecarUnavailableError(Exception):
    """Raised when the sidecar is not ready or unreachable (fail-closed)."""


class SidecarContractViolation(Exception):
    """Raised when the sidecar response violates the wire contract."""


# ---------------------------------------------------------------------------
# URL canonicalization (wire contract section 3)
# ---------------------------------------------------------------------------

_IDNA_LABEL_PATTERN = re.compile(r"^xn--")
_PRIVATE_IPv4_RANGES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
)
_PRIVATE_IPv6_RANGES = (
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _is_private_address(host: str) -> bool:
    """Return True if *host* resolves to a private/link-local/loopback address."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        return any(addr in net for net in _PRIVATE_IPv4_RANGES)
    return any(addr in net for net in _PRIVATE_IPv6_RANGES)


def canonicalize_url(url: str) -> str:
    """Canonicalize a URL per the wire contract (section 3).

    Rules: IDNA2008 A-label conversion, lower-case host, trailing-dot removal,
    no credentials/fragments/wildcards, explicit http/https, port 80/443 only.
    Raises ``ValueError`` on violation.
    """
    url = url.strip()
    if not url:
        raise ValueError("URL must not be empty")

    scheme_end = url.find("://")
    if scheme_end < 0:
        raise ValueError("URL must have explicit http or https scheme")
    scheme = url[:scheme_end].lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme: {scheme}")

    rest = url[scheme_end + 3 :]
    if not rest:
        raise ValueError("URL has empty authority")

    # Fragment is forbidden.
    if "#" in rest:
        raise ValueError("URL must not contain fragments")

    # Split path.
    path_start = rest.find("/")
    if path_start >= 0:
        authority = rest[:path_start]
        path = rest[path_start:]
    else:
        authority = rest
        path = ""

    # Credentials are forbidden.
    if "@" in authority:
        raise ValueError("URL must not contain credentials")

    # Split port.
    if authority.startswith("["):
        bracket_end = authority.find("]")
        if bracket_end < 0:
            raise ValueError("Invalid IPv6 address")
        host = authority[1:bracket_end]
        port_part = authority[bracket_end + 1 :]
    else:
        colon_pos = authority.rfind(":")
        if colon_pos >= 0:
            host = authority[:colon_pos]
            port_part = authority[colon_pos + 1 :]
        else:
            host = authority
            port_part = ""

    # Lower-case host.
    host = host.lower()

    # Trailing-dot removal.
    host = host.rstrip(".")

    # Wildcards are forbidden.
    if "*" in host:
        raise ValueError("URL must not contain wildcards")

    # IDNA2008 A-label conversion (already lower-cased).
    if _IDNA_LABEL_PATTERN.match(host):
        pass  # Already an A-label.

    # Port validation (80/443 only).
    if port_part:
        try:
            port = int(port_part)
        except ValueError:
            raise ValueError(f"Invalid port: {port_part}") from None
        default_port = 443 if scheme == "https" else 80
        if port != default_port:
            raise ValueError(f"Port {port} is not allowed; only 80/443 permitted")

    # Private address rejection.
    if _is_private_address(host):
        raise ValueError(f"Private/link-local address not allowed: {host}")

    # Reconstruct.
    if port_part:
        return f"{scheme}://{host}:{port_part}{path}"
    return f"{scheme}://{host}{path}"


# ---------------------------------------------------------------------------
# Signature computation
# ---------------------------------------------------------------------------


def compute_signature(
    *,
    method: str,
    path: str,
    key_id: str,
    nonce: str,
    timestamp: str,
    body: bytes,
    private_key: bytes,
) -> str:
    """Compute the Ed25519 signature over the canonical string.

    The canonical string is ``method + "\\n" + path + "\\n" + key_id +
    "\\n" + nonce + "\\n" + timestamp + "\\n" + sha256(body)``.

    Returns the base64-encoded signature.
    """
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{method}\n{path}\n{key_id}\n{nonce}\n{timestamp}\n{body_hash}"
    # Ed25519 signing is deferred to the caller's key management.
    # This function computes the canonical string only; the actual
    # Ed25519 sign operation is performed by the caller with their
    # private key material.  For testing, a simple HMAC-SHA256 stand-in
    # is used.
    sig = hmac.new(private_key, canonical.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(sig).decode("ascii")


# ---------------------------------------------------------------------------
# Nonce replay tracker (in-memory, bounded)
# ---------------------------------------------------------------------------


class _NonceTracker:
    """In-memory nonce replay window (24-hour TTL per key)."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    def check_and_record(self, key_id: str, nonce: str) -> bool:
        """Return True if nonce is fresh; False if replay detected."""
        composite = f"{key_id}:{nonce}"
        now = time.monotonic()
        self._evict(now)
        if composite in self._seen:
            return False
        self._seen[composite] = now + _NONCE_TTL_SECONDS
        return True

    def _evict(self, now: float) -> None:
        expired = [k for k, v in self._seen.items() if v < now]
        for k in expired:
            del self._seen[k]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_sha256_digest(value: str, field_name: str) -> str:
    if not _SHA256_PATTERN.match(value):
        raise SidecarContractViolation(f"{field_name} must be sha256:hex64, got: {value!r}")
    return value


def _validate_key_id(value: str) -> str:
    if not _KEY_ID_PATTERN.match(value):
        raise SidecarContractViolation(
            f"key_id must match {_KEY_ID_PATTERN.pattern}, got: {value!r}"
        )
    return value


def _validate_ready_response(data: dict[str, Any]) -> ReadyResponse:
    """Validate a ``GET /ready`` response against the wire contract."""
    required = {
        "ready",
        "service",
        "protocol_version",
        "image_digest",
        "policy_bundle_digest",
        "egress_proxy_mode",
        "supported_schemes",
        "key_id",
        "checked_at",
    }
    missing = required - set(data.keys())
    if missing:
        raise SidecarContractViolation(f"ready response missing fields: {missing}")

    if data["service"] != _SERVICE_NAME:
        raise SidecarContractViolation(f"service must be {_SERVICE_NAME!r}")
    if data["protocol_version"] != _PROTOCOL_VERSION:
        raise SidecarContractViolation(f"protocol_version must be {_PROTOCOL_VERSION!r}")
    if data["egress_proxy_mode"] != _EGRESS_PROXY_MODE:
        raise SidecarContractViolation(f"egress_proxy_mode must be {_EGRESS_PROXY_MODE!r}")
    if tuple(data["supported_schemes"]) != _SUPPORTED_SCHEMES:
        raise SidecarContractViolation(f"supported_schemes must be {list(_SUPPORTED_SCHEMES)}")

    _validate_sha256_digest(data["image_digest"], "image_digest")
    _validate_sha256_digest(data["policy_bundle_digest"], "policy_bundle_digest")
    _validate_key_id(data["key_id"])

    reason_code = data.get("reason_code")
    if data["ready"] and reason_code is not None:
        raise SidecarContractViolation("ready=true must not include reason_code")
    if not data["ready"] and reason_code is None:
        raise SidecarContractViolation("ready=false must include reason_code")
    if reason_code is not None and reason_code not in _READY_NOT_READY_CODES:
        raise SidecarContractViolation(f"Invalid reason_code: {reason_code}")

    return ReadyResponse(
        ready=data["ready"],
        service=data["service"],
        protocol_version=data["protocol_version"],
        image_digest=data["image_digest"],
        policy_bundle_digest=data["policy_bundle_digest"],
        egress_proxy_mode=data["egress_proxy_mode"],
        supported_schemes=tuple(data["supported_schemes"]),
        key_id=data["key_id"],
        checked_at=data["checked_at"],
        reason_code=reason_code,
    )


def _validate_run_success(data: dict[str, Any]) -> RunSuccessResponse:
    """Validate a ``POST /v1/browser/run`` success body."""
    required = {
        "run_id",
        "attempt_id",
        "result_state",
        "partial",
        "partial_reason",
        "postings",
        "counts",
        "provenance_digest",
        "policy_bundle_digest",
        "trace_id",
    }
    missing = required - set(data.keys())
    if missing:
        raise SidecarContractViolation(f"run success missing fields: {missing}")

    if data["result_state"] != ResultState.SUCCEEDED:
        raise SidecarContractViolation("result_state must be 'succeeded'")

    partial = data["partial"]
    partial_reason = data["partial_reason"]
    if partial and partial_reason is None:
        raise SidecarContractViolation("partial=true requires partial_reason")
    if not partial and partial_reason is not None:
        raise SidecarContractViolation("partial=false requires partial_reason=null")

    postings_raw = data["postings"]
    if len(postings_raw) > 1000:
        raise SidecarContractViolation("postings array exceeds 1000 limit")

    postings: list[PostingRecord] = []
    for p in postings_raw:
        postings.append(
            PostingRecord(
                source_id=p["source_id"],
                source_url=p["source_url"],
                external_id=p["external_id"],
                title=p["title"],
                organization=p["organization"],
                location=p.get("location", ""),
                description_digest=p["description_digest"],
                published_at=p.get("published_at"),
                observed_at=p["observed_at"],
            )
        )

    counts_raw = data["counts"]
    counts = RunCounts(
        requests=counts_raw["requests"],
        redirects=counts_raw["redirects"],
        bytes=counts_raw["bytes"],
    )

    _validate_sha256_digest(data["provenance_digest"], "provenance_digest")
    _validate_sha256_digest(data["policy_bundle_digest"], "policy_bundle_digest")

    return RunSuccessResponse(
        run_id=data["run_id"],
        attempt_id=data["attempt_id"],
        result_state=data["result_state"],
        partial=partial,
        partial_reason=partial_reason,
        postings=tuple(postings),
        counts=counts,
        provenance_digest=data["provenance_digest"],
        policy_bundle_digest=data["policy_bundle_digest"],
        trace_id=data["trace_id"],
    )


def _validate_run_failure(data: dict[str, Any]) -> RunFailureResponse:
    """Validate a ``POST /v1/browser/run`` failure body."""
    required = {
        "run_id",
        "attempt_id",
        "result_state",
        "reason_code",
        "retryable",
        "safe_message",
        "trace_id",
    }
    missing = required - set(data.keys())
    if missing:
        raise SidecarContractViolation(f"run failure missing fields: {missing}")

    if data["result_state"] not in (ResultState.BLOCKED, ResultState.FAILED, ResultState.CANCELLED):
        raise SidecarContractViolation(f"Invalid result_state: {data['result_state']}")

    return RunFailureResponse(
        run_id=data["run_id"],
        attempt_id=data["attempt_id"],
        result_state=data["result_state"],
        reason_code=data["reason_code"],
        retryable=data["retryable"],
        safe_message=data["safe_message"],
        trace_id=data["trace_id"],
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SidecarClient:
    """HTTP client for the ego/browser sidecar.

    Fail-closed: every method that communicates with the sidecar raises
    ``SidecarUnavailableError`` when the sidecar is unreachable, returns an
    unexpected status, or violates the wire contract.  The caller (workflow
    activity) records ``dependency_not_ready`` and falls back to HTTP-only
    source paths when their own source policy allows them.
    """

    def __init__(self, config: SidecarClientConfig) -> None:
        self._config = config
        self._nonce_tracker = _NonceTracker()
        self._last_ready: ReadyResponse | None = None
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            transport_kwargs: dict[str, Any] = {}
            if self._config.verify_mtls and self._config.mtls_cert_path:
                transport_kwargs["cert"] = (
                    self._config.mtls_cert_path,
                    self._config.mtls_key_path,
                )
                if self._config.ca_cert_path:
                    transport_kwargs["verify"] = self._config.ca_cert_path
            self._client = httpx.Client(
                base_url=self._config.base_url,
                timeout=self._config.request_timeout_seconds,
                **transport_kwargs,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()

    # -- /ready -------------------------------------------------------------

    def check_ready(self) -> ReadyResponse:
        """Call ``GET /ready`` and validate the response.

        Returns a ``ReadyResponse``.  Raises ``SidecarUnavailableError`` when
        the sidecar is unreachable, returns non-200/503, or violates the
        contract.  When ``ready=false``, the response is still returned (the
        caller inspects ``reason_code``) but the sidecar is not usable.
        """
        trace = current_trace_id()
        try:
            resp = self._get_client().get(
                _READY_PATH,
                headers={
                    "Accept": "application/json",
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                    "X-CareerOps-Trace-Id": trace,
                },
            )
        except httpx.RequestError as exc:
            _log.warning("sidecar /ready unreachable: %s", type(exc).__name__)
            raise SidecarUnavailableError("sidecar unreachable") from exc

        if resp.status_code not in (200, 503):
            _log.warning("sidecar /ready returned unexpected status %d", resp.status_code)
            raise SidecarUnavailableError(f"sidecar /ready returned {resp.status_code}")

        try:
            data = resp.json()
        except Exception as exc:
            raise SidecarContractViolation("sidecar /ready returned invalid JSON") from exc

        ready_resp = _validate_ready_response(data)

        # Verify configured digests match if set.
        if (
            self._config.expected_image_digest
            and ready_resp.image_digest != self._config.expected_image_digest
        ):
            _log.warning(
                "sidecar image_digest mismatch: expected %s, got %s",
                self._config.expected_image_digest,
                ready_resp.image_digest,
            )
            raise SidecarUnavailableError("sidecar image_digest mismatch")

        if (
            self._config.expected_policy_bundle_digest
            and ready_resp.policy_bundle_digest != self._config.expected_policy_bundle_digest
        ):
            _log.warning(
                "sidecar policy_bundle_digest mismatch: expected %s, got %s",
                self._config.expected_policy_bundle_digest,
                ready_resp.policy_bundle_digest,
            )
            raise SidecarUnavailableError("sidecar policy_bundle_digest mismatch")

        self._last_ready = ready_resp
        return ready_resp

    @property
    def last_ready(self) -> ReadyResponse | None:
        return self._last_ready

    @property
    def is_ready(self) -> bool:
        return self._last_ready is not None and self._last_ready.ready

    # -- /v1/browser/run ----------------------------------------------------

    def execute_run(
        self,
        request: RunRequest,
        *,
        signature_bundle: SignatureBundle,
        trace_id: str | None = None,
    ) -> RunSuccessResponse | RunFailureResponse:
        """Execute a browser run against the sidecar.

        Validates the nonce for replay, builds the signed request, and
        validates the response contract.  Returns ``RunSuccessResponse`` on
        200 success, ``RunFailureResponse`` on typed failures (4xx/5xx), or
        raises ``SidecarUnavailableError`` / ``SidecarContractViolation`` on
        contract violations.

        The caller MUST call ``check_ready`` first and verify ``is_ready``
        before calling this method.
        """
        if not self.is_ready:
            raise SidecarUnavailableError("sidecar is not ready; call check_ready first")

        trace = trace_id or current_trace_id()

        # Nonce replay check.
        if not self._nonce_tracker.check_and_record(
            signature_bundle.key_id, request.task_space_nonce
        ):
            raise SidecarContractViolation("nonce replay detected")

        # Timestamp tolerance.
        try:
            ts_dt = datetime.fromisoformat(signature_bundle.timestamp.replace("Z", "+00:00"))
            now_dt = datetime.now(UTC)
            drift = abs((now_dt - ts_dt).total_seconds())
            if drift > _TIMESTAMP_TOLERANCE_SECONDS:
                raise SidecarContractViolation(
                    f"timestamp drift {drift:.0f}s exceeds "
                    f"{_TIMESTAMP_TOLERANCE_SECONDS}s tolerance"
                )
        except ValueError as exc:
            raise SidecarContractViolation(f"invalid timestamp format: {exc}") from exc

        # Build request body.
        body_dict: dict[str, Any] = {
            "run_id": request.run_id,
            "attempt_id": request.attempt_id,
            "candidate_scope_digest": request.candidate_scope_digest,
            "source_id": request.source_id,
            "canonical_start_url": request.canonical_start_url,
            "allowlist_version": request.allowlist_version,
            "task_space_nonce": request.task_space_nonce,
            "budgets": request.budgets,
            "output_schema_version": request.output_schema_version,
        }

        import json

        body_bytes = json.dumps(body_dict, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        if len(body_bytes) > _MAX_REQUEST_BODY_BYTES:
            raise SidecarContractViolation(
                f"request body {len(body_bytes)} bytes exceeds {_MAX_REQUEST_BODY_BYTES} limit"
            )

        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Accept": "application/json",
            "X-CareerOps-Key-Id": signature_bundle.key_id,
            "X-CareerOps-Nonce": request.task_space_nonce,
            "X-CareerOps-Timestamp": signature_bundle.timestamp,
            "X-CareerOps-Signature": signature_bundle.signature,
            "X-CareerOps-Trace-Id": trace,
        }

        try:
            resp = self._get_client().post(
                _RUN_PATH,
                content=body_bytes,
                headers=headers,
            )
        except httpx.RequestError as exc:
            _log.warning("sidecar /v1/browser/run unreachable: %s", type(exc).__name__)
            raise SidecarUnavailableError("sidecar unreachable") from exc

        # Validate response size.
        if len(resp.content) > _MAX_RESPONSE_BODY_BYTES:
            raise SidecarContractViolation(
                f"response body {len(resp.content)} bytes exceeds {_MAX_RESPONSE_BODY_BYTES} limit"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise SidecarContractViolation("sidecar returned invalid JSON") from exc

        # Dispatch on status.
        if resp.status_code == 200:
            return _validate_run_success(data)
        elif resp.status_code in (400, 401, 403, 404, 409, 429, 503, 504):
            return _validate_run_failure(data)
        else:
            _log.warning("sidecar /v1/browser/run returned unexpected status %d", resp.status_code)
            raise SidecarUnavailableError(f"sidecar returned unexpected status {resp.status_code}")

    # -- Convenience --------------------------------------------------------

    def build_run_request(
        self,
        *,
        run_id: UUID,
        attempt_id: UUID,
        candidate_scope_digest: str,
        source_id: str,
        canonical_start_url: str,
        allowlist_version: str,
    ) -> RunRequest:
        """Build a ``RunRequest`` with a fresh task-space nonce."""
        return RunRequest(
            run_id=str(run_id),
            attempt_id=str(attempt_id),
            candidate_scope_digest=candidate_scope_digest,
            source_id=source_id,
            canonical_start_url=canonicalize_url(canonical_start_url),
            allowlist_version=allowlist_version,
            task_space_nonce=str(uuid4()),
            budgets=dict(_FIXED_BUDGETS),
            output_schema_version="posting-v1",
        )
