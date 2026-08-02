"""Real ``CrawlActivitySink`` backed by http_fetcher + RecipeEngine.

Implements the ``CrawlActivitySink`` protocol from ``m1_activities.py`` by
fetching the source URL via ``http_fetcher.fetch`` and running the matching
recipe through :class:`careerops.recipes.engine.RecipeEngine.execute`. Each
``RawJobRecord`` the engine emits is mapped to a ``CrawledPostingRecord``
with the fetch provenance attached.

Phase C (Task 11) removed the hard-coded adapter classes and the
``_ADAPTER_REGISTRY`` dict; the registry is now exactly what
:func:`build_recipe_registry` returns from ``vendor/crawl-recipes/``.

``ingest_posting`` writes to ``job_postings`` + ``job_posting_versions``
with dedup by ``source_id + external_id`` (posting) and
``job_posting_id + content_hash`` (version).

Section 5 extends the sink with ``crawl_source_with_signals`` which returns
a ``CrawlSourceResult`` carrying postings plus HTTP status, body prefix, and
parse-drift signals for the backoff policy (task 5.7).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from careerops.adapters.http_fetcher import FetchedResponse, fetch
from careerops.adapters.job_sources import (
    JobSourceAdapter,
    RawJobRecord,
)
from careerops.application.job_ingestion import JobIngestionService
from careerops.infrastructure.database.postgres_job_repo import PostgresJobReadRepository
from careerops.infrastructure.database.schema import companies, job_sources
from careerops.infrastructure.temporal.ego_browser_executor import EgoBrowserExecutor
from careerops.recipes.engine import RecipeEngine
from careerops.recipes.loader import load_manifest
from careerops.workflows.m1_contracts import (
    CrawledPostingRecord,
    CrawlJobSourceInput,
)

FetcherFn = Callable[[str], FetchedResponse]


class CrawlAgentProtocol(Protocol):
    def crawl(
        self, source_url: str, *, source_type: str = ""
    ) -> list[RawJobRecord]: ...

# Expected fields that every adapter should produce in structured_data.
# If these are missing and zero jobs were found, it signals parse drift.
_EXPECTED_POSTING_FIELDS = ("title",)


@dataclasses.dataclass(frozen=True, slots=True)
class CrawlSourceResult:
    """Extended result from ``crawl_source_with_signals``.

    Carries the postings plus HTTP-level and parse-level signals the backoff
    policy (task 5.7) needs to evaluate 403/429/CAPTCHA/parse-drift.
    The existing ``crawl_source`` method is unchanged — callers that do not
    need signals continue to use it.
    """

    postings: tuple[CrawledPostingRecord, ...]
    status_code: int = 0
    body_prefix: str = ""
    expected_fields_missing: tuple[str, ...] = ()


# Location of the declarative crawl-recipe catalog shipped in-tree.
#
# ``m1_crawl_sink.py`` lives at ``<repo>/src/careerops/infrastructure/temporal/``,
# so the repo root is ``parents[4]`` (parents[0]=temporal, [1]=infrastructure,
# [2]=careerops, [3]=src, [4]=repo root). The catalog lives at
# ``<repo>/vendor/crawl-recipes/`` and is populated by the recipe-authoring
# tasks (Task 8/9 onwards).
DEFAULT_RECIPES_DIR = Path(__file__).resolve().parents[4] / "vendor" / "crawl-recipes"


# Phase C live-rollout gate. Task 11 removed every hard-coded adapter class
# (``GreenhouseAdapter`` / ``LeverAdapter`` / ``AshbyAdapter`` /
# ``JsonLdAdapter`` / ``SitemapAdapter`` / ``StaticHtmlAdapter``) along with
# the ``_ADAPTER_REGISTRY`` dict that hosted them, so the crawl sink is now
# fully recipe-driven: :class:`RealCrawlActivitySink` populates
# ``self._adapters`` from :func:`build_recipe_registry` and dispatches every
# request through ``RecipeEngine.execute``.
#
# The gate is retained as an emergency stop: flipping it to ``False`` makes
# :func:`build_recipe_registry` return ``{}`` so the sink reports the source
# type as unsupported (Tier 2 fallback if an agent is wired, otherwise an
# empty result) without touching the recipe catalog. That keeps the
# rollback lever code-only rather than requiring a recipe revert.
RECIPES_LIVE: bool = True


def build_recipe_registry(recipes_dir: Path | None = None) -> dict[str, RecipeEngine]:
    """Build ``{source_type: RecipeEngine}`` from a recipe manifest.

    Missing ``manifest.json`` → ``{}`` (no error). This keeps the sink usable
    in environments that ship without the vendor catalog (CI sandboxes, unit
    tests, stripped containers).

    Gated by :data:`RECIPES_LIVE` (Phase C default ``True``): while ``False``,
    this returns ``{}`` unconditionally so the live crawl sink reports every
    source type as unsupported. See :data:`RECIPES_LIVE` for the rationale.

    Each loaded :class:`Recipe` becomes a :class:`RecipeEngine` keyed by its
    ``source_type``. There is no longer a legacy adapter registry to merge
    over: ``RealCrawlActivitySink.__init__`` consumes this dict verbatim.
    """
    # Emergency-stop gate (see :data:`RECIPES_LIVE`). Phase C removed the
    # legacy adapters entirely, so disabling recipes here leaves the sink
    # with no structured adapters — every request then falls through to the
    # Tier 2 agent (when wired) or returns an empty result.
    if not RECIPES_LIVE:
        return {}
    catalog = recipes_dir or DEFAULT_RECIPES_DIR
    if not (catalog / "manifest.json").exists():
        return {}
    return {recipe.source_type: RecipeEngine(recipe) for recipe in load_manifest(catalog)}


# Default provenance/parser_version when a raw record carries no explicit tag.
# Real records from the agent/extractor carry ``api-capture`` or
# ``llm-extraction``; this is only a defensive fallback.
_DEFAULT_PROVENANCE = "llm-extraction"


def _to_crawled_posting(
    raw: RawJobRecord,
    source_id: UUID | str,
    source_url: str,
    fetched_at: datetime,
    *,
    parser_version: str | None = None,
) -> CrawledPostingRecord:
    """Map a crawled :class:`RawJobRecord` to the sink's :class:`CrawledPostingRecord`.

    This is the single shared mapper for the Tier 2 path (real-autonomous-
    career-loop Phase 4): both the injected ``CrawlAgent`` results and any
    direct LLM extraction flow through it, so provenance is carried consistently
    into ``parser_version`` — the column ``ingest_posting`` writes to
    ``job_posting_versions``.

    ``parser_version`` defaults to the record's ``provenance`` (``api-capture``
    for captured job APIs, ``llm-extraction`` for model-extracted pages), which
    is what distinguishes Tier 2 postings from structured Tier 1 adapter output.
    """
    return CrawledPostingRecord(
        source_id=str(source_id),
        external_id=raw.external_id,
        canonical_url=raw.url or source_url,
        source_url=source_url,
        structured_data={
            "title": raw.title,
            "location": raw.location,
            "description": raw.description,
            "apply_url": raw.url,
        },
        parser_version=parser_version or raw.provenance or _DEFAULT_PROVENANCE,
        fetched_at=fetched_at.isoformat(),
    )


@dataclasses.dataclass(slots=True)
class CrawlCounters:
    """Per-source ingest counters for the canonical Tier 2 ingest path.

    Mirrors the Section-5 run counters at single-source granularity:
    ``discovered`` for new postings, ``updated`` for new versions of existing
    postings, ``failed`` for ingest errors. Used by :func:`ingest_crawled_records`
    so the same mapping + ingest + counting logic is shared between the
    execution service and direct callers (no parallel ingest implementation).
    """

    discovered: int = 0
    updated: int = 0
    failed: int = 0
    canonical_job_ids: set[UUID] = dataclasses.field(
        default_factory=lambda: set[UUID]()
    )


async def ingest_crawled_records(
    sink: RealCrawlActivitySink,
    records: list[RawJobRecord],
    *,
    source_id: UUID | str,
    source_url: str,
    crawl_run_id: UUID | None = None,
    plan_version_id: UUID | None = None,
    fetched_at: datetime | None = None,
) -> CrawlCounters:
    """Map + ingest crawled records through the canonical ``ingest_posting`` path.

    Folds the former ``CrawlAgentService.crawl_and_ingest`` responsibility into
    the crawl sink module: every record is mapped via ``_to_crawled_posting``
    (preserving ``llm-extraction`` / ``api-capture`` provenance) and ingested
    with the run/plan-version ids, accumulating :class:`CrawlCounters` from the
    idempotent ``is_new_posting`` / ``is_new_version`` flags. This is the same
    path :class:`CrawlExecutionService` uses, so there is one ingest contract.
    """
    counters = CrawlCounters()
    now = fetched_at or datetime.now(tz=UTC)
    for raw in records:
        posting = _to_crawled_posting(raw, source_id, source_url, now)
        try:
            result = await sink.ingest_posting(
                posting,
                crawl_run_id=crawl_run_id,
                plan_version_id=plan_version_id,
            )
        except Exception:
            counters.failed += 1
            continue
        if result.get("is_new_posting"):
            counters.discovered += 1
        elif result.get("is_new_version"):
            counters.updated += 1
        canonical_job_id = result.get("canonical_job_id")
        if canonical_job_id and (
            result.get("is_new_posting") or result.get("is_new_version")
        ):
            counters.canonical_job_ids.add(UUID(str(canonical_job_id)))
    return counters


class RealCrawlActivitySink:
    """Activity-side crawl adapter backed by http_fetcher + adapters.

    For each ``CrawlJobSourceInput``, fetches the ``base_url``, runs the
    list adapter matching ``source_type``, and returns the crawled postings.

    ``ingest_posting`` writes rows to ``job_postings`` and
    ``job_posting_versions`` with idempotent upserts (ON CONFLICT DO NOTHING).
    """

    def __init__(
        self,
        adapters: dict[str, JobSourceAdapter] | None = None,
        *,
        fetcher: FetcherFn | None = None,
        engine: Engine | None = None,
        browser_executor: EgoBrowserExecutor | None = None,
        agent: CrawlAgentProtocol | None = None,
        public_ats_fetcher: FetcherFn | None = None,
    ) -> None:
        # When the caller injects ``adapters`` (incl. an explicit ``{}``) we
        # use it verbatim — tests rely on the empty-dict case to build a sink
        # with no adapters. Otherwise the registry is exactly what
        # :func:`build_recipe_registry` returns: every entry is a
        # :class:`RecipeEngine` loaded from ``vendor/crawl-recipes/``. Phase C
        # removed the legacy hard-coded adapter dict that previously sat
        # underneath the recipe merge, so there is no longer a "merge over
        # legacy" step — the sink is fully recipe-driven.
        #
        # ``cast``: ``RecipeEngine`` exposes the same ``source_type`` /
        # ``parser_version`` / ``detect`` surface as ``JobSourceAdapter`` but
        # is keyed off ``execute(request, fetch)`` rather than
        # ``list_jobs(response_data)``. The dict is therefore NOT statically a
        # ``dict[str, JobSourceAdapter]``; the cast localises the type tension
        # here instead of widening every adapter lookup to ``Any``.
        if adapters is not None:
            self._adapters = adapters
        else:
            self._adapters = cast(
                "dict[str, JobSourceAdapter]", build_recipe_registry()
            )
        # Allow injecting a fake fetcher for tests.
        self._fetch: FetcherFn = fetcher or fetch
        self._public_ats_fetch = public_ats_fetcher
        self._engine = engine
        self._browser = browser_executor
        # Tier 2 multi-step agent (CrawlAgent). When set, ego sources are
        # delegated to it (navigate + API capture + LLM extraction) instead of
        # the single-shot structured capture. ``object`` typed to avoid an
        # import cycle with ``application.crawl_agent``; it is duck-typed as
        # ``.crawl(url) -> list[RawJobRecord]``.
        self._agent = agent

    @property
    def tier2_agent(self) -> CrawlAgentProtocol | None:
        """Return the canonical Tier 2 agent attached by the stack factory."""
        return self._agent

    def supports_source_type(self, source_type: str) -> bool:
        """Return whether Tier 1 has a structured adapter for the source."""
        return source_type in self._adapters

    def _fetch_for_request(self, request: CrawlJobSourceInput, url: str) -> FetchedResponse:
        if request.executor_mode == "http":
            if (
                request.source_type in {"greenhouse", "lever", "ashby"}
                and self._public_ats_fetch is not None
            ):
                return self._public_ats_fetch(url)
            return self._fetch(url)
        if request.executor_mode == "ego":
            if self._browser is None:
                raise RuntimeError("ego browser executor is not configured")
            return self._browser.fetch(url)
        raise RuntimeError("unsupported crawl executor mode")

    # ------------------------------------------------------------------
    # OFFICIAL meta-type sub-routing (PR #7 review fix, batch 1).
    #
    # ``CrawlSourceType.OFFICIAL`` is an enum meta-type — the DB stores
    # ``"official"`` and the manifest registers three concrete recipes
    # (``official_jsonld`` / ``official_sitemap`` / ``official_static``)
    # keyed by their actual list shape. A direct
    # ``self._adapters.get("official")`` lookup therefore returns ``None``
    # and would silently zero every OFFICIAL crawl.
    #
    # The correct dispatch is content-driven: fetch ``base_url`` once and
    # reuse the evaluator's own ``_extract_sitemap`` / ``_extract_json_ld``
    # (zero drift with the executor) to pick which ``official_*`` recipe
    # can parse the body. The chosen :class:`RecipeEngine` is then run
    # through its normal ``execute`` path — which re-fetches ``base_url``.
    # The double fetch is accepted (correctness > saving one fetch; the
    # probe payload is the list page itself, typically tens of KB).
    # ------------------------------------------------------------------

    def _resolve_official_engine(
        self, request: CrawlJobSourceInput
    ) -> RecipeEngine | None:
        """Probe ``base_url`` and return the matching ``official_*`` engine.

        Probe precedence (mirrors the legacy per-shape adapters):

        1. **sitemap** — body parses to ``<urlset>`` / ``<sitemapindex>`` and
           at least one ``<url><loc>`` survives the recipe's ``url_filter``.
        2. **json_ld** — body contains a ``<script type="application/ld+json">``
           block that survives the recipe's ``@['@type']=='JobPosting'`` filter.
        3. **static** — fallback for any other HTML.

        Returns ``None`` when no ``official_*`` engine is loaded (registry
        stripped / RECIPES_LIVE=False). Fetch failures propagate to the
        caller — same contract as the structured path.
        """
        sitemap_engine = self._adapters.get("official_sitemap")
        jsonld_engine = self._adapters.get("official_jsonld")
        static_engine = self._adapters.get("official_static")
        if (
            sitemap_engine is None
            and jsonld_engine is None
            and static_engine is None
        ):
            return None

        # Local import: evaluator pulls in ``extruct`` / ``selectolax``; keep
        # it out of the import path of callers that never hit an OFFICIAL
        # source (e.g. ego-only tests).
        from careerops.recipes.evaluator import _extract_json_ld, _extract_sitemap

        resp = self._fetch_for_request(request, request.base_url)
        body = resp.body or ""

        if sitemap_engine is not None:
            sitemap_recipe = cast("RecipeEngine", sitemap_engine)._recipe
            sitemap_extract = sitemap_recipe.steps[0].extract
            if _extract_sitemap(sitemap_extract, body):
                return cast("RecipeEngine", sitemap_engine)

        if jsonld_engine is not None:
            jsonld_recipe = cast("RecipeEngine", jsonld_engine)._recipe
            jsonld_extract = jsonld_recipe.steps[0].extract
            if _extract_json_ld(jsonld_extract, body):
                return cast("RecipeEngine", jsonld_engine)

        return cast("RecipeEngine", static_engine) if static_engine is not None else None

    def _resolve_engine_for_request(
        self, request: CrawlJobSourceInput
    ) -> RecipeEngine | None:
        """Pick the :class:`RecipeEngine` for ``request.source_type``.

        Non-OFFICIAL types index directly into the registry.
        ``"official"`` is the meta-type described in
        :meth:`_resolve_official_engine` and needs a content probe before a
        concrete ``official_*`` engine can be chosen.
        """
        if request.source_type == "official":
            return self._resolve_official_engine(request)
        adapter = self._adapters.get(request.source_type)
        return cast("RecipeEngine", adapter) if adapter is not None else None

    async def crawl_source(self, request: CrawlJobSourceInput) -> list[CrawledPostingRecord]:
        engine = self._resolve_engine_for_request(request)
        if engine is None:
            # No structured recipe covers this source type. If a Tier 2
            # agent is wired, delegate (http or ego — both can benefit):
            # the agent drives the browser itself, so executor_mode only
            # changes which fetcher the structured path would have used,
            # not whether the agent can run. Otherwise return empty; the
            # signals-bearing twin carries the parse-miss diagnostic.
            if self._agent is not None:
                agent_result = await self._crawl_via_agent(request)
                return list(agent_result.postings)
            return []

        result = await engine.execute(
            request, lambda url: self._fetch_for_request(request, url)
        )
        jobs = result.jobs
        source_url = result.source_url or request.base_url
        # RecipeEngine samples the first-step ``fetched_at``; fall back to now
        # only when the fetcher returned no timestamp.
        fetched_at_dt = result.fetched_at or datetime.now(tz=UTC)
        fetched_at = fetched_at_dt.isoformat()

        postings: list[CrawledPostingRecord] = []
        for record in jobs:
            # Flatten to dict[str, str] — Temporal JSON converter rejects
            # ``object`` values; stringify non-string scalars.
            raw: dict[str, str] = {}
            for k, v in record.raw_data.items():
                raw[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

            postings.append(
                CrawledPostingRecord(
                    source_id=request.source_id,
                    external_id=record.external_id,
                    canonical_url=record.url or request.base_url,
                    source_url=source_url,
                    structured_data={
                        "title": record.title or "",
                        "location": record.location or "",
                        "description": record.description,
                        "apply_url": record.url or "",
                        **raw,
                    },
                    parser_version=engine.parser_version,
                    fetched_at=fetched_at,
                )
            )

        # Tier 2 fallback: structured parsing yielded nothing (parse drift,
        # JS-rendered page, or an OFFICIAL probe whose recipe could not
        # extract rows). Hand the URL to the agent before declaring empty,
        # so a discovered dynamic source still gets a reasoned attempt.
        if not postings and self._agent is not None:
            agent_result = await self._crawl_via_agent(request)
            if agent_result.postings:
                return list(agent_result.postings)
        return postings

    async def crawl_source_with_signals(self, request: CrawlJobSourceInput) -> CrawlSourceResult:
        """Fetch and parse a source, returning postings plus backoff signals.

        Section 5 extension (task 5.7): wraps the same fetch + adapter logic
        as ``crawl_source`` but captures HTTP status code, body prefix (for
        CAPTCHA/login-wall detection), and expected-field parse-drift signals.

        Tier 2 fallback (PR #7 review fix, batch 1): the agent is no longer
        gated on ``executor_mode == 'ego'``. Any source type that ends up
        with no structured recipe (or whose OFFICIAL probe yields no rows)
        delegates to the injected ``CrawlAgent`` when one is wired —
        http sources benefit from agent fallback just as much as ego ones.
        When no agent is wired the result is NOT silently empty: the
        ``expected_fields_missing`` signal surfaces the miss so the backoff
        policy / operator can tell this apart from a successful zero-row
        crawl.
        """
        engine = self._resolve_engine_for_request(request)
        if engine is None:
            # No structured recipe covers this source type. Delegate to the
            # Tier 2 agent when wired (http or ego — both benefit); otherwise
            # surface the miss via ``expected_fields_missing`` so the empty
            # result is not mistaken for a successful zero-row crawl.
            if self._agent is not None:
                return await self._crawl_via_agent(request)
            return CrawlSourceResult(
                postings=(),
                expected_fields_missing=_EXPECTED_POSTING_FIELDS,
            )

        result = await engine.execute(
            request, lambda url: self._fetch_for_request(request, url)
        )
        jobs = result.jobs
        source_url = result.source_url or request.base_url
        status_code = result.status_code
        body_prefix = result.body_prefix
        fetched_at_dt = result.fetched_at or datetime.now(tz=UTC)
        fetched_at = fetched_at_dt.isoformat()

        postings: list[CrawledPostingRecord] = []
        for record in jobs:
            raw: dict[str, str] = {}
            for k, v in record.raw_data.items():
                raw[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

            postings.append(
                CrawledPostingRecord(
                    source_id=request.source_id,
                    external_id=record.external_id,
                    canonical_url=record.url or request.base_url,
                    source_url=source_url,
                    structured_data={
                        "title": record.title or "",
                        "location": record.location or "",
                        "description": record.description,
                        "apply_url": record.url or "",
                        **raw,
                    },
                    parser_version=engine.parser_version,
                    fetched_at=fetched_at,
                )
            )

        # Tier 2 fallback: structured parsing yielded nothing (parse drift
        # on an OFFICIAL probe, JS-rendered page, or a recipe that no longer
        # matches the live DOM). Delegate to the agent before declaring the
        # source empty.
        if not postings and self._agent is not None:
            return await self._crawl_via_agent(request)

        # Detect parse drift: expected fields missing across all postings.
        missing_fields: list[str] = []
        if postings:
            # Check the first posting for expected fields.
            sample = postings[0].structured_data
            for field_name in _EXPECTED_POSTING_FIELDS:
                if not sample.get(field_name):
                    missing_fields.append(field_name)
        elif jobs:
            # Jobs were found by the adapter but produced no postings — unusual.
            missing_fields.extend(_EXPECTED_POSTING_FIELDS)

        return CrawlSourceResult(
            postings=tuple(postings),
            status_code=status_code,
            body_prefix=body_prefix,
            expected_fields_missing=tuple(missing_fields),
        )

    async def _crawl_via_agent(self, request: CrawlJobSourceInput) -> CrawlSourceResult:
        """Tier 2 ego crawl: delegate to the injected ``CrawlAgent``.

        The agent drives the browser (navigate, capture network, LLM extraction)
        and returns ``RawJobRecord`` objects. Each is mapped through
        :func:`_to_crawled_posting`, which carries the record's provenance
        (``api-capture`` / ``llm-extraction``) into ``parser_version`` so the
        downstream ``ingest_posting`` writes it to ``job_posting_versions``.

        ``request.source_type`` is forwarded as an explicit hint so the agent
        can pick the matching Tier 2 skill playbook even when the URL alone is
        ambiguous (the agent still URL-matches as a fallback).
        """
        fetched_at = datetime.now(tz=UTC)
        if self._agent is None:
            return CrawlSourceResult(postings=())
        raw_records = self._agent.crawl(request.base_url, source_type=request.source_type)
        postings = tuple(
            _to_crawled_posting(raw, request.source_id, request.base_url, fetched_at)
            for raw in raw_records
        )
        return CrawlSourceResult(
            postings=postings,
            status_code=200 if postings else 0,
        )

    async def ingest_posting(
        self,
        record: CrawledPostingRecord,
        *,
        crawl_run_id: UUID | None = None,
        plan_version_id: UUID | None = None,
    ) -> dict[str, bool | str]:
        """Ingest a posting into the complete canonical job projection.

        Dedup rules:
        - Posting: unique on (source_id, external_id).
        - Version: unique on (job_posting_id, content_hash).
        - Canonical job: deterministic company/title/location fingerprint.

        Returns the workflow flags plus the canonical job and version ids so
        the crawl execution service can trigger matching and inbox projection.

        Section 5 provenance (tasks 5.1, 5.5, 5.6): when
        ``crawl_run_id`` / ``plan_version_id`` are supplied they are recorded
        on the version row so every ingested posting is traceable to the run
        and plan-version snapshot that produced it. Both are optional and
        nullable so non-run ingestion can still record a posting.
        """
        if self._engine is None:
            raise RuntimeError("ingest_posting requires an Engine")

        now = datetime.now(tz=UTC)
        fetched_at = datetime.fromisoformat(record.fetched_at) if record.fetched_at else now
        source_id = UUID(record.source_id)

        with self._engine.begin() as conn:
            source_row = conn.execute(
                sa.select(
                    job_sources.c.company_id,
                    companies.c.name.label("company_name"),
                )
                .join(companies, companies.c.id == job_sources.c.company_id)
                .where(job_sources.c.id == source_id)
            ).mappings().first()
        if source_row is None:
            raise RuntimeError(f"crawl source {source_id} has no company")

        service = JobIngestionService(PostgresJobReadRepository(self._engine))
        result = service.ingest_posting(
            source_id=source_id,
            external_id=record.external_id,
            canonical_url=record.canonical_url,
            structured_data=dict(record.structured_data),
            source_url=record.source_url,
            parser_version=record.parser_version,
            company_name=str(source_row["company_name"]),
            company_id=UUID(str(source_row["company_id"])),
            now=fetched_at,
            crawl_run_id=crawl_run_id,
            plan_version_id=plan_version_id,
        )
        return {
            "is_new_posting": result.is_new_posting,
            "is_new_version": result.is_new_version,
            "canonical_job_id": str(result.canonical_job_id),
            "posting_id": str(result.posting_id),
            "version_id": str(result.version_id),
        }
