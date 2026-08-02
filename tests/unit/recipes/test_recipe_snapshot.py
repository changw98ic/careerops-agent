"""Recipe snapshot tests — the parity acceptance Gate (PR #7 review fix batch 4).

The legacy adapter classes (``GreenhouseAdapter`` / ``LeverAdapter`` /
``AshbyAdapter`` / ``JsonLdAdapter`` / ``SitemapAdapter`` /
``StaticHtmlAdapter``) were removed in Phase C (Task 11), so a side-by-side
"recipe vs legacy" comparison is no longer possible. This module replaces it
with a **fixture snapshot**: for each of the six shipped recipes we feed a
fixed input body through the real :class:`RecipeEngine` (loaded from
``vendor/crawl-recipes/``) and assert the exact 5-tuple
``(external_id, title, location, url, description)`` the engine produces.

The expected tuples are baked-in literals computed from the legacy semantics
that Fix 1 aligned the recipes to:

* greenhouse / lever / ashby — ``external_id`` is the API ``id`` field.
* official_jsonld — ``external_id = sha256(url)[:16]`` (legacy JsonLdAdapter).
* official_sitemap — ``external_id = sha256(url)[:16]`` (legacy SitemapAdapter).
* official_static — ``external_id = sha256(title.strip())[:16]`` (legacy
  StaticHtmlAdapter).

So a drift in any recipe's field mapping, the engine's ``_row_to_record``
id derivation, OR the evaluator's extract/id logic breaks the snapshot before
it reaches production. This is the documented "recipe output is stable +
aligned to legacy semantics" acceptance gate.

Why ``RecipeEngine.execute`` (not just ``evaluate_extract``): the task brief
asks the engine to produce the expected output — exercising the full
fetch → run_steps → _row_to_record → AdapterFetchResult path, which is where
``external_id`` derivation and ``apply_url``→``url`` mapping actually live.
``evaluate_extract`` alone does not produce ``external_id`` for the
json_path adapters (greenhouse/lever/ashby) — the engine layer does.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from careerops.adapters.job_sources import RawJobRecord
from careerops.infrastructure.temporal.m1_crawl_sink import DEFAULT_RECIPES_DIR
from careerops.recipes.engine import RecipeEngine
from careerops.recipes.loader import load_recipe

# The recipes catalog is resolved from ``m1_crawl_sink.__file__``-derived
# ``DEFAULT_RECIPES_DIR`` (cwd-independent, the same source the runtime sink
# loads). Tests fail loudly if the catalog is missing — that is a packaging
# regression, not a skip condition.
RECIPES_DIR = DEFAULT_RECIPES_DIR / "recipes"


class _FakeResp:
    """Stand-in for ``FetchedResponse`` with the attributes ``execute`` reads."""

    def __init__(self, body: str, status: int = 200, final_url: str = "https://x/jobs") -> None:
        self.body = body
        self.status_code = status
        self.fetched_at = datetime(2026, 1, 1)
        self.final_url = final_url


def _engine_for(recipe_name: str) -> RecipeEngine:
    """Load ``recipes/<recipe_name>/recipe.yaml`` and wrap it in a RecipeEngine."""
    recipe = load_recipe(RECIPES_DIR / recipe_name / "recipe.yaml")
    return RecipeEngine(recipe)


def _run(extract) -> tuple[RawJobRecord, ...]:
    return asyncio.run(extract)


def _five_tuple(record: RawJobRecord) -> tuple:
    return (
        record.external_id,
        record.title,
        record.location,
        record.url,
        record.description,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Neutralise ``time.sleep`` so the greenhouse rate_limit (60/min) does
    not slow the suite. The snapshot asserts OUTPUT, not pacing."""
    monkeypatch.setattr("careerops.recipes.evaluator.time.sleep", lambda *_a, **_k: None)


