"""Domain contracts for the application workspace (Section 7).

Additive types that sit on top of :mod:`careerops.domain.applications`
without mutating its existing state machine or records:

- **Channel eligibility (task 7.4)** — the workspace MUST show why a channel is
  eligible or unavailable before the user can prepare a submission. The channel
  is derived from trusted job/source evidence and user choice, never from model
  output. ``EMAIL`` requires a verified recruiting contact (evidence-bound);
  ``EXTERNAL_FORM`` requires a trusted official apply URL; ``MANUAL`` always
  covers user-recorded submissions. Concrete contact resolution is deferred to
  Section 9 (trusted contacts) — this module defines the *interface* and a
  job-evidence default resolver so Gate B can drive external-form/manual
  tracking end-to-end.

- **Timeline projection (task 7.6)** — an append-only, chronological projection
  over application events, package events, submissions, and (in later gates)
  mail proposals, reviews, reminders and provider receipts. System suggestions
  are visibly distinguished from user-confirmed facts.

- **Package-binding interface (task 7.5)** — the binding between an application
  and its (job version, resume version, evidence, package version). The
  concrete package *implementation* is consumed from Section 8; this is the
  stable contract the workspace speaks to.

Iron rules honored:
- Additive (Iron Rule 8): new types, no existing domain record mutated.
- Model review-only (Iron Rule 2): no domain type here lets model output
  select a channel, bind a package, or transition state.
- Default-deny (Iron Rule 7): EMAIL is not auto-eligible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from careerops.domain.applications import (
    ApplicationEvent,
    ApplicationEventType,
    SubmissionChannel,
)


class ChannelIneligibilityReason(StrEnum):
    """Why a channel is NOT eligible for an application right now.

    These are deterministic, evidence-backed reasons surfaced to the user so
    they understand what is blocking a channel before preparation. They are
    NOT policy denials (those live in the capability resolver) — they reflect
    missing trusted evidence.
    """

    NO_TRUSTED_CONTACT = "no_trusted_contact"
    NO_OFFICIAL_APPLY_URL = "no_official_apply_url"
    CONTACT_RESOLUTION_DEFERRED = "contact_resolution_deferred"


@dataclass(frozen=True, slots=True)
class ChannelEligibility:
    """Eligibility verdict for a single submission channel.

    ``evidence_refs`` carries the trusted evidence that makes a channel
    eligible (e.g. the apply URL hash for EXTERNAL_FORM, or the contact id for
    EMAIL) so the workspace can display *why* a channel is available.
    """

    channel: SubmissionChannel
    eligible: bool
    reason: str = ""
    evidence_refs: dict[str, str] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class ChannelEligibilitySnapshot:
    """The full set of channel verdicts for one application/job pair.

    The workspace renders this as "available channels" + per-channel reasons.
    Order is deterministic: the channels are returned in a stable display order
    (EMAIL, EXTERNAL_FORM, MANUAL) regardless of eligibility.
    """

    candidate_id: UUID
    canonical_job_id: UUID
    channels: tuple[ChannelEligibility, ...] = ()

    def eligible_channels(self) -> tuple[ChannelEligibility, ...]:
        return tuple(c for c in self.channels if c.eligible)

    def is_eligible(self, channel: SubmissionChannel) -> bool:
        return any(c.channel is channel and c.eligible for c in self.channels)


# ---------------------------------------------------------------------------
# Channel-eligibility resolver interface (task 7.4)
# ---------------------------------------------------------------------------


class ChannelEligibilityResolver:
    """Protocol: resolve eligible submission channels for an application.

    Implementations derive eligibility from trusted job/source evidence and
    (for EMAIL) verified recruiting contacts. Model output MUST NOT influence
    the result. Section 9 supplies the trusted-contact resolver that makes
    EMAIL eligible; until then the default resolver marks EMAIL as deferred.
    """

    def resolve(
        self,
        *,
        candidate_id: UUID,
        canonical_job_id: UUID,
        apply_url: str,
        has_trusted_contact: bool = False,
    ) -> ChannelEligibilitySnapshot:
        """Return the channel-eligibility snapshot for the pair.

        ``apply_url`` is the trusted official apply URL resolved from job
        evidence (empty when none is known). ``has_trusted_contact`` reflects a
        Section-9 verified recruiting contact; Section 7 callers pass ``False``
        because contact resolution is deferred.
        """
        raise NotImplementedError


class JobEvidenceChannelResolver(ChannelEligibilityResolver):
    """Default resolver: EXTERNAL_FORM from apply_url, MANUAL always, EMAIL deferred.

    This is the Gate-B resolver. EMAIL is only eligible when a trusted contact
    is present (``has_trusted_contact=True``); otherwise it is returned with
    :attr:`ChannelIneligibilityReason.CONTACT_RESOLUTION_DEFERRED` so the
    workspace can explain that email delivery arrives with Section 9/10.
    """

    def resolve(
        self,
        *,
        candidate_id: UUID,
        canonical_job_id: UUID,
        apply_url: str,
        has_trusted_contact: bool = False,
    ) -> ChannelEligibilitySnapshot:
        trusted_url = apply_url.strip()
        channels: list[ChannelEligibility] = [
            ChannelEligibility(
                channel=SubmissionChannel.EMAIL,
                eligible=has_trusted_contact,
                reason=(
                    ""
                    if has_trusted_contact
                    else ChannelIneligibilityReason.CONTACT_RESOLUTION_DEFERRED.value
                ),
                evidence_refs={},
            ),
            ChannelEligibility(
                channel=SubmissionChannel.EXTERNAL_FORM,
                eligible=bool(trusted_url),
                reason=(
                    ""
                    if trusted_url
                    else ChannelIneligibilityReason.NO_OFFICIAL_APPLY_URL.value
                ),
                evidence_refs={"apply_url": trusted_url} if trusted_url else {},
            ),
            ChannelEligibility(
                channel=SubmissionChannel.MANUAL,
                eligible=True,
                reason="",
                evidence_refs={},
            ),
        ]
        return ChannelEligibilitySnapshot(
            candidate_id=candidate_id,
            canonical_job_id=canonical_job_id,
            channels=tuple(channels),
        )


# ---------------------------------------------------------------------------
# Package-binding interface (task 7.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackageBinding:
    """The immutable binding between an application and its approved package.

    The workspace shows the job version, resume version, evidence refs,
    package version, payload hash and approval state. Changing any bound input
    (job version, resume version, attachment, body byte) MUST invalidate the
    prior approval — enforced by the Section 8 package service via payload-hash
    comparison, not by this read-only binding record.

    ``payload_hash`` is ``None`` until a package is approved (Section 8).
    """

    application_id: UUID
    job_version_id: UUID | None
    resume_version_id: UUID | None
    package_version_id: UUID | None
    payload_hash: str | None
    approval_state: str
    evidence_refs: tuple[UUID, ...] = ()


class PackageBindingStore:
    """Protocol: read the current package binding for an application.

    The concrete package implementation lives in Section 8; this is the stable
    read surface the workspace and the submission-validation gate consume. A
    binding with ``approval_state == "approved"`` and a non-null
    ``payload_hash`` is the precondition for an email submission (Section 10);
    external-form/manual submissions do NOT require an approved package.
    """

    def get_binding(self, application_id: UUID) -> PackageBinding | None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Timeline projection (task 7.6)
# ---------------------------------------------------------------------------


class TimelineEntryKind(StrEnum):
    """What kind of timeline entry this is.

    ``PROPOSED_*`` entries are system suggestions (mail event proposals in
    later gates); they are visibly distinguished from confirmed facts so the
    user never reads a suggestion as a confirmed state change.
    """

    DECISION = "decision"
    PACKAGE_EVENT = "package_event"
    SUBMISSION = "submission"
    REMINDER = "reminder"
    RECEIPT = "receipt"
    PROPOSED_EVENT = "proposed_event"
    NOTE = "note"


class TimelineEntryStatus(StrEnum):
    """User-facing status of a timeline entry."""

    CONFIRMED = "confirmed"
    PROPOSED = "proposed"
    REJECTED = "rejected"
    PENDING = "pending"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One entry in the application timeline projection.

    ``source`` distinguishes who/what produced the entry (user, system,
    workflow, or a model/inbound proposal). Entries derived from proposals
    carry ``status == PROPOSED`` and never represent a confirmed state change
    until the user accepts them.
    """

    id: UUID
    application_id: UUID
    kind: TimelineEntryKind
    occurred_at: datetime
    title: str
    description: str = ""
    source: str = "user"
    status: TimelineEntryStatus = TimelineEntryStatus.CONFIRMED
    from_state: str | None = None
    to_state: str | None = None
    evidence_refs: dict[str, str] = field(default_factory=lambda: {})


