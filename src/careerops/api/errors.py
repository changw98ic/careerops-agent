from __future__ import annotations

from http import HTTPStatus
from typing import cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import JsonValue
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from careerops.api.contracts import ErrorBody, ErrorCode, ErrorResponse

# ---------------------------------------------------------------------------
# Domain exceptions — raise these in route handlers / dependencies
# ---------------------------------------------------------------------------


class CareerOpsHTTPException(HTTPException):
    """Base class for all CareerOps domain HTTP exceptions."""

    error_code: ErrorCode = ErrorCode.INTERNAL_ERROR
    retryable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        details: JsonValue = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=self._status_code(), detail=message, headers=headers)
        self.message = message
        self.details = details

    @classmethod
    def _status_code(cls) -> int:
        raise NotImplementedError


class UnauthorizedError(CareerOpsHTTPException):
    error_code = ErrorCode.UNAUTHORIZED
    message_default = "Authentication required"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.UNAUTHORIZED

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class CSRFRejectedError(CareerOpsHTTPException):
    error_code = ErrorCode.CSRF_REJECTED
    message_default = "CSRF token rejected"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.FORBIDDEN

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class InvalidCredentialsError(CareerOpsHTTPException):
    error_code = ErrorCode.INVALID_CREDENTIALS
    message_default = "Invalid credentials"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.UNAUTHORIZED

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class RateLimitedError(CareerOpsHTTPException):
    error_code = ErrorCode.RATE_LIMITED
    retryable = True
    message_default = "Too many requests"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.TOO_MANY_REQUESTS

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class BootstrapClosedError(CareerOpsHTTPException):
    error_code = ErrorCode.BOOTSTRAP_CLOSED
    message_default = "Bootstrap is no longer available"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.FORBIDDEN

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class CandidateProfileRequiredError(CareerOpsHTTPException):
    error_code = ErrorCode.CANDIDATE_PROFILE_REQUIRED
    message_default = "Candidate profile is required"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.FORBIDDEN

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class SmartIntakeDisabledError(CareerOpsHTTPException):
    error_code = ErrorCode.SMART_INTAKE_DISABLED
    message_default = "Smart intake is disabled"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.FORBIDDEN

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class SmartPreviewNotFoundError(CareerOpsHTTPException):
    error_code = ErrorCode.SMART_PREVIEW_NOT_FOUND
    message_default = "Smart intake preview not found"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.NOT_FOUND

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class SmartPreviewInProgressError(CareerOpsHTTPException):
    error_code = ErrorCode.SMART_PREVIEW_IN_PROGRESS
    message_default = "An equivalent smart intake preview is already running"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.CONFLICT

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class IdempotencyKeyReusedError(CareerOpsHTTPException):
    error_code = ErrorCode.IDEMPOTENCY_KEY_REUSED
    message_default = "Idempotency key was reused with different input"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.CONFLICT

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class StaleSmartIntakePreviewError(CareerOpsHTTPException):
    error_code = ErrorCode.STALE_SMART_INTAKE_PREVIEW
    message_default = "The form changed after this preview was created"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.CONFLICT

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class SmartPreviewExpiredError(CareerOpsHTTPException):
    error_code = ErrorCode.SMART_PREVIEW_EXPIRED
    message_default = "Smart intake preview has expired"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.GONE

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class NotFoundError(CareerOpsHTTPException):
    error_code = ErrorCode.NOT_FOUND
    message_default = "Resource not found"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.NOT_FOUND

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class ConflictError(CareerOpsHTTPException):
    error_code = ErrorCode.CONFLICT
    message_default = "Resource conflict"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.CONFLICT

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class DependencyNotReadyError(CareerOpsHTTPException):
    error_code = ErrorCode.DEPENDENCY_NOT_READY
    retryable = True
    message_default = "Service dependency is not ready"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.SERVICE_UNAVAILABLE

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Business-lifecycle exceptions (end-to-end-career-application-loop, task 1.5)
#
# These map the new ErrorCode values to fixed HTTP statuses. They are raised
# by route handlers / domain services and converted to the standard
# ErrorResponse envelope by ``handle_careerops_exception``. The generic
# HTTPException fallback mapping below is intentionally left untouched: a
# bare status code cannot distinguish these business codes, so callers must
# raise the typed exception to get the precise code.
# ---------------------------------------------------------------------------


