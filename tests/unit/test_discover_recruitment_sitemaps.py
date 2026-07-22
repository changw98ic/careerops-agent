from __future__ import annotations

import gzip
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
    script = Path(__file__).parents[2] / "scripts" / "discover_recruitment_sitemaps.py"
    scripts_dir = str(script.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("discover_recruitment_sitemaps", script)
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
    links: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "registrable_domain": registrable_domain,
        "employer_hiring_assessment": assessment,
        "links": links,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_select_sitemap_roots_filters_dedupes_and_scopes_known_ats() -> None:
    discovery = _load_discovery_module()
    rows = [
        _triage_row(
            record_id="one",
            links=[
                {
                    "text": "Careers",
                    "href": "https://www.example.com/careers",
                    "relationship": "first_party",
                },
                {
                    "text": "Jobs duplicate origin",
                    "href": "https://www.example.com/jobs",
                    "relationship": "first_party",
                },
                {
                    "text": "Blog",
                    "href": "https://www.example.com/blog",
                    "relationship": "first_party",
                },
            ],
        ),
        _triage_row(
            record_id="two",
            registrable_domain="acme.ai",
            assessment="external_ats_needs_verification",
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
            links=[
                {
                    "text": "Careers",
                    "href": "https://ignored.example/careers",
                    "relationship": "first_party",
                }
            ],
        ),
    ]

    roots = discovery.select_sitemap_roots(rows)

    assert [(root.root_url, root.ats_path_prefix, root.source_record_id) for root in roots] == [
        ("https://www.example.com", None, "one"),
        ("https://boards.greenhouse.io", "/acme", "two"),
    ]


def test_parse_robots_and_sitemap_xml_retains_recruitment_urls_only() -> None:
    discovery = _load_discovery_module()
    robots = """
    User-agent: *
    Sitemap: /sitemap.xml
    Sitemap: https://www.example.com/jobs-sitemap.xml
    """
    xml = """
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://www.example.com/careers/software-engineer</loc></url>
      <url><loc>https://www.example.com/blog/company-update</loc></url>
      <url><loc>https://jobs.example.net/open-role</loc></url>
    </urlset>
    """
    root = discovery.SitemapRoot(
        root_url="https://www.example.com",
        canonical_source_url="https://www.example.com/careers",
        source_record_id="one",
        source_index=0,
        source_assessment="first_party_career_entry",
        source_relationship="first_party",
        registrable_domain="example.com",
    )

    assert discovery.parse_robots_sitemaps(robots, "https://www.example.com/robots.txt") == (
        "https://www.example.com/sitemap.xml",
        "https://www.example.com/jobs-sitemap.xml",
    )
    retained = [
        loc
        for loc in discovery.parse_sitemap_xml(xml)
        if discovery.is_retained_recruitment_url(loc, root)
    ]
    assert retained == ["https://www.example.com/careers/software-engineer"]


def test_known_ats_sitemap_retention_stays_inside_customer_path() -> None:
    discovery = _load_discovery_module()
    root = discovery.SitemapRoot(
        root_url="https://boards.greenhouse.io",
        canonical_source_url="https://boards.greenhouse.io/acme/jobs",
        source_record_id="ats",
        source_index=0,
        source_assessment="external_ats_needs_verification",
        source_relationship="known_ats",
        registrable_domain="acme.ai",
        ats_path_prefix="/acme",
    )

    assert discovery.is_retained_recruitment_url(
        "https://boards.greenhouse.io/acme/jobs/software-engineer", root
    )
    assert not discovery.is_retained_recruitment_url(
        "https://boards.greenhouse.io/otherco/jobs/software-engineer", root
    )
    assert not discovery.is_retained_recruitment_url(
        "https://jobs.ashbyhq.com/acme/jobs/software-engineer", root
    )
    assert not discovery.is_allowed_sitemap_target_url(
        "https://boards.greenhouse.io/otherco/jobs-sitemap.xml", root
    )
    assert discovery.is_allowed_sitemap_target_url(
        "https://boards.greenhouse.io/acme/jobs-sitemap.xml", root
    )


def test_discovery_follows_robots_sitemap_index_and_writes_unique_urls(tmp_path: Path) -> None:
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
    responses = {
        "https://www.example.com/robots.txt": (
            "fetched",
            "Sitemap: https://www.example.com/sitemap-index.xml\n",
        ),
        "https://www.example.com/sitemap.xml": ("http_error", None),
        "https://www.example.com/sitemap_index.xml": ("http_error", None),
        "https://www.example.com/sitemap-index.xml": (
            "fetched",
            """
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <sitemap><loc>https://www.example.com/jobs-sitemap.xml</loc></sitemap>
            </sitemapindex>
            """,
        ),
        "https://www.example.com/sitemap/sitemap.xml": ("http_error", None),
        "https://www.example.com/sitemaps.xml": ("http_error", None),
        "https://www.example.com/jobs-sitemap.xml": (
            "fetched",
            """
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://www.example.com/careers/software-engineer</loc></url>
              <url><loc>https://www.example.com/careers/software-engineer</loc></url>
              <url><loc>https://www.example.com/blog/post</loc></url>
            </urlset>
            """,
        ),
    }

    def fake_fetch(target: Any, _config: Any) -> Any:
        status, body = responses[target.canonical_url]
        return discovery.FetchResult(target=target, status=status, body_text=body)

    summary = discovery.discover_recruitment_sitemap_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(),
        fetcher=fake_fetch,
    )

    discovered = _read_jsonl(tmp_path / discovery.DISCOVERED_FILENAME)
    assert summary["discovered_url_written"] == 1
    assert [row["canonical_url"] for row in discovered] == [
        "https://www.example.com/careers/software-engineer"
    ]
    assert summary["sitemap_enqueued_from_robots"] == 0
    assert summary["sitemap_index_child_enqueued"] == 1


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
        return discovery.FetchResult(
            target=target,
            status="fetched",
            body_text="""
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://www.example.com/careers/software-engineer</loc></url>
            </urlset>
            """,
        )

    summary = discovery.discover_recruitment_sitemap_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(),
        concurrency=3,
        fetcher=fake_fetch,
    )

    assert max_active == 3
    assert summary["attempted"] == 6
    assert summary["discovered_url_written"] == 1


