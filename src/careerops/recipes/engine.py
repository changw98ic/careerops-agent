"""RecipeEngine (Task 6) — the Phase A capstone that turns a declarative
:class:`Recipe` into an executable adapter producing ``AdapterFetchResult``.

The engine is the run-time counterpart of the schema (Task 2) and the
multi-step evaluator (Tasks 3-5). It binds a recipe to the existing crawl
contracts:

* Input  — ``FetcherFn`` (``Callable[[str], FetchedResponse]``) from
  ``m1_crawl_sink``: the caller injects a fetch bound to a specific source
  request (HTTP credentials, ego session, etc.). The engine itself holds no
  HTTP client and carries no request state — it is recipe + evaluator plus
  the field mapping into :class:`RawJobRecord`.
* Output — :class:`AdapterFetchResult`, extended in Task 6 with two
  HTTP-level signal fields (``status_code`` / ``body_prefix``) sampled from
  the *first* (list-step) response. The backoff policy (task 5.7) reads those
  to detect 403/429/CAPTCHA without re-fetching.

The first-step-signal rule is deliberate: the list response is the canonical
"did this source render at all" probe. Detail fetches are per-item and their
status is consumed by the per-item fault tolerance in ``run_steps``; only the
list step's signals propagate to the backoff layer.

This module is the single bridge between the recipes package (pure
declarative evaluation) and the adapters package (the crawl pipeline's
record contracts). Phase B will swap ``GreenhouseAdapter`` / ``LeverAdapter``
for ``RecipeEngine`` instances behind the same ``JobSourceAdapter`` surface.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from json import JSONDecodeError
from typing import Any

from careerops.adapters.job_sources import AdapterFetchResult, RawJobRecord
from careerops.recipes.evaluator import run_steps
from careerops.recipes.schema import Recipe

# Row keys that map onto top-level :class:`RawJobRecord` fields and are
# therefore excluded from the free-form ``raw_data`` payload. ``external_id``
# is included even though :class:`RawJobRecord` has no such field — recipes
# commonly use it as the id alias, and once it has fed ``external_id`` it
# should not also appear as a passthrough data field.
_INTERNAL_FIELDS = frozenset(
    {"id", "external_id", "title", "location", "description", "apply_url", "url"}
)


def _row_to_record(row: dict[str, Any]) -> RawJobRecord:
    """Map one evaluator row (field → value) onto a :class:`RawJobRecord`.

    Field resolution order:

    * ``external_id``: ``row["id"]`` → ``row["external_id"]`` → ``sha256(str(row))[:16]``.
      The sha256 fallback gives every row a stable, content-derived id even
      when the recipe did not extract one (mirrors the legacy
      ``SitemapAdapter`` / ``StaticHtmlAdapter`` convention).
    * ``url``: ``row["apply_url"]`` → ``row["url"]`` → ``""``. ``apply_url``
      wins because it is the canonical Greenhouse/Lever "where the candidate
      lands" link; ``url`` is the fallback for recipes that only expose a
      canonical posting URL.
    * ``raw_data``: every remaining key, preserving recipe-specific fields
      (``department``, ``employment_type``, etc.) for downstream display.
    """
    fallback_id = hashlib.sha256(str(row).encode()).hexdigest()[:16]
    external_id = str(row.get("id") or row.get("external_id") or fallback_id)
    url = row.get("apply_url") or row.get("url") or ""
    return RawJobRecord(
        external_id=external_id,
        title=str(row.get("title") or ""),
        location=str(row.get("location") or ""),
        url=url,
        description=str(row.get("description") or ""),
        raw_data={k: v for k, v in row.items() if k not in _INTERNAL_FIELDS},
    )


def _parse_body(body: object) -> object:
    """Parse a fetch body the way ``m1_crawl_sink._parse_body`` does.

    JSON-looking payloads (leading ``{`` or ``[``) are decoded; everything
    else (HTML, XML, plain text) is returned as-is so the evaluator's
    ``json_ld`` / ``sitemap`` / ``css`` modes receive the raw string they
    expect. Decode failures fall back to the raw string — one malformed JSON
    payload must not sink the whole run.
    """
    if not isinstance(body, str):
        return body
    stripped = body.lstrip()
    if stripped and stripped[0] in "{[":
        try:
            return json.loads(body)
        except (ValueError, JSONDecodeError):
            return body
    return body


class RecipeEngine:
    """Bind a :class:`Recipe` into the ``JobSourceAdapter``-shaped contract.

    The engine is stateless beyond the recipe it wraps; every ``execute``
    call runs the full step pipeline against the injected fetcher. The
    ``source_type`` / ``parser_version`` / ``detect`` surface mirrors the
    existing adapter classes so a Phase B registry swap is a drop-in.
    """

    def __init__(self, recipe: Recipe) -> None:
        self._recipe = recipe

    @property
    def source_type(self) -> str:
        return self._recipe.source_type

    @property
    def parser_version(self) -> str:
        # ``recipe:<source_type>:<recipe_version>`` is the namespace the
        # ingest layer uses to tell recipe-driven crawls apart from the
        # legacy hard-coded adapters (``greenhouse-v1`` etc.).
        return f"recipe:{self._recipe.source_type}:{self._recipe.recipe_version}"

    def detect(self, base_url: str) -> bool:
        """Match ``base_url`` against the recipe's :class:`Match` block.

        ``url_patterns`` containing a ``{slug}`` placeholder match by the
        substring before ``{`` — e.g. ``"https://x/{slug}"`` matches any URL
        containing ``"https://x/"``. ``host_suffix`` matches when the URL
        ends with the suffix (the canonical lever/ashby ATS host check).
        """
        prefix_match = any(p.split("{", 1)[0] in base_url for p in self._recipe.match.url_patterns)
        host_match = bool(
            self._recipe.match.host_suffix and base_url.endswith(self._recipe.match.host_suffix)
        )
        return prefix_match or host_match

    async def execute(self, request: Any, fetch: Callable[[str], Any]) -> AdapterFetchResult:
        """Run the recipe's steps via the injected ``fetch`` and return the
        assembled :class:`AdapterFetchResult`.

        ``request`` is the owning :class:`CrawlJobSourceInput` (or ``None``
        in tests). It is None-safe: only its ``source_identifier`` /
        ``base_url`` attributes are read, both via ``getattr`` with a default.
        The injected ``fetch`` carries all the per-request HTTP context
        (auth, ego session, rate-limit state) — the engine never opens a
        socket itself.

        First-step signals: the first ``fetch`` call's ``status_code``,
        ``body_prefix`` (first 4 KiB), ``final_url`` AND ``fetched_at`` are
        sampled into the result. Detail fetches' statuses and URLs are NOT
        propagated — they are per-item and are handled by ``run_steps``' per-
        item fault tolerance. The first-step rule keeps ``source_url`` /
        ``fetched_at`` aligned with the same list response that produced
        ``status_code`` / ``body_prefix`` (a detail URL leaking into
        ``source_url`` would corrupt canonical URL resolution and the backoff
        policy's signal consistency).
        """
        first_status = 0
        first_body = ""
        final_url = ""
        first_fetched_at = None
        first_sampled = False

        def fetch_one(endpoint: str) -> object:
            nonlocal first_status, first_body, final_url, first_fetched_at, first_sampled
            resp = fetch(endpoint)
            # Sample status_code / body_prefix / final_url / fetched_at from
            # the FIRST fetch only. A plain ``if not first_status`` guard
            # would re-enter when the first response legitimately returned
            # status 0 (or an empty status attribute), letting a later detail
            # fetch clobber the list-step signals — so a dedicated sampled-
            # flag is the correct gate. Detail responses' status/url stay
            # per-item.
            if not first_sampled:
                first_sampled = True
                first_status = getattr(resp, "status_code", 0) or 0
                raw_body = getattr(resp, "body", "") or ""
                first_body = raw_body[:4096]
                final_url = getattr(resp, "final_url", endpoint) or endpoint
                first_fetched_at = getattr(resp, "fetched_at", None)
            return _parse_body(getattr(resp, "body", ""))

        base_ns: dict[str, str] = {}
        if request is not None:
            slug = getattr(request, "source_identifier", "") or ""
            base_ns["slug"] = slug
            base_url_attr = getattr(request, "base_url", "") or ""
            base_ns["base_url"] = base_url_attr

        steps_ns: dict[str, list[dict]] = run_steps(
            self._recipe.steps,
            fetch_one,
            base_ns,
            rate_limit=self._recipe.rate_limit or None,
        )
        # Multi-step merge is already applied in-place by ``run_steps``; the
        # primary rows are those of the first (list) step. Taking
        # ``next(iter(...))`` is order-preserving on dict (Python 3.7+).
        primary: list[dict] = next(iter(steps_ns.values()), []) if steps_ns else []
        records = tuple(_row_to_record(row) for row in primary)

        return AdapterFetchResult(
            jobs=records,
            response_hash=hashlib.sha256(str(primary).encode()).hexdigest(),
            source_url=final_url,
            parser_version=self.parser_version,
            status_code=first_status,
            body_prefix=first_body,
            fetched_at=first_fetched_at,
        )
