"""Canonical crawl-stack factory (real-autonomous-career-loop Phase 4).

Builds the single ``RealCrawlActivitySink`` used by both the in-process API
path (``infrastructure/runtime.py``) and the Temporal worker
(``infrastructure/temporal/worker.py``). Centralizing construction here removes
the previous duplicate sink+service wiring and is the ONE place where the Tier 2
``CrawlAgent`` + ``LLMJobExtractor`` are attached to the production sink —
behind the model client — so the same implementation is injected everywhere.

When ``model_client`` is provided and enabled, the sink's ego branch gains the
multi-step agent (navigate + API capture + LLM extraction) as a parse-drift
fallback. Without a model client the sink stays on the structured Tier 1 path
(unchanged behavior). Either way the same ``RealCrawlActivitySink`` instance is
returned, so callers keep constructing ``CrawlExecutionService`` with their own
repositories.
"""

from __future__ import annotations

from typing import Protocol

from careerops.adapters.http_fetcher import FetchedResponse
from careerops.infrastructure.temporal.ego_browser_executor import EgoBrowserExecutor
from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink


class _Fetcher(Protocol):
    def __call__(self, url: str) -> FetchedResponse: ...


def build_real_crawl_sink(
    engine: object,
    *,
    fetcher: _Fetcher,
    model_client: object | None = None,
    browser_executor: EgoBrowserExecutor | None = None,
    browser_tool: object | None = None,
) -> RealCrawlActivitySink:
    """Build the canonical crawl sink, wiring Tier 2 when a model is available.

    Args:
        engine: SQLAlchemy engine used by ``ingest_posting``.
        fetcher: HTTP fetch callable (the structured Tier 1 path).
        model_client: structured model client. When present and enabled, a
            ``CrawlAgent`` + ``LLMJobExtractor`` are built and attached so the
            sink's ego branch can fall back to multi-step extraction. ``None``
            or a disabled client yields the structured-only sink.
        browser_executor: optional single-shot ego executor (defaults to a new
            ``EgoBrowserExecutor``).
        browser_tool: optional multi-step browser tool for the agent (defaults
            to a new ``EgoBrowserTool``). Tests inject a fake here.

    Returns:
        A ``RealCrawlActivitySink`` (with ``agent`` set when Tier 2 is wired).
    """
    ego = browser_executor or EgoBrowserExecutor()
    agent: object | None = None

    if model_client is not None and getattr(model_client, "is_enabled", False):
        # Lazy imports: keep the Tier 2 modules out of the import path of
        # callers that never configure a model (e.g. unit tests importing the
        # factory, or a structured-only deployment).
        from careerops.application.crawl_agent import CrawlAgent
        from careerops.application.llm_job_extraction import LLMJobExtractor
        from careerops.infrastructure.ego_tool import EgoBrowserTool

        tool = browser_tool or EgoBrowserTool()
        extractor = LLMJobExtractor(model_client)  # type: ignore[arg-type]
        agent = CrawlAgent(tool, extractor, model_client=model_client)  # type: ignore[arg-type]

    return RealCrawlActivitySink(
        fetcher=fetcher,
        engine=engine,  # type: ignore[arg-type]
        browser_executor=ego,
        agent=agent,
    )