# ---------------------------------------------------------------------------
# Structured ATS APIs (greenhouse / lever / ashby) — json_path mode
# ---------------------------------------------------------------------------


def test_snapshot_greenhouse():
    """Greenhouse recipe: list step with ``?content=true`` inlines the JD body,
    so the detail step short-circuits (every row already has ``description``).
    Covers nested ``location.name``, ``absolute_url``, ``html_unescape``."""
    eng = _engine_for("greenhouse")
    body = (
        '{"jobs": ['
        '{"id": "1", "title": "Eng", "location": {"name": "SF"}, '
        '"absolute_url": "https://boards.greenhouse.io/acme/jobs/1", '
        '"content": "desc &amp; more"},'
        '{"id": "2", "title": "PM", "location": {"name": "NYC"}, '
        '"absolute_url": "https://boards.greenhouse.io/acme/jobs/2", '
        '"content": ""}'
        "]}"
    )
    # Row 2 has empty content → the detail step fires for it. The fake fetcher
    # returns a detail body whose ``content`` fills the description, exactly as
    # the real Greenhouse detail endpoint would.
    detail_body = '{"id": "2", "title": "PM", "location": {"name": "NYC"}, "content": "pm body"}'

    def fetch(endpoint: str):
        if endpoint.endswith("/2"):
            return _FakeResp(detail_body, final_url="https://x/detail/2")
        return _FakeResp(body, final_url="https://x/jobs?content=true")

    result = _run(eng.execute(request=None, fetch=fetch))
    expected = [
        ("1", "Eng", "SF", "https://boards.greenhouse.io/acme/jobs/1", "desc & more"),
        ("2", "PM", "NYC", "https://boards.greenhouse.io/acme/jobs/2", "pm body"),
    ]
    assert [_five_tuple(r) for r in result.jobs] == expected


def test_snapshot_lever():
    """Lever recipe: top-level array, ``categories.location``, fallback chains
    for ``applyUrl``→``hostedUrl`` and ``descriptionPlain``→``description``."""
    eng = _engine_for("lever")
    body = (
        "["
        '{"id": "1", "text": "Eng", "categories": {"location": "SF"}, '
        '"applyUrl": "https://jobs.lever.co/acme/apply/1", '
        '"descriptionPlain": "plain &amp; text"},'
        '{"id": "2", "text": "PM", "categories": {"location": "NYC"}, '
        '"hostedUrl": "https://jobs.lever.co/acme/hosted/2", '
        '"description": "html &lt; text"}'
        "]"
    )
    result = _run(eng.execute(request=None, fetch=lambda _: _FakeResp(body)))
    expected = [
        ("1", "Eng", "SF", "https://jobs.lever.co/acme/apply/1", "plain & text"),
        ("2", "PM", "NYC", "https://jobs.lever.co/acme/hosted/2", "html < text"),
    ]
    assert [_five_tuple(r) for r in result.jobs] == expected


def test_snapshot_ashby():
    """Ashby recipe: ``$.jobs``, plain-string ``location``, three-way fallback
    chains for ``apply_url`` and ``description``."""
    eng = _engine_for("ashby")
    body = (
        '{"jobs": ['
        '{"id": "1", "title": "Eng", "location": "SF", '
        '"applyUrl": "https://app.ashbyhq.com/acme/apply/1", '
        '"descriptionPlain": "plain &amp; 1"},'
        '{"id": "2", "title": "PM", "location": "NYC", '
        '"jobUrl": "https://app.ashbyhq.com/acme/job/2", '
        '"descriptionHtml": "<p>html 2</p>"},'
        '{"id": "3", "title": "Dev", "location": "Remote", '
        '"url": "https://app.ashbyhq.com/acme/url/3", '
        '"description": "plain desc 3"}'
        "]}"
    )
    result = _run(eng.execute(request=None, fetch=lambda _: _FakeResp(body)))
    expected = [
        ("1", "Eng", "SF", "https://app.ashbyhq.com/acme/apply/1", "plain & 1"),
        ("2", "PM", "NYC", "https://app.ashbyhq.com/acme/job/2", "<p>html 2</p>"),
        ("3", "Dev", "Remote", "https://app.ashbyhq.com/acme/url/3", "plain desc 3"),
    ]
    assert [_five_tuple(r) for r in result.jobs] == expected


