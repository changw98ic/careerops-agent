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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from careerops.adapters.job_sources import RawJobRecord
from careerops.application.llm_job_extraction import LLMJobExtractor
from careerops.infrastructure.ego_tool import BrowserTool, CapturedApiCall, PageFetchResult
from careerops.model_gateway.base import StructuredModelClient, StructuredModelRequest
from careerops.recipes.skill_loader import Skill, load_skills

# Canonical Tier 2 skill catalog. ``crawl_agent.py`` lives at
# ``<repo>/src/careerops/application/``, so ``parents[3]`` is the repo root and
# the catalog is ``<repo>/vendor/crawl-recipes/skills``. Mirrors
# ``DEFAULT_RECIPES_DIR`` in ``m1_crawl_sink`` without dragging the agent into
# the infrastructure layer.
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[3] / "vendor" / "crawl-recipes" / "skills"

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

# Base ReAct system prompt used when no Tier 2 skill matches the source URL
# (the blind-run path). When a skill *does* match, its playbook is prepended
# (see :meth:`CrawlAgent._build_react_system_prompt`) so the model follows the
# site-specific steps instead of guessing.
_REACT_SYSTEM_PROMPT_BASE = (
    "You are an autonomous web crawler exploring a company career page to find "
    "job listings. You see the current page state. Decide ONE action to take "
    "next.\n\n"
    "Actions:\n"
    "- extract: Job titles are visible on this page NOW. Extract them.\n"
    "- click: Click a button/link to reach the job list. Provide the exact "
    "text in 'target'.\n"
    "- navigate: Open a different URL on the same site that looks like the "
    "job list. Provide the URL in 'target'.\n"
    "- wait: The page is still loading. Wait a few seconds.\n"
    "- scroll: Scroll down to reveal more content.\n"
    "- capture_network: Check what API calls the page made (may contain job "
    "data in JSON).\n"
    "- give_up: This page has no path to job listings.\n"
    "Always provide 'reasoning' for your choice."
)


class CrawlActionLimitReached(RuntimeError):
    """Raised before a browser operation would exceed a run/global budget."""


class CrawlTimeLimitReached(RuntimeError):
    """Raised before a browser operation would exceed the wall-clock limit."""


@dataclass(frozen=True, slots=True)
class CrawlAgentRunResult:
    """Measured result returned by the bounded Tier 2 crawl path."""

    records: tuple[RawJobRecord, ...] = ()
    action_count: int = 0
    consecutive_empty_pages: int = 0
    stop_reason: str = "completed"


@dataclass(slots=True)
class _CrawlMetrics:
    max_consecutive_empty: int = 3
    seen_identities: set[str] = field(default_factory=lambda: set[str]())
    consecutive_empty_pages: int = 0

    def note_page(self, records: list[RawJobRecord]) -> None:
        new_ids = {
            record.external_id
            for record in records
            if record.external_id and record.external_id not in self.seen_identities
        }
        if new_ids:
            self.seen_identities.update(new_ids)
            self.consecutive_empty_pages = 0
        else:
            self.consecutive_empty_pages += 1


