from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from http.client import HTTPMessage
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import HTTPError


def _load_discovery_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "discover_recruitment_commoncrawl.py"
    scripts_dir = str(script.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("discover_recruitment_commoncrawl", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _triage_row(
    *,
    record_id: str,
    assessment: str = "first_party_career_entry",
    registrable_domain: str = "example.com",
    final_url: str = "https://www.example.com/",
    links: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "registrable_domain": registrable_domain,
        "employer_hiring_assessment": assessment,
        "final_url": final_url,
        "links": links or [],
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_select_company_domains_keeps_only_eligible_first_party_domains() -> None:
    discovery = _load_discovery_module()
    rows = [
        _triage_row(
            record_id="one",
            links=[
                {
                    "text": "Careers",
                    "href": "https://www.example.com/careers",
                    "relationship": "first_party",
                }
            ],
        ),
        _triage_row(
            record_id="duplicate",
            links=[
                {
                    "text": "Jobs",
                    "href": "https://jobs.example.com/jobs",
                    "relationship": "first_party",
                }
            ],
        ),
        _triage_row(
            record_id="ats_only",
            assessment="external_ats_needs_verification",
            registrable_domain="acme.ai",
            final_url="https://boards.greenhouse.io/acme",
            links=[
                {
                    "text": "Open jobs",
                    "href": "https://boards.greenhouse.io/acme/jobs",
                    "relationship": "known_ats",
                }
            ],
        ),
        _triage_row(
            record_id="ignored",
            assessment="no_recruitment_evidence",
            registrable_domain="ignored.example",
            final_url="https://ignored.example/",
        ),
    ]

    domains = discovery.select_company_domains(rows)

    assert [(domain.registrable_domain, domain.source_record_id) for domain in domains] == [
        ("example.com", "one")
    ]


def test_build_query_targets_uses_commoncrawl_cdx_parameters() -> None:
    discovery = _load_discovery_module()
    indexes = [
        discovery.IndexInfo(
            index_id="CC-MAIN-2026-25",
            cdx_api_url="https://index.commoncrawl.org/CC-MAIN-2026-25-index",
        )
    ]
    companies = [
        discovery.CompanyDomain(
            registrable_domain="example.com",
            source_record_id="one",
            source_index=0,
            source_assessment="first_party_career_entry",
        )
    ]

    targets = discovery.build_query_targets(indexes, companies, max_results_per_query=7)

    assert len(targets) == len(discovery.QUERY_PATTERNS)
    assert targets[0].url.startswith("https://index.commoncrawl.org/CC-MAIN-2026-25-index?")
    assert "url=%2A.example.com%2Fcareers%2A" in targets[0].url
    assert "output=json" in targets[0].url
    assert "filter=status%3A200" in targets[0].url
    assert "limit=7" in targets[0].url


def test_load_commoncrawl_indexes_accepts_only_documented_https_index_host(
    monkeypatch: Any,
) -> None:
    discovery = _load_discovery_module()
    catalog = [
        {
            "id": "CC-MAIN-2026-25",
            "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-25-index",
        },
        {
            "id": "bad-http",
            "cdx-api": "http://index.commoncrawl.org/CC-MAIN-2026-21-index",
        },
        {"id": "bad-host", "cdx-api": "https://evil.example/CC-MAIN-2026-18-index"},
    ]

    monkeypatch.setattr(
        discovery,
        "_response_text",
        lambda *_args, **_kwargs: (
            json.dumps(catalog),
            "https://index.commoncrawl.org/collinfo.json",
            200,
            "application/json",
            1,
        ),
    )

    indexes = discovery.load_commoncrawl_indexes(discovery.FetchConfig(), max_indexes=3)

    assert indexes == (
        discovery.IndexInfo(
            index_id="CC-MAIN-2026-25",
            cdx_api_url="https://index.commoncrawl.org/CC-MAIN-2026-25-index",
        ),
    )


def test_retained_cdx_url_keeps_first_party_recruitment_urls_only() -> None:
    discovery = _load_discovery_module()
    company = discovery.CompanyDomain(
        registrable_domain="example.com",
        source_record_id="one",
        source_index=0,
        source_assessment="first_party_career_entry",
    )

    assert (
        discovery.retained_cdx_url(
            {"url": "https://www.example.com/careers/software-engineer"}, company
        )
        == "https://www.example.com/careers/software-engineer"
    )
    assert discovery.retained_cdx_url({"url": "https://www.example.com/blog/post"}, company) == ""
    assert (
        discovery.retained_cdx_url({"url": "https://boards.greenhouse.io/example/jobs"}, company)
        == ""
    )


def test_discovery_writes_deterministic_additional_seed_rows(tmp_path: Path) -> None:
    discovery = _load_discovery_module()
    rows = [
        _triage_row(
            record_id="one",
            links=[
                {
                    "text": "Careers",
                    "href": "https://www.example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]
    indexes = [
        discovery.IndexInfo(
            index_id="CC-MAIN-2026-25",
            cdx_api_url="https://index.commoncrawl.org/CC-MAIN-2026-25-index",
        )
    ]

    def fake_fetch(target: Any, _config: Any) -> Any:
        rows_by_pattern = {
            "/careers*": (
                {
                    "url": "https://www.example.com/careers/z-role",
                    "status": "200",
                    "mime": "text/html",
                    "timestamp": "20260601000000",
                    "digest": "Z",
                },
                {
                    "url": "https://www.example.com/blog/post",
                    "status": "200",
                    "mime": "text/html",
                },
            ),
            "/jobs*": (
                {
                    "url": "https://www.example.com/jobs/a-role",
                    "status": "200",
                    "mime": "text/html",
                    "timestamp": "20260602000000",
                    "digest": "A",
                },
                {"url": "https://www.example.com/careers/z-role", "status": "200"},
            ),
        }
        return discovery.FetchResult(
            target=target,
            status="fetched",
            rows=rows_by_pattern.get(target.pattern, ()),
        )

    summary = discovery.discover_recruitment_commoncrawl_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(),
        indexes=indexes,
        concurrency=1,
        fetcher=fake_fetch,
    )

    discovered = _read_jsonl(tmp_path / discovery.DISCOVERED_FILENAME)
    receipts = _read_jsonl(tmp_path / discovery.RECEIPT_FILENAME)
    assert summary["discovered_url_written"] == 2
    assert [row["canonical_url"] for row in discovered] == [
        "https://www.example.com/careers/z-role",
        "https://www.example.com/jobs/a-role",
    ]
    materialized_receipts = [
        row for row in receipts if row["materialized_url_count"] or row["duplicate_url_count"]
    ]
    materialized_counts = [
        (row["query_pattern"], row["materialized_url_count"]) for row in materialized_receipts
    ]
    assert materialized_counts == [
        ("/careers*", 1),
        ("/jobs*", 1),
    ]
    assert discovered[0]["url"] == discovered[0]["canonical_url"]
    assert discovered[0]["source_assessment"] == "first_party_career_entry"
    assert discovered[0]["source_relationship"] == "first_party"
    assert discovered[0]["registrable_domain"] == "example.com"
    assert discovered[0]["source_commoncrawl_index_id"] == "CC-MAIN-2026-25"


def test_discovery_fetches_with_bounded_concurrency(tmp_path: Path) -> None:
    discovery = _load_discovery_module()
    rows = [
        _triage_row(
            record_id="one",
            links=[
                {
                    "text": "Careers",
                    "href": "https://www.example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]
    indexes = [
        discovery.IndexInfo(
            index_id="CC-MAIN-2026-25",
            cdx_api_url="https://index.commoncrawl.org/CC-MAIN-2026-25-index",
        )
    ]
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_fetch(target: Any, _config: Any) -> Any:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return discovery.FetchResult(target=target, status="fetched")

    summary = discovery.discover_recruitment_commoncrawl_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(),
        indexes=indexes,
        concurrency=3,
        fetcher=fake_fetch,
    )

    assert max_active == 3
    assert summary["attempted"] == len(discovery.QUERY_PATTERNS)


def test_retryable_http_errors_retry_five_total_attempts(tmp_path: Path) -> None:
    discovery = _load_discovery_module()
    rows = [
        _triage_row(
            record_id="one",
            links=[
                {
                    "text": "Careers",
                    "href": "https://www.example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]
    indexes = [
        discovery.IndexInfo(
            index_id="CC-MAIN-2026-25",
            cdx_api_url="https://index.commoncrawl.org/CC-MAIN-2026-25-index",
        )
    ]

    def fake_fetch(target: Any, _config: Any) -> Any:
        return discovery.FetchResult(
            target=target,
            status="retryable_http_error",
            http_status=429,
            error_kind="HTTPError",
        )

    summary = discovery.discover_recruitment_commoncrawl_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(retry_backoff_seconds=0),
        indexes=indexes,
        provider_circuit_breaker_failures=0,
        fetcher=fake_fetch,
    )

    receipts = _read_jsonl(tmp_path / discovery.RECEIPT_FILENAME)
    assert summary["attempted"] == len(discovery.QUERY_PATTERNS) * 5
    assert summary["retry_exhausted"] == len(discovery.QUERY_PATTERNS)
    assert len(receipts) == len(discovery.QUERY_PATTERNS) * 5


def test_provider_circuit_breaker_stops_after_five_consecutive_retryable_failures(
    tmp_path: Path,
) -> None:
    discovery = _load_discovery_module()
    rows = [
        _triage_row(
            record_id="one",
            links=[
                {
                    "text": "Careers",
                    "href": "https://www.example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]
    indexes = [
        discovery.IndexInfo(
            index_id="CC-MAIN-2026-25",
            cdx_api_url="https://index.commoncrawl.org/CC-MAIN-2026-25-index",
        )
    ]

    def fake_fetch(target: Any, _config: Any) -> Any:
        return discovery.FetchResult(
            target=target,
            status="network_error",
            error_kind="URLError",
        )

    summary = discovery.discover_recruitment_commoncrawl_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(retry_backoff_seconds=0),
        indexes=indexes,
        concurrency=1,
        fetcher=fake_fetch,
    )

    receipts = _read_jsonl(tmp_path / discovery.RECEIPT_FILENAME)
    manifest = json.loads((tmp_path / discovery.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    summary_file = json.loads((tmp_path / discovery.SUMMARY_FILENAME).read_text(encoding="utf-8"))

    assert summary["attempted"] == 5
    assert summary["network_error"] == 5
    assert summary["retry_enqueued"] == 5
    assert summary["provider_circuit_breaker_tripped"] == 1
    assert summary["provider_circuit_breaker_failures"] == 5
    assert summary["consecutive_retryable_provider_failures"] == 5
    assert summary["pending_unfetched"] == len(discovery.QUERY_PATTERNS)
    assert "retry_exhausted" not in summary
    assert len(receipts) == 5
    assert manifest["provider_circuit_breaker_failures"] == 5
    assert summary_file["provider_circuit_breaker_tripped"] == 1


def test_provider_circuit_breaker_resets_after_non_retryable_response(tmp_path: Path) -> None:
    discovery = _load_discovery_module()
    rows = [
        _triage_row(
            record_id="one",
            links=[
                {
                    "text": "Careers",
                    "href": "https://www.example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]
    indexes = [
        discovery.IndexInfo(
            index_id="CC-MAIN-2026-25",
            cdx_api_url="https://index.commoncrawl.org/CC-MAIN-2026-25-index",
        )
    ]
    statuses = ["network_error"] * 4 + ["fetched"] + ["retryable_http_error"] * 5

    def fake_fetch(target: Any, _config: Any) -> Any:
        status = statuses.pop(0)
        return discovery.FetchResult(
            target=target,
            status=status,
            http_status=503 if status == "retryable_http_error" else None,
            error_kind="HTTPError" if status == "retryable_http_error" else None,
        )

    summary = discovery.discover_recruitment_commoncrawl_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(retry_backoff_seconds=0),
        indexes=indexes,
        concurrency=1,
        fetcher=fake_fetch,
    )

    assert summary["attempted"] == 10
    assert summary["provider_circuit_breaker_tripped"] == 1
    assert summary["consecutive_retryable_provider_failures"] == 5


def test_parse_args_exposes_provider_circuit_breaker_flag() -> None:
    discovery = _load_discovery_module()

    args = discovery._parse_args(
        [
            "--triage-input",
            "triage.jsonl",
            "--output-dir",
            "out",
            "--provider-circuit-breaker-failures",
            "9",
        ]
    )

    assert args.provider_circuit_breaker_failures == 9


def test_fetch_maps_429_and_5xx_http_errors_to_retryable_status(monkeypatch: Any) -> None:
    discovery = _load_discovery_module()
    company = discovery.CompanyDomain(
        registrable_domain="example.com",
        source_record_id="one",
        source_index=0,
        source_assessment="first_party_career_entry",
    )
    target = discovery.QueryTarget(
        canonical_url=(
            "https://index.commoncrawl.org/CC-MAIN-2026-25-index?url=%2A.example.com%2Fjobs%2A"
        ),
        url=("https://index.commoncrawl.org/CC-MAIN-2026-25-index?url=%2A.example.com%2Fjobs%2A"),
        index_id="CC-MAIN-2026-25",
        pattern="/jobs*",
        company=company,
    )
    codes = [429, 500, 404]

    def raise_http(url: str, _config: Any, *, accept: str) -> tuple[str, str, int, str, int]:
        del accept
        code = codes.pop(0)
        raise HTTPError(url, code, "status", HTTPMessage(), None)

    monkeypatch.setattr(discovery, "_response_text", raise_http)

    statuses = [discovery.fetch_cdx_query(target, discovery.FetchConfig()).status for _ in range(3)]

    assert statuses == ["retryable_http_error", "retryable_http_error", "http_error"]


def test_fetch_honors_public_url_guard_with_sandbox_alias(monkeypatch: Any) -> None:
    discovery = _load_discovery_module()
    calls: list[bool] = []

    class _Response:
        headers = HTTPMessage()

        def __enter__(self) -> Any:
            self.headers.add_header("Content-Type", "application/json; charset=utf-8")
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"url": "https://www.example.com/jobs"}\n'

        def geturl(self) -> str:
            return discovery.DEFAULT_INDEX_CATALOG_URL

        def getcode(self) -> int:
            return 200

    class _Opener:
        def open(self, _request: Any, *, timeout: float) -> Any:
            del timeout
            return _Response()

    def fake_guard(_url: str, *, allow_sandbox_egress_alias: bool) -> None:
        calls.append(allow_sandbox_egress_alias)

    monkeypatch.setattr(discovery, "_ensure_public_http_url", fake_guard)
    monkeypatch.setattr(discovery, "build_opener", lambda *_args: _Opener())

    discovery._response_text(
        discovery.DEFAULT_INDEX_CATALOG_URL,
        discovery.FetchConfig(allow_sandbox_egress_alias=True),
        accept="application/json",
    )

    assert calls == [True]


def test_resume_requeries_legacy_fetched_receipts_without_materialized_output(
    tmp_path: Path,
) -> None:
    discovery = _load_discovery_module()
    rows = [
        _triage_row(
            record_id="one",
            links=[
                {
                    "text": "Careers",
                    "href": "https://www.example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]
    indexes = [
        discovery.IndexInfo(
            index_id="CC-MAIN-2026-25",
            cdx_api_url="https://index.commoncrawl.org/CC-MAIN-2026-25-index",
        )
    ]
    completed_target = discovery.build_query_targets(
        indexes, discovery.select_company_domains(rows)
    )[0]
    (tmp_path / discovery.RECEIPT_FILENAME).write_text(
        json.dumps({"canonical_url": completed_target.canonical_url, "capture_status": "fetched"})
        + "\n",
        encoding="utf-8",
    )
    fetched: list[str] = []

    def fake_fetch(target: Any, _config: Any) -> Any:
        fetched.append(target.canonical_url)
        return discovery.FetchResult(
            target=target,
            status="fetched",
            rows=(
                {"url": "https://www.example.com/jobs/existing"},
                {"url": "https://www.example.com/jobs/new"},
            ),
        )

    summary = discovery.discover_recruitment_commoncrawl_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(),
        indexes=indexes,
        fetcher=fake_fetch,
    )

    discovered = _read_jsonl(tmp_path / discovery.DISCOVERED_FILENAME)
    assert completed_target.canonical_url in fetched
    assert [row["canonical_url"] for row in discovered] == [
        "https://www.example.com/jobs/existing",
        "https://www.example.com/jobs/new",
    ]
    assert "skipped_existing_receipt" not in summary


def test_resume_skips_new_fetched_receipt_with_materialization_evidence(tmp_path: Path) -> None:
    discovery = _load_discovery_module()
    rows = [
        _triage_row(
            record_id="one",
            links=[
                {
                    "text": "Careers",
                    "href": "https://www.example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]
    indexes = [
        discovery.IndexInfo(
            index_id="CC-MAIN-2026-25",
            cdx_api_url="https://index.commoncrawl.org/CC-MAIN-2026-25-index",
        )
    ]
    completed_target = discovery.build_query_targets(
        indexes, discovery.select_company_domains(rows)
    )[0]
    (tmp_path / discovery.RECEIPT_FILENAME).write_text(
        json.dumps(
            {
                "canonical_url": completed_target.canonical_url,
                "capture_status": "fetched",
                "result_count": 1,
                "retained_url_count": 1,
                "materialized_url_count": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / discovery.DISCOVERED_FILENAME).write_text(
        json.dumps(
            {
                "canonical_url": "https://www.example.com/jobs/existing",
                "source_commoncrawl_query_url": completed_target.canonical_url,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    fetched: list[str] = []

    def fake_fetch(target: Any, _config: Any) -> Any:
        fetched.append(target.canonical_url)
        return discovery.FetchResult(target=target, status="fetched")

    summary = discovery.discover_recruitment_commoncrawl_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(),
        indexes=indexes,
        fetcher=fake_fetch,
    )

    assert completed_target.canonical_url not in fetched
    assert summary["skipped_existing_receipt"] == 1


def test_max_discovered_urls_cap_leaves_queries_unfetched(tmp_path: Path) -> None:
    discovery = _load_discovery_module()
    rows = [
        _triage_row(
            record_id="one",
            links=[
                {
                    "text": "Careers",
                    "href": "https://www.example.com/careers",
                    "relationship": "first_party",
                }
            ],
        )
    ]
    indexes = [
        discovery.IndexInfo(
            index_id="CC-MAIN-2026-25",
            cdx_api_url="https://index.commoncrawl.org/CC-MAIN-2026-25-index",
        )
    ]

    def fake_fetch(target: Any, _config: Any) -> Any:
        return discovery.FetchResult(
            target=target,
            status="fetched",
            rows=({"url": f"https://www.example.com{target.pattern.replace('*', '/role')}"},),
        )

    summary = discovery.discover_recruitment_commoncrawl_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(),
        indexes=indexes,
        concurrency=1,
        max_discovered_urls=1,
        fetcher=fake_fetch,
    )

    discovered = _read_jsonl(tmp_path / discovery.DISCOVERED_FILENAME)
    assert len(discovered) == 1
    assert summary["discovery_cap_reached"] == 1
    assert summary["pending_unfetched"] > 0
