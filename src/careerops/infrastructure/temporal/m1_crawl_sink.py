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
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from careerops.adapters.http_fetcher import FetchedResponse, fetch
from careerops.adapters.job_sources import RawJobRecord
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


# Expected fields that every adapter should produce in structured_data.
# If these are missing and zero jobs were found, it signals parse drift.
_EXPECTED_POSTING_FIELDS = ("title",)


@dataclasses.dataclass(frozen=True, slots=True)
class CrawlSourceResult:
    """Extended result from ``crawl_source_with_signals``.

    Carries the postings plus HTTP-level and parse-level signals the backoff
    policy (task 5.7) needs to evaluate 403/429/CAPTCHA/parse-drift, plus two
    Tier 1 provenance flags the admission gate reads:

    * ``had_recipe`` — ``True`` when ``crawl_source_with_signals`` actually ran
      a :class:`RecipeEngine.execute` (direct source_type lookup, OFFICIAL
      content probe, or ``detect()`` URL routing). This is the authoritative
      "did structured Tier 1 parsing run at all" signal the classifier and the
      Tier 2 admission gate use as positive job-source evidence — more accurate
      than a registry-key lookup, because ``detect()``-routed recipes sit at a
      source_type the registry does not key on.
    * ``recipe_fallback`` — the recipe's declared ``fallback`` policy
      (``"llm_skill"`` / ``"none"``). A recipe that explicitly opts out of
      escalation (``fallback: none``) must block Tier 2 even when the recipe
      ran, so an author can pin a source to Tier 1-only.
    """

    postings: tuple[CrawledPostingRecord, ...]
    status_code: int = 0
    body_prefix: str = ""
    expected_fields_missing: tuple[str, ...] = ()
    had_recipe: bool = False
    recipe_fallback: str = "llm_skill"


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
# The gate is retained as an emergency stop: flipping it to ``True`` makes
# :func:`build_recipe_registry` return ``{}`` so the sink reports every source
# type as unsupported (``had_recipe=False`` empty result) without touching the
# recipe catalog. That keeps the rollback lever code-only rather than requiring
# a recipe revert.
RECIPES_DISABLED: bool = False


def build_recipe_registry(recipes_dir: Path | None = None) -> dict[str, RecipeEngine]:
    """Build ``{source_type: RecipeEngine}`` from a recipe manifest.

    Missing ``manifest.json`` → ``{}`` (no error). This keeps the sink usable
    in environments that ship without the vendor catalog (CI sandboxes, unit
    tests, stripped containers).

    Gated by :data:`RECIPES_DISABLED` (Phase C default ``False``): when
    ``True``, this returns ``{}`` unconditionally so the live crawl sink
    reports every source type as unsupported. See :data:`RECIPES_DISABLED`
    for the rationale.

    Each loaded :class:`Recipe` becomes a :class:`RecipeEngine` keyed by its
    ``source_type``. There is no longer a legacy adapter registry to merge
    over: ``RealCrawlActivitySink.__init__`` consumes this dict verbatim.
    """
    # Emergency-stop gate (see :data:`RECIPES_DISABLED`). Phase C removed the
    # legacy adapters entirely, so disabling recipes here leaves the sink
    # with no structured adapters — every request then returns an empty
    # Tier 1 result (``had_recipe=False``), which the classifier tags
    # NOT_JOB_SOURCE and the admission gate refuses to escalate.
    if RECIPES_DISABLED:
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


