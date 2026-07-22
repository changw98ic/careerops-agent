from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine, RowMapping

from careerops.application.gmail_send import (
    GmailSendAttachmentRef,
    GmailSendPayload,
    GmailSendProviderState,
)
from careerops.application.gmail_send_outbox import (
    GmailSendOutboxError,
    GmailSendProviderReceipt,
    GmailSendReconciliationJob,
    GmailSentMetadata,
    PreparedGmailSendDispatch,
    PreparedGmailSendState,
)
from careerops.application.outbox import ClaimedOutboxEvent

_OWNER = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")


class PostgresGmailSendCoordinator:
    """Database-owned boundary for Gmail send prepare/reconcile/receipt transitions."""

    def __init__(self, engine: Engine, *, owner: str = "careerops-gmail-send") -> None:
        if not _OWNER.fullmatch(owner):
            raise ValueError("Gmail send owner must be a bounded machine identifier")
        self._engine = engine
        self._owner = owner
        self._lease_tokens: dict[UUID, UUID] = {}

    def prepare(self, event: ClaimedOutboxEvent) -> PreparedGmailSendDispatch:
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    prepare_gmail_send_outbox_event_statement(
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
            raise GmailSendOutboxError(
                cast("str", reason_code),
                "Gmail send prepare did not produce a runnable dispatch",
            )
        dispatch = _dispatch_from_row(row)
        if dispatch.state is PreparedGmailSendState.READY:
            self._lease_tokens[event.event_id] = event.lease_token
        return dispatch

    def reconcile(
        self,
        dispatch: PreparedGmailSendDispatch,
        *,
        receipt: GmailSendProviderReceipt,
        metadata: GmailSentMetadata,
        reconciled_at: datetime,
    ) -> None:
        lease_token = self._require_lease(dispatch)
        with self._engine.begin() as connection:
            connection.execute(
                reconcile_gmail_send_outbox_event_statement(
                    event_id=dispatch.event_id,
                    owner=self._owner,
                    lease_token=lease_token,
                    receipt=receipt,
                    metadata=metadata,
                    reconciled_at=reconciled_at,
                )
            )

    def record_receipt(
        self,
        dispatch: PreparedGmailSendDispatch,
        receipt: GmailSendProviderReceipt,
    ) -> None:
        lease_token = self._require_lease(dispatch)
        with self._engine.begin() as connection:
            connection.execute(
                record_gmail_send_outbox_receipt_statement(
                    event_id=dispatch.event_id,
                    owner=self._owner,
                    lease_token=lease_token,
                    receipt=receipt,
                    receipt_id=uuid4(),
                )
            )
        if receipt.provider_state is GmailSendProviderState.SENT_CONFIRMED:
            self._lease_tokens.pop(dispatch.event_id, None)

    def record_ambiguous(
        self,
        dispatch: PreparedGmailSendDispatch,
        *,
        error_code: str,
    ) -> None:
        lease_token = self._require_lease(dispatch)
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    record_gmail_send_outbox_ambiguity_statement(
                        event_id=dispatch.event_id,
                        owner=self._owner,
                        lease_token=lease_token,
                        error_code=error_code,
                    )
                )
        finally:
            self._lease_tokens.pop(dispatch.event_id, None)

    def _require_lease(self, dispatch: PreparedGmailSendDispatch) -> UUID:
        lease_token = self._lease_tokens.get(dispatch.event_id)
        if lease_token is None:
            raise RuntimeError("Gmail send transition has no prepared lease token")
        return lease_token


