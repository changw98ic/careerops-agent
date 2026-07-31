"""Shared guarded Agent runtime used by review-only career capabilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import ClassVar, cast
from uuid import UUID, uuid4

from careerops.api.errors import ConflictError, InvalidStateError, NotFoundError
from careerops.domain.agent_runs import (
    AgentCapability,
    AgentReviewDecision,
    AgentRun,
    AgentRunRepository,
    AgentRunReview,
    AgentRunState,
)
from careerops.observability import current_trace_id
from careerops.orchestration.capability_resolver import CapabilityDecision, CapabilityKind

__all__ = [
    "AgentExecutionOutcome",
    "AgentInputBundle",
    "AgentRuntime",
    "canonical_input_hash",
]


@dataclass(frozen=True, slots=True)
class AgentInputBundle:
    """Bounded identities captured before any model call."""

    identities: Mapping[str, str]
    evidence_ids: tuple[UUID, ...] = ()
    input_hash: str = ""


@dataclass(frozen=True, slots=True)
class AgentExecutionOutcome:
    result: Mapping[str, object]
    state: AgentRunState = AgentRunState.SUCCEEDED
    error_category: str = ""
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def canonical_input_hash(identities: Mapping[str, str]) -> str:
    """Hash only version/hash identities, never source content."""
    payload = json.dumps(dict(sorted(identities.items())), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AgentRuntime:
    """Create, claim, execute, review, and list candidate-owned Agent runs."""

    _CAPABILITY_KINDS: ClassVar[dict[AgentCapability, CapabilityKind]] = {
        # All three are review-only model paths and share the existing
        # default-deny model capability. Keeping one gate avoids accidentally
        # releasing a new agent merely by adding an enum member.
        AgentCapability.JOB_MATCHING: CapabilityKind.MODEL_TAILORING,
        AgentCapability.RESUME_REVIEW: CapabilityKind.MODEL_TAILORING,
        AgentCapability.INTERVIEW_PREPARATION: CapabilityKind.MODEL_TAILORING,
    }

    def __init__(
        self,
        repository: AgentRunRepository,
        *,
        capability_resolver: object | None,
    ) -> None:
        self._repository = repository
        self._capability_resolver = capability_resolver

    def start(
        self,
        candidate_id: UUID,
        capability: AgentCapability,
        bundle: AgentInputBundle,
        *,
        schema_version: str,
        prompt_version: str,
        trace_id: str = "",
        now: datetime | None = None,
        retry_of: UUID | None = None,
    ) -> AgentRun:
        occurred_at = now or datetime.now(UTC)
        input_hash = bundle.input_hash or canonical_input_hash(bundle.identities)
        if retry_of is not None:
            # A retry must never resolve to the original run through the
            # idempotency lookup. The key is derived from the retried run so
            # retrying the SAME source run twice returns the same new run
            # (idempotent retry), while retrying a retry gets a fresh key.
            idempotency_key = f"{capability.value}:{input_hash}:retry:{retry_of}"
        else:
            idempotency_key = f"{capability.value}:{input_hash}"
        existing = self._repository.find_by_idempotency(candidate_id, capability, idempotency_key)
        if existing is not None:
            return existing

        released, reason = self._capability_decision(capability)
        state = AgentRunState.PENDING if released else AgentRunState.UNAVAILABLE
        run = AgentRun(
            id=uuid4(),
            candidate_id=candidate_id,
            capability=capability,
            state=state,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            input_identities=dict(bundle.identities),
            evidence_ids=bundle.evidence_ids,
            schema_version=schema_version,
            prompt_version=prompt_version,
            trace_id=trace_id or current_trace_id(),
            result=({} if released else {"status": "unavailable", "reason": _safe_reason(reason)}),
            error_category="" if released else "capability_unavailable",
            created_at=occurred_at,
            finished_at=None if released else occurred_at,
        )
        try:
            return self._repository.create(run)
        except ConflictError:
            winner = self._repository.find_by_idempotency(candidate_id, capability, idempotency_key)
            if winner is not None:
                return winner
            raise

    def execute(
        self,
        run: AgentRun,
        operation: Callable[[AgentRun], AgentExecutionOutcome],
        *,
        now: datetime | None = None,
    ) -> AgentRun:
        """Claim one pending run and execute exactly one bounded operation."""
        if run.state is not AgentRunState.PENDING:
            return run
        occurred_at = now or datetime.now(UTC)
        claim = getattr(self._repository, "claim", None)
        if callable(claim):
            claimed = cast(Callable[[UUID, UUID, datetime], AgentRun | None], claim)(
                run.candidate_id, run.id, occurred_at
            )
            if claimed is None:
                current = self._repository.get(run.candidate_id, run.id)
                return current or run
            run = claimed
        else:
            run = self._repository.update(
                replace(run, state=AgentRunState.RUNNING, started_at=occurred_at)
            )
        try:
            outcome = operation(run)
        except Exception as exc:
            outcome = AgentExecutionOutcome(
                result={"status": "failed"},
                state=AgentRunState.FAILED,
                error_category=type(exc).__name__,
            )
        finished = now or datetime.now(UTC)
        return self._repository.update(
            replace(
                run,
                state=outcome.state,
                result=_bounded_result(outcome.result),
                error_category=_safe_reason(outcome.error_category),
                model_id=outcome.model_id[:128],
                input_tokens=max(0, min(10_000_000, outcome.input_tokens)),
                output_tokens=max(0, min(10_000_000, outcome.output_tokens)),
                started_at=run.started_at or occurred_at,
                finished_at=finished,
            )
        )

    def complete_unavailable(
        self,
        run: AgentRun,
        result: Mapping[str, object],
        *,
        reason: str = "capability_unavailable",
        now: datetime | None = None,
    ) -> AgentRun:
        """Attach a deterministic review-only fallback to a denied run."""
        if run.state is not AgentRunState.UNAVAILABLE:
            return run
        return self._repository.update(
            replace(
                run,
                result=_bounded_result(result),
                error_category=_safe_reason(reason),
                finished_at=now or datetime.now(UTC),
            )
        )

    def stop(
        self,
        candidate_id: UUID,
        run_id: UUID,
        *,
        reason_code: str = "user_requested",
        now: datetime | None = None,
    ) -> AgentRun:
        """Cancel a run that is still PENDING.

        Synchronous in-process execution cannot be interrupted mid-call, so
        only a PENDING (not yet claimed) run can be stopped; RUNNING and
        terminal runs raise :class:`InvalidStateError`.
        """
        run = self.get(candidate_id, run_id)
        if run.state is not AgentRunState.PENDING:
            raise InvalidStateError(
                "agent run is not pending; synchronous execution cannot be interrupted"
            )
        occurred_at = now or datetime.now(UTC)
        return self._repository.update(
            replace(
                run,
                state=AgentRunState.CANCELLED,
                error_category=_safe_reason(reason_code),
                finished_at=occurred_at,
            )
        )

    def get(self, candidate_id: UUID, run_id: UUID) -> AgentRun:
        run = self._repository.get(candidate_id, run_id)
        if run is None:
            raise NotFoundError("agent run not found for candidate")
        return run

    def list(
        self,
        candidate_id: UUID,
        *,
        capability: AgentCapability | None = None,
        limit: int = 50,
    ) -> list[AgentRun]:
        return self._repository.list_for_candidate(
            candidate_id, capability=capability, limit=min(max(1, limit), 200)
        )

    def review(
        self,
        candidate_id: UUID,
        run_id: UUID,
        *,
        decision: AgentReviewDecision,
        actor_id: str,
        note: str = "",
        edited_result: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> AgentRun:
        run = self.get(candidate_id, run_id)
        if run.state not in {
            AgentRunState.SUCCEEDED,
            AgentRunState.UNAVAILABLE,
            AgentRunState.ABSTAINED,
            AgentRunState.REVIEWED,
        }:
            raise InvalidStateError("agent run is not ready for review")
        occurred_at = now or datetime.now(UTC)
        edited = dict(edited_result or {})
        review = AgentRunReview(
            id=uuid4(),
            run_id=run.id,
            candidate_id=candidate_id,
            decision=decision,
            actor_id=actor_id[:128],
            note=note[:1000],
            edited_result=_bounded_result(edited),
            created_at=occurred_at,
        )
        reviewed = replace(
            run,
            state=AgentRunState.REVIEWED,
            review_decision=decision,
            reviewed_by=actor_id[:128],
            review_note=note[:1000],
            result=_bounded_result(edited)
            if decision is AgentReviewDecision.EDITED
            else run.result,
            reviewed_at=occurred_at,
        )
        record_review = getattr(self._repository, "record_review", None)
        if callable(record_review):
            return cast(Callable[[AgentRunReview, AgentRun], AgentRun], record_review)(
                review, reviewed
            )
        self._repository.add_review(review)
        return self._repository.update(reviewed)

    def reviews(self, candidate_id: UUID, run_id: UUID) -> list[AgentRunReview]:
        self.get(candidate_id, run_id)
        return self._repository.list_reviews(candidate_id, run_id)

    def _capability_decision(self, capability: AgentCapability) -> tuple[bool, str]:
        resolver = self._capability_resolver
        if resolver is None:
            return False, "capability resolver unavailable"
        try:
            decision = resolver.decide(self._CAPABILITY_KINDS[capability])  # type: ignore[attr-defined]
        except Exception:
            return False, "capability unavailable"
        if not isinstance(decision, CapabilityDecision):
            return False, "capability unavailable"
        return bool(decision.released), decision.reason


def _safe_reason(value: str) -> str:
    return (value or "unavailable")[:160]


def _bounded_result(result: Mapping[str, object]) -> dict[str, object]:
    """Keep result storage structural and bounded; no free-form transcript."""
    try:
        encoded = json.dumps(dict(result), ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return {"status": "invalid_result"}
    if len(encoded) > 20_000:
        return {"status": "result_too_large"}
    return dict(result)