def event_to_timeline_entry(event: ApplicationEvent) -> TimelineEntry:
    """Project a single append-only :class:`ApplicationEvent` into a timeline entry.

    Maps the historical event type onto a timeline kind/status. This is the
    Gate-B projection surface; later gates extend it by merging mail proposals,
    provider receipts and reminders (which carry their own timestamps) into the
    same chronological stream without rewriting event history.
    """
    kind, title = _EVENT_KIND_MAP.get(
        event.event_type, (TimelineEntryKind.NOTE, event.event_type.value)
    )
    status = TimelineEntryStatus.CONFIRMED
    if event.event_type is ApplicationEventType.SUBMITTED_MANUALLY:
        title = "Submission recorded (manual / external form)"
    elif event.event_type is ApplicationEventType.SUBMITTED_VIA_PROVIDER:
        title = "Submission delivered (system-managed send)"
        status = TimelineEntryStatus.CONFIRMED
    elif event.event_type is ApplicationEventType.PROVIDER_SEND_FAILED:
        title = "Delivery failed (provider)"
        status = TimelineEntryStatus.FAILED
    elif event.event_type is ApplicationEventType.PACKAGE_ATTACHED:
        title = "Application package attached"
    elif event.event_type is ApplicationEventType.STATE_CHANGED:
        label = event.to_state.value if event.to_state else "state"
        title = f"State changed to {label}"
    elif event.event_type is ApplicationEventType.CREATED:
        title = "Application created"
    # Event data becomes evidence refs so the UI can deep-link (e.g. the apply
    # URL and submitted timestamp for a manual submission).
    evidence_refs = {
        k: str(v)
        for k, v in (event.event_data or {}).items()
        if isinstance(v, (str, int, float, bool))
    }
    return TimelineEntry(
        id=event.id,
        application_id=event.application_id,
        kind=kind,
        occurred_at=event.occurred_at or event.created_at or datetime.fromtimestamp(0),
        title=title,
        description=event.note,
        source=event.source.value,
        status=status,
        from_state=event.from_state.value if event.from_state else None,
        to_state=event.to_state.value if event.to_state else None,
        evidence_refs=evidence_refs,
    )


