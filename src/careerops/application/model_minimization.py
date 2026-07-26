"""Model-input minimization for optional resume tailoring (Section 3, task 3.7).

When the ``MODEL_TAILORING`` capability is released (default OFF — Iron Rule 6),
the ONLY content that may leave the local boundary toward a model provider is:

- the user-**selected** candidate evidence, and only the ``CONFIRMED`` subset of
  it (claim→evidence, Iron Rule 4), and
- a **bounded** excerpt of the job text the tailoring is for.

Everything else MUST be refused: raw resume files, credentials, unselected
mailbox content, unselected evidence, and any unconfirmed/rejected evidence.
Each refused input is recorded as a :class:`DeniedEgress` audit entry so the
operator can see what was intercepted at the boundary (spec: "Tailoring request
contains unrelated private material → removes or rejects that content before
model egress and records a denied-egress reason").

When the model capability is disabled (the default), :func:`build_tailoring_input`
returns ``enabled=False`` WITHOUT attempting any model request, and the
deterministic parsing / filtering / package-validation paths stay fully usable
(spec: "Model provider is disabled → deterministic parsing, evidence
management, filtering, and package validation remain usable and no model request
is made").

The capability gate is wired through :func:`is_external_effect_path_usable` so
Iron Rule 3 precedence (dependency-not-ready 503 beats capability denial 403)
lives in one place. This module builds the input only — it does NOT call a
model provider; the actual egress is a later (Section 8) concern that MUST
consume this minimized payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from careerops.domain.applications import ConfirmationStatus
from careerops.domain.candidates import EvidenceItem
from careerops.orchestration.capability_resolver import (
    CapabilityKind,
    SettingsCapabilityResolver,
    is_external_effect_path_usable,
)

__all__ = [
    "DEFAULT_MAX_JOB_CHARS",
    "DeniedEgress",
    "EvidenceReadRepositoryProtocol",
    "ModelTailoringInput",
    "ModelTailoringRequest",
    "build_tailoring_input",
]

# Bounded job-text excerpt (chars). The full posting never leaves the boundary;
# only this prefix is eligible for the model input. Large enough to carry the
# requirements section, small enough to bound cost/leakage.
DEFAULT_MAX_JOB_CHARS = 4000

# Reasons recorded in DeniedEgress — closed strings so metrics/audits can group.
_REASON_RAW_RESUME = "raw_resume_refused_at_model_boundary"
_REASON_CREDENTIAL = "credential_refused_at_model_boundary"
_REASON_UNSELECTED = "unselected_content_refused_at_model_boundary"
_REASON_EVIDENCE_NOT_FOUND = "selected_evidence_not_found"
_REASON_EVIDENCE_NOT_CONFIRMED = "selected_evidence_not_confirmed"


@dataclass(frozen=True, slots=True)
class DeniedEgress:
    """A bounded record of content refused at the model egress boundary.

    ``reason`` is a closed string (one of the ``_REASON_*`` constants above).
    ``detail`` carries an opaque, non-sensitive hint safe to surface in
    operator messaging (never the refused content itself).
    """

    reason: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ModelTailoringRequest:
    """Inputs the caller would have sent for tailoring.

    ``selected_evidence_ids`` is the subset the user explicitly chose;
    unselected evidence is never included. ``raw_resume_bytes`` and
    ``credentials`` are accepted by this builder only so it can refuse them
    auditably — a caller that has them on hand still gets a clean denial
    rather than silently forwarding them.
    """

    candidate_id: UUID
    selected_evidence_ids: tuple[UUID, ...]
    job_text: str
    raw_resume_bytes: bytes | None = None
    credentials: str | None = None


@dataclass(frozen=True, slots=True)
class ModelTailoringInput:
    """The minimized payload eligible to leave the boundary, or a disabled marker.

    ``enabled=False`` means the model capability is not released (or its
    backing dependency is down); ``evidence`` and ``job_excerpt`` are empty and
    the caller MUST proceed via the deterministic path with no model request.

    ``enabled=True`` means the payload is the minimal, validated input: only
    CONFIRMED selected evidence + a bounded job excerpt. ``denied_egress``
    records anything the builder refused (raw resume, credentials, unselected
    content, unconfirmed evidence); those entries are append-only audit facts.
    """

    enabled: bool
    evidence: tuple[EvidenceItem, ...]
    job_excerpt: str
    denied_egress: tuple[DeniedEgress, ...]
    disabled_reason: str = ""
    usable: bool = True


class EvidenceReadRepositoryProtocol(Protocol):
    """Read slice :func:`build_tailoring_input` needs to resolve selections."""

    def get_by_id(self, candidate_id: UUID, evidence_id: UUID) -> EvidenceItem: ...


def build_tailoring_input(
    *,
    request: ModelTailoringRequest,
    evidence_repository: EvidenceReadRepositoryProtocol,
    capability_resolver: SettingsCapabilityResolver,
    dependency_available: bool = True,
    max_job_chars: int = DEFAULT_MAX_JOB_CHARS,
) -> ModelTailoringInput:
    """Build the minimal model input or mark the path disabled.

    Iron Rule 5 + 6: the capability gate runs first. When the path is not
    usable (capability denied OR backing dependency down), the result is
    ``enabled=False`` and NO content is assembled — deterministic paths stay
    usable and no model request is made. The ``usable`` flag is False only
    when the cause was a missing dependency (so the caller can surface 503);
    a plain capability denial returns ``usable=True, enabled=False``.
    """
    usable, reason = is_external_effect_path_usable(
        capability_resolver,
        CapabilityKind.MODEL_TAILORING,
        dependency_available=dependency_available,
    )
    if not usable:
        if reason == "dependency_not_ready":
            return ModelTailoringInput(
                enabled=False,
                evidence=(),
                job_excerpt="",
                denied_egress=(),
                disabled_reason=reason,
                usable=False,
            )
        # Capability denied (default-deny): deterministic paths stay usable.
        return ModelTailoringInput(
            enabled=False,
            evidence=(),
            job_excerpt="",
            denied_egress=(),
            disabled_reason=reason,
            usable=True,
        )

    # Capability released — assemble the minimal payload and refuse everything
    # forbidden, recording one DeniedEgress entry per interception.
    denied: list[DeniedEgress] = []

    if request.raw_resume_bytes is not None:
        denied.append(DeniedEgress(reason=_REASON_RAW_RESUME))
    if request.credentials is not None:
        denied.append(DeniedEgress(reason=_REASON_CREDENTIAL))

    selected_evidence = _resolve_selected_evidence(
        candidate_id=request.candidate_id,
        selected_evidence_ids=request.selected_evidence_ids,
        evidence_repository=evidence_repository,
        denied=denied,
    )
    job_excerpt = _bound_job_text(request.job_text, max_job_chars)

    return ModelTailoringInput(
        enabled=True,
        evidence=selected_evidence,
        job_excerpt=job_excerpt,
        denied_egress=tuple(denied),
        disabled_reason="",
        usable=True,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _resolve_selected_evidence(
    *,
    candidate_id: UUID,
    selected_evidence_ids: tuple[UUID, ...],
    evidence_repository: EvidenceReadRepositoryProtocol,
    denied: list[DeniedEgress],
) -> tuple[EvidenceItem, ...]:
    """Return only the CONFIRMED selected evidence; deny the rest.

    A missing id, an id that belongs to another candidate (raises NotFound in
    the repo), an unconfirmed, or a rejected item is refused with a
    denied-egress entry. Only CONFIRMED evidence may reach the model boundary
    (claim→evidence + confirmation gate, Iron Rule 4).
    """
    from careerops.api.errors import NotFoundError  # local import: avoid a cycle at import time

    accepted: list[EvidenceItem] = []
    for evidence_id in selected_evidence_ids:
        try:
            item = evidence_repository.get_by_id(candidate_id, evidence_id)
        except NotFoundError:
            denied.append(DeniedEgress(reason=_REASON_EVIDENCE_NOT_FOUND, detail=str(evidence_id)))
            continue
        if item.confirmation_status is not ConfirmationStatus.CONFIRMED:
            denied.append(
                DeniedEgress(
                    reason=_REASON_EVIDENCE_NOT_CONFIRMED,
                    detail=f"{evidence_id}:{item.confirmation_status.value}",
                )
            )
            continue
        accepted.append(item)
    return tuple(accepted)


def _bound_job_text(job_text: str, max_chars: int) -> str:
    """Return the bounded leading excerpt of the job text (no full posting)."""
    if max_chars <= 0:
        return ""
    if len(job_text) <= max_chars:
        return job_text
    return job_text[:max_chars]
