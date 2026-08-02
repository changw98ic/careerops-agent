"""Loader behaviour tests beyond the two baseline contracts in test_schema.

Covers:

* ``load_manifest`` walks a ``manifest.json`` and returns each referenced
  recipe in declaration order.
* A *valid-but-unmatched* JSONPath (``$.jobs`` against empty data) is
  accepted — only syntactic breakage is rejected, matching
  ``jsonpath.findall`` semantics.
* ``when`` / ``foreach`` expressions are statically validated the same way
  ``items_path`` is.
* Schema-level malformations surface as pydantic ``ValidationError`` (which
  is itself a ``ValueError`` subclass, so callers catching ``ValueError``
  still handle it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from careerops.recipes.loader import load_manifest, load_recipe
from careerops.recipes.schema import Recipe


def _write_recipe(path: Path, items_path: str = "$.jobs") -> None:
    path.write_text(
        f"""
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {{url_patterns: ["x"]}}
steps:
  - id: list
    fetch: {{endpoint: "https://x/{{slug}}"}}
    extract: {{mode: json_path, items_path: "{items_path}",
      fields: {{title: {{path: title}}}}}}
""",
        encoding="utf-8",
    )


def test_load_manifest_reads_multiple_recipes(tmp_path):
    _write_recipe(tmp_path / "greenhouse.yaml")
    _write_recipe(tmp_path / "lever.yaml", items_path="$.postings")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "recipes": [
                    {"path": "greenhouse.yaml"},
                    {"path": "lever.yaml"},
                ]
            }
        ),
        encoding="utf-8",
    )

    recipes = load_manifest(tmp_path)

    assert len(recipes) == 2
    assert all(isinstance(r, Recipe) for r in recipes)
    # Order follows the manifest, not the filesystem.
    assert recipes[0].steps[0].extract.items_path == "$.jobs"
    assert recipes[1].steps[0].extract.items_path == "$.postings"


def test_load_manifest_empty_recipes_key(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"recipes": []}), encoding="utf-8")
    assert load_manifest(tmp_path) == []


# ---------------------------------------------------------------------------
# Manifest safety: path confinement + metadata consistency (PR #7 fix batch 4)
# ---------------------------------------------------------------------------


def test_load_manifest_rejects_dotdot_path(tmp_path):
    """A manifest path containing ``..`` must be rejected — it could escape
    the recipes directory and read arbitrary files via :func:`load_recipe`."""
    (tmp_path / "manifest.json").write_text(
        json.dumps({"recipes": [{"id": "evil", "path": "../../etc/passwd"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must not escape"):
        load_manifest(tmp_path)


def test_load_manifest_rejects_absolute_path(tmp_path):
    """Absolute manifest paths are rejected (path must be relative)."""
    (tmp_path / "manifest.json").write_text(
        json.dumps({"recipes": [{"id": "evil", "path": "/etc/passwd"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be relative"):
        load_manifest(tmp_path)


def test_load_manifest_rejects_source_type_mismatch(tmp_path):
    """The manifest's declared ``source_type`` must match the recipe's actual
    ``source_type`` — otherwise the registry key silently mislabels crawls."""
    _write_recipe(tmp_path / "r.yaml")  # recipe has source_type: greenhouse
    (tmp_path / "manifest.json").write_text(
        json.dumps({"recipes": [{"id": "r", "path": "r.yaml", "source_type": "lever"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="declares source_type 'lever'"):
        load_manifest(tmp_path)


def test_load_manifest_rejects_recipe_version_mismatch(tmp_path):
    """The manifest's declared ``recipe_version`` must match the recipe's."""
    _write_recipe(tmp_path / "r.yaml")  # recipe_version: "1"
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "recipes": [
                    {
                        "id": "r",
                        "path": "r.yaml",
                        "source_type": "greenhouse",
                        "recipe_version": "9",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="declares recipe_version"):
        load_manifest(tmp_path)


def test_load_manifest_allows_consistent_metadata(tmp_path):
    """When the manifest metadata matches the YAML, loading succeeds (the
    consistency check is a guard, not a stricter requirement)."""
    _write_recipe(tmp_path / "r.yaml")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "recipes": [
                    {
                        "id": "r",
                        "path": "r.yaml",
                        "source_type": "greenhouse",
                        "recipe_version": "1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    recipes = load_manifest(tmp_path)
    assert len(recipes) == 1
    assert recipes[0].source_type == "greenhouse"


def test_valid_jsonpath_with_no_match_is_accepted(tmp_path):
    # ``$.jobs[*].title`` is well-formed; against empty data it yields [].
    # The loader must NOT reject it (only syntactic breakage is rejected).
    p = tmp_path / "r.yaml"
    p.write_text(
        """
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["x"]}
steps:
  - id: list
    fetch: {endpoint: "x"}
    extract: {mode: json_path, items_path: "$.jobs[*].title",
      fields: {title: {path: title}}}
