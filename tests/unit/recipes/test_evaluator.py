"""Single-step evaluator for ``json_path`` extracts (Task 3).

Covers:

* ``evaluate_extract`` walks ``items_path`` against ``data`` and emits one
  ``dict`` per matched item, mapping each declared field name to its resolved
  value.
* Nested JSONPath fields (``$.location.name``) resolve against the current
  item, taking the first non-empty match.
* ``apply_field`` walks the ``[path, *fallback]`` candidate chain and returns
  the first non-empty match, falling back to ``""`` when every candidate
  misses.
* ``transform: html_unescape`` runs the resolved string through
  :func:`html.unescape`.
* Non-``json_path`` modes are not implemented at this layer in Task 3 (they
  arrive with the json_ld / sitemap / css executors in later tasks) and raise
  :class:`NotImplementedError`.

Filter evaluation (``extract.filter``) is intentionally *not* exercised here:
python-jsonpath 2.2.1 evaluates a filter ``$[?(...)]`` against a *single
object* as ``[]`` (filters only apply over arrays), so the per-item semantics
plus the ``?()``-correct wrapping are deferred to Task 5 where json_ld mode
makes filter a core feature. ``evaluate_extract`` in json_path mode therefore
ignores ``extract.filter`` today.
"""

from __future__ import annotations

import pytest

from careerops.recipes.evaluator import apply_field, evaluate_extract
from careerops.recipes.schema import Extract, Field_


def test_json_path_items_and_fields():
    ex = Extract(
        mode="json_path",
        items_path="$.jobs",
        fields={
            "title": Field_(path="title"),
            "loc": Field_(path="location.name"),
        },
    )
    data = {
        "jobs": [
            {"title": "Eng", "location": {"name": "SF"}},
            {"title": "PM", "location": {"name": "NYC"}},
        ]
    }
    rows = evaluate_extract(ex, data)
    assert rows == [
        {"title": "Eng", "loc": "SF"},
        {"title": "PM", "loc": "NYC"},
    ]


def test_field_fallback_chain():
    f = Field_(path="applyUrl", fallback=["hostedUrl", "url"])
    assert apply_field(f, {"applyUrl": "a"}) == "a"
    assert apply_field(f, {"hostedUrl": "b"}) == "b"
    assert apply_field(f, {"url": "c"}) == "c"
    assert apply_field(f, {}) == ""


def test_transform_html_unescape():
    f = Field_(path="content", transform="html_unescape")
    assert apply_field(f, {"content": "a &amp; b"}) == "a & b"


def test_items_path_missing_returns_empty():
    """A valid-but-unmatched items_path yields zero rows (no error)."""
    ex = Extract(mode="json_path", items_path="$.jobs", fields={"title": Field_(path="title")})
    assert evaluate_extract(ex, {}) == []


def test_nested_path_takes_first_non_empty_match():
    """``jsonpath.findall`` returns matches in document order; we take [0]."""
    ex = Extract(
        mode="json_path",
        items_path="$.jobs",
        fields={"loc": Field_(path="location.name")},
    )
    data = {"jobs": [{"location": {"name": "SF"}}, {"location": {}}]}
    rows = evaluate_extract(ex, data)
    assert rows == [{"loc": "SF"}, {"loc": ""}]


def test_field_value_coerced_to_string():
    """Non-string scalars are coerced via ``str()`` so the row is text-only."""
    f = Field_(path="count")
    assert apply_field(f, {"count": 42}) == "42"


def test_apply_field_on_non_dict_item_returns_empty():
    """A scalar / list item has no extractable fields; return ``""``."""
    f = Field_(path="title")
    assert apply_field(f, "not a dict") == ""
    assert apply_field(f, None) == ""


def test_transform_none_is_default_and_noop():
    f = Field_(path="content", transform="none")
    assert apply_field(f, {"content": "plain"}) == "plain"


def test_non_json_path_mode_raises():
    """json_ld / sitemap / css are implemented in later tasks, not Task 3."""
    ex = Extract(mode="json_ld", items_path="$.jobs", fields={"title": Field_(path="title")})
    with pytest.raises(NotImplementedError):
        evaluate_extract(ex, {"jobs": []})


def test_filter_ignored_in_json_path_mode():
    """json_path mode does not evaluate ``extract.filter`` (deferred to Task 5).

    A filter that *would* exclude everything if applied must leave the rows
    untouched here — this pins the deferral as observed behaviour so Task 5
    can flip it on deliberately.
    """
    ex = Extract(
        mode="json_path",
        items_path="$.jobs",
        fields={"title": Field_(path="title")},
        filter={"jsonpath": "@.type=='JobPosting'"},  # would drop everything if applied per-item
    )
    data = {"jobs": [{"title": "Eng"}, {"title": "PM"}]}
    rows = evaluate_extract(ex, data)
    assert rows == [{"title": "Eng"}, {"title": "PM"}]
