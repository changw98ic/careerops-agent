"""CareerOps LangGraph orchestration layer (plan v0.4 Stage 1).

Public API surface for v1:

- ``CareerOpsState`` and the JSON-safe DTOs (``RawJobDTO`` / ``DraftDTO`` / ...)
- ``InMemoryReviewMappingStore`` for thread↔approval↔intent mapping
- ``review_gate`` and the parsing helpers (``parse_review_decision``)
- ``build_graph`` to compile the LangGraph graph
"""

from __future__ import annotations

from careerops.orchestration.capability_resolver import (
    CapabilityDecision,
    CapabilityKind,
)
from careerops.orchestration.graph import build_graph
from careerops.orchestration.kernel_adapter import (
    Capability,
    CapabilityResolver,
    ReviewDecision,
    ReviewDecisionError,
    drafts_payload_hash,
    parse_review_decision,
    review_gate,
    review_idempotency_key,
)
from careerops.orchestration.mapping_store import (
    InMemoryReviewMappingStore,
    MappingConflictError,
    MappingNotFoundError,
    MappingRecord,
    ReviewMappingStore,
)
from careerops.orchestration.nodes import (
    ContactExtractor,
    Crawler,
    crawl_node,
    draft_node,
    extract_contacts_node,
    filter_node,
    match_node,
    resume_node,
    send_node,
)
from careerops.orchestration.state import (
    CareerOpsState,
    ContactDTO,
    DraftDTO,
    EditedDraftItem,
    EditedDraftPayload,
    ErrorDTO,
    JobMatchDTO,
    RawJobDTO,
    SendReceiptDTO,
    SkillProfileDTO,
    StateAppender,
)

__all__ = [
    "Capability",
    "CapabilityDecision",
    "CapabilityKind",
    "CapabilityResolver",
    "CareerOpsState",
    "ContactDTO",
    "ContactExtractor",
    "Crawler",
    "DraftDTO",
    "EditedDraftItem",
    "EditedDraftPayload",
    "ErrorDTO",
    "InMemoryReviewMappingStore",
    "JobMatchDTO",
    "MappingConflictError",
    "MappingNotFoundError",
    "MappingRecord",
    "RawJobDTO",
    "ReviewDecision",
    "ReviewDecisionError",
    "ReviewMappingStore",
    "SendReceiptDTO",
    "SkillProfileDTO",
    "StateAppender",
    "build_graph",
    "crawl_node",
    "draft_node",
    "drafts_payload_hash",
    "extract_contacts_node",
    "filter_node",
    "match_node",
    "parse_review_decision",
    "resume_node",
    "review_gate",
    "review_idempotency_key",
    "send_node",
]
