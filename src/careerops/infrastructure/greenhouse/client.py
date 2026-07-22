from __future__ import annotations

import base64
import errno
import hashlib
import json
import re
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Protocol, Self, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from careerops.application.greenhouse_submit import (
    GREENHOUSE_BOARD_API_HOST,
    GreenhouseAttachmentRef,
    GreenhouseJobSchemaSnapshot,
    GreenhouseSubmissionPayload,
    GreenhouseTarget,
    qualify_greenhouse_submission,
)
from careerops.infrastructure.greenhouse.credentials import (
    GREENHOUSE_SUBMIT_OPERATION,
    GreenhouseCredentialHandle,
)

_BROKER_VERSION: Final = "careerops.greenhouse.submission-broker.v1"
_DEFAULT_MAX_SCHEMA_RESPONSE_BYTES: Final = 1_048_576
_DEFAULT_MAX_BROKER_RESPONSE_BYTES: Final = 65_536
_DEFAULT_MAX_BROKER_REQUEST_BYTES: Final = 28 * 1024 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"^[A-Z0-9_]{1,160}$")
_RECONCILIATION_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_PUBLIC_SCHEMA_PATH = re.compile(r"^/v1/boards/[a-z0-9][a-z0-9_-]{0,79}/jobs/[1-9][0-9]*$")
_BROKER_RESPONSE_PARSER_VERSION: Final = "greenhouse-job-board-response.v1"
_DETERMINISTIC_REJECTION_REASONS: Final = {
    400: frozenset({"GREENHOUSE_PROVIDER_VALIDATION_REJECTED"}),
    401: frozenset({"GREENHOUSE_PROVIDER_AUTH_REJECTED"}),
    403: frozenset({"GREENHOUSE_PROVIDER_AUTH_REJECTED"}),
    404: frozenset({"GREENHOUSE_PROVIDER_JOB_NOT_FOUND"}),
    413: frozenset({"GREENHOUSE_PROVIDER_REQUEST_TOO_LARGE"}),
    415: frozenset({"GREENHOUSE_PROVIDER_MEDIA_TYPE_REJECTED"}),
    422: frozenset({"GREENHOUSE_PROVIDER_VALIDATION_REJECTED"}),
}


class GreenhouseApiError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        *,
        status_code: int | None = None,
        reason_codes: Sequence[str] = (),
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.status_code = status_code
        self.reason_codes = tuple(reason_codes)


class GreenhouseBrokerPrePostUnavailable(GreenhouseApiError):
    """No Unix connection was established, so no request byte reached the broker."""

    retryable: Final = True
    request_may_have_reached_broker: Final = False


class GreenhouseSubmissionOutcome(StrEnum):
    ACCEPTED_UNVERIFIED = "accepted_unverified"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


class GreenhouseBrokerJournalState(StrEnum):
    POST_STARTED = "post_started"
    RESPONSE_OBSERVED = "response_observed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class GreenhouseResolvedAttachment:
    ref: GreenhouseAttachmentRef
    data: bytes

    def __post_init__(self) -> None:
        if len(self.data) != self.ref.size_bytes:
            raise ValueError("resolved attachment size does not match approved ref")
        if hashlib.sha256(self.data).hexdigest() != self.ref.sha256:
            raise ValueError("resolved attachment sha256 does not match approved ref")


