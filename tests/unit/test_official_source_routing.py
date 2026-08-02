"""OFFICIAL meta-type sub-routing + Tier 1 signal contract (PR #7 review fix).

Background: ``CrawlSourceType.OFFICIAL`` is an enum meta-type. The recipe
manifest registers three concrete recipes (``official_jsonld`` /
``official_sitemap`` / ``official_static``) keyed by their actual list shape,
while the DB stores ``"official"``. A direct ``registry.get("official")``
lookup returns ``None``, so without content-driven sub-routing every
OFFICIAL crawl silently zeroed.

These tests pin the post-fix behaviour end-to-end through the real
``RealCrawlActivitySink`` + the real ``vendor/crawl-recipes/`` catalog:

* ``source_type="official"`` + a sitemap body → ``official_sitemap`` engine,
  postings with ``external_id = sha256(url)[:16]``.
* ``source_type="official"`` + a JSON-LD body with a ``JobPosting`` block →
  ``official_jsonld`` engine, ``external_id = sha256(url)[:16]``.
* ``source_type="official"`` + a static-HTML body → ``official_static``
  engine, ``external_id = sha256(title)[:16]``.
* ``source_type="official"`` + a clean 200 empty body → ``had_recipe=True``
  (so the classifier tags it VERIFIED_EMPTY, not NOT_JOB_SOURCE).
* ``supports_source_type("official")`` recognises the meta-type.
* ``source_type="unknown"`` → ``crawl_source_with_signals`` returns
  ``had_recipe=False`` + ``expected_fields_missing=("title",)``. The sink
  performs NO Tier 2 escalation (round 2); an unknown source never silently
  enters the agent — the admission gate decides that upstream.
* ``detect()`` URL fallback: an unknown ``source_type`` whose URL matches a
  recipe's ``Match`` block is still parsed by Tier 1 (``had_recipe=True``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from careerops.adapters.http_fetcher import FetchedResponse
from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink
from careerops.workflows.m1_contracts import CrawlJobSourceInput


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Fixtures: bodies that match each official_* recipe's expected shape.
# ---------------------------------------------------------------------------

JSONLD_BODY = (
    "<html><head>"
    '<script type="application/ld+json">'
    '{"@type":"JobPosting","title":"Senior Eng","url":"https://acme.test/jobs/1"}'
    "</script>"
    '<script type="application/ld+json">'
    '{"@type":"WebSite","name":"Acme"}'
    "</script>"
    "</head><body></body></html>"
)

SITEMAP_BODY = (
    '<?xml version="1.0"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://acme.test/about</loc></url>"
    "<url><loc>https://acme.test/jobs/1</loc></url>"
    "<url><loc>https://acme.test/careers/2</loc></url>"
    "</urlset>"
)

STATIC_BODY = (
    "<html><body>"
    '<div class="job_title">Backend Eng</div><div class="job_location">SF</div>'
    '<div class="job_title">Frontend Eng</div><div class="job_location">NYC</div>'
    "</body></html>"
)


def _fetcher_returning(body: str, status: int = 200):
    """Build a fake fetcher that always returns ``body`` regardless of URL."""

    def fetch(url: str) -> FetchedResponse:
        return FetchedResponse(
            status_code=status,
            final_url=url,
            fetched_at=datetime(2026, 8, 2, tzinfo=UTC),
            response_hash="h" * 64,
            body=body,
        )

    return fetch


def _official_request(body_label: str) -> CrawlJobSourceInput:
    """Build a CrawlJobSourceInput for an OFFICIAL source.

    ``base_url`` carries the body label so a multi-body fetcher can dispatch
    on it (see ``_multi_body_fetcher``). Real code passes a real URL; the
    fetcher fake is what makes the label meaningful.
    """
    return CrawlJobSourceInput(
        source_id="source-official",
        company_id="company-1",
        company_name="Acme",
        source_type="official",
        base_url=f"https://acme.test/{body_label}",
    )


def _multi_body_fetcher(mapping: dict[str, str]) -> Callable[[str], FetchedResponse]:
    """Build a fake fetcher that picks the body by URL suffix label.

    The probe helper and ``RecipeEngine.execute`` both call the fetcher for
    ``base_url``; this fake returns the same body on every call so probe and
    execute agree on the shape.
    """

    def fetch(url: str) -> FetchedResponse:
        for label, body in mapping.items():
            if label in url:
                return FetchedResponse(
                    status_code=200,
                    final_url=url,
                    fetched_at=datetime(2026, 8, 2, tzinfo=UTC),
                    response_hash="h" * 64,
                    body=body,
                )
        raise AssertionError(f"unexpected fetch URL: {url}")

    return fetch


# ---------------------------------------------------------------------------
# OFFICIAL sub-routing — the three concrete recipes get selected correctly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_official_jsonld_source_picks_jsonld_recipe_and_external_id():
    """A JSON-LD body routes to ``official_jsonld``; each posting's
    ``external_id`` is ``sha256(url)[:16]`` — the legacy ``JsonLdAdapter``
    dedup key — so existing postings keep their id after the recipe
    migration."""
    sink = RealCrawlActivitySink(fetcher=_fetcher_returning(JSONLD_BODY))
    result = await sink.crawl_source_with_signals(_official_request("jsonld"))

    assert len(result.postings) == 1
    posting = result.postings[0]
    assert posting.structured_data["title"] == "Senior Eng"
    # JSON-LD block's url field → sha256[:16]
    assert posting.external_id == _sha16("https://acme.test/jobs/1")
    # parser_version names the chosen recipe — proves official_jsonld ran.
    assert posting.parser_version == "recipe:official_jsonld:1"
    # signals: first-step status captured.
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_official_sitemap_source_picks_sitemap_recipe():
    """A sitemap XML body routes to ``official_sitemap``; the two job-bearing
    URLs survive the ``\\b(jobs|careers|positions)\\b`` filter and each
    posting's ``external_id`` is ``sha256(url)[:16]``."""
    sink = RealCrawlActivitySink(fetcher=_fetcher_returning(SITEMAP_BODY))
    result = await sink.crawl_source_with_signals(_official_request("sitemap"))

    urls = sorted(p.external_id for p in result.postings)
    assert urls == sorted(
        [_sha16("https://acme.test/jobs/1"), _sha16("https://acme.test/careers/2")]
    )
    # parser_version names the chosen recipe — proves official_sitemap ran.
    assert all(p.parser_version == "recipe:official_sitemap:1" for p in result.postings)
    # the about URL was filtered out
    assert not any("about" in (p.canonical_url or "") for p in result.postings)


