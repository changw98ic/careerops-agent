from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_MAX_REASON_LENGTH = 1_000


class AutopilotKillSwitchScopeType(StrEnum):
    GLOBAL = "global"
    CAMPAIGN = "campaign"
    PROVIDER = "provider"


class AutopilotKillSwitchState(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclass(frozen=True, slots=True)
class AutopilotKillSwitchScope:
    scope_type: AutopilotKillSwitchScopeType
    campaign_id: UUID | None = None
    provider: str | None = None

    @classmethod
    def global_scope(cls) -> AutopilotKillSwitchScope:
        return cls(AutopilotKillSwitchScopeType.GLOBAL)

    @classmethod
    def campaign(cls, campaign_id: UUID) -> AutopilotKillSwitchScope:
        return cls(AutopilotKillSwitchScopeType.CAMPAIGN, campaign_id=campaign_id)

    @classmethod
    def provider_scope(cls, provider: str) -> AutopilotKillSwitchScope:
        return cls(AutopilotKillSwitchScopeType.PROVIDER, provider=provider)

    @property
    def stable_key(self) -> str:
        if self.scope_type is AutopilotKillSwitchScopeType.GLOBAL:
            return "global"
        if self.scope_type is AutopilotKillSwitchScopeType.CAMPAIGN:
            return f"campaign:{self.campaign_id}"
        return f"provider:{self.provider}"

    def __post_init__(self) -> None:
        if self.scope_type is AutopilotKillSwitchScopeType.GLOBAL:
            if self.campaign_id is not None or self.provider is not None:
                raise ValueError("global kill-switch scope must not include identifiers")
            return
        if self.scope_type is AutopilotKillSwitchScopeType.CAMPAIGN:
            if self.campaign_id is None or self.provider is not None:
                raise ValueError("campaign kill-switch scope requires only campaign_id")
            return
        if self.campaign_id is not None or self.provider is None:
            raise ValueError("provider kill-switch scope requires only provider")
        _validate_identifier(self.provider, "provider")


@dataclass(frozen=True, slots=True)
class SetAutopilotKillSwitchCommand:
    actor_id: UUID
    scope: AutopilotKillSwitchScope
    state: AutopilotKillSwitchState
    reason: str
    trace_id: str

    @property
    def active(self) -> bool:
        return self.state is AutopilotKillSwitchState.ACTIVE

    @property
    def idempotency_key(self) -> str:
        reason_hash = hashlib.sha256(self.reason.encode("utf-8")).hexdigest()
        return (
            "autopilot-kill-switch:"
            f"{self.scope.stable_key}:{self.state.value}:{self.actor_id}:{self.trace_id}:"
            f"{reason_hash}"
        )

    def __post_init__(self) -> None:
        _validate_reason(self.reason)
        _validate_identifier(self.trace_id, "trace_id")


@dataclass(frozen=True, slots=True)
class AutopilotKillSwitchEventRef:
    event_id: UUID | int | None
    scope: AutopilotKillSwitchScope
    state: AutopilotKillSwitchState
    newly_created: bool


@dataclass(frozen=True, slots=True)
class CurrentAutopilotKillSwitchState:
    scope: AutopilotKillSwitchScope
    state: AutopilotKillSwitchState
    reason: str | None = None
    actor_id: UUID | None = None
    event_id: UUID | int | None = None
    changed_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.state is AutopilotKillSwitchState.ACTIVE

    def __post_init__(self) -> None:
        if self.reason is not None:
            _validate_reason(self.reason)
        if self.changed_at is not None:
            _validate_aware_datetime(self.changed_at, "changed_at")


class AutopilotKillSwitchStore(Protocol):
    def append_event(
        self,
        command: SetAutopilotKillSwitchCommand,
    ) -> AutopilotKillSwitchEventRef: ...

    def current_state(
        self,
        scope: AutopilotKillSwitchScope,
    ) -> CurrentAutopilotKillSwitchState: ...


class AutopilotKillSwitchService:
    """Application-only kill-switch surface with no provider or model authority."""

    def __init__(self, store: AutopilotKillSwitchStore) -> None:
        self._store = store

    def activate(self, command: SetAutopilotKillSwitchCommand) -> AutopilotKillSwitchEventRef:
        if command.state is not AutopilotKillSwitchState.ACTIVE:
            raise ValueError("activate requires an active kill-switch command")
        return self._store.append_event(command)

    def deactivate(self, command: SetAutopilotKillSwitchCommand) -> AutopilotKillSwitchEventRef:
        if command.state is not AutopilotKillSwitchState.INACTIVE:
            raise ValueError("deactivate requires an inactive kill-switch command")
        return self._store.append_event(command)

    def current_state(
        self,
        scope: AutopilotKillSwitchScope,
    ) -> CurrentAutopilotKillSwitchState:
        return self._store.current_state(scope)


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded machine identifier")


def _validate_reason(value: str) -> None:
    if not value.strip() or len(value) > _MAX_REASON_LENGTH:
        raise ValueError("reason must be non-empty and bounded")


def _validate_aware_datetime(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = [
    "AutopilotKillSwitchEventRef",
    "AutopilotKillSwitchScope",
    "AutopilotKillSwitchScopeType",
    "AutopilotKillSwitchService",
    "AutopilotKillSwitchState",
    "AutopilotKillSwitchStore",
    "CurrentAutopilotKillSwitchState",
    "SetAutopilotKillSwitchCommand",
]
