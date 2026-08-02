"""RecipeEngine (Task 6) — wires the declarative recipe (Tasks 2-5) into the
existing ``AdapterFetchResult`` contract.

The engine is the Phase A capstone: it consumes ``run_steps`` (the multi-step
evaluator from Task 4) and produces an ``AdapterFetchResult`` whose new
``status_code`` / ``body_prefix`` fields carry the first (list-step) HTTP
response signals that the backoff policy needs.

Test coverage:

* Multi-step greenhouse list→detail recipe: ``execute`` returns records with
  ``external_id`` / ``title`` resolved from the list step and ``description``
  filled from the detail step (merge), and the result carries the list step's
  HTTP status (200) and ``parser_version == "recipe:greenhouse:1"``.
* First-step signal correctness: when the detail step returns a different
  status (e.g. 202), the engine still reports the list step's status — only
  the first fetch becomes the signal of record.
* ``request=None`` is None-safe (the injected ``fetch`` carries all context).
* Single-step recipe path (no detail follow-up).
* ``detect`` for ``url_patterns`` (``{slug}`` substring match) and
  ``host_suffix``.
* ``_row_to_record`` external_id fallback (sha256 of the row) and apply_url
  preference over url.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import yaml

from careerops.adapters.job_sources import AdapterFetchResult
from careerops.recipes.engine import RecipeEngine
from careerops.recipes.schema import Match, Recipe


class FakeResp:
    """Stand-in for ``FetchedResponse`` (the body/status/final_url triple).

    ``final_url`` defaults to the list URL; detail-step tests MUST override it
    to a distinct URL so any regression that leaks a detail URL into
    ``result.source_url`` is caught (see test_execute_multistep_*).
    """

    def __init__(self, body: str, status: int = 200, final_url: str = "https://x/jobs") -> None:
        self.body = body
        self.status_code = status
        self.fetched_at = datetime(2026, 1, 1)
        self.final_url = final_url


MULTISTEP_GREENHOUSE_YAML = """
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["https://boards-api.greenhouse.io/v1/boards/{slug}"]}
steps:
  - id: list
    fetch:
      endpoint: "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    extract: {mode: json_path, items_path: "$.jobs",
      fields: {id: {path: id}, title: {path: title}, description: {path: description}}}
  - id: detail
    when: "$.steps.list[?(@.description=='')]"
    foreach: "$.steps.list[?(@.description=='')]"
    fetch: {endpoint: "{list_endpoint}/{id}"}
    extract: {mode: json_path, fields: {description: {path: content}}}
    merge: {strategy: overwrite_empty}
"""


SINGLESTEP_YAML = """
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["x"]}
steps:
  - id: list
    fetch: {endpoint: "https://x/jobs"}
    extract: {mode: json_path, items_path: "$.jobs",
      fields: {id: {path: id}, title: {path: title}}}
