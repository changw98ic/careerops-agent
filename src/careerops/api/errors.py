from __future__ import annotations

from http import HTTPStatus
from typing import cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import JsonValue
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from careerops.api.contracts import ErrorBody, ErrorResponse

_HTTP_ERROR_CODES: dict[int, str] = {
    HTTPStatus.BAD_REQUEST.value: "BAD_REQUEST",
    HTTPStatus.UNAUTHORIZED.value: "UNAUTHORIZED",
    HTTPStatus.FORBIDDEN.value: "FORBIDDEN",
    HTTPStatus.NOT_FOUND.value: "NOT_FOUND",
    HTTPStatus.METHOD_NOT_ALLOWED.value: "METHOD_NOT_ALLOWED",
    HTTPStatus.CONFLICT.value: "CONFLICT",
    HTTPStatus.UNPROCESSABLE_ENTITY.value: "VALIDATION_ERROR",
    HTTPStatus.TOO_MANY_REQUESTS.value: "RATE_LIMITED",
}

_HTTP_ERROR_MESSAGES: dict[int, str] = {
    HTTPStatus.NOT_FOUND.value: "Resource not found",
    HTTPStatus.METHOD_NOT_ALLOWED.value: "Method not allowed",
}


def _trace_id(request: Request) -> str:
    value = getattr(request.state, "trace_id", None)
    return value if isinstance(value, str) else "unavailable"


def _response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: JsonValue = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details,
            trace_id=_trace_id(request),
        )
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


async def handle_http_exception(request: Request, error: Exception) -> JSONResponse:
    exception = cast("HTTPException", error)
    status_code = exception.status_code
    code = _HTTP_ERROR_CODES.get(status_code, "HTTP_ERROR")
    message = _HTTP_ERROR_MESSAGES.get(status_code, "Request could not be processed")
    return _response(request, status_code=status_code, code=code, message=message)


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
    return _response(
        request,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details=cast("JsonValue", {"issues": issues}),
    )


async def handle_unexpected_error(request: Request, _error: Exception) -> JSONResponse:
    return _response(
        request,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        code="INTERNAL_ERROR",
        message="An internal error occurred",
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
