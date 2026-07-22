from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from careerops.application.gmail_send import GmailSendPayload, GmailSendProviderState
from careerops.application.gmail_send_outbox import (
    GmailSendProviderReceipt,
    GmailSentMetadata,
)
from careerops.infrastructure.database.gmail_send import (
    _dispatch_from_row,
    claim_gmail_send_outbox_events_statement,
    claim_gmail_send_reconciliation_jobs_statement,
    keep_gmail_send_reconciliation_ambiguous_statement,
    mark_gmail_send_outbox_published_statement,
    prepare_gmail_send_outbox_event_statement,
    reconcile_ambiguous_gmail_send_outbox_event_statement,
    reconcile_gmail_send_outbox_event_statement,
    record_gmail_send_outbox_ambiguity_statement,
    record_gmail_send_outbox_receipt_statement,
    release_gmail_send_outbox_event_statement,
)

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


def _payload_json() -> dict[str, object]:
    payload = GmailSendPayload(
        sender="alice@example.com",
        recipient="hr@example.com",
        subject="Application follow-up",
        text_body="Hello",
    )
    canonical = dict(payload.canonical_without_hash())
    canonical["payload_hash"] = payload.payload_hash
    canonical["message_id_header"] = payload.message_id_header
    return canonical


def _receipt() -> GmailSendProviderReceipt:
    payload = GmailSendPayload(
        sender="alice@example.com",
        recipient="hr@example.com",
        subject="Application follow-up",
        text_body="Hello",
    )
    return GmailSendProviderReceipt(
        provider_message_id="gmail-message-1",
        provider_thread_id="gmail-thread-1",
        rfc_message_id=payload.message_id_header,
        provider_state=GmailSendProviderState.SENT_CONFIRMED,
        received_at=NOW,
    )


def _metadata(receipt: GmailSendProviderReceipt) -> GmailSentMetadata:
    return GmailSentMetadata(
        provider_message_id=receipt.provider_message_id,
        provider_thread_id=receipt.provider_thread_id,
        label_ids=("SENT",),
        headers={
            "To": "hr@example.com",
            "Subject": "Application follow-up",
            "Message-ID": receipt.rfc_message_id,
        },
    )


def test_prepare_statement_calls_narrow_mail_sender_security_definer_function() -> None:
    statement = prepare_gmail_send_outbox_event_statement(
        event_id=uuid4(),
        owner="mail-sender-1",
        lease_token=uuid4(),
    )

    assert statement.text == (
        "SELECT * FROM careerops.prepare_gmail_send_outbox_event("
        ":event_id, :lease_owner, :lease_token)"
    )
    assert set(statement._bindparams) == {"event_id", "lease_owner", "lease_token"}


def test_dedicated_outbox_store_statements_do_not_require_generic_table_privileges() -> None:
    event_id = uuid4()
    lease_token = uuid4()
    claim = claim_gmail_send_outbox_events_statement(
        owner="mail-sender-1",
        lease_seconds=30,
        limit=5,
    )
    published = mark_gmail_send_outbox_published_statement(
        event_id=event_id,
        owner="mail-sender-1",
        lease_token=lease_token,
    )
    released = release_gmail_send_outbox_event_statement(
        event_id=event_id,
        owner="mail-sender-1",
        lease_token=lease_token,
        retry_at=NOW + timedelta(minutes=5),
        error_code="GMAIL_SEND_SENT_LABEL_MISSING",
        terminal=True,
    )

    assert claim.text == (
        "SELECT * FROM careerops.claim_gmail_send_outbox_events("
        ":lease_owner, :lease_seconds, :limit)"
    )
    assert published.text == (
        "SELECT careerops.mark_gmail_send_outbox_published(:event_id, :lease_owner, :lease_token)"
    )
    assert released.text == (
        "SELECT careerops.release_gmail_send_outbox_event("
        ":event_id, :lease_owner, :lease_token, :retry_at, :error_code, :terminal)"
    )


