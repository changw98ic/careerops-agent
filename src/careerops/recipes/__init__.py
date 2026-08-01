"""Recipe schema + YAML loader for the configurable crawl pipeline.

This package is the contract surface for Task 2 of the crawl-recipe-engine
plan: the Pydantic models describing a crawl :class:`Recipe` and the
``load_recipe`` / ``load_manifest`` entry points that read YAML from disk and
statically validate every JSONPath expression up front (syntax-only, against
empty data — a *valid but unmatched* expression returns ``[]`` and is accepted;
an *invalid* expression raises :class:`jsonpath.JSONPathError`, which the
loader re-raises as :class:`ValueError`).

Public types (re-exported here for downstream tasks): :class:`Recipe`,
:class:`Step`, :class:`Extract`, :class:`Field_`, :class:`Match`,
:class:`Fetch`, :class:`Filter`, :class:`Merge`.
"""

from __future__ import annotations

from careerops.recipes.loader import load_manifest, load_recipe
from careerops.recipes.schema import (
    Extract,
    Fetch,
    Field_,
    Filter,
    Match,
    Merge,
    Recipe,
    Step,
)

__all__ = [
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
]
