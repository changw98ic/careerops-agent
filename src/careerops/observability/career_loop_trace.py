"""Trace-correlated product instrumentation for the career loop.

The request trace ID is used only to correlate an in-process event with the
request/audit trail. It is never a Prometheus label. Prometheus receives only
bounded aggregate metrics, while a small bounded event buffer keeps recent
trace evidence available for local diagnostics without retaining user content.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from careerops.observability.career_loop_metrics import CareerLoopMetrics

__all__ = [
    "CareerLoopTrace",
    "CareerTraceEvent",
    "CareerTraceMetric",
    "bind_trace_id",
    "current_trace_id",
]

_SAFE_TRACE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_TRACE_ID = ContextVar("careerops_trace_id", default="system")


class CareerTraceMetric(StrEnum):
    """Closed metric names that may be emitted by the product trace."""

    INBOX_DECISION = "inbox_decision"
    TRUSTED_SHORTLIST = "trusted_shortlist"
    USER_CORRECTION = "user_correction"
    PACKAGE_APPROVAL = "package_approval"
    SEND_INTENT = "send_intent"
    CONFIRMED_SUBMISSION = "confirmed_submission"
    MAIL_LINKAGE = "mail_linkage"
    REVIEW_LATENCY = "review_latency"
    FOLLOW_UP = "follow_up"


@dataclass(frozen=True, slots=True)
class CareerTraceEvent:
    """A privacy-safe trace event retained for bounded local diagnostics."""

    trace_id: str
    metric: CareerTraceMetric
    outcome: str = ""
    value_seconds: float | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def current_trace_id() -> str:
    """Return the request trace ID bound to the current execution context."""

    return _TRACE_ID.get()


@contextmanager
def bind_trace_id(trace_id: str) -> Generator[None, None, None]:
    """Bind a validated request trace ID for service-level instrumentation."""

    safe_trace_id = trace_id if _SAFE_TRACE_ID.fullmatch(trace_id) else "system"
    token = _TRACE_ID.set(safe_trace_id)
    try:
        yield
    finally:
        _TRACE_ID.reset(token)


class CareerLoopTrace:
    """Record trace-correlated business events and bounded metric aggregates."""

    def __init__(self, metrics: CareerLoopMetrics, *, max_events: int = 4096) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._metrics = metrics
        self._events: deque[CareerTraceEvent] = deque(maxlen=max_events)

    def snapshot(self) -> tuple[CareerTraceEvent, ...]:
        """Return recent trace events without exposing user content."""

        return tuple(self._events)

    def record_inbox_decision(self, *, decision: str) -> None:
        if decision not in {"favorite", "ignore", "snooze", "restore"}:
            return
        self._metrics.record_inbox_decision(decision=decision)
        self._emit(CareerTraceMetric.INBOX_DECISION, outcome=decision)

    def record_trusted_shortlist(
        self, *, first_seen_at: datetime, shortlisted_at: datetime
    ) -> None:
        seconds = _elapsed_seconds(first_seen_at, shortlisted_at)
        if seconds is None:
            return
        self._metrics.observe_trusted_shortlist(seconds=seconds)
        self._emit(CareerTraceMetric.TRUSTED_SHORTLIST, value_seconds=seconds)

    def record_user_correction(self, *, stage: str) -> None:
        if stage not in {"shortlist", "package", "mail_linkage", "reply_draft"}:
            return
        self._metrics.record_user_correction(stage=stage)
        self._emit(CareerTraceMetric.USER_CORRECTION, outcome=stage)

    def record_package_approval(
        self,
        *,
        outcome: str,
        created_at: datetime | None = None,
        decided_at: datetime | None = None,
    ) -> None:
        if outcome not in {"approved", "rejected"}:
            return
        self._metrics.record_package_approval(outcome=outcome)
        seconds = _elapsed_seconds(created_at, decided_at)
        if seconds is not None:
            self._metrics.observe_review_latency(review_kind="package_approval", seconds=seconds)
        self._emit(CareerTraceMetric.PACKAGE_APPROVAL, outcome=outcome, value_seconds=seconds)

    def record_confirmed_submission(self, *, channel: str) -> None:
        if channel not in {"email", "external_form", "manual"}:
            return
        self._metrics.record_confirmed_submission(channel=channel)
        self._emit(CareerTraceMetric.CONFIRMED_SUBMISSION, outcome=channel)

    def record_mail_linkage(self, *, outcome: str) -> None:
        if outcome not in {"linked", "unresolved", "corrected"}:
            return
        self._metrics.record_mail_linkage(outcome=outcome)
        self._emit(CareerTraceMetric.MAIL_LINKAGE, outcome=outcome)

    def record_review_latency(
        self,
        *,
        review_kind: str,
        created_at: datetime | None,
        decided_at: datetime,
    ) -> None:
        if review_kind not in {"mail_proposal", "reply_draft", "package_approval"}:
            return
        seconds = _elapsed_seconds(created_at, decided_at)
        if seconds is None:
            return
        self._metrics.observe_review_latency(review_kind=review_kind, seconds=seconds)
        self._emit(CareerTraceMetric.REVIEW_LATENCY, outcome=review_kind, value_seconds=seconds)

    def record_follow_up(self, *, event: str) -> None:
        if event not in {"created", "snoozed", "completed", "cancelled"}:
            return
        self._metrics.record_follow_up(event=event)
        self._emit(CareerTraceMetric.FOLLOW_UP, outcome=event)

    def record_send_intent(self, *, phase: str) -> None:
        if phase not in {"pending", "sent", "failed", "reconciliation_required"}:
            return
        self._metrics.record_send_intent(phase=phase)
        self._emit(CareerTraceMetric.SEND_INTENT, outcome=phase)

    def _emit(
        self,
        metric: CareerTraceMetric,
        *,
        outcome: str = "",
        value_seconds: float | None = None,
    ) -> None:
        self._events.append(
            CareerTraceEvent(
                trace_id=current_trace_id(),
                metric=metric,
                outcome=outcome,
                value_seconds=value_seconds,
                occurred_at=datetime.now(UTC),
            )
        )


def _elapsed_seconds(started_at: datetime | None, ended_at: datetime | None) -> float | None:
    if started_at is None or ended_at is None:
        return None
    if started_at.tzinfo is None or ended_at.tzinfo is None:
        return None
    seconds = (ended_at - started_at).total_seconds()
    return seconds if seconds >= 0 else None
