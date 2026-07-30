"""Autonomous crawl agent: drives a BrowserTool to crawl a job source.

The agent — not a human and not a one-off script — decides how to crawl a
source and executes it:

1. When a model client is available, run a ReAct loop first: the model observes
   the page and decides what to do (navigate / click / wait / scroll / capture
   network / extract / give up). This adapts to any site without hardcoded
   triggers.
2. Fall back to the site's own job-list API: capture it via a network hook
   (packet capture), then replay it across pages (fast, structured, accurate,
   with real per-job detail URLs).
3. Last resort: render the page and extract postings via :class:`LLMJobExtractor`
   (works on any unstructured page, no API needed).

ego is driven programmatically through the :class:`BrowserTool` protocol; the
result is :class:`RawJobRecord` objects that flow into the existing
``ingest_posting`` contract via the crawl sink's ``_to_crawled_posting`` helper.

This is the canonical Tier 2 agent. It owns no source registry and no ingest
path — those live in the persisted ``job_sources`` registry and the crawl sink
respectively, so there is exactly one production crawl path.
"""

from __future__ import annotations

import hashlib
import json
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from careerops.adapters.job_sources import RawJobRecord
from careerops.application.llm_job_extraction import LLMJobExtractor
from careerops.infrastructure.ego_tool import BrowserTool, CapturedApiCall
from careerops.model_gateway.base import StructuredModelClient, StructuredModelRequest

# Generic page trigger used when a source supplies no site-specific trigger.
# It tries common search/click patterns to fire a job-list request. Inlined
# here (the canonical Tier 2 executor) so the agent has no dependency on a
# separate static source registry.
GENERIC_TRIGGER = """(() => {
  var sels = [
    'input[type="search"]',
    'input[placeholder*="搜索"]',
    'input[placeholder*="职位"]',
    'input[placeholder*="岗位"]',
    'input[placeholder*="关键词"]',
    'input[placeholder*="job" i]',
    'input[placeholder*="position" i]',
    'input[placeholder*="search" i]',
  ];
  for (var i = 0; i < sels.length; i++) {
    var inp = document.querySelector(sels[i]);
    if (inp) {
      var d = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value');
      if (d && d.set) { d.set.call(inp, ''); }
      inp.dispatchEvent(new Event('input', {bubbles: true}));
      inp.dispatchEvent(new Event('change', {bubbles: true}));
      var k = {bubbles: true, key: 'Enter', keyCode: 13};
      inp.dispatchEvent(new KeyboardEvent('keydown', k));
      inp.dispatchEvent(new KeyboardEvent('keyup', k));
    }
  }
  var els = Array.from(document.querySelectorAll(
    'button, a[role="button"], [role="button"], span, li'));
  var patterns = /^(搜索|Search|职位|Jobs|查看|社会|社招|全部|筛选)$/;
  els.forEach(function (e) {
    var t = (e.textContent || '').trim();
    if (patterns.test(t) && e.children.length < 4) {
      try { e.click(); } catch (x) {}
    }
  });
  for (var s = 0; s < 5; s++) {
    window.scrollTo(0, (s + 1) * 1000);
  }
  try {
    var u = new URL(window.location.href);
    u.searchParams.set('_t', Date.now() + '');
    window.history.pushState({}, '', u.toString());
    window.dispatchEvent(new PopStateEvent('popstate'));
  } catch (e2) {}
  return 'triggered';
})()"""

# Provenance tag for records parsed out of a captured job-list API response.
# Distinct from the LLM extractor's ``llm-extraction`` tag so the sink can tell
# API-captured postings apart from model-extracted ones.
API_CAPTURE_PROVENANCE = "api-capture"

# Common JSON paths to the job array in a job-list API response, tried in order.
_JOB_ARRAY_PATHS: tuple[tuple[str, ...], ...] = (
    ("content", "datas"),
    ("content", "list"),
    ("content", "data"),
    ("data", "list"),
    ("data", "datas"),
    ("data", "jobs"),
    ("data", "records"),
    ("data", "data"),
    ("data", "results"),
    ("result", "list"),
    ("result", "records"),
    ("results", "jobs"),
    ("aggregate", "jobs"),
    ("datas",),
    ("list",),
    ("jobs",),
    ("records",),
    ("results",),
    ("values",),
    ("items",),
)
_TITLE_KEYS = (
    "name",
    "title",
    "jobName",
    "positionName",
    "jobTitle",
    "job_title",
    "job_name",
    "position",
    "role",
    "label",
)
_URL_KEYS = ("positionUrl", "url", "link", "detailUrl", "jobUrl", "href")
_LOCATION_KEYS = ("location", "workLocation", "city", "workPlace", "address")


