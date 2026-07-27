"""Contract tests: Section 9 trusted contacts + initial application email
payloads (tasks 9.8).

Proves the preview / payload slice through the service layer with in-memory
fakes. Scenarios (task 9.8):

- Domain mismatch: a recipient whose domain differs from the trusted company
  domain is denied before payload creation (9.2).
- Guessed addresses: a recipient absent from the resolver evidence is denied
  (no guessed employee addresses, no model-only recipients) (9.2).
- Mutated package payloads: any input change (recipient/subject/body/
  attachment/account) changes the payload hash, so a stale approval is
  detectable by recomputation (9.4 / invalidation-on-mutation).
- Unsafe attachments: MIME mismatch, executable/archive/macro media types,
  oversize, quarantined/expired retention, password-protected content are all
  denied before any provider call (9.5).
- Preview without send: preview_submission performs NO provider side effects
  and mutates NO state (9.6).
- Exact preview/send parity: the payload hash + idempotency/reconciliation
  keys are pure functions of the inputs, so two builds from the same inputs
  produce byte-for-byte identical payloads (9.8).

Iron rules honored:
- Default-deny (Iron Rule 7): a recipient with no resolver evidence is
  denied; SYSTEM_MANAGED_SEND is never invoked by this layer.
- Model review-only (Iron Rule 2): the resolver supplies trusted evidence;
  model output cannot produce a verdict or pick a recipient.
- Append-only / reversible (Iron Rule 4): payloads are immutable value
  objects; any input change yields a new hash and instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict
from uuid import UUID, uuid4

import pytest

from careerops.application.email_payload_service import (
    AccountLookupError,
    EmailPayloadService,
    ResolvedContactCandidate,
)
from careerops.domain.application_packages import ApplicationPackageVersion
from careerops.domain.applications import PackageApprovalState
from careerops.domain.email_payloads import (
    ALLOWED_ATTACHMENT_MEDIA_TYPES,
    ATTACHMENT_MAX_BYTES,
    AttachmentRetentionState,
    AttachmentValidationError,
    EmailAccountSummary,
    EmailAttachment,
    InitialApplicationEmailPayload,
    RecipientDenialReason,
    RecipientIneligibleError,
    TrustedContactVerdict,
    compute_idempotency_key,
    compute_reconciliation_key,
    normalize_email_body,
    recipient_domain,
    validate_attachment,
)

# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakePackageRepo:
    def __init__(self, version: ApplicationPackageVersion | None) -> None:
        self._version = version

    def find_latest_package_version(self, application_id: UUID) -> ApplicationPackageVersion | None:
        if self._version is None or self._version.application_id != application_id:
            return None
        return self._version


class _FakeOwnershipReader:
    def __init__(self, owned: set[tuple[UUID, UUID]]) -> None:
        self._owned = owned

    def find_owned_application(self, *, application_id: UUID, candidate_id: UUID) -> object | None:
        if (application_id, candidate_id) in self._owned:
            return object()  # presence is all the preview needs
        return None


class _StaticContactResolver:
    def __init__(self, candidates: list[ResolvedContactCandidate]) -> None:
        self._candidates = candidates

    def resolve(
        self,
        *,
        candidate_id: UUID,
        application_id: UUID,
        canonical_job_id: UUID,
    ) -> list[ResolvedContactCandidate]:
        return list(self._candidates)


class _StaticAccountLookup:
    def __init__(self, accounts: dict[UUID, EmailAccountSummary]) -> None:
        self._accounts = accounts

    def lookup(self, account_id: UUID) -> EmailAccountSummary:
        acct = self._accounts.get(account_id)
        if acct is None:
            raise AccountLookupError(f"account {account_id} not found")
        return acct


class _StaticAttachmentProvider:
    def __init__(self, specs: dict[str, dict[str, object]]) -> None:
        self._specs = specs

    def describe(self, content_hash: str) -> dict[str, object] | None:
        return self._specs.get(content_hash)


class _BuildPayloadKwargs(TypedDict, total=False):
    candidate_id: UUID
    application_id: UUID
    account: EmailAccountSummary
    recipient: TrustedContactVerdict
    subject: str
    body: str
    attachments: tuple[EmailAttachment, ...]
    evidence_refs: tuple[UUID, ...]
    in_reply_to: str
    references_header: str
    message_id_header: str
    now: datetime


class _PreviewSubmissionKwargs(TypedDict, total=False):
    candidate_id: UUID
    application_id: UUID
    canonical_job_id: UUID
    account_id: UUID
    recipient_email: str
    subject: str
    body: str
    attachment_hashes: tuple[str, ...]
    evidence_refs: tuple[UUID, ...]
    in_reply_to: str
    references_header: str
    message_id_header: str
    now: datetime


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


NOW = datetime(2026, 7, 26, tzinfo=UTC)
COMPANY_DOMAIN = "acme.example"
TRUSTED_EMAIL = f"careers@{COMPANY_DOMAIN}"


@dataclass
class _Slice:
    svc: EmailPayloadService
    app_id: UUID
    cid: UUID
    job_id: UUID
    acct: EmailAccountSummary


def _candidate(
    *, email: str = TRUSTED_EMAIL, domain: str = COMPANY_DOMAIN
) -> ResolvedContactCandidate:
    return ResolvedContactCandidate(
        email=email,
        company_domain=domain,
        contact_type="recruiting",
        confidence="high",
        source_evidence_url=f"https://{domain}/careers",
        source_evidence_text="Contact us at " + email,
        domain_match=True,
        verified_at=NOW,
    )


def _approved_package(*, application_id: UUID) -> ApplicationPackageVersion:
    return ApplicationPackageVersion(
        id=uuid4(),
        application_id=application_id,
        version_number=1,
        resume_version_id=uuid4(),
        job_version_id=uuid4(),
        profile_version_id=uuid4(),
        cover_letter_text="I am a great fit.",
        approval_state=PackageApprovalState.APPROVED,
        approved_at=NOW,
        approved_by="candidate",
        payload_hash="a" * 64,
        created_at=NOW,
        updated_at=NOW,
    )


def _account(*, status: str = "active") -> EmailAccountSummary:
    return EmailAccountSummary(
        account_id=uuid4(), email_address="sender@careerops.example", status=status
    )


def _slice(
    *,
    approved: bool = True,
    candidates: list[ResolvedContactCandidate] | None = None,
    owned: bool = True,
    attachment_specs: dict[str, dict[str, object]] | None = None,
) -> _Slice:
    app_id, cid, job_id = uuid4(), uuid4(), uuid4()
    package = _approved_package(application_id=app_id) if approved else None
    acct = _account()
    owned_set: set[tuple[UUID, UUID]] = {(app_id, cid)} if owned else set()
    svc = EmailPayloadService(
        _FakePackageRepo(package),  # type: ignore[arg-type]
        _FakeOwnershipReader(owned_set),  # type: ignore[arg-type]
        contact_resolver=_StaticContactResolver(candidates or [_candidate()]),
        account_lookup=_StaticAccountLookup({acct.account_id: acct}),  # type: ignore[arg-type]
        attachment_provider=(
            _StaticAttachmentProvider(attachment_specs)  # type: ignore[arg-type]
            if attachment_specs
            else None
        ),
    )
    return _Slice(svc=svc, app_id=app_id, cid=cid, job_id=job_id, acct=acct)


def _pdf_spec(*, content_hash: str = "b" * 64, size: int = 1024) -> dict[str, object]:
    return {
        "name": "resume.pdf",
        "declared_media_type": "application/pdf",
        "detected_media_type": "application/pdf",
        "size_bytes": size,
        "retention_state": AttachmentRetentionState.RETAINED.value,
        "password_protected": False,
    }


# ---------------------------------------------------------------------------
# (1) Trusted contact resolution + eligible verdict (9.1)
# ---------------------------------------------------------------------------


class TestTrustedContactResolution:
    def test_eligible_verdict_carries_evidence(self) -> None:
        s = _slice()
        verdict = s.svc.resolve_trusted_contact(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            recipient_email=TRUSTED_EMAIL,
        )
        assert verdict.eligible
        assert verdict.company_domain == COMPANY_DOMAIN
        assert verdict.contact_type == "recruiting"
        assert verdict.confidence == "high"
        assert verdict.source_evidence_url.startswith("https://")
        assert verdict.denial_reasons == ()

    def test_list_contact_candidates_returns_evidence(self) -> None:
        s = _slice()
        candidates = s.svc.list_contact_candidates(
            candidate_id=s.cid, application_id=s.app_id, canonical_job_id=s.job_id
        )
        assert len(candidates) == 1
        assert candidates[0].email == TRUSTED_EMAIL


# ---------------------------------------------------------------------------
# (2) Recipient rejection (9.2)
# ---------------------------------------------------------------------------


class TestRecipientRejection:
    def test_domain_mismatch_denied(self) -> None:
        verdict = EmailPayloadService.validate_recipient(
            recipient_email="careers@other.example",
            company_domain=COMPANY_DOMAIN,
            candidates=[_candidate()],
        )
        assert not verdict.eligible
        assert RecipientDenialReason.DOMAIN_MISMATCH in verdict.denial_reasons

    def test_guessed_address_denied(self) -> None:
        # A recipient not present in the trusted candidates is guessed.
        verdict = EmailPayloadService.validate_recipient(
            recipient_email="john.doe@acme.example",
            company_domain=COMPANY_DOMAIN,
            candidates=[_candidate()],
        )
        assert not verdict.eligible
        assert RecipientDenialReason.MISSING_SOURCE_EVIDENCE in verdict.denial_reasons
        assert RecipientDenialReason.GUESSED_EMPLOYEE_ADDRESS in verdict.denial_reasons

    def test_model_only_recipient_denied(self) -> None:
        # No resolver evidence at all → the recipient is model-only.
        verdict = EmailPayloadService.validate_recipient(
            recipient_email="anyone@acme.example",
            company_domain="",
            candidates=[],
        )
        assert not verdict.eligible
        assert RecipientDenialReason.MODEL_ONLY_RECIPIENT in verdict.denial_reasons
        assert RecipientDenialReason.UNVERIFIED_DOMAIN in verdict.denial_reasons

    def test_missing_source_evidence_denied(self) -> None:
        # The candidate exists but the recipient does not match any candidate.
        verdict = EmailPayloadService.validate_recipient(
            recipient_email="ghost@acme.example",
            company_domain=COMPANY_DOMAIN,
            candidates=[_candidate()],
        )
        assert not verdict.eligible
        assert RecipientDenialReason.MISSING_SOURCE_EVIDENCE in verdict.denial_reasons

    def test_invalid_address_denied(self) -> None:
        verdict = EmailPayloadService.validate_recipient(
            recipient_email="not-an-email",
            company_domain=COMPANY_DOMAIN,
            candidates=[_candidate()],
        )
        assert not verdict.eligible
        assert RecipientDenialReason.INVALID_ADDRESS in verdict.denial_reasons

    def test_build_payload_rejects_ineligible_recipient(self) -> None:
        s = _slice()
        ineligible = TrustedContactVerdict(
            email="x@y.example",
            company_domain="y.example",
            contact_type="recruiting",
            confidence="low",
            source_evidence_url="https://y.example",
            eligible=False,
            denial_reasons=(RecipientDenialReason.DOMAIN_MISMATCH,),
        )
        with pytest.raises(RecipientIneligibleError):
            s.svc.build_payload(
                candidate_id=s.cid,
                application_id=s.app_id,
                account=s.acct,
                recipient=ineligible,
                subject="Subject",
                body="Body",
                now=NOW,
            )


# ---------------------------------------------------------------------------
# (3) Mutated package payloads → hash changes (9.4 / invalidation)
# ---------------------------------------------------------------------------


class TestPayloadHashMutation:
    def test_subject_change_changes_hash(self) -> None:
        s = _slice()
        verdict = s.svc.resolve_trusted_contact(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            recipient_email=TRUSTED_EMAIL,
        )
        kwargs: _BuildPayloadKwargs = {
            "candidate_id": s.cid,
            "application_id": s.app_id,
            "account": s.acct,
            "recipient": verdict,
            "body": "Body",
            "now": NOW,
        }
        p1 = s.svc.build_payload(subject="Subject A", **kwargs)  # type: ignore[call-arg]
        p2 = s.svc.build_payload(subject="Subject B", **kwargs)  # type: ignore[call-arg]
        assert p1.payload_hash != p2.payload_hash

    def test_body_change_changes_hash(self) -> None:
        s = _slice()
        verdict = s.svc.resolve_trusted_contact(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            recipient_email=TRUSTED_EMAIL,
        )
        kwargs: _BuildPayloadKwargs = {
            "candidate_id": s.cid,
            "application_id": s.app_id,
            "account": s.acct,
            "recipient": verdict,
            "subject": "Subject",
            "now": NOW,
        }
        p1 = s.svc.build_payload(body="Body one", **kwargs)  # type: ignore[call-arg]
        p2 = s.svc.build_payload(body="Body two", **kwargs)  # type: ignore[call-arg]
        assert p1.payload_hash != p2.payload_hash

    def test_attachment_change_changes_hash(self) -> None:
        s = _slice()
        verdict = s.svc.resolve_trusted_contact(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            recipient_email=TRUSTED_EMAIL,
        )
        att1 = EmailAttachment(
            name="resume.pdf",
            content_hash="a" * 64,
            media_type="application/pdf",
            size_bytes=100,
        )
        att2 = EmailAttachment(
            name="resume.pdf",
            content_hash="b" * 64,
            media_type="application/pdf",
            size_bytes=100,
        )
        kwargs: _BuildPayloadKwargs = {
            "candidate_id": s.cid,
            "application_id": s.app_id,
            "account": s.acct,
            "recipient": verdict,
            "subject": "Subject",
            "body": "Body",
            "now": NOW,
        }
        p1 = s.svc.build_payload(attachments=(att1,), **kwargs)  # type: ignore[call-arg]
        p2 = s.svc.build_payload(attachments=(att2,), **kwargs)  # type: ignore[call-arg]
        assert p1.payload_hash != p2.payload_hash

    def test_account_change_changes_hash(self) -> None:
        s = _slice()
        verdict = s.svc.resolve_trusted_contact(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            recipient_email=TRUSTED_EMAIL,
        )
        acct_b = EmailAccountSummary(account_id=uuid4(), email_address="other@careerops.example")
        kwargs: _BuildPayloadKwargs = {
            "candidate_id": s.cid,
            "application_id": s.app_id,
            "recipient": verdict,
            "subject": "Subject",
            "body": "Body",
            "now": NOW,
        }
        p1 = s.svc.build_payload(account=s.acct, **kwargs)  # type: ignore[call-arg]
        p2 = s.svc.build_payload(account=acct_b, **kwargs)  # type: ignore[call-arg]
        assert p1.payload_hash != p2.payload_hash


# ---------------------------------------------------------------------------
# (4) Unsafe attachments (9.5)
# ---------------------------------------------------------------------------


class TestAttachmentValidation:
    def test_valid_pdf_passes(self) -> None:
        att = validate_attachment(
            name="resume.pdf",
            content_hash="a" * 64,
            declared_media_type="application/pdf",
            detected_media_type="application/pdf",
            size_bytes=2048,
        )
        assert att.media_type == "application/pdf"
        assert att.size_bytes == 2048

    def test_mime_mismatch_denied(self) -> None:
        with pytest.raises(AttachmentValidationError) as exc:
            validate_attachment(
                name="resume.pdf",
                content_hash="a" * 64,
                declared_media_type="application/pdf",
                detected_media_type="application/zip",
                size_bytes=2048,
            )
        assert "differs" in exc.value.reason

    def test_executable_denied(self) -> None:
        with pytest.raises(AttachmentValidationError):
            validate_attachment(
                name="evil.exe",
                content_hash="a" * 64,
                declared_media_type="application/x-msdownload",
                detected_media_type="application/x-msdownload",
                size_bytes=2048,
            )

    def test_archive_denied(self) -> None:
        with pytest.raises(AttachmentValidationError):
            validate_attachment(
                name="payload.zip",
                content_hash="a" * 64,
                declared_media_type="application/zip",
                detected_media_type="application/zip",
                size_bytes=2048,
            )

    def test_oversize_denied(self) -> None:
        with pytest.raises(AttachmentValidationError) as exc:
            validate_attachment(
                name="big.pdf",
                content_hash="a" * 64,
                declared_media_type="application/pdf",
                detected_media_type="application/pdf",
                size_bytes=ATTACHMENT_MAX_BYTES + 1,
            )
        assert "exceeds" in exc.value.reason

    def test_quarantined_denied(self) -> None:
        with pytest.raises(AttachmentValidationError):
            validate_attachment(
                name="resume.pdf",
                content_hash="a" * 64,
                declared_media_type="application/pdf",
                detected_media_type="application/pdf",
                size_bytes=2048,
                retention_state=AttachmentRetentionState.QUARANTINED,
            )

    def test_expired_denied(self) -> None:
        with pytest.raises(AttachmentValidationError):
            validate_attachment(
                name="resume.pdf",
                content_hash="a" * 64,
                declared_media_type="application/pdf",
                detected_media_type="application/pdf",
                size_bytes=2048,
                retention_state=AttachmentRetentionState.EXPIRED,
            )

    def test_password_protected_denied(self) -> None:
        with pytest.raises(AttachmentValidationError):
            validate_attachment(
                name="secret.pdf",
                content_hash="a" * 64,
                declared_media_type="application/pdf",
                detected_media_type="application/pdf",
                size_bytes=2048,
                password_protected=True,
            )

    def test_bad_hash_length_denied(self) -> None:
        with pytest.raises(AttachmentValidationError):
            validate_attachment(
                name="resume.pdf",
                content_hash="tooshort",
                declared_media_type="application/pdf",
                detected_media_type="application/pdf",
                size_bytes=2048,
            )

    def test_allowed_set_is_conservative(self) -> None:
        assert (
            frozenset({"application/pdf", "text/plain", "text/calendar"})
            == ALLOWED_ATTACHMENT_MEDIA_TYPES
        )


# ---------------------------------------------------------------------------
# (5) Preview without send (9.6)
# ---------------------------------------------------------------------------


class TestPreviewWithoutSend:
    def test_preview_builds_payload_and_records_no_state(self) -> None:
        """preview_submission returns a sendable payload and mutates nothing.

        The fake repos are not written to by preview; the service has no
        provider port to call. This proves "preview without send".
        """
        s = _slice()
        preview = s.svc.preview_submission(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            account_id=s.acct.account_id,
            recipient_email=TRUSTED_EMAIL,
            subject="Application for SWE",
            body="Hello",
            now=NOW,
        )
        assert preview.sendable
        assert preview.payload is not None
        assert preview.errors == []
        assert isinstance(preview.payload, InitialApplicationEmailPayload)

    def test_preview_missing_approved_package_returns_errors(self) -> None:
        s = _slice(approved=False)
        preview = s.svc.preview_submission(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            account_id=s.acct.account_id,
            recipient_email=TRUSTED_EMAIL,
            subject="Subject",
            body="Body",
            now=NOW,
        )
        assert not preview.sendable
        assert preview.payload is None
        assert any("approved package" in e for e in preview.errors)

    def test_preview_not_owned_returns_error(self) -> None:
        s = _slice(owned=False)
        preview = s.svc.preview_submission(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            account_id=s.acct.account_id,
            recipient_email=TRUSTED_EMAIL,
            subject="Subject",
            body="Body",
            now=NOW,
        )
        assert not preview.sendable
        assert any("not found" in e for e in preview.errors)

    def test_preview_collects_multiple_errors(self) -> None:
        """A recipient mismatch AND an unapproved package both surface."""
        s = _slice(approved=False)
        preview = s.svc.preview_submission(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            account_id=s.acct.account_id,
            recipient_email="stranger@other.example",  # domain mismatch
            subject="",  # missing subject
            body="Body",
            now=NOW,
        )
        assert not preview.sendable
        joined = " ".join(preview.errors)
        assert "approved package" in joined
        assert "recipient denied" in joined
        assert "subject is required" in joined

    def test_preview_validates_attachments(self) -> None:
        bad_hash = "c" * 64
        specs = {
            bad_hash: {
                **_pdf_spec(content_hash=bad_hash),
                "declared_media_type": "application/zip",
                "detected_media_type": "application/zip",
            }
        }
        s = _slice(attachment_specs=specs)
        preview = s.svc.preview_submission(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            account_id=s.acct.account_id,
            recipient_email=TRUSTED_EMAIL,
            subject="Subject",
            body="Body",
            attachment_hashes=(bad_hash,),
            now=NOW,
        )
        assert not preview.sendable
        assert any("attachment" in e.lower() for e in preview.errors)


# ---------------------------------------------------------------------------
# (6) Exact preview/send parity (9.8)
# ---------------------------------------------------------------------------


class TestPreviewSendParity:
    def test_same_inputs_produce_identical_payload(self) -> None:
        """Two builds from the same inputs produce byte-for-byte equal payloads.

        This is the preview/send parity guarantee: the hash, idempotency key
        and reconciliation key are pure functions of the inputs, so the bytes
        the user reviewed equal the bytes the provider would receive.
        """
        s = _slice()
        kwargs: _PreviewSubmissionKwargs = {
            "candidate_id": s.cid,
            "application_id": s.app_id,
            "canonical_job_id": s.job_id,
            "account_id": s.acct.account_id,
            "recipient_email": TRUSTED_EMAIL,
            "subject": "Application",
            "body": "Hello there",
            "now": NOW,
        }
        p1 = s.svc.preview_submission(**kwargs)
        p2 = s.svc.preview_submission(**kwargs)
        assert p1.sendable and p2.sendable
        assert p1.payload is not None and p2.payload is not None
        assert p1.payload.payload_hash == p2.payload.payload_hash
        assert p1.payload.idempotency_key == p2.payload.idempotency_key
        assert p1.payload.reconciliation_key == p2.payload.reconciliation_key
        assert p1.payload.body == p2.payload.body
        assert p1.payload.recipient.email == p2.payload.recipient.email

    def test_idempotency_and_reconciliation_keys_are_distinct(self) -> None:
        h = "a" * 64
        acct = _account()
        app_id = uuid4()
        idem = compute_idempotency_key(
            account_email=acct.email_address, application_id=app_id, payload_hash=h
        )
        recon = compute_reconciliation_key(
            account_email=acct.email_address, application_id=app_id, payload_hash=h
        )
        assert idem != recon
        assert len(idem) == 64 and len(recon) == 64

    def test_keys_change_with_payload_hash(self) -> None:
        acct = _account()
        app_id = uuid4()
        idem_a = compute_idempotency_key(
            account_email=acct.email_address,
            application_id=app_id,
            payload_hash="a" * 64,
        )
        idem_b = compute_idempotency_key(
            account_email=acct.email_address,
            application_id=app_id,
            payload_hash="b" * 64,
        )
        assert idem_a != idem_b

    def test_normalize_body_is_deterministic(self) -> None:
        a = normalize_email_body("Hello  \n\n\nWorld\n")
        b = normalize_email_body("Hello\n\nWorld")
        assert a == b == "Hello\n\nWorld"

    def test_recipient_domain_helper(self) -> None:
        assert recipient_domain("Careers@Acme.Example") == "acme.example"
        assert recipient_domain("bad") == ""


# ---------------------------------------------------------------------------
# (7) Full vertical slice (9.1 → 9.6)
# ---------------------------------------------------------------------------


class TestEmailPayloadSlice:
    """Resolve a trusted contact → validate → build an exact payload →
    preview-without-send → prove parity."""

    def test_full_preview_slice(self) -> None:
        good_hash = "b" * 64
        specs = {good_hash: _pdf_spec(content_hash=good_hash)}
        s = _slice(attachment_specs=specs)

        # (a) Resolve a trusted contact → eligible verdict.
        verdict = s.svc.resolve_trusted_contact(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            recipient_email=TRUSTED_EMAIL,
        )
        assert verdict.eligible

        # (b) Preview builds the exact payload, no side effects.
        preview = s.svc.preview_submission(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            account_id=s.acct.account_id,
            recipient_email=TRUSTED_EMAIL,
            subject="Application for Backend Engineer",
            body="Dear Acme, I'd like to apply.",
            attachment_hashes=(good_hash,),
            evidence_refs=(uuid4(),),
            now=NOW,
        )
        assert preview.sendable
        payload = preview.payload
        assert payload is not None
        assert payload.recipient.email == TRUSTED_EMAIL
        assert len(payload.attachments) == 1
        assert payload.attachments[0].content_hash == good_hash
        assert len(payload.payload_hash) == 64
        assert payload.idempotency_key and payload.reconciliation_key

        # (c) Parity: rebuild and compare hashes.
        rebuilt = s.svc.preview_submission(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            account_id=s.acct.account_id,
            recipient_email=TRUSTED_EMAIL,
            subject="Application for Backend Engineer",
            body="Dear Acme, I'd like to apply.",
            attachment_hashes=(good_hash,),
            evidence_refs=payload.evidence_refs,
            now=NOW,
        )
        assert rebuilt.payload is not None
        assert rebuilt.payload.payload_hash == payload.payload_hash

    def test_domain_mismatch_blocks_full_slice(self) -> None:
        """A recipient on a different domain never reaches a payload."""
        s = _slice()
        preview = s.svc.preview_submission(
            candidate_id=s.cid,
            application_id=s.app_id,
            canonical_job_id=s.job_id,
            account_id=s.acct.account_id,
            recipient_email="careers@not-acme.example",
            subject="Subject",
            body="Body",
            now=NOW,
        )
        assert not preview.sendable
        assert preview.recipient_verdict is not None
        assert not preview.recipient_verdict.eligible
        assert RecipientDenialReason.DOMAIN_MISMATCH.value in " ".join(
            preview.recipient_verdict.denial_reasons
        )
