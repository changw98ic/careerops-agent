"""ATS adapter contracts (M1.5+).

The legacy hard-coded adapter classes (``GreenhouseAdapter`` /
``GreenhouseDetailAdapter`` / ``LeverAdapter`` / ``AshbyAdapter`` /
``AshbyDetailAdapter`` / ``JsonLdAdapter`` / ``SitemapAdapter`` /
``StaticHtmlAdapter``) were removed in Task 11 (Phase C): every source type
is now driven declaratively by a YAML recipe loaded from
``vendor/crawl-recipes/`` and executed by :class:`careerops.recipes.engine.RecipeEngine`.

This module retains only the value types and Protocol contracts the recipe
engine and the rest of the crawl pipeline share:

* :class:`RawJobRecord` -- the parser-layer value object (also produced by
  ``RecipeEngine._row_to_record`` and the Tier 2 ``CrawlAgent``).
* :class:`AdapterFetchResult` -- the engine's return shape, including the
  HTTP-level signal fields (``status_code`` / ``body_prefix``) sampled from
  the first fetch.
* :class:`JobSourceAdapter` / :class:`DetailJobSourceAdapter` Protocol
  contracts kept as the documented shape ``RecipeEngine`` satisfies; new
  call sites should treat ``RecipeEngine`` as the only concrete
  implementation and dispatch through ``adapter.execute(...)``.
* :func:`_hash_response` -- shared helper used by both the recipe engine
  and any future structured adapter that wants response-hash dedup.
"""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RawJobRecord:
    """A raw job record from an adapter before normalization."""

    external_id: str
    title: str
    location: str = ""
    url: str = ""
    description: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)
    # Provenance tag for Tier 2 records: ``api-capture`` (parsed from a captured
    # job-list API) or ``llm-extraction`` (model-extracted from rendered HTML).
    # Structured Tier 1 adapters leave this empty; the crawl sink carries it into
    # ``parser_version`` so the ingest layer can tell the sources apart.
    provenance: str = ""


@dataclass(frozen=True, slots=True)
class AdapterFetchResult:
    """Result of fetching jobs from a source."""

    jobs: tuple[RawJobRecord, ...] = ()
    response_hash: str = ""
    fetched_at: datetime | None = None
    source_url: str = ""
    parser_version: str = ""
    # HTTP-level signals from the first (list-step) response. ``status_code``
    # feeds the backoff policy (403/429/CAPTCHA detection); ``body_prefix`` is
    # the first 4 KiB of the body — a bounded slice that lets the CAPTCHA
    # detector run without holding the full payload. Both default to sentinels
    # (0 / "") so legacy callers that only parse ``response_data`` are
    # unaffected; the recipe engine (Task 6) is the first writer.
    status_code: int = 0
    body_prefix: str = ""


class JobSourceAdapter(Protocol):
    """Contract for all job source adapters.

    The only concrete implementation post-Task-11 is
    :class:`careerops.recipes.engine.RecipeEngine`; new call sites dispatch
    through ``adapter.execute(request, fetch)`` (see
    :meth:`RealCrawlActivitySink.crawl_source`). The Protocol is retained as
    the documented static shape.
    """

    @property
    def source_type(self) -> str: ...

    @property
    def parser_version(self) -> str: ...

    def detect(self, base_url: str) -> bool: ...

    def list_jobs(self, response_data: object) -> AdapterFetchResult: ...


class DetailJobSourceAdapter(Protocol):
    """Contract for adapters that parse a single job's detail response.

    Only the legacy per-job detail adapters (``GreenhouseDetailAdapter`` /
    ``AshbyDetailAdapter``) implemented this directly, and they were removed
    in Task 11 — the recipe engine now performs any multi-step list→detail
    fan-out declaratively (see the ``greenhouse`` recipe). The Protocol is
    retained as the documented shape for any future structured detail step.
    """

    @property
    def source_type(self) -> str: ...

    @property
    def parser_version(self) -> str: ...

    def fetch_job(
        self,
        detail_response: object,
        *,
        source_url: str,
        fetched_at: datetime,
    ) -> RawJobRecord: ...


def _hash_response(data: object) -> str:
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# Module re-exports. ``_hash_response`` is included here so pyright treats it
# as a kept surface even though the legacy adapters that called it were
# removed in Task 11; the recipe engine has its own response-hash logic, but
# future structured adapters (or tests) may reuse this helper.
__all__ = [
    "AdapterFetchResult",
    "DetailJobSourceAdapter",
    "JobSourceAdapter",
    "RawJobRecord",
    "_hash_response",
]
