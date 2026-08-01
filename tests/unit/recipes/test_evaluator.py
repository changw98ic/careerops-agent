"""Single-step evaluator for the four extract modes (Tasks 3 + 5) and
multi-step orchestration (Task 4).

Covers:

* ``evaluate_extract`` walks ``items_path`` against ``data`` (``json_path``
  mode) and emits one ``dict`` per matched item, mapping each declared field
  name to its resolved value.
* Nested JSONPath fields (``$.location.name``) resolve against the current
  item, taking the first non-empty match.
* ``apply_field`` walks the ``[path, *fallback]`` candidate chain and returns
  the first non-empty match, falling back to ``""`` when every candidate
  misses.
* ``transform: html_unescape`` runs the resolved string through
  :func:`html.unescape`.
* ``json_ld`` mode (Task 5): ``extruct`` parses JSON-LD blocks; ``filter`` is
  applied per item by wrapping the item dict in ``[item]`` (RFC 9535 filters
  only apply over arrays).
* ``sitemap`` mode (Task 5): XXE guard + ``url_filter`` regex.
* ``css`` mode (Task 5): each ``Field_.path`` is a CSS selector; columns are
  zip-paired by index.

``extract.filter`` is intentionally *not* exercised under ``json_path`` mode:
python-jsonpath 2.2.1 evaluates a filter ``$[?(...)]`` against a *single
object* as ``[]`` (filters only apply over arrays), so the per-item semantics
plus the ``?()``-correct wrapping live with the json_ld executor where filter
is a core feature. ``evaluate_extract`` in json_path mode therefore ignores
``extract.filter`` — pinned by ``test_filter_ignored_in_json_path_mode``.
"""

from __future__ import annotations

import pytest

from careerops.recipes.evaluator import apply_field, evaluate_extract, run_steps
from careerops.recipes.schema import Extract, Field_, Filter, Recipe


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
# Task 5 — json_ld / sitemap / css modes + guards
#
# Filter key-access note (verified against python-jsonpath 2.2.1):
# ``@.type`` accesses the literal key ``type``. To reach schema.org's ``@type``
# key the bracket form ``@['@type']`` is required, and string literals must be
# single-quoted (barewords raise ``JSONPathSyntaxError``). The canonical
# JobPosting filter fragment is therefore ``@['@type']=='JobPosting'``.
# ---------------------------------------------------------------------------


def test_json_ld_filters_jobposting():
    """json_ld mode parses JSON-LD blocks via extruct and applies
    ``extract.filter`` per item by wrapping the item in ``[item]``. A
    Person block is dropped, a JobPosting block survives, and the declared
    field mapping (``title``) resolves against the surviving block."""
    html_doc = (
        '<script type="application/ld+json">{"@type":"Person","name":"x"}</script>'
        '<script type="application/ld+json">{"@type":"JobPosting","title":"Eng"}</script>'
    )
    ex = Extract(
        mode="json_ld",
        filter=Filter(jsonpath="@['@type']=='JobPosting'"),
        fields={"title": Field_(path="title")},
    )
    rows = evaluate_extract(ex, html_doc)
    assert rows == [{"title": "Eng"}]


def test_json_ld_no_filter_keeps_all_blocks():
    """Without a filter every JSON-LD block becomes a row."""
    html_doc = (
        '<script type="application/ld+json">{"@type":"Person","name":"Alice"}</script>'
        '<script type="application/ld+json">{"@type":"JobPosting","title":"Eng"}</script>'
    )
    ex = Extract(
        mode="json_ld",
        fields={"name": Field_(path="name"), "title": Field_(path="title")},
    )
    rows = evaluate_extract(ex, html_doc)
    assert rows == [
        {"name": "Alice", "title": ""},
        {"name": "", "title": "Eng"},
    ]


def test_json_ld_malformed_block_returns_empty():
    """extruct propagates JSONDecodeError on a malformed script; the executor
    swallows it and returns ``[]`` so one bad block does not sink the crawl."""
    html_doc = '<script type="application/ld+json">{bad json}</script>'
    ex = Extract(mode="json_ld", fields={"title": Field_(path="title")})
    assert evaluate_extract(ex, html_doc) == []


def test_sitemap_xxe_rejected_and_url_filter():
    """sitemap mode parses ``<urlset>`` via ElementTree, applies ``url_filter``
    regex to each ``<loc>`` text, and emits ``{url, external_id}`` rows.
    ``<!DOCTYPE``/``<!ENTITY`` input is rejected before parsing (XXE defence)."""
    xml = (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://x.com/about</loc></url>"
        "<url><loc>https://x.com/jobs/1</loc></url>"
        "</urlset>"
    )
    ex = Extract(mode="sitemap", url_filter=r"\b(jobs|careers|positions)\b")
    rows = evaluate_extract(ex, xml)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://x.com/jobs/1"
    # external_id is sha256(url)[:16] — 16 hex chars, deterministic
    assert rows[0]["external_id"] == "084ccffa4f0902a2"
    assert len(rows[0]["external_id"]) == 16

    # XXE guard: DOCTYPE-bearing input yields [] without raising.
    evil = '<!DOCTYPE x [<!ENTITY xxe "y">]><urlset></urlset>'
    assert evaluate_extract(ex, evil) == []

    # ENTITY-bearing input is also rejected.
    evil2 = '<?xml version="1.0"?><!ENTITY foo "bar"><urlset></urlset>'
    assert evaluate_extract(ex, evil2) == []