def _records_to_postings(
    jobs: Sequence[RawJobRecord],
    request: CrawlJobSourceInput,
    source_url: str,
    fetched_at_iso: str,
    parser_version: str,
) -> list[CrawledPostingRecord]:
    """Map structured-recipe ``RawJobRecord`` rows into ``CrawledPostingRecord``s.

    Shared by :meth:`RealCrawlActivitySink.crawl_source` and
    :meth:`RealCrawlActivitySink.crawl_source_with_signals` so the Tier 1
    structured path has one flattening + mapping contract. ``raw_data`` is
    stringified (Temporal JSON converter rejects non-string scalars) and
    merged under the structured fields. Every text field defaults to ``""``
    so ``structured_data`` stays ``dict[str, str]`` — this also fixes the
    earlier style inconsistency where ``description`` lacked the ``or ""``
    guard that ``title`` / ``location`` already had.

    ``jobs`` is a :class:`Sequence` because :class:`RecipeEngine.execute`
    returns ``tuple[RawJobRecord, ...]``; the function only iterates, so a
    sequence is the honest (and widest-correct) input contract.

    Signals-only fields (``status_code`` / ``body_prefix`` /
    ``expected_fields_missing``) are not mapped here; the signals path
    populates them on :class:`CrawlSourceResult` separately.
    """
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
                    "description": record.description or "",
                    "apply_url": record.url or "",
                    **raw,
                },
                parser_version=parser_version,
                fetched_at=fetched_at_iso,
            )
        )
    return postings


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
    canonical_job_ids: set[UUID] = dataclasses.field(default_factory=lambda: set[UUID]())


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
        if canonical_job_id and (result.get("is_new_posting") or result.get("is_new_version")):
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
        adapters: dict[str, RecipeEngine] | None = None,
        *,
        fetcher: FetcherFn | None = None,
        engine: Engine | None = None,
        browser_executor: EgoBrowserExecutor | None = None,
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
        # The Tier 2 ``CrawlAgent`` is NOT held by the sink (PR #7 review
        # round 2): the sink is a pure Tier 1 signal source. Its only job is
        # fetch + ``RecipeEngine.execute`` and to surface signals; the
        # decision to escalate to Tier 2 lives in ``CrawlExecutionService``
        # and the bounded orchestrator. The agent is attached directly to the
        # ``BoundedTier2Orchestrator`` by the stack factory.
        if adapters is not None:
            self._adapters = adapters
        else:
            self._adapters = build_recipe_registry()
        # Allow injecting a fake fetcher for tests.
        self._fetch: FetcherFn = fetcher or fetch
        self._public_ats_fetch = public_ats_fetcher
        self._engine = engine
        self._browser = browser_executor

    def supports_source_type(self, source_type: str) -> bool:
        """Return whether Tier 1 has a structured recipe for the source.

        ``"official"`` is an enum meta-type: the DB stores ``"official"`` but
        the registry keys the three concrete shapes (``official_jsonld`` /
        ``official_sitemap`` / ``official_static``). A direct
        ``"official" in self._adapters`` lookup therefore returns ``False`` and
        would let the classifier mis-tag a clean 200-empty OFFICIAL crawl as
        ``NOT_JOB_SOURCE``. Recognise the meta-type so any OFFICIAL source
        with a loaded ``official_*`` recipe reports ``True``.
        """
        if source_type in self._adapters:
            return True
        return source_type == "official" and any(
            key.startswith("official_") for key in self._adapters
        )

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
    # ask each ``official_*`` engine whether its first-step extractor can
    # parse the body (:meth:`RecipeEngine.matches_body`). The chosen engine
    # is then run through its normal ``execute`` path — which re-fetches
    # ``base_url``. The double fetch is accepted (correctness > saving one
    # fetch; the probe payload is the list page itself, typically tens of KB).
    # ------------------------------------------------------------------

    def _resolve_official_engine(self, request: CrawlJobSourceInput) -> RecipeEngine | None:
        """Probe ``base_url`` and return the matching ``official_*`` engine.

        Probe precedence (mirrors the legacy per-shape adapters):

        1. **sitemap** — body parses to ``<urlset>`` / ``<sitemapindex>`` and
           at least one ``<url><loc>`` survives the recipe's ``url_filter``.
        2. **json_ld** — body contains a ``<script type="application/ld+json">``
           block that survives the recipe's ``@['@type']=='JobPosting'`` filter.
        3. **static** — fallback for any other HTML.

        Returns ``None`` when no ``official_*`` engine is loaded (registry
        stripped / RECIPES_DISABLED=True). Fetch failures propagate to the
        caller — same contract as the structured path.
        """
        sitemap_engine = self._adapters.get("official_sitemap")
        jsonld_engine = self._adapters.get("official_jsonld")
        static_engine = self._adapters.get("official_static")
        if sitemap_engine is None and jsonld_engine is None and static_engine is None:
            return None

        resp = self._fetch_for_request(request, request.base_url)
        body = resp.body or ""

        if sitemap_engine is not None and sitemap_engine.matches_body(body):
            return sitemap_engine
        if jsonld_engine is not None and jsonld_engine.matches_body(body):
            return jsonld_engine
        return static_engine

    def _resolve_engine_for_request(self, request: CrawlJobSourceInput) -> RecipeEngine | None:
        """Pick the :class:`RecipeEngine` for ``request.source_type``.

        Resolution order (PR #7 review round 2):

        1. ``"official"`` meta-type → content probe via
           :meth:`_resolve_official_engine`.
        2. Direct ``source_type`` lookup in the registry.
        3. ``detect()`` URL fallback: when the direct lookup misses, ask each
           loaded recipe whether ``base_url`` matches its :class:`Match` block
           (host suffix / URL patterns). A source whose ``source_type`` was
           mislabelled but whose URL matches a known recipe (e.g. a Greenhouse
           board URL stored under an unknown type) is therefore still parsed
           by Tier 1, and the run carries ``had_recipe=True`` as positive
           evidence for the admission gate. First hit wins; ``official_*``
           recipes declare no ``match_hosts`` so they never shadow an ATS.
        4. None — the source has no Tier 1 recipe. The sink returns an empty
           result with ``had_recipe=False`` so the classifier/admission gate
           decide escalation (an unknown source never silently enters Tier 2).
        """
        if request.source_type == "official":
            return self._resolve_official_engine(request)
        engine = self._adapters.get(request.source_type)
        if engine is not None:
            return engine
        for candidate in self._adapters.values():
            if candidate.detect(request.base_url):
                return candidate
        return None

    async def crawl_source(self, request: CrawlJobSourceInput) -> list[CrawledPostingRecord]:
        """Fetch + parse via the matching recipe; return postings (list-only).

        The bare contract is list-only (no signal channel). When no recipe
        covers the source, returns ``[]`` — the signals-bearing twin
        (:meth:`crawl_source_with_signals`) is the one that surfaces the miss
        and feeds the Tier 2 admission gate. This method performs NO Tier 2
        escalation: the sink is a pure Tier 1 signal source (PR #7 review
        round 2).
        """
        engine = self._resolve_engine_for_request(request)
        if engine is None:
            return []

        result = await engine.execute(request, lambda url: self._fetch_for_request(request, url))
        source_url = result.source_url or request.base_url
        fetched_at_iso = (result.fetched_at or datetime.now(tz=UTC)).isoformat()

        return _records_to_postings(
            result.jobs, request, source_url, fetched_at_iso, engine.parser_version
        )

    async def crawl_source_with_signals(self, request: CrawlJobSourceInput) -> CrawlSourceResult:
        """Fetch + parse a source, returning postings plus backoff + Tier 1 signals.

        Section 5 extension (task 5.7): wraps the same fetch + recipe logic as
        :meth:`crawl_source` but captures HTTP status code, body prefix (for
        CAPTCHA/login-wall detection), expected-field parse-drift signals, and
        the two Tier 1 provenance flags (``had_recipe`` / ``recipe_fallback``)
        that the classifier and Tier 2 admission gate read.

        This method performs NO Tier 2 escalation (PR #7 review round 2): the
        sink is a pure Tier 1 signal source. A source with no recipe
        (``had_recipe=False``) returns an empty result carrying
        ``expected_fields_missing`` so the backoff policy / classifier can tell
        it apart from a successful zero-row crawl, and the admission gate
        refuses to escalate it (beads: "Tier 1 empty results must not be
        directly upgraded"). A recipe that ran but yielded nothing
        (``had_recipe=True``) is positive job-source evidence — parse drift on
        a real source, which Tier 2 may then reason about.
        """
        engine = self._resolve_engine_for_request(request)
        if engine is None:
            # No structured recipe covers this source (direct lookup,
            # OFFICIAL probe, and detect() all missed). Surface the miss via
            # ``expected_fields_missing`` and ``had_recipe=False`` so the
            # classifier tags this NOT_JOB_SOURCE and the admission gate
            # refuses to escalate an unknown source into Tier 2.
            return CrawlSourceResult(
                postings=(),
                expected_fields_missing=_EXPECTED_POSTING_FIELDS,
                had_recipe=False,
            )

        result = await engine.execute(request, lambda url: self._fetch_for_request(request, url))
        source_url = result.source_url or request.base_url
        status_code = result.status_code
        body_prefix = result.body_prefix
        fetched_at_iso = (result.fetched_at or datetime.now(tz=UTC)).isoformat()

        postings = _records_to_postings(
            result.jobs, request, source_url, fetched_at_iso, engine.parser_version
        )

        # Detect parse drift: expected fields missing across all postings.
        missing_fields: list[str] = []
        if postings:
            # Check the first posting for expected fields.
            sample = postings[0].structured_data
            for field_name in _EXPECTED_POSTING_FIELDS:
                if not sample.get(field_name):
                    missing_fields.append(field_name)
        elif result.jobs:
            # Jobs were found by the adapter but produced no postings — unusual.
            missing_fields.extend(_EXPECTED_POSTING_FIELDS)

        return CrawlSourceResult(
            postings=tuple(postings),
            status_code=status_code,
            body_prefix=body_prefix,
            expected_fields_missing=tuple(missing_fields),
            had_recipe=True,
            recipe_fallback=engine.fallback,
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
            source_row = (
                conn.execute(
                    sa.select(
                        job_sources.c.company_id,
                        companies.c.name.label("company_name"),
                    )
                    .join(companies, companies.c.id == job_sources.c.company_id)
                    .where(job_sources.c.id == source_id)
                )
                .mappings()
                .first()
            )
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
