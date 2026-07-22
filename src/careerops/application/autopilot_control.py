"""Pure application contract for bounded-autopilot campaign control.

This module is intentionally side-effect free. It defines the immutable shapes that a
future database-backed adapter can serve to the authenticated console without importing
database engines, outbox publishers, browsers, credentials, or provider workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

_MAX_IDENTIFIER_LENGTH = 96
_MAX_DISPLAY_TEXT_LENGTH = 240


class AutopilotControlCapabilityState(StrEnum):
    DISABLED = "disabled"
    READ_ONLY = "read_only"
    AVAILABLE = "available"


class CampaignGrantStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class ReviewQueueTab(StrEnum):
    PENDING = "pending"
    EXCEPTIONS = "exceptions"
    RECOVERY = "recovery"


class ReviewResolutionMode(StrEnum):
    AGENT_APPROVABLE = "agent_approvable"
    MANUAL_ONLY = "manual_only"
    REMEDIATION_REQUIRED = "remediation_required"


class AutopilotCommandState(StrEnum):
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AutopilotControlCapability:
    state: AutopilotControlCapabilityState
    title_zh: str
    description_zh: str
    reason_code: str

    @property
    def enabled(self) -> bool:
        return self.state is not AutopilotControlCapabilityState.DISABLED

    def __post_init__(self) -> None:
        _validate_display_text(self.title_zh, "capability title_zh")
        _validate_display_text(self.description_zh, "capability description_zh")
        _validate_identifier(self.reason_code, "capability reason_code")


@dataclass(frozen=True, slots=True)
class CampaignGrantCard:
    grant_id: UUID
    campaign_id: UUID
    version: int
    status: CampaignGrantStatus
    title_zh: str
    scope_summary_zh: str
    action_kinds: tuple[str, ...]
    channels: tuple[str, ...]
    target_hosts: tuple[str, ...]
    approved_material_hashes: tuple[str, ...]
    max_submissions: int
    remaining_submissions: int
    expires_at: datetime
    created_at: datetime

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("campaign grant version must be positive")
        if self.max_submissions < 1:
            raise ValueError("campaign grant max_submissions must be positive")
        if self.remaining_submissions < 0:
            raise ValueError("campaign grant remaining_submissions must not be negative")
        if self.remaining_submissions > self.max_submissions:
            raise ValueError("campaign grant remaining_submissions exceeds max_submissions")
        _validate_display_text(self.title_zh, "campaign grant title_zh")
        _validate_display_text(self.scope_summary_zh, "campaign grant scope_summary_zh")
        _validate_aware_datetime(self.expires_at, "campaign grant expires_at")
        _validate_aware_datetime(self.created_at, "campaign grant created_at")
        object.__setattr__(
            self, "action_kinds", _freeze_identifiers(self.action_kinds, "action kind")
        )
        object.__setattr__(self, "channels", _freeze_identifiers(self.channels, "channel"))
        object.__setattr__(
            self, "target_hosts", _freeze_identifiers(self.target_hosts, "target host")
        )
        object.__setattr__(
            self,
            "approved_material_hashes",
            _freeze_hashes(self.approved_material_hashes, "approved material hash"),
        )


@dataclass(frozen=True, slots=True)
class CampaignGrantSnapshot:
    capability: AutopilotControlCapability
    generated_at: datetime
    grants: tuple[CampaignGrantCard, ...] = ()

    def __post_init__(self) -> None:
        _validate_aware_datetime(self.generated_at, "campaign grant snapshot generated_at")
        object.__setattr__(self, "grants", tuple(self.grants))


@dataclass(frozen=True, slots=True)
class ReviewQueueCount:
    tab: ReviewQueueTab
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("review queue count must not be negative")


@dataclass(frozen=True, slots=True)
class ReviewQueueItem:
    item_id: UUID
    tab: ReviewQueueTab
    resolution_mode: ReviewResolutionMode
    reason_code: str
    title_zh: str
    summary_zh: str
    created_at: datetime
    campaign_id: UUID | None = None
    grant_id: UUID | None = None
    action_intent_id: UUID | None = None
    target_host: str | None = None
    payload_hash: str | None = None

    @property
    def can_agent_continue_after_approval(self) -> bool:
        return self.resolution_mode is ReviewResolutionMode.AGENT_APPROVABLE

    def __post_init__(self) -> None:
        _validate_identifier(self.reason_code, "review item reason_code")
        _validate_display_text(self.title_zh, "review item title_zh")
        _validate_display_text(self.summary_zh, "review item summary_zh")
        _validate_aware_datetime(self.created_at, "review item created_at")
        if (
            self.resolution_mode is ReviewResolutionMode.MANUAL_ONLY
            and self.tab is ReviewQueueTab.PENDING
        ):
            raise ValueError("manual-only review items must not be listed as pending approvals")
        if self.target_host is not None:
            _validate_identifier(self.target_host, "review item target_host")
        if self.payload_hash is not None:
            _validate_sha256_hex(self.payload_hash, "review item payload_hash")


@dataclass(frozen=True, slots=True)
class ReviewQueueSnapshot:
    capability: AutopilotControlCapability
    generated_at: datetime
    active_tab: ReviewQueueTab
    counts: tuple[ReviewQueueCount, ...] = ()
    items: tuple[ReviewQueueItem, ...] = ()

    def __post_init__(self) -> None:
        _validate_aware_datetime(self.generated_at, "review queue snapshot generated_at")
        object.__setattr__(self, "counts", tuple(self.counts))
        object.__setattr__(self, "items", tuple(self.items))
        for item in self.items:
            if item.tab is not self.active_tab:
                raise ValueError("review queue snapshot items must match active_tab")


@dataclass(frozen=True, slots=True)
class AutopilotCommandResult:
    state: AutopilotCommandState
    reason_code: str
    title_zh: str
    description_zh: str

    def __post_init__(self) -> None:
        _validate_identifier(self.reason_code, "autopilot command reason_code")
        _validate_display_text(self.title_zh, "autopilot command title_zh")
        _validate_display_text(self.description_zh, "autopilot command description_zh")


class CampaignGrantSnapshotProvider(Protocol):
    async def campaign_grants(
        self,
        *,
        actor_id: UUID,
        now: datetime,
    ) -> CampaignGrantSnapshot: ...


class ReviewQueueSnapshotProvider(Protocol):
    async def review_queue(
        self,
        *,
        actor_id: UUID,
        tab: ReviewQueueTab,
        now: datetime,
    ) -> ReviewQueueSnapshot: ...


class AutopilotControlPlane(CampaignGrantSnapshotProvider, ReviewQueueSnapshotProvider, Protocol):
    async def request_grant_activation(
        self,
        *,
        actor_id: UUID,
        grant_id: UUID,
    ) -> AutopilotCommandResult: ...

    async def request_grant_revocation(
        self,
        *,
        actor_id: UUID,
        grant_id: UUID,
    ) -> AutopilotCommandResult: ...

    async def request_review_resolution(
        self,
        *,
        actor_id: UUID,
        review_item_id: UUID,
        mode: ReviewResolutionMode,
    ) -> AutopilotCommandResult: ...


class DisabledAutopilotControlPlane:
    """Fail-closed control plane used until the durable grant store is released."""

    def __init__(self, *, reason_code: str = "AUTOPILOT_CONTROL_PLANE_DISABLED") -> None:
        _validate_identifier(reason_code, "disabled control plane reason_code")
        self._reason_code = reason_code

    async def campaign_grants(
        self,
        *,
        actor_id: UUID,
        now: datetime,
    ) -> CampaignGrantSnapshot:
        del actor_id
        return CampaignGrantSnapshot(
            capability=self._capability(),
            generated_at=_validated_now(now),
            grants=(),
        )

    async def review_queue(
        self,
        *,
        actor_id: UUID,
        tab: ReviewQueueTab,
        now: datetime,
    ) -> ReviewQueueSnapshot:
        del actor_id
        return ReviewQueueSnapshot(
            capability=self._capability(),
            generated_at=_validated_now(now),
            active_tab=tab,
            counts=_empty_review_counts(),
            items=(),
        )

    async def request_grant_activation(
        self,
        *,
        actor_id: UUID,
        grant_id: UUID,
    ) -> AutopilotCommandResult:
        del actor_id, grant_id
        return _disabled_command_result(self._reason_code)

    async def request_grant_revocation(
        self,
        *,
        actor_id: UUID,
        grant_id: UUID,
    ) -> AutopilotCommandResult:
        del actor_id, grant_id
        return _disabled_command_result(self._reason_code)

    async def request_review_resolution(
        self,
        *,
        actor_id: UUID,
        review_item_id: UUID,
        mode: ReviewResolutionMode,
    ) -> AutopilotCommandResult:
        del actor_id, review_item_id, mode
        return _disabled_command_result(self._reason_code)

    def _capability(self) -> AutopilotControlCapability:
        return AutopilotControlCapability(
            state=AutopilotControlCapabilityState.DISABLED,
            title_zh="高自治求职未启用",
            description_zh="当前只开放只读占位视图, 不会创建授权、批准投递或触发外部动作。",
            reason_code=self._reason_code,
        )


def _empty_review_counts() -> tuple[ReviewQueueCount, ...]:
    return tuple(ReviewQueueCount(tab=tab, count=0) for tab in ReviewQueueTab)


def _disabled_command_result(reason_code: str) -> AutopilotCommandResult:
    return AutopilotCommandResult(
        state=AutopilotCommandState.REJECTED,
        reason_code=reason_code,
        title_zh="操作未启用",
        description_zh="高自治控制面仍处于禁用状态, 本请求未被执行。",
    )


def _validated_now(value: datetime) -> datetime:
    _validate_aware_datetime(value, "control plane now")
    return value


def _validate_aware_datetime(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _freeze_identifiers(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    frozen = tuple(values)
    if not frozen:
        raise ValueError(f"{label} list must not be empty")
    for value in frozen:
        _validate_identifier(value, label)
    return frozen


def _freeze_hashes(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    frozen = tuple(values)
    if not frozen:
        raise ValueError(f"{label} list must not be empty")
    for value in frozen:
        _validate_sha256_hex(value, label)
    return frozen


def _validate_identifier(value: str, label: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{label} must not be blank")
    if len(value) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{label} is too long")
    if any(character.isspace() for character in value):
        raise ValueError(f"{label} must not contain whitespace")


def _validate_display_text(value: str, label: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{label} must not be blank")
    if len(value) > _MAX_DISPLAY_TEXT_LENGTH:
        raise ValueError(f"{label} is too long")


def _validate_sha256_hex(value: str, label: str) -> None:
    if len(value) != 64 or not all(
        "0" <= character <= "9" or "a" <= character <= "f" for character in value
    ):
        raise ValueError(f"{label} must be a lowercase sha256 hex digest")


__all__ = [
    "AutopilotCommandResult",
    "AutopilotCommandState",
    "AutopilotControlCapability",
    "AutopilotControlCapabilityState",
    "AutopilotControlPlane",
    "CampaignGrantCard",
    "CampaignGrantSnapshot",
    "CampaignGrantSnapshotProvider",
    "CampaignGrantStatus",
    "DisabledAutopilotControlPlane",
    "ReviewQueueCount",
    "ReviewQueueItem",
    "ReviewQueueSnapshot",
    "ReviewQueueSnapshotProvider",
    "ReviewQueueTab",
    "ReviewResolutionMode",
]
