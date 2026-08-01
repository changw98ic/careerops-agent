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

from careerops.recipes.evaluator import apply_field, evaluate_extract, run_steps
from careerops.recipes.schema import Extract, Field_, Recipe


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


# ---------------------------------------------------------------------------
# Task 4 — multi-step orchestration (steps / when / foreach / merge)
# ---------------------------------------------------------------------------

import yaml  # noqa: E402  — local import keeps single-step section dep-free

# Greenhouse list→detail recipe. List step extracts ``description`` from the
# ``description`` field of each list item (so the list JSON's description
# values flow through directly); the detail step extracts ``description`` from
# the ``content`` field of the per-job detail payload. ``when``/``foreach``
# both filter on ``description==''`` so the detail fan-out only happens for
# list rows whose description is still empty.
GREENHOUSE_YAML = """
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["x"]}
steps:
  - id: list
    fetch:
      endpoint: "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
      method: GET
    extract: {mode: json_path, items_path: "$.jobs",
      fields: {id: {path: id}, title: {path: title}, description: {path: description}}}
  - id: detail
    when: "$.steps.list[?(@.description=='')]"
    foreach: "$.steps.list[?(@.description=='')]"
    fetch: {endpoint: "{list_endpoint}/{id}", method: GET}
    extract: {mode: json_path, fields: {description: {path: content}}}
    merge: {strategy: overwrite_empty}
"""


def test_multistep_detail_fills_empty_description():
    r = Recipe.model_validate(yaml.safe_load(GREENHOUSE_YAML))
    list_json = {
        "jobs": [
            {"id": "1", "title": "A", "description": ""},
            {"id": "2", "title": "B", "description": "has"},
        ]
    }
    detail_json = {"content": "<p>desc</p>"}

    def fetch_one(endpoint):
        return detail_json if endpoint.endswith("/1") else list_json

    ns = run_steps(
        r.steps,
        fetch_one,
        {"slug": "acme", "list_endpoint": "https://boards-api.greenhouse.io/v1/boards/acme/jobs"},
    )
    by_id = {row["id"]: row for row in ns["list"]}
    assert by_id["1"]["description"] == "<p>desc</p>"  # filled from detail
    assert by_id["2"]["description"] == "has"  # list value preserved
    # detail step's own output is the per-item extracts (one row here)
    assert ns["detail"] == [{"description": "<p>desc</p>"}]


def test_multistep_when_empty_skips_detail_step():
    """If the ``when`` expression yields no matches, the step is skipped: no
    fetches happen and its slot is ``[]``."""
    r = Recipe.model_validate(yaml.safe_load(GREENHOUSE_YAML))
    # every list row already has a description → when filter matches nothing
    list_json = {"jobs": [{"id": "1", "title": "A", "description": "filled"}]}

    fetched: list[str] = []

    def fetch_one(endpoint):
        fetched.append(endpoint)
        return list_json

    ns = run_steps(r.steps, fetch_one, {"slug": "acme", "list_endpoint": "x"})
    assert ns["detail"] == []  # skipped
    assert fetched == [  # only the list endpoint was hit
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
    ]
    assert ns["list"][0]["description"] == "filled"  # untouched


def test_multistep_foreach_preserves_document_order():
    """foreach iterates targets in document order — first empty row processed
    first (visible via per-id detail payloads)."""
    r = Recipe.model_validate(yaml.safe_load(GREENHOUSE_YAML))
    list_json = {
        "jobs": [
            {"id": "1", "title": "A", "description": ""},
            {"id": "2", "title": "B", "description": ""},
            {"id": "3", "title": "C", "description": ""},
        ]
    }

    def fetch_one(endpoint):
        if "/jobs/" in endpoint:  # detail call
            tail = endpoint.rsplit("/", 1)[-1]
            return {"content": f"<p>desc for {tail}</p>"}
        return list_json  # list call

    ns = run_steps(r.steps, fetch_one, {"slug": "acme", "list_endpoint": "base"})
    by_id = {row["id"]: row for row in ns["list"]}
    assert by_id["1"]["description"] == "<p>desc for 1</p>"
    assert by_id["2"]["description"] == "<p>desc for 2</p>"
    assert by_id["3"]["description"] == "<p>desc for 3</p>"
    # detail step rows are emitted in the same order as foreach iteration
    assert [d["description"] for d in ns["detail"]] == [
        "<p>desc for 1</p>",
        "<p>desc for 2</p>",
        "<p>desc for 3</p>",
    ]


# ---------------------------------------------------------------------------
# Merge strategy coverage. The greenhouse recipe's ``when``/``foreach``
# filter only visits rows whose ``description`` is empty, so all three
# strategies behave identically under it. To distinguish them we use a
# separate recipe whose ``foreach`` iterates *every* list row regardless of
# the description value.
# ---------------------------------------------------------------------------

