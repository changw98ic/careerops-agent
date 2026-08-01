"""Single-step recipe evaluator for ``json_path`` extracts (Task 3).

This module is the run-time counterpart of the declarative schema in
:mod:`careerops.recipes.schema`: it takes an :class:`Extract` block and the
parsed response ``data`` and produces a list of flat field->value dicts
("rows"), one per matched item. Each row is the unit the crawl pipeline later
merges into a posting record.

Two public entry points:

* :func:`evaluate_extract` — locate the item list via ``extract.items_path``
  (a JSONPath evaluated against ``data``), then resolve every declared field
  against each item.
* :func:`apply_field` — resolve a single :class:`Field_` against an item: walk
  ``[field.path, *field.fallback]`` in order and return the first non-empty
  JSONPath match (stringified), optionally unescaped.

Non-``json_path`` modes are out of scope for Task 3 and raise
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

import jsonpath

from careerops.recipes.schema import Extract, Field_


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
