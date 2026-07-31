"""Resume API routes (Section 3, tasks 3.3 / 3.4 / 3.5).

Additive routes under ``/api/v1/candidates/{candidate_id}/resumes`` backed by
:class:`ResumeService`. Registration accepts a multipart upload, validates
media type + size, stores the resume content-addressed, runs the deterministic
parse, and extracts unconfirmed evidence — all scoped to the candidate in the
URL path.

Iron Rule 1 + 2: candidate identity comes from the path parameter (never a
session or body field); service pulled via :func:`require_repository`
(missing → 503, never silent).
Iron Rule 3: only parsed + confirmed resumes surface from ``/eligible``.
"""

# Pydantic ``Field(default_factory=list)`` and the ``object``-typed response
# mappers produce ``reportUnknown*`` reports that obscure the real mapping
# logic; the existing applications router suppresses them the same way.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field

from careerops.api.capability_dependency import require_repository
from careerops.api.errors import PayloadTooLargeError
from careerops.application.evidence_service import EvidenceService
from careerops.application.resume_service import ResumeService

router = APIRouter(prefix="/api/v1/candidates/{candidate_id}/resumes", tags=["resumes"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ResumeVersionResponse(BaseModel):
    id: str
    candidate_id: str
    version_number: int
    file_reference: str
    content_hash: str
    target_type: str = "general"
    parse_status: str = "pending"
    parse_error: str = ""
    confirmation_status: str = "unconfirmed"
    source_reference: str = ""
    parsed_at: str | None = None
    confirmed_at: str | None = None
    created_at: str | None = None


class ResumeListResponse(BaseModel):
    items: list[ResumeVersionResponse] = Field(default_factory=list)
    total: int = 0


class ResumeRegisterResponse(BaseModel):
    resume: ResumeVersionResponse
    deduplicated: bool
    parse_status: str
    parse_error: str = ""
    extracted_evidence_count: int = 0


class EvidenceItemResponse(BaseModel):
    id: str
    kind: str
    name: str
    description: str = ""
    extractor_version: str = ""
    source_span: str = ""
    confirmation_status: str = "unconfirmed"
    evidence_hash: str = ""
    resume_version_id: str | None = None


class EvidenceListResponse(BaseModel):
    items: list[EvidenceItemResponse] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _resume_service(request: Request) -> ResumeService:
    return require_repository(request, "resume_service")  # type: ignore[return-value]


def _evidence_service(request: Request) -> EvidenceService:
    return require_repository(request, "evidence_service")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_resumes(
    candidate_id: UUID,
    service: Annotated[ResumeService, Depends(_resume_service)],
    limit: int = 50,
) -> ResumeListResponse:
    versions = service.list_versions(candidate_id, limit=limit)
    return ResumeListResponse(
        items=[_to_response(v) for v in versions],
        total=len(versions),
    )


@router.get("/eligible")
def list_eligible_resumes(
    candidate_id: UUID,
    service: Annotated[ResumeService, Depends(_resume_service)],
) -> ResumeListResponse:
    """Parsed + confirmed resumes — the only ones eligible for packages."""
    versions = service.list_eligible(candidate_id)
    return ResumeListResponse(
        items=[_to_response(v) for v in versions],
        total=len(versions),
    )


@router.get("/{version_id}")
def get_resume(
    version_id: UUID,
    candidate_id: UUID,
    service: Annotated[ResumeService, Depends(_resume_service)],
) -> ResumeVersionResponse:
    version = service.get_version(candidate_id, version_id)
    return _to_response(version)


@router.post("", status_code=201)
async def register_resume(
    candidate_id: UUID,
    request: Request,
    service: Annotated[ResumeService, Depends(_resume_service)],
    file: Annotated[UploadFile, File(description="Resume file upload")],
    target_type: Annotated[str, Form(description="Resume target type")] = "general",
    source_reference: Annotated[str, Form(description="Free-form source note")] = "",
) -> ResumeRegisterResponse:
    """Register a resume upload (content-addressed, deduped, parsed).

    Rejects unsupported media types with ``InvalidStateError`` (409). Rejects
    oversize uploads with ``PayloadTooLargeError`` (413) from an early
    Content-Length guard BEFORE the body is buffered into memory; the service's
    post-read size check stays as defense-in-depth.
    """
    # Early size guard: reject oversize from the Content-Length header before
    # we ever read the body into memory. Missing/invalid header falls through
    # to the authoritative post-read check in the service.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            declared_size = -1
        if declared_size > service.max_bytes:
            raise PayloadTooLargeError(
                f"resume upload declared size {declared_size} bytes exceeds "
                f"the configured limit of {service.max_bytes} bytes"
            )

    content = await file.read()
    request_obj = service.register(
        candidate_id,
        _registration_input(content, file.content_type, target_type, source_reference),
    )
    return ResumeRegisterResponse(
        resume=_to_response(request_obj.resume),
        deduplicated=request_obj.deduplicated,
        parse_status=request_obj.parse_status.value,
        parse_error=request_obj.parse_error,
        extracted_evidence_count=len(request_obj.extracted_evidence),
    )


@router.post("/{version_id}/confirm")
def confirm_resume_content(
    version_id: UUID,
    candidate_id: UUID,
    service: Annotated[ResumeService, Depends(_resume_service)],
) -> ResumeVersionResponse:
    """Mark a parsed resume's extracted content CONFIRMED by the user.

    Rejects an unparseable resume with ``InvalidStateError`` (409) so a FAILED
    parse can never be marked trusted (spec: "Unconfirmed resume cannot be
    submitted").
    """
    version = service.confirm_content(candidate_id, version_id)
    return _to_response(version)


@router.get("/{version_id}/evidence")
def list_resume_evidence(
    version_id: UUID,
    candidate_id: UUID,
    resume_service: Annotated[ResumeService, Depends(_resume_service)],
    evidence_service: Annotated[EvidenceService, Depends(_evidence_service)],
) -> EvidenceListResponse:
    """Return the evidence items extracted from one resume version.

    ``resume_version_id`` filtering is applied after the candidate-scoped read
    so the ownership scope stays the single source of truth.
    """
    # Verify ownership (404 if the resume belongs to another candidate).
    resume_service.get_version(candidate_id, version_id)
    items = [
        i
        for i in evidence_service.list_for_candidate(candidate_id)
        if i.resume_version_id == version_id
    ]
    return EvidenceListResponse(
        items=[_evidence_to_response(i) for i in items],
        total=len(items),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registration_input(
    content: bytes,
    content_type: str | None,
    target_type: str,
    source_reference: str,
):
    from careerops.application.resume_service import ResumeRegistrationRequest

    return ResumeRegistrationRequest(
        content=content,
        media_type=content_type or "",
        target_type=target_type,
        source_reference=source_reference,
    )


def _to_response(version: object) -> ResumeVersionResponse:
    parsed_at = version.parsed_at  # type: ignore[attr-defined]
    confirmed_at = version.confirmed_at  # type: ignore[attr-defined]
    created_at = version.created_at  # type: ignore[attr-defined]
    return ResumeVersionResponse(
        id=str(version.id),  # type: ignore[attr-defined]
        candidate_id=str(version.candidate_id),  # type: ignore[attr-defined]
        version_number=version.version_number,  # type: ignore[attr-defined]
        file_reference=version.file_reference,  # type: ignore[attr-defined]
        content_hash=version.content_hash,  # type: ignore[attr-defined]
        target_type=version.target_type,  # type: ignore[attr-defined]
        parse_status=version.parse_status.value,  # type: ignore[attr-defined]
        parse_error=getattr(version, "parse_error", ""),
        confirmation_status=version.confirmation_status.value,  # type: ignore[attr-defined]
        source_reference=version.source_reference,  # type: ignore[attr-defined]
        parsed_at=parsed_at.isoformat() if parsed_at else None,
        confirmed_at=confirmed_at.isoformat() if confirmed_at else None,
        created_at=created_at.isoformat() if created_at else None,
    )


def _evidence_to_response(item: object) -> EvidenceItemResponse:
    return EvidenceItemResponse(
        id=str(item.id),  # type: ignore[attr-defined]
        kind=item.kind.value,  # type: ignore[attr-defined]
        name=item.name,  # type: ignore[attr-defined]
        description=item.description,  # type: ignore[attr-defined]
        extractor_version=item.extractor_version,  # type: ignore[attr-defined]
        source_span=item.source_span,  # type: ignore[attr-defined]
        confirmation_status=item.confirmation_status.value,  # type: ignore[attr-defined]
        evidence_hash=item.evidence_hash,  # type: ignore[attr-defined]
        resume_version_id=(
            str(item.resume_version_id) if item.resume_version_id else None  # type: ignore[attr-defined]
        ),
    )