def test_network_errors_retry_five_total_attempts(tmp_path: Path) -> None:
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

    def fake_fetch(target: Any, _config: Any) -> Any:
        return discovery.FetchResult(
            target=target, status="network_error", error_kind="TimeoutError"
        )

    summary = discovery.discover_recruitment_sitemap_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(),
        fetcher=fake_fetch,
    )

    receipts = _read_jsonl(tmp_path / discovery.RECEIPT_FILENAME)
    assert summary["attempted"] == 30
    assert summary["network_error_retry_exhausted"] == 6
    assert len(receipts) == 30


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

    def fake_fetch(target: Any, _config: Any) -> Any:
        return discovery.FetchResult(
            target=target,
            status="retryable_http_error",
            http_status=429,
            error_kind="HTTPError",
        )

    summary = discovery.discover_recruitment_sitemap_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(),
        fetcher=fake_fetch,
    )

    receipts = _read_jsonl(tmp_path / discovery.RECEIPT_FILENAME)
    assert summary["attempted"] == 30
    assert summary["retryable_http_error"] == 30
    assert summary["network_error_retry_exhausted"] == 6
    assert len(receipts) == 30


def test_non_retryable_http_errors_are_terminal(tmp_path: Path) -> None:
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

    def fake_fetch(target: Any, _config: Any) -> Any:
        return discovery.FetchResult(
            target=target,
            status="http_error",
            http_status=404,
            error_kind="HTTPError",
        )

    summary = discovery.discover_recruitment_sitemap_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(),
        fetcher=fake_fetch,
    )

    receipts = _read_jsonl(tmp_path / discovery.RECEIPT_FILENAME)
    assert summary["attempted"] == 6
    assert summary["http_error"] == 6
    assert "retry_enqueued" not in summary
    assert len(receipts) == 6


def test_fetch_maps_429_and_5xx_http_errors_to_retryable_status(monkeypatch: Any) -> None:
    discovery = _load_discovery_module()
    root = discovery.SitemapRoot(
        root_url="https://www.example.com",
        canonical_source_url="https://www.example.com/careers",
        source_record_id="one",
        source_index=0,
        source_assessment="first_party_career_entry",
        source_relationship="first_party",
        registrable_domain="example.com",
    )
    target = discovery.FetchTarget(
        canonical_url="https://www.example.com/sitemap.xml",
        url="https://www.example.com/sitemap.xml",
        kind="sitemap",
        root=root,
    )
    codes = [429, 500, 404]

    class _RaisingOpener:
        def open(self, request: Any, *, timeout: float) -> Any:
            code = codes.pop(0)
            raise HTTPError(request.full_url, code, "status", HTTPMessage(), None)

    monkeypatch.setattr(discovery, "_ensure_public_http_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(discovery, "build_opener", lambda *_args: _RaisingOpener())

    statuses = [
        discovery.fetch_sitemap_target(target, discovery.FetchConfig()).status for _ in range(3)
    ]

    assert statuses == ["retryable_http_error", "retryable_http_error", "http_error"]


def test_bounded_gzip_decompress_rejects_expansion_over_cap() -> None:
    discovery = _load_discovery_module()
    compressed = gzip.compress(b"a" * 128)

    try:
        discovery._bounded_gzip_decompress(compressed, 32)
    except discovery.DecompressedSizeLimitError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("gzip expansion above max_sitemap_bytes must fail")

    decompressed, truncated = discovery._bounded_gzip_decompress(compressed, 128)
    assert decompressed == b"a" * 128
    assert truncated is False


def test_resume_skips_completed_and_existing_discovered_urls(tmp_path: Path) -> None:
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
    discovered_path = tmp_path / discovery.DISCOVERED_FILENAME
    receipt_path = tmp_path / discovery.RECEIPT_FILENAME
    discovered_path.write_text(
        json.dumps({"canonical_url": "https://www.example.com/careers/software-engineer"}) + "\n",
        encoding="utf-8",
    )
    receipt_path.write_text(
        json.dumps(
            {"canonical_url": "https://www.example.com/robots.txt", "capture_status": "fetched"}
        )
        + "\n",
        encoding="utf-8",
    )
    fetched: list[str] = []

    def fake_fetch(target: Any, _config: Any) -> Any:
        fetched.append(target.canonical_url)
        return discovery.FetchResult(
            target=target,
            status="fetched",
            body_text="""
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
              <url><loc>https://www.example.com/careers/software-engineer</loc></url>
              <url><loc>https://www.example.com/careers/product-manager</loc></url>
            </urlset>
            """,
        )

    summary = discovery.discover_recruitment_sitemap_urls(
        rows,
        tmp_path,
        config=discovery.FetchConfig(),
        fetcher=fake_fetch,
    )

    assert "https://www.example.com/robots.txt" not in fetched
    discovered = _read_jsonl(discovered_path)
    assert [row["canonical_url"] for row in discovered] == [
        "https://www.example.com/careers/software-engineer",
        "https://www.example.com/careers/product-manager",
    ]
    assert summary["discovered_duplicate_skipped"] == 9
