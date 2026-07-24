"""Thin LangGraph node wrappers for CareerOps (plan v0.4 §3 Stage 1 + Stage 2).

Each node is a thin adapter between domain code (adapters / application
services / kernel) and the LangGraph state. Nodes MUST:

- Read from ``state`` only via ``state.get(...)`` (TypedDict ``total=False``).
- Return a plain ``dict`` update. Never mutate state in place.
- Convert domain dataclasses to JSON-safe DTOs at the boundary via
  ``orchestration.conversions`` (the ONLY place those translations happen).

v1 wiring:

- ``crawl`` accepts an injected ``Crawler`` callable so tests can pass fixtures.
  The real boundary is ``AdapterCrawler`` (``http_fetcher`` + ``JobSourceAdapter``
  + optional ``DetailJobSourceAdapter``), which converts via
  ``raw_job_record_to_dto`` with the fetch layer's provenance.
- ``extract`` accepts an injected ``ContactExtractor`` callable; the real path
  is ``build_file_contact_extractor`` wrapping ``contact_extraction``.
- ``filter`` delegates to the pure ``filter_jobs`` (remote / direction / region
  / salary / description-coverage) and records rejected reasons on ``errors``.
- ``match`` injects ``DisabledModelAdapter`` so v1 never makes a real model
  call. The output is advisory and never a positive match (ADR 0006).
- ``send`` runs ``kernel.execute`` against a ``FakeProvider``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from careerops.adapters.http_fetcher import FetchedResponse
from careerops.adapters.job_sources import (
    DetailJobSourceAdapter,
    JobSourceAdapter,
    RawJobRecord,
)
from careerops.application.contact_extraction import extract_from_file
from careerops.application.email_drafting import generate_body
from careerops.application.llm_matching import (
    JobMatchResult,
    LLMJobMatcher,
    SkillProfile,
    build_skill_profile,
)
from careerops.application.side_effect_kernel import SideEffectKernel
from careerops.model_gateway.base import StructuredModelClient
from careerops.orchestration.conversions import (
    JobProvenance,
    build_draft_dto,
    job_match_result_to_dto,
    raw_job_record_to_dto,
    skill_profile_to_dto,
)
from careerops.orchestration.filter_node import FilterCriteria, filter_jobs
from careerops.orchestration.state import (
    CareerOpsState,
    ContactDTO,
    DraftDTO,
    EditedDraftPayload,
    ErrorDTO,
    JobMatchDTO,
    RawJobDTO,
    SendReceiptDTO,
    SkillProfileDTO,
)

__all__ = [
    "AdapterCrawler",
    "ContactExtractor",
    "Crawler",
    "HttpFetcher",
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


class HttpFetcher(Protocol):
    """Callable subset of ``adapters.http_fetcher`` used by ``AdapterCrawler``."""

    def __call__(self, url: str) -> FetchedResponse: ...


def _parse_body(body: str) -> object:
    """Parse a fetch body into the form its adapter expects.

    JSON-based adapters (Greenhouse / Lever / Ashby) take a parsed dict/list;
    text-based adapters (JsonLd / Sitemap / StaticHtml) take the raw string. We
    attempt JSON first and fall back to the string so one boundary serves both
    families without each adapter having to re-parse.
    """
    stripped = body.lstrip()
    if stripped and stripped[0] in "{[":
        from json import loads

        try:
            return loads(body)
        except (JSONDecodeError, ValueError):
            return body
    return body


# ---------------------------------------------------------------------------
# Real crawl boundary: http_fetcher + JobSourceAdapter (+ optional detail)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdapterCrawler:
    """``Crawler`` backed by ``http_fetcher`` + a ``JobSourceAdapter``.

    For each list URL the crawler fetches the body, runs the list adapter, and
    converts each ``RawJobRecord`` to a ``RawJobDTO`` attaching the fetch
    provenance (``source_url`` / ``fetched_at`` / ``response_hash`` /
    ``parser_version``). When a ``detail_adapter`` + ``detail_url_for`` are
    supplied AND a list record has no description, the crawler fetches the
    per-job detail endpoint and lets the detail parser fill the JD body; the
    detail response's provenance then overrides the list provenance (the
    description is authoritative for what we store, plan v0.4 §2.3).

    The injected ``fetcher`` is the ONLY I/O seam: production wires
    ``http_fetcher.fetch``; tests pass a deterministic fake. The node itself
    performs no I/O.
    """

    adapter: JobSourceAdapter
    fetcher: HttpFetcher
    list_urls: tuple[str, ...]
    detail_adapter: DetailJobSourceAdapter | None = None
    detail_url_for: Callable[[RawJobRecord], str] | None = None

    def __call__(self) -> tuple[RawJobDTO, ...]:
        out: list[RawJobDTO] = []
        for url in self.list_urls:
            resp = self.fetcher(url)
            fetch_result = self.adapter.list_jobs(_parse_body(resp.body))
            list_prov = JobProvenance(
                source_url=resp.final_url or url,
                fetched_at=resp.fetched_at,
                response_hash=resp.response_hash,
                parser_version=self.adapter.parser_version,
            )
            for record in fetch_result.jobs:
                final_record = record
                prov = list_prov
                if (
                    self.detail_adapter is not None
                    and self.detail_url_for is not None
                    and not record.description
                ):
                    detail_url = self.detail_url_for(record)
                    dresp = self.fetcher(detail_url)
                    final_record = self.detail_adapter.fetch_job(
                        _parse_body(dresp.body),
                        source_url=dresp.final_url or detail_url,
                        fetched_at=dresp.fetched_at,
                    )
                    prov = JobProvenance(
                        source_url=dresp.final_url or detail_url,
                        fetched_at=dresp.fetched_at,
                        response_hash=dresp.response_hash,
                        parser_version=self.detail_adapter.parser_version,
                    )
                out.append(raw_job_record_to_dto(final_record, prov))
        return tuple(out)


def build_file_contact_extractor(snapshot_paths: tuple[Path, ...]) -> ContactExtractor:
    """Build a ``ContactExtractor`` from on-disk social-content snapshots.

    Wraps ``contact_extraction.extract_from_file`` (a pure file read + regex
    extraction). The raw dicts it returns already match the ``ContactDTO``
    shape (``email`` / ``platform`` / ``company_hint`` / ``post_type`` /
    ``context`` / ``source_file`` / ``publicly_listed`` / ``extracted_at``); we
    project them into typed DTOs and de-duplicate by email. ``jobs`` is accepted
    to satisfy the ``ContactExtractor`` Protocol but is not used (v1 has no
    per-job contact association).
    """

    def extractor(jobs: tuple[RawJobDTO, ...]) -> tuple[ContactDTO, ...]:
        del jobs
        seen: set[str] = set()
        out: list[ContactDTO] = []
        for path in snapshot_paths:
            for raw in extract_from_file(path):
                email = str(raw.get("email", ""))
                if not email or email in seen:
                    continue
                seen.add(email)
                dto: ContactDTO = ContactDTO(
                    email=email,
                    platform=str(raw.get("platform", "")),
                    company_hint=str(raw.get("company_hint", "")),
                    context=str(raw.get("context", "")),
                    source_file=str(raw.get("source_file", "")),
                    publicly_listed=bool(raw.get("publicly_listed", True)),
                )
                post_type = raw.get("post_type")
                if isinstance(post_type, str):
                    dto["post_type"] = post_type
                extracted_at = raw.get("extracted_at")
                if isinstance(extracted_at, str):
                    dto["extracted_at"] = extracted_at
                out.append(dto)
        return tuple(out)

    return extractor


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------


def crawl_node(state: CareerOpsState, *, crawler: Crawler) -> dict[str, Any]:
    """Fetch raw job records via the injected crawler.

    Production wires ``AdapterCrawler`` (``http_fetcher + JobSourceAdapter``)
    behind the callable; tests inject a deterministic tuple. The node does no
    I/O of its own so it remains a pure adapter for the graph.
    """
    del state
    jobs = crawler()
    return {"raw_job_records": jobs}


# ---------------------------------------------------------------------------
# extract contacts
# ---------------------------------------------------------------------------


def extract_contacts_node(state: CareerOpsState, *, extractor: ContactExtractor) -> dict[str, Any]:
    """Extract recruiting contacts from the crawled jobs' raw_data."""
    jobs = state.get("filtered_jobs") or state.get("raw_job_records") or ()
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
    dto: SkillProfileDTO = skill_profile_to_dto(profile)
    return {"resume_text": resume_text, "skill_profile": dto}


