"""LangGraph graph builder for CareerOps (plan v0.4 §3 Stage 1).

``build_graph`` wires the 8 nodes defined in ``orchestration.nodes`` and the
``review_gate`` from ``orchestration.kernel_adapter`` into a single
``StateGraph``. ``review_gate`` is the only HITL boundary; its ``Command(goto=...)``
return routes to ``send`` / ``draft`` / ``END`` without a separate conditional
edge table.

Inputs are passed via dependency injection:

- ``crawler`` / ``extractor`` / ``resume_text`` / ``model_client``: data sources.
- ``kernel`` / ``review_mapping`` / ``capability_resolver``: the authorization
  chain used by ``review_gate``.
- ``checkpointer``: ``MemorySaver`` for v1; ``PostgresSaver`` is v1.1.
"""

# langgraph ships without bundled pyright stubs; mirror job_sources.py and
# suppress only the Unknown-family errors that the missing stubs introduce.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from careerops.application.side_effect_kernel import SideEffectKernel
from careerops.model_gateway.base import StructuredModelClient
from careerops.orchestration.kernel_adapter import (
    CapabilityResolver,
    review_gate,
)
from careerops.orchestration.mapping_store import ReviewMappingStore
from careerops.orchestration.nodes import (
    ContactExtractor,
    Crawler,
    crawl_node,
    dedup_node,
    draft_node,
    extract_contacts_node,
    filter_node,
    match_node,
    resume_node,
    send_node,
)
from careerops.orchestration.state import CareerOpsState

__all__ = ["CareerGraph", "build_graph"]


type CareerGraph = Any  # CompiledGraph typing varies across langgraph patch versions


def build_graph(
    *,
    crawler: Crawler,
    extractor: ContactExtractor,
    resume_text: str,
    model_client: StructuredModelClient,
    kernel: SideEffectKernel,
    review_mapping: ReviewMappingStore,
    capability_resolver: CapabilityResolver,
    checkpointer: BaseCheckpointSaver[Any],
    send_callback: Callable[[int], None] | None = None,
) -> CareerGraph:
    """Compile the CareerOps graph.

    ``review_gate`` is wrapped with ``functools.partial`` to inject
    ``kernel`` / ``review_mapping`` / ``capability_resolver``; the node still
    receives ``state`` and ``config`` from LangGraph at call time.

    The graph edges:

    - ``START -> crawl -> extract_contacts -> resume -> filter -> match -> draft -> review_gate``
    - ``review_gate`` returns ``Command(goto="send"|"draft"|END)``
    - ``send -> END``
    """
    graph: StateGraph[Any] = StateGraph(CareerOpsState)

    graph.add_node("crawl", partial(crawl_node, crawler=crawler))
    graph.add_node("extract_contacts", partial(extract_contacts_node, extractor=extractor))
    graph.add_node("resume", partial(resume_node, resume_text=resume_text))
    graph.add_node("filter", filter_node)
    graph.add_node("dedup", dedup_node)
    graph.add_node(
        "match",
        partial(
            match_node,
            model_client=model_client,
            capability_resolver=capability_resolver,
        ),
    )
    graph.add_node("draft", draft_node)
    graph.add_node(
        "review_gate",
        partial(
            review_gate,
            kernel=kernel,
            review_mapping=review_mapping,
            capability_resolver=capability_resolver,
        ),
    )
    graph.add_node("send", partial(send_node, kernel=kernel, send_callback=send_callback))

    graph.add_edge(START, "crawl")
    graph.add_edge("crawl", "extract_contacts")
    graph.add_edge("extract_contacts", "resume")
    graph.add_edge("resume", "filter")
    graph.add_edge("filter", "dedup")
    graph.add_edge("dedup", "match")
    graph.add_edge("match", "draft")
    graph.add_edge("draft", "review_gate")
    # ``review_gate``'s ``Command(goto=...)`` return routes dynamically; no
    # static edge from review_gate is added.
    graph.add_edge("send", END)

    return graph.compile(checkpointer=checkpointer)
