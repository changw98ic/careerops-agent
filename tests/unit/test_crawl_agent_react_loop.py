"""CrawlAgent ReAct loop dispatch tests (real-autonomous-career-loop, spec 4).

Pins the ReAct behaviour of ``CrawlAgent._llm_agent_loop`` INDEPENDENTLY of the
ingest layer. A ``_FakeBrowserTool`` records every browser primitive (navigate /
capture_html / run_page_script / capture_network) and a ``_FakeModelClient``
replays a scripted action sequence, so each test drives exactly one concrete
action dispatch:

- scroll -> extract: extractor returns records -> ``crawl()`` returns them.
- extract with zero records: the loop does NOT terminate, it keeps going.
- capture_network: a canned JSON job array is parsed via ``_parse_job_array`` /
  ``_to_record`` and the resulting records carry ``provenance='api-capture'``
  with detail URLs resolved against the page origin.
- click / navigate: the matching ``BrowserTool`` primitive is invoked.
- give_up: the loop stops immediately (a single model call) and returns ``[]``;
  ``crawl()`` then falls through to ``_try_current_page``.
- max_steps: a non-terminating script runs at most ``max_steps`` times.

These tests are disjoint from ``test_llm_extraction_provenance.py`` (which pins
the extractor + ``_to_crawled_posting`` sink contract): they assert dispatch
mechanics only, never touching ingest.
"""

from __future__ import annotations

from typing import Any

from careerops.adapters.job_sources import RawJobRecord
from careerops.application.crawl_agent import CrawlAgent, GENERIC_TRIGGER, _origin
from careerops.infrastructure.ego_tool import CapturedApiCall, PageFetchResult
from careerops.model_gateway.base import StructuredModelRequest, StructuredModelResponse

_SOURCE_URL = "https://demo.test/careers"
_ORIGIN = _origin(_SOURCE_URL)  # "https://demo.test"
_SCROLL_SCRIPT = "window.scrollTo(0, document.body.scrollHeight)"


