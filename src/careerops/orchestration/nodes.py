"""Thin LangGraph node wrappers for CareerOps (plan v0.4 §3 Stage 1).

Each node is a thin adapter between domain code (adapters / application
services / kernel) and the LangGraph state. Nodes MUST:

- Read from ``state`` only via ``state.get(...)`` (TypedDict ``total=False``).
- Return a plain ``dict`` update. Never mutate state in place.
- Convert domain dataclasses to JSON-safe DTOs at the boundary.

v1 simplifications (Stage 2 will fill out the rest):

- ``crawl`` accepts a pre-built tuple of ``RawJobDTO`` via a callable so tests
  can inject fixtures; the v1 production path is still the same callable, just
  populated by ``http_fetcher + adapter``.
- ``extract`` accepts a callable that returns ``ContactDTO`` tuples.
- ``filter`` is inlined (simple match_score threshold).
- ``match`` injects ``DisabledModelAdapter`` so v1 never makes a real model
  call. The output is advisory and never a positive match.
- ``send`` runs ``kernel.execute`` against a ``FakeProvider``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from careerops.application.email_drafting import generate_body
from careerops.application.llm_matching import (
    JobMatchResult,
    LLMJobMatcher,
    SkillProfile,
    build_skill_profile,
)
from careerops.application.side_effect_kernel import SideEffectKernel
from careerops.model_gateway.base import StructuredModelClient
from careerops.orchestration.state import (
    CareerOpsState,
    ContactDTO,
    DraftDTO,
    EditedDraftPayload,
    JobMatchDTO,
    RawJobDTO,
    SendReceiptDTO,
    SkillProfileDTO,
)

__all__ = [
    "ContactExtractor",
    "Crawler",
    "crawl_node",
    "draft_node",
    "extract_contacts_node",
    "filter_node",
    "match_node",
    "resume_node",
    "send_node",
]


# ---------------------------------------------------------------------------
# Injected collaborators (Protocols so tests can pass fakes)
# ---------------------------------------------------------------------------


class Crawler(Protocol):
    """Returns the batch of raw job DTOs for this run."""

    def __call__(self) -> tuple[RawJobDTO, ...]: ...


class ContactExtractor(Protocol):
    """Returns the batch of contact DTOs for this run."""

    def __call__(self, jobs: tuple[RawJobDTO, ...]) -> tuple[ContactDTO, ...]: ...


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------


def crawl_node(state: CareerOpsState, *, crawler: Crawler) -> dict[str, Any]:
    """Fetch raw job records via the injected crawler.

    Production wires ``http_fetcher + JobSourceAdapter`` behind the callable;
    tests inject a deterministic tuple. The node does no I/O of its own so it
    remains a pure adapter for the graph.
    """
    del state
    jobs = crawler()
    return {"raw_job_records": jobs}


# ---------------------------------------------------------------------------
# extract contacts
# ---------------------------------------------------------------------------


def extract_contacts_node(state: CareerOpsState, *, extractor: ContactExtractor) -> dict[str, Any]:
    """Extract recruiting contacts from the crawled jobs' raw_data."""
    jobs = state.get("raw_job_records") or ()
    contacts = extractor(jobs)
    return {"contacts": contacts}


# ---------------------------------------------------------------------------
# resume analysis
# ---------------------------------------------------------------------------


def resume_node(state: CareerOpsState, *, resume_text: str) -> dict[str, Any]:
    """Build a deterministic skill profile from the user's resume text.

    The profile drives the (v1 disabled) match node; ADR 0006 keeps model calls
    advisory so this deterministic foundation is always present.
    """
    profile: SkillProfile = build_skill_profile(resume_text)
    dto: SkillProfileDTO = {
        "skills": profile.skills,
        "level": profile.level,
        "years": profile.years,
        "highlights": profile.highlights,
    }
    return {"resume_text": resume_text, "skill_profile": dto}


# ---------------------------------------------------------------------------
# filter (inlined for v1)
# ---------------------------------------------------------------------------


