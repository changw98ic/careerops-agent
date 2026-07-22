from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")
_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9._/@+-]{1,260}$")
_EVENT_PREFIX = "crawler-execution"


class CrawlerExecutionApprovalOutcome(StrEnum):
    APPROVE = "approved"
    REJECT = "rejected"


class CrawlerExecutionDecisionState(StrEnum):
    ENQUEUE_WORKFLOW_SIGNAL = "enqueue_workflow_signal"
    REJECTED = "rejected"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class CrawlerExecutionRequestDraft:
    """Immutable server-side reference to a reviewed local crawler request document."""

    request_id: UUID
    owner_user_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    payload_hash: str
    manifest_path: str
    request_artifact_path: str
    manifest_sha256: str
    request_sha256: str
    reviewed_plan_sha256: str
    source_ids: Sequence[str]
    reason: str
    created_at: datetime
    expires_at: datetime
    execution_key: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_hash(self.payload_hash, "payload_hash")
        object.__setattr__(self, "manifest_path", _validate_manifest_path(self.manifest_path))
        object.__setattr__(
            self,
            "request_artifact_path",
            _validate_review_artifact_path(self.request_artifact_path),
        )
        _validate_hash(self.manifest_sha256, "manifest_sha256")
        _validate_hash(self.request_sha256, "request_sha256")
        _validate_hash(self.reviewed_plan_sha256, "reviewed_plan_sha256")
        source_ids = _freeze_nonempty_identifiers(self.source_ids, "source_ids")
        _validate_text(self.reason, "reason")
        _validate_aware(self.created_at, "created_at")
        _validate_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("crawler execution request must expire after it is created")
        object.__setattr__(self, "source_ids", source_ids)
        object.__setattr__(
            self,
            "execution_key",
            _stable_key(
                "dispatch",
                self.request_id,
                self.action_intent_id,
                self.payload_version_id,
                self.payload_hash,
                self.request_sha256,
                self.reviewed_plan_sha256,
            ),
        )

    @property
    def idempotency_key(self) -> str:
        return f"crawler-execution-request:{self.execution_key}"

    @property
    def hash_bindings(self) -> MappingProxyType[str, str]:
        return MappingProxyType(
            {
                "manifest": self.manifest_sha256,
                "request": self.request_sha256,
                "reviewed_plan": self.reviewed_plan_sha256,
                "payload": self.payload_hash,
            }
        )

    @property
    def target(self) -> MappingProxyType[str, str]:
        return MappingProxyType(
            {
                "manifest_path": self.manifest_path,
                "request_artifact_path": self.request_artifact_path,
            }
        )

    @property
    def payload(self) -> MappingProxyType[str, object]:
        return MappingProxyType(
            {
                "version": "crawler-execution-request.v1",
                "provider_execution": "disabled_until_outbox_worker",
                "manifest_sha256": self.manifest_sha256,
                "request_sha256": self.request_sha256,
                "reviewed_plan_sha256": self.reviewed_plan_sha256,
                "source_ids": list(self.source_ids),
                "reason": self.reason,
            }
        )


@dataclass(frozen=True, slots=True)
class CrawlerExecutionApprovalDraft:
    """Authenticated server-side approval/reject decision for one request."""

    request_id: UUID
    outcome: CrawlerExecutionApprovalOutcome
    decided_by_user_id: UUID | None
    decision_reason: str
    decided_at: datetime
    approval_artifact_path: str | None = None
    approval_artifact_sha256: str | None = None
    local_cli_approval_present: bool = False

    def __post_init__(self) -> None:
        _validate_text(self.decision_reason, "decision_reason")
        _validate_aware(self.decided_at, "decided_at")
        if self.outcome is CrawlerExecutionApprovalOutcome.APPROVE:
            if self.approval_artifact_path is None or self.approval_artifact_sha256 is None:
                raise ValueError("approved crawler execution requires an approval artifact")
            object.__setattr__(
                self,
                "approval_artifact_path",
                _validate_review_artifact_path(self.approval_artifact_path),
            )
            _validate_hash(self.approval_artifact_sha256, "approval_artifact_sha256")
        elif self.approval_artifact_path is not None or self.approval_artifact_sha256 is not None:
            raise ValueError("rejected crawler execution must not carry an approval artifact")


