"""Domain contracts for job-specific application package versions (Section 8).

Additive to :mod:`careerops.domain.applications` (which keeps the M3
single-package ``ApplicationPackage``). This module introduces the immutable,
versioned, evidence-bound package the new contract requires (design Decision 4,
career-profile-and-resume spec):

- A package is specific to one application + exact job version + profile
  version + confirmed resume version + selected evidence.
- Every positive claim references confirmed candidate evidence; a claim with no
  evidence makes the package invalid and blocks approval (task 8.6).
- The package stores a deterministic requirement/evidence comparison with
  explainable gaps (task 8.3) and a diff against the immutable resume content
  (task 8.5). User edits and review-only model suggestions create NEW versions;
  the prior version — including an approved one — is never overwritten.
- Approval (task 8.7) freezes the payload hash, approver and timestamp. Any
  later mutation of a bound input produces a new draft version, superseding
  the prior approval (invalidation-on-mutation).

Iron rules honored:
- Append-only / reversible (Iron Rule 4): versions are immutable; history is
  never rewritten.
- Model review-only (Iron Rule 2): model diff entries carry ``source='model'``
  and never become trusted claims without user confirmation.
- Default-deny (Iron Rule 7): tailoring is optional and gated by the
  MODEL_TAILORING capability at the service layer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from careerops.domain.applications import PackageApprovalState

__all__ = [
    "ApplicationPackageVersion",
    "PackageApprovalValidationError",
    "PackageAttachment",
    "PackageClaimVersion",
    "PackageDiffEntry",
    "RequirementGap",
    "compute_payload_hash",
]


@dataclass(frozen=True, slots=True)
class PackageAttachment:
    """A permitted attachment on a package version (e.g. the resume PDF).

    ``content_hash`` is the sha256 of the attachment bytes; it binds the
    attachment into the payload hash so any byte change is detectable.
    """

    name: str
    content_hash: str
    media_type: str = ""
    size_bytes: int = 0

    def __post_init__(self) -> None:
        if not self.content_hash:
            raise ValueError("attachment content_hash is required")


@dataclass(frozen=True, slots=True)
class PackageClaimVersion:
    """A claim in a package version, bound to confirmed candidate evidence.

    A claim with no ``evidence_ids`` is invalid and blocks approval (task 8.6).
    """

    claim_text: str
    evidence_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not self.claim_text.strip():
            raise ValueError("claim_text is required")
        if not self.evidence_ids:
            raise ValueError("every positive claim must be traceable to evidence")


@dataclass(frozen=True, slots=True)
class RequirementGap:
    """An explainable gap between a job requirement and candidate evidence.

    Produced by the deterministic requirement/evidence comparison (task 8.3),
    reusing the Section 6 match levels. ``match_level`` is one of
    unsupported / hard_fail / partial / transferable.
    """

    requirement_name: str
    match_level: str
    reason: str = ""
    rules_version: str = ""


@dataclass(frozen=True, slots=True)
class PackageDiffEntry:
    """One edit in a package diff against the immutable resume content.

    ``source`` is ``'user'`` for user edits or ``'model'`` for review-only
    suggestions. Model entries never become trusted claims without explicit
    user confirmation (they are folded into a new version only on accept).
    """

    section: str
    original_text: str = ""
    proposed_text: str = ""
    evidence_ids: tuple[UUID, ...] = ()
    source: str = "user"


@dataclass(frozen=True, slots=True)
class ApplicationPackageVersion:
    """An immutable, job-specific application package version."""

    id: UUID
    application_id: UUID
    version_number: int
    resume_version_id: UUID
    job_version_id: UUID | None = None
    profile_version_id: UUID | None = None
    cover_letter_text: str = ""
    notes: str = ""
    answers: dict[str, str] = field(default_factory=lambda: {})
    claims: tuple[PackageClaimVersion, ...] = ()
    attachments: tuple[PackageAttachment, ...] = ()
    diff: tuple[PackageDiffEntry, ...] = ()
    requirement_gaps: tuple[RequirementGap, ...] = ()
    payload_hash: str | None = None
    approval_state: PackageApprovalState = PackageApprovalState.DRAFT
    approved_at: datetime | None = None
    approved_by: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_approved(self) -> bool:
        return self.approval_state is PackageApprovalState.APPROVED


class PackageApprovalValidationError(Exception):
    """Raised when package approval preconditions are not met (task 8.6)."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("package approval blocked: " + "; ".join(reasons))


def compute_payload_hash(
    *,
    resume_version_id: UUID,
    job_version_id: UUID | None,
    profile_version_id: UUID | None,
    cover_letter_text: str,
    notes: str,
    answers: dict[str, str],
    claims: tuple[PackageClaimVersion, ...],
    attachments: tuple[PackageAttachment, ...],
) -> str:
    """Return the canonical sha256 (64 hex) over the exact package inputs.

    The hash binds the resume version, job/profile versions, body text, answers,
    claims (text + sorted evidence refs) and attachment hashes. It is frozen at
    approval time; any input mutation changes the hash, so a stored approved
    payload hash that no longer matches the recomputed hash signals tampering or
    a superseded version. Version immutability (copy-on-write) is what makes an
    approved payload stable in practice.
    """
    canonical = {
        "resume_version_id": str(resume_version_id),
        "job_version_id": str(job_version_id) if job_version_id else None,
        "profile_version_id": str(profile_version_id) if profile_version_id else None,
        "cover_letter_text": cover_letter_text,
        "notes": notes,
        "answers": dict(sorted(answers.items())),
        "claims": [
            {
                "claim_text": c.claim_text,
                "evidence_ids": sorted(str(e) for e in c.evidence_ids),
            }
            for c in claims
        ],
        "attachments": sorted(
            [{"name": a.name, "content_hash": a.content_hash} for a in attachments],
            key=lambda a: a["name"],
        ),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
