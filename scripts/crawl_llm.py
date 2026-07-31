#!/usr/bin/env python3
"""LLM crawler CLI: run the autonomous ReAct crawler end-to-end on one source.

This is the synchronous, single-user trigger for the LLM-driven crawl path
(real-autonomous-career-loop Phase 4 + 2.3/2.4). It avoids the Temporal
dependency entirely: it builds the same ``RuntimeResources`` the API uses,
resolves ONE configured source, and runs
``CrawlAgentService.crawl_and_ingest`` to completion:

    configured source -> PlaywrightTool navigates/searches/scrolls
                      -> LLM ReAct extraction (bounded schema, one repair)
                      -> ingest via the shared RealCrawlActivitySink
                         (tagged 'llm-extraction' provenance)
                      -> matching + inbox projection chain

Power-on prerequisite: the model provider must be configured (the stack is
gated on ``model_client.is_enabled`` in ``RuntimeResources``). If it is not,
this script exits non-zero with the exact environment variables to set.

Usage:
    uv run python scripts/crawl_llm.py --help
    uv run python scripts/crawl_llm.py --source-name aliyun
    uv run python scripts/crawl_llm.py --source-url https://careers.example.com
    uv run python scripts/crawl_llm.py --source-id <uuid> --max-pages 8
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from sqlalchemy import text


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="crawl_llm.py",
        description=(
            "Run the LLM-driven autonomous crawler on one configured source. "
            "Resolves a source by id / url / name (or the single registered "
            "source when none is given), crawls it via the ReAct browser loop, "
            "and ingests postings with 'llm-extraction' provenance."
        ),
    )
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--source-id",
        help="Resolve the source by its UUID (job_sources.id).",
    )
    selector.add_argument(
        "--source-url",
        help="Resolve the source whose base_url matches this URL.",
    )
    selector.add_argument(
        "--source-name",
        help="Resolve the source whose identifier matches this name.",
    )
    parser.add_argument(
        "--owner-id",
        help=(
            "Candidate id that owns the crawl (single-user default resolved "
            "from the candidates table when omitted)."
        ),
    )
    parser.add_argument(
        "--plan-version-id",
        help="Optional crawl plan version id stamped onto ingest provenance.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=5,
        help="Upper bound on pages/iterations the agent explores (default: 5).",
    )
    return parser.parse_args(argv)


def _url_matches(base_url: str, requested: str) -> bool:
    """Match a source base_url against the requested URL.

    Exact first, then one startswith the other, so trailing-slash and path
    differences do not block an otherwise-correct selection.
    """
    a = base_url.strip().rstrip("/")
    b = requested.strip().rstrip("/")
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _print_available(sources: list[object]) -> None:
    if not sources:
        print("  (no crawl sources registered)")
        return
    for src in sources:
        print(
            f"  - id={getattr(src, 'id')} name={getattr(src, 'source_identifier', '')!r} "
            f"url={getattr(src, 'base_url', '')!r} "
            f"enabled={getattr(src, 'enabled', False)}"
        )


def _resolve_owner(resources: object, override: str | None) -> UUID:
    """Resolve the single-user candidate id, honoring an explicit override."""
    if override:
        return UUID(override)
    engine = getattr(resources, "database")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM careerops.candidates ORDER BY created_at LIMIT 1")
        ).first()
    if row is None:
        raise SystemExit(
            "No candidate found in the database. Bootstrap the console first "
            "(create the single user) or pass --owner-id <candidate-uuid>."
        )
    value = row[0]
    return value if isinstance(value, UUID) else UUID(str(value))


def _resolve_source(
    resources: object,
    owner_id: UUID,
    *,
    source_id: str | None,
    source_url: str | None,
    source_name: str | None,
) -> object:
    repo = getattr(resources, "crawl_source_repo")
    sources = repo.list_for(owner_id, limit=200)

    if source_id:
        matches = [s for s in sources if str(getattr(s, "id")) == source_id]
        hint = f"no source with id={source_id}"
    elif source_url:
        matches = [s for s in sources if _url_matches(getattr(s, "base_url", ""), source_url)]
        hint = f"no source whose base_url matches {source_url!r}"
    elif source_name:
        needle = source_name.lower()
        matches = [
            s
            for s in sources
            if needle in str(getattr(s, "source_identifier", "")).lower()
        ]
        hint = f"no source whose name matches {source_name!r}"
    else:
        # Default: the single-user registry is expected to have one source.
        if len(sources) == 1:
            return sources[0]
        if not sources:
            raise SystemExit(
                "No crawl sources are registered. Register one (POST "
                "/api/v1/crawl-sources) or pass --source-url to crawl an "
                "ad-hoc URL once it is registered."
            )
        raise SystemExit(
            f"{len(sources)} sources are registered; specify one with "
            "--source-id / --source-url / --source-name. Available:"
        )

    if not matches:
        print(f"Error: {hint}.", file=sys.stderr)
        print("Available sources:", file=sys.stderr)
        _print_available(sources)
        raise SystemExit(2)
    if len(matches) > 1:
        print(f"Error: multiple sources matched; narrow the selector.", file=sys.stderr)
        _print_available(matches)
        raise SystemExit(2)
    return matches[0]


def _print_provider_help() -> None:
    print(
        "The LLM crawler is unavailable. The model provider is disabled or the\n"
        "Playwright browser could not launch. To power it on, set:\n"
        "  CAREEROPS_MODEL_PROVIDER=anthropic-compat   (or any non-disabled value)\n"
        "  CAREEROPS_MODEL_BASE_URL=<your Anthropic-compatible endpoint>\n"
        "  CAREEROPS_MODEL_API_KEY=<your api key>\n"
        "  CAREEROPS_MODEL_NAME=<model id>\n"
        "and install the browser once:  playwright install chromium\n"
        "(Check the startup log for any 'LLM crawler browser unavailable' warning.)",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Settings validation is actionable on its own: it names any missing env
    # var, so let it surface directly rather than masking it here.
    from careerops.config import get_settings
    from careerops.infrastructure.runtime import create_runtime_resources

    settings = get_settings()
    resources = create_runtime_resources(settings)

    service = getattr(resources, "crawl_agent_service", None)
    if service is None:
        _print_provider_help()
        return 1

    owner_id = _resolve_owner(resources, args.owner_id)
    source = _resolve_source(
        resources,
        owner_id,
        source_id=args.source_id,
        source_url=args.source_url,
        source_name=args.source_name,
    )
    source_id = getattr(source, "id")
    source_url = getattr(source, "base_url", "") or ""
    source_name = getattr(source, "source_identifier", "") or source_url
    plan_version_id = UUID(args.plan_version_id) if args.plan_version_id else None

    print(
        f"Crawling source {source_name!r}\n"
        f"  url={source_url}\n"
        f"  source_id={source_id} owner_id={owner_id} max_pages={args.max_pages}",
        flush=True,
    )

    async def _run() -> object:
        try:
            return await service.crawl_and_ingest(
                owner_id=owner_id,
                source_id=source_id,
                source_url=source_url,
                source_name=source_name,
                plan_version_id=plan_version_id,
                max_pages=args.max_pages,
            )
        finally:
            await resources.close()

    counters = asyncio.run(_run())
    discovered = getattr(counters, "discovered", 0)
    updated = getattr(counters, "updated", 0)
    closed = getattr(counters, "closed", 0)
    failed = getattr(counters, "failed", 0)
    print(
        f"\nDone: discovered={discovered} updated={updated} closed={closed} failed={failed}"
    )
    return 0 if failed == 0 and (discovered or updated) else 1


if __name__ == "__main__":
    raise SystemExit(main())