def filter_node(state: CareerOpsState) -> dict[str, Any]:
    """Deterministic filter: drop jobs whose description is empty.

    Stage 2 will plug the real filter service here. v1 keeps the bar at
    "has a non-empty description" so the downstream match node has something
    to score. This is intentionally not a match decision (ADR 0006).
    """
    jobs = state.get("raw_job_records") or ()
    kept = tuple(j for j in jobs if (j.get("description") or "").strip())
    # Overwrite (not append): filter is the canonical job set after this point.
    return {"raw_job_records": kept}


# ---------------------------------------------------------------------------
# match
# ---------------------------------------------------------------------------


def _job_to_match_input(job: RawJobDTO) -> dict[str, Any]:
    """Render a RawJobDTO into the dict shape ``LLMJobMatcher.match_job`` expects."""
    return {
        "title": job.get("title", ""),
        "company": job.get("raw_data", {}).get("company", "")
        if isinstance(job.get("raw_data"), dict)
        else "",
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "raw_data": job.get("raw_data", {}),
    }


def _match_result_to_dto(job: RawJobDTO, result: JobMatchResult) -> JobMatchDTO:
    return JobMatchDTO(
        external_id=job.get("external_id", ""),
        company=result.company,
        title=result.title,
        match_score=result.match_score,
        tier=result.tier,
        recommendation=result.recommendation,
        seniority_fit=result.seniority_fit,
        remote_compatible=result.remote_compatible,
        matched_requirements=result.matched_requirements,
        gaps=result.gaps,
        reasoning=result.reasoning,
        is_review_only=result.is_review_only,
        error=result.error,
    )


def match_node(
    state: CareerOpsState,
    *,
    model_client: StructuredModelClient,
) -> dict[str, Any]:
    """Score jobs against the skill profile.

    v1: ``DisabledModelAdapter`` is injected, so every result is an
    ``error="model provider disabled"`` advisory. The orchestrator never
    treats this as a positive match; the review_gate human is the fallback.
    """
    profile_dto = state.get("skill_profile")
    if profile_dto is None:
        return {"matches": ()}
    profile = SkillProfile(
        skills=tuple(profile_dto.get("skills", ())),
        level=profile_dto.get("level", "unknown"),
        years=profile_dto.get("years", ""),
        highlights=profile_dto.get("highlights", ""),
    )
    matcher = LLMJobMatcher(model_client)
    jobs = state.get("raw_job_records") or ()
    results: list[JobMatchDTO] = []
    for job in jobs:
        result = matcher.match_job(_job_to_match_input(job), profile)
        results.append(_match_result_to_dto(job, result))
    # Sort by score desc, stable on external_id for deterministic ordering.
    results.sort(key=lambda r: (-r.get("match_score", 0), r.get("external_id", "")))
    return {"matches": tuple(results)}


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------


def _apply_edit_payload(
    drafts: tuple[DraftDTO, ...], edit_payload: EditedDraftPayload | None
) -> tuple[DraftDTO, ...]:
    """Apply a constrained edit payload to the existing draft set.

    ``edit_payload`` is the ``EditedDraftPayload`` from review_gate; only
    ``id`` / ``subject`` / ``body`` may change. Drafts not mentioned in the
    payload pass through untouched.
    """
    if edit_payload is None:
        return drafts
    items = edit_payload.get("drafts") or ()
    if not items:
        return drafts
    by_id: dict[str, dict[str, object]] = {
        str(item.get("id", "")): {
            "id": str(item.get("id", "")),
            "subject": str(item.get("subject", "")),
            "body": str(item.get("body", "")),
        }
        for item in items
    }
    out: list[DraftDTO] = []
    for draft in drafts:
        draft_id = draft.get("id", "")
        edit = by_id.get(draft_id)
        if edit is None:
            out.append(draft)
            continue
        merged = dict(draft)
        if isinstance(edit.get("subject"), str):
            merged["subject"] = edit["subject"]  # type: ignore[typeddict-item]
        if isinstance(edit.get("body"), str):
            merged["body"] = edit["body"]  # type: ignore[typeddict-item]
        merged["revision"] = int(draft.get("revision", 0)) + 1
        out.append(DraftDTO(merged))  # type: ignore[typeddict-item]
    return tuple(out)