@dataclass(frozen=True, slots=True)
class CrawlerExecutionDispatchDecision:
    state: CrawlerExecutionDecisionState
    reason_codes: tuple[str, ...]
    request_id: UUID
    execution_key: str | None = None
    outbox_event_key: str | None = None

    @property
    def can_enqueue(self) -> bool:
        return self.state is CrawlerExecutionDecisionState.ENQUEUE_WORKFLOW_SIGNAL


@dataclass(frozen=True, slots=True)
class CrawlerExecutionRequestSummary:
    request_id: UUID
    owner_user_id: UUID
    manifest_path: str
    request_artifact_path: str
    manifest_sha256: str
    request_sha256: str
    reviewed_plan_sha256: str
    source_ids: tuple[str, ...]
    reason: str
    created_at: datetime
    expires_at: datetime


class CrawlerExecutionRequestStore(Protocol):
    def create_request(self, draft: CrawlerExecutionRequestDraft) -> UUID: ...

    def get_request(self, request_id: UUID) -> CrawlerExecutionRequestDraft | None: ...

    def list_pending_requests(
        self,
        *,
        owner_user_id: UUID,
        limit: int = 50,
    ) -> tuple[CrawlerExecutionRequestSummary, ...]: ...


class CrawlerExecutionApprovalStore(Protocol):
    def approve_and_enqueue(
        self,
        request: CrawlerExecutionRequestDraft,
        approval: CrawlerExecutionApprovalDraft,
        decision: CrawlerExecutionDispatchDecision,
    ) -> UUID: ...

    def reject(
        self,
        request: CrawlerExecutionRequestDraft,
        approval: CrawlerExecutionApprovalDraft,
        decision: CrawlerExecutionDispatchDecision,
    ) -> UUID: ...


class CrawlerExecutionPlanner:
    """Plan durable handoff from reviewed request to internal crawler workflow signal."""

    def plan(
        self,
        request: CrawlerExecutionRequestDraft,
        approval: CrawlerExecutionApprovalDraft,
        *,
        now: datetime,
    ) -> CrawlerExecutionDispatchDecision:
        _validate_aware(now, "now")
        reasons: list[str] = []
        if approval.request_id != request.request_id:
            reasons.append("REQUEST_APPROVAL_MISMATCH")
        if now >= request.expires_at:
            reasons.append("CRAWLER_REQUEST_EXPIRED")
        if approval.decided_at < request.created_at:
            reasons.append("APPROVAL_PRECEDES_REQUEST")
        if approval.decided_by_user_id is None:
            reasons.append("AUTHENTICATED_APPROVAL_REQUIRED")
        if approval.local_cli_approval_present and approval.decided_by_user_id is None:
            reasons.append("LOCAL_CLI_APPROVAL_NOT_EXECUTION_AUTHORITY")
        if reasons:
            return CrawlerExecutionDispatchDecision(
                CrawlerExecutionDecisionState.STOPPED,
                tuple(dict.fromkeys(reasons)),
                request_id=request.request_id,
            )
        if approval.outcome is CrawlerExecutionApprovalOutcome.REJECT:
            return CrawlerExecutionDispatchDecision(
                CrawlerExecutionDecisionState.REJECTED,
                ("CRAWLER_EXECUTION_REJECTED",),
                request_id=request.request_id,
            )
        dispatch_key = f"{_EVENT_PREFIX}:{request.execution_key}"
        return CrawlerExecutionDispatchDecision(
            CrawlerExecutionDecisionState.ENQUEUE_WORKFLOW_SIGNAL,
            ("CRAWLER_EXECUTION_APPROVED_FOR_WORKFLOW",),
            request_id=request.request_id,
            # The database independently requires the durable dispatch key to equal the
            # outbox event key.  Keep one prefixed value instead of relying on two values
            # that could drift as the handoff crosses the transaction boundary.
            execution_key=dispatch_key,
            outbox_event_key=dispatch_key,
        )


