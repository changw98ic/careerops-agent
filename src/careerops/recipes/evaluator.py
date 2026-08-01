"""Recipe evaluator: single-step extract (Task 3) and multi-step orchestration
(Task 4 — ``steps`` / ``when`` / ``foreach`` / ``merge``).

This module is the run-time counterpart of the declarative schema in
:mod:`careerops.recipes.schema`. It takes declarative blocks and parsed HTTP
responses and produces flat ``field -> value`` dicts ("rows"), one per matched
item. Each row is the unit the crawl pipeline later merges into a posting
record.

Public entry points:

* :func:`evaluate_extract` — single-step: locate the item list via
  ``extract.items_path`` (a JSONPath evaluated against ``data``), then resolve
  every declared field against each item.
* :func:`apply_field` — resolve a single :class:`Field_` against an item: walk
  ``[field.path, *field.fallback]`` in order and return the first non-empty
  JSONPath match (stringified), optionally unescaped.
* :func:`run_steps` — multi-step: drive a recipe's ``steps`` in order with
  ``when`` gating, ``foreach`` per-item fetching and ``merge`` strategies.

Non-``json_path`` modes are out of scope for Tasks 3/4 and raise
:class:`NotImplementedError`; they arrive with the json_ld / sitemap / css
executors in later tasks.

Filter evaluation (``extract.filter``) is deferred to Task 5. RFC 9535
filters ``$[?(...)]`` only apply over *arrays* — applied to a single object
item, python-jsonpath 2.2.1 returns ``[]`` (verified). The per-item
evaluation semantics plus the ``?()``-correct wrapping are intertwined with
json_ld mode, where filter is a core feature, so both are solved together in
Task 5. ``evaluate_extract`` in json_path mode therefore ignores
``extract.filter`` today.
"""

from __future__ import annotations

import html
import logging
from urllib.parse import urlsplit, urlunsplit

import jsonpath

from careerops.recipes.schema import Extract, Field_

logger = logging.getLogger(__name__)


def apply_field(field: Field_, item: object) -> str:
    """Resolve a single :class:`Field_` against ``item`` to a string.

    Walks the candidate chain ``[field.path, *field.fallback]`` in order; for
    each candidate evaluates ``jsonpath.findall(f"$.{candidate}", item)`` and
    takes the first match of the *first* candidate that returns a non-empty
    list. Missing candidates yield ``[]`` and are skipped. When every
    candidate misses the result is ``""``.

    Non-string scalars (numbers / bools) are coerced via ``str()`` so every
    row value is text. Non-dict items (scalars, lists, ``None``) have no
    extractable fields and return ``""``.

    When ``field.transform == "html_unescape"`` the resolved non-empty string
    is passed through :func:`html.unescape`; the ``"none"`` transform (the
    schema default) is a no-op.
    """
    if not isinstance(item, dict):
        return ""

    candidates = [field.path, *field.fallback]
    raw: object = None
    for candidate in candidates:
        values = jsonpath.findall(f"$.{candidate}", item)
        if values:
            raw = values[0]
            break

    text = "" if raw is None else str(raw)
    if field.transform == "html_unescape" and text:
        text = html.unescape(text)
    return text


