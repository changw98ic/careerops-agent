"""M3 application service: recruiting contacts with evidence-backed provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from careerops.domain.contacts import (
    ContactAction,
    ContactConfidence,
    ContactSource,
    RecruitingContact,
    compute_allowed_actions,
)

# Valid sources that are accepted for contact creation
VALID_CONTACT_SOURCES: frozenset[ContactSource] = frozenset(
    {
        ContactSource.JOB_PAGE,
        ContactSource.CAREERS_PAGE,
        ContactSource.ATS_LISTING,
        ContactSource.OFFICIAL_RECRUITING_PAGE,
        ContactSource.ESTABLISHED_THREAD,
    }
)


@dataclass(frozen=True, slots=True)
class ContactCreateRequest:
    """Request to create a recruiting contact."""

    company_id: UUID
    email: str
    name: str = ""
    role: str = ""
    source: ContactSource = ContactSource.JOB_PAGE
    source_url: str = ""
    source_text: str = ""
    publicly_listed: bool = True
    domain_match: bool = True
    confidence: ContactConfidence = ContactConfidence.HIGH


class ContactRepository(Protocol):
    def find_by_email(self, company_id: UUID, email: str) -> RecruitingContact | None: ...

    def find_by_company(self, company_id: UUID) -> list[RecruitingContact]: ...

    def save(self, contact: RecruitingContact) -> None: ...


class ContactValidationError(Exception):
    """Raised when contact creation fails validation."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ContactService:
    """Service for creating and managing recruiting contacts.

    Enforces:
    - Evidence coverage = 100% (source_url always required)
    - Guessed emails = 0 (publicly_listed must be True)
    - Employee false positive = 0 (only recruiting contacts accepted)
    - Low confidence or domain mismatch -> display/review only
    """

    def __init__(self, repository: ContactRepository) -> None:
        self._repository = repository

    def create_contact(self, request: ContactCreateRequest, now: datetime) -> RecruitingContact:
        """Create a new recruiting contact with validation."""
        if not request.source_url:
            raise ContactValidationError("source_url is required (evidence coverage)")
        if not request.publicly_listed:
            raise ContactValidationError("only publicly listed contacts are accepted")
        if request.source not in VALID_CONTACT_SOURCES:
            raise ContactValidationError(f"invalid contact source: {request.source}")
        if not request.email or "@" not in request.email:
            raise ContactValidationError("valid email is required")

        # Idempotency: return existing if same company+email
        existing = self._repository.find_by_email(request.company_id, request.email)
        if existing is not None:
            return existing

        allowed_actions = compute_allowed_actions(
            confidence=request.confidence,
            domain_match=request.domain_match,
        )

        contact = RecruitingContact(
            id=uuid4(),
            company_id=request.company_id,
            email=request.email,
            name=request.name,
            role=request.role,
            source=request.source,
            source_url=request.source_url,
            source_text=request.source_text,
            publicly_listed=request.publicly_listed,
            domain_match=request.domain_match,
            confidence=request.confidence,
            allowed_actions=allowed_actions,
            verified_at=now,
            created_at=now,
        )
        self._repository.save(contact)
        return contact

    def get_contacts_for_company(self, company_id: UUID) -> list[RecruitingContact]:
        """List all contacts for a company."""
        return self._repository.find_by_company(company_id)

    def can_initiate_contact(self, contact: RecruitingContact) -> bool:
        """Check if contact can be used for initiating outreach."""
        return ContactAction.INITIATE_CONTACT in contact.allowed_actions
