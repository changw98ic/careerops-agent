"""Recipe evaluator: single-step extract (Tasks 3 + 5) and multi-step
orchestration (Task 4 — ``steps`` / ``when`` / ``foreach`` / ``merge``).

This module is the run-time counterpart of the declarative schema in
:mod:`careerops.recipes.schema`. It takes declarative blocks and parsed HTTP
responses and produces flat ``field -> value`` dicts ("rows"), one per matched
item. Each row is the unit the crawl pipeline later merges into a posting
record.

Public entry points:

* :func:`evaluate_extract` — single-step: dispatch on ``extract.mode``:

  - ``json_path`` (Task 3): locate the item list via ``items_path`` (a
    JSONPath evaluated against ``data``), then resolve every declared field
    against each item.
  - ``json_ld`` (Task 5): parse embedded JSON-LD ``<script>`` blocks via
    ``extruct`` and apply ``extract.filter`` per item.
  - ``sitemap`` (Task 5): parse an XML sitemap with an XXE guard and an
    optional ``url_filter`` regex.
  - ``css`` (Task 5): run each ``Field_.path`` as a CSS selector and zip
    columns by index.

* :func:`apply_field` — resolve a single :class:`Field_` against an item: walk
  ``[field.path, *field.fallback]`` in order and return the first non-empty
  JSONPath match (stringified), optionally unescaped.
* :func:`run_steps` — multi-step: drive a recipe's ``steps`` in order with
  ``when`` gating, ``foreach`` per-item fetching and ``merge`` strategies.

Filter semantics (Task 5). RFC 9535 filters ``$[?(...)]`` only apply over
*arrays* — applied to a single object item, python-jsonpath 2.2.1 returns
``[]`` (verified). The json_ld executor therefore **wraps each item dict in a
single-element array** ``[item]`` before evaluating the filter, so a per-item
condition yields a non-empty match list when the item qualifies. The filter
fragment is the raw condition (e.g. ``@['@type']=='JobPosting'``); the
executor wraps it as ``$[?({filter.jsonpath})]``.

Two notes on JSON-LD key access (verified against python-jsonpath 2.2.1):

* ``@.type`` accesses the literal key ``type``. To reach the schema.org
  ``@type`` key, use the bracket form ``@['@type']``.
* String literals inside the filter must be **single-quoted**; bareword
  identifiers raise ``JSONPathSyntaxError``.

``evaluate_extract`` in ``json_path`` mode ignores ``extract.filter`` — the
filter only makes sense for json_ld's per-item flow, and pinning that
behaviour keeps Task 3's tests stable.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import time
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

import jsonpath

from careerops.recipes.schema import Extract, Field_, Step

# Type alias for the flat ``field -> value`` rows every extractor produces.
# Each row maps a declared field name to its stringified value (plus the
# executor-injected ``external_id`` key for modes that compute it inline).
Row = dict[str, Any]

logger = logging.getLogger(__name__)


# XXE defence for sitemap mode. ``ElementTree.fromstring`` does not itself
# block DOCTYPE/ENTITY declarations (verified), so we reject any XML whose
# prolog contains those tokens before handing it to the parser. The check is
# case-insensitive on the raw bytes; a sitemap that legitimately needs a
# DOCTYPE does not exist in the wild.
_UNSAFE_XML_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)

# Sitemaps declare ``xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"``;
# ElementTree requires a prefix-to-namespace mapping for namespaced xpath.
_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def apply_field(field: Field_, item: object) -> str:
    """Resolve a single :class:`Field_` against ``item`` to a string.

    Walks the candidate chain ``[field.path, *field.fallback]`` in order; for
    each candidate evaluates ``jsonpath.findall(f"$.{candidate}", item)`` and
    takes the first **non-empty** value of the first candidate that yields one.
    Missing candidates yield ``[]`` and are skipped; a candidate whose match is
    an empty string is also skipped (PR #7 review fix: this mirrors the legacy
    adapters' Python ``or`` short-circuit, where ``descriptionPlain=""``
    falls through to ``description`` rather than winning as ``""``). When every
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

    # ``cast``: ``isinstance(item, dict)`` narrows to ``dict[Unknown, Unknown]``
    # which trips ``reportUnknownArgumentType`` on the ``findall`` call below;
    # the executor treats every item as a ``str -> Any`` mapping.
    item_dict: dict[str, Any] = cast("dict[str, Any]", item)

    candidates = [field.path, *field.fallback]
    raw: object = None
    for candidate in candidates:
        # ``jsonpath.findall`` is typed ``list[Unknown]``; assigning to an
        # explicit ``list[Any]`` absorbs the unknown element type.
        values: list[Any] = jsonpath.findall(f"$.{candidate}", item_dict)
        for value in values:
            # Skip empty-string matches so the fallback chain continues
            # (legacy ``or`` parity). ``None`` and ``""`` both miss.
            if value is not None and value != "":
                raw = value
                break
        if raw is not None:
            break

    text = "" if raw is None else str(raw)
    if field.transform == "html_unescape" and text:
        text = html.unescape(text)
    return text


