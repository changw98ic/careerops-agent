from __future__ import annotations

import importlib.util
import json
import sys
from email.message import Message
from http.client import HTTPMessage
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import HTTPError, URLError


def _load_importer_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "import_yc_ai_directory_candidates.py"
    scripts_dir = str(script.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("import_yc_ai_directory_candidates", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _company(**overrides: Any) -> dict[str, Any]:
    company = {
        "id": 123,
        "name": "NewCo AI",
        "slug": "newco-ai",
        "website": "https://www.newco.ai/?utm_source=yc",
        "batch": "S2026",
        "status": "Active",
        "location": "San Francisco",
        "team_size": 3,
        "one_liner": "Builds AI agents.",
        "tags": ["ai", "b2b"],
    }
    company.update(overrides)
    return company


def _html_with_payload(payload: dict[str, Any]) -> str:
    escaped = json.dumps(payload).replace("&", "&amp;").replace('"', "&quot;")
    return f'<html><body><div id="app" data-page="{escaped}"></div></body></html>'


def _fetch(module: ModuleType, html_text: str, *, page_number: int = 1) -> Any:
    body = html_text.encode()
    return module.PageFetch(
        page_number=page_number,
        url=module.directory_page_url(page_number),
        final_url=module.directory_page_url(page_number),
        http_status=200,
        content_type="text/html",
        body_bytes=len(body),
        body_sha256="a" * 64,
        attempts=1,
        text=html_text,
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        final_url: str = "https://www.ycombinator.com/companies/industry/ai",
        status: int = 200,
    ) -> None:
        self._body = body
        self._offset = 0
        self._final_url = final_url
        self._status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self._final_url

    def getcode(self) -> int:
        return self._status


class _FakeOpener:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.opened_urls: list[str] = []

    def open(self, request: Any, *, timeout: float) -> Any:
        del timeout
        self.opened_urls.append(request.full_url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_parse_data_page_payloads_decodes_html_escaped_json() -> None:
    importer = _load_importer_module()
    payload = {"props": {"companies": [_company()]}}

    payloads = importer.parse_data_page_payloads(_html_with_payload(payload))

    assert payloads == [payload]


def test_parse_source_records_recurses_and_filters_non_company_websites() -> None:
    importer = _load_importer_module()
    payload = {
        "props": {
            "companies": {
                "data": [
                    _company(),
                    _company(name="Docs", website="https://docs.example.com/guide"),
                    _company(
                        name="YC Profile", website="https://www.ycombinator.com/companies/foo"
                    ),
                    _company(name="Missing", website=""),
                    _company(name="Alt Website", website="", website_url="https://alt.example.ai"),
                ]
            }
        }
    }
    records, summary = importer.parse_source_records(
        [_fetch(importer, _html_with_payload(payload))]
    )

    assert [record.registrable_domain for record in records] == ["newco.ai", "example.ai"]
    assert records[0].canonical_url == "https://www.newco.ai/"
    assert records[0].yc_url == "https://www.ycombinator.com/companies/newco-ai"
    assert summary["company_like_objects"] == 5
    assert summary["filtered_without_company_website"] == 3


def test_import_writes_metadata_only_yc_provenance(tmp_path: Path) -> None:
    importer = _load_importer_module()
    fetch = _fetch(importer, _html_with_payload({"props": {"companies": [_company()]}}))

    summary = importer.import_yc_candidates(
        [fetch],
        tmp_path / "yc.jsonl",
        existing_record_ids=set(),
        existing_hostnames=set(),
        existing_domains=set(),
    )

    rows = _read_jsonl(tmp_path / "yc.jsonl")

    assert summary["candidate_written"] == 1
    assert summary["unique_output_domains"] == 1
    assert rows[0]["candidate_entity_name"] == "NewCo AI"
    assert rows[0]["candidate_type"] == "provisional_public_yc_ai_directory_company_candidate"
    assert rows[0]["representative_website"] == "https://www.newco.ai/"
    evidence = rows[0]["source_evidence"][0]
    assert evidence["source_id"] == importer.SOURCE_ID
    assert evidence["source_slug"] == "newco-ai"
    assert evidence["source_batch"] == "S2026"
    assert evidence["source_tags"] == ["ai", "b2b"]
    assert "No linked company site was fetched" in rows[0]["collection_boundary"]


def test_select_new_candidates_dedupes_existing_record_domain_and_hostname() -> None:
    importer = _load_importer_module()
    fetch = _fetch(
        importer,
        _html_with_payload(
            {
                "props": {
                    "companies": [
                        _company(name="Existing Record", website="https://existing-record.ai"),
                        _company(name="Existing Domain", website="https://app.existing-domain.ai"),
                        _company(name="Existing Host", website="https://seen.example.com"),
                        _company(name="New", website="https://www.newco.ai"),
                        _company(name="Duplicate New", website="https://app.newco.ai"),
                    ]
                }
            }
        ),
    )
    records, _parse_summary = importer.parse_source_records([fetch])

    rows, summary = importer.select_new_candidates(
        records,
        {1: fetch},
        existing_record_ids={"agent-ecosystem-domain--existing-record.ai"},
        existing_hostnames={"seen.example.com"},
        existing_domains={"existing-domain.ai"},
    )

    assert [row["registrable_domain"] for row in rows] == ["newco.ai"]
    assert summary["deduped_existing"] == 3
    assert summary["candidate_written"] == 1


def test_load_existing_identity_reads_record_ids_domains_and_hosts(tmp_path: Path) -> None:
    importer = _load_importer_module()
    path = tmp_path / "existing.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "record_id": "agent-ecosystem-domain--acme.ai",
                        "registrable_domain": "acme.ai",
                        "representative_website": "https://www.acme.ai/",
                    }
                ),
                json.dumps({"record_id": "other", "website": "https://docs.example.co.uk"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    record_ids, hostnames, domains = importer.load_existing_identity([path])

    assert "agent-ecosystem-domain--acme.ai" in record_ids
    assert "www.acme.ai" in hostnames
    assert "example.co.uk" in domains


def test_fetch_page_retries_transient_errors(monkeypatch: Any) -> None:
    importer = _load_importer_module()
    html_text = _html_with_payload({"props": {"companies": [_company()]}})
    opener = _FakeOpener(
        [
            HTTPError(importer.SOURCE_URL, 503, "unavailable", HTTPMessage(), None),
            URLError("temporary"),
            _FakeResponse(html_text.encode()),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(importer, "getproxies", lambda: {})
    monkeypatch.setattr(importer, "_ensure_public_http_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(importer, "build_opener", lambda *_args: opener)
    monkeypatch.setattr(importer.time, "sleep", lambda seconds: sleeps.append(seconds))

    fetch = importer.fetch_page(
        1,
        importer.ImportConfig(retry_backoff_seconds=0.25),
    )

    assert fetch.text == html_text
    assert fetch.attempts == 3
    assert sleeps == [0.25, 0.5]


def test_fetch_page_stops_on_403_without_retry(monkeypatch: Any) -> None:
    importer = _load_importer_module()
    opener = _FakeOpener([HTTPError(importer.SOURCE_URL, 403, "forbidden", HTTPMessage(), None)])
    monkeypatch.setattr(importer, "getproxies", lambda: {})
    monkeypatch.setattr(importer, "_ensure_public_http_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(importer, "build_opener", lambda *_args: opener)

    try:
        importer.fetch_page(1, importer.ImportConfig())
    except importer.TerminalHttpStatusError as error:
        assert error.status_code == 403
    else:  # pragma: no cover - assertion branch
        raise AssertionError("403 should stop collection")

    assert len(opener.opened_urls) == 1


def test_fetch_page_enforces_response_size_cap(monkeypatch: Any) -> None:
    importer = _load_importer_module()
    opener = _FakeOpener([_FakeResponse(b"x" * 10)])
    monkeypatch.setattr(importer, "getproxies", lambda: {})
    monkeypatch.setattr(importer, "_ensure_public_http_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(importer, "build_opener", lambda *_args: opener)

    try:
        importer.fetch_page(1, importer.ImportConfig(max_response_bytes=4))
    except importer.ResponseTooLargeError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("oversized page should fail")


def test_directory_page_url_uses_plain_first_page_and_query_later() -> None:
    importer = _load_importer_module()

    assert importer.directory_page_url(1) == importer.SOURCE_URL
    assert importer.directory_page_url(7) == f"{importer.SOURCE_URL}?page=7"