def test_record_reconcile_receipt_and_ambiguity_statements_are_narrow_functions() -> None:
    event_id = uuid4()
    lease_token = uuid4()
    receipt = _receipt()
    metadata = _metadata(receipt)
    reconcile = reconcile_gmail_send_outbox_event_statement(
        event_id=event_id,
        owner="mail-sender-1",
        lease_token=lease_token,
        receipt=receipt,
        metadata=metadata,
        reconciled_at=NOW,
    )
    receipt_statement = record_gmail_send_outbox_receipt_statement(
        event_id=event_id,
        owner="mail-sender-1",
        lease_token=lease_token,
        receipt=receipt,
        receipt_id=uuid4(),
    )
    ambiguous = record_gmail_send_outbox_ambiguity_statement(
        event_id=event_id,
        owner="mail-sender-1",
        lease_token=lease_token,
        error_code="GMAIL_SEND_POST_OUTCOME_AMBIGUOUS",
    )

    assert "careerops.reconcile_gmail_send_outbox_event" in reconcile.text
    assert reconcile._bindparams["provider_message_id"].value == "gmail-message-1"
    metadata_json = reconcile._bindparams["sent_metadata_json"].value
    assert isinstance(metadata_json, dict)
    assert metadata_json["label_ids"] == ["SENT"]
    assert "careerops.record_gmail_send_outbox_receipt" in receipt_statement.text
    assert receipt_statement._bindparams["provider_state"].value == "sent_confirmed"
    assert ambiguous.text == (
        "SELECT careerops.record_gmail_send_outbox_ambiguity("
        ":event_id, :lease_owner, :lease_token, :error_code)"
    )


def test_ambiguous_reconciliation_statements_are_replay_safe_and_do_not_send() -> None:
    event_id = uuid4()
    lease_token = uuid4()
    receipt = _receipt()
    claim = claim_gmail_send_reconciliation_jobs_statement(
        owner="mail-reconciler-1",
        lease_seconds=60,
        limit=10,
    )
    confirmed = reconcile_ambiguous_gmail_send_outbox_event_statement(
        event_id=event_id,
        owner="mail-reconciler-1",
        lease_token=lease_token,
        receipt=receipt,
        metadata=_metadata(receipt),
        reconciled_at=NOW,
        receipt_id=uuid4(),
    )
    still_ambiguous = keep_gmail_send_reconciliation_ambiguous_statement(
        event_id=event_id,
        owner="mail-reconciler-1",
        lease_token=lease_token,
        error_code="GMAIL_SEND_SENT_MESSAGE_NOT_FOUND",
    )

    assert claim.text == (
        "SELECT * FROM careerops.claim_gmail_send_reconciliation_jobs("
        ":lease_owner, :lease_seconds, :limit)"
    )
    assert "reconcile_ambiguous_gmail_send_outbox_event" in confirmed.text
    assert "keep_gmail_send_reconciliation_ambiguous" in still_ambiguous.text
    assert "send" not in claim.text.removeprefix("SELECT * FROM careerops.claim_gmail_send")


def test_dispatch_row_maps_domain_payload_and_confirmed_receipt_for_replay() -> None:
    payload_json = _payload_json()
    event_id = uuid4()
    intent_id = uuid4()
    payload_id = uuid4()
    account_id = uuid4()
    row = {
        "event_id": event_id,
        "event_key": "gmail-send:" + "a" * 64,
        "action_intent_id": intent_id,
        "payload_version_id": payload_id,
        "reservation_key": "a" * 64,
        "reconciliation_key": "b" * 64,
        "send_account_id": account_id,
        "send_credential_handle": "send-handle-1",
        "readonly_credential_handle": "readonly-handle-1",
        "account_subject": "alice@example.com",
        "payload_json": json.dumps(payload_json),
        "existing_provider_message_id": "gmail-message-1",
        "existing_provider_thread_id": "gmail-thread-1",
        "existing_rfc_message_id": payload_json["message_id_header"],
        "existing_provider_state": "sent_confirmed",
        "existing_received_at": NOW,
        "prepare_state": "already_confirmed",
        "reason_code": None,
    }

    dispatch = _dispatch_from_row(row)  # type: ignore[arg-type]

    assert dispatch.event_id == event_id
    assert dispatch.payload.sender == "alice@example.com"
    assert dispatch.payload.recipient == "hr@example.com"
    assert dispatch.payload.payload_hash == payload_json["payload_hash"]
    assert dispatch.confirmed_receipt is not None
    assert dispatch.confirmed_receipt.provider_state is GmailSendProviderState.SENT_CONFIRMED
