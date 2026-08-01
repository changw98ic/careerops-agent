"""Recipe schema + YAML/Markdown loaders for the configurable crawl pipeline.

This package is the contract surface for two layers of the crawl-recipe-engine
plan:

* Task 2 — the Pydantic models describing a declarative crawl :class:`Recipe`
  (Tier 1) and the ``load_recipe`` / ``load_manifest`` entry points that read
  YAML from disk and statically validate every JSONPath expression up front
  (syntax-only, against empty data — a *valid but unmatched* expression
  returns ``[]`` and is accepted; an *invalid* expression raises
  :class:`jsonpath.JSONPathError`, which the loader re-raises as
  :class:`ValueError`).
* Task 12 — the Tier 2 :class:`Skill` playbook loader (``load_skill`` /
  ``load_skills``) for sites too dynamic for a declarative recipe. Skills are
  Markdown + YAML frontmatter; the loader only parses the format, wiring into
  the CrawlAgent loop is a later task.

Public types (re-exported here for downstream tasks): :class:`Recipe`,
:class:`Step`, :class:`Extract`, :class:`Field_`, :class:`Match`,
:class:`Fetch`, :class:`Filter`, :class:`Merge`, :class:`Skill`.
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
from careerops.recipes.skill_loader import Skill, load_skill, load_skills

__all__ = [
    "Extract",
    "Fetch",
    "Field_",
    "Filter",
    "Match",
    "Merge",
    "Recipe",
    "Skill",
    "Step",
    "load_manifest",
    "load_recipe",
    "load_skill",
    "load_skills",
]
