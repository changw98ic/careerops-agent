from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

GREENHOUSE_CREDENTIAL_KIND: Final = "greenhouse_job_board_submit_credential"
GREENHOUSE_SUBMIT_OPERATION: Final = "submit_application"

_BOARD_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_EMPLOYER_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_HANDLE = re.compile(r"^[A-Za-z0-9._:@/-]{1,255}$")
_PROFILE_VERSION = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class GreenhouseCredentialBrokerError(RuntimeError):
    """A Greenhouse submission broker request or redacted response was unusable."""


@dataclass(frozen=True, slots=True)
class GreenhouseCredentialHandle:
    """Secret-free binding to a broker-owned Greenhouse Job Board API key.

    The provider key is intentionally absent. The external Unix-socket broker owns the key,
    fixed-origin provider requests, and its once-only attempt journal.
    """

    opaque_handle: str
    owner_user_id: UUID
    employer_key: str
    board_token: str
    credential_profile_version: str
    credential_profile_hash: str
    profile_status: str
    profile_expires_at: datetime
    allowed_operations: tuple[str, ...]

    def __post_init__(self) -> None:
        if _OPAQUE_HANDLE.fullmatch(self.opaque_handle) is None:
            raise ValueError("opaque_handle must be a bounded opaque reference")
        _validate_identifier(self.employer_key, _EMPLOYER_KEY, "employer_key")
        _validate_board_token(self.board_token)
        _validate_identifier(
            self.credential_profile_version,
            _PROFILE_VERSION,
            "credential_profile_version",
        )
        if _HASH.fullmatch(self.credential_profile_hash) is None:
            raise ValueError("credential_profile_hash must be a lowercase sha256")
        if self.profile_status != "active":
            raise ValueError("Greenhouse credential profile must be active")
        _validate_aware(self.profile_expires_at, "profile_expires_at")
        normalized = tuple(
            sorted(
                {operation.strip() for operation in self.allowed_operations if operation.strip()}
            )
        )
        if normalized != (GREENHOUSE_SUBMIT_OPERATION,):
            raise ValueError("Greenhouse credentials must allow exactly submit_application")
        object.__setattr__(self, "allowed_operations", normalized)

    def is_expired(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        _validate_aware(current, "now")
        return self.profile_expires_at <= current

    def canonical_binding(self) -> dict[str, object]:
        return {
            "credential_kind": GREENHOUSE_CREDENTIAL_KIND,
            "opaque_handle": self.opaque_handle,
            "owner_user_id": str(self.owner_user_id),
            "employer_key": self.employer_key,
            "board_token": self.board_token,
            "credential_profile_version": self.credential_profile_version,
            "credential_profile_hash": self.credential_profile_hash,
            "profile_status": self.profile_status,
            "profile_expires_at": self.profile_expires_at.isoformat(),
            "allowed_operations": list(self.allowed_operations),
        }

    def __repr__(self) -> str:
        return (
            "GreenhouseCredentialHandle("
            "opaque_handle=<redacted>, "
            f"owner_user_id={str(self.owner_user_id)!r}, "
            f"employer_key={self.employer_key!r}, "
            f"board_token={self.board_token!r}, "
            f"credential_profile_version={self.credential_profile_version!r}, "
            f"credential_profile_hash={self.credential_profile_hash!r}, "
            f"profile_status={self.profile_status!r}, "
            f"profile_expires_at={self.profile_expires_at.isoformat()!r}, "
            f"allowed_operations={self.allowed_operations!r})"
        )


def _validate_board_token(value: str) -> None:
    # ``internal`` is Greenhouse's reserved board token, not password material.
    if _BOARD_TOKEN.fullmatch(value) is None or value == "internal":
        raise ValueError("board_token must be a canonical public Greenhouse board token")


def _validate_identifier(value: str, pattern: re.Pattern[str], field_name: str) -> None:
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")


def _validate_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = [
    "GREENHOUSE_CREDENTIAL_KIND",
    "GREENHOUSE_SUBMIT_OPERATION",
    "GreenhouseCredentialBrokerError",
    "GreenhouseCredentialHandle",
]
