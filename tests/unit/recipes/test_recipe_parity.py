"""Parity tests: recipe list-step extract vs legacy adapter ``list_jobs``.

For each ATS source (greenhouse / lever / ashby) the recipe's list-step
``extract`` block must produce the same per-job 5-tuple
``(external_id, title, location, url, description)`` as the legacy hand-coded
adapter given identical mock JSON. This is the regression gate for the
recipe translation: if a field mapping drifts, the parity assertion fails
before the legacy adapter is removed (Task 11).

Scope. Only the list-step ``extract`` is exercised directly via
:func:`evaluate_extract`. The detail-step fan-out (greenhouse) and the full
:class:`RecipeEngine` binding (Task 6) are covered by their own task tests;
here we isolate the field-mapping equivalence, which is the part Task 8
authors. The comparison key:

* legacy ``RawJobRecord`` exposes ``external_id`` / ``url``.
* recipe rows expose ``id`` / ``apply_url`` (the canonical ATS field names).
* :class:`RecipeEngine._row_to_record` (Task 6) performs the ``id →
  external_id`` / ``apply_url → url`` mapping at the engine layer; we mirror
  that mapping in the test's tuple key so the extract comparison stays at
  the field-mapping layer.

Mock data design. Each site's payload has multiple rows exercising:
* a populated primary field (happy path),
* a nested field (greenhouse ``location.name``; lever ``categories.location``),
* ``transform: html_unescape`` on the description,
* the fallback chain when the primary field key is **missing** (lever
  ``applyUrl``→``hostedUrl``; ashby ``applyUrl``→``jobUrl``→``url``;
  description ``descriptionPlain``→``descriptionHtml``→``description``).

The missing-key fallback path is the one :func:`apply_field` handles
correctly (a missing key yields an empty ``findall`` list, so the next
candidate is tried). The legacy adapters use Python ``or`` short-circuit,
which additionally falls back when the primary value is an empty string;
that edge case is a pre-existing :func:`apply_field` semantic (Task 3
design — "first non-empty *list*" vs legacy "first non-empty *value*") and
is out of scope for the recipe translation; the mock data avoids empty-but-
present primary values to keep the parity assertion focused on mapping
correctness.
"""

from __future__ import annotations

from careerops.adapters.job_sources import (
    AshbyAdapter,
    GreenhouseAdapter,
    JsonLdAdapter,
    LeverAdapter,
    RawJobRecord,
    SitemapAdapter,
    StaticHtmlAdapter,
)
from careerops.infrastructure.temporal.m1_crawl_sink import DEFAULT_RECIPES_DIR
from careerops.recipes.evaluator import evaluate_extract
from careerops.recipes.loader import load_recipe

# Resolve the recipes directory from ``__file__``-derived DEFAULT_RECIPES_DIR
# (``<repo>/vendor/crawl-recipes``) rather than a relative path. The latter is
# cwd-fragile: running pytest from a subdirectory would raise FileNotFoundError
# on ``vendor/crawl-recipes/recipes``. The single source of truth lives in
# ``m1_crawl_sink``; importing it keeps the parity tests aligned with the
# runtime catalog location.
RECIPES_DIR = DEFAULT_RECIPES_DIR / "recipes"


def _list_step_extract_rows(recipe_name: str) -> list[dict]:
    """Load ``<recipe_name>/recipe.yaml`` and run its list step's extract
    directly (no fetch, no engine). Returns the evaluator's rows."""
    recipe = load_recipe(RECIPES_DIR / f"{recipe_name}" / "recipe.yaml")
    list_step = next(s for s in recipe.steps if s.id == "list")
    return list_step.extract


def _legacy_tuples(adapter, data) -> list[tuple]:
    """Run the legacy adapter and project each ``RawJobRecord`` to the
    5-tuple ``(external_id, title, location, url, description)``."""
    jobs: tuple[RawJobRecord, ...] = adapter.list_jobs(data).jobs
    return [(j.external_id, j.title, j.location, j.url, j.description) for j in jobs]


def _recipe_tuples(extract, data) -> list[tuple]:
    """Run the recipe list-step extract and project each row to the same
    5-tuple shape, mapping ``id``→``external_id`` and ``apply_url``→``url``
    (the engine-layer mapping; mirrored here to compare at the extract layer)."""
    rows = evaluate_extract(extract, data)
    return [
        (r.get("id"), r.get("title"), r.get("location"), r.get("apply_url"), r.get("description"))
        for r in rows
    ]


