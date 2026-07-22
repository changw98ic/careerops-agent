from __future__ import annotations

import re
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine, RowMapping

from careerops.application.outbox import ClaimedOutboxEvent
from careerops.application.submission_dispatch import (
    SyntheticProviderState,
    SyntheticSubmissionReceipt,
)
from careerops.application.submission_outbox import (
    BoundSyntheticSubmissionDispatch,
    PreparedSyntheticSubmissionState,
    SyntheticSubmissionDispatchError,
)

_OWNER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class PostgresSyntheticSubmissionCoordinator:
    """Database-owned boundary for synthetic submission prepare/receipt transitions."""

    def __init__(self, engine: Engine, *, owner: str = "careerops-outbox") -> None:
        if not _OWNER.fullmatch(owner):
            raise ValueError("outbox owner must be a bounded machine identifier")
        self._engine = engine
        self._owner = owner
        self._lease_tokens: dict[UUID, UUID] = {}

    def prepare(
        self,
        event: ClaimedOutboxEvent,
    ) -> BoundSyntheticSubmissionDispatch:
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    prepare_synthetic_submission_outbox_event_statement(
                        event_id=event.event_id,
                        owner=self._owner,
                        lease_token=event.lease_token,
                    )
                )
                .mappings()
                .one()
            )
        reason_code = row["reason_code"]
        if reason_code is not None:
            raise SyntheticSubmissionDispatchError(
                cast("str", reason_code),
                "synthetic submission prepare did not produce a runnable dispatch",
            )
        dispatch = _dispatch_from_row(row)
        if dispatch.state is PreparedSyntheticSubmissionState.READY:
            self._lease_tokens[event.event_id] = event.lease_token
        return dispatch

    def record_receipt(
        self,
        dispatch: BoundSyntheticSubmissionDispatch,
        receipt: SyntheticSubmissionReceipt,
    ) -> None:
        lease_token = self._lease_tokens.get(dispatch.event_id)
        if lease_token is None:
            raise RuntimeError("synthetic submission receipt has no prepared lease token")
        with self._engine.begin() as connection:
            connection.execute(
                record_synthetic_submission_outbox_receipt_statement(
                    event_id=dispatch.event_id,
                    owner=self._owner,
                    lease_token=lease_token,
                    receipt=receipt,
                    receipt_id=uuid4(),
                )
            )
        if receipt.provider_state is SyntheticProviderState.CONFIRMED:
            self._lease_tokens.pop(dispatch.event_id, None)

    def record_ambiguous(
        self,
        dispatch: BoundSyntheticSubmissionDispatch,
        *,
        error_code: str,
    ) -> None:
        lease_token = self._lease_tokens.get(dispatch.event_id)
        if lease_token is None:
            raise RuntimeError("synthetic submission ambiguity has no prepared lease token")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    record_synthetic_submission_outbox_ambiguity_statement(
                        event_id=dispatch.event_id,
                        owner=self._owner,
                        lease_token=lease_token,
                        error_code=error_code,
                    )
                )
        finally:
            self._lease_tokens.pop(dispatch.event_id, None)


def prepare_synthetic_submission_outbox_event_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
) -> sa.TextClause:
    return sa.text(
        "SELECT * FROM careerops.prepare_synthetic_submission_outbox_event("
        ":event_id, :lease_owner, :lease_token)"
    ).bindparams(
        event_id=event_id,
        lease_owner=owner,
        lease_token=lease_token,
    )


def record_synthetic_submission_outbox_receipt_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    receipt: SyntheticSubmissionReceipt,
    receipt_id: UUID,
) -> sa.TextClause:
    return sa.text(
        "SELECT careerops.record_synthetic_submission_outbox_receipt("
        ":event_id, :lease_owner, :lease_token, :provider, :provider_resource_id, "
        ":reconciliation_key, :provider_state, :received_at, :receipt_id)"
    ).bindparams(
        event_id=event_id,
        lease_owner=owner,
        lease_token=lease_token,
        provider=receipt.provider,
        provider_resource_id=receipt.provider_resource_id,
        reconciliation_key=receipt.reconciliation_key,
        provider_state=receipt.provider_state.value,
        received_at=receipt.received_at,
        receipt_id=receipt_id,
    )


def record_synthetic_submission_outbox_ambiguity_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    error_code: str,
) -> sa.TextClause:
    return sa.text(
        "SELECT careerops.record_synthetic_submission_outbox_ambiguity("
        ":event_id, :lease_owner, :lease_token, :error_code)"
    ).bindparams(
        event_id=event_id,
        lease_owner=owner,
        lease_token=lease_token,
        error_code=error_code,
    )


def _dispatch_from_row(row: RowMapping) -> BoundSyntheticSubmissionDispatch:
    existing_receipt = None
    existing_provider = row["existing_provider"]
    if existing_provider is not None:
        existing_receipt = SyntheticSubmissionReceipt(
            provider=cast("str", existing_provider),
            provider_resource_id=cast("str", row["existing_provider_resource_id"]),
            reconciliation_key=cast("str", row["reconciliation_key"]),
            provider_state=SyntheticProviderState(cast("str", row["existing_provider_state"])),
            received_at=cast("datetime", row["existing_received_at"]),
        )
    return BoundSyntheticSubmissionDispatch(
        event_id=cast("UUID", row["event_id"]),
        event_key=cast("str", row["event_key"]),
        action_intent_id=cast("UUID", row["action_intent_id"]),
        payload_version_id=cast("UUID", row["payload_version_id"]),
        reservation_key=cast("str", row["reservation_key"]),
        reconciliation_key=cast("str", row["reconciliation_key"]),
        state=PreparedSyntheticSubmissionState(cast("str", row["prepare_state"])),
        confirmed_receipt=existing_receipt,
    )


def compile_query_for_test(statement: sa.ClauseElement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


__all__ = [
    "PostgresSyntheticSubmissionCoordinator",
    "compile_query_for_test",
    "prepare_synthetic_submission_outbox_event_statement",
    "record_synthetic_submission_outbox_ambiguity_statement",
    "record_synthetic_submission_outbox_receipt_statement",
]
