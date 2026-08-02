"""Pydantic models for crawl recipes.

A recipe is the declarative description of how to crawl one ATS source type:
where to fetch, how to extract structured job fields from the response, and
how to merge multi-step results. The shape is intentionally permissive
(strings rather than enums for source identifiers) so new ATS sources can be
onboarded by authoring YAML rather than editing code; only the fields that
have a fixed vocabulary (``executor_mode``, extract ``mode``, ``transform``,
``fallback``) are constrained with ``Literal``.

The class name ``Field_`` (trailing underscore) avoids shadowing
:func:`pydantic.Field` which is used to declare defaults on the same module.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Match(BaseModel):
    """URL routing match for a recipe."""

    url_patterns: list[str] = Field(default_factory=list)
    host_suffix: str = ""


class Fetch(BaseModel):
    """HTTP fetch descriptor for a step.

    Tier 1 recipes are GET-only against public JSON / HTML / XML endpoints,
    and the recipe engine itself never opens a socket — the injected
    ``fetch_one`` callable carries all per-request HTTP context (auth,
    session, rate-limit state). ``method`` / ``headers`` / ``body`` /
    ``pagination`` therefore have no executor wiring on this path: POST and
    cursor pagination are Tier 2 concerns handled by the ``CrawlAgent``
    skill playbook (see ``vendor/crawl-recipes/skills/workday``), not the
    declarative recipe. They were declared on the schema but never read by
    ``fetch_one`` (which only takes the endpoint), so declaring them here
    was misleading — a recipe setting ``method: POST`` would silently be
    fetched as GET. Removed per YAGNI; reintroducing them is a Phase B
    decision when a Tier 1 source genuinely needs them and the executor
    gains the wiring.
    """

    endpoint: str


class Field_(BaseModel):
    """A single extracted field within an :class:`Extract` block.

    ``path`` is interpreted by the executor relative to each matched item
    (e.g. ``"title"`` resolves to ``item["title"]`` for json_path mode); it is
    intentionally *not* a full JSONPath expression and is therefore not
    statically validated by the loader. ``fallback`` paths are tried in order
    when ``path`` misses. ``transform`` is applied to the resolved value
    before it is written into the posting record.
    """

    model_config = {"populate_by_name": True}
    path: str
    fallback: list[str] = Field(default_factory=list)
    transform: Literal["html_unescape", "none"] = "none"


class Filter(BaseModel):
    """A JSONPath filter condition applied to extracted items.

    ``jsonpath`` holds the *condition* only (the fragment that sits inside an
    RFC 9535 filter selector). ``@`` refers to the current item; the executor
    wraps the condition as ``$[?({filter.jsonpath})]`` and evaluates it
    against ``[item]`` (single-item array — RFC 9535 filters only apply over
    arrays). String literals must be **single-quoted** (python-jsonpath 2.2.1
    raises ``JSONPathSyntaxError`` on bareword identifiers).

    Key access caveat (verified against python-jsonpath 2.2.1): ``@.type``
    accesses the literal key ``type``. To reach a key with a reserved
    character prefix such as schema.org's ``@type``, use the bracket form
    ``@['@type']``. Example: ``@['@type']=='JobPosting'``.
    """

    jsonpath: str  # condition, e.g. "@['@type']=='JobPosting'"; wrapped as $[?({...})]


class Extract(BaseModel):
    """Extraction strategy for a step's response body."""

    mode: Literal["json_path", "json_ld", "sitemap", "css"]
    items_path: str | None = None
    fields: dict[str, Field_] = Field(default_factory=dict)
    filter: Filter | None = None
    url_filter: str | None = None  # sitemap only


class Merge(BaseModel):
    """How multi-step field values are merged into one posting."""

    strategy: Literal["overwrite_empty", "overwrite_all", "keep_first"] = "overwrite_empty"


class Step(BaseModel):
    """A single fetch+extract stage within a recipe."""

    id: str
    when: str | None = None
    foreach: str | None = None
    fetch: Fetch
    extract: Extract
    merge: Merge | None = None


class Recipe(BaseModel):
    """A complete crawl recipe for one ATS source type."""

    recipe_version: str
    source_type: str
    executor_mode: Literal["http", "ego"] = "http"
    match: Match
    steps: list[Step]
    rate_limit: dict[str, int] = Field(default_factory=dict)
    fallback: Literal["llm_skill", "none"] = "llm_skill"
