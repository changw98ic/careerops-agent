from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import time
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_collector_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "collect_recruitment_pages.py"
    spec = importlib.util.spec_from_file_location("collect_recruitment_pages", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _triage_row(
    *,
    record_id: str,
    assessment: str,
    registrable_domain: str = "example.com",
    links: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "registrable_domain": registrable_domain,
        "employer_hiring_assessment": assessment,
        "links": links,
    }


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str,
        final_url: str = "https://mapped.example/careers",
        status: int = 200,
    ) -> None:
        self._body = body
        self._final_url = final_url
        self._status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._final_url

    def getcode(self) -> int:
        return self._status


class _FakeOpener:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.opened_urls: list[str] = []

    def open(self, request: Any, *, timeout: float) -> _FakeResponse:
        self.opened_urls.append(request.full_url)
        return self.responses.pop(0)


def _crawl_request(url: str = "https://mapped.example/careers") -> Any:
    collector = _load_collector_module()
    return collector.CrawlRequest(
        canonical_url=collector.canonicalize_url(url),
        url=url,
        source_record_id="mapped",
        source_index=0,
        source_assessment="first_party_career_entry",
        source_relationship="first_party",
        registrable_domain="example",
        depth=0,
    )


def _request_for_host(collector: ModuleType, host: str, path: str) -> Any:
    url = f"https://{host}{path}"
    return collector.CrawlRequest(
        canonical_url=collector.canonicalize_url(url),
        url=url,
        source_record_id=host,
        source_index=0,
        source_assessment="first_party_career_entry",
        source_relationship="first_party",
        registrable_domain=host,
        depth=0,
    )


def _patch_dns(monkeypatch: Any, collector: ModuleType) -> None:
    def fake_getaddrinfo(host: str, port: int | None, *, type: int) -> list[Any]:
        assert type == socket.SOCK_STREAM
        if host == "mapped.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.10", port or 443))]
        if host == "arbitrary-nonglobal.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.10", port or 443))]
        raise socket.gaierror(f"unexpected host {host}")

    monkeypatch.setattr(collector.socket, "getaddrinfo", fake_getaddrinfo)


def test_select_seed_urls_filters_assessments_relationships_and_canonical_dedupes() -> None:
    collector = _load_collector_module()
    rows = [
        _triage_row(
            record_id="one",
            assessment="likely_self_hiring",
            links=[
                {
                    "text": "Careers",
                    "href": "HTTPS://WWW.EXAMPLE.COM:443/careers/#team",
                    "relationship": "first_party",
                },
                {
                    "text": "Careers duplicate",
                    "href": "https://www.example.com/careers",
                    "relationship": "first_party",
                },
                {
                    "text": "Partner jobs",
                    "href": "https://jobs.example.net/jobs",
                    "relationship": "external",
                },
            ],
        ),
        _triage_row(
            record_id="two",
            assessment="no_recruitment_evidence",
            links=[
                {
                    "text": "Careers",
                    "href": "https://ignored.example.com/careers",
                    "relationship": "first_party",
                }
            ],
        ),
        _triage_row(
            record_id="three",
            assessment="external_ats_needs_verification",
            links=[
                {
                    "text": "Open roles",
                    "href": "https://boards.greenhouse.io/acme",
                    "relationship": "known_ats",
                }
            ],
        ),
    ]

    seeds = collector.select_seed_urls(rows)

    assert [seed.canonical_url for seed in seeds] == [
        "https://www.example.com/careers",
        "https://boards.greenhouse.io/acme",
    ]
    assert [seed.source_record_id for seed in seeds] == ["one", "three"]
    assert [seed.source_relationship for seed in seeds] == ["first_party", "known_ats"]
    assert [seed.seed_source for seed in seeds] == ["triage_link", "triage_link"]


def test_select_additional_seed_urls_filters_scope_and_dedupes_with_triage() -> None:
    collector = _load_collector_module()
    seen: set[str] = {"https://example.com/careers"}
    rows = [
        {
            "canonical_url": "https://example.com/careers",
            "source_sitemap_url": "https://example.com/sitemap.xml",
            "source_record_id": "duplicate",
            "source_index": 7,
            "source_assessment": "first_party_career_entry",
            "source_relationship": "first_party",
            "registrable_domain": "example.com",
        },
        {
            "canonical_url": "https://example.com/jobs/engineering",
            "source_sitemap_url": "https://example.com/sitemap.xml",
            "source_record_id": "site-one",
            "source_index": 8,
            "source_assessment": "first_party_career_entry",
            "source_relationship": "first_party",
            "registrable_domain": "example.com",
        },
        {
            "canonical_url": "https://partner.example.net/jobs",
            "source_sitemap_url": "https://example.com/sitemap.xml",
            "source_assessment": "first_party_career_entry",
            "source_relationship": "external",
            "registrable_domain": "example.com",
        },
        {
            "canonical_url": "https://example.com/blog/careers-case-study",
            "source_sitemap_url": "https://example.com/sitemap.xml",
            "source_assessment": "no_recruitment_evidence",
            "source_relationship": "first_party",
            "registrable_domain": "example.com",
        },
        {
            "canonical_url": "https://boards.greenhouse.io/acme/jobs/123",
            "source_sitemap_url": "https://boards.greenhouse.io/acme/sitemap.xml",
            "source_record_id": "ats-one",
            "source_assessment": "external_ats_needs_verification",
            "source_relationship": "known_ats",
            "registrable_domain": "example.com",
        },
    ]

    seeds = collector.select_additional_seed_urls(rows, seen)

    assert [seed.canonical_url for seed in seeds] == [
        "https://example.com/jobs/engineering",
        "https://boards.greenhouse.io/acme/jobs/123",
    ]
    assert [seed.source_record_id for seed in seeds] == ["site-one", "ats-one"]
    assert [seed.source_relationship for seed in seeds] == ["first_party", "known_ats"]
    assert [seed.discovered_from for seed in seeds] == [
        "https://example.com/sitemap.xml",
        "https://boards.greenhouse.io/acme/sitemap.xml",
    ]
    assert [seed.seed_source for seed in seeds] == [
        "additional_seed_jsonl",
        "additional_seed_jsonl",
    ]


