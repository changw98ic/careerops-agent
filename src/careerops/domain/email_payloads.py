"""Domain contracts for trusted recruiting contacts and initial application
email payloads (Section 9, tasks 9.1-9.5).

Additive types that compose the Section-8 approved package, a trusted
recipient, a selected sending account and permitted attachments into the
*exact sendable representation* the user reviews before final confirmation.
This module ships ONLY value objects and pure functions — no persistence, no
provider calls, no state transitions. Actual sending is Section 10.

Contracts:

- **Trusted contact resolution (9.1)** — :class:`TrustedContactVerdict` binds a
  recruiting contact to its source evidence, company domain, contact type,
  confidence and retention metadata, and exposes an evidence-bound eligibility
  verdict. The verdict is DATA: model output never produces one.

- **Recipient rejection (9.2)** — :class:`RecipientDenialReason` enumerates the
  only reasons a recipient may be denied before payload creation: guessed
  employee address, unverified domain, missing source evidence, or a
  model-only recipient. :class:`RecipientIneligibleError` carries the reasons.

- **Initial application email payload (9.3 + 9.4)** —
  :class:`InitialApplicationEmailPayload` is the immutable, hash-bound
  representation binding the application, account, recipient, subject,
  normalized body, attachment hashes, thread headers (when applicable),
  evidence references, and the stable idempotency/reconciliation keys. The
  payload hash + keys are recomputable from the same inputs, so the bytes the
  provider receives are exactly the bytes the user reviewed (preview/send
  parity, task 9.8).

- **Attachment safe-material validation (9.5)** — :func:`validate_attachment`
  enforces declared/detected media-type agreement, size cap, content-hash,
  quarantine and retention checks. Unsupported/mismatched/executable/macro/
  archive/password-protected/expired attachments raise
  :class:`AttachmentValidationError`.

Iron rules honored:
- Additive (Iron Rule 8): new value objects; no existing domain record mutated.
- Model review-only (Iron Rule 2): no type here lets model output select a
  recipient, fabricate a verdict, or compute a trusted hash.
- Default-deny (Iron Rule 7): recipients default to ineligible; attachments
  default to denied.
- Append-only / reversible (Iron Rule 4): payloads are immutable value
  objects; any input change yields a new hash and a new payload instance.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

__all__ = [
    "ALLOWED_ATTACHMENT_MEDIA_TYPES",
    "ATTACHMENT_MAX_BYTES",
    "AttachmentRetentionState",
    "AttachmentValidationError",
    "EmailAccountSummary",
    "EmailAttachment",
    "EmailPayloadError",
    "InitialApplicationEmailPayload",
    "RecipientDenialReason",
    "RecipientIneligibleError",
    "TrustedContactVerdict",
    "compute_idempotency_key",
    "compute_payload_hash",
    "compute_reconciliation_key",
    "normalize_email_body",
    "recipient_domain",
    "validate_attachment",
]

# ---------------------------------------------------------------------------
# Attachment safe-material policy (task 9.5)
# ---------------------------------------------------------------------------

#: Hard size cap for any single attachment bound to an initial application
#: email. Deliberately conservative; the resume PDF + a cover letter are the
#: expected content. Oversized attachments are denied before any provider call.
ATTACHMENT_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MiB

#: Media types permitted for outbound application attachments. This mirrors
#: the inbound quarantine allowlist (``domain.email.ALLOWED_ATTACHMENT_TYPES``)
#: but is kept independent so outbound policy can evolve without weakening
#: inbound quarantine. Anything else (executables, archives, macros, office
#: docs with macros, password-protected content) is denied.
ALLOWED_ATTACHMENT_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/calendar",
    }
)

#: Media types that are *always* denied regardless of declared type, because
#: they are commonly weaponized (executables, scripts, archives, macro-bearing
#: office formats). A declared OR detected value here is an immediate deny.
_DENIED_MEDIA_PREFIXES: tuple[str, ...] = (
    "application/x-msdownload",
    "application/x-dosexec",
    "application/x-executable",
    "application/x-sh",
    "application/x-shar",
    "application/x-bat",
    "application/x-csh",
    "application/zip",
    "application/x-zip-compressed",
    "application/x-rar-compressed",
    "application/x-7z-compressed",
    "application/x-tar",
    "application/gzip",
    "application/x-bzip2",
    "application/vnd.ms-word",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.wordprocessingml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml",
    "application/vnd.openxmlformats-officedocument.presentationml",
)


class AttachmentRetentionState(StrEnum):
    """Retention/quarantine state of an attachment's underlying content.

    A payload may only bind attachments whose source content is currently
    retained and cleared (not quarantined, expired, or revoked). This is the
    outbound analogue of the inbound quarantine state.
    """

    RETAINED = "retained"
    QUARANTINED = "quarantined"
    EXPIRED = "expired"
    REVOKED = "revoked"


class RecipientDenialReason(StrEnum):
    """The only reasons a recipient may be denied before payload creation (9.2).

    These are deterministic, evidence-backed reasons; they surface to the user
    so a denial is explainable. A recipient is denied when ANY of these holds.
    """

    GUESSED_EMPLOYEE_ADDRESS = "guessed_employee_address"
    UNVERIFIED_DOMAIN = "unverified_domain"
    MISSING_SOURCE_EVIDENCE = "missing_source_evidence"
    MODEL_ONLY_RECIPIENT = "model_only_recipient"
    DOMAIN_MISMATCH = "domain_mismatch"
    INVALID_ADDRESS = "invalid_address"


# A very small, intentionally permissive RFC-5322-ish local@domain check. The
# real trust decision is the source-evidence + domain-match check below; this
# only rejects syntactically broken addresses so a typo can never become a
# trusted recipient.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def recipient_domain(email: str) -> str:
    """Return the lowercased domain part of ``email`` ("" if none)."""
    parts = email.strip().rsplit("@", 1)
    if len(parts) != 2:
        return ""
    return parts[1].strip().lower()


def normalize_email_body(body: str) -> str:
    """Return a canonicalized body for hashing (task 9.4 normalization).

    Normalization is intentionally minimal and deterministic: strip trailing
    whitespace per line, collapse runs of blank lines, and trim the whole
    string. The goal is that two visually identical bodies produce the same
    hash without obscuring real edits — it is NOT a semantic normalization.
    """
    if not body:
        return ""
    lines = [line.rstrip() for line in body.splitlines()]
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            continue
        if blank_run and out:
            out.append("")
        blank_run = 0
        out.append(line)
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmailPayloadError(Exception):
    """Base for email-payload domain errors (translated at the API)."""


class RecipientIneligibleError(EmailPayloadError):
    """The recipient failed evidence/domain/source checks (task 9.2).

    Carries the deterministic denial reasons so the API can surface them.
    """

    def __init__(self, recipient: str, reasons: list[RecipientDenialReason]) -> None:
        self.recipient = recipient
        self.reasons = list(reasons)
        super().__init__(
            f"recipient {recipient!r} is ineligible: " + ", ".join(r.value for r in reasons)
        )


class AttachmentValidationError(EmailPayloadError):
    """An attachment failed MIME/size/hash/quarantine/retention checks (9.5)."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"attachment {name!r} rejected: {reason}")


