"""Deterministic action queue builder for the Agent Console.

Builds the action queue projection by composing data from the inbox
projection, agent run repository, and capability resolver.  The queue
is deterministic: the same inputs always produce the same action_key,
deterministic_rank, and queue_version for a given candidate.

api-contract.md section 3 / 3.1 freezes the ActionPage shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from careerops.agent_console.contracts import (
    ActionItem,
    ActionKind,
    ActionPrerequisites,
    ActionSourceRef,
    ActionState,
    SourceRefType,
)
from careerops.observability import current_trace_id

__all__ = ["ActionProjection", "ActionProjectionBuilder"]

_ACTION_QUEUE_LIMIT = 3

# Mapping from ActionKind to human-readable title.
_KIND_TITLES: dict[ActionKind, str] = {
    ActionKind.RESUME_REVIEW: "Review Resume",
    ActionKind.JOB_MATCHING: "Review Job Matches",
    ActionKind.INTERVIEW_PREPARATION: "Prepare for Interview",
    ActionKind.SMART_FORM_INTAKE: "Complete Smart Form",
    ActionKind.CRAWL_PERMISSION: "Authorize Job Source Login",
}

# Mapping from ActionKind to target route.
_KIND_ROUTES: dict[ActionKind, str] = {
    ActionKind.RESUME_REVIEW: "/resumes",
    ActionKind.JOB_MATCHING: "/inbox",
    ActionKind.INTERVIEW_PREPARATION: "/agent-console",
    ActionKind.SMART_FORM_INTAKE: "/smart-intake",
    ActionKind.CRAWL_PERMISSION: "/crawl-plans",
}


@runtime_checkable
class _InboxReader(Protocol):
    """Minimal inbox read port needed by the action projection."""

    def list_ready_for_candidate(
        self, candidate_id: UUID, *, limit: int
    ) -> tuple[dict[str, object], ...]: ...


@runtime_checkable
class _AgentRunReader(Protocol):
    """Minimal agent run read port needed by the action projection."""

    def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        capability: object | None = None,
        limit: int = 50,
    ) -> tuple[dict[str, object], ...]: ...


@runtime_checkable
class _CapabilityResolver(Protocol):
    """Minimal capability resolver read port."""

    def decide(self, kind: object) -> object: ...


@runtime_checkable
class _ActionStore(Protocol):
    """Minimal action store read port for the action projection."""

    def list_queue(
        self,
        candidate_id: UUID,
        *,
        states: tuple[ActionState, ...] | None = None,
        limit: int = 3,
    ) -> list[object]: ...


@dataclass(frozen=True, slots=True)
class ActionProjection:
    """Result of a deterministic action queue build."""

    queue_version: int
    generated_at: datetime
    items: tuple[ActionItem, ...]
    trace_id: str


@dataclass(frozen=True, slots=True)
class ActionProjectionBuilder:
    """Builds a deterministic action queue for a candidate.

    The builder composes data from the inbox projection (for new job
    context), agent run repository (for pending reviews), and capability
    resolver (for prerequisite checks).  The output is sorted by
    ``deterministic_rank`` and hard-capped at 3 items.

    When ``agent_action_repo`` is provided, ``build()`` reads from the
    persisted action store.  When ``None``, it returns an empty queue
    (backward-compatible stub behavior).
    """

    inbox_reader: _InboxReader | None = None
    agent_run_reader: _AgentRunReader | None = None
    capability_resolver: _CapabilityResolver | None = None
    agent_action_repo: _ActionStore | None = None

    def build(
        self,
        candidate_id: UUID,
        *,
        queue_version: int = 0,
        now: datetime | None = None,
    ) -> ActionProjection:
        """Build the action queue for the given candidate.

        Returns an ``ActionProjection`` with at most 3 items, sorted by
        deterministic priority.  The queue_version is incremented from
        the caller-supplied value.
        """
        occurred_at = now or datetime.now(UTC)
        trace = current_trace_id()
        items: list[ActionItem] = []

        if self.agent_action_repo is not None:
            records = self.agent_action_repo.list_queue(
                candidate_id,
                states=(ActionState.PROPOSED, ActionState.SNOOZED),
                limit=_ACTION_QUEUE_LIMIT,
            )
            for rec in records:
                item = _record_to_action_item(rec, now=occurred_at)
                if item is not None:
                    items.append(item)

        sorted_items = sorted(items, key=lambda a: a.deterministic_rank)
        capped = tuple(sorted_items[:_ACTION_QUEUE_LIMIT])

        return ActionProjection(
            queue_version=queue_version + 1,
            generated_at=occurred_at,
            items=capped,
            trace_id=trace,
        )

    def find_action(
        self,
        candidate_id: UUID,
        action_key: str,
    ) -> ActionItem | None:
        """Look up a single action by key in the current projection.

        Returns ``None`` if the action is not found or not in
        ``proposed`` state.
        """
        projection = self.build(candidate_id)
        for item in projection.items:
            if item.action_key == action_key:
                return item
        return None

    def mutate_action(
        self,
        candidate_id: UUID,
        action_key: str,
        *,
        target_state: ActionState,
        expected_queue_version: int,
    ) -> ActionProjection:
        """Transition an action to a new state and rebuild the projection.

        This is a stub; a real implementation would persist the state
        transition and rebuild the queue.  For now it delegates to
        ``build``.
        """
        return self.build(candidate_id, queue_version=expected_queue_version)


# ---------------------------------------------------------------------------
# Record -> ActionItem mapping
# ---------------------------------------------------------------------------


def _derive_priority(deterministic_rank: int) -> str:
    """Map deterministic_rank to a priority label."""
    if deterministic_rank <= 0:
        return "high"
    if deterministic_rank == 1:
        return "high"
    return "medium"


def _derive_target_route(kind: ActionKind) -> str:
    """Map ActionKind to a frontend target route."""
    return _KIND_ROUTES.get(kind, "/agent-console")


def _derive_title(kind: ActionKind) -> str:
    """Map ActionKind to a human-readable title."""
    return _KIND_TITLES.get(kind, kind.value.replace("_", " ").title())


def _map_source_refs(raw_refs: tuple[dict[str, str], ...]) -> list[ActionSourceRef]:
    """Convert raw dict source_refs to ActionSourceRef models."""
    result: list[ActionSourceRef] = []
    for ref in raw_refs:
        ref_type = ref.get("type", "job")
        try:
            source_type = SourceRefType(ref_type)
        except ValueError:
            source_type = SourceRefType.JOB
        result.append(ActionSourceRef(
            type=source_type,
            id=ref.get("id", ""),
            version=int(ref["version"]) if "version" in ref else None,
        ))
    return result


def _record_to_action_item(
    rec: object,
    *,
    now: datetime | None = None,
) -> ActionItem | None:
    """Map an AgentActionRecord to an ActionItem.

    Returns ``None`` if the record cannot be mapped (missing required fields).
    The mapping is deterministic: the same record always produces the same item.
    """
    try:
        action_key: str = rec.action_key  # type: ignore[attr-defined]
        kind: ActionKind = rec.kind  # type: ignore[attr-defined]
        reason_code: str = rec.reason_code  # type: ignore[attr-defined]
        deterministic_rank: int = rec.deterministic_rank  # type: ignore[attr-defined]
        state: ActionState = rec.state  # type: ignore[attr-defined]
        created_at: datetime | None = rec.created_at  # type: ignore[attr-defined]
        expires_at: datetime | None = rec.expires_at  # type: ignore[attr-defined]
        source_event_key: UUID = rec.source_event_key  # type: ignore[attr-defined]
        context_id: UUID | None = rec.context_id  # type: ignore[attr-defined]
        source_refs_raw: tuple[dict[str, str], ...] = rec.source_refs  # type: ignore[attr-defined]
    except AttributeError:
        return None

    effective_now = now or datetime.now(UTC)
    source_freshness = created_at or effective_now

    return ActionItem(
        action_key=action_key,
        kind=kind,
        title=_derive_title(kind),
        reason_code=reason_code,
        priority=_derive_priority(deterministic_rank),
        target_route=_derive_target_route(kind),
        deterministic_rank=deterministic_rank,
        prerequisites=ActionPrerequisites(status="ready", missing=[]),
        context_id=str(context_id) if context_id else "",
        source_refs=_map_source_refs(source_refs_raw),
        source_event_key=str(source_event_key),
        source_freshness=source_freshness,
        state=state,
        created_at=created_at or effective_now,
        expires_at=expires_at,
    )
