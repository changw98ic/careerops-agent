from __future__ import annotations

import re
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql.base import Executable

from careerops.application.application_prep import (
    PreparedApplicationDraft,
    materialize_json_mapping,
    prepared_draft_payload_hash,
)
from careerops.infrastructure.database.schema import action_intents, action_payload_versions

_CREATED_BY = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")


class PostgresApplicationDraftStore:
    """Persist a prepared draft exactly once as an internal action intent.

    The caller owns the transaction. A stable draft key converges retries on one immutable
    internal intent/payload pair; this store never enqueues an outbox event, publishes provider
    work, creates receipts, or reserves submission capacity.
    """

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("application draft persistence requires an explicit transaction")
        self._connection = connection

    def save_prepared_draft(
        self,
        draft: PreparedApplicationDraft,
        *,
        created_by: str,
    ) -> UUID:
        _validate_created_by(created_by)
        if draft.action_kind != "create_internal_draft":
            raise ValueError("only internal application drafts can be persisted here")
        _validate_prepared_draft_identity(draft)
        intent_id = uuid4()
        inserted_id = self._connection.scalar(
            insert_action_intent_statement(
                intent_id=intent_id,
                draft=draft,
                created_by=created_by,
            )
        )
        if isinstance(inserted_id, UUID):
            payload_version_id = uuid4()
            self._connection.execute(
                insert_payload_version_statement(
                    payload_version_id=payload_version_id,
                    intent_id=intent_id,
                    draft=draft,
                )
            )
            self._connection.execute(
                bind_current_payload_statement(
                    intent_id=intent_id,
                    payload_version_id=payload_version_id,
                )
            )
            return inserted_id

        existing = (
            self._connection.execute(select_prepared_draft_statement(draft.idempotency_key))
            .mappings()
            .one_or_none()
        )
        if existing is None:
            raise RuntimeError("draft idempotency conflict did not yield an existing draft")
        _assert_matching_prepared_draft(existing, draft)
        return cast("UUID", existing["intent_id"])


def insert_action_intent_statement(
    *,
    intent_id: UUID,
    draft: PreparedApplicationDraft,
    created_by: str,
) -> Executable:
    return (
        postgresql.insert(action_intents)
        .values(
            id=intent_id,
            action_kind=draft.action_kind,
            resource_type=draft.resource_type,
            resource_id=draft.resource_id,
            idempotency_key=draft.idempotency_key,
            status="proposed",
            created_by=created_by,
        )
        .on_conflict_do_nothing(index_elements=[action_intents.c.idempotency_key])
        .returning(action_intents.c.id)
    )


def insert_payload_version_statement(
    *,
    payload_version_id: UUID,
    intent_id: UUID,
    draft: PreparedApplicationDraft,
) -> sa.Insert:
    return sa.insert(action_payload_versions).values(
        id=payload_version_id,
        action_intent_id=intent_id,
        version=1,
        target=materialize_json_mapping(draft.target),
        payload=materialize_json_mapping(draft.payload),
        attachment_refs=[materialize_json_mapping(item) for item in draft.attachment_refs],
        payload_hash=draft.payload_hash,
    )


def bind_current_payload_statement(
    *,
    intent_id: UUID,
    payload_version_id: UUID,
) -> sa.Update:
    return (
        sa.update(action_intents)
        .where(action_intents.c.id == intent_id)
        .values(current_payload_version_id=payload_version_id)
    )


def select_prepared_draft_statement(idempotency_key: str) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(
            action_intents.c.id.label("intent_id"),
            action_intents.c.action_kind,
            action_intents.c.resource_type,
            action_intents.c.resource_id,
            action_intents.c.idempotency_key,
            action_payload_versions.c.target,
            action_payload_versions.c.payload,
            action_payload_versions.c.attachment_refs,
            action_payload_versions.c.payload_hash,
        )
        .select_from(
            action_intents.join(
                action_payload_versions,
                sa.and_(
                    action_payload_versions.c.action_intent_id == action_intents.c.id,
                    action_payload_versions.c.id == action_intents.c.current_payload_version_id,
                ),
            )
        )
        .where(action_intents.c.idempotency_key == idempotency_key)
        .with_for_update(of=action_intents)
    )


def _assert_matching_prepared_draft(
    existing: RowMapping,
    draft: PreparedApplicationDraft,
) -> None:
    expected = {
        "action_kind": draft.action_kind,
        "resource_type": draft.resource_type,
        "resource_id": draft.resource_id,
        "idempotency_key": draft.idempotency_key,
        "target": materialize_json_mapping(draft.target),
        "payload": materialize_json_mapping(draft.payload),
        "attachment_refs": [materialize_json_mapping(item) for item in draft.attachment_refs],
        "payload_hash": draft.payload_hash,
    }
    if any(existing[key] != value for key, value in expected.items()):
        raise ValueError("draft idempotency key is already bound to a different draft identity")


def _validate_prepared_draft_identity(draft: PreparedApplicationDraft) -> None:
    if (
        prepared_draft_payload_hash(
            target=draft.target,
            payload=draft.payload,
            attachment_refs=draft.attachment_refs,
        )
        != draft.payload_hash
    ):
        raise ValueError("prepared draft payload hash does not match immutable content")


def _validate_created_by(value: str) -> None:
    if not _CREATED_BY.fullmatch(value):
        raise ValueError("created_by must be a bounded actor identifier")


__all__ = [
    "PostgresApplicationDraftStore",
    "bind_current_payload_statement",
    "insert_action_intent_statement",
    "insert_payload_version_statement",
    "select_prepared_draft_statement",
]