_EVENT_KIND_MAP: dict[ApplicationEventType, tuple[TimelineEntryKind, str]] = {
    ApplicationEventType.CREATED: (TimelineEntryKind.DECISION, "Application created"),
    ApplicationEventType.STATE_CHANGED: (TimelineEntryKind.DECISION, "State changed"),
    ApplicationEventType.SUBMITTED_MANUALLY: (TimelineEntryKind.SUBMISSION, "Submission recorded"),
    ApplicationEventType.SUBMITTED_VIA_PROVIDER: (
        TimelineEntryKind.SUBMISSION,
        "Submission delivered (system-managed send)",
    ),
    ApplicationEventType.PROVIDER_SEND_FAILED: (TimelineEntryKind.SUBMISSION, "Delivery failed"),
    ApplicationEventType.PACKAGE_ATTACHED: (
        TimelineEntryKind.PACKAGE_EVENT,
        "Application package attached",
    ),
    ApplicationEventType.FOLLOW_UP_SCHEDULED: (TimelineEntryKind.REMINDER, "Follow-up scheduled"),
    ApplicationEventType.FOLLOW_UP_SNOOZED: (TimelineEntryKind.REMINDER, "Follow-up snoozed"),
    ApplicationEventType.FOLLOW_UP_RESCHEDULED: (
        TimelineEntryKind.REMINDER,
        "Follow-up rescheduled",
    ),
    ApplicationEventType.FOLLOW_UP_CANCELLED: (TimelineEntryKind.REMINDER, "Follow-up cancelled"),
    ApplicationEventType.NOTE_ADDED: (TimelineEntryKind.NOTE, "Note added"),
}
