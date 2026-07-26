"""Contract tests for the business-lifecycle error codes (OpenSpec task 1.5).

Covers the six new error codes added by the end-to-end-career-application-loop
change: INVALID_STATE, STALE_PAYLOAD, UNAVAILABLE_DEPENDENCY, DENIED_POLICY,
UNRESOLVED_EMAIL_LINK, RECONCILIATION_REQUIRED.

Each test pins the (error_code, http_status, retryable) triple so a later
gate cannot silently re-map a business code to the wrong status. The envelope
shape (``ErrorResponse``) is exercised end-to-end through the FastAPI
exception handler to prove the new exceptions flow through the existing
``handle_careerops_exception`` path without breaking it.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from careerops.api.contracts import ErrorCode
from careerops.api.errors import (
    CareerOpsHTTPException,
    DeniedPolicyError,
    InvalidStateError,
    ReconciliationRequiredError,
    StalePayloadError,
    UnavailableDependencyError,
    UnresolvedEmailLinkError,
    handle_careerops_exception,
)

# (exception_class, error_code, expected_http_status, retryable)
BUSINESS_ERROR_CASES: tuple[tuple[type[CareerOpsHTTPException], ErrorCode, int, bool], ...] = (
    (InvalidStateError, ErrorCode.INVALID_STATE, 409, False),
    (StalePayloadError, ErrorCode.STALE_PAYLOAD, 409, False),
    (UnavailableDependencyError, ErrorCode.UNAVAILABLE_DEPENDENCY, 503, True),
    (DeniedPolicyError, ErrorCode.DENIED_POLICY, 403, False),
    (UnresolvedEmailLinkError, ErrorCode.UNRESOLVED_EMAIL_LINK, 409, False),
    (ReconciliationRequiredError, ErrorCode.RECONCILIATION_REQUIRED, 409, False),
)


@pytest.mark.parametrize(("exc_cls", "code", "status", "retryable"), BUSINESS_ERROR_CASES)
def test_business_error_status_code_and_code(
    exc_cls: type[CareerOpsHTTPException],
    code: ErrorCode,
    status: int,
    retryable: bool,
) -> None:
    """Each business exception maps to its pinned status code and ErrorCode."""
    exc = exc_cls()
    assert exc.error_code == code
    assert exc.status_code == status
    assert exc.retryable is retryable


@pytest.mark.parametrize(("exc_cls", "code", "status", "retryable"), BUSINESS_ERROR_CASES)
def test_business_error_custom_message_preserved(
    exc_cls: type[CareerOpsHTTPException],
    code: ErrorCode,
    status: int,
    retryable: bool,
) -> None:
    """A caller-supplied message wins over the class default."""
    exc = exc_cls("specific failure reason")
    assert exc.message == "specific failure reason"
    assert exc.error_code == code


@pytest.mark.parametrize(("exc_cls", "code", "status", "retryable"), BUSINESS_ERROR_CASES)
def test_business_error_envelope_via_handler(
    exc_cls: type[CareerOpsHTTPException],
    code: ErrorCode,
    status: int,
    retryable: bool,
) -> None:
    """Raising the exception flows through the existing handler into ErrorResponse."""
    app = FastAPI()

    @app.get("/_raise")
    def _raise() -> dict[str, str]:  # pragma: no cover - never reached
        raise exc_cls("boom", details={"k": "v"})

    # Reuse the shared handler so the test exercises the production code path.
    app.add_exception_handler(CareerOpsHTTPException, handle_careerops_exception)

    client = TestClient(app)
    response = client.get("/_raise")

    assert response.status_code == status
    body = response.json()
    assert set(body.keys()) == {"error"}
    error: Mapping[str, object] = body["error"]
    assert error["code"] == str(code)
    assert error["message"] == "boom"
    assert error["retryable"] is retryable
    assert error["details"] == {"k": "v"}
    assert "trace_id" in error
    assert isinstance(error["trace_id"], str)


def test_business_errors_are_distinct_from_generic_conflict() -> None:
    """INVALID_STATE must not collapse onto the generic CONFLICT code."""
    assert ErrorCode.INVALID_STATE != ErrorCode.CONFLICT
    assert ErrorCode.STALE_PAYLOAD != ErrorCode.CONFLICT
    assert ErrorCode.UNRESOLVED_EMAIL_LINK != ErrorCode.CONFLICT
    assert ErrorCode.RECONCILIATION_REQUIRED != ErrorCode.CONFLICT
    # DENIED_POLICY is distinct from the existing CANDIDATE_PROFILE_REQUIRED 403 code.
    assert ErrorCode.DENIED_POLICY != ErrorCode.CANDIDATE_PROFILE_REQUIRED
    # UNAVAILABLE_DEPENDENCY is distinct from the generic DEPENDENCY_NOT_READY 503 code.
    assert ErrorCode.UNAVAILABLE_DEPENDENCY != ErrorCode.DEPENDENCY_NOT_READY


def test_trace_id_unavailable_when_state_missing() -> None:
    """Handler degrades gracefully when request.state.trace_id is unset."""
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    request = Request(scope)

    import asyncio
    import json

    response = asyncio.run(handle_careerops_exception(request, DeniedPolicyError("denied")))
    assert response.status_code == 403
    parsed = json.loads(bytes(response.body))
    assert parsed["error"]["code"] == str(ErrorCode.DENIED_POLICY)
    assert parsed["error"]["trace_id"] == "unavailable"