# ---------------------------------------------------------------------------
# Trusted contact verdict (task 9.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmailAccountSummary:
    """Trusted, opaque summary of the selected sending account.

    Only the fields needed to build a payload are exposed. The credential
    reference itself NEVER travels into the payload or the model path — it
    stays behind the worker boundary (Section 10). The ``status`` must be
    ``active`` for the account to be selectable.
    """

    account_id: UUID
    email_address: str
    status: str = "active"

    def __post_init__(self) -> None:
        if not _EMAIL_RE.match(self.email_address):
            raise ValueError(f"account email_address is not valid: {self.email_address!r}")

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True, slots=True)
class TrustedContactVerdict:
    """An evidence-bound eligibility verdict for a recruiting contact (9.1).

    Binds a contact to its source evidence, the trusted company domain, the
    contact type, the confidence and the retention metadata. ``eligible`` is
    True only when every evidence/domain/source check passes; otherwise
    ``denial_reasons`` is non-empty and the recipient MUST NOT enter a
    payload. This record is the single source of truth for "is this recipient
    trusted"; model output never produces one.
    """

    email: str
    contact_type: str
    confidence: str
    company_domain: str = ""
    source_evidence_url: str = ""
    source_evidence_text: str = ""
    domain_match: bool = True
    verified_at: datetime | None = None
    retention_until: datetime | None = None
    eligible: bool = False
    denial_reasons: tuple[RecipientDenialReason, ...] = ()
    application_linkage_id: UUID | None = None

    def __post_init__(self) -> None:
        if not self.email:
            raise ValueError("email is required for a contact verdict")
        # An ELIGIBLE verdict must carry the trusted company domain AND source
        # evidence — that is precisely what makes it trusted (9.1). An
        # ineligible verdict may legitimately carry neither (e.g. a model-only
        # recipient with no resolver evidence at all); its ``denial_reasons``
        # explain why no evidence is present.
        if self.eligible:
            if not self.company_domain:
                raise ValueError("an eligible verdict requires the trusted company_domain")
            if not self.source_evidence_url:
                raise ValueError("an eligible verdict requires source_evidence_url (9.1)")
        if self.eligible and self.denial_reasons:
            raise ValueError("an eligible verdict must carry no denial reasons")


