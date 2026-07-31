"""Tests for OpenAPI schema: error response status codes declared on every path."""

from __future__ import annotations

from careerops.api.app import create_app


def _openapi() -> dict:
    # The console_auth_service parameter was removed with the session/CSRF
    # auth layer (auth-rm Task 9); the error-response OpenAPI machinery is
    # independent of it.
    app = create_app()
    return app.openapi()


class TestOpenApiErrorResponses:
    def test_every_path_declares_standard_error_codes(self) -> None:
        schema = _openapi()
        # 401 was removed with the auth-layer error codes (auth-rm sweep):
        # no handler can raise it anymore.
        required_statuses = {"403", "404", "409", "422", "429", "503"}
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
            "RATE_LIMITED",
            "CANDIDATE_PROFILE_REQUIRED",
            "NOT_FOUND",
            "CONFLICT",
            "DEPENDENCY_NOT_READY",
            "VALIDATION_ERROR",
            "BAD_REQUEST",
            "FORBIDDEN",
            "METHOD_NOT_ALLOWED",
            "INTERNAL_ERROR",
            "INVALID_STATE",
            "STALE_PAYLOAD",
            "UNAVAILABLE_DEPENDENCY",
            "DENIED_POLICY",
            "UNRESOLVED_EMAIL_LINK",
            "RECONCILIATION_REQUIRED",
            "PAYLOAD_TOO_LARGE",
            "SMART_INTAKE_DISABLED",
            "SMART_PREVIEW_NOT_FOUND",
            "SMART_PREVIEW_IN_PROGRESS",
            "IDEMPOTENCY_KEY_REUSED",
            "STALE_SMART_INTAKE_PREVIEW",
            "SMART_PREVIEW_EXPIRED",
        }
        assert set(code_enum) == expected_codes

    def test_401_is_no_longer_declared(self) -> None:
        """The auth-layer 401 response was removed with the login layer."""
        schema = _openapi()
        paths = schema.get("paths", {})
        first_path = next(iter(paths))
        first_method = next(
            m
            for m in paths[first_path]
            if m not in ("parameters", "summary", "description", "servers")
        )
        responses = paths[first_path][first_method]["responses"]
        assert "401" not in responses

    def test_429_is_marked_retryable(self) -> None:
        schema = _openapi()
        paths = schema.get("paths", {})
        first_path = next(iter(paths))
        first_method = next(
            m
            for m in paths[first_path]
            if m not in ("parameters", "summary", "description", "servers")
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
            m
            for m in paths[first_path]
            if m not in ("parameters", "summary", "description", "servers")
        )
        resp_503 = paths[first_path][first_method]["responses"]["503"]
        content = resp_503["content"]["application/json"]
        example = content.get("example", {})
        error = example.get("error", {})
        assert error.get("retryable") is True