def evaluate_extract(extract: Extract, data: object) -> list[dict]:
    """Resolve an :class:`Extract` block against ``data`` into rows.

    Only ``json_path`` mode is implemented in Task 3. ``items_path`` is
    evaluated against ``data``; each matched object becomes one row whose
    keys are the declared field names and whose values come from
    :func:`apply_field`. A valid-but-unmatched ``items_path`` yields ``[]``.

    ``extract.filter`` is ignored in json_path mode — filter evaluation is
    deferred to Task 5 (single-item array semantics + ``?()`` wrapping);
    see module docstring.
    """
    if extract.mode != "json_path":
        raise NotImplementedError(extract.mode)

    # filter evaluation deferred to Task 5 (single-item array semantics + ?() wrapping)
    if not isinstance(data, (dict, list)):
        return []
    items_path = extract.items_path or "$"
    matched = jsonpath.findall(items_path, data)

    # ``items_path`` semantics: it points AT the collection of items, not into
    # it. ``$.jobs`` therefore resolves to a single matched node whose *value*
    # is the array, so ``findall`` returns ``[[a, b]]`` rather than ``[a, b]``.
    # When we get exactly one list-valued match, that list IS the item array
    # and we unwrap it. The ``$.jobs[*]`` form (already a flat list of dicts)
    # and a top-level array (``$`` against ``[a, b]``) are both handled too.
    # Verified against python-jsonpath 2.2.1.
    if len(matched) == 1 and isinstance(matched[0], list):
        items = matched[0]
    else:
        items = matched

    rows: list[dict] = []
    for item in items:
        row = {name: apply_field(field, item) for name, field in extract.fields.items()}
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Multi-step orchestration (Task 4)
#
# The greenhouse list→detail pattern is the canonical two-hop crawl: a list
# step returns job cards whose ``description`` is empty, and a detail step
# fans out one fetch per card to fill the missing field. ``run_steps`` models
# this declaratively so a recipe YAML — not code — drives the orchestration.
#
# JSONPath return shapes (verified against python-jsonpath 2.2.1, the version
# pinned in pyproject.toml):
#
#   expr                              findall result
#   --------------------------------  -----------------------------------
#   ``$.steps.list``                  ``[[a, b]]``  (single-node array value)
#   ``$.steps.list[*]``               ``[a, b]``    (flat, references)
#   ``$.steps.list[?(@.k=='')]``      ``[a]``       (flat, references, doc order)
#   no match                          ``[]``
#
# Two consequences:
# 1. ``findall`` returns *references* to the underlying dicts, so mutating a
#    foreach target mutates the canonical row stored under
#    ``ns["steps"][<source_step>]`` — no key scan needed for merge.
# 2. The ``$.steps.list`` (bare-path) form needs unwrapping to be iterable as
#    items; :func:`_eval_pathlist` applies the same single-list-node unwrap
#    rule as :func:`evaluate_extract` so both forms work as ``when``/``foreach``
#    expressions.
# ---------------------------------------------------------------------------


class _SafeDict(dict):
    """``str.format_map`` mapping that preserves unknown ``{keys}`` literally.

    Used for endpoint templating: a detail step referencing ``{list_endpoint}``
    before the list step has run should not raise; it should leave the
    placeholder so the miss is visible in the rendered URL.
    """

    def __missing__(self, key: str) -> str:  # pragma: no cover - trivial
        return "{" + key + "}"


def _render(template: str, ctx: dict) -> str:
    """Render ``template`` via ``str.format_map`` with missing keys preserved."""
    return template.format_map(_SafeDict(ctx))


def _eval_pathlist(expr: str, ns: dict) -> list:
    """``jsonpath.findall`` + single-list-node unwrap.

    ``$.steps.list`` resolves to one node whose *value* is the array, so
    ``findall`` returns ``[[a, b]]`` rather than ``[a, b]``. The filter form
    (``$.steps.list[?(...)]``) and wildcard form (``$.steps.list[*]``) already
    return a flat list of items. The unwrap makes both forms iterable as item
    lists. Verified against python-jsonpath 2.2.1.
    """
    matched = jsonpath.findall(expr, ns)
    if len(matched) == 1 and isinstance(matched[0], list):
        return matched[0]
    return matched


def _merge_into(parent_row: dict, detail_rows: list[dict], strategy: str) -> None:
    """Merge ``detail_rows`` into ``parent_row`` per ``strategy`` (in place).

    ``parent_row`` is a reference to a row inside a prior step's output
    (``jsonpath.findall`` returns node references), so in-place mutation
    updates the canonical row callers see through ``ns["steps"][<src>]``.

    Strategies:

    * ``overwrite_empty`` (default): only fill fields whose current value is
      falsy (``""`` / ``None`` / ``0``). This mirrors the greenhouse
      detail-cache: the list value wins whenever it was already present.
    * ``overwrite_all``: replace every field the detail step provides.
    * ``keep_first``: only set keys that are *absent* from ``parent_row``;
      an existing key wins even if its value is empty.
    """
    for d in detail_rows:
        for k, v in d.items():
            if strategy == "overwrite_all":
                parent_row[k] = v
            elif strategy == "keep_first":
                if k not in parent_row:
                    parent_row[k] = v
            else:  # overwrite_empty (and any unknown → safe default)
                if not parent_row.get(k):
                    parent_row[k] = v