@pytest.mark.asyncio
async def test_official_static_source_picks_static_recipe_and_external_id():
    """A static-HTML body (no JSON-LD, not XML) routes to ``official_static``
    as the fallback; each posting's ``external_id`` is
    ``sha256(title)[:16]`` — the legacy ``StaticHtmlAdapter`` dedup key."""
    sink = RealCrawlActivitySink(fetcher=_fetcher_returning(STATIC_BODY))
    result = await sink.crawl_source_with_signals(_official_request("static"))

    assert len(result.postings) == 2
    titles_to_ids = {p.structured_data["title"]: p.external_id for p in result.postings}
    assert titles_to_ids == {
        "Backend Eng": _sha16("Backend Eng"),
        "Frontend Eng": _sha16("Frontend Eng"),
    }
    assert all(p.parser_version == "recipe:official_static:1" for p in result.postings)


@pytest.mark.asyncio
async def test_official_subrouting_is_decided_by_body_not_by_registry_key():
    """Calling crawl_source on source_type='official' must NOT short-circuit
    on a missing ``'official'`` registry key — the meta-type is resolved by
    probing the body. This test exercises all three bodies through one sink
    to prove the probe is per-request, not memoised."""
    sink = RealCrawlActivitySink(
        fetcher=_multi_body_fetcher(
            {"jsonld": JSONLD_BODY, "sitemap": SITEMAP_BODY, "static": STATIC_BODY}
        )
    )

    jsonld = await sink.crawl_source(_official_request("jsonld"))
    sitemap = await sink.crawl_source(_official_request("sitemap"))
    static = await sink.crawl_source(_official_request("static"))

    assert jsonld[0].parser_version == "recipe:official_jsonld:1"
    assert all(p.parser_version == "recipe:official_sitemap:1" for p in sitemap)
    assert all(p.parser_version == "recipe:official_static:1" for p in static)


