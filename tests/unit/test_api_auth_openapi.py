"""Tests for OpenAPI schema: error response status codes declared on every path."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.api.app import create_app
from careerops.auth.service import ConsoleAuthService


def _openapi(auth_service: MagicMock | None = None) -> dict:
    app = create_app(console_auth_service=auth_service)
    return app.openapi()


class TestOpenApiErrorResponses:
    def test_every_path_declares_standard_error_codes(self) -> None:
        schema = _openapi()
        required_statuses = {"401", "403", "404", "409", "422", "429", "503"}
        paths = schema.get("paths", {})
        assert len(paths) > 0, "OpenAPI schema should have paths"

        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if method in ("parameters", "summary", "description", "servers"):
                    continue
                if not isinstance(operation, dict):
                    continue
                responses = operation.get("responses", {})
                for status in required_statuses:
                    assert status in responses, (
                        f"{method.upper()} {path} missing {status} response declaration"
                    )

    def test_error_response_schema_is_defined(self) -> None:
        schema = _openapi()
        components = schema.get("components", {})
        schemas = components.get("schemas", {})
        assert "ErrorResponse" in schemas
        error_schema = schemas["ErrorResponse"]
        props = error_schema.get("properties", {})
        assert "error" in props

    def test_error_codes_enum_is_complete(self) -> None:
        schema = _openapi()
        components = schema.get("components", {})
        schemas = components.get("schemas", {})
        error_props = schemas["ErrorResponse"]["properties"]["error"]["properties"]
        code_enum = error_props["code"]["enum"]
        expected_codes = {
            "UNAUTHORIZED",
            "CSRF_REJECTED",
            "INVALID_CREDENTIALS",
            "RATE_LIMITED",
            "BOOTSTRAP_CLOSED",
            "CANDIDATE_PROFILE_REQUIRED",
            "NOT_FOUND",
            "CONFLICT",
            "DEPENDENCY_NOT_READY",
            "VALIDATION_ERROR",
            "BAD_REQUEST",
            "FORBIDDEN",
            "METHOD_NOT_ALLOWED",
            "INTERNAL_ERROR",
        }
        assert set(code_enum) == expected_codes

    def test_401_response_has_both_unauthorized_and_invalid_credentials_examples(self) -> None:
        schema = _openapi()
        paths = schema.get("paths", {})
        first_path = next(iter(paths))
        first_method = next(
            m for m in paths[first_path] if m not in ("parameters", "summary", "description", "servers")
        )
        resp_401 = paths[first_path][first_method]["responses"]["401"]
        content = resp_401["content"]["application/json"]
        examples = content.get("examples", {})
        assert "unauthorized" in examples
        assert "invalid_credentials" in examples

    def test_429_is_marked_retryable(self) -> None:
        schema = _openapi()
        paths = schema.get("paths", {})
        first_path = next(iter(paths))
        first_method = next(
            m for m in paths[first_path] if m not in ("parameters", "summary", "description", "servers")
        )
        resp_429 = paths[first_path][first_method]["responses"]["429"]
        content = resp_429["content"]["application/json"]
        example = content.get("example", {})
        error = example.get("error", {})
        assert error.get("retryable") is True

    def test_503_is_marked_retryable(self) -> None:
        schema = _openapi()
        paths = schema.get("paths", {})
        first_path = next(iter(paths))
        first_method = next(
            m for m in paths[first_path] if m not in ("parameters", "summary", "description", "servers")
        )
        resp_503 = paths[first_path][first_method]["responses"]["503"]
        content = resp_503["content"]["application/json"]
        example = content.get("example", {})
        error = example.get("error", {})
        assert error.get("retryable") is True