def _draft_hash(draft: DraftDTO) -> str:
    import hashlib
    import json

    payload = {
        "id": draft.get("id", ""),
        "recipient": draft.get("recipient", ""),
        "subject": draft.get("subject", ""),
        "body": draft.get("body", ""),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def draft_node(state: CareerOpsState) -> dict[str, Any]:
    """Generate application email drafts for each match.

    v1: drafts one email per crawled job (filtered set). The recipient comes
    from the extracted contacts (first publicly_listed email per job's
    platform family). When no contact is available, the draft is recorded
    with an empty recipient and the review_gate will reject at policy time
    (target not allowlisted).

    On re-entry after edit (``edit_payload`` present), the existing drafts are
    updated in place and ``edit_payload`` is consumed (set to ``None``).
    """
    edit_payload = state.get("edit_payload")
    existing = state.get("drafts")
    if existing is not None:
        # Edit loop: apply edits and consume the payload.
        updated = _apply_edit_payload(existing, edit_payload)
        return {"drafts": updated, "edit_payload": None}

    jobs = state.get("raw_job_records") or ()
    contacts = state.get("contacts") or ()
    resume_text = state.get("resume_text", "")

    # Index contacts by email; v1 has no per-job association, so any
    # publicly_listed contact is a candidate. Stage 2 will tighten this.
    public_emails_list: list[str] = []
    for c in contacts:
        if c.get("publicly_listed") and isinstance(c.get("email"), str):
            public_emails_list.append(str(c.get("email", "")))
    public_emails = tuple(public_emails_list)

    drafts: list[DraftDTO] = []
    for job in jobs:
        subject, body = generate_body(
            {
                "title": job.get("title", ""),
                "company": "",
                "location": job.get("location", ""),
                "url": job.get("url", ""),
                "raw_data": job.get("raw_data", {}),
            },
            resume_text,
        )
        recipient = public_emails[0] if public_emails else ""
        draft = DraftDTO(
            id=str(uuid4()),
            job_external_id=job.get("external_id", ""),
            recipient=recipient,
            subject=subject,
            body=body,
            revision=0,
        )
        draft["payload_hash"] = _draft_hash(draft)
        drafts.append(draft)
    return {"drafts": tuple(drafts), "review_revision": state.get("review_revision", 0)}


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def send_node(
    state: CareerOpsState,
    *,
    kernel: SideEffectKernel,
) -> dict[str, Any]:
    """Execute the side effect for the approved intent.

    v1: ``pending_intent_id`` was written to state by ``review_gate`` after a
    successful approve decision. The node calls ``kernel.execute`` and records
    the receipt. The kernel's own Gate 2 enforces APPROVED + non-expired; no
    additional check is performed here.
    """
    pending_intent_id = state.get("pending_intent_id")
    if not pending_intent_id:
        return {"send_receipts": ()}
    pending_approval_id = state.get("pending_approval_id") or ""
    now = datetime.now(tz=UTC)
    outcome = kernel.execute(UUID(pending_intent_id), now=now)
    replay = kernel.replay(UUID(pending_intent_id))
    provider = "unknown"
    provider_resource_id = ""
    reconciliation_key = ""
    if replay.receipts:
        latest = replay.receipts[-1]
        provider = latest.provider
        provider_resource_id = latest.provider_resource_id
        reconciliation_key = latest.reconciliation_key
    receipt_dto: SendReceiptDTO = {
        "intent_id": str(pending_intent_id),
        "approval_id": pending_approval_id,
        "provider": provider,
        "provider_resource_id": provider_resource_id,
        "reconciliation_key": reconciliation_key,
        "final_state": outcome.status.value,
    }
    return {"send_receipts": (receipt_dto,)}
