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
call.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonpath
import yaml

from careerops.recipes.schema import Recipe


def _validate_jsonpath(expr: str, where: str) -> None:
    """Dry-parse ``expr`` against empty data and raise ``ValueError`` on syntax errors.

    ``jsonpath.JSONPathError`` is the public base class for every error this
    library raises (syntax, index, name, type). Catching it (rather than the
    concrete ``JSONPathSyntaxError``) keeps the validator robust to whichever
    specific subclass a given malformed expression triggers.
    """

    try:
        jsonpath.findall(expr, {})  # dry-parse: valid-but-unmatched -> []
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


def load_recipe(path: Path) -> Recipe:
    """Load and validate a single recipe YAML file at ``path``."""

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    recipe = Recipe.model_validate(data)
    _validate_recipe_paths(recipe)
    return recipe


def load_manifest(recipes_dir: Path) -> list[Recipe]:
    """Load every recipe listed in ``recipes_dir/manifest.json``."""

    manifest_path = Path(recipes_dir) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    loaded: list[Recipe] = []
    for entry in manifest.get("recipes", []):
        loaded.append(load_recipe(Path(recipes_dir) / entry["path"]))
    return loaded