def run_steps(steps, fetch_one, base_ns) -> dict:
    """Execute a recipe's ``steps`` in order, returning ``{step_id: [rows]}``.

    ``fetch_one(endpoint) -> object`` is the injected fetcher (returns the
    parsed JSON response). ``base_ns`` carries template variables such as
    ``{slug}`` and optionally ``{list_endpoint}``; it is copied, not mutated.

    Per-step semantics:

    * ``when`` (step-level gate): evaluate ``step.when`` against the namespace;
      an empty match list skips the step entirely (its slot becomes ``[]`` and
      no fetch is issued). This is the short-circuit that prevents detail
      fetches when every list row already has the field filled.
    * ``foreach`` (per-item fan-out): evaluate ``step.foreach`` against the
      namespace and iterate the matched items **in document order**, one fetch
      per item. The item's own fields are exposed to endpoint templating so
      ``{id}`` resolves to ``item["id"]``.
    * ``merge``: each per-item extract's rows are merged into the matching
      parent row (located by jsonpath reference identity) using the strategy.
    * **per-item fault tolerance** (foreach only): a single detail fetch or
      extract failure is logged and skipped — the failed item emits no row
      and is not merged (its parent list row keeps its current value), then
      iteration continues. This preserves the product contract that list
      data remains useful when a detail fetch fails. List-step fetches
      (non-foreach branch) are NOT guarded: a list fetch failure means the
      run has no primary data, so the exception propagates.

    After a non-foreach step runs, the namespace publishes ``list_endpoint``
    derived from that step's endpoint with the query string stripped — this is
    the detail-step analogue of the legacy
    ``_fetch_greenhouse_details`` URL-derivation (the path that produced
    ``https://boards-api.greenhouse.io/v1/boards/<slug>/jobs`` from the list
    URL by dropping ``?content=true``).
    """
    ns: dict = {"steps": {}}
    ctx = dict(base_ns)
    for step in steps:
        rows: list[dict] = []
        if step.when is not None and not _eval_pathlist(step.when, ns):
            ns["steps"][step.id] = rows
            continue
        if step.foreach is not None:
            targets = _eval_pathlist(step.foreach, ns)
            for item in targets:
                local = dict(ctx)
                if isinstance(item, dict):
                    local.update({k: str(v) for k, v in item.items()})
                ep = _render(step.fetch.endpoint, local)
                # Per-item fault tolerance: a single detail fetch failure must
                # not sink the whole run. Skip the failed item (no row emit,
                # no merge — the parent list row keeps its current value, e.g.
                # empty description) and continue to the next item. This
                # mirrors the legacy m1_crawl_sink contract: "List data
                # remains useful on detail failure." List-step fetches (the
                # ``else`` branch below) are NOT wrapped — a list fetch
                # failure means the run has no primary data and should
                # propagate.
                try:
                    data = fetch_one(ep)
                    drows = evaluate_extract(step.extract, data)
                except Exception as exc:  # broad guard: see comment above
                    logger.warning(
                        "recipes.foreach item failed (step=%s endpoint=%s): %s",
                        step.id,
                        ep,
                        exc,
                    )
                    continue
                rows.extend(drows)
                if step.merge is not None and isinstance(item, dict):
                    _merge_into(item, drows, step.merge.strategy)
        else:
            ep = _render(step.fetch.endpoint, ctx)
            data = fetch_one(ep)
            # Publish list_endpoint for subsequent detail steps: strip the
            # query string, keep scheme/netloc/path. Matches the legacy
            # _fetch_greenhouse_details derivation.
            parts = urlsplit(ep)
            ctx["list_endpoint"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            rows = evaluate_extract(step.extract, data)
        ns["steps"][step.id] = rows
    return ns["steps"]