def test_sitemap_no_url_filter_keeps_all():
    """Without ``url_filter`` every ``<loc>`` becomes a row."""
    xml = (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://x.com/about</loc></url>"
        "<url><loc>https://x.com/jobs/1</loc></url>"
        "</urlset>"
    )
    ex = Extract(mode="sitemap")
    rows = evaluate_extract(ex, xml)
    assert [r["url"] for r in rows] == ["https://x.com/about", "https://x.com/jobs/1"]


def test_sitemap_malformed_xml_returns_empty():
    """Non-XML input yields ``[]`` via the ParseError guard."""
    ex = Extract(mode="sitemap")
    assert evaluate_extract(ex, "not xml at all") == []


def test_css_zip_pairing():
    """css mode reuses ``Field_.path`` as a CSS selector and zip-pairs columns
    by index up to the longest column's length."""
    doc = (
        "<div>"
        '<h2 class="t">A</h2><span class="l">SF</span>'
        '<h2 class="t">B</h2><span class="l">NYC</span>'
        "</div>"
    )
    ex = Extract(
        mode="css",
        fields={"title": Field_(path="h2.t"), "location": Field_(path="span.l")},
    )
    rows = evaluate_extract(ex, doc)
    assert rows == [
        {"title": "A", "location": "SF"},
        {"title": "B", "location": "NYC"},
    ]


def test_css_unequal_columns_pad_empty():
    """When one column is longer than the other, missing positions are
    padded with ``""`` so the row count equals the longest column's length."""
    doc = (
        "<div>"
        '<h2 class="t">A</h2><span class="l">only-one</span>'
        '<h2 class="t">B</h2>'
        '<h2 class="t">C</h2>'
        "</div>"
    )
    ex = Extract(
        mode="css",
        fields={"title": Field_(path="h2.t"), "location": Field_(path="span.l")},
    )
    rows = evaluate_extract(ex, doc)
    assert rows == [
        {"title": "A", "location": "only-one"},
        {"title": "B", "location": ""},
        {"title": "C", "location": ""},
    ]


def test_css_no_matches_returns_empty():
    """Every selector missing → max column length 0 → ``[]``."""
    ex = Extract(mode="css", fields={"title": Field_(path=".nonexistent")})
    assert evaluate_extract(ex, "<div></div>") == []


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


def test_multistep_detail_fetch_failure_skips_item_keeps_list_data():
    """A per-item detail fetch failure must not sink the run.

    Product contract (mirrors legacy m1_crawl_sink ``except Exception``):
    "List data remains useful on detail failure." Concretely, when fetch_one
    raises for one item's detail endpoint:

    * the failed item is skipped (its row keeps its current value — empty
      description here — and emits no detail row),
    * the other items are still fetched and merged,
    * the list step's data is returned intact.

    Uses a bare-path foreach (``$.steps.list[*]``) so both rows are visited
    regardless of description value, isolating the failure handling from the
    when-filter short-circuit.
    """
    yaml_text = GREENHOUSE_YAML.replace(
        "$.steps.list[?(@.description=='')]",
        "$.steps.list[*]",
    )
    # drop the `when:` line entirely so the detail step always runs
    yaml_text = yaml_text.replace('    when: "$.steps.list[*]"\n', "")
    r = Recipe.model_validate(yaml.safe_load(yaml_text))
    list_json = {
        "jobs": [
            {"id": "1", "title": "A", "description": ""},  # detail fetch will raise
            {"id": "2", "title": "B", "description": ""},  # detail fetch will succeed
        ]
    }
    detail_json_ok = {"content": "<p>desc for 2</p>"}

    def fetch_one(endpoint):
        if endpoint.endswith("/jobs/1"):
            raise RuntimeError("simulated detail 503")
        if endpoint.endswith("/jobs/2"):
            return detail_json_ok
        return list_json  # list call

    ns = run_steps(r.steps, fetch_one, {"slug": "acme", "list_endpoint": "https://x/jobs"})

    by_id = {row["id"]: row for row in ns["list"]}
    # job 1 (failed) keeps its empty description; job 2 (succeeded) is filled
    assert by_id["1"]["description"] == ""
    assert by_id["2"]["description"] == "<p>desc for 2</p>"
    # list data itself is intact: both rows present with title + id
    assert {row["id"] for row in ns["list"]} == {"1", "2"}
    # only the successful item emitted a detail row
    assert ns["detail"] == [{"description": "<p>desc for 2</p>"}]


def test_multistep_list_step_failure_propagates():
    """List-step fetch failures are NOT swallowed — the run has no primary
    data, so the exception must propagate to the caller.

    This pins the scope of the per-item fault tolerance: only foreach
    (detail) per-item fetches are guarded; the list fetch failing is a hard
    error.
    """
    r = Recipe.model_validate(yaml.safe_load(GREENHOUSE_YAML))

    def fetch_one(endpoint):
        if "jobs?content=true" in endpoint:
            raise RuntimeError("list endpoint down")
        return {"content": "x"}

    with pytest.raises(RuntimeError, match="list endpoint down"):
        run_steps(r.steps, fetch_one, {"slug": "acme", "list_endpoint": "x"})