# ---------------------------------------------------------------------------
# filter (delegates to pure filter_jobs)
# ---------------------------------------------------------------------------


def filter_node(
    state: CareerOpsState,
    *,
    criteria: FilterCriteria | None = None,
) -> dict[str, Any]:
    """Deterministic filter via ``filter_jobs``.

    Drops jobs that fail the description-coverage / remote / direction / region
    / salary gates. The kept tuple OVERWRITES ``raw_job_records`` (filter is the
    canonical job set after this point). Rejected jobs are appended to
    ``errors`` as ``ErrorDTO`` entries (node="filter") so the reason is visible
    downstream without leaking JD content.

    This is intentionally not a match decision (ADR 0006); the advisory match
    node runs after it.
    """
    jobs = state.get("filtered_jobs") or state.get("raw_job_records") or ()
    result = filter_jobs(jobs, criteria=criteria)
    errors = tuple(
        ErrorDTO(
            node="filter",
            error_type=rejected.reason,
            message=f"{rejected.external_id}:{rejected.title}",
        )
        for rejected in result.rejected
    )
    # Overwrite raw_job_records with the kept set; append rejection reasons.
    return {"filtered_jobs": result.kept, "errors": errors}


def dedup_node(state: CareerOpsState) -> dict[str, Any]:
    """Semantic deduplication via SimHash on description text.

    Drops jobs within ``DedupCriteria.threshold`` Hamming distance of a
    previously seen description (first occurrence kept). Duplicate reasons
    are appended to ``errors`` so the review gate can surface them.
    """
    from careerops.orchestration.dedup import DedupCriteria, dedup_jobs

    jobs = state.get("filtered_jobs") or state.get("raw_job_records") or ()
    result = dedup_jobs(jobs, criteria=DedupCriteria())
    errors = tuple(
        ErrorDTO(
            node="dedup",
            error_type="semantic_duplicate",
            message=f"{dup.external_id}:{dup.title} (distance={dup.distance})",
        )
        for dup in result.duplicates
    )
    return {"filtered_jobs": result.kept, "errors": errors}


