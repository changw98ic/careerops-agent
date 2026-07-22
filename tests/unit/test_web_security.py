from __future__ import annotations

import logging

import pytest
from starlette.requests import Request

from careerops.web.security import (
    ConsoleWebSettings,
    OriginHostValidator,
    RequestOriginRejected,
)


def _request(*, host: str, origin: str | None = None) -> Request:
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/bootstrap",
            "headers": headers,
        }
    )


def _validator() -> OriginHostValidator:
    return OriginHostValidator(
        ConsoleWebSettings(
            allowed_hosts=frozenset({"localhost:8000"}),
            allowed_origins=frozenset({"http://localhost:8000"}),
        )
    )


def _captured_payload(caplog: pytest.LogCaptureFixture) -> str:
    pieces: list[str] = []
    for record in caplog.records:
        pieces.append(record.getMessage())
        for key in ("event", "reason", "request_reason"):
            value = getattr(record, key, None)
            if value is not None:
                pieces.append(str(value))
    return "\n".join(pieces)


def test_validate_mutation_accepts_allowed_host_and_origin() -> None:
    _validator().validate_mutation(_request(host="localhost:8000", origin="http://localhost:8000"))


def test_validate_mutation_rejects_disallowed_host_with_reason_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    with pytest.raises(RequestOriginRejected, match="Host"):
        _validator().validate_mutation(
            _request(host="evil.example:8000", origin="http://localhost:8000")
        )

    payload = _captured_payload(caplog)
    assert "request origin rejected" in payload
    assert "host_not_allowed" in payload
    assert "evil.example:8000" not in payload
    assert "http://localhost:8000" not in payload


def test_validate_mutation_rejects_missing_origin_with_reason_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    with pytest.raises(RequestOriginRejected, match="Origin"):
        _validator().validate_mutation(_request(host="localhost:8000"))

    payload = _captured_payload(caplog)
    assert "request origin rejected" in payload
    assert "origin_missing" in payload
    assert "profile=missing" in payload
    assert "localhost:8000" not in payload


def test_validate_mutation_rejects_disallowed_origin_with_reason_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    disallowed_origin = "https://evil.example/callback?token=secret"

    with pytest.raises(RequestOriginRejected, match="Origin"):
        _validator().validate_mutation(_request(host="localhost:8000", origin=disallowed_origin))

    payload = _captured_payload(caplog)
    assert "request origin rejected" in payload
    assert "origin_not_allowed" in payload
    assert "profile=unexpected_components" in payload
    assert disallowed_origin not in payload
    assert "evil.example" not in payload
    assert "secret" not in payload
    assert "localhost:8000" not in payload


def test_validate_mutation_rejects_opaque_origin_with_bounded_profile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    with pytest.raises(RequestOriginRejected, match="Origin"):
        _validator().validate_mutation(_request(host="localhost:8000", origin="null"))

    payload = _captured_payload(caplog)
    assert "origin_not_allowed" in payload
    assert "profile=opaque" in payload
