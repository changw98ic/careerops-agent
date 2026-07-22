from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, Engine, RowMapping

from careerops.application.crawler_outbox import (
    CrawlerDispatchError,
    CrawlerExecutionDispatch,
)
from careerops.application.outbox import ClaimedOutboxEvent
from careerops.infrastructure.database.outbox import (
    PostgresOutboxRepository,
    PostgresOutboxStore,
)
from careerops.infrastructure.database.schema import (
    crawler_execution_approvals,
    crawler_execution_dispatches,
    crawler_execution_requests,
    outbox_events,
)

_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")
_OWNER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_RECONCILIATION_ERROR_CODES = frozenset({"CRAWLER_EXECUTION_RECONCILIATION_REQUIRED"})


class PostgresCrawlerExecutionOutboxStore:
    """Finish bound crawler events and their action intents in one transaction.

    The generic outbox store only owns an event lifecycle. Crawler execution also has a durable
    action intent, so its terminal transition must be coupled to a result record and the intent
    status. The database-owned completion function validates the active lease and dispatch
    binding before making all three changes atomically.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._outbox = PostgresOutboxStore(engine)

    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        return self._outbox.claim(
            owner=owner,
            now=now,
            lease_for=lease_for,
            limit=limit,
            event_key_prefix=event_key_prefix,
        )

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None:
        with self._engine.begin() as connection:
            _validate_completion_time(owner=owner, now=now)
            complete_crawler_execution_outbox_event(
                connection,
                event_id=event_id,
                owner=owner,
                lease_token=lease_token,
                outcome="succeeded",
                error_code=None,
            )

    def release(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
        retry_at: datetime,
        error_code: str,
        terminal: bool,
    ) -> None:
        with self._engine.begin() as connection:
            if not terminal or not _has_bound_crawler_dispatch(connection, event_id):
                PostgresOutboxRepository(connection).release(
                    event_id,
                    owner=owner,
                    lease_token=lease_token,
                    now=now,
                    retry_at=retry_at,
                    error_code=error_code,
                    terminal=terminal,
                )
                return

            _validate_terminal_completion(
                owner=owner,
                now=now,
                retry_at=retry_at,
                error_code=error_code,
            )
            complete_crawler_execution_outbox_event(
                connection,
                event_id=event_id,
                owner=owner,
                lease_token=lease_token,
                outcome=(
                    "reconciliation_required"
                    if error_code in _RECONCILIATION_ERROR_CODES
                    else "failed"
                ),
                error_code=error_code,
            )


def complete_crawler_execution_outbox_event(
    connection: Connection,
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    outcome: str,
    error_code: str | None,
) -> None:
    connection.execute(
        sa.text(
            "SELECT careerops.complete_crawler_execution_outbox_event("
            ":event_id, :lease_owner, :lease_token, :outcome, :error_code, :result_id)"
        ),
        {
            "event_id": event_id,
            "lease_owner": owner,
            "lease_token": lease_token,
            "outcome": outcome,
            "error_code": error_code,
            "result_id": uuid4(),
        },
    )


def _has_bound_crawler_dispatch(connection: Connection, event_id: UUID) -> bool:
    return bool(
        connection.scalar(
            sa.select(crawler_execution_dispatches.c.id)
            .where(crawler_execution_dispatches.c.outbox_event_id == event_id)
            .limit(1)
        )
    )


def _validate_completion_time(*, owner: str, now: datetime) -> None:
    if not _OWNER.fullmatch(owner):
        raise ValueError("lease owner must be a bounded machine identifier")
    _require_aware(now, "now")


def _validate_terminal_completion(
    *,
    owner: str,
    now: datetime,
    retry_at: datetime,
    error_code: str,
) -> None:
    _validate_completion_time(owner=owner, now=now)
    _require_aware(retry_at, "retry_at")
    if retry_at < now:
        raise ValueError("retry_at must not precede now")
    if not _ERROR_CODE.fullmatch(error_code):
        raise ValueError("error_code must be a bounded machine code")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


class PostgresCrawlerExecutionDispatchReader:
    """Load the crawler dispatch that is bound to one claimed outbox event."""

    def __init__(self, engine: Engine, *, root: Path) -> None:
        self._engine = engine
        self._root = root.resolve()

    def load_dispatch(self, event: ClaimedOutboxEvent) -> CrawlerExecutionDispatch:
        with self._engine.begin() as connection:
            row = (
                connection.execute(select_crawler_dispatch_for_outbox_event_statement(event))
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise CrawlerDispatchError(
                "CRAWLER_DISPATCH_METADATA_MISSING",
                "crawler dispatch row is missing for the claimed outbox event",
            )
        return _dispatch_from_row(row, root=self._root)


def select_crawler_dispatch_for_outbox_event_statement(
    event: ClaimedOutboxEvent,
) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(
            outbox_events.c.id.label("event_id"),
            outbox_events.c.event_key,
            outbox_events.c.action_intent_id,
            outbox_events.c.payload_version_id,
            crawler_execution_dispatches.c.execution_key,
            crawler_execution_requests.c.manifest_path,
            crawler_execution_requests.c.request_artifact_path,
            crawler_execution_requests.c.request_sha256,
            crawler_execution_approvals.c.approval_artifact_path,
            crawler_execution_approvals.c.approval_artifact_sha256,
        )
        .select_from(
            crawler_execution_dispatches.join(
                outbox_events,
                outbox_events.c.id == crawler_execution_dispatches.c.outbox_event_id,
            )
            .join(
                crawler_execution_requests,
                crawler_execution_requests.c.id == crawler_execution_dispatches.c.request_id,
            )
            .join(
                crawler_execution_approvals,
                crawler_execution_approvals.c.request_id == crawler_execution_requests.c.id,
            )
        )
        .where(
            outbox_events.c.id == event.event_id,
            outbox_events.c.event_key == event.event_key,
            outbox_events.c.action_intent_id == event.action_intent_id,
            outbox_events.c.payload_version_id == event.payload_version_id,
            crawler_execution_dispatches.c.outbox_event_id == event.event_id,
            crawler_execution_dispatches.c.action_intent_id == event.action_intent_id,
            crawler_execution_approvals.c.decision == "approved",
        )
    )


def _dispatch_from_row(row: RowMapping, *, root: Path) -> CrawlerExecutionDispatch:
    approval_path = row["approval_artifact_path"]
    approval_sha256 = row["approval_artifact_sha256"]
    if not isinstance(approval_path, str) or not isinstance(approval_sha256, str):
        raise CrawlerDispatchError(
            "CRAWLER_DISPATCH_METADATA_INVALID",
            "approved crawler dispatch is missing approval artifact binding",
        )
    return CrawlerExecutionDispatch(
        event_id=cast("UUID", row["event_id"]),
        event_key=cast("str", row["event_key"]),
        action_intent_id=cast("UUID", row["action_intent_id"]),
        payload_version_id=cast("UUID", row["payload_version_id"]),
        execution_key=cast("str", row["execution_key"]),
        config_path=_root_relative_path(root, cast("str", row["manifest_path"])),
        request_path=_review_artifact_path(root, cast("str", row["request_artifact_path"])),
        approval_path=_review_artifact_path(root, approval_path),
        request_sha256=cast("str", row["request_sha256"]),
        approval_sha256=approval_sha256,
    )


def _root_relative_path(root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        raise CrawlerDispatchError(
            "CRAWLER_DISPATCH_METADATA_INVALID",
            "crawler dispatch paths must be repository-relative",
        )
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise CrawlerDispatchError(
            "CRAWLER_DISPATCH_METADATA_INVALID",
            "crawler dispatch path escapes the repository root",
        ) from error
    return resolved


def _review_artifact_path(root: Path, raw_path: str) -> Path:
    path = _root_relative_path(root, raw_path)
    review_root = (root / "datasets/private/crawler-execution-reviews").resolve()
    try:
        path.relative_to(review_root)
    except ValueError as error:
        raise CrawlerDispatchError(
            "CRAWLER_DISPATCH_METADATA_INVALID",
            "crawler request and approval artifacts must stay under crawler execution reviews",
        ) from error
    return path


def compile_query_for_test(statement: sa.Select[tuple[object, ...]]) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))
