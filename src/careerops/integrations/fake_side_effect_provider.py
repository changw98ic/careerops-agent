"""Provider port and chaos-capable fake provider for the M5A side-effect kernel.

The fake provider supports the M5A.7 chaos matrix:
- before-call crash
- after-call-before-commit crash (effect applied, receipt not committed)
- timeout-with-success (UNKNOWN result, effect actually applied)
- duplicate receipt (idempotent on reconciliation key)
- revoke
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from careerops.domain.side_effects import (
    ProviderCallResult,
    ProviderFailureClass,
    ProviderResultKind,
    ReceiptFinalState,
    ReconciliationResult,
)


class SideEffectProvider(Protocol):
    """Port for an external write provider.

    ``reconcile`` MUST be safe to call before any retry: API timeout does not
    imply the provider did not execute the action.
    """

    provider_name: str

    def execute(
        self,
        *,
        reconciliation_key: str,
        request_fingerprint: str,
        target: dict[str, object],
        payload: dict[str, object],
        now: datetime,
    ) -> ProviderCallResult: ...

    def reconcile(self, *, reconciliation_key: str, now: datetime) -> ReconciliationResult: ...

    def revoke(
        self, *, reconciliation_key: str, provider_resource_id: str, now: datetime
    ) -> ReconciliationResult: ...


class FakeFailureMode(StrEnum):
    NONE = "none"
    CRASH_BEFORE_CALL = "crash_before_call"
    CRASH_AFTER_CALL_BEFORE_COMMIT = "crash_after_call_before_commit"
    TIMEOUT_WITH_SUCCESS = "timeout_with_success"
    VALIDATION_ERROR = "validation_error"
    TRANSIENT_ERROR = "transient_error"


class FakeProviderCrash(RuntimeError):
    """Simulates a process crash; the kernel must reconcile, not blind retry."""


@dataclass
class FakeProviderEffect:
    reconciliation_key: str
    provider_resource_id: str
    final_state: ReceiptFinalState
    created_at: datetime
    metadata: dict[str, object] = field(default_factory=lambda: {})


class FakeSideEffectProvider:
    """In-memory provider with a programmable chaos matrix.

    Effects are keyed by reconciliation key so duplicate calls are idempotent.
    """

    provider_name = "fake"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._effects: dict[str, FakeProviderEffect] = {}
        self._failure_mode: FakeFailureMode = FakeFailureMode.NONE
        self.execute_call_count = 0
        self.reconcile_call_count = 0
        self.revoke_call_count = 0

    def set_failure_mode(self, mode: FakeFailureMode) -> None:
        with self._lock:
            self._failure_mode = mode

    def execute(
        self,
        *,
        reconciliation_key: str,
        request_fingerprint: str,
        target: dict[str, object],
        payload: dict[str, object],
        now: datetime,
    ) -> ProviderCallResult:
        with self._lock:
            self.execute_call_count += 1
            mode = self._failure_mode

            if mode is FakeFailureMode.CRASH_BEFORE_CALL:
                raise FakeProviderCrash("crash before provider call")

            # Idempotency: a prior effect for this reconciliation key wins.
            existing = self._effects.get(reconciliation_key)
            if existing is not None:
                return ProviderCallResult(
                    kind=ProviderResultKind.SUCCESS,
                    provider_resource_id=existing.provider_resource_id,
                    reconciliation_key=reconciliation_key,
                    metadata={"duplicate": True},
                )

            if mode is FakeFailureMode.VALIDATION_ERROR:
                return ProviderCallResult(
                    kind=ProviderResultKind.FAILURE,
                    error_code="PROVIDER_VALIDATION_REJECTED",
                    failure_class=ProviderFailureClass.VALIDATION,
                )

            if mode is FakeFailureMode.TRANSIENT_ERROR:
                return ProviderCallResult(
                    kind=ProviderResultKind.FAILURE,
                    error_code="PROVIDER_TRANSIENT_FAILURE",
                    failure_class=ProviderFailureClass.TRANSIENT,
                )

            resource_id = f"fake-resource-{len(self._effects) + 1}"
            self._effects[reconciliation_key] = FakeProviderEffect(
                reconciliation_key=reconciliation_key,
                provider_resource_id=resource_id,
                final_state=ReceiptFinalState.SUCCEEDED,
                created_at=now,
                metadata={
                    "request_fingerprint": request_fingerprint,
                    "target": dict(target),
                    "payload": dict(payload),
                },
            )

            if mode is FakeFailureMode.CRASH_AFTER_CALL_BEFORE_COMMIT:
                # Effect is applied; the worker crashes before committing the receipt.
                raise FakeProviderCrash("crash after provider call, before commit")

            if mode is FakeFailureMode.TIMEOUT_WITH_SUCCESS:
                # Effect is applied; the call returns UNKNOWN (timeout).
                return ProviderCallResult(
                    kind=ProviderResultKind.UNKNOWN,
                    error_code="PROVIDER_TIMEOUT",
                    failure_class=ProviderFailureClass.UNKNOWN,
                    reconciliation_key=reconciliation_key,
                    provider_resource_id=resource_id,
                )

            return ProviderCallResult(
                kind=ProviderResultKind.SUCCESS,
                provider_resource_id=resource_id,
                reconciliation_key=reconciliation_key,
            )

    def reconcile(self, *, reconciliation_key: str, now: datetime) -> ReconciliationResult:
        del now
        with self._lock:
            self.reconcile_call_count += 1
            effect = self._effects.get(reconciliation_key)
            if effect is None:
                return ReconciliationResult(found=False)
            return ReconciliationResult(
                found=True,
                final_state=effect.final_state,
                provider_resource_id=effect.provider_resource_id,
                metadata=dict(effect.metadata),
            )

    def revoke(
        self, *, reconciliation_key: str, provider_resource_id: str, now: datetime
    ) -> ReconciliationResult:
        with self._lock:
            self.revoke_call_count += 1
            effect = self._effects.get(reconciliation_key)
            if effect is None or effect.provider_resource_id != provider_resource_id:
                return ReconciliationResult(found=False)
            revoked = FakeProviderEffect(
                reconciliation_key=reconciliation_key,
                provider_resource_id=provider_resource_id,
                final_state=ReceiptFinalState.REVOKED,
                created_at=now,
                metadata=dict(effect.metadata),
            )
            self._effects[reconciliation_key] = revoked
            return ReconciliationResult(
                found=True,
                final_state=ReceiptFinalState.REVOKED,
                provider_resource_id=provider_resource_id,
            )

    def effect_count(self) -> int:
        with self._lock:
            return sum(
                1
                for effect in self._effects.values()
                if effect.final_state is ReceiptFinalState.SUCCEEDED
            )

    def get_effect(self, reconciliation_key: str) -> FakeProviderEffect | None:
        with self._lock:
            return self._effects.get(reconciliation_key)


class OutboxEventRecord:
    """Minimal outbox event used by the kernel test doubles."""

    def __init__(self, event_id: UUID, intent_id: UUID, payload_version_id: UUID) -> None:
        self.event_id = event_id
        self.intent_id = intent_id
        self.payload_version_id = payload_version_id