# ---------------------------------------------------------------------------
# Official meta-type recipes (json_ld / sitemap / css)
# ---------------------------------------------------------------------------


def test_snapshot_official_jsonld():
    """official_jsonld recipe: ``@['@type']=='JobPosting'`` filter drops
    non-JobPosting blocks; ``external_id = sha256(url)[:16]``; nested
    ``jobLocation.address.addressLocality``; a row missing ``jobLocation``
    yields ``location == ""``."""
    eng = _engine_for("official_jsonld")
    html_doc = (
        '<script type="application/ld+json">'
        '{"@type":"Person","name":"Alice"}'
        "</script>"
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","title":"Eng","description":"desc 1",'
        '"url":"https://x.com/jobs/1",'
        '"jobLocation":{"address":{"addressLocality":"SF"}}}'
        "</script>"
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","title":"PM","description":"desc 2",'
        '"url":"https://x.com/jobs/2"}'
        "</script>"
    )
    result = _run(eng.execute(request=None, fetch=lambda _: _FakeResp(html_doc)))
    expected = [
        ("084ccffa4f0902a2", "Eng", "SF", "https://x.com/jobs/1", "desc 1"),
        ("bdcb80741417fe9c", "PM", "", "https://x.com/jobs/2", "desc 2"),
    ]
    assert [_five_tuple(r) for r in result.jobs] == expected


def test_snapshot_official_sitemap():
    """official_sitemap recipe: ``url_filter`` drops non-job URLs;
    ``external_id = sha256(url)[:16]`` inline (the evaluator computes it, so
    it IS part of the snapshot). XXE guard rejects DOCTYPE/ENTITY input."""
    eng = _engine_for("official_sitemap")
    xml = (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://x.com/about</loc></url>"
        "<url><loc>https://x.com/jobs/1</loc></url>"
        "<url><loc>https://x.com/careers/2</loc></url>"
        "</urlset>"
    )
    result = _run(eng.execute(request=None, fetch=lambda _: _FakeResp(xml)))
    # Sitemap rows carry only ``url`` + ``external_id`` (no title/location/desc);
    # the 5-tuple's middle three collapse to "".
    expected = [
        ("084ccffa4f0902a2", "", "", "https://x.com/jobs/1", ""),
        ("ef66ec4a1676c1fd", "", "", "https://x.com/careers/2", ""),
    ]
    assert [_five_tuple(r) for r in result.jobs] == expected


def test_snapshot_official_static():
    """official_static recipe: css mode zip-pairs the title/location columns.
    Covers all three class-name forms the selector list now accepts:
    ``job_title`` (underscore), ``job-title`` (dash), ``jobtitle`` (concat).
    ``external_id = sha256(title.strip())[:16]``."""
    eng = _engine_for("official_static")
    doc = (
        "<div>"
        '<h2 class="job_title">Eng</h2><span class="job_location">SF</span>'
        '<h2 class="job-title">PM</h2><span class="job-location">NYC</span>'
        '<h2 class="jobtitle">Dev</h2><span class="joblocation">Remote</span>'
        "</div>"
    )
    result = _run(eng.execute(request=None, fetch=lambda _: _FakeResp(doc)))
    expected = [
        ("67210a3e52e46311", "Eng", "SF", "", ""),
        ("6b987654a4116384", "PM", "NYC", "", ""),
        # sha256("Dev")[:16]
        ("9c24f45a7ea9e466", "Dev", "Remote", "", ""),
    ]
    assert [_five_tuple(r) for r in result.jobs] == expected
