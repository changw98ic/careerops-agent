from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

_ACTOR = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_MAX_TEXT_LENGTH = 1_000


class AutopilotCommandRejected(RuntimeError):
    """A durable grant command could not be applied under the current authority state."""

    def __init__(self, reason_code: str) -> None:
        _validate_identifier(reason_code, "reason_code")
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class AutopilotGrantScopeDraft:
    """Immutable scope for one append-only campaign grant version.

    Expiry is intentionally validated for shape here, not against client-provided time.
    The database ``expires_at > created_at`` constraint is the final trusted server-time guard.
    """

    subject_actor: str
    allowed_action_kinds: Sequence[str]
    allowed_channels: Sequence[str]
    allowed_target_hosts: Sequence[str]
    material_hashes: Sequence[str]
    max_total_submissions: int
    max_daily_submissions: int
    max_per_company: int
    policy_ruleset_version: str
    release_version: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not _ACTOR.fullmatch(self.subject_actor):
            raise ValueError("subject_actor must be a bounded actor identifier")
        object.__setattr__(
            self,
            "allowed_action_kinds",
            _freeze_nonempty_identifiers(self.allowed_action_kinds, "allowed_action_kinds"),
        )
        object.__setattr__(
            self,
            "allowed_channels",
            _freeze_nonempty_identifiers(self.allowed_channels, "allowed_channels"),
        )
        object.__setattr__(
            self,
            "allowed_target_hosts",
            _freeze_nonempty_identifiers(self.allowed_target_hosts, "allowed_target_hosts"),
        )
        object.__setattr__(
            self,
            "material_hashes",
            _freeze_nonempty_hashes(self.material_hashes, "material_hashes"),
        )
        _validate_positive(self.max_total_submissions, "max_total_submissions")
        _validate_positive(self.max_daily_submissions, "max_daily_submissions")
        _validate_positive(self.max_per_company, "max_per_company")
        _validate_text(self.policy_ruleset_version, "policy_ruleset_version")
        _validate_text(self.release_version, "release_version")
        _validate_aware(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class CampaignDraft:
    """User-owned campaign metadata paired with the first grant version."""

    name: str
    objective: str
    criteria: Mapping[str, JsonValue] = field(default_factory=lambda: {})
    exclusions: Sequence[JsonValue] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_text(self.name, "name")
        _validate_text(self.objective, "objective")
        object.__setattr__(self, "criteria", dict(self.criteria))
        object.__setattr__(self, "exclusions", tuple(self.exclusions))


@dataclass(frozen=True, slots=True)
class CreateCampaignGrantCommand:
    actor_id: UUID
    campaign: CampaignDraft
    grant_scope: AutopilotGrantScopeDraft
    trace_id: str

    def __post_init__(self) -> None:
        _validate_scope_actor(self.grant_scope, self.actor_id)
        _validate_trace_id(self.trace_id)


@dataclass(frozen=True, slots=True)
class SupersedeGrantCommand:
    actor_id: UUID
    campaign_id: UUID
    current_grant_version_id: UUID
    replacement_scope: AutopilotGrantScopeDraft
    reason: str
    trace_id: str

    def __post_init__(self) -> None:
        _validate_scope_actor(self.replacement_scope, self.actor_id)
        _validate_text(self.reason, "reason")
        _validate_trace_id(self.trace_id)


@dataclass(frozen=True, slots=True)
class RevokeGrantCommand:
    actor_id: UUID
    grant_version_id: UUID
    reason: str
    trace_id: str

    def __post_init__(self) -> None:
        _validate_text(self.reason, "reason")
        _validate_trace_id(self.trace_id)


@dataclass(frozen=True, slots=True)
class CampaignGrantVersionRef:
    campaign_id: UUID
    grant_version_id: UUID
    version: int

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be positive")


@dataclass(frozen=True, slots=True)
class GrantRevocationResult:
    revocation_id: UUID
    grant_version_id: UUID
    campaign_id: UUID
    superseded_by_grant_version_id: UUID | None
    newly_created: bool


class AutopilotGrantCommandStore(Protocol):
    def create_campaign_with_grant(
        self,
        command: CreateCampaignGrantCommand,
    ) -> CampaignGrantVersionRef: ...

    def supersede_grant(
        self,
        command: SupersedeGrantCommand,
    ) -> CampaignGrantVersionRef: ...

    def revoke_grant(
        self,
        command: RevokeGrantCommand,
    ) -> GrantRevocationResult: ...


class AutopilotGrantCommandService:
    """Thin application service for durable grant commands.

    The service deliberately delegates all persistence to an injected store. It does not
    import outbox, browser, credential, provider, or side-effect modules.
    """

    def __init__(self, store: AutopilotGrantCommandStore) -> None:
        self._store = store

    def create_campaign_with_grant(
        self,
        command: CreateCampaignGrantCommand,
    ) -> CampaignGrantVersionRef:
        return self._store.create_campaign_with_grant(command)

    def supersede_grant(self, command: SupersedeGrantCommand) -> CampaignGrantVersionRef:
        return self._store.supersede_grant(command)

    def revoke_grant(self, command: RevokeGrantCommand) -> GrantRevocationResult:
        return self._store.revoke_grant(command)


def _freeze_nonempty_identifiers(values: Sequence[str], label: str) -> tuple[str, ...]:
    frozen = tuple(values)
    if not frozen:
        raise ValueError(f"{label} must not be empty")
    for value in frozen:
        _validate_identifier(value, label)
    if len(set(frozen)) != len(frozen):
        raise ValueError(f"{label} must not contain duplicates")
    return frozen


def _freeze_nonempty_hashes(values: Sequence[str], label: str) -> tuple[str, ...]:
    frozen = tuple(values)
    if not frozen:
        raise ValueError(f"{label} must not be empty")
    for value in frozen:
        if not _HASH.fullmatch(value):
            raise ValueError(f"{label} must contain lowercase sha256 hex digests")
    if len(set(frozen)) != len(frozen):
        raise ValueError(f"{label} must not contain duplicates")
    return frozen


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded machine identifier")


def _validate_positive(value: int, label: str) -> None:
    if value < 1:
        raise ValueError(f"{label} must be positive")


def _validate_text(value: str, label: str) -> None:
    if not value.strip() or len(value) > _MAX_TEXT_LENGTH:
        raise ValueError(f"{label} must be non-empty and bounded")


def _validate_trace_id(value: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("trace_id must be a bounded machine identifier")


def _validate_scope_actor(scope: AutopilotGrantScopeDraft, actor_id: UUID) -> None:
    if scope.subject_actor != str(actor_id):
        raise ValueError("grant scope subject_actor must match command actor_id")


def _validate_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
