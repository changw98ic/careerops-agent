"""Initial application email payload service (Section 9, tasks 9.1-9.6).

Composes the Section-8 approved package, a trusted recipient, a selected
sending account and permitted attachments into the *exact sendable
representation* the user reviews before final confirmation. This service
performs NO provider side effects and persists NO send intent — that is
Section 10. It is the preview / validation / hash-computation layer.

Layering:

- **9.1** :meth:`EmailPayloadService.resolve_trusted_contact` turns raw
  contact evidence (sourced from a :class:`TrustedContactResolver`) into an
  evidence-bound :class:`TrustedContactVerdict`, binding source evidence,
  company domain, contact type, confidence and retention metadata.
- **9.2** :meth:`EmailPayloadService.validate_recipient` rejects guessed
  employee addresses, unverified domains, missing source evidence and
  model-only recipients *before* any payload is created.
- **9.3 + 9.4** :meth:`EmailPayloadService.build_payload` assembles the
  immutable :class:`InitialApplicationEmailPayload` from the approved
  package + account + recipient + subject + body + attachments + thread
  headers, computing the normalized payload hash, attachment hashes, evidence
  references and the stable idempotency/reconciliation keys.
- **9.5** attachment MIME/size/hash/quarantine/retention validation is
  delegated to :func:`careerops.domain.email_payloads.validate_attachment`.
- **9.6** :meth:`EmailPayloadService.preview_submission` returns the exact
  sendable representation plus any validation errors, with NO provider side
  effects and NO state mutation.

Iron rules honored:
- Additive (Iron Rule 8): new service; no existing service mutated.
- Model review-only (Iron Rule 2): the resolver supplies trusted evidence;
  model output never selects the recipient or computes a verdict.
- Default-deny (Iron Rule 7): a recipient with no resolver evidence is
  denied; SYSTEM_MANAGED_SEND stays denied at the contract layer (Section 10
  gates the actual send; this service only previews).
- Server-side ownership (Iron Rule 2/6): every method takes the
  server-resolved ``candidate_id`` and re-checks application ownership.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from careerops.domain.application_packages import ApplicationPackageVersion
from careerops.domain.applications import PackageApprovalState
from careerops.domain.email_payloads import (
    AttachmentRetentionState,
    AttachmentValidationError,
    EmailAccountSummary,
    EmailAttachment,
    InitialApplicationEmailPayload,
    RecipientDenialReason,
    RecipientIneligibleError,
    TrustedContactVerdict,
    compute_idempotency_key,
    compute_payload_hash,
    compute_reconciliation_key,
    recipient_domain,
    validate_attachment,
)

__all__ = [
    "AccountLookupError",
    "ApplicationNotOwnedError",
    "ApprovedPackageRequiredError",
    "EmailPayloadService",
    "NoTrustedContactError",
    "PackageVersionRepository",
    "ResolvedContactCandidate",
    "SubmissionPreview",
    "TrustedContactResolver",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmailPayloadServiceError(Exception):
    """Base for email-payload service errors (translated at the API)."""


class ApplicationNotOwnedError(EmailPayloadServiceError):
    """The application does not exist or is not owned by the candidate."""

    def __init__(self, application_id: UUID) -> None:
        super().__init__(f"application not found for candidate: {application_id}")


class ApprovedPackageRequiredError(EmailPayloadServiceError):
    """No approved package version is currently bound to the application."""

    def __init__(self, application_id: UUID) -> None:
        super().__init__(f"application {application_id} has no currently-approved package version")


class AccountLookupError(EmailPayloadServiceError):
    """The selected sending account is missing, revoked or not active."""


class NoTrustedContactError(EmailPayloadServiceError):
    """No trusted contact evidence is available for the application/job."""


# ---------------------------------------------------------------------------
# Resolved contact candidate (raw evidence from the resolver)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedContactCandidate:
    """Raw, resolver-supplied contact evidence awaiting trust evaluation.

    The :class:`TrustedContactResolver` returns these; the service applies
    the deterministic trust checks (9.1/9.2) to produce a verdict. All fields
    are trusted (sourced from the contact repo / job evidence / thread
    metadata), never from model output.
    """

    email: str
    company_domain: str
    contact_type: str
    confidence: str
    source_evidence_url: str
    source_evidence_text: str = ""
    domain_match: bool = True
    verified_at: datetime | None = None
    retention_until: datetime | None = None
    application_linkage_id: UUID | None = None


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class TrustedContactResolver(Protocol):
    """Protocol: supply raw trusted contact evidence for an application.

    Implementations read from the contact repo + job/source evidence (and,
    for replies in later gates, linked-thread metadata). Model output MUST
    NOT influence the returned candidates. Returns ``[]`` when no contact
    evidence is available (the recipient is then denied for missing source
    evidence).
    """

    def resolve(
        self,
        *,
        candidate_id: UUID,
        application_id: UUID,
        canonical_job_id: UUID,
    ) -> list[ResolvedContactCandidate]: ...


class PackageVersionRepository(Protocol):
    """Minimal package-version read surface the payload service needs."""

    def find_latest_package_version(
        self, application_id: UUID
    ) -> ApplicationPackageVersion | None: ...


class ApplicationOwnershipReader(Protocol):
    """Minimal read surface to enforce candidate ownership of an application."""

    def find_owned_application(
        self, *, application_id: UUID, candidate_id: UUID
    ) -> object | None: ...


class AccountLookup(Protocol):
    """Protocol: resolve a sending account to a trusted summary (9.3).

    Returns an :class:`EmailAccountSummary` or raises :class:`AccountLookupError`.
    The lookup MUST NOT expose the credential reference; only the account id,
    email and status travel into the payload.
    """

    def lookup(self, account_id: UUID) -> EmailAccountSummary: ...


class AttachmentProvider(Protocol):
    """Protocol: fetch safe-material inputs for an attachment (9.5).

    Given a content_hash (the sha256 the package bound), returns the declared
    + detected media types, byte size, retention state and password-protection
    flag so :func:`validate_attachment` can decide. Returns ``None`` when the
    underlying content is unknown/expired — the attachment is then denied.
    """

    def describe(self, content_hash: str) -> dict[str, object] | None: ...


class _DefaultContactResolver:
    """Default resolver: returns no candidates (EMAIL stays denied).

    Until a real resolver is wired (contact repo + job/source evidence), the
    preview surfaces "no trusted contact" rather than guessing a recipient.
    """

    def resolve(
        self,
        *,
        candidate_id: UUID,
        application_id: UUID,
        canonical_job_id: UUID,
    ) -> list[ResolvedContactCandidate]:
        return []


# ---------------------------------------------------------------------------
# Submission preview (task 9.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubmissionPreview:
    """The exact sendable representation + validation errors (task 9.6).

    ``payload`` is ``None`` when any blocking validation error is present; the
    UI then renders ``errors`` so the user can resolve them. When ``payload``
    is present, it is byte-for-byte what the provider would receive after the
    user's final confirmation (preview/send parity, task 9.8). Producing a
    preview performs NO provider side effects and mutates NO state.
    """

    application_id: UUID
    payload: InitialApplicationEmailPayload | None
    errors: list[str] = field(default_factory=list)
    recipient_verdict: TrustedContactVerdict | None = None

    @property
    def sendable(self) -> bool:
        """True when the payload is buildable (no blocking errors)."""
        return self.payload is not None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class EmailPayloadService:
    """Resolve trusted contacts, validate recipients, and build/preview the
    initial application email payload (tasks 9.1-9.6)."""

    def __init__(
        self,
        package_repo: PackageVersionRepository,
        ownership_reader: ApplicationOwnershipReader,
        *,
        contact_resolver: TrustedContactResolver | None = None,
        account_lookup: AccountLookup | None = None,
        attachment_provider: AttachmentProvider | None = None,
    ) -> None:
        self._packages = package_repo
        self._ownership = ownership_reader
        self._contact_resolver = contact_resolver or _DefaultContactResolver()
        self._account_lookup = account_lookup
        self._attachment_provider = attachment_provider

    # ------------------------------------------------------------------
    # 9.1 + 9.2 — trusted contact resolution + recipient validation
    # ------------------------------------------------------------------

    def list_contact_candidates(
        self,
        *,
        candidate_id: UUID,
        application_id: UUID,
        canonical_job_id: UUID,
    ) -> list[ResolvedContactCandidate]:
        """Return the raw trusted contact evidence for the application.

        Public so the UI can show *why* a recipient is or is not available
        before the user picks one. Returns ``[]`` when no evidence exists.
        """
        return list(
            self._contact_resolver.resolve(
                candidate_id=candidate_id,
                application_id=application_id,
                canonical_job_id=canonical_job_id,
            )
        )

    def resolve_trusted_contact(
        self,
        *,
        candidate_id: UUID,
        application_id: UUID,
        canonical_job_id: UUID,
        recipient_email: str,
    ) -> TrustedContactVerdict:
        """Resolve a single recipient to an evidence-bound verdict (9.1).

        Applies the deterministic trust checks (9.2): the recipient must
        match a resolver-supplied candidate (no guessed addresses), the
        domain must match the trusted company domain, source evidence must be
        present, and the recipient must not be model-only. Returns the
        verdict (``eligible=False`` + reasons when denied).
        """
        candidates = self.list_contact_candidates(
            candidate_id=candidate_id,
            application_id=application_id,
            canonical_job_id=canonical_job_id,
        )
        return self.validate_recipient(
            recipient_email=recipient_email,
            company_domain=(candidates[0].company_domain if candidates else ""),
            candidates=candidates,
        )

    @staticmethod
    def validate_recipient(
        *,
        recipient_email: str,
        company_domain: str,
        candidates: list[ResolvedContactCandidate],
    ) -> TrustedContactVerdict:
        """Apply the deterministic recipient trust checks (task 9.2).

        Returns a :class:`TrustedContactVerdict` (eligible or not). Denial
        reasons are collected deterministically; the verdict is the single
        source of truth for "may this recipient enter a payload". A recipient
        not present in ``candidates`` is treated as guessed (no source
        evidence) — model-only recipients never appear in ``candidates``
        because the resolver is trusted.
        """
        reasons: list[RecipientDenialReason] = []
        normalized = recipient_email.strip().lower()
        target_domain = recipient_domain(normalized)

        # Find the matching candidate (exact, case-insensitive email match).
        match = next(
            (c for c in candidates if c.email.strip().lower() == normalized),
            None,
        )
        if match is None:
            # The recipient was not supplied by the trusted resolver — it is
            # either guessed or model-only. Both are denied (9.2).
            reasons.append(RecipientDenialReason.MISSING_SOURCE_EVIDENCE)
            if not candidates:
                reasons.append(RecipientDenialReason.MODEL_ONLY_RECIPIENT)
            else:
                reasons.append(RecipientDenialReason.GUESSED_EMPLOYEE_ADDRESS)

        if not target_domain:
            reasons.append(RecipientDenialReason.INVALID_ADDRESS)

        # Domain check: recipient domain must match the trusted company domain.
        trusted_domain = company_domain.strip().lower()
        if trusted_domain and target_domain and target_domain != trusted_domain:
            reasons.append(RecipientDenialReason.DOMAIN_MISMATCH)
        elif not trusted_domain:
            reasons.append(RecipientDenialReason.UNVERIFIED_DOMAIN)

        if not reasons and match is not None:
            return TrustedContactVerdict(
                email=match.email,
                company_domain=match.company_domain,
                contact_type=match.contact_type,
                confidence=match.confidence,
                source_evidence_url=match.source_evidence_url,
                source_evidence_text=match.source_evidence_text,
                domain_match=match.domain_match,
                verified_at=match.verified_at,
                retention_until=match.retention_until,
                eligible=True,
                denial_reasons=(),
                application_linkage_id=match.application_linkage_id,
            )
        return TrustedContactVerdict(
            email=normalized or recipient_email,
            company_domain=trusted_domain,
            contact_type=match.contact_type if match else "unknown",
            confidence=match.confidence if match else "none",
            source_evidence_url=match.source_evidence_url if match else "",
            source_evidence_text=match.source_evidence_text if match else "",
            domain_match=False,
            verified_at=match.verified_at if match else None,
            retention_until=match.retention_until if match else None,
            eligible=False,
            denial_reasons=tuple(reasons),
            application_linkage_id=match.application_linkage_id if match else None,
        )

    # ------------------------------------------------------------------
    # 9.3 + 9.4 — build the immutable payload
    # ------------------------------------------------------------------

    def build_payload(
        self,
        *,
        candidate_id: UUID,
        application_id: UUID,
        account: EmailAccountSummary,
        recipient: TrustedContactVerdict,
        subject: str,
        body: str,
        attachments: tuple[EmailAttachment, ...] = (),
        evidence_refs: tuple[UUID, ...] = (),
        in_reply_to: str = "",
        references_header: str = "",
        message_id_header: str = "",
        now: datetime,
    ) -> InitialApplicationEmailPayload:
        """Assemble the exact sendable payload from approved inputs (9.3/9.4).

        The caller supplies the approved package's content (subject/body/
        attachments) and the validated account + recipient verdict. This
        method computes the normalized payload hash, attachment hashes,
        evidence references and the stable idempotency/reconciliation keys,
        and returns the immutable :class:`InitialApplicationEmailPayload`.

        Raises :class:`RecipientIneligibleError` if the recipient verdict is
        not eligible (defense-in-depth — the preview path collects errors
        instead, but a direct build call must fail closed).
        """
        if not recipient.eligible:
            raise RecipientIneligibleError(recipient.email, list(recipient.denial_reasons))
        if not account.is_active:
            raise AccountLookupError(f"account {account.account_id} is not active")
        package = self._require_approved_package(
            candidate_id=candidate_id, application_id=application_id
        )
        payload_hash = compute_payload_hash(
            application_id=application_id,
            account=account,
            recipient_email=recipient.email,
            subject=subject,
            body=body,
            attachments=attachments,
            in_reply_to=in_reply_to,
            references_header=references_header,
            evidence_refs=evidence_refs,
        )
        idempotency_key = compute_idempotency_key(
            account_email=account.email_address,
            application_id=application_id,
            payload_hash=payload_hash,
        )
        reconciliation_key = compute_reconciliation_key(
            account_email=account.email_address,
            application_id=application_id,
            payload_hash=payload_hash,
        )
        return InitialApplicationEmailPayload(
            application_id=application_id,
            package_version_id=package.id,
            account=account,
            recipient=recipient,
            subject=subject,
            body=body,
            attachments=attachments,
            evidence_refs=evidence_refs,
            in_reply_to=in_reply_to,
            references_header=references_header,
            message_id_header=message_id_header,
            payload_hash=payload_hash,
            idempotency_key=idempotency_key,
            reconciliation_key=reconciliation_key,
            created_at=now,
        )

    # ------------------------------------------------------------------
    # 9.5 — attachment validation helper
    # ------------------------------------------------------------------

    def validate_attachment(self, *, content_hash: str) -> EmailAttachment:
        """Validate one attachment against the safe-material policy (9.5).

        Looks up the attachment's declared/detected media type, size, retention
        state and password-protection flag via the wired
        :class:`AttachmentProvider`, then delegates to
        :func:`careerops.domain.email_payloads.validate_attachment`.
        """
        provider = self._attachment_provider
        if provider is None:
            raise AttachmentValidationError("<unknown>", "attachment provider not wired")
        spec = provider.describe(content_hash)
        if spec is None:
            raise AttachmentValidationError(
                "<unknown>", f"no content found for hash {content_hash}"
            )
        return validate_attachment(
            name=str(spec.get("name", "")),
            content_hash=content_hash,
            declared_media_type=str(spec.get("declared_media_type", "")),
            detected_media_type=str(spec.get("detected_media_type", "")),
            size_bytes=int(spec.get("size_bytes", 0)),
            retention_state=AttachmentRetentionState(
                spec.get("retention_state", AttachmentRetentionState.RETAINED.value)
            ),
            password_protected=bool(spec.get("password_protected", False)),
        )

    # ------------------------------------------------------------------
    # 9.6 — submission preview (NO provider side effects)
    # ------------------------------------------------------------------

    def preview_submission(
        self,
        *,
        candidate_id: UUID,
        application_id: UUID,
        canonical_job_id: UUID,
        account_id: UUID,
        recipient_email: str,
        subject: str,
        body: str,
        attachment_hashes: tuple[str, ...] = (),
        evidence_refs: tuple[UUID, ...] = (),
        in_reply_to: str = "",
        references_header: str = "",
        message_id_header: str = "",
        now: datetime,
    ) -> SubmissionPreview:
        """Return the exact sendable representation + validation errors (9.6).

        Performs NO provider side effects and mutates NO state. Validation is
        collected (not raised): blocking problems are returned in ``errors``
        with ``payload=None`` so the UI can show them. When ``sendable`` is
        True, ``payload`` is byte-for-byte what the provider would receive
        after the user's final confirmation (preview/send parity, task 9.8).

        Section 10 performs the actual send after re-validating and recording
        a durable intent; this method only computes the preview.
        """
        errors: list[str] = []

        # 1. ownership
        owned = self._ownership.find_owned_application(
            application_id=application_id, candidate_id=candidate_id
        )
        if owned is None:
            return SubmissionPreview(
                application_id=application_id,
                payload=None,
                errors=["application not found for candidate"],
            )

        # 2. approved package
        try:
            package = self._require_approved_package(
                candidate_id=candidate_id, application_id=application_id
            )
        except ApprovedPackageRequiredError as exc:
            errors.append(str(exc))
            package = None

        # 3. account
        account: EmailAccountSummary | None = None
        if self._account_lookup is None:
            errors.append("account lookup not wired")
        else:
            try:
                account = self._account_lookup.lookup(account_id)
            except AccountLookupError as exc:
                errors.append(str(exc))
            else:
                if not account.is_active:
                    errors.append(f"account {account_id} is not active")

        # 4. recipient verdict (9.1 + 9.2)
        verdict = self.resolve_trusted_contact(
            candidate_id=candidate_id,
            application_id=application_id,
            canonical_job_id=canonical_job_id,
            recipient_email=recipient_email,
        )
        if not verdict.eligible:
            errors.extend(f"recipient denied: {r.value}" for r in verdict.denial_reasons)

        # 5. attachments (9.5)
        validated_attachments: list[EmailAttachment] = []
        for h in attachment_hashes:
            try:
                validated_attachments.append(self.validate_attachment(content_hash=h))
            except AttachmentValidationError as exc:
                errors.append(str(exc))

        if not subject.strip():
            errors.append("subject is required")

        if errors or package is None or account is None or not verdict.eligible:
            return SubmissionPreview(
                application_id=application_id,
                payload=None,
                errors=errors,
                recipient_verdict=verdict,
            )

        # 6. build the exact payload (9.3 + 9.4). build_payload cannot raise
        # here because we have already validated recipient/account/package.
        payload = self.build_payload(
            candidate_id=candidate_id,
            application_id=application_id,
            account=account,
            recipient=verdict,
            subject=subject,
            body=body,
            attachments=tuple(validated_attachments),
            evidence_refs=evidence_refs,
            in_reply_to=in_reply_to,
            references_header=references_header,
            message_id_header=message_id_header,
            now=now,
        )
        return SubmissionPreview(
            application_id=application_id,
            payload=payload,
            errors=[],
            recipient_verdict=verdict,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_approved_package(
        self, *, candidate_id: UUID, application_id: UUID
    ) -> ApplicationPackageVersion:
        latest = self._packages.find_latest_package_version(application_id)
        if latest is None or latest.approval_state is not PackageApprovalState.APPROVED:
            raise ApprovedPackageRequiredError(application_id)
        return latest

    def _require_owned(self, *, application_id: UUID, candidate_id: UUID) -> object:
        owned = self._ownership.find_owned_application(
            application_id=application_id, candidate_id=candidate_id
        )
        if owned is None:
            raise ApplicationNotOwnedError(application_id)
        return owned
