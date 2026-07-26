"""Resume-registration application service (Section 3, tasks 3.3 / 3.4 / 3.5).

Owns the resume lifecycle above the Section-2 repositories:

1. **Content-addressed registration (3.3)**: validate media type + size, hash
   the bytes, dedupe against an existing identical-hash version for the same
   candidate (no second physical blob), and persist an immutable
   :class:`ResumeVersion` referencing the stored blob.
2. **Deterministic parse (3.4)**: run the existing
   :mod:`careerops.application.resume_analysis` extractor on text-extractable
   media types, persist ``parse_status`` (parsed/failed), and surface parse
   errors WITHOUT persisting unsupported raw artifacts (no evidence rows, no
   derived content from an unparseable file).
3. **Evidence extraction (3.5)**: from a parsed resume, store one
   :class:`EvidenceItem` per detected skill with a bounded ``source_span``,
   ``extractor_version``, ``confirmation_status = UNCONFIRMED``, a deterministic
   ``evidence_hash``, and a link back to the resume version. Re-extraction is
   idempotent via ``find_by_evidence_hash``.

Iron rules honored:

- **Server-side candidate ownership** (Iron Rule 1): every method takes the
  server-resolved ``candidate_id``.
- **Immutable versions** (Iron Rule 3): a resume with ``parse_status = FAILED``
  or ``confirmation_status != CONFIRMED`` is NOT eligible for a package
  (delegated to the repository's ``find_eligible_resumes`` gate).
- **Claim→evidence** (Iron Rule 4): extracted claims carry
  ``confirmation_status = UNCONFIRMED`` and a bounded ``source_span``; they are
  PROPOSALS until the user confirms them via :class:`EvidenceService`.
- **No silent fallback** (Iron Rule 2): the service depends on a
  :class:`StoragePort`; a missing/unready storage surfaces as 503 at the route.

The repository stays untouched in semantics: this service reuses the existing
``save_resume`` upsert to advance ``parse_status`` / ``confirmation_status`` and
adds one additive read (``list_resumes``) consumed only through the
:class:`ResumeRepositoryProtocol` seam below.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO, Protocol
from uuid import UUID, uuid4

from careerops.api.errors import InvalidStateError, NotFoundError
from careerops.application.ports.storage import (
    ContentClassification,
    ContentOwner,
    StoragePort,
)
from careerops.application.resume_analysis import detect_skills
from careerops.domain.applications import ConfirmationStatus, ResumeParseStatus, ResumeVersion
from careerops.domain.candidates import EvidenceItem, EvidenceKind

__all__ = [
    "PARSEABLE_RESUME_MEDIA_TYPES",
    "RESUME_EXTRACTOR_VERSION",
    "SUPPORTED_RESUME_MEDIA_TYPES",
    "ResumeRegistrationRequest",
    "ResumeRegistrationResult",
    "ResumeRepositoryProtocol",
    "ResumeService",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bounded allowlist of media types the service accepts for registration. text/*
# types are deterministically parseable in Section 3; application/pdf is
# registerable (immutable version + content-addressed blob) but not yet
# text-extractable here, so it parses to FAILED with a clear reason rather than
# fabricating evidence from bytes the service cannot read.
SUPPORTED_RESUME_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "application/pdf",
    }
)
PARSEABLE_RESUME_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
    }
)

# Version tag for the deterministic skill extractor. Bumped when
# ``resume_analysis.SKILL_CATEGORIES`` changes so a re-parse on the same resume
# can be detected and the extracted evidence rows re-derived cleanly.
RESUME_EXTRACTOR_VERSION = "resume-analysis-v1"

# Resumes are accepted user content retained for the long term. The
# content-addressed store enforces size via ``max_object_bytes``; the service
# pre-checks the size so an oversize upload gets a clear error instead of a
# StorageError mid-write.
RESUME_OWNER_RESOURCE_TYPE = "resume_version"
RESUME_CONTENT_CLASSIFICATION = ContentClassification.ACCEPTED_ATTACHMENT
_RESUME_RETENTION_DAYS = 365

# Bounded source-span window extracted around each detected skill (chars).
# The DB caps source_span at 8192; we stay well under it.
_SOURCE_SPAN_WINDOW = 320
_SOURCE_SPAN_MAX = 8192


# ---------------------------------------------------------------------------
# Repository + storage seams
# ---------------------------------------------------------------------------


class ResumeRepositoryProtocol(Protocol):
    """Repository seam consumed by :class:`ResumeService`.

    Satisfied by :class:`PostgresApplicationRepository` and
    :class:`InMemoryApplicationRepository`. ``list_resumes`` is the one
    additive read this stage introduces; every other method already exists on
    the Section-2 repository.
    """

    def find_resume_by_content_hash(
        self, candidate_id: UUID, content_hash: str
    ) -> ResumeVersion | None: ...

    def find_resume_by_id(self, candidate_id: UUID, version_id: UUID) -> ResumeVersion | None: ...

    def find_latest_version(self, candidate_id: UUID) -> ResumeVersion | None: ...

    def find_eligible_resumes(
        self, candidate_id: UUID, *, limit: int = 50
    ) -> list[ResumeVersion]: ...

    def list_resumes(self, candidate_id: UUID, *, limit: int = 50) -> list[ResumeVersion]: ...

    def save_resume(self, version: ResumeVersion) -> None: ...


class EvidenceWriteRepositoryProtocol(Protocol):
    """The evidence-store slice :class:`ResumeService` needs for extraction.

    Satisfied by :class:`PostgresEvidenceRepository` and
    :class:`InMemoryEvidenceRepository`.
    """

    def find_by_evidence_hash(
        self, candidate_id: UUID, evidence_hash: str
    ) -> EvidenceItem | None: ...

    def store(self, item: EvidenceItem) -> EvidenceItem: ...


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResumeRegistrationRequest:
    """Input for :meth:`ResumeService.register`.

    ``content`` is the raw resume bytes the route read from the upload.
    ``media_type`` is validated against :data:`SUPPORTED_RESUME_MEDIA_TYPES`.
    """

    content: bytes
    media_type: str
    target_type: str = "general"
    source_reference: str = ""


@dataclass(frozen=True, slots=True)
class ResumeRegistrationResult:
    """Outcome of a registration.

    ``deduplicated`` is True when an identical-hash resume already existed for
    the candidate and was returned without storing a second blob or re-parsing.
    ``parse_status`` reports the terminal parse state; ``parse_error`` carries
    a human-readable reason when parsing failed (and is empty otherwise).
    """

    resume: ResumeVersion
    deduplicated: bool
    parse_status: ResumeParseStatus
    parse_error: str
    extracted_evidence: tuple[EvidenceItem, ...]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ResumeService:
    """Application service for registering, parsing, and extracting resumes."""

    def __init__(
        self,
        resume_repository: ResumeRepositoryProtocol,
        evidence_repository: EvidenceWriteRepositoryProtocol,
        storage: StoragePort,
        *,
        max_bytes: int,
    ) -> None:
        self._resumes = resume_repository
        self._evidence = evidence_repository
        self._storage = storage
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        """Configured per-upload byte cap (read-only view for route guards)."""
        return self._max_bytes

    # -- reads --------------------------------------------------------------

    def list_versions(self, candidate_id: UUID, *, limit: int = 50) -> list[ResumeVersion]:
        """Return the candidate's resume versions, newest version_number first."""
        return self._resumes.list_resumes(candidate_id, limit=limit)

    def get_version(self, candidate_id: UUID, version_id: UUID) -> ResumeVersion:
        """Return one resume version, scoped by candidate ownership (404 otherwise)."""
        version = self._resumes.find_resume_by_id(candidate_id, version_id)
        if version is None:
            raise NotFoundError("resume version not found for candidate")
        return version

    def list_eligible(self, candidate_id: UUID, *, limit: int = 50) -> list[ResumeVersion]:
        """Parsed + confirmed resumes — the only ones eligible for packages.

        Iron Rule 3: a resume with ``parse_status = FAILED`` or
        ``confirmation_status != CONFIRMED`` MUST NOT be eligible. The
        repository enforces both; this method is the single read path a
        package service consumes.
        """
        return self._resumes.find_eligible_resumes(candidate_id, limit=limit)

    # -- writes -------------------------------------------------------------

    def register(
        self,
        candidate_id: UUID,
        request: ResumeRegistrationRequest,
        *,
        now: datetime | None = None,
    ) -> ResumeRegistrationResult:
        """Register a resume: validate, content-address, dedupe, parse, extract.

        Raises :class:`InvalidStateError` (409) for an unsupported media type
        or an oversize upload — the caller never reaches the content-addressed
        store with bytes the service refuses to own.
        """
        media_type = request.media_type.strip().lower()
        _validate_media_type(media_type)
        _validate_size(len(request.content), self._max_bytes)

        content_hash = hashlib.sha256(request.content).hexdigest()

        # Content-addressed dedupe (task 3.3): identical hash for the same
        # candidate returns the existing version — no second blob, no re-parse.
        existing = self._resumes.find_resume_by_content_hash(candidate_id, content_hash)
        if existing is not None:
            return ResumeRegistrationResult(
                resume=existing,
                deduplicated=True,
                parse_status=existing.parse_status,
                parse_error=_failure_reason(existing),
                # Previously extracted evidence stays linked to the resume via
                # resume_version_id; we do not re-extract on a dedupe hit.
                extracted_evidence=(),
            )

        # Persist the blob (content-addressed: the store itself never writes a
        # second physical file for the same digest). The owner resource id is
        # the resume version id we are about to create, generated upfront so
        # the blob metadata and the resume row reference the same identity.
        resume_id = uuid4()
        occurred_at = now if now is not None else datetime.now(tz=UTC)
        stored = self._storage.put(
            _as_binary_stream(request.content),
            media_type=media_type,
            classification=RESUME_CONTENT_CLASSIFICATION,
            owner=ContentOwner(
                resource_type=RESUME_OWNER_RESOURCE_TYPE,
                resource_id=resume_id,
            ),
            retention_until=occurred_at + timedelta(days=_RESUME_RETENTION_DAYS),
            expected_sha256=content_hash,
        )

        # Deterministic parse + evidence extraction BEFORE persisting the
        # terminal status, so the resume row lands with its final lifecycle
        # state. Parse failure does NOT raise — it records parse_status=FAILED
        # with a clear reason and extracts no evidence (task 3.4: "expose parse
        # errors without persisting unsupported raw artifacts").
        parse_status, parse_error, extracted = self._parse_and_extract(
            candidate_id=candidate_id,
            resume_id=resume_id,
            media_type=media_type,
            content=request.content,
            content_hash=content_hash,
            file_reference=stored.object_key,
            now=occurred_at,
        )

        version_number = self._next_version_number(candidate_id)
        resume = ResumeVersion(
            id=resume_id,
            candidate_id=candidate_id,
            version_number=version_number,
            file_reference=stored.object_key,
            content_hash=content_hash,
            target_type=request.target_type,
            human_confirmed=False,
            parse_status=parse_status,
            confirmation_status=ConfirmationStatus.UNCONFIRMED,
            source_reference=request.source_reference,
            parsed_at=occurred_at if parse_status is ResumeParseStatus.PARSED else None,
            confirmed_at=None,
            created_at=occurred_at,
        )
        self._resumes.save_resume(resume)
        return ResumeRegistrationResult(
            resume=resume,
            deduplicated=False,
            parse_status=parse_status,
            parse_error=parse_error,
            extracted_evidence=extracted,
        )

    def confirm_content(
        self,
        candidate_id: UUID,
        version_id: UUID,
        *,
        actor_id: str = "",
        now: datetime | None = None,
    ) -> ResumeVersion:
        """Mark a parsed resume's extracted content CONFIRMED by the user.

        Only ``parse_status = PARSED`` AND ``confirmation_status = CONFIRMED``
        resumes are package-eligible; confirming an unparseable resume is
        rejected with :class:`InvalidStateError` so a user cannot mark a
        FAILED parse as trusted (spec: "Unconfirmed resume cannot be
        submitted").
        """
        existing = self.get_version(candidate_id, version_id)
        if existing.parse_status is not ResumeParseStatus.PARSED:
            raise InvalidStateError(
                "resume cannot be confirmed before parsing succeeds "
                f"(current parse_status={existing.parse_status.value})"
            )
        if existing.confirmation_status is ConfirmationStatus.CONFIRMED:
            return existing  # idempotent
        occurred_at = now if now is not None else datetime.now(tz=UTC)
        confirmed = _rebuild_resume(
            existing, confirmation_status=ConfirmationStatus.CONFIRMED, confirmed_at=occurred_at
        )
        self._resumes.save_resume(confirmed)
        return self.get_version(candidate_id, version_id)

    # -- internals ----------------------------------------------------------

    def _next_version_number(self, candidate_id: UUID) -> int:
        latest = self._resumes.find_latest_version(candidate_id)
        if latest is None:
            return 1
        return latest.version_number + 1

    def _parse_and_extract(
        self,
        *,
        candidate_id: UUID,
        resume_id: UUID,
        media_type: str,
        content: bytes,
        content_hash: str,
        file_reference: str,
        now: datetime,
    ) -> tuple[ResumeParseStatus, str, tuple[EvidenceItem, ...]]:
        """Run the deterministic extractor and persist evidence on success.

        Returns ``(parse_status, parse_error, extracted_evidence)``. On any
        failure (non-text media, decode error, storage error) the status is
        ``FAILED`` with a bounded reason and NO evidence rows are written.
        """
        if media_type not in PARSEABLE_RESUME_MEDIA_TYPES:
            # Registerable but not text-extractable in Section 3. Be honest:
            # do not derive evidence from bytes the service cannot read.
            return (
                ResumeParseStatus.FAILED,
                f"text extraction is not wired for media type '{media_type}'",
                (),
            )

        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            return (
                ResumeParseStatus.FAILED,
                f"resume is not valid UTF-8 text: {error.reason}",
                (),
            )

        try:
            detected = detect_skills(text)
        except Exception as error:  # deterministic extractor must never crash registration
            return (
                ResumeParseStatus.FAILED,
                f"deterministic parse raised: {type(error).__name__}: {error}",
                (),
            )

        extracted = self._extract_evidence(
            candidate_id=candidate_id,
            resume_id=resume_id,
            text=text,
            detected=detected,
            now=now,
        )
        return (ResumeParseStatus.PARSED, "", extracted)

    def _extract_evidence(
        self,
        *,
        candidate_id: UUID,
        resume_id: UUID,
        text: str,
        detected: dict[str, list[str]],
        now: datetime,
    ) -> tuple[EvidenceItem, ...]:
        """Store one unconfirmed evidence item per detected skill (task 3.5).

        Idempotent: a re-parse of the same resume produces the same
        ``evidence_hash`` per claim and is skipped via
        :meth:`find_by_evidence_hash`. ``content_hash`` is set to the evidence
        hash so the DB-level idempotency key (candidate_id, repository,
        commit_sha, path, symbol, content_hash) stays unique per claim instead
        of collapsing every resume-derived row for a candidate onto one.
        """
        text_lower = text.lower()
        stored: list[EvidenceItem] = []
        for _category, skills in detected.items():
            for skill in skills:
                span = _bounded_source_span(text_lower, skill)
                evidence_hash = _evidence_hash(
                    candidate_id=candidate_id,
                    resume_id=resume_id,
                    kind=EvidenceKind.SKILL,
                    name=skill,
                    source_span=span,
                )
                if self._evidence.find_by_evidence_hash(candidate_id, evidence_hash) is not None:
                    continue  # idempotent re-extraction
                item = EvidenceItem(
                    id=uuid4(),
                    candidate_id=candidate_id,
                    kind=EvidenceKind.SKILL,
                    name=skill,
                    description="",
                    content_hash=evidence_hash,
                    extractor_version=RESUME_EXTRACTOR_VERSION,
                    source_span=span,
                    confirmation_status=ConfirmationStatus.UNCONFIRMED,
                    evidence_hash=evidence_hash,
                    resume_version_id=resume_id,
                    created_at=now,
                )
                stored.append(self._evidence.store(item))
        return tuple(stored)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_media_type(media_type: str) -> None:
    if media_type not in SUPPORTED_RESUME_MEDIA_TYPES:
        raise InvalidStateError(
            f"unsupported resume media type '{media_type}'; "
            f"supported: {sorted(SUPPORTED_RESUME_MEDIA_TYPES)}"
        )