class CrawlerExecutionService:
    """Synchronous application service for async/web adapter wrapping."""

    def __init__(
        self,
        request_store: CrawlerExecutionRequestStore,
        approval_store: CrawlerExecutionApprovalStore,
        planner: CrawlerExecutionPlanner | None = None,
    ) -> None:
        self._request_store = request_store
        self._approval_store = approval_store
        self._planner = planner or CrawlerExecutionPlanner()

    def create_request(self, draft: CrawlerExecutionRequestDraft) -> UUID:
        return self._request_store.create_request(draft)

    def get_request(self, request_id: UUID) -> CrawlerExecutionRequestDraft | None:
        return self._request_store.get_request(request_id)

    def list_pending_requests(
        self,
        *,
        owner_user_id: UUID,
        limit: int = 50,
    ) -> tuple[CrawlerExecutionRequestSummary, ...]:
        return self._request_store.list_pending_requests(
            owner_user_id=owner_user_id,
            limit=limit,
        )

    def approve_request(
        self,
        request_id: UUID,
        approval: CrawlerExecutionApprovalDraft,
        *,
        now: datetime,
    ) -> UUID:
        request = self._load_request(request_id)
        decision = self._planner.plan(request, approval, now=now)
        if not decision.can_enqueue:
            raise CrawlerExecutionRejected(decision.reason_codes)
        return self._approval_store.approve_and_enqueue(request, approval, decision)

    def reject_request(
        self,
        request_id: UUID,
        approval: CrawlerExecutionApprovalDraft,
        *,
        now: datetime,
    ) -> UUID:
        request = self._load_request(request_id)
        decision = self._planner.plan(request, approval, now=now)
        if decision.state is not CrawlerExecutionDecisionState.REJECTED:
            raise CrawlerExecutionRejected(decision.reason_codes)
        return self._approval_store.reject(request, approval, decision)

    def _load_request(self, request_id: UUID) -> CrawlerExecutionRequestDraft:
        request = self._request_store.get_request(request_id)
        if request is None:
            raise CrawlerExecutionRejected(("CRAWLER_REQUEST_NOT_FOUND",))
        return request


class CrawlerExecutionRejected(RuntimeError):
    def __init__(self, reason_codes: Sequence[str]) -> None:
        frozen = tuple(reason_codes)
        if not frozen:
            raise ValueError("reason_codes must not be empty")
        for reason in frozen:
            _validate_identifier(reason, "reason_code")
        super().__init__(",".join(frozen))
        self.reason_codes = frozen


def crawler_request_payload_hash(
    *,
    target: Mapping[str, object],
    payload: Mapping[str, object],
) -> str:
    encoded = json.dumps(
        {"target": target, "payload": payload, "attachment_refs": []},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_key(label: str, *parts: object) -> str:
    encoded = json.dumps(
        {"label": label, "parts": [str(part) for part in parts]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _freeze_nonempty_identifiers(values: Sequence[str], label: str) -> tuple[str, ...]:
    frozen = tuple(values)
    if not frozen:
        raise ValueError(f"{label} must not be empty")
    for value in frozen:
        _validate_identifier(value, label)
    if len(set(frozen)) != len(frozen):
        raise ValueError(f"{label} must not contain duplicates")
    return tuple(sorted(frozen))


def _validate_manifest_path(value: str) -> str:
    path = _validate_relative_path(value, "manifest_path")
    if not path.startswith("datasets/manifests/"):
        raise ValueError("manifest_path must be under datasets/manifests/")
    return path


def _validate_review_artifact_path(value: str) -> str:
    path = _validate_relative_path(value, "review_artifact_path")
    if not path.startswith("datasets/private/crawler-execution-reviews/"):
        raise ValueError(
            "review artifacts must be under datasets/private/crawler-execution-reviews/"
        )
    return path


def _validate_relative_path(value: str, label: str) -> str:
    if value.startswith("/") or "\\" in value or ".." in value.split("/"):
        raise ValueError(f"{label} must be a safe repository-relative path")
    if not _RELATIVE_PATH.fullmatch(value):
        raise ValueError(f"{label} must be a bounded repository-relative path")
    return value


def _validate_hash(value: str, label: str) -> None:
    if not _HASH.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase sha256 hex digest")


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded machine identifier")


def _validate_text(value: str, label: str) -> None:
    if not value.strip() or len(value) > 1_000:
        raise ValueError(f"{label} must be non-empty bounded text")


def _validate_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = [
    "CrawlerExecutionApprovalDraft",
    "CrawlerExecutionApprovalOutcome",
    "CrawlerExecutionApprovalStore",
    "CrawlerExecutionDecisionState",
    "CrawlerExecutionDispatchDecision",
    "CrawlerExecutionPlanner",
    "CrawlerExecutionRejected",
    "CrawlerExecutionRequestDraft",
    "CrawlerExecutionRequestStore",
    "CrawlerExecutionRequestSummary",
    "CrawlerExecutionService",
    "crawler_request_payload_hash",
]