# ---------------------------------------------------------------------------
# match
# ---------------------------------------------------------------------------


def _job_to_match_input(job: RawJobDTO) -> dict[str, Any]:
    """Render a RawJobDTO into the dict shape ``LLMJobMatcher.match_job`` expects."""
    raw = job.get("raw_data")
    company = ""
    if isinstance(raw, dict):
        company_value = raw.get("company", "")
        if isinstance(company_value, str):
            company = company_value
    return {
        "title": job.get("title", ""),
        "company": company,
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "raw_data": dict(raw) if isinstance(raw, dict) else {},
    }


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
    jobs = state.get("filtered_jobs") or state.get("raw_job_records") or ()
    results: list[JobMatchDTO] = []
    for job in jobs:
        result: JobMatchResult = matcher.match_job(_job_to_match_input(job), profile)
        results.append(job_match_result_to_dto(job.get("external_id", ""), result))
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

    jobs = state.get("filtered_jobs") or state.get("raw_job_records") or ()
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
        draft = build_draft_dto(
            job_external_id=job.get("external_id", ""),
            recipient=recipient,
            subject=subject,
            body=body,
            draft_id=str(uuid4()),
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
    send_callback: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Execute the side effect for the approved intent.

    v1: ``pending_intent_id`` was written to state by ``review_gate`` after a
    successful approve decision. The node calls ``kernel.execute`` and records
    the receipt. The kernel's own Gate 2 enforces APPROVED + non-expired; no
    additional check is performed here.

    When ``send_callback`` is provided, it is called with ``amount=1`` after a
    confirmed send (``IntentStatus.CONFIRMED``). The callback MUST be
    non-blocking and never raise; failures are swallowed so the main flow is
    unaffected.
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
    # Increment apply_submitted on confirmed send (non-blocking, never raises).
    if outcome.status.value == "confirmed" and send_callback is not None:
        with suppress(Exception):
            send_callback(1)
    receipt_dto: SendReceiptDTO = {
        "intent_id": pending_intent_id,
        "approval_id": pending_approval_id,
        "provider": provider,
        "provider_resource_id": provider_resource_id,
        "reconciliation_key": reconciliation_key,
        "final_state": outcome.status.value,
    }
    return {"send_receipts": (receipt_dto,)}