class PostgresGmailSendOutboxStore:
    """Dedicated MAIL_SENDER outbox store using Gmail-send-only SQL functions."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        del now
        _validate_owner(owner)
        if event_key_prefix not in (None, "gmail-send:"):
            raise ValueError("Gmail send outbox store only claims gmail-send events")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        lease_seconds = _lease_seconds(lease_for)
        with self._engine.begin() as connection:
            rows = (
                connection.execute(
                    claim_gmail_send_outbox_events_statement(
                        owner=owner,
                        lease_seconds=lease_seconds,
                        limit=limit,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_claimed_from_row(row) for row in rows)

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None:
        del now
        with self._engine.begin() as connection:
            connection.execute(
                mark_gmail_send_outbox_published_statement(
                    event_id=event_id,
                    owner=owner,
                    lease_token=lease_token,
                )
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
        del now
        with self._engine.begin() as connection:
            connection.execute(
                release_gmail_send_outbox_event_statement(
                    event_id=event_id,
                    owner=owner,
                    lease_token=lease_token,
                    retry_at=retry_at,
                    error_code=error_code,
                    terminal=terminal,
                )
            )


class PostgresGmailSendReconciliationRepository:
    """Claim and settle ambiguous Gmail sends without re-sending provider POSTs."""

    def __init__(self, engine: Engine, *, lease_seconds: int = 300) -> None:
        if lease_seconds < 1 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        self._engine = engine
        self._lease_seconds = lease_seconds

    def claim_due(
        self,
        *,
        owner: str,
        now: datetime,
        limit: int,
    ) -> tuple[GmailSendReconciliationJob, ...]:
        del now
        _validate_owner(owner)
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with self._engine.begin() as connection:
            rows = (
                connection.execute(
                    claim_gmail_send_reconciliation_jobs_statement(
                        owner=owner,
                        lease_seconds=self._lease_seconds,
                        limit=limit,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_reconciliation_job_from_row(row) for row in rows)

    def record_confirmed(
        self,
        *,
        job: GmailSendReconciliationJob,
        metadata: GmailSentMetadata,
        now: datetime,
    ) -> None:
        receipt = GmailSendProviderReceipt(
            provider_message_id=metadata.provider_message_id,
            provider_thread_id=metadata.provider_thread_id,
            rfc_message_id=job.payload.message_id_header,
            provider_state=GmailSendProviderState.SENT_CONFIRMED,
            received_at=now,
        )
        with self._engine.begin() as connection:
            connection.execute(
                reconcile_ambiguous_gmail_send_outbox_event_statement(
                    event_id=job.event_id,
                    owner=job.lease_owner,
                    lease_token=job.lease_token,
                    receipt=receipt,
                    metadata=metadata,
                    reconciled_at=now,
                    receipt_id=uuid4(),
                )
            )

    def keep_ambiguous(
        self,
        *,
        job: GmailSendReconciliationJob,
        error_code: str,
        now: datetime,
    ) -> None:
        del now
        with self._engine.begin() as connection:
            connection.execute(
                keep_gmail_send_reconciliation_ambiguous_statement(
                    event_id=job.event_id,
                    owner=job.lease_owner,
                    lease_token=job.lease_token,
                    error_code=error_code,
                )
            )


def prepare_gmail_send_outbox_event_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
) -> sa.TextClause:
    return sa.text(
        "SELECT * FROM careerops.prepare_gmail_send_outbox_event("
        ":event_id, :lease_owner, :lease_token)"
    ).bindparams(
        event_id=event_id,
        lease_owner=owner,
        lease_token=lease_token,
    )


def claim_gmail_send_outbox_events_statement(
    *,
    owner: str,
    lease_seconds: int,
    limit: int,
) -> sa.TextClause:
    return sa.text(
        "SELECT * FROM careerops.claim_gmail_send_outbox_events("
        ":lease_owner, :lease_seconds, :limit)"
    ).bindparams(
        lease_owner=owner,
        lease_seconds=lease_seconds,
        limit=limit,
    )


def mark_gmail_send_outbox_published_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
) -> sa.TextClause:
    return sa.text(
        "SELECT careerops.mark_gmail_send_outbox_published(:event_id, :lease_owner, :lease_token)"
    ).bindparams(event_id=event_id, lease_owner=owner, lease_token=lease_token)


def release_gmail_send_outbox_event_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    retry_at: datetime,
    error_code: str,
    terminal: bool,
) -> sa.TextClause:
    if not _ERROR_CODE.fullmatch(error_code):
        raise ValueError("error_code must be a bounded machine code")
    return sa.text(
        "SELECT careerops.release_gmail_send_outbox_event("
        ":event_id, :lease_owner, :lease_token, :retry_at, :error_code, :terminal)"
    ).bindparams(
        event_id=event_id,
        lease_owner=owner,
        lease_token=lease_token,
        retry_at=retry_at,
        error_code=error_code,
        terminal=terminal,
    )


def reconcile_gmail_send_outbox_event_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    receipt: GmailSendProviderReceipt,
    metadata: GmailSentMetadata,
    reconciled_at: datetime,
) -> sa.TextClause:
    return sa.text(
        "SELECT careerops.reconcile_gmail_send_outbox_event("
        ":event_id, :lease_owner, :lease_token, :provider_message_id, "
        ":provider_thread_id, :rfc_message_id, :sent_metadata_json, :reconciled_at)"
    ).bindparams(
        sa.bindparam("sent_metadata_json", value=_metadata_json(metadata), type_=postgresql.JSONB),
        event_id=event_id,
        lease_owner=owner,
        lease_token=lease_token,
        provider_message_id=receipt.provider_message_id,
        provider_thread_id=receipt.provider_thread_id,
        rfc_message_id=receipt.rfc_message_id,
        reconciled_at=reconciled_at,
    )


def record_gmail_send_outbox_receipt_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    receipt: GmailSendProviderReceipt,
    receipt_id: UUID,
) -> sa.TextClause:
    return sa.text(
        "SELECT careerops.record_gmail_send_outbox_receipt("
        ":event_id, :lease_owner, :lease_token, :provider_message_id, "
        ":provider_thread_id, :rfc_message_id, :provider_state, :received_at, :receipt_id)"
    ).bindparams(
        event_id=event_id,
        lease_owner=owner,
        lease_token=lease_token,
        provider_message_id=receipt.provider_message_id,
        provider_thread_id=receipt.provider_thread_id,
        rfc_message_id=receipt.rfc_message_id,
        provider_state=receipt.provider_state.value,
        received_at=receipt.received_at,
        receipt_id=receipt_id,
    )


def record_gmail_send_outbox_ambiguity_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    error_code: str,
) -> sa.TextClause:
    if not _ERROR_CODE.fullmatch(error_code):
        raise ValueError("error_code must be a bounded machine code")
    return sa.text(
        "SELECT careerops.record_gmail_send_outbox_ambiguity("
        ":event_id, :lease_owner, :lease_token, :error_code)"
    ).bindparams(
        event_id=event_id,
        lease_owner=owner,
        lease_token=lease_token,
        error_code=error_code,
    )


def claim_gmail_send_reconciliation_jobs_statement(
    *,
    owner: str,
    lease_seconds: int,
    limit: int,
) -> sa.TextClause:
    return sa.text(
        "SELECT * FROM careerops.claim_gmail_send_reconciliation_jobs("
        ":lease_owner, :lease_seconds, :limit)"
    ).bindparams(
        lease_owner=owner,
        lease_seconds=lease_seconds,
        limit=limit,
    )


def reconcile_ambiguous_gmail_send_outbox_event_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    receipt: GmailSendProviderReceipt,
    metadata: GmailSentMetadata,
    reconciled_at: datetime,
    receipt_id: UUID,
) -> sa.TextClause:
    return sa.text(
        "SELECT careerops.reconcile_ambiguous_gmail_send_outbox_event("
        ":event_id, :lease_owner, :lease_token, :provider_message_id, "
        ":provider_thread_id, :rfc_message_id, :sent_metadata_json, "
        ":reconciled_at, :receipt_id)"
    ).bindparams(
        sa.bindparam("sent_metadata_json", value=_metadata_json(metadata), type_=postgresql.JSONB),
        event_id=event_id,
        lease_owner=owner,
        lease_token=lease_token,
        provider_message_id=receipt.provider_message_id,
        provider_thread_id=receipt.provider_thread_id,
        rfc_message_id=receipt.rfc_message_id,
        reconciled_at=reconciled_at,
        receipt_id=receipt_id,
    )


def keep_gmail_send_reconciliation_ambiguous_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    error_code: str,
) -> sa.TextClause:
    if not _ERROR_CODE.fullmatch(error_code):
        raise ValueError("error_code must be a bounded machine code")
    return sa.text(
        "SELECT careerops.keep_gmail_send_reconciliation_ambiguous("
        ":event_id, :lease_owner, :lease_token, :error_code)"
    ).bindparams(
        event_id=event_id,
        lease_owner=owner,
        lease_token=lease_token,
        error_code=error_code,
    )


def _dispatch_from_row(row: RowMapping) -> PreparedGmailSendDispatch:
    payload = _payload_from_row(row)
    existing_receipt = None
    if row["existing_provider_message_id"] is not None:
        existing_receipt = GmailSendProviderReceipt(
            provider_message_id=cast("str", row["existing_provider_message_id"]),
            provider_thread_id=cast("str", row["existing_provider_thread_id"]),
            rfc_message_id=cast("str", row["existing_rfc_message_id"]),
            provider_state=GmailSendProviderState(cast("str", row["existing_provider_state"])),
            received_at=cast("datetime", row["existing_received_at"]),
        )
    return PreparedGmailSendDispatch(
        event_id=cast("UUID", row["event_id"]),
        event_key=cast("str", row["event_key"]),
        action_intent_id=cast("UUID", row["action_intent_id"]),
        payload_version_id=cast("UUID", row["payload_version_id"]),
        reservation_key=cast("str", row["reservation_key"]),
        reconciliation_key=cast("str", row["reconciliation_key"]),
        send_account_id=cast("UUID", row["send_account_id"]),
        send_credential_handle=cast("str", row["send_credential_handle"]),
        readonly_credential_handle=cast("str", row["readonly_credential_handle"]),
        account_subject=cast("str", row["account_subject"]),
        payload=payload,
        state=PreparedGmailSendState(cast("str", row["prepare_state"])),
        confirmed_receipt=existing_receipt,
    )


def _payload_from_row(row: RowMapping) -> GmailSendPayload:
    raw = row["payload_json"]
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(decoded, Mapping):
        raise GmailSendOutboxError("GMAIL_SEND_PAYLOAD_INVALID", "payload_json must be an object")
    payload = cast("Mapping[str, object]", decoded)
    attachments_raw = payload.get("attachment_refs", ())
    attachments_seq: Sequence[object] = (
        cast("Sequence[object]", attachments_raw)
        if isinstance(attachments_raw, Sequence) and not isinstance(attachments_raw, (str, bytes))
        else ()
    )
    attachments = tuple(
        _attachment_ref(cast("Mapping[str, object]", item))
        for item in attachments_seq
        if isinstance(item, Mapping)
    )
    send_payload = GmailSendPayload(
        sender=_required_str(payload, "sender"),
        recipient=_required_str(payload, "recipient"),
        subject=_required_str(payload, "subject"),
        text_body=_required_str(payload, "text_body"),
        attachment_refs=attachments,
        thread_id=_optional_str(payload, "thread_id"),
        in_reply_to_message_id=_optional_str(payload, "in_reply_to_message_id"),
    )
    expected_hash = payload.get("payload_hash")
    if isinstance(expected_hash, str) and expected_hash != send_payload.payload_hash:
        raise GmailSendOutboxError(
            "GMAIL_SEND_PAYLOAD_HASH_MISMATCH",
            "payload_json.payload_hash does not match canonical GmailSendPayload",
        )
    return send_payload


def _attachment_ref(payload: Mapping[str, object]) -> GmailSendAttachmentRef:
    return GmailSendAttachmentRef(
        object_key=_required_str(payload, "object_key"),
        sha256=_required_str(payload, "sha256"),
        size_bytes=_required_int(payload, "size_bytes"),
        filename=_required_str(payload, "filename"),
        content_type=_required_str(payload, "content_type"),
    )


def _metadata_json(metadata: GmailSentMetadata) -> dict[str, object]:
    return {
        "provider_message_id": metadata.provider_message_id,
        "provider_thread_id": metadata.provider_thread_id,
        "label_ids": list(metadata.label_ids),
        "headers": dict(metadata.headers),
    }


def _claimed_from_row(row: RowMapping) -> ClaimedOutboxEvent:
    return ClaimedOutboxEvent(
        event_id=cast("UUID", row["event_id"]),
        event_key=cast("str", row["event_key"]),
        action_intent_id=cast("UUID", row["action_intent_id"]),
        payload_version_id=cast("UUID", row["payload_version_id"]),
        event_type=cast("str", row["event_type"]),
        available_at=cast("datetime", row["available_at"]),
        attempt_count=cast("int", row["attempt_count"]),
        lease_token=cast("UUID", row["lease_token"]),
        lease_until=cast("datetime", row["lease_until"]),
    )


def _reconciliation_job_from_row(row: RowMapping) -> GmailSendReconciliationJob:
    return GmailSendReconciliationJob(
        event_id=cast("UUID", row["event_id"]),
        event_key=cast("str", row["event_key"]),
        action_intent_id=cast("UUID", row["action_intent_id"]),
        payload_version_id=cast("UUID", row["payload_version_id"]),
        reservation_key=cast("str", row["reservation_key"]),
        reconciliation_key=cast("str", row["reconciliation_key"]),
        send_account_id=cast("UUID", row["send_account_id"]),
        readonly_credential_handle=cast("str", row["readonly_credential_handle"]),
        account_subject=cast("str", row["account_subject"]),
        payload=_payload_from_row(row),
        lease_owner=cast("str", row["lease_owner"]),
        lease_token=cast("UUID", row["lease_token"]),
        attempt_count=cast("int", row["attempt_count"]),
    )


def _required_str(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise GmailSendOutboxError(
            "GMAIL_SEND_PAYLOAD_INVALID",
            f"payload_json.{field_name} must be a non-empty string",
        )
    return value


def _optional_str(payload: Mapping[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise GmailSendOutboxError(
            "GMAIL_SEND_PAYLOAD_INVALID",
            f"payload_json.{field_name} must be null or a non-empty string",
        )
    return value


def _required_int(payload: Mapping[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GmailSendOutboxError(
            "GMAIL_SEND_PAYLOAD_INVALID",
            f"payload_json.{field_name} must be an integer",
        )
    return value


def _validate_owner(owner: str) -> None:
    if not _OWNER.fullmatch(owner):
        raise ValueError("owner must be a bounded machine identifier")


def _lease_seconds(lease_for: timedelta) -> int:
    if lease_for <= timedelta(0) or lease_for > timedelta(hours=1):
        raise ValueError("lease_for must be positive and at most one hour")
    return max(1, int(lease_for.total_seconds()))


def compile_query_for_test(statement: sa.ClauseElement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


__all__ = [
    "PostgresGmailSendCoordinator",
    "PostgresGmailSendOutboxStore",
    "PostgresGmailSendReconciliationRepository",
    "_dispatch_from_row",
    "claim_gmail_send_outbox_events_statement",
    "claim_gmail_send_reconciliation_jobs_statement",
    "compile_query_for_test",
    "keep_gmail_send_reconciliation_ambiguous_statement",
    "mark_gmail_send_outbox_published_statement",
    "prepare_gmail_send_outbox_event_statement",
    "reconcile_ambiguous_gmail_send_outbox_event_statement",
    "reconcile_gmail_send_outbox_event_statement",
    "record_gmail_send_outbox_ambiguity_statement",
    "record_gmail_send_outbox_receipt_statement",
    "release_gmail_send_outbox_event_statement",
]