def test_url_domain_eligibility_allows_first_party_and_known_ats_only() -> None:
    collector = _load_collector_module()

    assert collector.is_eligible_crawl_url(
        "https://jobs.example.com/open-roles", "example.com", "Open roles"
    )
    assert collector.is_eligible_crawl_url(
        "https://boards.greenhouse.io/acme", "example.com", "Careers"
    )
    assert not collector.is_eligible_crawl_url(
        "https://jobs.example.net/open-roles", "example.com", "Open roles"
    )
    assert not collector.is_eligible_crawl_url(
        "https://example.com/blog/case-study", "example.com", "Case study"
    )
    assert (
        collector.link_relationship("https://jobs.ashbyhq.com/acme", "example.com") == "known_ats"
    )


def test_fetch_page_rejects_mapped_hostname_without_sandbox_alias_flag(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_dns(monkeypatch, collector)
    opened = False

    def fail_build_opener(*_args: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("network opener must not be built without alias opt-in")

    monkeypatch.setattr(collector, "build_opener", fail_build_opener)

    outcome = collector.fetch_page(
        _crawl_request(),
        collector.CollectorConfig(timeout_seconds=2, max_response_bytes=1024),
    )

    assert outcome.raw_row is None
    assert outcome.receipt["capture_status"] == "invalid_url"
    assert opened is False


def test_fetch_page_allows_mapped_hostname_with_sandbox_alias_flag(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_dns(monkeypatch, collector)
    opener = _FakeOpener([_FakeResponse(b"aliased", content_type="text/html")])
    monkeypatch.setattr(collector, "build_opener", lambda *_args: opener)

    outcome = collector.fetch_page(
        _crawl_request(),
        collector.CollectorConfig(
            timeout_seconds=2,
            max_response_bytes=1024,
            allow_sandbox_egress_alias=True,
        ),
    )

    assert outcome.receipt["capture_status"] == "captured"
    assert outcome.raw_row is not None
    assert outcome.raw_row["response_text"] == "aliased"
    assert opener.opened_urls == ["https://mapped.example/careers"]


def test_fetch_page_reuses_thread_fetch_context_without_skipping_url_guard(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_dns(monkeypatch, collector)
    public_checks: list[str] = []
    build_calls = 0
    opener = _FakeOpener(
        [
            _FakeResponse(b"first", content_type="text/html"),
            _FakeResponse(b"second", content_type="text/html"),
        ]
    )
    original_ensure_public_http_url = collector._ensure_public_http_url

    def fake_build_opener(*_args: object) -> _FakeOpener:
        nonlocal build_calls
        build_calls += 1
        return opener

    def tracking_ensure_public_http_url(url: str, *args: object, **kwargs: object) -> None:
        public_checks.append(url)
        original_ensure_public_http_url(url, *args, **kwargs)

    monkeypatch.setattr(collector, "build_opener", fake_build_opener)
    monkeypatch.setattr(collector, "_ensure_public_http_url", tracking_ensure_public_http_url)

    config = collector.CollectorConfig(
        timeout_seconds=2,
        max_response_bytes=1024,
        allow_sandbox_egress_alias=True,
    )
    collector._set_thread_fetch_context(config)

    first = collector.fetch_page(_crawl_request(), config)
    second = collector.fetch_page(_crawl_request(), config)

    assert build_calls == 1
    assert public_checks == [
        "https://mapped.example/careers",
        "https://mapped.example/careers",
    ]
    assert [first.raw_row["response_text"], second.raw_row["response_text"]] == [
        "first",
        "second",
    ]
    assert opener.opened_urls == [
        "https://mapped.example/careers",
        "https://mapped.example/careers",
    ]


def test_fetch_page_rejects_sandbox_alias_ip_literal_even_with_flag(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    opened = False

    def fail_build_opener(*_args: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("network opener must not be built for alias IP literals")

    monkeypatch.setattr(collector, "build_opener", fail_build_opener)

    outcome = collector.fetch_page(
        _crawl_request("https://198.18.0.10/careers"),
        collector.CollectorConfig(
            timeout_seconds=2,
            max_response_bytes=1024,
            allow_sandbox_egress_alias=True,
        ),
    )

    assert outcome.raw_row is None
    assert outcome.receipt["capture_status"] == "invalid_url"
    assert opened is False


def test_fetch_page_rejects_arbitrary_non_global_dns_even_with_proxy_and_alias_flag(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_dns(monkeypatch, collector)
    monkeypatch.setenv("https_proxy", "http://proxy.example:8080")
    opened = False

    def fail_build_opener(*_args: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("network opener must not be built for arbitrary non-global DNS")

    monkeypatch.setattr(collector, "build_opener", fail_build_opener)

    outcome = collector.fetch_page(
        _crawl_request("https://arbitrary-nonglobal.example/careers"),
        collector.CollectorConfig(
            timeout_seconds=2,
            max_response_bytes=1024,
            allow_sandbox_egress_alias=True,
        ),
    )

    assert outcome.raw_row is None
    assert outcome.receipt["capture_status"] == "invalid_url"
    assert opened is False


def test_redirect_to_mapped_hostname_allows_sandbox_alias_when_flagged(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_dns(monkeypatch, collector)
    handler = collector._PublicRedirectHandler(allow_sandbox_egress_alias=True)
    redirect = handler.redirect_request(
        collector.Request("https://public.example/redirect"),
        None,
        302,
        "Found",
        {},
        "https://mapped.example/careers",
    )

    assert redirect is not None
    assert redirect.full_url == "https://mapped.example/careers"


def test_redirect_rejects_arbitrary_non_global_dns(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_dns(monkeypatch, collector)
    handler = collector._PublicRedirectHandler(allow_sandbox_egress_alias=True)

    try:
        handler.redirect_request(
            collector.Request("https://public.example/redirect"),
            None,
            302,
            "Found",
            {},
            "https://arbitrary-nonglobal.example/careers",
        )
    except collector.UnsafeTargetError:
        pass
    else:
        raise AssertionError("redirect DNS validation must not be bypassed by proxies")


def test_extract_recruitment_links_keeps_only_first_party_or_ats_career_links() -> None:
    collector = _load_collector_module()

    links = collector.extract_recruitment_links(
        """
        <a href="/careers">Careers</a>
        <a href="/blog">Blog</a>
        <a href="https://boards.greenhouse.io/acme">Open roles</a>
        <a href="https://example.net/jobs">External jobs</a>
        """,
        "https://example.com/",
        "example.com",
    )

    assert links == (
        "https://example.com/careers",
        "https://boards.greenhouse.io/acme",
    )


def test_host_aware_pending_queue_round_robins_eligible_hosts() -> None:
    collector = _load_collector_module()
    requests = [
        _request_for_host(collector, "one.example", "/careers"),
        _request_for_host(collector, "one.example", "/jobs"),
        _request_for_host(collector, "two.example", "/careers"),
        _request_for_host(collector, "three.example", "/careers"),
    ]
    queue = collector.HostAwarePendingQueue(
        requests,
        max_concurrency_per_host=1,
        global_concurrency=3,
    )
    in_flight_by_host = collector.Counter()

    first = queue.popleft_eligible(in_flight_by_host)
    assert first is not None
    in_flight_by_host[collector._request_host(first)] += 1
    second = queue.popleft_eligible(in_flight_by_host)
    assert second is not None
    in_flight_by_host[collector._request_host(second)] += 1
    third = queue.popleft_eligible(in_flight_by_host)
    assert third is not None
    in_flight_by_host[collector._request_host(third)] += 1

    assert [first.canonical_url, second.canonical_url, third.canonical_url] == [
        "https://one.example/careers",
        "https://two.example/careers",
        "https://three.example/careers",
    ]
    assert queue.popleft_eligible(in_flight_by_host) is None

    in_flight_by_host["one.example"] -= 1
    fourth = queue.popleft_eligible(in_flight_by_host)

    assert fourth is not None
    assert fourth.canonical_url == "https://one.example/jobs"
    assert len(queue) == 0


def test_host_aware_pending_queue_preserves_fifo_when_host_cap_is_high() -> None:
    collector = _load_collector_module()
    requests = [
        _request_for_host(collector, "one.example", "/careers"),
        _request_for_host(collector, "one.example", "/jobs"),
        _request_for_host(collector, "two.example", "/careers"),
    ]
    queue = collector.HostAwarePendingQueue(
        requests,
        max_concurrency_per_host=3,
        global_concurrency=3,
    )

    assert [
        queue.popleft_eligible(collector.Counter()).canonical_url,
        queue.popleft_eligible(collector.Counter()).canonical_url,
        queue.popleft_eligible(collector.Counter()).canonical_url,
    ] == [
        "https://one.example/careers",
        "https://one.example/jobs",
        "https://two.example/careers",
    ]


def test_host_aware_pending_queue_skips_deferred_hosts() -> None:
    collector = _load_collector_module()
    requests = [
        _request_for_host(collector, "one.example", "/careers"),
        _request_for_host(collector, "two.example", "/careers"),
        _request_for_host(collector, "one.example", "/jobs"),
    ]
    queue = collector.HostAwarePendingQueue(
        requests,
        max_concurrency_per_host=1,
        global_concurrency=2,
    )
    deferred_until_by_host = {"one.example": 20.0}

    first = queue.popleft_ready(
        collector.Counter(),
        deferred_until_by_host,
        now=10.0,
    )

    assert first is not None
    assert first.canonical_url == "https://two.example/careers"
    assert queue.next_ready_at(deferred_until_by_host, now=10.0) == 20.0


def test_collect_recruitment_pages_limits_concurrent_fetches_per_host(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    lock = threading.Lock()
    active_by_host: dict[str, int] = {}
    max_active_by_host: dict[str, int] = {}
    calls: list[str] = []

    def fake_fetch(request: Any, _config: Any) -> Any:
        host = collector._request_host(request)
        with lock:
            calls.append(request.canonical_url)
            active_by_host[host] = active_by_host.get(host, 0) + 1
            max_active_by_host[host] = max(
                max_active_by_host.get(host, 0),
                active_by_host[host],
            )
        try:
            time.sleep(0.02)
            receipt = {
                "canonical_url": request.canonical_url,
                "requested_url": request.url,
                "source_record_id": request.source_record_id,
                "source_index": request.source_index,
                "source_assessment": request.source_assessment,
                "source_relationship": request.source_relationship,
                "registrable_domain": request.registrable_domain,
                "depth": request.depth,
                "discovered_from": request.discovered_from,
                "capture_status": "captured",
                "captured_at": "2026-07-18T00:00:00Z",
                "raw_data_file": collector.RAW_FILENAME,
                "body_bytes": 4,
                "body_sha256": "0" * 64,
            }
            raw_row = {**receipt, "response_text": "body", "extracted_links": []}
            return collector.FetchOutcome(raw_row=raw_row, receipt=receipt, extracted_links=())
        finally:
            with lock:
                active_by_host[host] -= 1

    monkeypatch.setattr(collector, "fetch_page", fake_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            registrable_domain="one.example",
            links=[
                {
                    "text": "Careers",
                    "href": "https://one.example/careers",
                    "relationship": "first_party",
                },
                {
                    "text": "Jobs",
                    "href": "https://one.example/jobs",
                    "relationship": "first_party",
                },
                {
                    "text": "Open roles",
                    "href": "https://one.example/open-roles",
                    "relationship": "first_party",
                },
            ],
        ),
        _triage_row(
            record_id="two",
            assessment="first_party_career_entry",
            registrable_domain="two.example",
            links=[
                {
                    "text": "Careers",
                    "href": "https://two.example/careers",
                    "relationship": "first_party",
                }
            ],
        ),
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=32),
        concurrency=3,
        retry_rounds=0,
        depth=0,
        max_stored_bytes=100_000,
        max_concurrency_per_host=1,
    )

    assert summary["captured"] == 4
    assert max_active_by_host == {"one.example": 1, "two.example": 1}
    assert calls[:2] == ["https://one.example/careers", "https://two.example/careers"]


def test_collect_recruitment_pages_cools_host_and_keeps_other_hosts_filling_slots(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    calls: list[str] = []
    sleeps: list[float] = []
    now = 100.0

    def fake_monotonic() -> float:
        return now

    def fake_sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    def fake_fetch(request: Any, _config: Any) -> Any:
        calls.append(request.canonical_url)
        if collector._request_host(request) == "two.example":
            receipt = {
                "canonical_url": request.canonical_url,
                "requested_url": request.url,
                "source_record_id": request.source_record_id,
                "source_index": request.source_index,
                "source_assessment": request.source_assessment,
                "source_relationship": request.source_relationship,
                "registrable_domain": request.registrable_domain,
                "depth": request.depth,
                "discovered_from": request.discovered_from,
                "capture_status": "captured",
                "captured_at": "2026-07-18T00:00:00Z",
                "raw_data_file": collector.RAW_FILENAME,
                "body_bytes": 4,
                "body_sha256": "0" * 64,
            }
            raw_row = {**receipt, "response_text": "body", "extracted_links": []}
            return collector.FetchOutcome(raw_row=raw_row, receipt=receipt, extracted_links=())
        receipt = {
            "canonical_url": request.canonical_url,
            "requested_url": request.url,
            "source_record_id": request.source_record_id,
            "source_index": request.source_index,
            "source_assessment": request.source_assessment,
            "source_relationship": request.source_relationship,
            "registrable_domain": request.registrable_domain,
            "depth": request.depth,
            "discovered_from": request.discovered_from,
            "capture_status": "network_error",
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": None,
        }
        return collector.FetchOutcome(raw_row=None, receipt=receipt, extracted_links=())

    monkeypatch.setattr(collector, "fetch_page", fake_fetch)
    monkeypatch.setattr(collector.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(collector.time, "sleep", fake_sleep)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            registrable_domain="one.example",
            links=[
                {
                    "text": "Careers",
                    "href": "https://one.example/careers",
                    "relationship": "first_party",
                },
                {
                    "text": "Jobs",
                    "href": "https://one.example/jobs",
                    "relationship": "first_party",
                },
            ],
        ),
        _triage_row(
            record_id="two",
            assessment="first_party_career_entry",
            registrable_domain="two.example",
            links=[
                {
                    "text": "Careers",
                    "href": "https://two.example/careers",
                    "relationship": "first_party",
                }
            ],
        ),
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=32),
        concurrency=1,
        retry_rounds=0,
        depth=0,
        max_stored_bytes=100_000,
        max_concurrency_per_host=1,
        host_failure_cooldown_threshold=1,
        host_failure_cooldown_seconds=5.0,
    )

    assert calls[:2] == ["https://one.example/careers", "https://two.example/careers"]
    assert calls[2] == "https://one.example/jobs"
    assert sleeps == [5.0]
    assert summary["network_error"] == 2
    assert summary["captured"] == 1
    assert summary["host_cooldown_enqueued"] == 2
    assert summary["host_cooldown_waits"] == 1


def test_collect_recruitment_pages_uses_actual_raw_jsonl_bytes_for_cap_and_resume_queue(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    calls: list[str] = []

    def fake_fetch(request: Any, config: Any) -> Any:
        calls.append(request.canonical_url)
        body = b"x"
        receipt = {
            "canonical_url": request.canonical_url,
            "requested_url": request.url,
            "source_record_id": request.source_record_id,
            "source_index": request.source_index,
            "source_assessment": request.source_assessment,
            "source_relationship": request.source_relationship,
            "registrable_domain": request.registrable_domain,
            "depth": request.depth,
            "discovered_from": request.discovered_from,
            "capture_status": "captured",
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": collector.RAW_FILENAME,
            "body_bytes": len(body),
            "body_sha256": "0" * 64,
        }
        raw_row = {
            **receipt,
            "response_text": body.decode(),
            "extracted_links": [],
        }
        return collector.FetchOutcome(raw_row=raw_row, receipt=receipt, extracted_links=())

    monkeypatch.setattr(collector, "fetch_page", fake_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            links=[
                {
                    "text": "Careers",
                    "href": "https://example.com/careers",
                    "relationship": "first_party",
                },
                {
                    "text": "Jobs",
                    "href": "https://jobs.example.com/jobs",
                    "relationship": "first_party",
                },
            ],
        )
    ]

    first_seed = collector.select_seed_urls(rows)[0]
    first_outcome = fake_fetch(first_seed, collector.CollectorConfig())
    expected_first_line_bytes = len(f"{collector._json_line(first_outcome.raw_row)}\n".encode())
    calls.clear()

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=8),
        concurrency=2,
        retry_rounds=0,
        depth=0,
        max_stored_bytes=expected_first_line_bytes,
    )
    raw_rows = [
        json.loads(line)
        for line in (tmp_path / collector.RAW_FILENAME).read_text(encoding="utf-8").splitlines()
    ]

    assert summary["attempted"] == 2
    assert summary["captured"] == 1
    assert summary["storage_cap_reached"] == 1
    assert summary["stored_bytes"] == expected_first_line_bytes
    assert summary["storage_remaining_bytes"] == 0
    assert summary["pending_unfetched"] == 0
    assert (tmp_path / collector.RAW_FILENAME).stat().st_size == expected_first_line_bytes
    assert calls == ["https://example.com/careers", "https://jobs.example.com/jobs"]
    assert [row["body_bytes"] for row in raw_rows] == [1]

    second = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=8),
        concurrency=2,
        retry_rounds=0,
        depth=0,
        max_stored_bytes=expected_first_line_bytes,
    )

    assert second["attempted"] == 0
    assert second["skipped_existing_receipt"] == 2
    assert second["pending_unfetched"] == 0


def test_total_regular_file_bytes_dedupes_overlapping_roots(tmp_path: Path) -> None:
    collector = _load_collector_module()
    nested = tmp_path / "nested"
    nested.mkdir()
    root_file = tmp_path / "root.bin"
    nested_file = nested / "nested.bin"
    root_file.write_bytes(b"abc")
    nested_file.write_bytes(b"defgh")

    assert collector.total_regular_file_bytes([tmp_path, nested]) == 8


def test_collect_recruitment_pages_stops_before_fetch_when_aggregate_cap_is_reached(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    (tmp_path / "existing.bin").write_bytes(b"12345")

    def fail_fetch(*_args: Any) -> None:
        raise AssertionError("aggregate cap must prevent scheduling fetches")

    monkeypatch.setattr(collector, "fetch_page", fail_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            links=[
                {
                    "text": "Careers",
                    "href": "https://example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=8),
        concurrency=1,
        retry_rounds=0,
        depth=0,
        max_stored_bytes=100_000,
        aggregate_data_dirs=[tmp_path],
        max_total_data_bytes=5,
    )

    assert summary["attempted"] == 0
    assert summary["pending_unfetched"] == 1
    assert summary["aggregate_data_bytes"] >= 5
    assert summary["aggregate_storage_remaining_bytes"] == 0


def test_collect_recruitment_pages_stops_before_raw_append_when_aggregate_cap_would_cross(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    calls: list[str] = []

    def fake_fetch(request: Any, _config: Any) -> Any:
        calls.append(request.canonical_url)
        receipt = {
            "canonical_url": request.canonical_url,
            "requested_url": request.url,
            "source_record_id": request.source_record_id,
            "source_index": request.source_index,
            "source_assessment": request.source_assessment,
            "source_relationship": request.source_relationship,
            "registrable_domain": request.registrable_domain,
            "depth": request.depth,
            "discovered_from": request.discovered_from,
            "capture_status": "captured",
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": collector.RAW_FILENAME,
            "body_bytes": 10,
            "body_sha256": "0" * 64,
        }
        raw_row = {**receipt, "response_text": "0123456789", "extracted_links": []}
        return collector.FetchOutcome(raw_row=raw_row, receipt=receipt, extracted_links=())

    monkeypatch.setattr(collector, "fetch_page", fake_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            links=[
                {
                    "text": "Careers",
                    "href": "https://example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=8),
        concurrency=1,
        retry_rounds=0,
        depth=0,
        max_stored_bytes=100_000,
        aggregate_data_dirs=[tmp_path],
        max_total_data_bytes=1,
    )

    assert calls == ["https://example.com/careers"]
    assert summary["attempted"] == 1
    assert summary["aggregate_storage_cap_reached"] == 1
    assert summary["stored_bytes"] == 0
    assert (tmp_path / collector.RAW_FILENAME).read_text(encoding="utf-8") == ""


def test_collect_recruitment_pages_ingests_additional_sitemap_seed_rows(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    calls: list[str] = []

    def fake_fetch(request: Any, _config: Any) -> Any:
        calls.append(request.canonical_url)
        receipt = {
            "canonical_url": request.canonical_url,
            "requested_url": request.url,
            "source_record_id": request.source_record_id,
            "source_index": request.source_index,
            "source_assessment": request.source_assessment,
            "source_relationship": request.source_relationship,
            "registrable_domain": request.registrable_domain,
            "depth": request.depth,
            "discovered_from": request.discovered_from,
            "seed_source": request.seed_source,
            "source_seed_url": request.source_seed_url,
            "capture_status": "captured",
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": collector.RAW_FILENAME,
            "body_bytes": 4,
            "body_sha256": "0" * 64,
        }
        raw_row = {**receipt, "response_text": "body", "extracted_links": []}
        return collector.FetchOutcome(raw_row=raw_row, receipt=receipt, extracted_links=())

    monkeypatch.setattr(collector, "fetch_page", fake_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            links=[
                {
                    "text": "Careers",
                    "href": "https://example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]
    additional_rows = [
        {
            "canonical_url": "https://example.com/careers",
            "source_sitemap_url": "https://example.com/sitemap.xml",
            "source_record_id": "duplicate",
            "source_assessment": "first_party_career_entry",
            "source_relationship": "first_party",
            "registrable_domain": "example.com",
        },
        {
            "canonical_url": "https://example.com/jobs/engineering",
            "source_sitemap_url": "https://example.com/sitemap.xml",
            "source_record_id": "site-one",
            "source_assessment": "first_party_career_entry",
            "source_relationship": "first_party",
            "registrable_domain": "example.com",
        },
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=8),
        concurrency=1,
        retry_rounds=0,
        depth=0,
        additional_seed_rows=additional_rows,
        max_stored_bytes=100_000,
    )
    raw_rows = [
        json.loads(line)
        for line in (tmp_path / collector.RAW_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    receipt_rows = [
        json.loads(line)
        for line in (tmp_path / collector.RECEIPT_FILENAME).read_text(encoding="utf-8").splitlines()
    ]

    assert calls == ["https://example.com/careers", "https://example.com/jobs/engineering"]
    assert summary["triage_seed_count"] == 1
    assert summary["additional_seed_count"] == 1
    assert summary["seed_count"] == 2
    assert summary["captured"] == 2
    assert [row["seed_source"] for row in raw_rows] == [
        "triage_link",
        "additional_seed_jsonl",
    ]
    assert raw_rows[1]["discovered_from"] == "https://example.com/sitemap.xml"
    assert receipt_rows[1]["source_seed_url"] == "https://example.com/jobs/engineering"


def test_collect_recruitment_pages_can_skip_valid_triage_seeds_for_additional_only_crawl(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    calls: list[str] = []

    def fake_fetch(request: Any, _config: Any) -> Any:
        calls.append(request.canonical_url)
        receipt = {
            "canonical_url": request.canonical_url,
            "requested_url": request.url,
            "source_record_id": request.source_record_id,
            "source_index": request.source_index,
            "source_assessment": request.source_assessment,
            "source_relationship": request.source_relationship,
            "registrable_domain": request.registrable_domain,
            "depth": request.depth,
            "discovered_from": request.discovered_from,
            "seed_source": request.seed_source,
            "source_seed_url": request.source_seed_url,
            "capture_status": "captured",
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": collector.RAW_FILENAME,
            "body_bytes": 4,
            "body_sha256": "0" * 64,
        }
        raw_row = {**receipt, "response_text": "body", "extracted_links": []}
        return collector.FetchOutcome(raw_row=raw_row, receipt=receipt, extracted_links=())

    monkeypatch.setattr(collector, "fetch_page", fake_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            links=[
                {
                    "text": "Careers",
                    "href": "https://example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]
    additional_rows = [
        {
            "canonical_url": "https://example.com/careers",
            "source_sitemap_url": "https://example.com/sitemap.xml",
            "source_record_id": "additional-duplicate",
            "source_assessment": "first_party_career_entry",
            "source_relationship": "first_party",
            "registrable_domain": "example.com",
        },
        {
            "canonical_url": "https://example.com/jobs/engineering",
            "source_sitemap_url": "https://example.com/sitemap.xml",
            "source_record_id": "additional-new",
            "source_assessment": "first_party_career_entry",
            "source_relationship": "first_party",
            "registrable_domain": "example.com",
        },
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=8),
        concurrency=1,
        retry_rounds=0,
        depth=0,
        additional_seed_rows=additional_rows,
        max_stored_bytes=100_000,
        skip_triage_seeds=True,
    )
    manifest = json.loads((tmp_path / collector.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    raw_rows = [
        json.loads(line)
        for line in (tmp_path / collector.RAW_FILENAME).read_text(encoding="utf-8").splitlines()
    ]

    assert calls == ["https://example.com/careers", "https://example.com/jobs/engineering"]
    assert summary["triage_seed_candidate_count"] == 1
    assert summary["triage_seed_count"] == 0
    assert summary["triage_seed_skipped"] == 1
    assert summary["additional_seed_count"] == 2
    assert summary["seed_count"] == 2
    assert summary["captured"] == 2
    assert manifest["skip_triage_seeds"] is True
    assert manifest["summary"]["triage_seed_skipped"] == 1
    assert [row["seed_source"] for row in raw_rows] == [
        "additional_seed_jsonl",
        "additional_seed_jsonl",
    ]


def test_parse_args_accepts_skip_triage_seeds_flag() -> None:
    collector = _load_collector_module()

    args = collector._parse_args(
        [
            "--triage-input",
            "triage.jsonl",
            "--additional-seeds",
            "additional.jsonl",
            "--output-dir",
            "out",
            "--skip-triage-seeds",
        ]
    )

    assert args.skip_triage_seeds is True


def test_collect_recruitment_pages_reconstructs_discovered_links_on_resume(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    parent_url = "https://example.com/careers"
    child_url = "https://example.com/jobs/engineering"
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / collector.RECEIPT_FILENAME).write_text(
        json.dumps(
            {
                "canonical_url": parent_url,
                "requested_url": parent_url,
                "source_record_id": "one",
                "source_index": 0,
                "source_assessment": "first_party_career_entry",
                "source_relationship": "first_party",
                "registrable_domain": "example.com",
                "depth": 0,
                "capture_status": "captured",
                "captured_at": "2026-07-18T00:00:00Z",
                "raw_data_file": collector.RAW_FILENAME,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / collector.RAW_FILENAME).write_text(
        json.dumps(
            {
                "canonical_url": parent_url,
                "requested_url": parent_url,
                "source_record_id": "one",
                "source_index": 0,
                "source_assessment": "first_party_career_entry",
                "source_relationship": "first_party",
                "registrable_domain": "example.com",
                "depth": 0,
                "discovered_from": None,
                "seed_source": "triage_link",
                "source_seed_url": parent_url,
                "captured_at": "2026-07-18T00:00:00Z",
                "response_text": "<a href='/jobs/engineering'>Engineering jobs</a>",
                "extracted_links": [child_url],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[Any] = []

    def fake_fetch(request: Any, _config: Any) -> Any:
        calls.append(request)
        receipt = {
            "canonical_url": request.canonical_url,
            "requested_url": request.url,
            "source_record_id": request.source_record_id,
            "source_index": request.source_index,
            "source_assessment": request.source_assessment,
            "source_relationship": request.source_relationship,
            "registrable_domain": request.registrable_domain,
            "depth": request.depth,
            "discovered_from": request.discovered_from,
            "seed_source": request.seed_source,
            "source_seed_url": request.source_seed_url,
            "capture_status": "captured",
            "captured_at": "2026-07-18T00:00:01Z",
            "raw_data_file": collector.RAW_FILENAME,
            "body_bytes": 5,
            "body_sha256": "0" * 64,
        }
        raw_row = {**receipt, "response_text": "child", "extracted_links": []}
        return collector.FetchOutcome(raw_row=raw_row, receipt=receipt, extracted_links=())

    monkeypatch.setattr(collector, "fetch_page", fake_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            links=[
                {
                    "text": "Careers",
                    "href": parent_url,
                    "relationship": "first_party",
                }
            ],
        )
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=32),
        concurrency=1,
        retry_rounds=0,
        depth=1,
        max_stored_bytes=100_000,
    )

    assert [request.canonical_url for request in calls] == [child_url]
    assert calls[0].depth == 1
    assert calls[0].discovered_from == parent_url
    assert calls[0].source_seed_url == parent_url
    assert summary["skipped_existing_receipt"] == 1
    assert summary["resume_reconstructed_discovered_count"] == 1
    assert summary["attempted"] == 1
    assert summary["captured"] == 1


def test_reconstruct_resume_discovered_requests_filters_depth_scope_and_completed(
    tmp_path: Path,
) -> None:
    collector = _load_collector_module()
    raw_path = tmp_path / collector.RAW_FILENAME
    raw_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "canonical_url": "https://example.com/careers",
                        "requested_url": "https://example.com/careers",
                        "source_record_id": "one",
                        "source_index": 0,
                        "source_assessment": "first_party_career_entry",
                        "source_relationship": "first_party",
                        "registrable_domain": "example.com",
                        "depth": 0,
                        "seed_source": "triage_link",
                        "source_seed_url": "https://example.com/careers",
                        "extracted_links": [
                            "HTTPS://EXAMPLE.COM/jobs/engineering#fragment",
                            "https://example.com/jobs/engineering",
                            "https://example.net/jobs",
                            "https://example.com/blog/careers-case-study",
                            "https://boards.greenhouse.io/acme",
                        ],
                    },
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "canonical_url": "https://example.com/jobs",
                        "requested_url": "https://example.com/jobs",
                        "source_record_id": "two",
                        "source_index": 1,
                        "source_assessment": "first_party_career_entry",
                        "source_relationship": "first_party",
                        "registrable_domain": "example.com",
                        "depth": 1,
                        "seed_source": "triage_link",
                        "source_seed_url": "https://example.com/jobs",
                        "extracted_links": ["https://example.com/jobs/deeper"],
                    },
                    sort_keys=True,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    completed = {"https://boards.greenhouse.io/acme"}
    seen = {"https://example.com/careers"}

    requests = collector.reconstruct_resume_discovered_requests(
        raw_path,
        completed=completed,
        seen=seen,
        depth=1,
    )

    assert [request.canonical_url for request in requests] == [
        "https://example.com/jobs/engineering",
        "https://example.com/blog/careers-case-study",
    ]
    assert [request.depth for request in requests] == [1, 1]
    assert all(request.discovered_from == "https://example.com/careers" for request in requests)
    assert "https://example.com/jobs/deeper" not in seen
    request_urls = [request.canonical_url for request in requests]
    assert "https://boards.greenhouse.io/acme" not in request_urls


def test_collect_recruitment_pages_does_not_fetch_reconstructed_links_at_storage_cap(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    parent_url = "https://example.com/careers"
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / collector.RECEIPT_FILENAME).write_text(
        json.dumps(
            {
                "canonical_url": parent_url,
                "requested_url": parent_url,
                "capture_status": "captured",
                "captured_at": "2026-07-18T00:00:00Z",
                "raw_data_file": collector.RAW_FILENAME,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    raw_path = tmp_path / collector.RAW_FILENAME
    raw_path.write_text(
        json.dumps(
            {
                "canonical_url": parent_url,
                "requested_url": parent_url,
                "source_assessment": "first_party_career_entry",
                "source_relationship": "first_party",
                "registrable_domain": "example.com",
                "depth": 0,
                "extracted_links": ["https://example.com/jobs/engineering"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    def fail_fetch(*_args: Any) -> None:
        raise AssertionError("storage cap must prevent reconstructed fetches")

    monkeypatch.setattr(collector, "fetch_page", fail_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            links=[
                {
                    "text": "Careers",
                    "href": parent_url,
                    "relationship": "first_party",
                }
            ],
        )
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=32),
        concurrency=1,
        retry_rounds=0,
        depth=1,
        max_stored_bytes=raw_path.stat().st_size,
    )

    assert summary["resume_reconstructed_discovered_count"] == 1
    assert summary["attempted"] == 0
    assert summary["pending_unfetched"] == 1
    assert raw_path.stat().st_size == summary["stored_bytes"]


def test_collect_recruitment_pages_retries_http_429_then_captures(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    canonical_url = "https://example.com/careers"
    calls: list[str] = []

    def fake_fetch(request: Any, _config: Any) -> Any:
        calls.append(request.canonical_url)
        if len(calls) == 1:
            receipt = {
                "canonical_url": request.canonical_url,
                "requested_url": request.url,
                "source_record_id": request.source_record_id,
                "source_index": request.source_index,
                "source_assessment": request.source_assessment,
                "source_relationship": request.source_relationship,
                "registrable_domain": request.registrable_domain,
                "depth": request.depth,
                "discovered_from": request.discovered_from,
                "capture_status": "http_error",
                "http_status": 429,
                "captured_at": "2026-07-18T00:00:00Z",
                "raw_data_file": None,
            }
            return collector.FetchOutcome(raw_row=None, receipt=receipt, extracted_links=())
        receipt = {
            "canonical_url": request.canonical_url,
            "requested_url": request.url,
            "source_record_id": request.source_record_id,
            "source_index": request.source_index,
            "source_assessment": request.source_assessment,
            "source_relationship": request.source_relationship,
            "registrable_domain": request.registrable_domain,
            "depth": request.depth,
            "discovered_from": request.discovered_from,
            "capture_status": "captured",
            "captured_at": "2026-07-18T00:00:01Z",
            "raw_data_file": collector.RAW_FILENAME,
            "body_bytes": 4,
            "body_sha256": "0" * 64,
        }
        raw_row = {**receipt, "response_text": "body", "extracted_links": []}
        return collector.FetchOutcome(raw_row=raw_row, receipt=receipt, extracted_links=())

    monkeypatch.setattr(collector, "fetch_page", fake_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            links=[
                {
                    "text": "Careers",
                    "href": canonical_url,
                    "relationship": "first_party",
                }
            ],
        )
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=32),
        concurrency=1,
        retry_rounds=4,
        depth=0,
        max_stored_bytes=100_000,
    )

    assert calls == [canonical_url, canonical_url]
    assert summary["http_error"] == 1
    assert summary["retry_enqueued"] == 1
    assert summary["captured"] == 1


def test_collect_recruitment_pages_does_not_retry_terminal_http_404(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    canonical_url = "https://example.com/careers"
    calls: list[str] = []

    def fake_fetch(request: Any, _config: Any) -> Any:
        calls.append(request.canonical_url)
        receipt = {
            "canonical_url": request.canonical_url,
            "requested_url": request.url,
            "source_record_id": request.source_record_id,
            "source_index": request.source_index,
            "source_assessment": request.source_assessment,
            "source_relationship": request.source_relationship,
            "registrable_domain": request.registrable_domain,
            "depth": request.depth,
            "discovered_from": request.discovered_from,
            "capture_status": "http_error",
            "http_status": 404,
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": None,
        }
        return collector.FetchOutcome(raw_row=None, receipt=receipt, extracted_links=())

    monkeypatch.setattr(collector, "fetch_page", fake_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            links=[
                {
                    "text": "Careers",
                    "href": canonical_url,
                    "relationship": "first_party",
                }
            ],
        )
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=32),
        concurrency=1,
        retry_rounds=4,
        depth=0,
        max_stored_bytes=100_000,
    )

    assert calls == [canonical_url]
    assert summary["http_error"] == 1
    assert summary["retry_enqueued"] == 0
    assert summary["http_error_retry_exhausted"] == 0


def test_collect_recruitment_pages_resumes_retryable_http_errors_until_five_total_failures(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    receipt_path = tmp_path / collector.RECEIPT_FILENAME
    tmp_path.mkdir(exist_ok=True)
    canonical_url = "https://example.com/careers"
    with receipt_path.open("a", encoding="utf-8") as handle:
        for _ in range(4):
            handle.write(
                json.dumps(
                    {
                        "canonical_url": canonical_url,
                        "requested_url": canonical_url,
                        "capture_status": "http_error",
                        "http_status": 500,
                        "captured_at": "2026-07-18T00:00:00Z",
                        "raw_data_file": None,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    calls: list[str] = []

    def fake_fetch(request: Any, _config: Any) -> Any:
        calls.append(request.canonical_url)
        receipt = {
            "canonical_url": request.canonical_url,
            "requested_url": request.url,
            "source_record_id": request.source_record_id,
            "source_index": request.source_index,
            "source_assessment": request.source_assessment,
            "source_relationship": request.source_relationship,
            "registrable_domain": request.registrable_domain,
            "depth": request.depth,
            "discovered_from": request.discovered_from,
            "capture_status": "http_error",
            "http_status": 500,
            "captured_at": "2026-07-18T00:00:01Z",
            "raw_data_file": None,
        }
        return collector.FetchOutcome(raw_row=None, receipt=receipt, extracted_links=())

    monkeypatch.setattr(collector, "fetch_page", fake_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            links=[
                {
                    "text": "Careers",
                    "href": canonical_url,
                    "relationship": "first_party",
                }
            ],
        )
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=32),
        concurrency=1,
        retry_rounds=4,
        depth=0,
        max_stored_bytes=100_000,
    )
    streaks = json.loads(
        (tmp_path / collector.FAILURE_STREAKS_FILENAME).read_text(encoding="utf-8")
    )

    assert calls == [canonical_url]
    assert summary["http_error"] == 1
    assert summary["retry_enqueued"] == 0
    assert summary["http_error_retry_exhausted"] == 1
    assert streaks["by_url"][canonical_url] == 5


def test_collect_recruitment_pages_resumes_network_errors_until_five_total_failures(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    receipt_path = tmp_path / collector.RECEIPT_FILENAME
    tmp_path.mkdir(exist_ok=True)
    canonical_url = "https://example.com/careers"
    for _ in range(4):
        receipt_path.write_text(
            receipt_path.read_text(encoding="utf-8") if receipt_path.exists() else "",
            encoding="utf-8",
        )
        with receipt_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "canonical_url": canonical_url,
                        "requested_url": canonical_url,
                        "capture_status": "network_error",
                        "captured_at": "2026-07-18T00:00:00Z",
                        "raw_data_file": None,
                    }
                )
                + "\n"
            )

    calls: list[str] = []

    def fake_fetch(request: Any, _config: Any) -> Any:
        calls.append(request.canonical_url)
        receipt = {
            "canonical_url": request.canonical_url,
            "requested_url": request.url,
            "source_record_id": request.source_record_id,
            "source_index": request.source_index,
            "source_assessment": request.source_assessment,
            "source_relationship": request.source_relationship,
            "registrable_domain": request.registrable_domain,
            "depth": request.depth,
            "discovered_from": request.discovered_from,
            "capture_status": "network_error",
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": None,
        }
        return collector.FetchOutcome(raw_row=None, receipt=receipt, extracted_links=())

    monkeypatch.setattr(collector, "fetch_page", fake_fetch)
    rows = [
        _triage_row(
            record_id="one",
            assessment="first_party_career_entry",
            links=[
                {
                    "text": "Careers",
                    "href": canonical_url,
                    "relationship": "first_party",
                }
            ],
        )
    ]

    summary = collector.collect_recruitment_pages(
        rows,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=1, max_response_bytes=8),
        concurrency=1,
        retry_rounds=4,
        depth=0,
        max_stored_bytes=100_000,
    )
    streaks = json.loads(
        (tmp_path / collector.FAILURE_STREAKS_FILENAME).read_text(encoding="utf-8")
    )

    assert calls == [canonical_url]
    assert summary["attempted"] == 1
    assert summary["network_error"] == 1
    assert summary["network_error_retry_exhausted"] == 1
    assert summary["retry_enqueued"] == 0
    assert streaks["by_url"][canonical_url] == 5
