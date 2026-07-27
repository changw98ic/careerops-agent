"""Domain contracts for system-managed Gmail sending (Section 10).

Section 10 turns the CareerOps final-confirmation click into a *durable
approval / intent request* rather than a direct provider call. The user's
confirmation is persisted as the kernel ``ApprovalRequest``; an isolated
side-effect worker later resolves opaque credentials, calls the (fake, in
this change) Gmail provider, persists the provider receipt, and only then
appends an application ``SUBMITTED`` event.

This module ships ONLY the value types the service and API exchange. It
intentionally reuses the existing :mod:`careerops.domain.side_effects`
authorization chain (``ActionIntent`` / payload version / policy decision /
approval / attempt / receipt) and the :mod:`careerops.domain.applications`
append-only event log — no new persistence table is introduced.

Iron rules honored:
- Model review-only (Iron Rule 2): the recipient, channel and trusted facts
  are server-sourced; model output never populates :class:`SystemSendRequest`.
- Append-only / reversible (Iron Rule 4): the application event is appended
  once, only after a confirmed receipt; ambiguous outcomes never produce a
  submitted event.
- Default-deny (Iron Rule 7): the contract types carry denial reason codes
  so the service can refuse BEFORE any provider call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from careerops.domain.email_payloads import normalize_email_body

__all__ = [
    "POLICY_VERSION",
    "SystemSendDenialReason",
    "SystemSendPhase",
    "SystemSendRequest",
    "SystemSendStatus",
    "compute_system_send_idempotency_key",
    "compute_system_send_payload_hash",
]


# Bumped whenever the revalidation ruleset changes. Recorded on the payload so
# a stale confirmation (carried over from an older policy version) re-validates
# against the current rules at confirmation time (task 10.2).
POLICY_VERSION = "section10.system_send.v1"


class SystemSendPhase(StrEnum):
    """User-facing lifecycle of one system-managed send.

    ``PENDING`` — confirmation recorded; the isolated worker has not yet
    produced a terminal receipt. ``SENT`` — provider receipt confirmed and the
    application ``SUBMITTED`` event appended. ``FAILED`` — definitive provider
    failure (validation class). ``RECONCILIATION_REQUIRED`` — ambiguous outcome
    (timeout / crash / unknown result); blind retry is disabled and the user
    receives a reconciliation task.
    """

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class SystemSendDenialReason(StrEnum):
    """Reason codes for a confirmation denied BEFORE the provider call (10.8).

    Every denial is fail-closed: the service raises before ``kernel.propose``
    is allowed to reach the provider, and the codes are safe to surface in UI
    / operator messaging. They are produced from trusted business state only.
    """

    APPLICATION_NOT_OWNED = "application_not_owned"
    APPLICATION_NOT_PREPARING = "application_not_preparing"
    PACKAGE_NOT_APPROVED = "package_not_approved"
    PAYLOAD_HASH_MISMATCH = "payload_hash_mismatch"
    PACKAGE_BINDING_STALE = "package_binding_stale"
    RECIPIENT_NOT_ELIGIBLE = "recipient_not_eligible"
    CAPABILITY_NOT_RELEASED = "capability_not_released"
    ACCOUNT_NOT_ACTIVE = "account_not_active"
    POLICY_DENIED = "policy_denied"
    OUTBOX_UNAVAILABLE = "outbox_unavailable"


@dataclass(frozen=True, slots=True)
class SystemSendRequest:
    """Trusted inputs for one CareerOps final-confirmation (task 10.1).

    Every field is server-sourced business state. The recipient is derived
    from a trusted recruiting contact / verified reply target (never model
    output); the payload hash binds the exact bytes the user reviewed so any
    post-confirmation mutation invalidates the approval.
    """

    application_id: UUID
    candidate_id: UUID
    account_email: str
    recipient: str
    subject: str
    body: str
    package_version_id: UUID
    payload_hash: str
    job_version_id: UUID | None = None
    resume_version_id: UUID | None = None
    attachment_hashes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    thread_headers: dict[str, str] = field(default_factory=lambda: {})
    account_id: UUID | None = None
    package_payload_hash: str | None = None
    canonical_job_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SystemSendStatus:
    """Durable status returned to the UI / worker (tasks 10.3, 10.7, 10.11).

    ``phase`` is the single source of truth for the UI send-progress state.
    ``intent_id`` / ``payload_version_id`` identify the kernel authorization
    chain; ``receipt`` fields are populated only after a confirmed provider
    success. ``denial_reasons`` carries the fail-closed codes when the
    confirmation was refused before the provider call.
    """

    application_id: UUID
    intent_id: UUID | None
    phase: SystemSendPhase
    payload_hash: str | None = None
    policy_version: str = POLICY_VERSION
    provider_resource_id: str | None = None
    provider_message_id: str | None = None
    submitted_at: datetime | None = None
    denial_reasons: tuple[str, ...] = ()
    reconciled: bool = False


def compute_system_send_idempotency_key(
    *,
    application_id: UUID,
    account_email: str,
    recipient: str,
    normalized_payload_hash: str,
) -> str:
    """Stable idempotency key for one logical system-managed send.

    Binds application + account + recipient + the exact payload bytes. The
    same confirmation (same recipient, same reviewed body) always maps to the
    same :class:`ActionIntent`, so a duplicate click, a worker crash, or a
    timeout all reconcile against the same provider reconciliation key and
    produce at most one provider effect (tasks 10.6, 10.7).
    """
    canonical = (
        f"system_send:{application_id}:{account_email}:{recipient}:{normalized_payload_hash}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_system_send_payload_hash(
    *,
    application_id: UUID,
    account_id: UUID | None,
    account_email: str,
    recipient: str,
    subject: str,
    body: str,
    attachment_hashes: tuple[str, ...] = (),
    attachment_names: tuple[str, ...] = (),
    thread_headers: dict[str, str] | None = None,
    evidence_refs: tuple[str, ...] = (),
) -> str:
    """Hash the exact sendable representation before creating an intent.

    This mirrors the Section-9 preview hash inputs.  The API-supplied hash is
    only an assertion from the client; the confirmation path must recompute
    this value from the request and the approved package before any kernel
    proposal is created.  Attachment names are read from the approved package
    when available, so changing an attachment or its rendered name changes the
    hash as well.
    """
    if attachment_names and len(attachment_names) != len(attachment_hashes):
        raise ValueError("attachment names must match attachment hashes")
    names = attachment_names or attachment_hashes
    headers = thread_headers or {}
    canonical = {
        "application_id": str(application_id),
        "account_id": str(account_id) if account_id else "",
        "account_email": account_email,
        "recipient": recipient.strip().lower(),
        "subject": subject,
        "body": normalize_email_body(body),
        "attachments": sorted(
            [
                {"name": name, "content_hash": content_hash}
                for name, content_hash in zip(names, attachment_hashes, strict=True)
            ],
            key=lambda item: item["name"],
        ),
        "in_reply_to": headers.get("in_reply_to", ""),
        "references": headers.get("references", ""),
        "evidence_refs": sorted(str(ref) for ref in evidence_refs),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
