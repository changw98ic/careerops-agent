"""Canonical crawl-stack factory (real-autonomous-career-loop Phase 4).

Builds the production crawl components used by both the in-process API path
(``infrastructure/runtime.py``) and the Temporal worker
(``infrastructure/temporal/worker.py``):

* a single :class:`RealCrawlActivitySink` — the pure Tier 1 signal source
  (fetch + ``RecipeEngine.execute``), and
* when a model client is available, the Tier 2 ``CrawlAgent`` + LLM extractor
  that the bounded orchestrator drives.

Centralizing construction here is the ONE place where the Tier 2 ``CrawlAgent``
is built. PR #7 review round 2 separated ownership: the sink holds NO agent
reference (it is a pure Tier 1 signal source), and the agent is handed
directly to :class:`BoundedTier2Orchestrator` by the caller. The factory
therefore returns a :class:`CrawlStack` (sink + agent) so callers can wire the
agent into the orchestrator and own its lifecycle, rather than reaching back
into the sink for it.

When ``model_client`` is absent or disabled the agent slot is ``None`` and the
sink stays on the structured Tier 1 path (unchanged behavior).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from careerops.adapters.http_fetcher import FetchedResponse
from careerops.application.bounded_tier2 import Tier2Agent
from careerops.infrastructure.temporal.ego_browser_executor import EgoBrowserExecutor
from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink


class _Fetcher(Protocol):
    def __call__(self, url: str) -> FetchedResponse: ...


@dataclass(slots=True)
class CrawlStack:
    """The factory's bundle: the Tier 1 sink and the optional Tier 2 agent.

    ``agent`` is ``None`` when no model client is configured (structured-only
    deployment). Callers inject it into :class:`BoundedTier2Orchestrator` and
    own its ``close()`` lifecycle; the sink never touches it.
    """

    sink: RealCrawlActivitySink
    agent: Tier2Agent | None


def build_real_crawl_sink(
    engine: object,
    *,
    fetcher: _Fetcher,
    public_ats_fetcher: _Fetcher | None = None,
    model_client: object | None = None,
    browser_executor: EgoBrowserExecutor | None = None,
    browser_tool: object | None = None,
) -> CrawlStack:
    """Build the canonical crawl stack, wiring Tier 2 when a model is available.

    Args:
        engine: SQLAlchemy engine used by ``ingest_posting``.
        fetcher: HTTP fetch callable (the structured Tier 1 path).
        public_ats_fetcher: larger bounded-payload fetcher used only for
            confirmed Greenhouse, Lever, and Ashby JSON adapters.
        model_client: structured model client. When present and enabled, a
            ``CrawlAgent`` + ``LLMJobExtractor`` are built and returned in the
            ``CrawlStack.agent`` slot so the caller can inject it into the
            bounded orchestrator. ``None`` or a disabled client yields
            ``agent=None`` (structured-only sink).
        browser_executor: optional single-shot ego executor (defaults to a new
            ``EgoBrowserExecutor``).
        browser_tool: optional multi-step browser tool for the agent (defaults
            to a new ``PlaywrightTool``). Tests inject a fake here.

    Returns:
        A :class:`CrawlStack` (sink + agent). The sink never holds the agent;
        callers wire ``stack.agent`` into the orchestrator and close it.
    """
    ego = browser_executor or EgoBrowserExecutor()
    agent: Tier2Agent | None = None

    if model_client is not None and getattr(model_client, "is_enabled", False):
        # Lazy imports: keep the Tier 2 modules out of the import path of
        # callers that never configure a model (e.g. unit tests importing the
        # factory, or a structured-only deployment).
        from careerops.application.crawl_agent import CrawlAgent
        from careerops.application.llm_job_extraction import LLMJobExtractor
        from careerops.infrastructure.playwright_tool import PlaywrightTool

        tool = browser_tool or PlaywrightTool()
        extractor = LLMJobExtractor(model_client)  # type: ignore[arg-type]
        agent = CrawlAgent(tool, extractor, model_client=model_client)  # type: ignore[arg-type]

    sink = RealCrawlActivitySink(
        fetcher=fetcher,
        public_ats_fetcher=public_ats_fetcher,
        engine=engine,  # type: ignore[arg-type]
        browser_executor=ego,
    )
    return CrawlStack(sink=sink, agent=agent)