# ---------------------------------------------------------------------------
# Email attachment + payload (tasks 9.3 + 9.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    """A permitted attachment bound to an email payload (task 9.3).

    ``content_hash`` is the sha256 of the attachment bytes; it binds the
    attachment into the payload hash so any byte change is detectable. The
    attachment must already have passed :func:`validate_attachment` before it
    is bound here — this type does not re-validate by construction so that the
    payload is cheap to assemble, but the service layer always validates first.
    """

    name: str
    content_hash: str
    media_type: str
    size_bytes: int
    retention_state: AttachmentRetentionState = AttachmentRetentionState.RETAINED

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("attachment name is required")
        if len(self.content_hash) != 64:
            raise ValueError("attachment content_hash must be a 64-char sha256 hex")
        if self.size_bytes < 0:
            raise ValueError("attachment size_bytes must be non-negative")


@dataclass(frozen=True, slots=True)
class InitialApplicationEmailPayload:
    """The exact, immutable, hash-bound sendable representation (tasks 9.3/9.4).

    Every field a provider needs to send the email is present and frozen. The
    ``payload_hash`` binds recipient + subject + normalized body + attachment
    hashes + account + application, so any post-review mutation is detectable
    by recomputation. ``idempotency_key`` and ``reconciliation_key`` are the
    stable identities Section 10 uses for deduplication and ambiguity
    resolution — they are deterministic functions of the payload, so the same
    logical send always maps to the same identities.

    Thread headers (``in_reply_to`` / ``references`` / ``message_id``) are
    populated when the payload rides on a linked thread; for an initial
    application they are typically empty (a fresh thread is created).
    """

    application_id: UUID
    package_version_id: UUID
    account: EmailAccountSummary
    recipient: TrustedContactVerdict
    subject: str
    body: str
    attachments: tuple[EmailAttachment, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()
    in_reply_to: str = ""
    references_header: str = ""
    message_id_header: str = ""
    payload_hash: str = ""
    idempotency_key: str = ""
    reconciliation_key: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValueError("subject is required")
        if not self.recipient.eligible:
            raise ValueError("recipient verdict must be eligible to build a payload (9.2)")
        if not self.payload_hash:
            raise ValueError("payload_hash is required (compute via compute_payload_hash)")
        if not self.idempotency_key or not self.reconciliation_key:
            raise ValueError("idempotency_key and reconciliation_key are required (9.4)")

    @property
    def attachment_hashes(self) -> tuple[str, ...]:
        return tuple(a.content_hash for a in self.attachments)


# ---------------------------------------------------------------------------
# Pure functions: hashes + keys (task 9.4)
# ---------------------------------------------------------------------------


def compute_payload_hash(
    *,
    application_id: UUID,
    account: EmailAccountSummary,
    recipient_email: str,
    subject: str,
    body: str,
    attachments: tuple[EmailAttachment, ...],
    in_reply_to: str = "",
    references_header: str = "",
    evidence_refs: tuple[UUID, ...] = (),
) -> str:
    """Return the canonical sha256 (64 hex) over the exact sendable bytes.

    Binds application, account, recipient, subject, normalized body,
    attachment hashes, thread headers and evidence refs. Two payloads with
    the same hash are byte-for-byte equivalent at the provider boundary —
    this is the preview/send parity guarantee (task 9.8).
    """
    canonical = {
        "application_id": str(application_id),
        "account_id": str(account.account_id),
        "account_email": account.email_address,
        "recipient": recipient_email.strip().lower(),
        "subject": subject,
        "body": normalize_email_body(body),
        "attachments": sorted(
            [{"name": a.name, "content_hash": a.content_hash} for a in attachments],
            key=lambda a: a["name"],
        ),
        "in_reply_to": in_reply_to,
        "references": references_header,
        "evidence_refs": sorted(str(e) for e in evidence_refs),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_idempotency_key(*, account_email: str, application_id: UUID, payload_hash: str) -> str:
    """Stable idempotency key for an initial application send (task 9.4).

    Binds the sending account + the application + the exact payload hash. The
    same logical send (same account, same application, same bytes) always maps
    to the same key, so a duplicate confirmation (double-click, retry after
    crash) yields at most one provider effect (Section 10 enforces this).
    """
    canonical = f"initial:{account_email}:{application_id}:{payload_hash}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_reconciliation_key(
    *, account_email: str, application_id: UUID, payload_hash: str
) -> str:
    """Stable provider reconciliation key for an initial send (task 9.4).

    Distinct from the idempotency key: this is what the worker searches the
    provider with when a local receipt is missing (timeout / crash). Using a
    separate key means a reconciliation lookup cannot be confused with a
    duplicate-send decision.
    """
    canonical = f"reconcile:initial:{account_email}:{application_id}:{payload_hash}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Attachment validation (task 9.5)
# ---------------------------------------------------------------------------


def validate_attachment(
    *,
    name: str,
    content_hash: str,
    declared_media_type: str,
    detected_media_type: str,
    size_bytes: int,
    retention_state: AttachmentRetentionState = AttachmentRetentionState.RETAINED,
    password_protected: bool = False,
) -> EmailAttachment:
    """Validate a single attachment against the safe-material policy (9.5).

    Returns the bound :class:`EmailAttachment` when every check passes, else
    raises :class:`AttachmentValidationError`. Checks:

    - declared and detected media types agree and are in the allowlist;
    - no denied prefix (executable / archive / macro-bearing office format);
    - size within ``ATTACHMENT_MAX_BYTES``;
    - content_hash is a 64-char sha256;
    - retention state is RETAINED (not quarantined/expired/revoked);
    - not password-protected.
    """
    if not name.strip():
        raise AttachmentValidationError(name or "<unnamed>", "name is required")
    declared = declared_media_type.strip().lower()
    detected = detected_media_type.strip().lower()
    if declared != detected:
        raise AttachmentValidationError(
            name,
            f"declared media type {declared!r} differs from detected {detected!r}",
        )
    for prefix in _DENIED_MEDIA_PREFIXES:
        if declared == prefix or declared.startswith(prefix):
            raise AttachmentValidationError(name, f"media type {declared!r} is on the deny list")
    if declared not in ALLOWED_ATTACHMENT_MEDIA_TYPES:
        raise AttachmentValidationError(name, f"media type {declared!r} is not in the allowed set")
    if size_bytes > ATTACHMENT_MAX_BYTES:
        raise AttachmentValidationError(
            name,
            f"size {size_bytes} exceeds max {ATTACHMENT_MAX_BYTES}",
        )
    if len(content_hash) != 64:
        raise AttachmentValidationError(name, "content_hash must be a 64-char sha256 hex")
    if retention_state is not AttachmentRetentionState.RETAINED:
        raise AttachmentValidationError(
            name, f"retention state is {retention_state.value} (not retained)"
        )
    if password_protected:
        raise AttachmentValidationError(name, "password-protected content is denied")
    return EmailAttachment(
        name=name,
        content_hash=content_hash,
        media_type=declared,
        size_bytes=size_bytes,
        retention_state=retention_state,
    )