@dataclass(frozen=True, slots=True)
class GreenhouseSubmissionEvidence:
    """Redacted evidence from one broker-owned provider attempt.

    No outcome from this transport is confirmed. ``accepted_unverified`` and ``ambiguous`` both
    require separately authorized employer-side or reviewed manual reconciliation.
    """

    outcome: GreenhouseSubmissionOutcome
    reason_code: str
    submission_identity: str
    reconciliation_key: str
    broker_request_sha256: str
    broker_response_sha256: str
    status_code: int | None
    journal_receipt_hash: str | None
    journal_state: GreenhouseBrokerJournalState | None
    journal_sequence: int | None
    expected_schema_hash: str
    payload_hash: str
    material_hash: str
    provider_request_sha256: str | None
    provider_response_sha256: str | None
    observed_schema_hash: str | None
    observed_schema_raw_response_sha256: str | None
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.submission_identity, "submission_identity"),
            (self.broker_request_sha256, "broker_request_sha256"),
            (self.broker_response_sha256, "broker_response_sha256"),
            (self.expected_schema_hash, "expected_schema_hash"),
            (self.payload_hash, "payload_hash"),
            (self.material_hash, "material_hash"),
        ):
            _validate_hash(value, field_name)
        if _REASON_CODE.fullmatch(self.reason_code) is None:
            raise ValueError("reason_code must be a bounded machine code")
        if _RECONCILIATION_KEY.fullmatch(self.reconciliation_key) is None:
            raise ValueError("reconciliation_key must be bounded")
        for value, field_name in (
            (self.journal_receipt_hash, "journal_receipt_hash"),
            (self.provider_request_sha256, "provider_request_sha256"),
            (self.provider_response_sha256, "provider_response_sha256"),
            (self.observed_schema_hash, "observed_schema_hash"),
            (
                self.observed_schema_raw_response_sha256,
                "observed_schema_raw_response_sha256",
            ),
        ):
            if value is not None:
                _validate_hash(value, field_name)
        if self.status_code is not None and _bounded_http_status(self.status_code) is None:
            raise ValueError("status_code must be a bounded HTTP status")
        if (self.journal_state is None) is not (self.journal_sequence is None):
            raise ValueError("journal state and sequence must be supplied together")
        if self.journal_sequence is not None and (
            isinstance(self.journal_sequence, bool) or self.journal_sequence <= 0
        ):
            raise ValueError("journal_sequence must be a positive integer")
        encoded = json.dumps(
            {
                "version": "greenhouse-broker-evidence.v1",
                "outcome": self.outcome.value,
                "reason_code": self.reason_code,
                "submission_identity": self.submission_identity,
                "reconciliation_key": self.reconciliation_key,
                "broker_request_sha256": self.broker_request_sha256,
                "broker_response_sha256": self.broker_response_sha256,
                "status_code": self.status_code,
                "journal_receipt_hash": self.journal_receipt_hash,
                "journal_state": self.journal_state.value if self.journal_state else None,
                "journal_sequence": self.journal_sequence,
                "expected_schema_hash": self.expected_schema_hash,
                "payload_hash": self.payload_hash,
                "material_hash": self.material_hash,
                "provider_request_sha256": self.provider_request_sha256,
                "provider_response_sha256": self.provider_response_sha256,
                "observed_schema_hash": self.observed_schema_hash,
                "observed_schema_raw_response_sha256": (self.observed_schema_raw_response_sha256),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(self, "evidence_hash", hashlib.sha256(encoded).hexdigest())

    @property
    def reconciliation_required(self) -> bool:
        return self.outcome is not GreenhouseSubmissionOutcome.REJECTED

    @property
    def next_attempt_allowed(self) -> bool:
        return False

    @property
    def journal_committed(self) -> bool:
        return self.journal_receipt_hash is not None and self.journal_state is not None

    @property
    def post_may_have_started(self) -> bool:
        return (
            self.journal_state
            in {
                GreenhouseBrokerJournalState.POST_STARTED,
                GreenhouseBrokerJournalState.RESPONSE_OBSERVED,
                GreenhouseBrokerJournalState.AMBIGUOUS,
            }
            or self.outcome is GreenhouseSubmissionOutcome.AMBIGUOUS
        )


class _HttpResponse(Protocol):
    @property
    def status(self) -> int | None: ...

    def read(self, amount: int = -1) -> bytes: ...

    def getcode(self) -> int: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


type _OpenRequest = Callable[[Request, float], _HttpResponse]


class GreenhouseJobBoardHttpClient:
    """Public schema GET only; provider POST and API keys are broker-owned."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_schema_response_bytes: int = _DEFAULT_MAX_SCHEMA_RESPONSE_BYTES,
        open_request: _OpenRequest | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("timeout_seconds must be between 0 and 30")
        if not 1024 <= max_schema_response_bytes <= 4 * 1024 * 1024:
            raise ValueError("max_schema_response_bytes must be between 1024 and 4194304")
        self._timeout_seconds = timeout_seconds
        self._max_schema_response_bytes = max_schema_response_bytes
        self._open_request = open_request or _open_fixed_origin_without_redirects

    def fetch_job_schema(self, target: GreenhouseTarget) -> GreenhouseJobSchemaSnapshot:
        request = Request(
            target.schema_endpoint,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "CareerOps/0.1"},
        )
        try:
            with self._open_request(request, self._timeout_seconds) as response:
                status_code = _response_status(response)
                response_body = response.read(self._max_schema_response_bytes + 1)
        except HTTPError as exc:
            status_code = _bounded_http_status(exc.code)
            error_code = (
                "GREENHOUSE_SCHEMA_REDIRECT_REJECTED"
                if status_code is not None and 300 <= status_code < 400
                else "GREENHOUSE_SCHEMA_HTTP_ERROR"
            )
            raise GreenhouseApiError(error_code, status_code=status_code) from None
        except (URLError, TimeoutError, OSError):
            raise GreenhouseApiError("GREENHOUSE_SCHEMA_TRANSPORT_ERROR") from None
        if len(response_body) > self._max_schema_response_bytes:
            raise GreenhouseApiError("GREENHOUSE_SCHEMA_RESPONSE_TOO_LARGE")
        if status_code < 200 or status_code >= 300:
            error_code = (
                "GREENHOUSE_SCHEMA_REDIRECT_REJECTED"
                if 300 <= status_code < 400
                else f"GREENHOUSE_SCHEMA_HTTP_{status_code}"
            )
            raise GreenhouseApiError(error_code, status_code=status_code)
        return GreenhouseJobSchemaSnapshot.from_json_bytes(
            target=target,
            raw_response=response_body,
        )


class UnixGreenhouseSubmissionBroker:
    """Secret-free RPC client for the key-owning, once-only Greenhouse broker."""

    def __init__(
        self,
        *,
        socket_path: Path,
        timeout_seconds: float = 30.0,
        max_request_bytes: int = _DEFAULT_MAX_BROKER_REQUEST_BYTES,
        max_response_bytes: int = _DEFAULT_MAX_BROKER_RESPONSE_BYTES,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._socket_path = Path(socket_path)
        if not self._socket_path.is_absolute():
            raise ValueError("submission broker socket path must be absolute")
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        if not 1024 <= max_request_bytes <= 32 * 1024 * 1024:
            raise ValueError("max_request_bytes must be between 1024 and 33554432")
        if not 256 <= max_response_bytes <= 1_048_576:
            raise ValueError("max_response_bytes must be between 256 and 1048576")
        self._timeout_seconds = timeout_seconds
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._now = now or (lambda: datetime.now(UTC))

    def submit(
        self,
        handle: GreenhouseCredentialHandle,
        *,
        schema: GreenhouseJobSchemaSnapshot,
        payload: GreenhouseSubmissionPayload,
        attachments: Sequence[GreenhouseResolvedAttachment],
        reconciliation_key: str,
    ) -> GreenhouseSubmissionEvidence:
        if _RECONCILIATION_KEY.fullmatch(reconciliation_key) is None:
            raise ValueError("reconciliation_key must be bounded")
        qualification = qualify_greenhouse_submission(schema=schema, payload=payload)
        if not qualification.can_submit:
            raise GreenhouseApiError(
                "GREENHOUSE_SUBMISSION_NOT_QUALIFIED",
                reason_codes=qualification.reason_codes,
            )
        if handle.is_expired(now=self._now()):
            raise GreenhouseApiError("GREENHOUSE_CREDENTIAL_PROFILE_EXPIRED", status_code=403)
        if handle.board_token != payload.target.board_token:
            raise GreenhouseApiError("GREENHOUSE_CREDENTIAL_BOARD_MISMATCH", status_code=403)
        if handle.allowed_operations != (GREENHOUSE_SUBMIT_OPERATION,):
            raise GreenhouseApiError("GREENHOUSE_CREDENTIAL_OPERATION_MISMATCH", status_code=403)
        _validate_resolved_attachments(payload, attachments)
        broker_request = _build_broker_request(
            handle=handle,
            schema=schema,
            payload=payload,
            attachments=attachments,
            reconciliation_key=reconciliation_key,
        )
        encoded = json.dumps(broker_request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self._max_request_bytes:
            raise GreenhouseApiError("GREENHOUSE_BROKER_REQUEST_TOO_LARGE")
        broker_request_hash = hashlib.sha256(encoded).hexdigest()

        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(self._timeout_seconds)
            try:
                client.connect(str(self._socket_path))
            except OSError:
                client.close()
                raise
        except OSError:
            raise GreenhouseBrokerPrePostUnavailable(
                "GREENHOUSE_BROKER_PREPOST_UNAVAILABLE"
            ) from None
        try:
            with client:
                client.sendall(encoded)
                try:
                    client.shutdown(socket.SHUT_WR)
                except OSError as exc:
                    if exc.errno not in {errno.ENOTCONN, errno.EPIPE}:
                        raise
                response_body, response_was_bounded = _recv_bounded(
                    client,
                    self._max_response_bytes,
                )
        except OSError:
            return _local_ambiguous_evidence(
                payload=payload,
                reconciliation_key=reconciliation_key,
                broker_request_hash=broker_request_hash,
                broker_response=b"",
                reason_code="GREENHOUSE_BROKER_TRANSPORT_AMBIGUOUS",
            )
        if not response_was_bounded:
            return _local_ambiguous_evidence(
                payload=payload,
                reconciliation_key=reconciliation_key,
                broker_request_hash=broker_request_hash,
                broker_response=response_body,
                reason_code="GREENHOUSE_BROKER_RESPONSE_TOO_LARGE_AMBIGUOUS",
            )
        return _parse_broker_response(
            response_body=response_body,
            schema=schema,
            payload=payload,
            reconciliation_key=reconciliation_key,
            broker_request_hash=broker_request_hash,
        )

    def __repr__(self) -> str:
        return (
            "UnixGreenhouseSubmissionBroker("
            f"socket_path={str(self._socket_path)!r}, "
            f"timeout_seconds={self._timeout_seconds!r}, "
            f"max_request_bytes={self._max_request_bytes!r}, "
            f"max_response_bytes={self._max_response_bytes!r})"
        )


class _DenyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


def _open_fixed_origin_without_redirects(request: Request, timeout: float) -> _HttpResponse:
    _validate_fixed_origin_request(request)
    opener: OpenerDirector = build_opener(ProxyHandler({}), _DenyRedirectHandler())
    return cast("_HttpResponse", opener.open(request, timeout=timeout))  # nosec B310


def _validate_fixed_origin_request(request: Request) -> None:
    parts = urlsplit(request.full_url)
    try:
        port = parts.port
    except ValueError as exc:
        raise GreenhouseApiError("GREENHOUSE_DYNAMIC_ORIGIN_REJECTED") from exc
    if (
        request.get_method() != "GET"
        or parts.scheme != "https"
        or parts.hostname != GREENHOUSE_BOARD_API_HOST
        or port not in {None, 443}
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or _PUBLIC_SCHEMA_PATH.fullmatch(parts.path) is None
        or "%" in parts.path
        or "//" in parts.path
        or any(ord(character) < 32 for character in request.full_url)
        or parts.query != "questions=true"
        or request.get_header("Authorization") is not None
    ):
        raise GreenhouseApiError("GREENHOUSE_DYNAMIC_ORIGIN_REJECTED")


def _build_broker_request(
    *,
    handle: GreenhouseCredentialHandle,
    schema: GreenhouseJobSchemaSnapshot,
    payload: GreenhouseSubmissionPayload,
    attachments: Sequence[GreenhouseResolvedAttachment],
    reconciliation_key: str,
) -> dict[str, object]:
    return {
        "version": _BROKER_VERSION,
        "provider": "greenhouse",
        "operation": "submit_application_once",
        "credential": handle.canonical_binding(),
        "destination": payload.target.canonical(),
        "expected_schema": {
            "schema_hash": schema.schema_hash,
            "raw_response_sha256": schema.raw_response_sha256,
            "internal_job_id": schema.internal_job_id,
            "updated_at": schema.updated_at,
            "application_deadline": schema.application_deadline,
        },
        "submission": {
            "submission_identity": payload.submission_identity,
            "reconciliation_key": reconciliation_key,
            "payload_hash": payload.payload_hash,
            "material_hash": payload.material_hash,
            "source_draft_payload_hash": payload.source_draft_payload_hash,
            "approved_material_hashes": list(payload.approved_material_hashes),
            "fields": _json_fields(payload.fields),
        },
        "attachments": [
            {
                "ref": attachment.ref.canonical(),
                "content_base64": base64.b64encode(attachment.data).decode("ascii"),
            }
            for attachment in attachments
        ],
    }


def _parse_broker_response(
    *,
    response_body: bytes,
    schema: GreenhouseJobSchemaSnapshot,
    payload: GreenhouseSubmissionPayload,
    reconciliation_key: str,
    broker_request_hash: str,
) -> GreenhouseSubmissionEvidence:
    try:
        decoded = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _local_ambiguous_evidence(
            payload=payload,
            reconciliation_key=reconciliation_key,
            broker_request_hash=broker_request_hash,
            broker_response=response_body,
            reason_code="GREENHOUSE_BROKER_RESPONSE_INVALID_AMBIGUOUS",
        )
    if not isinstance(decoded, dict):
        return _local_ambiguous_evidence(
            payload=payload,
            reconciliation_key=reconciliation_key,
            broker_request_hash=broker_request_hash,
            broker_response=response_body,
            reason_code="GREENHOUSE_BROKER_RESPONSE_INVALID_AMBIGUOUS",
        )
    mapping = cast("Mapping[str, object]", decoded)
    try:
        outcome = GreenhouseSubmissionOutcome(_required_str(mapping, "outcome"))
        reason_code = _required_str(mapping, "reason_code")
        if _REASON_CODE.fullmatch(reason_code) is None:
            raise ValueError("invalid reason code")
        if _required_str(mapping, "submission_identity") != payload.submission_identity:
            raise ValueError("submission identity mismatch")
        if _required_str(mapping, "reconciliation_key") != reconciliation_key:
            raise ValueError("reconciliation key mismatch")
        if _required_str(mapping, "broker_request_sha256") != broker_request_hash:
            raise ValueError("broker request hash mismatch")
        if _required_str(mapping, "parser_version") != _BROKER_RESPONSE_PARSER_VERSION:
            raise ValueError("broker parser version mismatch")
        if _required_str(mapping, "expected_schema_hash") != schema.schema_hash:
            raise ValueError("expected schema hash mismatch")
        if _required_str(mapping, "payload_hash") != payload.payload_hash:
            raise ValueError("payload hash mismatch")
        if _required_str(mapping, "material_hash") != payload.material_hash:
            raise ValueError("material hash mismatch")
        observed_schema_hash = _required_hash(mapping, "observed_schema_hash")
        if observed_schema_hash != schema.schema_hash:
            raise ValueError("observed schema hash mismatch")
        observed_raw_hash = _required_hash(mapping, "observed_schema_raw_response_sha256")
        if observed_raw_hash != schema.raw_response_sha256:
            raise ValueError("observed raw schema hash mismatch")
        if mapping.get("journal_committed") is not True:
            raise ValueError("broker journal was not committed")
        journal_state = GreenhouseBrokerJournalState(_required_str(mapping, "journal_state"))
        journal_sequence = _required_positive_int(mapping, "journal_sequence")
        if mapping.get("next_attempt_allowed") is not False:
            raise ValueError("broker response permits a retry")
        if outcome is not GreenhouseSubmissionOutcome.REJECTED and (
            mapping.get("reconciliation_required") is not True
        ):
            raise ValueError("broker response omitted mandatory reconciliation")
        status_code = _optional_status(mapping.get("status_code"))
        provider_request_hash = _optional_hash(mapping, "provider_request_sha256")
        provider_response_hash = _optional_hash(mapping, "provider_response_sha256")
        if outcome is GreenhouseSubmissionOutcome.ACCEPTED_UNVERIFIED:
            if status_code is None or not 200 <= status_code < 300:
                raise ValueError("accepted-unverified requires a 2xx status")
            if journal_state is not GreenhouseBrokerJournalState.RESPONSE_OBSERVED:
                raise ValueError("accepted-unverified requires response-observed journal state")
            if provider_request_hash is None or provider_response_hash is None:
                raise ValueError(
                    "accepted-unverified requires provider request and response hashes"
                )
        if outcome is GreenhouseSubmissionOutcome.REJECTED:
            allowed_reasons = (
                _DETERMINISTIC_REJECTION_REASONS.get(status_code)
                if status_code is not None
                else None
            )
            if allowed_reasons is None or reason_code not in allowed_reasons:
                raise ValueError("rejected requires a deterministic 4xx status")
            if journal_state is not GreenhouseBrokerJournalState.RESPONSE_OBSERVED:
                raise ValueError("rejected requires response-observed journal state")
            if provider_request_hash is None or provider_response_hash is None:
                raise ValueError("rejected requires provider request and response hashes")
        evidence = GreenhouseSubmissionEvidence(
            outcome=outcome,
            reason_code=reason_code,
            submission_identity=payload.submission_identity,
            reconciliation_key=reconciliation_key,
            broker_request_sha256=broker_request_hash,
            broker_response_sha256=hashlib.sha256(response_body).hexdigest(),
            status_code=status_code,
            journal_receipt_hash=_required_hash(mapping, "journal_receipt_hash"),
            journal_state=journal_state,
            journal_sequence=journal_sequence,
            expected_schema_hash=schema.schema_hash,
            payload_hash=payload.payload_hash,
            material_hash=payload.material_hash,
            provider_request_sha256=provider_request_hash,
            provider_response_sha256=provider_response_hash,
            observed_schema_hash=observed_schema_hash,
            observed_schema_raw_response_sha256=observed_raw_hash,
        )
    except (KeyError, TypeError, ValueError):
        return _local_ambiguous_evidence(
            payload=payload,
            reconciliation_key=reconciliation_key,
            broker_request_hash=broker_request_hash,
            broker_response=response_body,
            reason_code="GREENHOUSE_BROKER_RESPONSE_INVALID_AMBIGUOUS",
        )
    return evidence


def _local_ambiguous_evidence(
    *,
    payload: GreenhouseSubmissionPayload,
    reconciliation_key: str,
    broker_request_hash: str,
    broker_response: bytes,
    reason_code: str,
) -> GreenhouseSubmissionEvidence:
    return GreenhouseSubmissionEvidence(
        outcome=GreenhouseSubmissionOutcome.AMBIGUOUS,
        reason_code=reason_code,
        submission_identity=payload.submission_identity,
        reconciliation_key=reconciliation_key,
        broker_request_sha256=broker_request_hash,
        broker_response_sha256=hashlib.sha256(broker_response).hexdigest(),
        status_code=None,
        journal_receipt_hash=None,
        journal_state=None,
        journal_sequence=None,
        expected_schema_hash=payload.schema_hash,
        payload_hash=payload.payload_hash,
        material_hash=payload.material_hash,
        provider_request_sha256=None,
        provider_response_sha256=None,
        observed_schema_hash=None,
        observed_schema_raw_response_sha256=None,
    )


def _recv_bounded(client: socket.socket, max_bytes: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = client.recv(min(4096, max_bytes + 1 - received))
        if not chunk:
            return b"".join(chunks), True
        received += len(chunk)
        chunks.append(chunk)
        if received > max_bytes:
            return b"".join(chunks), False


def _validate_resolved_attachments(
    payload: GreenhouseSubmissionPayload,
    attachments: Sequence[GreenhouseResolvedAttachment],
) -> None:
    if tuple(item.ref for item in attachments) != payload.attachment_refs:
        raise ValueError("resolved attachments must exactly match approved payload refs")


def _json_fields(fields: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in fields.items():
        result[name] = (
            list(cast("tuple[object, ...]", value)) if isinstance(value, tuple) else value
        )
    return result


def _required_str(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing {field_name}")
    return value


def _required_hash(payload: Mapping[str, object], field_name: str) -> str:
    value = _required_str(payload, field_name)
    _validate_hash(value, field_name)
    return value


def _required_positive_int(payload: Mapping[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid {field_name}")
    return value


def _optional_hash(payload: Mapping[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid {field_name}")
    _validate_hash(value, field_name)
    return value


def _optional_status(value: object) -> int | None:
    if value is None:
        return None
    status = _bounded_http_status(value)
    if status is None:
        raise ValueError("invalid status_code")
    return status


def _response_status(response: _HttpResponse) -> int:
    raw_status: object = response.status
    if raw_status is None:
        raw_status = response.getcode()
    status = _bounded_http_status(raw_status)
    if status is None:
        raise GreenhouseApiError("GREENHOUSE_INVALID_HTTP_STATUS")
    return status


def _bounded_http_status(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 100 or value > 599:
        return None
    return value


def _validate_hash(value: str, field_name: str) -> None:
    if _HASH.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase sha256")


__all__ = [
    "GreenhouseApiError",
    "GreenhouseBrokerJournalState",
    "GreenhouseBrokerPrePostUnavailable",
    "GreenhouseJobBoardHttpClient",
    "GreenhouseResolvedAttachment",
    "GreenhouseSubmissionEvidence",
    "GreenhouseSubmissionOutcome",
    "UnixGreenhouseSubmissionBroker",
]
