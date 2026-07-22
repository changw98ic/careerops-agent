from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from careerops.application.gmail_send import (
    GMAIL_SEND_ACTION_KIND,
    GMAIL_SEND_ADAPTER_ID,
    GMAIL_SEND_CHANNEL,
    GMAIL_SEND_EXACT_APPROVAL_KIND,
    GMAIL_SEND_FIXTURE_ID,
    GMAIL_SEND_TARGET_HOST,
    GmailSendAttachmentRef,
    GmailSendAuthority,
    GmailSendDecisionState,
    GmailSendPayload,
    GmailSendPlanner,
    GmailSendProviderState,
    QualifiedGmailSendRequest,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def attachment_ref(data: bytes = b"resume pdf bytes") -> GmailSendAttachmentRef:
    return GmailSendAttachmentRef(
        object_key="resume:approved:v1",
        filename="resume.pdf",
        content_type="application/pdf",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def payload() -> GmailSendPayload:
    return GmailSendPayload(
        sender="candidate@example.com",
        recipient="recruiter@example.com",
        subject="Application follow-up",
        text_body="Hello, attaching the reviewed follow-up package.",
        attachment_refs=(attachment_ref(),),
        thread_id="gmail-thread-1",
        in_reply_to_message_id="<provider-message@example.com>",
    )


def authority(send_payload: GmailSendPayload) -> GmailSendAuthority:
    return GmailSendAuthority(
        campaign_id=UUID("00000000-0000-0000-0000-000000000001"),
        grant_version_id=UUID("00000000-0000-0000-0000-000000000002"),
        authorization_id=UUID("00000000-0000-0000-0000-000000000003"),
        action_intent_id=UUID("00000000-0000-0000-0000-000000000004"),
        payload_version_id=UUID("00000000-0000-0000-0000-000000000005"),
        policy_decision_id=UUID("00000000-0000-0000-0000-000000000006"),
        action_kind=GMAIL_SEND_ACTION_KIND,
        channel=GMAIL_SEND_CHANNEL,
        target_host=GMAIL_SEND_TARGET_HOST,
        approval_kind=GMAIL_SEND_EXACT_APPROVAL_KIND,
        approved_payload_hash=send_payload.payload_hash,
        approved_target_hash=send_payload.target_hash,
        approved_grant_material_hash=send_payload.grant_material_hash,
        active_grant=True,
        authorized_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


def test_payload_hash_binds_exact_target_body_recipient_and_attachments() -> None:
    send_payload = payload()

    assert send_payload.recipient_sha256 == hashlib.sha256(b"recruiter@example.com").hexdigest()
    assert send_payload.body_sha256 == hashlib.sha256(send_payload.text_body.encode()).hexdigest()
    assert send_payload.per_recipient_cap_key == send_payload.recipient_sha256
    assert send_payload.canonical_target() == {
        "version": "gmail-send-target.v1",
        "target_host": GMAIL_SEND_TARGET_HOST,
        "channel": GMAIL_SEND_CHANNEL,
        "adapter_id": GMAIL_SEND_ADAPTER_ID,
        "fixture_id": GMAIL_SEND_FIXTURE_ID,
        "recipient_sha256": send_payload.recipient_sha256,
        "body_sha256": send_payload.body_sha256,
        "attachment_sha256": [send_payload.attachment_refs[0].sha256],
        "grant_material_hash": send_payload.grant_material_hash,
    }
    assert (
        send_payload.payload_hash
        != GmailSendPayload(
            sender="candidate@example.com",
            recipient="other@example.com",
            subject=send_payload.subject,
            text_body=send_payload.text_body,
            attachment_refs=send_payload.attachment_refs,
        ).payload_hash
    )
    assert (
        send_payload.grant_material_hash
        != GmailSendPayload(
            sender="candidate@example.com",
            recipient="recruiter@example.com",
            subject=send_payload.subject,
            text_body="mutated body",
            attachment_refs=send_payload.attachment_refs,
        ).grant_material_hash
    )


def test_planner_reserves_only_exact_human_reviewed_gmail_send_authority() -> None:
    send_payload = payload()
    decision = GmailSendPlanner().plan(
        QualifiedGmailSendRequest(
            authority=authority(send_payload),
            payload=send_payload,
            now=NOW,
        )
    )

    assert decision.state is GmailSendDecisionState.RESERVE_GMAIL_SEND
    assert decision.can_reserve is True
    assert decision.reason_codes == ("GMAIL_SEND_QUALIFIED_FOR_RESERVATION",)
    assert decision.reservation_key
    assert decision.outbox_event_key == f"gmail-send:{decision.reservation_key}"
    assert decision.reconciliation_key
    assert decision.message_id_header == send_payload.message_id_header


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("action_kind", "gmail_readonly_signal_review", "ACTION_KIND_MUST_BE_SEND_EMAIL"),
        ("channel", "gmail:readonly", "CHANNEL_MUST_BE_GMAIL_SEND"),
        ("target_host", "mail.google.com", "TARGET_HOST_MUST_BE_GMAIL_API"),
        ("approval_kind", "generic_human_approval", "EXACT_GMAIL_SEND_APPROVAL_REQUIRED"),
        ("active_grant", False, "ACTIVE_GRANT_REQUIRED"),
    ],
)
def test_planner_fails_closed_on_non_exact_authority(
    field: str,
    value: str | bool,
    reason: str,
) -> None:
    send_payload = payload()
    base = authority(send_payload)
    mutated = GmailSendAuthority(
        campaign_id=base.campaign_id,
        grant_version_id=base.grant_version_id,
        authorization_id=base.authorization_id,
        action_intent_id=base.action_intent_id,
        payload_version_id=base.payload_version_id,
        policy_decision_id=base.policy_decision_id,
        action_kind=str(value) if field == "action_kind" else base.action_kind,
        channel=str(value) if field == "channel" else base.channel,
        target_host=str(value) if field == "target_host" else base.target_host,
        approval_kind=str(value) if field == "approval_kind" else base.approval_kind,
        approved_payload_hash=base.approved_payload_hash,
        approved_target_hash=base.approved_target_hash,
        approved_grant_material_hash=base.approved_grant_material_hash,
        active_grant=bool(value) if field == "active_grant" else base.active_grant,
        authorized_at=base.authorized_at,
        expires_at=base.expires_at,
    )

    decision = GmailSendPlanner().plan(
        QualifiedGmailSendRequest(authority=mutated, payload=send_payload, now=NOW)
    )

    assert decision.state is GmailSendDecisionState.STOPPED
    assert reason in decision.reason_codes


def test_planner_rejects_generic_model_kill_switch_and_ambiguous_state() -> None:
    send_payload = payload()

    decision = GmailSendPlanner().plan(
        QualifiedGmailSendRequest(
            authority=authority(send_payload),
            payload=send_payload,
            now=NOW,
            global_kill_switch_active=True,
            campaign_kill_switch_active=True,
            provider_kill_switch_active=True,
            generic_approval_present=True,
            model_output_approval_present=True,
            provider_state=GmailSendProviderState.AMBIGUOUS,
        )
    )

    assert decision.state is GmailSendDecisionState.STOPPED
    for reason in (
        "GLOBAL_KILL_SWITCH_ACTIVE",
        "CAMPAIGN_KILL_SWITCH_ACTIVE",
        "PROVIDER_KILL_SWITCH_ACTIVE",
        "GENERIC_APPROVAL_NOT_EXECUTION_AUTHORITY",
        "MODEL_OUTPUT_NOT_EXECUTION_AUTHORITY",
        "AMBIGUOUS_PROVIDER_STATE",
    ):
        assert reason in decision.reason_codes


def test_attachment_contract_rejects_unapproved_shapes() -> None:
    with pytest.raises(ValueError, match="at most five"):
        GmailSendPayload(
            sender="candidate@example.com",
            recipient="recruiter@example.com",
            subject="Hello",
            text_body="Body",
            attachment_refs=(
                attachment_ref(b"1"),
                attachment_ref(b"2"),
                attachment_ref(b"3"),
                attachment_ref(b"4"),
                attachment_ref(b"5"),
                attachment_ref(b"6"),
            ),
        )

    with pytest.raises(ValueError, match="path separators"):
        GmailSendAttachmentRef(
            object_key="resume:bad",
            filename="../resume.pdf",
            content_type="application/pdf",
            size_bytes=1,
            sha256=hashlib.sha256(b"x").hexdigest(),
        )

    with pytest.raises(ValueError, match="total attachment"):
        GmailSendPayload(
            sender="candidate@example.com",
            recipient="recruiter@example.com",
            subject="Hello",
            text_body="Body",
            attachment_refs=(
                GmailSendAttachmentRef(
                    object_key="resume:large",
                    filename="large.pdf",
                    content_type="application/pdf",
                    size_bytes=(20 * 1024 * 1024) + 1,
                    sha256="0" * 64,
                ),
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject", "approved\r\nBcc: attacker@example.com"),
        ("filename", "resume.pdf\nX-Injected: true"),
    ],
)
def test_header_fields_reject_control_characters(field: str, value: str) -> None:
    if field == "subject":
        with pytest.raises(ValueError, match="control characters"):
            GmailSendPayload(
                sender="candidate@example.com",
                recipient="recruiter@example.com",
                subject=value,
                text_body="Body",
            )
        return
    with pytest.raises(ValueError, match="control characters"):
        GmailSendAttachmentRef(
            object_key="resume:approved:v1",
            filename=value,
            content_type="application/pdf",
            size_bytes=1,
            sha256=hashlib.sha256(b"x").hexdigest(),
        )


def test_thread_and_in_reply_to_must_be_bound_together() -> None:
    with pytest.raises(ValueError, match="supplied together"):
        GmailSendPayload(
            sender="candidate@example.com",
            recipient="recruiter@example.com",
            subject="Follow-up",
            text_body="Body",
            thread_id="thread-1",
        )
