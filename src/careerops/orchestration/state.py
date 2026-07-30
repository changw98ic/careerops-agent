"""CareerOps LangGraph orchestration state (plan v0.4 §2.1).

This is the FIRST ``TypedDict`` state in the repo. It stores ONLY JSON-safe DTOs
on the checkpoint:

- UUIDs are converted to ``str``.
- ``datetime`` values are stored as ISO-8601 aware UTC strings.
- enums are stored as their value strings.
- collections are ``tuple`` (never ``list``) so the state is hashable / immutable.

Domain dataclasses live only at node boundaries. Nodes return ``dict`` updates;
they never mutate state in place. ``thread_id`` lives in ``config["configurable"]``,
NOT in the state.

``StateAppender`` lets multiple nodes contribute to the tuple fields without
clobbering each other when LangGraph merges partial state updates.
"""

from __future__ import annotations

from typing import Annotated, TypedDict, cast

__all__ = [
    "AnnotatedContacts",
    "AnnotatedErrors",
    "AnnotatedJobs",
    "AnnotatedMatches",
    "AnnotatedReceipts",
    "CareerOpsState",
    "ContactDTO",
    "DraftDTO",
    "EditedDraftItem",
    "EditedDraftPayload",
    "ErrorDTO",
    "JobMatchDTO",
    "RawJobDTO",
    "SendReceiptDTO",
    "SkillProfileDTO",
    "StateAppender",
]


def StateAppender(left: tuple[object, ...] | None, right: object) -> tuple[object, ...]:
    """LangGraph reducer that concatenates tuple state fields.

    LangGraph treats any callable ``(left, right) -> combined`` annotated on a
    TypedDict field as a reducer. The default reducer replaces the value; for
    accumulator fields (raw jobs, contacts, matches, errors, send_receipts)
    we want each node's returned tuple to extend the previous state.
    ``tuple_a + tuple_b`` preserves order and is idempotent under re-run when
    upstream nodes return the same output (review_gate relies on this for
    resume).
    """
    base: tuple[object, ...] = () if left is None else left
    if right is None:
        return base
    if isinstance(right, tuple):
        right_items: tuple[object, ...] = tuple(cast("tuple[object, ...]", right))
        return (*base, *right_items)
    return (*base, right)


# ---------------------------------------------------------------------------
# JSON-safe DTOs
# ---------------------------------------------------------------------------


class RawJobDTO(TypedDict, total=False):
    """JSON-safe raw job record.

    Mirrors the minimum of ``adapters.job_sources.RawJobRecord`` plus the
    ``source_url`` / ``fetched_at`` / ``response_hash`` provenance written by
    ``http_fetcher.FetchedResponse``. ``raw_data`` is the parsed JSON object
    exactly as returned by the adapter (already JSON-safe).
    """

    external_id: str
    title: str
    location: str
    url: str
    description: str
    source_url: str
    fetched_at: str
    response_hash: str
    parser_version: str
    raw_data: dict[str, object]


class ContactDTO(TypedDict, total=False):
    """JSON-safe extracted recruiting contact."""

    email: str
    platform: str
    company_hint: str
    post_type: str | None
    context: str
    source_file: str
    publicly_listed: bool
    extracted_at: str


class SkillProfileDTO(TypedDict, total=False):
    """JSON-safe skill profile for the match node."""

    skills: tuple[str, ...]
    level: str
    years: str
    highlights: str


class JobMatchDTO(TypedDict, total=False):
    """JSON-safe match result.

    A v1 ``disabled`` model yields an empty ``error``/``recommendation="skip"``
    record; the orchestrator never treats a disabled-model output as a positive
    match (ADR 0006).
    """

    external_id: str
    company: str
    title: str
    match_score: int
    tier: str
    recommendation: str
    seniority_fit: str
    remote_compatible: bool | None
    matched_requirements: tuple[str, ...]
    gaps: tuple[str, ...]
    reasoning: str
    is_review_only: bool
    error: str


class DraftDTO(TypedDict, total=False):
    """JSON-safe application email draft bound to a job and recipient."""

    id: str
    job_external_id: str
    recipient: str
    subject: str
    body: str
    revision: int
    payload_hash: str


class SendReceiptDTO(TypedDict, total=False):
    """JSON-safe send receipt written by the send node."""

    intent_id: str
    approval_id: str
    provider: str
    provider_resource_id: str
    reconciliation_key: str
    final_state: str


class ErrorDTO(TypedDict, total=False):
    """JSON-safe error captured during a node execution."""

    node: str
    error_type: str
    message: str


class EditedDraftItem(TypedDict, total=False):
    """One reviewer edit entry: subject/body only (no recipient change)."""

    id: str
    subject: str
    body: str


class EditedDraftPayload(TypedDict, total=False):
    """Constrained edit payload accepted from a reviewer.

    A reviewer may only edit ``subject`` / ``body`` (and choose which draft by
    ``id``); recipient / target changes require a fresh proposal and re-enter
    policy. The orchestration layer validates this in ``parse_review_decision``.
    """

    drafts: tuple[EditedDraftItem, ...]


# ---------------------------------------------------------------------------
# Annotated tuple fields (reducers)
# ---------------------------------------------------------------------------

# ``Annotated[tuple[X, ...], StateAppender]`` declares a tuple field whose
# updates are merged via concatenation rather than replacement. ``StateAppender``
# is a bare callable (no parens): LangGraph invokes it as ``(left, right) -> combined``.
AnnotatedJobs = Annotated[tuple[RawJobDTO, ...], StateAppender]
AnnotatedContacts = Annotated[tuple[ContactDTO, ...], StateAppender]
AnnotatedMatches = Annotated[tuple[JobMatchDTO, ...], StateAppender]
AnnotatedReceipts = Annotated[tuple[SendReceiptDTO, ...], StateAppender]
AnnotatedErrors = Annotated[tuple[ErrorDTO, ...], StateAppender]


# ---------------------------------------------------------------------------
# CareerOpsState
# ---------------------------------------------------------------------------


class CareerOpsState(TypedDict, total=False):
    """Top-level Career orchestration state (plan v0.4 §2.1).

    Tuple fields use ``StateAppender`` so multiple nodes may append. Scalar
    fields use plain overwrite semantics. ``requested_for`` is the server-side
    user identity (immutable once set); ``thread_id`` is intentionally absent
    because it lives in ``config["configurable"]``.
    """

    requested_for: str
    raw_job_records: AnnotatedJobs
    filtered_jobs: tuple[RawJobDTO, ...]  # filter_node output: overwriting reducer
    contacts: AnnotatedContacts
    resume_text: str
    skill_profile: SkillProfileDTO | None
    matches: AnnotatedMatches
    drafts: tuple[DraftDTO, ...]
    review_revision: int
    pending_approval_id: str | None
    pending_intent_id: str | None
    approved_draft_ids: tuple[str, ...]
    edit_payload: EditedDraftPayload | None
    # Outcome of the autonomous A/B approval loop at review_gate
    # (real-autonomous-career-loop design D2): "approved" (B approved, sent
    # autonomously as AGENT) or "escalated" (left for human review, not sent).
    ab_outcome: str
    send_receipts: AnnotatedReceipts
    errors: AnnotatedErrors
