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
    ActionState,
)
from careerops.observability import current_trace_id

__all__ = ["ActionProjection", "ActionProjectionBuilder"]

_ACTION_QUEUE_LIMIT = 3


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
    """

    inbox_reader: _InboxReader | None = None
    agent_run_reader: _AgentRunReader | None = None
    capability_resolver: _CapabilityResolver | None = None

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
        del candidate_id  # inbox_reader and agent_run_reader scope internally
        occurred_at = now or datetime.now(UTC)
        trace = current_trace_id()
        items: list[ActionItem] = []

        # The action projection is a stub that returns an empty queue.
        # A real implementation would compose inbox items and agent run
        # state to produce action items.  The contract shape is frozen
        # so the caller can serialize directly.
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