"""


def _run(coro):
    return asyncio.run(coro)


def test_execute_multistep_returns_records_and_signals():
    """List→detail recipe: list rows become records with merged detail fields,
    and the result carries the list step's status_code + parser_version +
    source_url.

    The detail FakeResp gets a DISTINCT final_url so any regression that
    leaks a detail URL into ``result.source_url`` (the original Important
    bug — ``final_url`` was assigned on every fetch, not just the first) is
    caught directly here.
    """
    r = Recipe.model_validate(yaml.safe_load(MULTISTEP_GREENHOUSE_YAML))
    eng = RecipeEngine(r)
    list_json = '{"jobs": [{"id": "1", "title": "Eng", "description": ""}]}'
    detail_json = '{"content": "filled-by-detail"}'
    list_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    detail_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1"

    def fetch(endpoint):
        if endpoint.endswith("/1"):
            # detail response carries its OWN final_url + status — both must
            # be ignored for the result-level signals.
            return FakeResp(detail_json, status=202, final_url=detail_url)
        return FakeResp(list_json, status=200, final_url=list_url)

    result = _run(eng.execute(request=None, fetch=fetch))
    assert isinstance(result, AdapterFetchResult)
    assert len(result.jobs) == 1
    assert result.jobs[0].external_id == "1"
    assert result.jobs[0].title == "Eng"
    # detail step filled the empty description via overwrite_empty merge
    assert result.jobs[0].description == "filled-by-detail"
    assert result.status_code == 200
    assert result.parser_version == "recipe:greenhouse:1"
    # body_prefix carries the list step's raw body (bounded to 4 KiB)
    assert '"jobs"' in result.body_prefix
    # source_url is the LIST step's final_url — NOT the detail URL.
    # This is the regression guard for the first-step-signal consistency.
    assert result.source_url == list_url
    assert result.source_url != detail_url


def test_execute_uses_first_step_status_when_detail_returns_other_status():
    """Only the first (list) fetch becomes the signal of record. A detail step
    returning 202 with its own final_url must NOT overwrite the list step's
    200 / list URL."""
    r = Recipe.model_validate(yaml.safe_load(MULTISTEP_GREENHOUSE_YAML))
    eng = RecipeEngine(r)
    list_json = '{"jobs": [{"id": "1", "title": "A", "description": ""}]}'
    detail_json = '{"content": "x"}'
    list_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"

    def fetch(endpoint):
        if endpoint.endswith("/1"):
            return FakeResp(detail_json, status=202, final_url=list_url + "/1")
        return FakeResp(list_json, status=200, final_url=list_url)

    result = _run(eng.execute(request=None, fetch=fetch))
    assert result.status_code == 200  # list step wins, not 202
    assert result.source_url == list_url  # list URL wins, not detail URL
    # body_prefix also comes from the first fetch
    assert '"jobs"' in result.body_prefix


def test_execute_request_none_is_safe():
    """``request=None`` must not raise — the injected fetch carries all context
    the engine needs (slug template vars resolve to literal ``{slug}`` via the
    SafeDict preserve rule, but a recipe with no templating works cleanly)."""
    r = Recipe.model_validate(yaml.safe_load(SINGLESTEP_YAML))
    eng = RecipeEngine(r)

    def fetch(endpoint):
        return FakeResp('{"jobs": [{"id": "7", "title": "T"}]}')

    result = _run(eng.execute(request=None, fetch=fetch))
    assert result.jobs[0].external_id == "7"
    assert result.jobs[0].title == "T"
    assert result.status_code == 200


def test_execute_single_step_recipe():
    """A recipe with one step still produces a valid result with signals."""
    r = Recipe.model_validate(yaml.safe_load(SINGLESTEP_YAML))
    eng = RecipeEngine(r)

    def fetch(endpoint):
        return FakeResp('{"jobs": [{"id": "1", "title": "Only"}]}')

    result = _run(eng.execute(request=None, fetch=fetch))
    assert len(result.jobs) == 1
    assert result.jobs[0].title == "Only"
    assert result.parser_version == "recipe:greenhouse:1"
    assert result.status_code == 200


def test_source_type_and_parser_version_properties():
    r = Recipe.model_validate(yaml.safe_load(SINGLESTEP_YAML))
    eng = RecipeEngine(r)
    assert eng.source_type == "greenhouse"
    assert eng.parser_version == "recipe:greenhouse:1"


def test_detect_url_pattern_substring_with_slug_placeholder():
    """``url_patterns`` containing ``{slug}`` match by the substring before
    ``{`` — so any base_url containing the board path prefix is detected."""
    r = Recipe.model_validate(yaml.safe_load(MULTISTEP_GREENHOUSE_YAML))
    eng = RecipeEngine(r)
    assert eng.detect("https://boards-api.greenhouse.io/v1/boards/stripe/jobs") is True
    assert eng.detect("https://api.lever.co/v0/postings/acme") is False


def test_detect_host_suffix():
    r = Recipe(
        recipe_version="1",
        source_type="lever",
        match=Match(host_suffix="lever.co"),
        steps=[],
    )
    eng = RecipeEngine(r)
    assert eng.detect("https://api.lever.co") is True
    assert eng.detect("https://boards-api.greenhouse.io") is False


def test_row_to_record_external_id_fallback_when_missing():
    """A row with no id/external_id gets a deterministic sha256[:16] id."""
    from careerops.recipes.engine import _row_to_record

    row = {"title": "NoId"}
    rec = _row_to_record(row)
    # 16 hex chars; stable for the same row content
    assert len(rec.external_id) == 16
    assert rec.title == "NoId"


def test_row_to_record_id_wins_over_external_id():
    """When both ``id`` and ``external_id`` are present, ``id`` wins (it is the
    first term in the resolution chain ``id -> external_id -> sha256``)."""
    from careerops.recipes.engine import _row_to_record

    rec = _row_to_record({"id": "primary", "external_id": "alt"})
    assert rec.external_id == "primary"


def test_row_to_record_external_id_used_when_id_missing():
    """When ``id`` is absent, ``external_id`` is the next candidate before the
    sha256 fallback."""
    from careerops.recipes.engine import _row_to_record

    rec = _row_to_record({"external_id": "alt"})
    assert rec.external_id == "alt"


def test_row_to_record_apply_url_preferred_over_url():
    from careerops.recipes.engine import _row_to_record

    rec = _row_to_record({"id": "1", "apply_url": "https://apply", "url": "https://canonical"})
    assert rec.url == "https://apply"


def test_row_to_record_raw_data_excludes_internal_fields():
    from careerops.recipes.engine import _row_to_record

    rec = _row_to_record(
        {
            "id": "1",
            "external_id": "ext",
            "title": "T",
            "location": "SF",
            "description": "D",
            "apply_url": "https://a",
            "url": "https://u",
            "department": "Eng",
            "extra": 42,
        }
    )
    # internal fields are NOT in raw_data
    assert "id" not in rec.raw_data
    assert "external_id" not in rec.raw_data
    assert "title" not in rec.raw_data
    assert "apply_url" not in rec.raw_data
    # non-internal fields ARE preserved
    assert rec.raw_data == {"department": "Eng", "extra": 42}