STRATEGY_YAML_TEMPLATE = """
recipe_version: "1"
source_type: test
executor_mode: http
match: {url_patterns: ["x"]}
steps:
  - id: list
    fetch: {endpoint: "https://x/jobs", method: GET}
    extract: {mode: json_path, items_path: "$.jobs",
      fields: {id: {path: id}, description: {path: description}}}
  - id: detail
    foreach: "$.steps.list[*]"
    fetch: {endpoint: "{list_endpoint}/{id}", method: GET}
    extract: {mode: json_path, fields: {description: {path: content}}}
    merge: {strategy: __STRATEGY__}
"""


def _strategy_recipe(strategy: str) -> Recipe:
    yaml_text = STRATEGY_YAML_TEMPLATE.replace("__STRATEGY__", strategy)
    return Recipe.model_validate(yaml.safe_load(yaml_text))


def test_multistep_merge_overwrite_empty_only_fills_empty_fields():
    """overwrite_empty must NOT clobber a non-empty list value with detail data."""
    r = _strategy_recipe("overwrite_empty")
    # description already non-empty on the row → detail value rejected
    list_json = {"jobs": [{"id": "1", "description": "keep me"}]}
    detail_json = {"content": "from-detail"}

    def fetch_one(endpoint):
        return detail_json if endpoint.endswith("/1") else list_json

    ns = run_steps(r.steps, fetch_one, {"slug": "acme", "list_endpoint": "https://x/jobs"})
    assert ns["list"][0]["description"] == "keep me"


def test_multistep_merge_overwrite_all_replaces_existing():
    """overwrite_all replaces every field the detail step provides, even when
    the list row already had a value."""
    r = _strategy_recipe("overwrite_all")
    list_json = {"jobs": [{"id": "1", "description": "keep me"}]}
    detail_json = {"content": "from-detail"}

    def fetch_one(endpoint):
        return detail_json if endpoint.endswith("/1") else list_json

    ns = run_steps(r.steps, fetch_one, {"slug": "acme", "list_endpoint": "https://x/jobs"})
    assert ns["list"][0]["description"] == "from-detail"


def test_multistep_merge_keep_first_preserves_existing_key():
    """keep_first does not overwrite a key the parent row already has, even
    when the value is empty."""
    r = _strategy_recipe("keep_first")
    # description present but empty — keep_first still keeps it (key exists)
    list_json = {"jobs": [{"id": "1", "description": ""}]}
    detail_json = {"content": "from-detail"}

    def fetch_one(endpoint):
        return detail_json if endpoint.endswith("/1") else list_json

    ns = run_steps(r.steps, fetch_one, {"slug": "acme", "list_endpoint": "https://x/jobs"})
    # key was already present (with empty value) → keep_first refuses to overwrite
    assert ns["list"][0]["description"] == ""


def test_multistep_endpoint_render_safe_dict_keeps_missing_keys():
    """Missing template variables are left literally rather than raising.

    No ``list_endpoint`` is provided in base_ns — the list step derives it
    from its own endpoint (query stripped), so detail's
    ``{list_endpoint}/{id}`` resolves correctly.
    """
    r = Recipe.model_validate(yaml.safe_load(GREENHOUSE_YAML))
    list_json = {"jobs": [{"id": "1", "title": "A", "description": ""}]}
    detail_json = {"content": "x"}

    def fetch_one(endpoint):
        return detail_json if endpoint.endswith("/1") else list_json

    ns = run_steps(r.steps, fetch_one, {"slug": "acme"})
    assert ns["list"][0]["description"] == "x"


def test_multistep_foreach_bare_path_unwrap():
    """``foreach: $.steps.list`` (no filter, no wildcard) is the single-node
    array-valued form; the evaluator unwraps it so we iterate items, not
    ``[items]``. Exercise this by switching both ``when`` and ``foreach`` to
    the bare path over a list with two empty-description rows."""
    yaml_text = GREENHOUSE_YAML.replace(
        "$.steps.list[?(@.description=='')]",
        "$.steps.list",
    )
    r = Recipe.model_validate(yaml.safe_load(yaml_text))
    list_json = {
        "jobs": [
            {"id": "1", "title": "A", "description": ""},
            {"id": "2", "title": "B", "description": ""},
        ]
    }
    detail_json = {"content": "filled"}

    def fetch_one(endpoint):
        return detail_json if "/jobs/" in endpoint else list_json

    ns = run_steps(r.steps, fetch_one, {"slug": "acme", "list_endpoint": "https://x/jobs"})
    # both rows got fetched and filled → bare-path unwrap worked
    assert ns["list"][0]["description"] == "filled"
    assert ns["list"][1]["description"] == "filled"