class _FakeBrowserTool:
    """In-memory ``BrowserTool`` that records every primitive call.

    ``run_page_script`` discriminates by script content so a single fake can
    serve the page-state snapshot (``_get_page_state``), the scroll primitive,
    the click primitive (``_click_text``) and the job-list-link scan
    (``_find_job_list_links``) without per-test stub wiring.
    """

    def __init__(
        self,
        *,
        html: str = "<html><body>careers</body></html>",
        network_calls: list[CapturedApiCall] | None = None,
        click_succeeds: bool = True,
    ) -> None:
        self._html = html
        self._network_calls = list(network_calls or [])
        self._click_succeeds = click_succeeds
        self.navigate_calls: list[tuple[str, float]] = []
        self.capture_html_calls: int = 0
        self.run_page_script_calls: list[str] = []
        self.capture_network_calls: list[dict[str, Any]] = []
        self.fetch_in_page_calls: list[dict[str, Any]] = []

    @property
    def is_ready(self) -> bool:
        return True

    def navigate(self, url: str, *, wait_s: float = 8.0) -> dict[str, str]:
        self.navigate_calls.append((url, wait_s))
        return {"url": url, "title": "Demo Careers"}

    def capture_html(self) -> str:
        self.capture_html_calls += 1
        return self._html

    def run_page_script(self, script: str) -> Any:
        self.run_page_script_calls.append(script)
        # _click_text builds a script containing els[i].click(); report a
        # deterministic clicked / not-found result for the click action.
        if "els[i].click()" in script:
            return "clicked" if self._click_succeeds else "not-found"
        # _find_job_list_links (crawl() priority-3 fall-through only) returns a
        # JSON list; keep it empty so crawl() terminates cleanly.
        if "a[href]" in script and "push" in script:
            return "[]"
        # Everything else (page-state snapshot, scroll primitive) just needs a
        # truthy string return value.
        return "URL: https://demo.test/careers\nTITLE: Demo Careers"

    def capture_network(
        self,
        *,
        url_contains: str = "",
        trigger_script: str = "",
        wait_s: float = 6.0,
    ) -> list[CapturedApiCall]:
        self.capture_network_calls.append(
            {"url_contains": url_contains, "trigger_script": trigger_script, "wait_s": wait_s}
        )
        return list(self._network_calls)

    def fetch_in_page(
        self,
        url: str,
        *,
        method: str = "GET",
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> PageFetchResult:
        self.fetch_in_page_calls.append({"url": url, "method": method, "body": body})
        return PageFetchResult(status=200, body="{}")


class _FakeExtractor:
    """Stand-in ``LLMJobExtractor`` that returns a fixed record list."""

    def __init__(self, records: list[RawJobRecord]) -> None:
        self._records = list(records)
        self.calls: int = 0

    def extract(
        self,
        html: str,
        *,
        source_url: str = "",
        trace_id: str = "",
    ) -> list[RawJobRecord]:
        self.calls += 1
        return list(self._records)


class _FakeModelClient:
    """``StructuredModelClient`` that replays a scripted (action, target) list."""

    def __init__(self, actions: list[tuple[str, str]]) -> None:
        # Each entry is (action, target), popped in order on each invoke().
        self._actions = list(actions)
        self.invoke_count: int = 0
        self.requests: list[StructuredModelRequest] = []

    @property
    def is_enabled(self) -> bool:
        return True

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:
        self.invoke_count += 1
        self.requests.append(request)
        action, target = self._actions.pop(0) if self._actions else ("give_up", "")
        return StructuredModelResponse(
            task_type=request.task_type,
            result={"thought": "scripted step", "action": action, "target": target},
            model_id="test-react-fake",
            prompt_version="crawl_react-test",
            is_review_only=False,
            trace_id=request.trace_id,
        )


class _DisabledModelClient:
    """Model client whose ``is_enabled`` is False (the pre-power-on state)."""

    @property
    def is_enabled(self) -> bool:
        return False

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:  # pragma: no cover
        raise AssertionError("disabled client must never be invoked")


def _llm_records() -> list[RawJobRecord]:
    return [
        RawJobRecord(
            external_id="abc123",
            title="Senior Engineer",
            location="Remote",
            url="https://demo.test/j/1",
            description="Lead the platform team.",
            provenance="llm-extraction",
        )
    ]


def _build_agent(
    browser: _FakeBrowserTool,
    extractor: _FakeExtractor,
    actions: list[tuple[str, str]],
) -> tuple[CrawlAgent, _FakeModelClient]:
    model = _FakeModelClient(actions)
    agent = CrawlAgent(browser, extractor, model_client=model)
    return agent, model


class TestCrawlAgentReActLoop:
    def test_disabled_model_skips_loop_without_dispatch(self) -> None:
        # The ReAct loop early-returns [] unless a model client is present AND
        # enabled. No browser primitive should fire.
        browser = _FakeBrowserTool()
        extractor = _FakeExtractor(_llm_records())

        no_model = CrawlAgent(browser, extractor, model_client=None)
        assert no_model._llm_agent_loop(_ORIGIN, max_pages=1, wait_s=0, max_steps=4) == []
        assert browser.run_page_script_calls == []
        assert browser.navigate_calls == []

        disabled = CrawlAgent(browser, extractor, model_client=_DisabledModelClient())
        assert disabled._llm_agent_loop(_ORIGIN, max_pages=1, wait_s=0, max_steps=4) == []
        assert browser.capture_network_calls == []

    def test_scroll_then_extract_returns_records_via_crawl(self) -> None:
        # The prescribed happy path: step1 scroll, step2 extract -> the extractor
        # returns records and crawl() surfaces them. Pins scroll + extract
        # dispatch end-to-end through crawl(), independent of ingest.
        browser = _FakeBrowserTool()
        extractor = _FakeExtractor(_llm_records())
        agent, model = _build_agent(browser, extractor, [("scroll", ""), ("extract", "")])

        records = agent.crawl(_SOURCE_URL, wait_s=0, max_pages=1)

        assert [r.title for r in records] == ["Senior Engineer"]
        assert all(r.provenance == "llm-extraction" for r in records)
        # crawl() navigates to the source first, then the loop runs 2 steps.
        assert model.invoke_count == 2
        assert browser.navigate_calls[0] == (_SOURCE_URL, 0.0)
        # scroll primitive fired on step 1; extract captured HTML on step 2.
        assert _SCROLL_SCRIPT in browser.run_page_script_calls
        assert browser.capture_html_calls == 1
        assert extractor.calls == 1

    def test_extract_with_no_records_keeps_loop_running(self) -> None:
        # extract must only terminate the loop when it actually finds jobs;
        # an empty extraction records an observation and the loop continues.
        browser = _FakeBrowserTool()
        extractor = _FakeExtractor([])
        agent, model = _build_agent(browser, extractor, [("extract", ""), ("give_up", "")])

        records = agent._llm_agent_loop(_ORIGIN, max_pages=1, wait_s=0, max_steps=4)

        assert records == []
        assert extractor.calls == 1
        assert browser.capture_html_calls == 1
        assert model.invoke_count == 2  # extract (no termination) then give_up

    def test_capture_network_action_yields_api_capture_provenance(self) -> None:
        # capture_network dispatch: the loop parses captured JSON via
        # _parse_job_array / _to_record, so records carry provenance='api-capture'
        # (distinct from the llm-extraction path) and resolve detail URLs.
        canned = CapturedApiCall(
            url="https://demo.test/api/jobs",
            method="GET",
            body=None,
            response=(
                '{"data":{"jobs":['
                '{"title":"Backend Engineer","url":"/jobs/1","location":"Berlin"}'
                "]}}"
            ),
        )
        browser = _FakeBrowserTool(network_calls=[canned])
        extractor = _FakeExtractor(_llm_records())  # must NOT be used here
        agent, model = _build_agent(browser, extractor, [("capture_network", "")])

        records = agent._llm_agent_loop(_ORIGIN, max_pages=1, wait_s=0, max_steps=4)

        assert len(records) == 1
        record = records[0]
        assert record.title == "Backend Engineer"
        assert record.provenance == "api-capture"
        assert record.url == f"{_ORIGIN}/jobs/1"  # relative URL resolved against origin
        assert record.location == "Berlin"
        # The ReAct capture_network call uses empty filters + a 3s wait.
        assert browser.capture_network_calls == [
            {"url_contains": "", "trigger_script": "", "wait_s": 3}
        ]
        assert extractor.calls == 0
        assert model.invoke_count == 1  # records found on step 1 -> return

    def test_click_action_dispatches_click_text(self) -> None:
        browser = _FakeBrowserTool(click_succeeds=True)
        agent, model = _build_agent(
            browser, _FakeExtractor([]), [("click", "View all jobs"), ("give_up", "")]
        )

        records = agent._llm_agent_loop(_ORIGIN, max_pages=1, wait_s=0, max_steps=4)

        assert records == []
        click_scripts = [s for s in browser.run_page_script_calls if "els[i].click()" in s]
        assert len(click_scripts) == 1
        assert "View all jobs" in click_scripts[0]  # target threaded into the script
        # click must not navigate; only the (suppressed) give_up follows.
        assert browser.navigate_calls == []
        assert model.invoke_count == 2

    def test_navigate_action_dispatches_navigate(self) -> None:
        browser = _FakeBrowserTool()
        target = "https://demo.test/jobs"
        agent, model = _build_agent(
            browser, _FakeExtractor([]), [("navigate", target), ("give_up", "")]
        )

        records = agent._llm_agent_loop(_ORIGIN, max_pages=1, wait_s=0, max_steps=4)

        assert records == []
        assert browser.navigate_calls == [(target, 0.0)]
        assert model.invoke_count == 2

    def test_give_up_stops_loop_immediately(self) -> None:
        browser = _FakeBrowserTool()
        agent, model = _build_agent(browser, _FakeExtractor([]), [("give_up", "")])

        records = agent._llm_agent_loop(_ORIGIN, max_pages=1, wait_s=0, max_steps=8)

        assert records == []
        # give_up breaks on step 1: exactly one model call, no action dispatch.
        assert model.invoke_count == 1
        assert browser.capture_network_calls == []
        assert browser.capture_html_calls == 0

    def test_give_up_falls_through_to_try_current_page(self) -> None:
        # When the ReAct loop gives up, crawl() must continue to the API-capture
        # + extraction fallback (_try_current_page), not return [] silently.
        browser = _FakeBrowserTool()
        extractor = _FakeExtractor([])  # fallback extraction also finds nothing
        agent, model = _build_agent(browser, extractor, [("give_up", "")])

        records = agent.crawl(_SOURCE_URL, wait_s=0, max_pages=1)

        assert records == []
        assert model.invoke_count == 1  # the loop gave up after one step
        # _try_current_page ran: it captured network with the generic trigger and
        # then ran the extractor over the captured HTML.
        assert len(browser.capture_network_calls) == 1
        assert browser.capture_network_calls[0]["trigger_script"] == GENERIC_TRIGGER
        assert browser.capture_html_calls == 1
        assert extractor.calls == 1

    def test_loop_respects_max_steps(self) -> None:
        # A non-terminating script (scroll forever) must stop at max_steps and
        # return [], never spin until the action list is exhausted.
        browser = _FakeBrowserTool()
        agent, model = _build_agent(browser, _FakeExtractor([]), [("scroll", "")] * 20)

        records = agent._llm_agent_loop(_ORIGIN, max_pages=1, wait_s=0, max_steps=3)

        assert records == []
        assert model.invoke_count == 3
        # One scroll primitive per step (page-state snapshot is the other call).
        assert browser.run_page_script_calls.count(_SCROLL_SCRIPT) == 3


class TestBoundedCrawlAgent:
    def test_counts_real_browser_operations_and_stops_before_action_four(self) -> None:
        browser = _FakeBrowserTool()
        agent = CrawlAgent(
            browser,
            _FakeExtractor([]),
            model_client=_DisabledModelClient(),
        )

        result = agent.crawl_bounded(
            _SOURCE_URL,
            max_actions=3,
            max_duration_s=30,
            max_consecutive_empty=3,
            wait_s=0,
        )

        assert result.stop_reason == "action_limit"
        assert result.action_count == 3
        actual_operations = (
            len(browser.navigate_calls)
            + browser.capture_html_calls
            + len(browser.run_page_script_calls)
            + len(browser.capture_network_calls)
            + len(browser.fetch_in_page_calls)
        )
        assert actual_operations == 3

    def test_stops_after_three_result_pages_without_new_identity(self) -> None:
        empty_api = CapturedApiCall(
            url="https://demo.test/api/jobs",
            method="POST",
            body='{"page":1}',
            response="{}",
        )
        browser = _FakeBrowserTool(network_calls=[empty_api])
        agent = CrawlAgent(
            browser,
            _FakeExtractor([]),
            model_client=_DisabledModelClient(),
        )

        result = agent.crawl_bounded(
            _SOURCE_URL,
            max_actions=30,
            max_duration_s=30,
            max_consecutive_empty=3,
            wait_s=0,
        )

        assert result.stop_reason == "duplicate_stop"
        assert result.consecutive_empty_pages == 3
        assert len(browser.fetch_in_page_calls) == 3