""",
        encoding="utf-8",
    )
    r = load_recipe(p)
    assert r.steps[0].extract.items_path == "$.jobs[*].title"


def test_when_and_foreach_are_validated(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(
        """
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["x"]}
steps:
  - id: list
    when: "$.[broken-when"
    foreach: "$.boards[*]"
    fetch: {endpoint: "x"}
    extract: {mode: json_path, items_path: "$.jobs",
      fields: {title: {path: title}}}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="jsonpath"):
        load_recipe(p)


def test_foreach_invalid_is_rejected(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(
        """
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["x"]}
steps:
  - id: list
    foreach: "$.[nope"
    fetch: {endpoint: "x"}
    extract: {mode: json_path, items_path: "$.jobs",
      fields: {title: {path: title}}}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="jsonpath"):
        load_recipe(p)


def test_missing_required_field_raises_validation_error(tmp_path):
    # No `fetch` block -> pydantic ValidationError. ValidationError is a
    # ValueError subclass, so callers using `pytest.raises(ValueError)` are
    # covered either way; here we assert the precise type for clarity.
    p = tmp_path / "r.yaml"
    p.write_text(
        """
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["x"]}
steps:
  - id: list
    extract: {mode: json_path, items_path: "$.jobs",
      fields: {title: {path: title}}}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_recipe(p)


def _write_recipe_with_filter(path: Path, filter_expr: str) -> None:
    path.write_text(
        f"""
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {{url_patterns: ["x"]}}
steps:
  - id: list
    fetch: {{endpoint: "https://x/{{slug}}"}}
    extract:
      mode: json_path
      items_path: "$.jobs"
      filter: {{jsonpath: "{filter_expr}"}}
      fields: {{title: {{path: title}}}}
""",
        encoding="utf-8",
    )


def test_filter_with_quoted_literal_is_accepted(tmp_path):
    # Canonical RFC 9535 condition: '@' = current item, single-quoted string
    # literal. The loader wraps as $[?(@.type=='JobPosting')] for the dry-parse;
    # against empty data that yields [] (valid syntax), so the recipe loads.
    p = tmp_path / "r.yaml"
    _write_recipe_with_filter(p, "@.type=='JobPosting'")
    r = load_recipe(p)
    assert r.steps[0].extract.filter is not None
    assert r.steps[0].extract.filter.jsonpath == "@.type=='JobPosting'"


@pytest.mark.parametrize(
    "bad_filter",
    [
        # Missing right-hand operand.
        "@.type==",
        # Unquoted string literal: python-jsonpath 2.2.1 rejects bare
        # identifiers after ==; the brief's original example.
        "@.type==JobPosting",
        # Empty filter body once wrapped -> $[?()].
        "",
    ],
    ids=["missing-rhs", "unquoted-literal", "empty-condition"],
)
def test_filter_invalid_condition_is_rejected(tmp_path, bad_filter):
    p = tmp_path / "r.yaml"
    _write_recipe_with_filter(p, bad_filter)
    with pytest.raises(ValueError, match="jsonpath"):
        load_recipe(p)


def test_package_reexports_public_api():
    # The public types are re-exported from careerops.recipes so downstream
    # tasks can `from careerops.recipes import Recipe, load_recipe`. Importing
    # the module is itself the contract test; aliases keep the bare names that
    # ruff's organize-imports would otherwise reorder away.
    import careerops.recipes as pkg

    for name in (
        "Extract",
        "Fetch",
        "Field_",
        "Filter",
        "Match",
        "Merge",
        "Recipe",
        "Step",
        "load_manifest",
        "load_recipe",
    ):
        assert hasattr(pkg, name), f"careerops.recipes is missing re-export: {name}"
        assert getattr(pkg, name).__name__ == name or callable(getattr(pkg, name))

    assert pkg.Recipe is Recipe
    assert callable(pkg.load_recipe)
    assert callable(pkg.load_manifest)
