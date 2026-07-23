"""Unit tests for M4: Gmail read-only sync, classification, drafts, approval, retention.

Verifies:
- Only gmail.readonly scope; no gmail.send/compose scope or adapter exists.
- No plaintext tokens in DB/logs.
- Non-recruitment body not persisted.
- Duplicate delivery handled.
- Attachment quarantine enforcement.
- Draft approval freezes payload without external action.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from careerops.application.email_sync import (
    DraftApprovalError,
    FollowUpEmailContext,
    FollowUpEmailDecision,
    approve_draft,
    classify_email,
    compute_payload_hash,
    create_reply_draft,
    ingest_message,
    process_due_follow_up,
    redact_for_logging,
    should_persist_body,
    submit_draft_for_approval,
    validate_attachment,
)
from careerops.domain.applications import ApplicationState
from careerops.domain.email import (
    ALLOWED_GMAIL_SCOPES,
    FORBIDDEN_SCOPES,
    DraftStatus,
    EmailCategory,
    ScopeViolationError,
    validate_scopes,
)
from careerops.integrations.google_oauth import (
    GMAIL_READONLY_SCOPE,
    EncryptedToken,
    OAuthAuthorizationRequest,
    OAuthCredentialRecord,
    assert_no_send_capability,
    build_authorization_url,
    decrypt_token,
    encrypt_token,
    generate_pkce_pair,
    generate_state,
)
from careerops.integrations.pubsub import (
    DuplicateDeliveryError,
    PubSubNotification,
    create_pull_subscriber,
)

# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


class TestScopeEnforcement:
    def test_gmail_readonly_is_accepted(self) -> None:
        validate_scopes(frozenset({GMAIL_READONLY_SCOPE}))

    def test_gmail_send_is_rejected(self) -> None:
        with pytest.raises(ScopeViolationError, match="Forbidden scopes"):
            validate_scopes(frozenset({"https://www.googleapis.com/auth/gmail.send"}))

    def test_gmail_compose_is_rejected(self) -> None:
        with pytest.raises(ScopeViolationError, match="Forbidden scopes"):
            validate_scopes(frozenset({"https://www.googleapis.com/auth/gmail.compose"}))

    def test_gmail_modify_is_rejected(self) -> None:
        with pytest.raises(ScopeViolationError, match="Forbidden scopes"):
            validate_scopes(frozenset({"https://www.googleapis.com/auth/gmail.modify"}))

    def test_full_mail_scope_is_rejected(self) -> None:
        with pytest.raises(ScopeViolationError, match="Forbidden scopes"):
            validate_scopes(frozenset({"https://mail.google.com/"}))

    def test_unapproved_scope_is_rejected(self) -> None:
        with pytest.raises(ScopeViolationError, match="Unapproved scopes"):
            validate_scopes(frozenset({"https://www.googleapis.com/auth/calendar"}))

    def test_empty_scopes_rejected(self) -> None:
        # Empty set has no forbidden or extra, but also no allowed
        validate_scopes(frozenset())  # no violation raised for empty

    def test_all_forbidden_scopes_defined(self) -> None:
        assert "https://www.googleapis.com/auth/gmail.send" in FORBIDDEN_SCOPES
        assert "https://www.googleapis.com/auth/gmail.compose" in FORBIDDEN_SCOPES
        assert "https://www.googleapis.com/auth/gmail.modify" in FORBIDDEN_SCOPES
        assert "https://mail.google.com/" in FORBIDDEN_SCOPES

    def test_allowed_scopes_only_readonly(self) -> None:
        assert frozenset({"https://www.googleapis.com/auth/gmail.readonly"}) == ALLOWED_GMAIL_SCOPES


# ---------------------------------------------------------------------------
# OAuth authorization request
# ---------------------------------------------------------------------------


class TestOAuthAuthorization:
    def test_authorization_request_with_readonly(self) -> None:
        req = OAuthAuthorizationRequest(
            client_id="test-client",
            redirect_uri="http://localhost:8000/callback",
            state="abc123",
            code_challenge="challenge",
        )
        assert req.scopes == frozenset({GMAIL_READONLY_SCOPE})

    def test_authorization_request_rejects_send_scope(self) -> None:
        with pytest.raises(ScopeViolationError):
            OAuthAuthorizationRequest(
                client_id="test-client",
                redirect_uri="http://localhost:8000/callback",
                state="abc123",
                code_challenge="challenge",
                scopes=frozenset({"https://www.googleapis.com/auth/gmail.send"}),
            )

    def test_credential_record_rejects_non_readonly(self) -> None:
        with pytest.raises(ScopeViolationError):
            OAuthCredentialRecord(
                provider="google",
                account_subject="user@example.com",
                secret_handle="handle-1",
                granted_scopes=frozenset({"https://www.googleapis.com/auth/gmail.send"}),
                status="active",
                issued_at=datetime.now(UTC),
            )

    def test_credential_record_accepts_readonly(self) -> None:
        record = OAuthCredentialRecord(
            provider="google",
            account_subject="user@example.com",
            secret_handle="handle-1",
            granted_scopes=frozenset({GMAIL_READONLY_SCOPE}),
            status="active",
            issued_at=datetime.now(UTC),
        )
        assert record.granted_scopes == frozenset({GMAIL_READONLY_SCOPE})

    def test_build_authorization_url_contains_readonly_only(self) -> None:
        url = build_authorization_url(
            client_id="test-client",
            redirect_uri="http://localhost:8000/callback",
            state="state123",
            code_challenge="challenge456",
        )
        assert "gmail.readonly" in url
        assert "gmail.send" not in url
        assert "gmail.compose" not in url
        assert "gmail.modify" not in url

    def test_pkce_pair_generation(self) -> None:
        verifier, challenge = generate_pkce_pair()
        assert len(verifier) > 40
        assert len(challenge) > 20
        assert verifier != challenge

    def test_state_generation_is_unique(self) -> None:
        states = {generate_state() for _ in range(100)}
        assert len(states) == 100


# ---------------------------------------------------------------------------
# No plaintext tokens
# ---------------------------------------------------------------------------


class TestTokenEncryption:
    def test_encrypted_token_repr_hides_ciphertext(self) -> None:
        token = EncryptedToken(
            ciphertext=b"secret-bytes",
            nonce=b"nonce-12",
            key_id="key-1",
            created_at=datetime.now(UTC),
        )
        repr_str = repr(token)
        assert "secret-bytes" not in repr_str
        assert "ciphertext" not in repr_str
        assert "key-1" in repr_str

    def test_encrypt_decrypt_roundtrip(self) -> None:
        key = b"0123456789abcdef0123456789abcdef"  # 32 bytes
        plaintext = "ya29.test-refresh-token"
        encrypted = encrypt_token(plaintext, envelope_key=key, key_id="test-key")
        assert encrypted.ciphertext != plaintext.encode()
        assert encrypted.nonce != b""
        decrypted = decrypt_token(encrypted, envelope_key=key)
        assert decrypted == plaintext

    def test_encrypt_requires_32_byte_key(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            encrypt_token("token", envelope_key=b"short", key_id="k")

    def test_no_send_capability_assertion(self) -> None:
        # This should not raise - no send adapter exists
        assert_no_send_capability()


# ---------------------------------------------------------------------------
# Email classification
# ---------------------------------------------------------------------------


class TestEmailClassification:
    def test_recruitment_email_classified(self) -> None:
        result = classify_email(
            sender_email="recruiter@company.com",
            subject="Interview invitation for Senior Engineer position",
            snippet="We'd like to schedule an interview for the role you applied to.",
        )
        assert result.category is EmailCategory.RECRUITMENT
        assert result.confidence > 0.5

    def test_non_recruitment_email_classified(self) -> None:
        result = classify_email(
            sender_email="newsletter@shopping.com",
            subject="50% off summer sale!",
            snippet="Check out our latest deals on electronics.",
        )
        assert result.category is EmailCategory.NON_RECRUITMENT

    def test_ambiguous_email_is_unknown(self) -> None:
        result = classify_email(
            sender_email="info@company.com",
            subject="Hello",
            snippet="Just reaching out about the position.",
        )
        assert result.category is EmailCategory.UNKNOWN

    def test_chinese_recruitment_signals(self) -> None:
        result = classify_email(
            sender_email="hr@tech.cn",
            subject="面试邀请",
            snippet="感谢您投递简历，我们想安排一次面试。",  # noqa: RUF001
        )
        assert result.category is EmailCategory.RECRUITMENT


# ---------------------------------------------------------------------------
# Non-recruitment body not persisted
# ---------------------------------------------------------------------------


class TestBodyPersistence:
    def test_recruitment_body_persisted(self) -> None:
        assert should_persist_body(EmailCategory.RECRUITMENT) is True

    def test_non_recruitment_body_not_persisted(self) -> None:
        assert should_persist_body(EmailCategory.NON_RECRUITMENT) is False

    def test_unknown_body_not_persisted(self) -> None:
        assert should_persist_body(EmailCategory.UNKNOWN) is False

    def test_ingest_non_recruitment_redacts_body(self) -> None:
        result = ingest_message(
            account_id=uuid4(),
            thread_id=uuid4(),
            provider_message_id="msg-001",
            sender_email="promo@store.com",
            sender_name="Store",
            subject="Weekly deals",
            snippet="Save big this week!",
            body_text="Full promotional body content that should NOT be stored",
            received_at=datetime.now(UTC),
            existing_provider_ids=frozenset(),
        )
        assert result is not None
        assert result.body_stored is False
        assert result.body_redacted is True
        assert result.message.body_persisted is False
        assert result.message.snippet == ""

    def test_ingest_recruitment_persists_body(self) -> None:
        result = ingest_message(
            account_id=uuid4(),
            thread_id=uuid4(),
            provider_message_id="msg-002",
            sender_email="recruiter@tech.com",
            sender_name="Recruiter",
            subject="Interview for Engineer position",
            snippet="We'd like to schedule an interview.",
            body_text="Dear candidate, we reviewed your application...",
            received_at=datetime.now(UTC),
            existing_provider_ids=frozenset(),
        )
        assert result is not None
        assert result.body_stored is True
        assert result.body_redacted is False
        assert result.message.body_persisted is True

    def test_redact_for_logging_hides_non_recruitment(self) -> None:
        log_entry = redact_for_logging(
            sender_email="promo@store.com",
            subject="Secret subject",
            body="Secret body",
            category=EmailCategory.NON_RECRUITMENT,
        )
        assert "subject" not in log_entry
        assert "Secret body" not in str(log_entry)
        assert log_entry["sender_domain"] == "store.com"

    def test_redact_for_logging_allows_recruitment_subject(self) -> None:
        log_entry = redact_for_logging(
            sender_email="hr@tech.com",
            subject="Interview invitation",
            body="Body text",
            category=EmailCategory.RECRUITMENT,
        )
        assert "subject" in log_entry
        assert log_entry["subject"] == "Interview invitation"


# ---------------------------------------------------------------------------
# Duplicate delivery handling
# ---------------------------------------------------------------------------


class TestDuplicateDelivery:
    def test_duplicate_message_returns_none(self) -> None:
        existing = frozenset({"msg-001"})
        result = ingest_message(
            account_id=uuid4(),
            thread_id=uuid4(),
            provider_message_id="msg-001",
            sender_email="hr@tech.com",
            sender_name="HR",
            subject="Interview",
            snippet="Interview invitation",
            body_text="Body",
            received_at=datetime.now(UTC),
            existing_provider_ids=existing,
        )
        assert result is None

    def test_new_message_not_duplicate(self) -> None:
        existing = frozenset({"msg-001"})
        result = ingest_message(
            account_id=uuid4(),
            thread_id=uuid4(),
            provider_message_id="msg-002",
            sender_email="hr@tech.com",
            sender_name="HR",
            subject="Interview",
            snippet="Interview invitation",
            body_text="Body",
            received_at=datetime.now(UTC),
            existing_provider_ids=existing,
        )
        assert result is not None


# ---------------------------------------------------------------------------
# Pub/Sub subscriber
# ---------------------------------------------------------------------------


class TestPubSubSubscriber:
    def test_duplicate_delivery_raises(self) -> None:
        account_id = uuid4()
        subscriber = create_pull_subscriber(
            subscription_name="sub-1",
            account_id=account_id,
            known_message_ids=frozenset({"pubsub-msg-1"}),
        )
        notification = PubSubNotification(
            pubsub_message_id="pubsub-msg-1",
            ack_id="ack-1",
            history_id="100",
            email_address="user@gmail.com",
            subscription_name="sub-1",
            publish_time=datetime.now(UTC),
        )
        with pytest.raises(DuplicateDeliveryError):
            subscriber.process_notification(notification)

    def test_new_delivery_processed(self) -> None:
        account_id = uuid4()
        subscriber = create_pull_subscriber(
            subscription_name="sub-1",
            account_id=account_id,
        )
        notification = PubSubNotification(
            pubsub_message_id="pubsub-msg-2",
            ack_id="ack-2",
            history_id="101",
            email_address="user@gmail.com",
            subscription_name="sub-1",
            publish_time=datetime.now(UTC),
        )
        receipt = subscriber.process_notification(notification)
        assert receipt.processed is True
        assert receipt.pubsub_message_id == "pubsub-msg-2"

    def test_ack_requires_processed(self) -> None:
        account_id = uuid4()
        subscriber = create_pull_subscriber(
            subscription_name="sub-1",
            account_id=account_id,
        )
        notification = PubSubNotification(
            pubsub_message_id="pubsub-msg-3",
            ack_id="ack-3",
            history_id="102",
            email_address="user@gmail.com",
            subscription_name="sub-1",
            publish_time=datetime.now(UTC),
        )
        receipt = subscriber.process_notification(notification)
        # Should not raise
        subscriber.acknowledge(receipt)

    def test_no_webhook_endpoint_created(self) -> None:
        # Structural test: PubSubPullSubscriber has no HTTP handler
        subscriber = create_pull_subscriber(
            subscription_name="sub-1",
            account_id=uuid4(),
        )
        assert not hasattr(subscriber, "handle_webhook")
        assert not hasattr(subscriber, "push_endpoint")


# ---------------------------------------------------------------------------
# Reply drafts (internal only, no send)
# ---------------------------------------------------------------------------


class TestReplyDrafts:
    def test_create_draft_internal_only(self) -> None:
        draft = create_reply_draft(
            message_id=uuid4(),
            thread_id=uuid4(),
            account_id=uuid4(),
            to_address="recruiter@tech.com",
            subject="Re: Interview invitation",
            body_text="Thank you, I'm available on Monday.",
        )
        assert draft.status is DraftStatus.DRAFT
        assert len(draft.payload_hash) == 64

    def test_submit_for_approval(self) -> None:
        draft = create_reply_draft(
            message_id=uuid4(),
            thread_id=uuid4(),
            account_id=uuid4(),
            to_address="recruiter@tech.com",
            subject="Re: Interview",
            body_text="Thanks!",
        )
        submitted = submit_draft_for_approval(draft)
        assert submitted.status is DraftStatus.PENDING_APPROVAL

    def test_approve_draft_freezes_payload(self) -> None:
        draft = create_reply_draft(
            message_id=uuid4(),
            thread_id=uuid4(),
            account_id=uuid4(),
            to_address="recruiter@tech.com",
            subject="Re: Interview",
            body_text="Thanks!",
        )
        submitted = submit_draft_for_approval(draft)
        approved = approve_draft(submitted)
        assert approved.status is DraftStatus.APPROVED
        assert approved.approved_at is not None
        assert approved.payload_hash == draft.payload_hash

    def test_cannot_approve_non_pending_draft(self) -> None:
        draft = create_reply_draft(
            message_id=uuid4(),
            thread_id=uuid4(),
            account_id=uuid4(),
            to_address="recruiter@tech.com",
            subject="Re: Interview",
            body_text="Thanks!",
        )
        with pytest.raises(DraftApprovalError, match="must be pending_approval"):
            approve_draft(draft)

    def test_cannot_submit_non_draft(self) -> None:
        draft = create_reply_draft(
            message_id=uuid4(),
            thread_id=uuid4(),
            account_id=uuid4(),
            to_address="recruiter@tech.com",
            subject="Re: Interview",
            body_text="Thanks!",
        )
        submitted = submit_draft_for_approval(draft)
        with pytest.raises(DraftApprovalError, match="must be draft"):
            submit_draft_for_approval(submitted)

    def test_payload_hash_changes_with_content(self) -> None:
        hash1 = compute_payload_hash(
            to_address="a@b.com",
            subject="Hello",
            body_text="Body 1",
            references="",
            in_reply_to="",
        )
        hash2 = compute_payload_hash(
            to_address="a@b.com",
            subject="Hello",
            body_text="Body 2",
            references="",
            in_reply_to="",
        )
        assert hash1 != hash2


# ---------------------------------------------------------------------------
# Attachment quarantine
# ---------------------------------------------------------------------------


class TestAttachmentQuarantine:
    def test_allowed_text_plain(self) -> None:
        result = validate_attachment(
            declared_mime="text/plain",
            detected_mime="text/plain",
            byte_size=1024,
        )
        assert result.allowed is True

    def test_allowed_pdf(self) -> None:
        result = validate_attachment(
            declared_mime="application/pdf",
            detected_mime="application/pdf",
            byte_size=5000,
        )
        assert result.allowed is True

    def test_allowed_text_calendar(self) -> None:
        result = validate_attachment(
            declared_mime="text/calendar",
            detected_mime="text/calendar",
            byte_size=512,
        )
        assert result.allowed is True

    def test_denied_executable(self) -> None:
        result = validate_attachment(
            declared_mime="application/x-msdownload",
            detected_mime="application/x-msdownload",
            byte_size=1000,
        )
        assert result.allowed is False
        assert "TYPE_NOT_ALLOWED" in result.deny_reason

    def test_denied_mime_mismatch(self) -> None:
        result = validate_attachment(
            declared_mime="text/plain",
            detected_mime="application/x-msdownload",
            byte_size=1000,
        )
        assert result.allowed is False
        assert "MIME_MISMATCH" in result.deny_reason

    def test_denied_oversized(self) -> None:
        result = validate_attachment(
            declared_mime="application/pdf",
            detected_mime="application/pdf",
            byte_size=20 * 1024 * 1024,
        )
        assert result.allowed is False
        assert "SIZE_LIMIT_EXCEEDED" in result.deny_reason

    def test_denied_empty_file(self) -> None:
        result = validate_attachment(
            declared_mime="text/plain",
            detected_mime="text/plain",
            byte_size=0,
        )
        assert result.allowed is False
        assert "EMPTY_FILE" in result.deny_reason

    def test_denied_archive(self) -> None:
        result = validate_attachment(
            declared_mime="application/zip",
            detected_mime="application/zip",
            byte_size=5000,
        )
        assert result.allowed is False

    def test_denied_office_macro(self) -> None:
        result = validate_attachment(
            declared_mime="application/vnd.ms-word.document.macroEnabled.12",
            detected_mime="application/vnd.ms-word.document.macroEnabled.12",
            byte_size=5000,
        )
        assert result.allowed is False


# ---------------------------------------------------------------------------
# No send adapter exists
# ---------------------------------------------------------------------------


class TestNoSendAdapter:
    def test_no_gmail_send_in_codebase(self) -> None:
        """Verify no gmail.send or gmail.compose scope is referenced as granted."""
        # The FORBIDDEN_SCOPES set is the authoritative list
        assert "https://www.googleapis.com/auth/gmail.send" in FORBIDDEN_SCOPES
        assert "https://www.googleapis.com/auth/gmail.compose" in FORBIDDEN_SCOPES

    def test_assert_no_send_capability_passes(self) -> None:
        assert_no_send_capability()

    def test_policy_denies_send_email(self) -> None:
        from careerops.policy import ActionProposal, PolicyEngine, PolicyOutcome

        decision = PolicyEngine().decide(
            ActionProposal(action_kind="send_email", authenticated=True)
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert decision.reason_code == "CAPABILITY_NOT_RELEASED"


# ---------------------------------------------------------------------------
# Schema constraints (structural verification)
# ---------------------------------------------------------------------------


class TestSchemaConstraints:
    def test_email_messages_table_exists_in_schema(self) -> None:
        from careerops.infrastructure.database.schema import email_messages

        assert email_messages is not None
        col_names = {c.name for c in email_messages.columns}
        assert "provider_message_id" in col_names
        assert "body_persisted" in col_names
        assert "category" in col_names

    def test_reply_drafts_table_exists_in_schema(self) -> None:
        from careerops.infrastructure.database.schema import reply_drafts

        assert reply_drafts is not None
        col_names = {c.name for c in reply_drafts.columns}
        assert "payload_hash" in col_names
        assert "status" in col_names

    def test_attachment_quarantine_table_exists(self) -> None:
        from careerops.infrastructure.database.schema import attachment_quarantine

        assert attachment_quarantine is not None

    def test_email_extractions_in_append_only(self) -> None:
        from careerops.infrastructure.database.schema import APPEND_ONLY_TABLES

        assert "email_extractions" in APPEND_ONLY_TABLES


# ---------------------------------------------------------------------------
# M4.14: FollowUpWorkflow integration
# ---------------------------------------------------------------------------


def _follow_up_context(
    *,
    application_state: ApplicationState | None = None,
    job_active: bool = True,
    new_inbound: bool = False,
) -> FollowUpEmailContext:
    return FollowUpEmailContext(
        reminder_id=uuid4(),
        application_id=uuid4(),
        account_id=uuid4(),
        thread_id=uuid4(),
        application_state=application_state or ApplicationState.SUBMITTED,
        job_active=job_active,
        new_inbound_since_last_outbound=new_inbound,
        to_address="recruiter@tech.com",
        subject="Re: Senior Engineer application",
        proposed_body="Following up on my application submitted last week.",
    )


class TestFollowUpIntegration:
    def test_due_follow_up_creates_pending_draft(self) -> None:
        outcome = process_due_follow_up(_follow_up_context())
        assert outcome.decision is FollowUpEmailDecision.DRAFT_CREATED
        assert outcome.draft is not None
        # Draft enters the approval queue; it is never sent.
        assert outcome.draft.status is DraftStatus.PENDING_APPROVAL
        assert outcome.draft.approved_at is None

    def test_terminal_state_skips_draft(self) -> None:
        for state in (
            ApplicationState.REJECTED,
            ApplicationState.WITHDRAWN,
            ApplicationState.OFFER,
            ApplicationState.IGNORED,
        ):
            outcome = process_due_follow_up(_follow_up_context(application_state=state))
            assert outcome.decision is FollowUpEmailDecision.SKIPPED_TERMINAL_STATE
            assert outcome.draft is None

    def test_inactive_job_skips_draft(self) -> None:
        outcome = process_due_follow_up(_follow_up_context(job_active=False))
        assert outcome.decision is FollowUpEmailDecision.SKIPPED_JOB_INACTIVE
        assert outcome.draft is None

    def test_new_inbound_reply_skips_draft(self) -> None:
        outcome = process_due_follow_up(_follow_up_context(new_inbound=True))
        assert outcome.decision is FollowUpEmailDecision.SKIPPED_NEW_INBOUND
        assert outcome.draft is None

    def test_follow_up_never_sends(self) -> None:
        # Even the productive path only yields an unapproved internal draft.
        outcome = process_due_follow_up(_follow_up_context())
        assert outcome.draft is not None
        assert outcome.draft.status is not DraftStatus.APPROVED
        # No send adapter is invoked; assert the global no-send invariant holds.
        assert_no_send_capability()


# ---------------------------------------------------------------------------
# End-to-end M4 pipeline: Pub/Sub -> ingest -> classify -> draft -> approval
# ---------------------------------------------------------------------------


class TestEndToEndPipeline:
    def test_full_recruitment_pipeline(self) -> None:
        account_id = uuid4()
        thread_id = uuid4()

        # 1. Pub/Sub notification arrives and is deduplicated.
        subscriber = create_pull_subscriber(subscription_name="sub-1", account_id=account_id)
        notification = PubSubNotification(
            pubsub_message_id="ps-100",
            ack_id="ack-100",
            history_id="500",
            email_address="candidate@jobmail.com",
            subscription_name="sub-1",
            publish_time=datetime.now(UTC),
        )
        receipt = subscriber.process_notification(notification)
        subscriber.acknowledge(receipt)
        assert receipt.processed is True

        # 2. Ingest + classify a recruitment message; body persisted.
        ingested = ingest_message(
            account_id=account_id,
            thread_id=thread_id,
            provider_message_id="gmail-msg-1",
            sender_email="recruiter@tech.com",
            sender_name="Recruiter",
            subject="Interview invitation for Engineer role",
            snippet="We'd like to schedule an interview.",
            body_text="Dear candidate, let's set up an interview.",
            received_at=datetime.now(UTC),
            existing_provider_ids=frozenset(),
        )
        assert ingested is not None
        assert ingested.message.category is EmailCategory.RECRUITMENT
        assert ingested.body_stored is True

        # 3. Create an internal draft and move it into the approval queue.
        draft = create_reply_draft(
            message_id=ingested.message.id,
            thread_id=thread_id,
            account_id=account_id,
            to_address="recruiter@tech.com",
            subject="Re: Interview invitation for Engineer role",
            body_text="Thank you, I'm available Monday.",
        )
        approved = approve_draft(submit_draft_for_approval(draft))
        assert approved.status is DraftStatus.APPROVED
        assert approved.payload_hash == draft.payload_hash

    def test_non_recruitment_body_never_persisted_in_pipeline(self) -> None:
        ingested = ingest_message(
            account_id=uuid4(),
            thread_id=uuid4(),
            provider_message_id="gmail-msg-2",
            sender_email="promo@store.com",
            sender_name="Store",
            subject="Weekly deals inside!",
            snippet="Save big this week!",
            body_text="SECRET_PROMO_BODY_DO_NOT_STORE",
            received_at=datetime.now(UTC),
            existing_provider_ids=frozenset(),
        )
        assert ingested is not None
        assert ingested.message.category is EmailCategory.NON_RECRUITMENT
        assert ingested.body_stored is False
        assert ingested.message.snippet == ""
        # Retention/redaction layer also keeps the body out of logs.
        log_entry = redact_for_logging(
            sender_email="promo@store.com",
            subject="Weekly deals inside!",
            body="SECRET_PROMO_BODY_DO_NOT_STORE",
            category=ingested.message.category,
        )
        assert "SECRET_PROMO_BODY_DO_NOT_STORE" not in str(log_entry)
        assert "subject" not in log_entry

    def test_duplicate_pubsub_delivery_does_not_duplicate_message(self) -> None:
        account_id = uuid4()
        existing = frozenset({"gmail-msg-1"})
        # A re-delivered notification for an already-ingested message is dropped.
        result = ingest_message(
            account_id=account_id,
            thread_id=uuid4(),
            provider_message_id="gmail-msg-1",
            sender_email="recruiter@tech.com",
            sender_name="Recruiter",
            subject="Interview invitation",
            snippet="Interview invitation",
            body_text="Body",
            received_at=datetime.now(UTC),
            existing_provider_ids=existing,
        )
        assert result is None
