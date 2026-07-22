from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from careerops.infrastructure.greenhouse.credentials import (
    GREENHOUSE_CREDENTIAL_KIND,
    GREENHOUSE_SUBMIT_OPERATION,
    GreenhouseCredentialHandle,
)

NOW = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
OWNER_ID = UUID("00000000-0000-0000-0000-000000000123")


def handle(**overrides: object) -> GreenhouseCredentialHandle:
    values: dict[str, object] = {
        "opaque_handle": "greenhouse:acme:job-board:v3",
        "owner_user_id": OWNER_ID,
        "employer_key": "acme",
        "board_token": "acme",
        "credential_profile_version": "profile-v3",
        "credential_profile_hash": "a" * 64,
        "profile_status": "active",
        "profile_expires_at": NOW + timedelta(minutes=10),
        "allowed_operations": (GREENHOUSE_SUBMIT_OPERATION,),
    }
    values.update(overrides)
    return GreenhouseCredentialHandle(**values)  # type: ignore[arg-type]


def test_handle_binds_owner_employer_board_profile_status_expiry_and_operation() -> None:
    item = handle()

    assert item.canonical_binding() == {
        "credential_kind": GREENHOUSE_CREDENTIAL_KIND,
        "opaque_handle": "greenhouse:acme:job-board:v3",
        "owner_user_id": str(OWNER_ID),
        "employer_key": "acme",
        "board_token": "acme",
        "credential_profile_version": "profile-v3",
        "credential_profile_hash": "a" * 64,
        "profile_status": "active",
        "profile_expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "allowed_operations": [GREENHOUSE_SUBMIT_OPERATION],
    }
    assert item.is_expired(now=NOW) is False
    assert item.is_expired(now=NOW + timedelta(minutes=10)) is True


def test_handle_never_contains_or_renders_provider_api_key_material() -> None:
    item = handle(opaque_handle="opaque-do-not-render")

    rendered = repr(item)
    assert "opaque-do-not-render" not in rendered
    assert "<redacted>" in rendered
    assert "api_key" not in item.canonical_binding()
    assert "authorization" not in str(item.canonical_binding()).casefold()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"board_token": "internal"}, "canonical public"),
        ({"board_token": "acme/jobs"}, "canonical public"),
        ({"credential_profile_hash": "not-a-hash"}, "lowercase sha256"),
        ({"profile_status": "revoked"}, "must be active"),
        ({"allowed_operations": ("read_jobs",)}, "exactly submit_application"),
        (
            {"allowed_operations": (GREENHOUSE_SUBMIT_OPERATION, "read_jobs")},
            "exactly submit_application",
        ),
        ({"employer_key": "acme/employer"}, "bounded identifier"),
    ],
)
def test_handle_fails_closed_on_binding_or_scope_drift(
    override: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        handle(**override)


def test_handle_requires_timezone_aware_profile_expiry() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        handle(profile_expires_at=datetime(2026, 7, 21, 8, 10))