def evaluate_extract(extract: Extract, data: object) -> list[Row]:
    """Resolve an :class:`Extract` block against ``data`` into rows.

    Dispatch on ``extract.mode``:

    * ``json_path``: ``items_path`` is evaluated against ``data``; each matched
      object becomes one row whose keys are the declared field names and whose
      values come from :func:`apply_field`. A valid-but-unmatched
      ``items_path`` yields ``[]``.
    * ``json_ld``: ``data`` is treated as HTML and parsed via ``extruct``;
      each JSON-LD block is a candidate row, optionally pruned by
      ``extract.filter`` (see module docstring for the wrapping rule).
    * ``sitemap``: ``data`` is treated as XML; URLs matching ``url_filter``
      become rows of ``{"url", "external_id"}``. DOCTYPE/ENTITY input is
      rejected up front (XXE defence).
    * ``css``: ``data`` is treated as HTML; each ``Field_.path`` is used as a
      CSS selector and the columns are zip-paired by index.

    ``extract.filter`` is ignored outside ``json_ld`` mode — it is a json_ld
    feature; see module docstring.
    """
    if extract.mode == "json_path":
        return _extract_json_path(extract, data)
    if extract.mode == "json_ld":
        return _extract_json_ld(extract, data)
    if extract.mode == "sitemap":
        return _extract_sitemap(extract, data)
    if extract.mode == "css":
        return _extract_css(extract, data)
    raise NotImplementedError(extract.mode)


def _extract_json_path(extract: Extract, data: object) -> list[Row]:
    """``json_path`` mode (Task 3). ``extract.filter`` is intentionally
    ignored — see module docstring."""
    if not isinstance(data, (dict, list)):
        return []
    items_path = extract.items_path or "$"
    # ``cast``: ``data`` narrowed to ``dict[Unknown, Unknown] | list[Unknown]``
    # trips the ``findall`` argument check; the evaluator is agnostic to the
    # value types inside the parsed payload.
    matched: list[Any] = jsonpath.findall(items_path, cast("Any", data))

    # ``items_path`` semantics: it points AT the collection of items, not into
    # it. ``$.jobs`` therefore resolves to a single matched node whose *value*
    # is the array, so ``findall`` returns ``[[a, b]]`` rather than ``[a, b]``.
    # When we get exactly one list-valued match, that list IS the item array
    # and we unwrap it. The ``$.jobs[*]`` form (already a flat list of dicts)
    # and a top-level array (``$`` against ``[a, b]``) are both handled too.
    # Verified against python-jsonpath 2.2.1.
    items: list[Any] = (
        cast("list[Any]", matched[0])
        if len(matched) == 1 and isinstance(matched[0], list)
        else matched
    )

    rows: list[Row] = []
    for item in items:
        row: Row = {name: apply_field(field, item) for name, field in extract.fields.items()}
        rows.append(row)
    return rows


