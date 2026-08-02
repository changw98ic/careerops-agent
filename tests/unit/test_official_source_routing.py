"""OFFICIAL meta-type sub-routing + http Tier 2 fallback (PR #7 review fix, batch 1).

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
* ``source_type="unknown"`` + a wired agent → agent fallback runs (http
  mode, no longer gated on ``executor_mode == 'ego'``).
* ``source_type="unknown"`` + no agent → ``crawl_source_with_signals``
  returns a non-empty ``expected_fields_missing`` signal (not silent).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from careerops.adapters.http_fetcher import FetchedResponse
from careerops.adapters.job_sources import RawJobRecord
from careerops.infrastructure.temporal.m1_crawl_sink import (
    CrawlSourceResult,
    RealCrawlActivitySink,
)
from careerops.workflows.m1_contracts import CrawlJobSourceInput


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Fixtures: bodies that match each official_* recipe's expected shape.
# ---------------------------------------------------------------------------

JSONLD_BODY = (
    '<html><head>'
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
    assert urls == sorted([_sha16("https://acme.test/jobs/1"), _sha16("https://acme.test/careers/2")])
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
# http Tier 2 fallback — no longer gated on executor_mode == 'ego'.
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Stand-in for ``CrawlAgent``. Records the URL it was called with and
    returns one canned ``RawJobRecord`` so callers can verify the agent path
    actually ran."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def crawl(self, source_url: str, *, source_type: str = "") -> list[RawJobRecord]:
        self.calls.append(source_url)
        return [
            RawJobRecord(
                external_id="agent-id-1",
                title="Agent-extracted",
                location="Remote",
                url=source_url,
                description="from agent",
                provenance="llm-extraction",
            )
        ]


@pytest.mark.asyncio
async def test_http_unknown_source_falls_back_to_agent_when_wired():
    """An http source whose source_type has no recipe must delegate to the
    injected agent (previously gated on ``executor_mode == 'ego'`` and so
    silently empty for http)."""
    agent = _FakeAgent()
    sink = RealCrawlActivitySink(
        # No recipe matches source_type 'unknown_xyz'; an empty adapter dict
        # forces every lookup to miss so we exercise the fallback directly.
        adapters={},
        fetcher=_fetcher_returning("<html></html>"),
        agent=agent,
    )
    request = CrawlJobSourceInput(
        source_id="src-1",
        company_id="c",
        company_name="C",
        source_type="unknown_xyz",
        base_url="https://acme.test/jobs",
        executor_mode="http",
    )

    # crawl_source returns the agent's postings as a list.
    postings = await sink.crawl_source(request)
    assert len(postings) == 1
    assert postings[0].structured_data["title"] == "Agent-extracted"
    assert agent.calls == ["https://acme.test/jobs"]

    # crawl_source_with_signals returns the same via CrawlSourceResult.
    agent.calls.clear()
    result = await sink.crawl_source_with_signals(request)
    assert isinstance(result, CrawlSourceResult)
    assert len(result.postings) == 1
    assert result.postings[0].external_id == "agent-id-1"
    assert agent.calls == ["https://acme.test/jobs"]


@pytest.mark.asyncio
async def test_http_unknown_source_without_agent_signals_miss_not_silently_empty():
    """When no agent is wired and no recipe matches, the signals-bearing
    result surfaces ``expected_fields_missing`` so the backoff policy /
    operator can tell this apart from a successful zero-row crawl. The
    bare ``crawl_source`` returns an empty list (its contract is list-only;
    it has no signal channel)."""
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

    # signals version: not silent — expected_fields_missing is populated.
    result = await sink.crawl_source_with_signals(request)
    assert result.postings == ()
    assert result.expected_fields_missing == ("title",)

    # bare version: empty list, no signal channel (by contract).
    postings = await sink.crawl_source(request)
    assert postings == []
