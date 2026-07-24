"""Bidirectional conversions between domain dataclasses and JSON-safe DTOs.

Plan v0.4 §2.6. The checkpoint state stores JSON-safe DTOs only (``state.py``);
domain dataclasses (``adapters.job_sources.RawJobRecord``,
``domain.contacts.RecruitingContact``, ``domain.email.ReplyDraft``) live only at
node boundaries. This module is the ONLY place those translations happen.

Provenance rules enforced here:

- ``RawJobRecord`` carries no provenance; the fetch layer
  (``adapters.http_fetcher.FetchedResponse`` / ``AdapterFetchResult``) owns
  ``source_url`` / ``fetched_at`` / ``response_hash`` / ``parser_version``.
  ``JobProvenance`` bundles those four and ``raw_job_record_to_dto`` attaches
  them. ``raw_job_dto_to_record`` strips them back to the parser's view.
- ``RecruitingContact`` REQUIRES a non-empty ``source_url`` and
  ``publicly_listed=True`` (enforced in ``__post_init__``). The DTO has neither
  ``id`` / ``company_id`` nor a URL, so ``contact_dto_to_recruiting_contact``
  takes them as required parameters — the caller is the trusted boundary that
  supplies provenance, never the DTO itself.
- ``ReplyDraft`` requires ``message_id`` / ``thread_id`` / ``account_id`` UUIDs.
  v1 has no real Gmail thread, so ``draft_dto_to_reply_draft`` accepts them as
  required params (the caller passes placeholder UUIDs at the send boundary);
  it never invents recipient or target changes (ADR 0006 / plan v0.4 §2.5).

All functions are pure: no I/O, no logging, no model calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from careerops.adapters.job_sources import RawJobRecord
from careerops.application.llm_matching import JobMatchResult, SkillProfile
from careerops.domain.contacts import (
    ContactConfidence,
    ContactSource,
    RecruitingContact,
    compute_allowed_actions,
)
from careerops.domain.email import DraftStatus, ReplyDraft
from careerops.orchestration.state import (
    ContactDTO,
    DraftDTO,
    JobMatchDTO,
    RawJobDTO,
    SkillProfileDTO,
)

__all__ = [
    "JobProvenance",
    "build_draft_dto",
    "contact_dto_to_recruiting_contact",
    "draft_dto_to_reply_draft",
    "job_match_dto_to_result",
    "job_match_result_to_dto",
    "raw_job_dto_to_record",
    "raw_job_record_to_dto",
    "raw_job_records_to_dtos",
    "recruiting_contact_to_dto",
    "skill_profile_from_dto",
    "skill_profile_to_dto",
]


# ---------------------------------------------------------------------------
# Job provenance bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobProvenance:
    """Fetch-layer provenance attached to a ``RawJobRecord`` at the boundary.

    Mirrors the four fields ``http_fetcher.FetchedResponse`` produces plus the
    adapter ``parser_version``. ``fetched_at`` is the aware-UTC datetime captured
    at fetch time; it is serialized to ISO-8601 on the DTO.
    """

    source_url: str = ""
    fetched_at: datetime | None = None
    response_hash: str = ""
    parser_version: str = ""


# ---------------------------------------------------------------------------
# RawJobRecord <-> RawJobDTO
# ---------------------------------------------------------------------------


def raw_job_record_to_dto(record: RawJobRecord, provenance: JobProvenance) -> RawJobDTO:
    """Attach fetch provenance to a parsed ``RawJobRecord`` -> JSON-safe DTO.

    ``raw_data`` is the parsed JSON object exactly as returned by the adapter
    (already JSON-safe). ``fetched_at`` becomes an ISO-8601 aware-UTC string;
    if the provenance carries no timestamp the field is omitted (``total=False``).
    """
    dto: RawJobDTO = RawJobDTO(
        external_id=record.external_id,
        title=record.title,
        location=record.location,
        url=record.url,
        description=record.description,
        raw_data=dict(record.raw_data),
    )
    if provenance.source_url:
        dto["source_url"] = provenance.source_url
    if provenance.fetched_at is not None:
        dto["fetched_at"] = provenance.fetched_at.isoformat()
    if provenance.response_hash:
        dto["response_hash"] = provenance.response_hash
    if provenance.parser_version:
        dto["parser_version"] = provenance.parser_version
    return dto


def raw_job_records_to_dtos(
    records: tuple[RawJobRecord, ...], provenance: JobProvenance
) -> tuple[RawJobDTO, ...]:
    """Batch-convert records sharing one provenance (e.g. one list response)."""
    return tuple(raw_job_record_to_dto(r, provenance) for r in records)


def raw_job_dto_to_record(dto: RawJobDTO) -> RawJobRecord:
    """Strip checkpoint DTO back to the adapter's parser view.

    Provenance (``source_url`` / ``fetched_at`` / ``response_hash`` /
    ``parser_version``) is intentionally dropped: it belongs to the fetch layer,
    not the parser. Round-trip ``record -> dto -> record`` is lossless for the
    parser fields; ``dto -> record -> dto`` is lossless only when the same
    provenance is re-attached.
    """
    raw = dto.get("raw_data")
    return RawJobRecord(
        external_id=dto.get("external_id", ""),
        title=dto.get("title", ""),
        location=dto.get("location", ""),
        url=dto.get("url", ""),
        description=dto.get("description", ""),
        raw_data=dict(raw) if isinstance(raw, dict) else {},
    )


# ---------------------------------------------------------------------------
# RecruitingContact <-> ContactDTO
# ---------------------------------------------------------------------------


def recruiting_contact_to_dto(contact: RecruitingContact) -> ContactDTO:
    """Project a domain ``RecruitingContact`` onto its JSON-safe DTO.

    ``id`` / ``company_id`` / ``source_url`` are provenance owned by the domain
    layer; only the fields needed downstream (draft recipient selection,
    evidence display) are kept. ``publicly_listed`` is preserved because the
    draft node only considers publicly listed contacts as recipients.
    """
    dto: ContactDTO = ContactDTO(
        email=contact.email,
        publicly_listed=contact.publicly_listed,
    )
    if contact.role:
        dto["platform"] = contact.role
    if contact.name:
        dto["company_hint"] = contact.name
    if contact.source:
        dto["post_type"] = contact.source.value
    if contact.source_text:
        dto["context"] = contact.source_text
    if contact.source_url:
        dto["source_file"] = contact.source_url
    if contact.verified_at is not None:
        dto["extracted_at"] = contact.verified_at.isoformat()
    return dto


def contact_dto_to_recruiting_contact(
    dto: ContactDTO,
    *,
    contact_id: UUID,
    company_id: UUID,
    source_url: str,
    confidence: ContactConfidence = ContactConfidence.HIGH,
    domain_match: bool = True,
    source: ContactSource = ContactSource.JOB_PAGE,
) -> RecruitingContact:
    """Build a domain ``RecruitingContact`` from a DTO + trusted provenance.

    The DTO carries no ``id`` / ``company_id`` / ``source_url`` and cannot
    itself guarantee ``publicly_listed``. Those are supplied by the caller
    (the trusted node boundary); ``source_url`` MUST be non-empty or the domain
    constructor raises (fail-closed evidence invariant).

    A DTO with ``publicly_listed`` explicitly False is rejected: only publicly
    listed contacts are accepted into the domain (``RecruitingContact`` invariant).
    """
    if dto.get("publicly_listed") is False:
        raise ValueError("only publicly_listed contacts can be promoted to domain")
    verified_raw = dto.get("extracted_at")
    verified_at: datetime | None = None
    if isinstance(verified_raw, str) and verified_raw:
        verified_at = datetime.fromisoformat(verified_raw)
    confidence_final = confidence
    allowed = compute_allowed_actions(confidence_final, domain_match)
    return RecruitingContact(
        id=contact_id,
        company_id=company_id,
        email=dto.get("email", ""),
        name=dto.get("company_hint", ""),
        role=dto.get("platform", ""),
        source=source,
        source_url=source_url,
        source_text=dto.get("context", ""),
        publicly_listed=True,
        domain_match=domain_match,
        confidence=confidence_final,
        allowed_actions=allowed,
        verified_at=verified_at,
    )


# ---------------------------------------------------------------------------
# SkillProfile <-> SkillProfileDTO
# ---------------------------------------------------------------------------


def skill_profile_to_dto(profile: SkillProfile) -> SkillProfileDTO:
    """Project a ``SkillProfile`` onto its JSON-safe DTO."""
    return SkillProfileDTO(
        skills=tuple(profile.skills),
        level=profile.level,
        years=profile.years,
        highlights=profile.highlights,
    )


def skill_profile_from_dto(dto: SkillProfileDTO) -> SkillProfile:
    """Reconstruct a ``SkillProfile`` from its DTO."""
    skills_raw = dto.get("skills")
    return SkillProfile(
        skills=tuple(skills_raw) if isinstance(skills_raw, tuple) else (),
        level=dto.get("level", "unknown"),
        years=dto.get("years", ""),
        highlights=dto.get("highlights", ""),
    )


# ---------------------------------------------------------------------------
# JobMatchResult <-> JobMatchDTO
# ---------------------------------------------------------------------------


def job_match_result_to_dto(job_external_id: str, result: JobMatchResult) -> JobMatchDTO:
    """Project an advisory ``JobMatchResult`` onto its JSON-safe DTO.

    ``transferable_skills`` / ``confidence`` / ``model_id`` are dropped: the
    checkpoint only needs the fields the review_gate and draft node consume.
    ``is_review_only`` is carried verbatim (ADR 0006: model output is advisory).
    """
    return JobMatchDTO(
        external_id=job_external_id,
        company=result.company,
        title=result.title,
        match_score=result.match_score,
        tier=result.tier,
        recommendation=result.recommendation,
        seniority_fit=result.seniority_fit,
        remote_compatible=result.remote_compatible,
        matched_requirements=tuple(result.matched_requirements),
        gaps=tuple(result.gaps),
        reasoning=result.reasoning,
        is_review_only=result.is_review_only,
        error=result.error,
    )


def job_match_dto_to_result(dto: JobMatchDTO) -> JobMatchResult:
    """Reconstruct an advisory ``JobMatchResult`` from its DTO.

    Fields absent from the DTO (``transferable_skills`` / ``confidence`` /
    ``model_id``) take their defaults. ``external_id`` is not part of the result
    (it identifies the job, not the match).
    """
    matched_raw = dto.get("matched_requirements")
    gaps_raw = dto.get("gaps")
    remote = dto.get("remote_compatible")
    return JobMatchResult(
        company=dto.get("company", ""),
        title=dto.get("title", ""),
        match_score=int(dto.get("match_score", 0)),
        tier=dto.get("tier", "mismatch"),
        matched_requirements=tuple(matched_raw) if isinstance(matched_raw, tuple) else (),
        gaps=tuple(gaps_raw) if isinstance(gaps_raw, tuple) else (),
        seniority_fit=dto.get("seniority_fit", "unclear"),
        remote_compatible=remote if isinstance(remote, bool) else None,
        reasoning=dto.get("reasoning", ""),
        recommendation=dto.get("recommendation", "skip"),
        is_review_only=bool(dto.get("is_review_only", True)),
        error=dto.get("error", ""),
    )


# ---------------------------------------------------------------------------
# (subject, body) -> DraftDTO  +  DraftDTO -> ReplyDraft envelope
# ---------------------------------------------------------------------------


def build_draft_dto(
    *,
    job_external_id: str,
    recipient: str,
    subject: str,
    body: str,
    draft_id: str,
    revision: int = 0,
    payload_hash: str = "",
) -> DraftDTO:
    """Assemble a ``DraftDTO`` from generated (subject, body) + envelope.

    The draft node generates ``(subject, body)`` via ``generate_body`` and then
    binds the draft to a job and recipient here. ``payload_hash`` is optional
    (the caller computes it via the draft node's per-draft hash); when empty the
    field is omitted to keep the DTO faithful to what the caller actually knows.
    """
    dto: DraftDTO = DraftDTO(
        id=draft_id,
        job_external_id=job_external_id,
        recipient=recipient,
        subject=subject,
        body=body,
        revision=revision,
    )
    if payload_hash:
        dto["payload_hash"] = payload_hash
    return dto


def draft_dto_to_reply_draft(
    draft: DraftDTO,
    *,
    message_id: UUID,
    thread_id: UUID,
    account_id: UUID,
    application_id: UUID | None = None,
    status: DraftStatus = DraftStatus.DRAFT,
) -> ReplyDraft:
    """Promote a ``DraftDTO`` to a domain ``ReplyDraft`` at the send boundary.

    The DTO carries no envelope (``message_id`` / ``thread_id`` / ``account_id``);
    those UUIDs are supplied by the caller. v1 has no real Gmail thread, so the
    send boundary passes placeholder UUIDs — this function NEVER invents them and
    NEVER changes ``to_address`` / ``subject`` / ``body`` (recipient/target
    changes must re-enter policy via a fresh proposal, plan v0.4 §2.5).

    M4 invariant preserved: this produces a stored draft only; it does not call
    any Gmail Send/Compose API.

    Raises ``ValueError`` if the draft carries no parseable UUID ``id`` — the
    draft node always mints one, so a missing id is a caller bug.
    """
    raw_id = draft.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        raise ValueError("draft dto must carry a UUID id to promote to ReplyDraft")
    return ReplyDraft(
        id=UUID(raw_id),
        message_id=message_id,
        thread_id=thread_id,
        account_id=account_id,
        application_id=application_id,
        status=status,
        to_address=draft.get("recipient", ""),
        subject=draft.get("subject", ""),
        body_text=draft.get("body", ""),
        payload_hash=draft.get("payload_hash", ""),
    )