def _extract_json_ld(extract: Extract, html_str: object) -> list[Row]:
    """``json_ld`` mode (Task 5).

    Parses every ``<script type="application/ld+json">`` block via
    ``extruct.extract(html_str, syntaxes=['json-ld'])`` and emits one row per
    block. ``extract.filter`` is applied per item by wrapping the item dict in
    a single-element array ``[item]`` and running
    ``jsonpath.findall('$[?({filter.jsonpath})]', [item])``; a non-empty match
    list keeps the item. This is the canonical workaround for RFC 9535 filters
    applying only to arrays (see module docstring).

    ``extruct`` raises ``json.JSONDecodeError`` on a malformed JSON-LD script
    and ``TypeError`` on non-string input — both are caught and the executor
    returns ``[]`` so one bad block does not sink the whole crawl (real-page
    robustness; mirrors the legacy per-item fault tolerance).

    External-id alignment (PR #7 review fix): each row carries
    ``external_id = sha256(url)[:16]`` mirroring the legacy ``JsonLdAdapter``
    dedup key, so a JobPosting keeps the same id before/after the recipe
    migration. The url source is the schema.org block's ``url`` field first
    (the canonical JobPosting field), then the recipe-mapped ``apply_url`` /
    ``url`` row fields. When no url is present on any source, the row's
    ``external_id`` is ``""`` — mirroring the legacy ``if url else ""`` guard
    so url-less blocks do not collapse onto a single ``sha256("")`` id (PR #7
    review fix batch 4).
    """
    if not isinstance(html_str, str):
        return []
    import extruct  # pyright: ignore[reportMissingTypeStubs]

    try:
        blocks: list[Any] = extruct.extract(html_str, syntaxes=["json-ld"]).get("json-ld", [])
    except Exception as exc:  # JSONDecodeError on malformed scripts, others
        logger.warning("recipes.json_ld extract failed: %s", exc)
        return []

    rows: list[Row] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_dict: dict[str, Any] = cast("dict[str, Any]", block)
        if extract.filter is not None:
            wrapped = f"$[?({extract.filter.jsonpath})]"
            if not jsonpath.findall(wrapped, [block_dict]):
                continue
        row: Row = {name: apply_field(field, block_dict) for name, field in extract.fields.items()}
        url_source = block_dict.get("url") or row.get("apply_url") or row.get("url") or ""
        if not isinstance(url_source, str):
            url_source = str(url_source)
        # Legacy-parity empty-url guard: ``sha256("")`` would give every
        # url-less block the same id; the legacy adapter returned ``""`` for
        # missing urls, so we mirror that here.
        if url_source:
            row["external_id"] = hashlib.sha256(url_source.encode()).hexdigest()[:16]
        else:
            row["external_id"] = ""
        rows.append(row)
    return rows


def _extract_sitemap(extract: Extract, xml_str: object) -> list[Row]:
    """``sitemap`` mode (Task 5).

    Parses an XML sitemap and emits one row per ``<url><loc>`` whose text
    matches ``extract.url_filter`` (compiled as a regex search). Each row is
    ``{"url": loc, "external_id": sha256(loc)[:16]}`` — the deterministic ID
    mirrors the legacy ``SitemapAdapter`` contract.

    Defences:

    * XXE: input containing ``<!DOCTYPE`` or ``<!ENTITY`` (case-insensitive)
      is rejected before parsing. ``ElementTree.fromstring`` does not block
      external entities by itself (verified), so the regex gate is the
      primary safeguard.
    * Parse errors: malformed XML yields ``[]`` rather than propagating.
    * Namespace: sitemaps declare ``xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"``;
      the xpath ``.//sm:url/sm:loc`` with the ``_SITEMAP_NS`` mapping matches.
      A sitemap without that namespace returns ``[]`` under the namespaced
      query — that is the correct behaviour for the declared schema.
    """
    if not isinstance(xml_str, str) or _UNSAFE_XML_RE.search(xml_str):
        return []
    try:
        root = ElementTree.fromstring(xml_str)
    except ElementTree.ParseError as exc:
        logger.warning("recipes.sitemap xml parse failed: %s", exc)
        return []

    url_re = re.compile(extract.url_filter) if extract.url_filter else None
    rows: list[Row] = []
    for loc in root.findall(".//sm:url/sm:loc", _SITEMAP_NS):
        url = (loc.text or "").strip()
        if not url:
            continue
        if url_re is not None and not url_re.search(url):
            continue
        rows.append({"url": url, "external_id": hashlib.sha256(url.encode()).hexdigest()[:16]})
    return rows