def test_greenhouse_list_parity():
    """Greenhouse recipe list-step extract == ``GreenhouseAdapter.list_jobs``.

    Covers: nested ``location.name``, ``absolute_url`` apply link, the
    ``content`` field with ``html_unescape`` transform (the real Greenhouse
    list endpoint with ``?content=true`` inlines the JD body in ``content``),
    and an empty-``content`` row (description becomes ``""``).
    """
    data = {
        "jobs": [
            {
                "id": "1",
                "title": "Eng",
                "location": {"name": "SF"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                "content": "desc &amp; more",
            },
            {
                "id": "2",
                "title": "PM",
                "location": {"name": "NYC"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                "content": "",
            },
        ]
    }
    extract = _list_step_extract_rows("greenhouse")
    assert _recipe_tuples(extract, data) == _legacy_tuples(GreenhouseAdapter(), data)


def test_lever_list_parity():
    """Lever recipe list-step extract == ``LeverAdapter.list_jobs``.

    Covers: top-level list (``items_path: "$"``), ``categories.location``
    nested field, missing-key fallback for both ``applyUrl``→``hostedUrl``
    and ``descriptionPlain``→``description``, and ``html_unescape`` on the
    description.
    """
    data = [
        {
            "id": "1",
            "text": "Eng",
            "categories": {"location": "SF"},
            "applyUrl": "https://jobs.lever.co/acme/apply/1",
            "descriptionPlain": "plain &amp; text",
        },
        {
            "id": "2",
            "text": "PM",
            "categories": {"location": "NYC"},
            # applyUrl MISSING → recipe fallback chain → hostedUrl
            "hostedUrl": "https://jobs.lever.co/acme/hosted/2",
            # descriptionPlain MISSING → recipe fallback chain → description
            "description": "html &lt; text",
        },
    ]
    extract = _list_step_extract_rows("lever")
    assert _recipe_tuples(extract, data) == _legacy_tuples(LeverAdapter(), data)


def test_ashby_list_parity():
    """Ashby recipe list-step extract == ``AshbyAdapter.list_jobs``.

    Covers: ``$.jobs`` items, plain-string ``location``, the three-way
    fallback chain for both ``apply_url`` (``applyUrl``→``jobUrl``→``url``)
    and ``description`` (``descriptionPlain``→``descriptionHtml``→``description``),
    and ``html_unescape`` on the description.
    """
    data = {
        "jobs": [
            {
                "id": "1",
                "title": "Eng",
                "location": "SF",
                "applyUrl": "https://app.ashbyhq.com/acme/apply/1",
                "descriptionPlain": "plain &amp; 1",
            },
            {
                "id": "2",
                "title": "PM",
                "location": "NYC",
                # applyUrl MISSING → fallback jobUrl; descriptionPlain MISSING → descriptionHtml
                "jobUrl": "https://app.ashbyhq.com/acme/job/2",
                "descriptionHtml": "<p>html 2</p>",
            },
            {
                "id": "3",
                "title": "Dev",
                "location": "Remote",
                # applyUrl + jobUrl both MISSING → fallback url
                "url": "https://app.ashbyhq.com/acme/url/3",
                # descriptionPlain + descriptionHtml both MISSING → fallback description
                "description": "plain desc 3",
            },
        ]
    }
    extract = _list_step_extract_rows("ashby")
    assert _recipe_tuples(extract, data) == _legacy_tuples(AshbyAdapter(), data)


# ---------------------------------------------------------------------------
# Task 9 — official jsonld / sitemap / static recipe parity
#
# These recipes translate the legacy "official" adapters (JsonLdAdapter /
# SitemapAdapter / StaticHtmlAdapter) into the declarative DSL. Each recipe is
# a single ``fetch`` step; parity compares the step's ``evaluate_extract``
# output against the legacy adapter's ``list_jobs`` output on identical mock
# data, projected to the field-mapping layer both share.
#
# external_id note. json_ld and static recipes do not declare an id field —
# legacy computes the id in code (sha256 of url / title), and the DSL has no
# sha256 transform, so the engine layer (``_row_to_record``) derives the id
# from the full row instead. Parity for those two modes therefore compares
# only the field-mapping layer (title / description / location / apply_url for
# json_ld; title / location for static) and excludes external_id. Sitemap
# mode is the exception: the evaluator computes ``sha256(url)[:16]`` inline,
# identically to the legacy adapter, so sitemap parity includes external_id.
#
# RECIPES_DIR is cwd-independent (Task 8 Minor fix): ``DEFAULT_RECIPES_DIR`` is
# derived from ``m1_crawl_sink.__file__``, so the parity tests resolve the
# recipe catalog the same way the runtime sink does, regardless of the
# directory pytest was launched from.
# ---------------------------------------------------------------------------


def _official_step_extract(recipe_name: str, step_id: str = "fetch"):
    """Load ``recipes/<recipe_name>/recipe.yaml`` and return the named step's
    extract block directly (no fetch, no engine). The official recipes are
    single-step (``id: fetch``), so the default matches all three."""
    recipe = load_recipe(RECIPES_DIR / recipe_name / "recipe.yaml")
    return next(s.extract for s in recipe.steps if s.id == step_id)


def test_official_jsonld_parity():
    """official_jsonld recipe extract == ``JsonLdAdapter.list_jobs``.

    Covers:

    * the ``@['@type']=='JobPosting'`` filter drops a ``Person`` block and
      keeps ``JobPosting`` blocks;
    * nested ``jobLocation.address.addressLocality`` resolution (row 1);
    * a row missing ``jobLocation`` yields ``location == ""`` on both paths
      (the legacy adapter returns "" and the recipe's ``[location]`` fallback
      misses because no top-level ``location`` key is present);
    * the ``description`` and ``url`` (→ ``apply_url``) field mappings.

    ``external_id`` is excluded — see the module-level note.
    """
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
    extract = _official_step_extract("official_jsonld")
    recipe_rows = evaluate_extract(extract, html_doc)
    recipe_proj = [
        (r["title"], r["description"], r["location"], r["apply_url"]) for r in recipe_rows
    ]
    legacy_jobs = JsonLdAdapter().list_jobs(html_doc).jobs
    legacy_proj = [(j.title, j.description, j.location, j.url) for j in legacy_jobs]
    assert recipe_proj == legacy_proj
    # Explicit guard: the Person block was filtered out (only 2 JobPostings).
    assert len(recipe_proj) == 2


def test_official_sitemap_parity():
    """official_sitemap recipe extract == ``SitemapAdapter.list_jobs``.

    Covers:

    * the ``url_filter`` drops a non-job URL (``/about``) and keeps ``/jobs/1``
      and ``/careers/2`` (detail URLs with a trailing path segment — the form
      both the legacy ``/(jobs|careers|positions)/`` regex and the recipe's
      ``\\b(jobs|careers|positions)\\b`` word-boundary regex agree on);
    * the evaluator computes ``external_id`` as ``sha256(url)[:16]``
      identically to the legacy adapter, so it is included in the comparison;
    * sitemap mode emits ``{url, external_id}`` with no declared ``fields``;
    * the XXE guard: both legacy and recipe reject ``<!DOCTYPE``/``<!ENTITY``
      bearing input with an empty result and no raise.
    """
    xml = (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://x.com/about</loc></url>"
        "<url><loc>https://x.com/jobs/1</loc></url>"
        "<url><loc>https://x.com/careers/2</loc></url>"
        "</urlset>"
    )
    extract = _official_step_extract("official_sitemap")
    recipe_rows = evaluate_extract(extract, xml)
    recipe_proj = [(r["external_id"], r["url"]) for r in recipe_rows]
    legacy_jobs = SitemapAdapter().list_jobs(xml).jobs
    legacy_proj = [(j.external_id, j.url) for j in legacy_jobs]
    assert recipe_proj == legacy_proj

    # XXE guard parity: DOCTYPE/ENTITY-bearing input yields [] on both paths.
    evil = '<!DOCTYPE x [<!ENTITY xxe "y">]><urlset></urlset>'
    assert evaluate_extract(extract, evil) == []
    assert SitemapAdapter().list_jobs(evil).jobs == ()


def test_official_static_parity():
    """official_static recipe extract == ``StaticHtmlAdapter.list_jobs``.

    Covers: css mode zip-pairs the ``[class*=job_title]`` and
    ``[class*=job_location]`` columns by index. Mock data uses the underscore
    class-name form — the form both the legacy regex ``job[-_]?title`` and the
    css substring selector ``[class*=job_title]`` agree on. Dash-form
    (``job-title``) and concatenated (``jobtitle``) class names are a known
    recipe-refinement candidate (see recipe.yaml comment) and are out of scope
    for this field-mapping parity assertion.

    ``external_id`` is excluded — see the module-level note.
    """
    doc = (
        "<div>"
        '<h2 class="job_title">Eng</h2><span class="job_location">SF</span>'
        '<h2 class="job_title">PM</h2><span class="job_location">NYC</span>'
        "</div>"
    )
    extract = _official_step_extract("official_static")
    recipe_rows = evaluate_extract(extract, doc)
    recipe_proj = [(r["title"], r["location"]) for r in recipe_rows]
    legacy_jobs = StaticHtmlAdapter().list_jobs(doc).jobs
    legacy_proj = [(j.title, j.location) for j in legacy_jobs]
    assert recipe_proj == legacy_proj