# ---------------------------------------------------------------------------
# supports_source_type meta-type recognition + clean-empty OFFICIAL signal.
# ---------------------------------------------------------------------------


def test_supports_source_type_recognises_official_meta_type():
    """``supports_source_type("official")`` returns ``True`` whenever any
    ``official_*`` recipe is loaded, so the classifier tags a clean 200-empty
    OFFICIAL crawl as VERIFIED_EMPTY rather than NOT_JOB_SOURCE."""
    sink = RealCrawlActivitySink(fetcher=_fetcher_returning("<html></html>"))
    assert sink.supports_source_type("official") is True
    assert sink.supports_source_type("greenhouse") is True
    assert sink.supports_source_type("unknown_xyz") is False


def test_supports_source_type_false_when_no_official_recipes_loaded():
    """With an empty adapter dict the meta-type has nothing behind it."""
    sink = RealCrawlActivitySink(adapters={}, fetcher=_fetcher_returning(""))
    assert sink.supports_source_type("official") is False


@pytest.mark.asyncio
async def test_official_clean_200_empty_signals_had_recipe():
    """A clean 200 OFFICIAL page whose recipe ran but found no jobs carries
    ``had_recipe=True`` and no missing fields — the classifier maps that to
    VERIFIED_EMPTY (not NOT_JOB_SOURCE), because the recipe actually ran."""
    # A JSON-LD body with no JobPosting block: official_jsonld's extract
    # yields zero rows, but the recipe DID run, so had_recipe=True.
    sink = RealCrawlActivitySink(
        fetcher=_fetcher_returning(
            '<html><head><script type="application/ld+json">'
            '{"@type":"WebSite","name":"Acme"}</script>'
            "</head><body></body></html>"
        )
    )
    result = await sink.crawl_source_with_signals(_official_request("jsonld"))
    assert result.postings == ()
    assert result.status_code == 200
    assert result.expected_fields_missing == ()
    assert result.had_recipe is True


# ---------------------------------------------------------------------------
# Unknown source: Tier 1 signals only, NO Tier 2 escalation in the sink.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_source_returns_tier1_signals_not_silently_empty():
    """A source with no recipe (``had_recipe=False``) returns an empty result
    carrying ``expected_fields_missing`` so the classifier tags it
    NOT_JOB_SOURCE and the admission gate refuses to escalate. The sink
    performs NO Tier 2 escalation itself (PR #7 round 2). The bare
    ``crawl_source`` returns ``[]`` (its contract is list-only)."""
    sink = RealCrawlActivitySink(
        adapters={},
        fetcher=_fetcher_returning("<html></html>"),
    )
    request = CrawlJobSourceInput(
        source_id="src-1",
        company_id="c",
        company_name="C",
        source_type="unknown_xyz",
        base_url="https://acme.test/jobs",
        executor_mode="http",
    )

    # signals version: not silent — expected_fields_missing populated, no recipe.
    result = await sink.crawl_source_with_signals(request)
    assert result.postings == ()
    assert result.expected_fields_missing == ("title",)
    assert result.had_recipe is False

    # bare version: empty list, no signal channel (by contract).
    postings = await sink.crawl_source(request)
    assert postings == []


@pytest.mark.asyncio
async def test_detect_url_fallback_routes_mislabelled_source():
    """A source whose ``source_type`` is unknown but whose ``base_url`` matches
    a recipe's ``Match`` block (e.g. a Greenhouse board URL) is still parsed by
    Tier 1 via ``detect()``, and the run carries ``had_recipe=True`` as
    positive evidence for the admission gate."""
    sink = RealCrawlActivitySink(fetcher=_fetcher_returning("[]"))
    request = CrawlJobSourceInput(
        source_id="src-1",
        company_id="c",
        company_name="C",
        # Mislabelled type, but the URL matches the greenhouse recipe's
        # ``url_patterns: [boards.greenhouse.io]`` so detect() routes it.
        source_type="unknown_xyz",
        base_url="https://boards.greenhouse.io/acme",
        executor_mode="http",
    )
    result = await sink.crawl_source_with_signals(request)
    # The greenhouse recipe matched via detect(); it ran even on an empty body.
    assert result.had_recipe is True
    assert result.status_code == 200
