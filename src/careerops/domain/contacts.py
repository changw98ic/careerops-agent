"""Domain models for M3: recruiting contacts with evidence-backed provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ContactSource(StrEnum):
    """Allowed sources for recruiting contacts."""

    JOB_PAGE = "job_page"
    CAREERS_PAGE = "careers_page"
    ATS_LISTING = "ats_listing"
    OFFICIAL_RECRUITING_PAGE = "official_recruiting_page"
    ESTABLISHED_THREAD = "established_thread"


class ContactConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ContactAction(StrEnum):
    """Actions allowed for a contact based on confidence and domain match."""

    DISPLAY = "display"
    REVIEW = "review"
    DRAFT_REPLY = "draft_reply"
    INITIATE_CONTACT = "initiate_contact"


@dataclass(frozen=True, slots=True)
class RecruitingContact:
    """A publicly listed recruiting contact with full provenance.

    Invariants:
    - source_url is always present (evidence coverage = 100%)
    - publicly_listed must be True for the contact to be usable
    - guessed emails are never stored (guessed count = 0)
    - domain_mismatch or low confidence restricts to display/review only
    """

    id: UUID
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
    allowed_actions: tuple[ContactAction, ...] = (ContactAction.DISPLAY,)
    verified_at: datetime | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.source_url:
            raise ValueError("source_url is required for contact evidence")
        if not self.publicly_listed:
            raise ValueError("only publicly listed contacts are accepted")


def compute_allowed_actions(
    confidence: ContactConfidence,
    domain_match: bool,
) -> tuple[ContactAction, ...]:
    """Determine allowed actions based on confidence and domain match.

    Low confidence or domain mismatch restricts to display/review only.
    """
    if confidence == ContactConfidence.LOW or not domain_match:
        return (ContactAction.DISPLAY, ContactAction.REVIEW)
    if confidence == ContactConfidence.MEDIUM:
        return (ContactAction.DISPLAY, ContactAction.REVIEW, ContactAction.DRAFT_REPLY)
    return (
        ContactAction.DISPLAY,
        ContactAction.REVIEW,
        ContactAction.DRAFT_REPLY,
        ContactAction.INITIATE_CONTACT,
    )
