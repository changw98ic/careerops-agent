from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from careerops.adapters.http_fetcher import FetchedResponse
from careerops.infrastructure.temporal import ego_browser_executor as ego
from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink
from careerops.workflows.m1_contracts import CrawlJobSourceInput


def test_ego_executor_rejects_ssrf_before_dependency_lookup() -> None:
    executor = ego.EgoBrowserExecutor(binary="missing-ego-browser")

    with pytest.raises(ego.BrowserPolicyError):
        executor.fetch("http://127.0.0.1/internal")


def test_ego_executor_reports_missing_binary_without_http_fallback(monkeypatch) -> None:
    monkeypatch.setattr(ego, "validate_url", lambda _url: None)
    executor = ego.EgoBrowserExecutor(binary="missing-ego-browser")

    with pytest.raises(ego.BrowserDependencyError, match="unavailable"):
        executor.fetch("https://jobs.example.test")


def test_ego_executor_returns_bounded_browser_capture(monkeypatch) -> None:
    monkeypatch.setattr(ego, "validate_url", lambda _url: None)
    monkeypatch.setattr(ego.shutil, "which", lambda _binary: "/usr/local/bin/ego-browser")
    body = "<html><script type='application/ld+json'>{}</script></html>"
    stdout = (
        "noise\n"
        + ego._RESULT_MARKER
        + json.dumps({"finalUrl": "https://jobs.example.test/jobs", "html": body})
    )

    class _Completed:
        returncode = 0

        def __init__(self, output: str) -> None:
            self.stdout = output

    calls: list[list[str]] = []

    def _run(args, **_kwargs):
        calls.append(args)
        return _Completed(stdout)

    monkeypatch.setattr(ego.subprocess, "run", _run)
    response = ego.EgoBrowserExecutor().fetch("https://jobs.example.test")

    assert response.status_code == 200
    assert response.body == body
    assert calls and calls[0][:3] == ["ego-browser", "nodejs", "-e"]


def test_sink_selects_ego_executor_explicitly() -> None:
    fetched = FetchedResponse(
        status_code=200,
        final_url="https://jobs.example.test",
        fetched_at=datetime.now(UTC),
        response_hash="a" * 64,
        body='{"jobs": []}',
    )
    browser_calls: list[str] = []

    class _Browser:
        def fetch(self, url: str) -> FetchedResponse:
            browser_calls.append(url)
            return fetched

    def _http(_url: str) -> FetchedResponse:
        raise AssertionError("HTTP executor must not be used for ego sources")

    sink = RealCrawlActivitySink(fetcher=_http, browser_executor=_Browser())  # type: ignore[arg-type]
    request = CrawlJobSourceInput(
        source_id="source",
        company_id="company",
        company_name="",
        source_type="greenhouse",
        base_url="https://jobs.example.test",
        executor_mode="ego",
    )

    result = asyncio.run(sink.crawl_source_with_signals(request))

    assert result.status_code == 200
    assert browser_calls == ["https://jobs.example.test"]