def _validate_size(size: int, max_bytes: int) -> None:
    if size <= 0:
        raise InvalidStateError("resume content is empty")
    if size > max_bytes:
        raise InvalidStateError(
            f"resume size {size} bytes exceeds the configured limit of {max_bytes} bytes"
        )


def _as_binary_stream(content: bytes) -> BinaryIO:
    return io.BytesIO(content)


def _bounded_source_span(text_lower: str, skill: str) -> str:
    """Return a bounded snippet around the first occurrence of ``skill``.

    The skill was already matched by :func:`detect_skills`; this finds the
    first index of the skill in the lower-cased text and returns a window
    around it. If the skill cannot be located (e.g. regex boundary matched but
    plain find misses), the skill name alone is returned. Always capped to
    ``_SOURCE_SPAN_MAX`` so the DB check constraint never trips.
    """
    index = text_lower.find(skill)
    if index < 0:
        return skill[:_SOURCE_SPAN_MAX]
    half = _SOURCE_SPAN_WINDOW // 2
    start = max(0, index - half)
    end = min(len(text_lower), index + len(skill) + half)
    snippet = text_lower[start:end].strip()
    return snippet[:_SOURCE_SPAN_MAX]


def _evidence_hash(
    *,
    candidate_id: UUID,
    resume_id: UUID,
    kind: EvidenceKind,
    name: str,
    source_span: str,
) -> str:
    """Deterministic per-claim hash backing extraction idempotency."""
    payload = "|".join(
        [
            str(candidate_id),
            str(resume_id),
            kind.value,
            name,
            source_span,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rebuild_resume(
    existing: ResumeVersion,
    *,
    confirmation_status: ConfirmationStatus | None = None,
    confirmed_at: datetime | None = None,
    parse_status: ResumeParseStatus | None = None,
    parsed_at: datetime | None = None,
) -> ResumeVersion:
    """Rebuild the frozen ResumeVersion with the supplied lifecycle fields."""
    return ResumeVersion(
        id=existing.id,
        candidate_id=existing.candidate_id,
        version_number=existing.version_number,
        file_reference=existing.file_reference,
        content_hash=existing.content_hash,
        target_type=existing.target_type,
        human_confirmed=existing.human_confirmed,
        parse_status=parse_status if parse_status is not None else existing.parse_status,
        confirmation_status=(
            confirmation_status if confirmation_status is not None else existing.confirmation_status
        ),
        source_reference=existing.source_reference,
        parsed_at=parsed_at if parsed_at is not None else existing.parsed_at,
        confirmed_at=confirmed_at if confirmed_at is not None else existing.confirmed_at,
        created_at=existing.created_at,
    )


def _failure_reason(resume: ResumeVersion) -> str:
    """Surface the stored parse-failure reason on a dedupe hit (read path)."""
    if resume.parse_status is ResumeParseStatus.FAILED:
        return getattr(resume, "source_reference", "") or "prior parse failed"
    return ""
