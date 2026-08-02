"""YAML recipe loader with static JSONPath validation.

Loading is a two-phase operation:

1. ``yaml.safe_load`` + :meth:`Recipe.model_validate` parses the document
   into the typed schema (rejects missing/ill-typed fields).
2. ``_validate_recipe_paths`` dry-parses every JSONPath expression declared
   in the recipe (``items_path``, ``when``, ``foreach``, filter expression)
   by calling :func:`jsonpath.findall` against empty data ``{}``.

The dry-parse distinguishes *syntax* errors from *no-match* results:
``jsonpath.findall("$.jobs", {})`` returns ``[]`` (valid syntax, nothing to
match in empty data) while ``jsonpath.findall("$.[broken", {})`` raises
:class:`jsonpath.JSONPathError` (specifically ``JSONPathSyntaxError``).
Only the former is a recipe-authoring error, so only it is converted into a
:class:`ValueError` that surfaces a clear, recipe-author-facing message.

``load_manifest`` reads a ``manifest.json`` that lists recipe files relative
to the recipes directory, so a whole source-type catalog can be loaded in one
call. Two safety invariants are enforced (PR #7 review fix batch 4):

* **path confinement** — each manifest ``path`` MUST be relative and MUST NOT
  escape the recipes directory (no ``..`` segments, no absolute paths). A
  malicious or malformed manifest pointing at ``../../etc/passwd`` would
  otherwise let :func:`load_recipe` read arbitrary files.
* **metadata consistency** — each manifest entry's ``source_type`` /
  ``recipe_version`` MUST match the loaded YAML's fields, so the manifest
  cannot advertise one source type while shipping a recipe for another.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import jsonpath
import yaml

from careerops.recipes.schema import Recipe

# Reused dry-parse target. An empty ``dict[str, object]`` (rather than a bare
# ``{}``) keeps pyright's ``reportUnknownArgumentType`` quiet on the
# ``findall`` call: the library is not typed, so a bare ``{`` infers as
# ``dict[Unknown, Unknown]``.
_DRY_PARSE_DATA: dict[str, object] = {}


def _validate_jsonpath(expr: str, where: str) -> None:
    """Dry-parse ``expr`` against empty data and raise ``ValueError`` on syntax errors.

    ``jsonpath.JSONPathError`` is the public base class for every error this
    library raises (syntax, index, name, type). Catching it (rather than the
    concrete ``JSONPathSyntaxError``) keeps the validator robust to whichever
    specific subclass a given malformed expression triggers.
    """

    try:
        jsonpath.findall(expr, _DRY_PARSE_DATA)  # dry-parse: valid-but-unmatched -> []
    except jsonpath.JSONPathError as exc:
        raise ValueError(f"invalid jsonpath in {where}: {expr!r}: {exc}") from exc


def _validate_recipe_paths(recipe: Recipe) -> None:
    """Run static JSONPath validation over every path-bearing field in ``recipe``."""

    for step in recipe.steps:
        if step.extract.items_path:
            _validate_jsonpath(step.extract.items_path, f"step {step.id!r} items_path")
        if step.when:
            _validate_jsonpath(step.when, f"step {step.id!r} when")
        if step.foreach:
            _validate_jsonpath(step.foreach, f"step {step.id!r} foreach")
        if step.extract.filter:
            # Wrap the inner condition in an RFC 9535 filter selector so the
            # dry-parse catches malformed filter syntax. ``filter.jsonpath``
            # holds the *condition* only (e.g. ``@.type=='JobPosting'``);
            # string literals must be single-quoted per python-jsonpath 2.2.1.
            # Evaluation semantics (applying the filter to actual item data)
            # are the executor's job in a later task.
            _validate_jsonpath(
                f"$[?({step.extract.filter.jsonpath})]",
                f"step {step.id!r} filter",
            )


def _validate_confined_path(rel_path: str, manifest_id: str) -> Path:
    """Return ``rel_path`` as a POSIX path, rejecting escapes (PR #7 fix batch 4).

    A manifest ``path`` MUST stay inside the recipes directory: no absolute
    path and no ``..`` segment that walks above the catalog root. ``Path``
    normalisation alone is not enough (``a/../../b`` resolves outside but has
    no single ``..`` prefix), so each part is checked: any ``..`` part is
    rejected outright — a legitimate in-catalog path never needs one.
    """
    p = Path(rel_path)
    if p.is_absolute():
        raise ValueError(
            f"manifest recipe path for {manifest_id!r} must be relative, "
            f"got absolute path {rel_path!r}"
        )
    if any(part == ".." for part in p.parts):
        raise ValueError(
            f"manifest recipe path for {manifest_id!r} must not escape the "
            f"recipes directory (no '..' segments), got {rel_path!r}"
        )
    return p


def _validate_manifest_entry(entry: dict[str, Any], recipe: Recipe) -> None:
    """Cross-check manifest metadata against the loaded recipe (PR #7 fix batch 4).

    The manifest advertises ``source_type`` / ``recipe_version`` per entry so
    operators can audit the catalog without parsing YAML. If those drift from
    the actual recipe, the registry key (``source_type``) and the parser
    namespace (``recipe:<source_type>:<recipe_version>``) would silently
    mislabel crawls — surface the drift at load time instead.
    """
    manifest_id = str(entry.get("id") or "<unknown>")
    declared_source_type = entry.get("source_type")
    if declared_source_type is not None and declared_source_type != recipe.source_type:
        raise ValueError(
            f"manifest entry {manifest_id!r} declares source_type "
            f"{declared_source_type!r} but recipe.yaml has {recipe.source_type!r}"
        )
    declared_version = entry.get("recipe_version")
    if declared_version is not None and str(declared_version) != recipe.recipe_version:
        raise ValueError(
            f"manifest entry {manifest_id!r} declares recipe_version "
            f"{declared_version!r} but recipe.yaml has {recipe.recipe_version!r}"
        )


def load_recipe(path: Path) -> Recipe:
    """Load and validate a single recipe YAML file at ``path``."""

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    recipe = Recipe.model_validate(data)
    _validate_recipe_paths(recipe)
    return recipe


def load_manifest(recipes_dir: Path) -> list[Recipe]:
    """Load every recipe listed in ``recipes_dir/manifest.json``.

    Enforces path confinement (relative, no ``..`` escapes) and manifest↔YAML
    metadata consistency per entry — see module docstring.
    """

    manifest_path = Path(recipes_dir) / "manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    loaded: list[Recipe] = []
    for entry in manifest.get("recipes", []):
        rel = _validate_confined_path(str(entry["path"]), str(entry.get("id") or "<unknown>"))
        recipe = load_recipe(Path(recipes_dir) / rel)
        _validate_manifest_entry(cast("dict[str, Any]", entry), recipe)
        loaded.append(recipe)
    return loaded