def _extract_css(extract: Extract, html_str: object) -> list[Row]:
    """``css`` mode (Task 5).

    ``Field_.path`` is reused as a CSS selector (the schema field keeps its
    name; the executor reinterprets it per mode). Each declared field becomes
    one column; rows are produced by **zipping columns by index** up to the
    longest column's length, with shorter columns padding missing positions
    with ``""``. This is the index-aligned pairing the legacy
    ``StaticHtmlAdapter`` did with two parallel regex matches.

    A selector that matches nothing yields an empty column; if every column
    is empty the result is ``[]`` (max length 0). ``selectolax``'s node
    ``.text(strip=True)`` extracts the trimmed text.

    External-id alignment (PR #7 review fix): each row carries
    ``external_id = sha256(title.strip())[:16]`` mirroring the legacy
    ``StaticHtmlAdapter`` dedup key, so a job card keeps the same id
    before/after the recipe migration. Empty titles still hash to a stable
    value (sha256 of empty string) — every untitled row collapses to one id,
    matching the legacy behaviour.
    """
    if not isinstance(html_str, str):
        return []
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html_str)
    columns: dict[str, list[Any]] = {
        name: tree.css(field.path) for name, field in extract.fields.items()
    }
    maxlen = max((len(nodes) for nodes in columns.values()), default=0)

    rows: list[Row] = []
    for i in range(maxlen):
        row: Row = {}
        for name, nodes in columns.items():
            row[name] = nodes[i].text(strip=True) if i < len(nodes) else ""
        title_source = (row.get("title") or "").strip()
        row["external_id"] = hashlib.sha256(title_source.encode()).hexdigest()[:16]
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


class _SafeDict(dict[str, str]):
    """``str.format_map`` mapping that preserves unknown ``{keys}`` literally.

    Used for endpoint templating: a detail step referencing ``{list_endpoint}``
    before the list step has run should not raise; it should leave the
    placeholder so the miss is visible in the rendered URL.
    """

    def __missing__(self, key: str) -> str:  # pragma: no cover - trivial
        return "{" + key + "}"


def _render(template: str, ctx: dict[str, Any]) -> str:
    """Render ``template`` via ``str.format_map`` with missing keys preserved."""
    return template.format_map(_SafeDict(ctx))


def _eval_pathlist(expr: str, ns: dict[str, Any]) -> list[Any]:
    """``jsonpath.findall`` + single-list-node unwrap.

    ``$.steps.list`` resolves to one node whose *value* is the array, so
    ``findall`` returns ``[[a, b]]`` rather than ``[a, b]``. The filter form
    (``$.steps.list[?(...)]``) and wildcard form (``$.steps.list[*]``) already
    return a flat list of items. The unwrap makes both forms iterable as item
    lists. Verified against python-jsonpath 2.2.1.
    """
    matched: list[Any] = jsonpath.findall(expr, ns)
    if len(matched) == 1 and isinstance(matched[0], list):
        return cast("list[Any]", matched[0])
    return matched


def _merge_into(parent_row: Row, detail_rows: list[Row], strategy: str) -> None:
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