class InvalidStateError(CareerOpsHTTPException):
    """Illegal application/package lifecycle transition or state mutation (HTTP 409)."""

    error_code = ErrorCode.INVALID_STATE
    message_default = "Operation not permitted in current state"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.CONFLICT

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class StalePayloadError(CareerOpsHTTPException):
    """Payload, package version, or confirmation binding changed since approval (HTTP 409)."""

    error_code = ErrorCode.STALE_PAYLOAD
    message_default = "Payload has changed since confirmation"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.CONFLICT

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class UnavailableDependencyError(CareerOpsHTTPException):
    """A required policy/provider/credential dependency is unavailable (HTTP 503, retryable)."""

    error_code = ErrorCode.UNAVAILABLE_DEPENDENCY
    retryable = True
    message_default = "Required dependency is unavailable"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.SERVICE_UNAVAILABLE

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class DeniedPolicyError(CareerOpsHTTPException):
    """Action denied by policy evaluation; capability/recipient/evidence check failed (HTTP 403)."""

    error_code = ErrorCode.DENIED_POLICY
    message_default = "Action denied by policy"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.FORBIDDEN

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class UnresolvedEmailLinkError(CareerOpsHTTPException):
    """Email thread matches multiple applications and needs user resolution (HTTP 409)."""

    error_code = ErrorCode.UNRESOLVED_EMAIL_LINK
    message_default = "Email thread has unresolved application link"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.CONFLICT

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class ReconciliationRequiredError(CareerOpsHTTPException):
    """Provider outcome ambiguous; blind retry disabled pending reconciliation (HTTP 409)."""

    error_code = ErrorCode.RECONCILIATION_REQUIRED
    message_default = "Outcome requires reconciliation before retry"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.CONFLICT

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class PayloadTooLargeError(CareerOpsHTTPException):
    """Upload payload exceeded the configured size limit (HTTP 413).

    Used by the early Content-Length guard in upload routes so an oversize
    request is rejected BEFORE any byte is buffered into memory. The
    post-read size validation in the service layer stays as defense-in-depth.
    """

    error_code = ErrorCode.PAYLOAD_TOO_LARGE
    message_default = "Payload too large"

    @classmethod
    def _status_code(cls) -> int:
        return HTTPStatus.REQUEST_ENTITY_TOO_LARGE

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Internal mapping for generic HTTPException fallback
# ---------------------------------------------------------------------------

_HTTP_ERROR_CODES: dict[int, ErrorCode] = {
    HTTPStatus.BAD_REQUEST.value: ErrorCode.BAD_REQUEST,
    HTTPStatus.UNAUTHORIZED.value: ErrorCode.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN.value: ErrorCode.FORBIDDEN,
    HTTPStatus.NOT_FOUND.value: ErrorCode.NOT_FOUND,
    HTTPStatus.METHOD_NOT_ALLOWED.value: ErrorCode.METHOD_NOT_ALLOWED,
    HTTPStatus.CONFLICT.value: ErrorCode.CONFLICT,
    HTTPStatus.UNPROCESSABLE_ENTITY.value: ErrorCode.VALIDATION_ERROR,
    HTTPStatus.TOO_MANY_REQUESTS.value: ErrorCode.RATE_LIMITED,
    HTTPStatus.SERVICE_UNAVAILABLE.value: ErrorCode.DEPENDENCY_NOT_READY,
}

_HTTP_ERROR_MESSAGES: dict[int, str] = {
    HTTPStatus.NOT_FOUND.value: "Resource not found",
    HTTPStatus.METHOD_NOT_ALLOWED.value: "Method not allowed",
    HTTPStatus.SERVICE_UNAVAILABLE.value: "Service dependency is not ready",
}


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _trace_id(request: Request) -> str:
    value = getattr(request.state, "trace_id", None)
    return value if isinstance(value, str) else "unavailable"


def error_response(
    request: Request,
    *,
    status_code: int,
    code: ErrorCode,
    message: str,
    retryable: bool = False,
    details: JsonValue = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            retryable=retryable,
            details=details,
            trace_id=_trace_id(request),
        )
    )
    headers = (
        {"Cache-Control": "no-store"}
        if request.url.path.startswith("/api/v1/smart-intake/")
        else None
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


async def handle_careerops_exception(request: Request, error: Exception) -> JSONResponse:
    exc = cast("CareerOpsHTTPException", error)
    return error_response(
        request,
        status_code=exc.status_code,
        code=exc.error_code,
        message=exc.message or str(exc.detail),
        retryable=exc.retryable,
        details=exc.details,
    )


async def handle_http_exception(request: Request, error: Exception) -> JSONResponse:
    exception = cast("HTTPException", error)
    status_code = exception.status_code
    code = _HTTP_ERROR_CODES.get(status_code, ErrorCode.INTERNAL_ERROR)
    message = _HTTP_ERROR_MESSAGES.get(status_code, "Request could not be processed")
    retryable = status_code in (
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.SERVICE_UNAVAILABLE,
    )
    return error_response(
        request,
        status_code=status_code,
        code=code,
        message=message,
        retryable=retryable,
    )


async def handle_validation_error(request: Request, error: Exception) -> JSONResponse:
    exception = cast("RequestValidationError", error)
    issues = [
        {
            "location": [str(part) for part in item.get("loc", ())],
            "type": str(item.get("type", "validation_error")),
            "message": str(item.get("msg", "Invalid value")),
        }
        for item in exception.errors()
    ]
    return error_response(
        request,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code=ErrorCode.VALIDATION_ERROR,
        message="Request validation failed",
        details=cast("JsonValue", {"issues": issues}),
    )


async def handle_unexpected_error(request: Request, _error: Exception) -> JSONResponse:
    return error_response(
        request,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        code=ErrorCode.INTERNAL_ERROR,
        message="An internal error occurred",
    )


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(CareerOpsHTTPException, handle_careerops_exception)
    app.add_exception_handler(HTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