def _dig(obj: object, path: tuple[str, ...]) -> object:
    cur: object = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cast("dict[str, object]", cur).get(key)
    return cur


def _parse_job_array(resp: object) -> list[dict[str, object]]:
    for path in _JOB_ARRAY_PATHS:
        arr = _dig(resp, path)
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            return cast("list[dict[str, object]]", arr)
    # Heuristic: recursively search for any list of dicts with title-like keys.
    return _heuristic_find_jobs(resp)


def _heuristic_find_jobs(obj: object, depth: int = 0) -> list[dict[str, object]]:
    """Recursively find a list of dicts containing title-like fields."""
    if depth > 5:
        return []
    if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict):
        sample = cast("dict[str, object]", obj[0])
        if any(k in sample for k in _TITLE_KEYS):
            return cast("list[dict[str, object]]", obj)
    if isinstance(obj, dict):
        for v in cast("dict[str, object]", obj).values():
            result = _heuristic_find_jobs(v, depth + 1)
            if result:
                return result
    return []


def _origin(source_url: str) -> str:
    parts = urlsplit(source_url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _to_record(item: dict[str, object], origin: str) -> RawJobRecord | None:
    title = ""
    for key in _TITLE_KEYS:
        val = item.get(key)
        if val:
            title = str(val).strip()
            break
    if not title:
        return None
    url = ""
    for key in _URL_KEYS:
        val = item.get(key)
        if val:
            url = str(val).strip()
            break
    if url.startswith("/"):
        url = origin + url
    location = ""
    for key in _LOCATION_KEYS:
        val = item.get(key)
        if val:
            location = str(val).strip()
            break
    ext_id = hashlib.sha256(f"{title}|{url}".encode()).hexdigest()[:16]
    return RawJobRecord(
        external_id=ext_id,
        title=title,
        location=location,
        url=url,
        provenance=API_CAPTURE_PROVENANCE,
    )


class CrawlAgent:
    """Crawl a source autonomously via ego + (ReAct / API capture / LLM extraction)."""

    def __init__(
        self,
        tool: BrowserTool,
        extractor: LLMJobExtractor,
        *,
        model_client: StructuredModelClient | None = None,
    ) -> None:
        self._tool = tool
        self._extractor = extractor
        self._model = model_client

    def crawl(
        self,
        source_url: str,
        *,
        trigger_script: str = "",
        api_substr: str = "",
        max_pages: int = 20,
        wait_s: float = 7.0,
    ) -> list[RawJobRecord]:
        """Crawl ``source_url``, exploring deeper if the first page has 0 jobs.

        Order:
        1. Navigate to the source.
        2. When a model client is present and enabled, run the ReAct loop first
           (the model adapts to the site: navigate/click/scroll/capture/extract).
           This is the primary Tier 2 path and runs before any hardcoded
           extraction so an empty first page does not short-circuit it.
        3. If the ReAct loop yields nothing (or no model is configured), fall
           back to ``_try_current_page`` (API capture + LLM extraction).
        4. Last resort: follow job-list links found on the current page.
        """
        origin = _origin(source_url)
        self._tool.navigate(source_url, wait_s=wait_s)
        trigger = trigger_script or GENERIC_TRIGGER

        # Primary: ReAct loop when a model is available.
        if self._model is not None and self._model.is_enabled:
            records = self._llm_agent_loop(
                origin,
                max_pages=max_pages,
                wait_s=wait_s,
            )
            if records:
                return records

        # Fallback: API capture + LLM extraction on the current page.
        records = self._try_current_page(
            origin,
            trigger=trigger,
            api_substr=api_substr,
            max_pages=max_pages,
            wait_s=wait_s,
        )
        if records:
            return records

        # Last resort: follow job-list links found on the current page.
        for link in self._find_job_list_links():
            self._tool.navigate(link, wait_s=wait_s)
            records = self._try_current_page(
                origin,
                trigger=trigger,
                api_substr=api_substr,
                max_pages=max_pages,
                wait_s=wait_s,
            )
            if records:
                return records
        return []

    def _try_current_page(
        self,
        origin: str,
        *,
        trigger: str,
        api_substr: str,
        max_pages: int,
        wait_s: float,
    ) -> list[RawJobRecord]:
        """Try API capture + LLM extraction on the page the browser is on."""
        caps = self._tool.capture_network(
            url_contains=api_substr,
            trigger_script=trigger,
            wait_s=wait_s,
        )
        # First pass: check if any captured response ALREADY has jobs
        # (PlaywrightTool captures initial-load APIs — no replay needed).
        for cap in caps:
            if cap.method not in ("POST", "GET") or not cap.url:
                continue
            try:
                resp_json = json.loads(cap.response)
            except (ValueError, TypeError):
                continue
            arr = _parse_job_array(resp_json)
            if arr:
                records = [r for r in (_to_record(item, origin) for item in arr) if r]
                if records:
                    # Try to paginate via replay for more jobs.
                    extra = self._safe_replay(cap, origin, max_pages)
                    return records + extra
        # Second pass: replay each captured API (ego path — hook may have
        # truncated the response). Safe-replay catches fetch errors.
        for cap in caps:
            if cap.method not in ("POST", "GET") or not cap.url:
                continue
            records = self._safe_replay(cap, origin, max_pages)
            if records:
                return records
        html = self._tool.capture_html()
        return self._extractor.extract(html)

    def _safe_replay(self, api: CapturedApiCall, origin: str, max_pages: int) -> list[RawJobRecord]:
        """Try to paginate via fetch_in_page; return [] on any error."""
        try:
            return self._crawl_via_api(api, origin=origin, max_pages=max_pages)
        except Exception:
            return []

    def _find_job_list_links(self) -> list[str]:
        """Extract links from the current page that look like job-list pages."""
        script = (
            "(() => {"
            "  var links = Array.from(document.querySelectorAll('a[href]'));"
            "  var p = /(job|position|search|list|opening|vacanc|career"
            "|zhaopin|gangwei|zhiwei|shehui)/i;"
            "  var seen = new Set(); var out = [];"
            "  links.forEach(function(a) {"
            "    var h = a.href;"
            "    if (h && h.indexOf('http') === 0 && !seen.has(h) && p.test(h))"
            "      { seen.add(h); out.push(h); }"
            "  });"
            "  return JSON.stringify(out.slice(0, 10));"
            "})()"
        )
        try:
            result = self._tool.run_page_script(script)
            raw = json.loads(result) if isinstance(result, str) else result
            links = cast("list[object]", raw)
            return [str(link) for link in links if isinstance(link, str)][:5]
        except (ValueError, TypeError, RuntimeError):
            pass
        return []

    def _llm_agent_loop(
        self,
        origin: str,
        *,
        max_pages: int,
        wait_s: float,
        max_steps: int = 6,
    ) -> list[RawJobRecord]:
        """Full LLM-driven crawl loop: observe -> decide -> act -> repeat.

        The model sees the page state (URL, title, visible text, interactive
        elements) and decides what to do: navigate, click, wait, scroll,
        capture network, extract from HTML, or give up. Adapts to any site
        without hardcoded triggers.

        Returns ``[]`` immediately when no model client is configured or it is
        disabled — the caller then falls back to ``_try_current_page``.
        """
        if self._model is None or not self._model.is_enabled:
            return []
        action_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "reasoning"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "extract",
                        "click",
                        "navigate",
                        "wait",
                        "scroll",
                        "capture_network",
                        "give_up",
                    ],
                },
                "target": {"type": "string", "maxLength": 200},
                "reasoning": {"type": "string", "maxLength": 500},
            },
        }
        for step in range(max_steps):
            page_state = self._get_page_state()
            request = StructuredModelRequest(
                task_type="crawl_agent",
                system_prompt=(
                    "You are an autonomous web crawler exploring a company "
                    "career page to find job listings. You see the current "
                    "page state. Decide ONE action to take next.\n\n"
                    "Actions:\n"
                    "- extract: Job titles are visible on this page NOW. "
                    "Extract them.\n"
                    "- click: Click a button/link to reach the job list. "
                    "Provide the exact text in 'target'.\n"
                    "- navigate: Open a different URL on the same site that "
                    "looks like the job list. Provide the URL in 'target'.\n"
                    "- wait: The page is still loading. Wait a few seconds.\n"
                    "- scroll: Scroll down to reveal more content.\n"
                    "- capture_network: Check what API calls the page made "
                    "(may contain job data in JSON).\n"
                    "- give_up: This page has no path to job listings.\n"
                    "Always provide 'reasoning' for your choice."
                ),
                user_prompt=f"Step {step + 1}/{max_steps}.",
                untrusted_content=page_state,
                schema_name="crawl_agent",
                schema=action_schema,
                max_tokens=512,
                timeout_seconds=60.0,
            )
            try:
                response = self._model.invoke(request)
            except Exception:
                break
            result = cast("dict[str, object]", response.result)
            action = str(result.get("action", "give_up"))
            target = str(result.get("target", ""))
            if action == "give_up":
                break
            if action == "extract":
                html = self._tool.capture_html()
                records = self._extractor.extract(html)
                if records:
                    return records
            elif action == "click" and target:
                self._click_text(target)
            elif action == "navigate" and target:
                self._tool.navigate(target, wait_s=wait_s)
            elif action == "wait":
                import time

                time.sleep(int(wait_s))
            elif action == "scroll":
                self._tool.run_page_script("window.scrollTo(0, document.body.scrollHeight)")
            elif action == "capture_network":
                caps = self._tool.capture_network(
                    url_contains="",
                    trigger_script="",
                    wait_s=3,
                )
                for cap in caps:
                    try:
                        arr = _parse_job_array(json.loads(cap.response[:20000]))
                    except (ValueError, TypeError):
                        continue
                    if arr:
                        records = [r for r in (_to_record(item, origin) for item in arr) if r]
                        if records:
                            return records
        return []

    def _get_page_state(self) -> str:
        """Get a compact description of the current page for the LLM."""
        try:
            state = self._tool.run_page_script(
                "(() => {"
                " var url = window.location.href;"
                " var title = document.title || '';"
                " var body = (document.body?.innerText || '').slice(0, 3000);"
                " var sels = 'a,button,input,[role=button],[role=tab]';"
                " var els = Array.from(document.querySelectorAll(sels));"
                " var interactive = els.slice(0, 30).map(function(e) {"
                "  var t = (e.textContent||e.value||e.placeholder||'')"
                "    .trim().slice(0, 60);"
                "  return t ? ('- ' + t) : '';"
                " }).filter(Boolean).join('\\n');"
                " return 'URL: ' + url + '\\nTITLE: ' + title"
                "  + '\\n\\nVISIBLE TEXT:\\n' + body"
                "  + '\\n\\nCLICKABLE ELEMENTS:\\n' + interactive;"
                "})()"
            )
            return str(state) if state else ""
        except (ValueError, TypeError, RuntimeError):
            return ""

    def _click_text(self, text: str) -> bool:
        """Click the first element on the page whose text matches."""
        script = (
            "(() => { var t = " + json.dumps(text) + ";"
            " var els = document.querySelectorAll("
            "'a,button,[role=button],span,li,div');"
            " for (var i = 0; i < els.length; i++) {"
            " var et = (els[i].textContent || '').trim();"
            " if (et.indexOf(t) !== -1 && et.length < t.length + 50)"
            " { els[i].click(); return 'clicked'; } }"
            " return 'not-found'; })()"
        )
        try:
            result = self._tool.run_page_script(script)
            return result == "clicked"
        except (ValueError, TypeError, RuntimeError):
            return False

    def _crawl_via_api(
        self,
        api: CapturedApiCall,
        *,
        origin: str,
        max_pages: int,
    ) -> list[RawJobRecord]:
        records: list[RawJobRecord] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            body = self._page_body(api.body, page)
            res = self._tool.fetch_in_page(
                api.url,
                method=api.method,
                body=body,
                headers={"content-type": "application/json"} if body else None,
            )
            try:
                parsed: object = json.loads(res.body)
            except (ValueError, TypeError):
                break
            arr = _parse_job_array(parsed)
            if not arr:
                break
            for item in arr:
                record = _to_record(item, origin)
                if record is not None and record.external_id not in seen:
                    seen.add(record.external_id)
                    records.append(record)
            # Stop when a page returns fewer than a full page (last page).
            if len(arr) < 2:
                break
        return records

    @staticmethod
    def _page_body(body_str: str | None, page: int) -> str | None:
        if not body_str:
            return None
        try:
            data = cast("dict[str, object]", json.loads(body_str))
        except (ValueError, TypeError):
            return body_str
        for key in ("pageIndex", "pageNo", "page", "pageNum"):
            if key in data:
                data[key] = page
        return json.dumps(data, ensure_ascii=False)