def run_steps(
    steps: list[Step],
    fetch_one: Callable[[str], object],
    base_ns: dict[str, str],
    rate_limit: dict[str, int] | None = None,
) -> dict[str, list[Row]]:
    """Execute a recipe's ``steps`` in order, returning ``{step_id: [rows]}``.

    ``fetch_one(endpoint) -> object`` is the injected fetcher (returns the
    parsed JSON response). ``base_ns`` carries template variables such as
    ``{slug}`` and optionally ``{list_endpoint}``; it is copied, not mutated.

    ``rate_limit`` mirrors :attr:`Recipe.rate_limit` (``{requests_per_minute:
    60}``). When present and ``requests_per_minute > 0``, every
    ``fetch_one`` call is preceded by ``time.sleep(60 / requests_per_minute)``
    so the recipe self-throttles at the declared rate (greenhouse boards ask
    for 60/min). ``None`` / empty / non-positive values skip throttling —
    tests pass ``rate_limit=None`` (or omit it) so the suite never sleeps.

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
      data remains useful when a detail fetch fails.
    * **whole-step fault tolerance** (spec §D "整步失败 → 产出空, 不阻断后续"):
      a non-foreach (list) step whose ``fetch_one`` raises is caught — the
      step's slot becomes ``[]`` and execution continues to the next step
      rather than propagating. A failed list step means the recipe yields
      zero postings for that run (its detail dependents see an empty
      ``$.steps.<id>`` and naturally short-circuit); that is preferable to
      aborting an entire crawl when one ATS board is temporarily down. The
      failure is logged at WARNING level. ``evaluate_extract`` failures on
      the list branch are guarded the same way (a malformed body should not
      abort the run either).

    After a non-foreach step runs, the namespace publishes ``list_endpoint``
    derived from that step's endpoint with the query string stripped — this is
    the detail-step analogue of the legacy
    ``_fetch_greenhouse_details`` URL-derivation (the path that produced
    ``https://boards-api.greenhouse.io/v1/boards/<slug>/jobs`` from the list
    URL by dropping ``?content=true``).
    """
    rpm = (rate_limit or {}).get("requests_per_minute") or 0
    interval = 60.0 / rpm if rpm > 0 else 0.0

    def _throttled_fetch(endpoint: str) -> object:
        if interval > 0:
            time.sleep(interval)
        return fetch_one(endpoint)

    ns: dict[str, Any] = {"steps": {}}
    ctx = dict(base_ns)
    for step in steps:
        rows: list[Row] = []
        if step.when is not None and not _eval_pathlist(step.when, ns):
            ns["steps"][step.id] = rows
            continue
        if step.foreach is not None:
            targets: list[Any] = _eval_pathlist(step.foreach, ns)
            for item in targets:
                local = dict(ctx)
                item_row: Row | None = cast("Row", item) if isinstance(item, dict) else None
                if item_row is not None:
                    local.update({k: str(v) for k, v in item_row.items()})
                ep = _render(step.fetch.endpoint, local)
                # Per-item fault tolerance: a single detail fetch failure must
                # not sink the whole run. Skip the failed item (no row emit,
                # no merge — the parent list row keeps its current value, e.g.
                # empty description) and continue to the next item. This
                # mirrors the legacy m1_crawl_sink contract: "List data
                # remains useful on detail failure."
                try:
                    data = _throttled_fetch(ep)
                    drows: list[Row] = evaluate_extract(step.extract, data)
                except Exception as exc:  # broad guard: see comment above
                    logger.warning(
                        "recipes.foreach item failed (step=%s endpoint=%s): %s",
                        step.id,
                        ep,
                        exc,
                    )
                    continue
                rows.extend(drows)
                if step.merge is not None and item_row is not None:
                    _merge_into(item_row, drows, step.merge.strategy)
        else:
            ep = _render(step.fetch.endpoint, ctx)
            # Whole-step fault tolerance (spec §D): a list-step fetch or
            # extract failure yields ``[]`` for this step and the run
            # continues to subsequent steps rather than propagating. The
            # failed recipe surfaces zero postings (its detail dependents
            # see an empty parent list and short-circuit via ``when`` /
            # ``foreach``) — preferable to aborting an entire crawl when
            # one board is down. Logged at WARNING so operators can see it.
            try:
                data = _throttled_fetch(ep)
                rows = evaluate_extract(step.extract, data)
            except Exception as exc:
                logger.warning(
                    "recipes.list step failed (step=%s endpoint=%s): %s",
                    step.id,
                    ep,
                    exc,
                )
                ns["steps"][step.id] = []
                continue
            # Publish list_endpoint for subsequent detail steps: strip the
            # query string, keep scheme/netloc/path. Matches the legacy
            # _fetch_greenhouse_details derivation.
            parts = urlsplit(ep)
            ctx["list_endpoint"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        ns["steps"][step.id] = rows
    return ns["steps"]
