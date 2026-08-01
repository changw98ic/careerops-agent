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

from pathlib import Path

from careerops.adapters.job_sources import (
    AshbyAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    RawJobRecord,
)
from careerops.recipes.evaluator import evaluate_extract
from careerops.recipes.loader import load_recipe

RECIPES_DIR = Path("vendor/crawl-recipes/recipes")


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
