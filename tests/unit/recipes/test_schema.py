"""Schema + loader smoke tests for crawl recipes (Task 2 baseline contracts).

These two tests are the load-bearing contract from the Task 2 brief: a
minimal well-formed recipe round-trips through ``load_recipe`` with the
expected field values, and a recipe containing a syntactically broken
JSONPath ``items_path`` is rejected with a ``ValueError`` whose message
mentions ``jsonpath`` so authors can diagnose it.
"""

from __future__ import annotations

import pytest

from careerops.recipes.loader import load_recipe
from careerops.recipes.schema import Recipe


def test_recipe_loads_minimal(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(
        """
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["boards.greenhouse.io/{slug}"]}
steps:
  - id: list
    fetch: {endpoint: "https://x/{slug}", method: GET}
    extract: {mode: json_path, items_path: "$.jobs",
      fields: {title: {path: title}, id: {path: id}}}
""",
        encoding="utf-8",
    )
    r = load_recipe(p)
    # Imported type is re-exported identically from the package root.
    assert isinstance(r, Recipe)
    assert r.source_type == "greenhouse"
    assert r.executor_mode == "http"
    assert r.match.url_patterns == ["boards.greenhouse.io/{slug}"]
    assert r.steps[0].id == "list"
    assert r.steps[0].fetch.method == "GET"
    assert r.steps[0].extract.mode == "json_path"
    assert r.steps[0].extract.items_path == "$.jobs"
    assert r.steps[0].extract.fields["title"].path == "title"
    assert r.steps[0].extract.fields["id"].path == "id"
    # Field_ defaults are populated as designed.
    assert r.steps[0].extract.fields["title"].fallback == []
    assert r.steps[0].extract.fields["title"].transform == "none"


def test_invalid_jsonpath_rejected(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(
        """
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["x"]}
steps:
  - id: list
    fetch: {endpoint: "x", method: GET}
    extract: {mode: json_path, items_path: "$.[broken",
      fields: {title: {path: title}}}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="jsonpath"):
        load_recipe(p)