class _BoundedBrowserTool:
    """Counts and authorizes each concrete BrowserTool operation."""

    def __init__(
        self,
        delegate: BrowserTool,
        *,
        max_actions: int,
        deadline: float,
        consume_action: Callable[[], bool] | None,
    ) -> None:
        self._delegate = delegate
        self._max_actions = max_actions
        self._deadline = deadline
        self._consume_action = consume_action
        self.action_count = 0

    @property
    def is_ready(self) -> bool:
        return self._delegate.is_ready

    def _before_action(self) -> None:
        if time.monotonic() >= self._deadline:
            raise CrawlTimeLimitReached("Tier 2 wall-clock limit reached")
        if self.action_count >= self._max_actions:
            raise CrawlActionLimitReached("Tier 2 browser-action limit reached")
        if self._consume_action is not None and not self._consume_action():
            raise CrawlActionLimitReached("global Tier 2 browser-action budget exhausted")
        self.action_count += 1

    def navigate(self, url: str, *, wait_s: float = 8.0) -> dict[str, str]:
        self._before_action()
        return self._delegate.navigate(url, wait_s=wait_s)

    def capture_html(self) -> str:
        self._before_action()
        return self._delegate.capture_html()

    def run_page_script(self, script: str) -> object:
        self._before_action()
        return self._delegate.run_page_script(script)

    def capture_network(
        self,
        *,
        url_contains: str = "",
        trigger_script: str = "",
        wait_s: float = 6.0,
    ) -> list[CapturedApiCall]:
        self._before_action()
        return self._delegate.capture_network(
            url_contains=url_contains,
            trigger_script=trigger_script,
            wait_s=wait_s,
        )

    def fetch_in_page(
        self,
        url: str,
        *,
        method: str = "GET",
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> PageFetchResult:
        self._before_action()
        return self._delegate.fetch_in_page(
            url,
            method=method,
            body=body,
            headers=headers,
        )

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
    if isinstance(obj, list):
        items = cast("list[object]", obj)
        if not items or not isinstance(items[0], dict):
            return []
        sample = cast("dict[str, object]", items[0])
        if any(k in sample for k in _TITLE_KEYS):
            return cast("list[dict[str, object]]", items)
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
        skills: dict[str, Skill] | None = None,
        skills_dir: Path | None = None,
    ) -> None:
        self._tool = tool
        self._extractor = extractor
        self._model = model_client
        self._metrics: _CrawlMetrics | None = None
        self._run_lock = threading.Lock()
        # Tier 2 skill playbooks. When a source URL matches a skill, the
        # skill's Markdown body is prepended to the ReAct system prompt so the
        # model follows a known-good playbook instead of blind ReAct. Default
        # loads the shipped vendor catalog (``vendor/crawl-recipes/skills``);
        # a missing directory yields ``{}`` (load_skills returns empty) and a
        # malformed SKILL.md degrades to ``{}`` rather than crashing the agent
        # in production — the loader itself stays strict so CI catches bad
        # skills via its own validation tests.
        if skills is not None:
            self._skills: dict[str, Skill] = dict(skills)
        else:
            directory = skills_dir or DEFAULT_SKILLS_DIR
            try:
                self._skills = load_skills(directory)
            except (OSError, ValueError):
                self._skills = {}

    def close(self) -> None:
        """Release the process-owned browser, when the tool supports it."""
        close = getattr(self._tool, "close", None)
        if callable(close):
            close()

    def crawl_bounded(
        self,
        source_url: str,
        *,
        max_actions: int = 30,
        max_duration_s: float = 300.0,
        max_consecutive_empty: int = 3,
        session_ref: str | None = None,
        consume_action: Callable[[], bool] | None = None,
        trigger_script: str = "",
        api_substr: str = "",
        max_pages: int = 20,
        wait_s: float = 7.0,
        source_type: str = "",
    ) -> CrawlAgentRunResult:
        """Serialize runs because one browser page/session belongs to this agent."""
        with self._run_lock:
            return self._crawl_bounded_locked(
                source_url,
                max_actions=max_actions,
                max_duration_s=max_duration_s,
                max_consecutive_empty=max_consecutive_empty,
                session_ref=session_ref,
                consume_action=consume_action,
                trigger_script=trigger_script,
                api_substr=api_substr,
                max_pages=max_pages,
                wait_s=wait_s,
                source_type=source_type,
            )

    def _crawl_bounded_locked(
        self,
        source_url: str,
        *,
        max_actions: int = 30,
        max_duration_s: float = 300.0,
        max_consecutive_empty: int = 3,
        session_ref: str | None = None,
        consume_action: Callable[[], bool] | None = None,
        trigger_script: str = "",
        api_substr: str = "",
        max_pages: int = 20,
        wait_s: float = 7.0,
        source_type: str = "",
    ) -> CrawlAgentRunResult:
        """Run the agent with limits enforced at every browser operation."""
        if max_actions <= 0 or max_duration_s <= 0 or max_consecutive_empty <= 0:
            raise ValueError("Tier 2 limits must be positive")

        bind_session = getattr(self._tool, "bind_session", None)
        if not callable(bind_session):
            if session_ref:
                return CrawlAgentRunResult(stop_reason="session_expired")
        else:
            bind_session(session_ref)

        original_tool = self._tool
        bounded = _BoundedBrowserTool(
            original_tool,
            max_actions=max_actions,
            deadline=time.monotonic() + max_duration_s,
            consume_action=consume_action,
        )
        self._tool = cast("BrowserTool", bounded)
        self._metrics = _CrawlMetrics(max_consecutive_empty=max_consecutive_empty)
        stop_reason = "completed"
        records: list[RawJobRecord] = []
        try:
            records = self.crawl(
                source_url,
                trigger_script=trigger_script,
                api_substr=api_substr,
                max_pages=max_pages,
                wait_s=wait_s,
                source_type=source_type,
            )
            if bounded.action_count >= max_actions:
                stop_reason = "action_limit"
            elif self._metrics.consecutive_empty_pages >= max_consecutive_empty:
                stop_reason = "duplicate_stop"
            elif session_ref and not records:
                html = self._tool.capture_html().lower()
                if any(
                    signal in html
                    for signal in (
                        "login required",
                        "sign in to continue",
                        "please log in",
                        "you must be logged in",
                        "authentication required",
                    )
                ):
                    stop_reason = "session_expired"
        except CrawlActionLimitReached:
            stop_reason = "action_limit"
        except CrawlTimeLimitReached:
            stop_reason = "time_limit"
        finally:
            metrics = self._metrics
            self._metrics = None
            self._tool = original_tool

        deduped: dict[str, RawJobRecord] = {
            record.external_id: record
            for record in records
            if record.external_id
        }
        return CrawlAgentRunResult(
            records=tuple(deduped.values()),
            action_count=bounded.action_count,
            consecutive_empty_pages=metrics.consecutive_empty_pages,
            stop_reason=stop_reason,
        )

    def _note_page(self, records: list[RawJobRecord]) -> bool:
        if self._metrics is None:
            return False
        self._metrics.note_page(records)
        return (
            self._metrics.consecutive_empty_pages
            >= self._metrics.max_consecutive_empty
        )

    def _empty_page_limit_reached(self) -> bool:
        return (
            self._metrics is not None
            and self._metrics.consecutive_empty_pages
            >= self._metrics.max_consecutive_empty
        )

    def crawl(
        self,
        source_url: str,
        *,
        trigger_script: str = "",
        api_substr: str = "",
        max_pages: int = 20,
        wait_s: float = 7.0,
        source_type: str = "",
    ) -> list[RawJobRecord]:
        """Crawl ``source_url``, exploring deeper if the first page has 0 jobs.

        Order:
        1. Navigate to the source.
        2. When a model client is present and enabled, run the ReAct loop first
           (the model adapts to the site: navigate/click/scroll/capture/extract).
           This is the primary Tier 2 path and runs before any hardcoded
           extraction so an empty first page does not short-circuit it. When a
           Tier 2 skill matches ``source_url`` (or the explicit
           ``source_type`` hint), its playbook is injected into the ReAct
           system prompt so the model follows known-good steps instead of
           blind ReAct.
        3. If the ReAct loop yields nothing (or no model is configured), fall
           back to ``_try_current_page`` (API capture + LLM extraction).
        4. Last resort: follow job-list links found on the current page.
        """
        origin = _origin(source_url)
        self._tool.navigate(source_url, wait_s=wait_s)
        trigger = trigger_script or GENERIC_TRIGGER
        skill = self._match_skill(source_url, source_type=source_type)

        # Primary: ReAct loop when a model is available.
        if self._model is not None and self._model.is_enabled:
            records = self._llm_agent_loop(
                origin,
                skill=skill,
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
                    self._note_page(records)
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
            if self._empty_page_limit_reached():
                return []
        html = self._tool.capture_html()
        records = self._extractor.extract(html, source_url=origin)
        self._note_page(records)
        return records

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

    def _match_skill(self, url: str, *, source_type: str = "") -> Skill | None:
        """Return the Tier 2 skill whose match hints apply to ``url``.

        Honors an explicit ``source_type`` hint first (lets the sink
        disambiguate when a URL could match multiple skills), then falls back
        to URL matching against ``match.host_suffix`` and
        ``match.url_patterns``. Matching is case-insensitive substring on the
        full URL — permissive enough for path-embedded tenants (e.g. workday)
        while staying deterministic. Returns ``None`` when no skill is loaded
        or none matches (the caller then blind-runs ReAct).
        """
        if not self._skills:
            return None
        if source_type:
            exact = self._skills.get(source_type)
            if exact is not None:
                return exact
        lowered = url.lower()
        for skill in self._skills.values():
            match = skill.match
            host_suffix = match.get("host_suffix")
            if (
                isinstance(host_suffix, str)
                and host_suffix
                and host_suffix.lower() in lowered
            ):
                return skill
            url_patterns = match.get("url_patterns")
            if isinstance(url_patterns, list):
                for pattern in url_patterns:
                    if (
                        isinstance(pattern, str)
                        and pattern
                        and pattern.lower() in lowered
                    ):
                        return skill
        return None

    def _build_react_system_prompt(self, skill: Skill | None) -> str:
        """Build the ReAct system prompt, injecting ``skill.body`` when present.

        No skill (or empty body) → the blind-run base prompt, unchanged. A
        matched skill → the playbook is wrapped in explicit ``SKILL PLAYBOOK``
        delimiters and prepended, with a one-line framing note telling the
        model which ``source_type`` it is crawling.
        """
        if skill is None or not skill.body:
            return _REACT_SYSTEM_PROMPT_BASE
        return (
            f"You are crawling a known site type ({skill.source_type}). "
            "Apply the following skill playbook to drive every action you "
            "take; it overrides the generic heuristics below.\n\n"
            "--- SKILL PLAYBOOK ---\n"
            f"{skill.body}\n"
            "--- END PLAYBOOK ---\n\n"
            f"{_REACT_SYSTEM_PROMPT_BASE}"
        )

    def _llm_agent_loop(
        self,
        origin: str,
        *,
        skill: Skill | None = None,
        max_pages: int,
        wait_s: float,
        max_steps: int = 6,
    ) -> list[RawJobRecord]:
        """Full LLM-driven crawl loop: observe -> decide -> act -> repeat.

        The model sees the page state (URL, title, visible text, interactive
        elements) and decides what to do: navigate, click, wait, scroll,
        capture network, extract from HTML, or give up. Adapts to any site
        without hardcoded triggers.

        When ``skill`` is provided (the caller matched a Tier 2 skill for this
        source), its playbook is prepended to the system prompt so the model
        follows site-specific steps instead of guessing.

        Returns ``[]`` immediately when no model client is configured or it is
        disabled — the caller then falls back to ``_try_current_page``.
        """
        if self._model is None or not self._model.is_enabled:
            return []
        system_prompt = self._build_react_system_prompt(skill)
        action_schema: dict[str, object] = {
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
                system_prompt=system_prompt,
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
                if self._note_page([]):
                    break
                continue
            arr = _parse_job_array(parsed)
            if not arr:
                if self._note_page([]):
                    break
                continue
            page_records: list[RawJobRecord] = []
            for item in arr:
                record = _to_record(item, origin)
                if record is not None and record.external_id not in seen:
                    seen.add(record.external_id)
                    records.append(record)
                    page_records.append(record)
            if self._note_page(page_records):
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
