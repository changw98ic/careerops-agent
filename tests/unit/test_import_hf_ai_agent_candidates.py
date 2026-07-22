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
    script = Path(__file__).parents[2] / "scripts" / "import_hf_ai_agent_candidates.py"
    scripts_dir = str(script.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("import_hf_ai_agent_candidates", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _metadata(module: ModuleType) -> Any:
    return module.DatasetMetadata(
        api_url=module.SOURCE_API_URL,
        dataset_url=module.SOURCE_DATASET_URL,
        revision="38e5cd3ddad337c2a0d887325f02b3853c2b8c4c",
        license="mit",
        source_file=module.SOURCE_FILE,
        raw_file_url=module.raw_dataset_file_url(
            module.SOURCE_REPOSITORY,
            "38e5cd3ddad337c2a0d887325f02b3853c2b8c4c",
            module.SOURCE_FILE,
        ),
        body_bytes=321,
        body_sha256="b" * 64,
        attempts=1,
    )


def _fetch(module: ModuleType) -> Any:
    metadata = _metadata(module)
    return module.DatasetFetch(
        text="",
        url=metadata.raw_file_url,
        final_url=metadata.raw_file_url,
        http_status=200,
        content_type="application/json",
        body_bytes=123,
        body_truncated=False,
        body_sha256="a" * 64,
        attempts=1,
    )


def _dataset_line(**overrides: Any) -> str:
    row = {
        "content_name": "NewCo Agent",
        "publisher_id": "pub-newco-agent",
        "website": "https://www.newco.ai/?utm_source=list",
        "statistic": {"Bing Rank": 4.2},
        "subfield": "Coding Agent",
        "field": "AI AGENT",
        "description": "metadata only",
        "month": "202503",
        "content": "",
    }
    row.update(overrides)
    return json.dumps(row)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "application/json",
        final_url: str = "https://huggingface.co/datasets/repo/raw/sha/file.json",
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


def test_parse_source_records_filters_invalid_and_non_company_websites() -> None:
    importer = _load_importer_module()
    dataset_text = "\n".join(
        [
            _dataset_line(),
            _dataset_line(content_name="Docs", website="https://docs.example.com/guide"),
            _dataset_line(content_name="GitHub", website="https://github.com/org/project"),
            _dataset_line(content_name="Bad", website="mailto:hello@example.com"),
            _dataset_line(content_name="Missing", website=""),
        ]
    )

    records, summary = importer.parse_source_records(dataset_text)

    assert [record.registrable_domain for record in records] == ["newco.ai"]
    assert records[0].canonical_url == "https://www.newco.ai/"
    assert summary["source_rows"] == 5
    assert summary["filtered_non_company"] == 2
    assert summary["invalid_website"] == 1
    assert summary["missing_website"] == 1


def test_import_writes_provisional_rows_with_huggingface_mit_provenance(tmp_path: Path) -> None:
    importer = _load_importer_module()
    summary = importer.import_hf_candidates(
        _dataset_line(),
        _metadata(importer),
        _fetch(importer),
        tmp_path / "hf.jsonl",
        existing_record_ids=set(),
        existing_hostnames=set(),
        existing_domains=set(),
    )

    rows = _read_jsonl(tmp_path / "hf.jsonl")

    assert summary["candidate_written"] == 1
    assert rows[0]["candidate_entity_name"] == "NewCo Agent"
    assert rows[0]["candidate_type"] == "agent_ecosystem_company_candidate"
    assert rows[0]["representative_website"] == "https://www.newco.ai/"
    assert rows[0]["source_usage_status"] == (
        "publisher_declared_mit_metadata_seed_pending_human_provenance_review"
    )
    evidence = rows[0]["source_evidence"][0]
    assert evidence["dataset_url"] == importer.SOURCE_DATASET_URL
    assert evidence["dataset_license"] == "mit"
    assert evidence["source_row"] == 1
    assert evidence["source_record_id"] == "pub-newco-agent"
    assert "No linked product/company site was fetched" in rows[0]["collection_boundary"]


def test_select_new_candidates_dedupes_existing_record_domain_and_hostname() -> None:
    importer = _load_importer_module()
    records, _summary = importer.parse_source_records(
        "\n".join(
            [
                _dataset_line(content_name="Existing Record", website="https://existing-record.ai"),
                _dataset_line(
                    content_name="Existing Domain", website="https://app.existing-domain.ai"
                ),
                _dataset_line(content_name="Existing Host", website="https://seen.example.com"),
                _dataset_line(content_name="New", website="https://www.newco.ai"),
                _dataset_line(content_name="Duplicate New", website="https://app.newco.ai"),
            ]
        )
    )

    rows, summary = importer.select_new_candidates(
        records,
        _metadata(importer),
        _fetch(importer),
        existing_record_ids={"agent-ecosystem-domain--existing-record.ai"},
        existing_hostnames={"seen.example.com"},
        existing_domains={"existing-domain.ai"},
    )

    assert [row["registrable_domain"] for row in rows] == ["newco.ai"]
    assert summary["deduped_existing"] == 4


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


def test_resolve_dataset_metadata_uses_api_sha_license_and_sibling(monkeypatch: Any) -> None:
    importer = _load_importer_module()
    api_body = json.dumps(
        {
            "sha": "38e5cd3ddad337c2a0d887325f02b3853c2b8c4c",
            "cardData": {"license": "mit"},
            "siblings": [{"rfilename": ".gitattributes"}, {"rfilename": importer.SOURCE_FILE}],
        }
    ).encode()
    opener = _FakeOpener([_FakeResponse(api_body)])
    calls: list[bool] = []

    def fake_guard(
        _url: str, _proxies: dict[str, str], *, allow_sandbox_egress_alias: bool
    ) -> None:
        calls.append(allow_sandbox_egress_alias)

    monkeypatch.setattr(importer, "getproxies", lambda: {})
    monkeypatch.setattr(importer, "_ensure_public_http_url", fake_guard)
    monkeypatch.setattr(importer, "build_opener", lambda *_args: opener)

    metadata = importer.resolve_dataset_metadata(
        importer.SOURCE_API_URL,
        importer.ImportConfig(allow_sandbox_egress_alias=True),
    )

    assert metadata.revision == "38e5cd3ddad337c2a0d887325f02b3853c2b8c4c"
    assert metadata.license == "mit"
    assert metadata.raw_file_url.endswith(
        "/raw/38e5cd3ddad337c2a0d887325f02b3853c2b8c4c/data_agent_202503.json"
    )
    assert calls == [True]
    assert opener.opened_urls == [importer.SOURCE_API_URL]


def test_fetch_public_bytes_retries_retryable_errors(monkeypatch: Any) -> None:
    importer = _load_importer_module()
    opener = _FakeOpener(
        [
            HTTPError("https://hf.example/data", 503, "unavailable", HTTPMessage(), None),
            URLError("temporary"),
            _FakeResponse(b"ok"),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(importer, "getproxies", lambda: {})
    monkeypatch.setattr(importer, "_ensure_public_http_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(importer, "build_opener", lambda *_args: opener)
    monkeypatch.setattr(importer.time, "sleep", lambda seconds: sleeps.append(seconds))

    body, _final_url, _status, _content_type, attempts = importer._fetch_public_bytes(
        "https://hf.example/data",
        importer.ImportConfig(retry_backoff_seconds=0.25),
        max_bytes=10,
        accept="application/json",
    )

    assert body == b"ok"
    assert attempts == 3
    assert sleeps == [0.25, 0.5]


def test_fetch_public_bytes_does_not_retry_non_retryable_http(monkeypatch: Any) -> None:
    importer = _load_importer_module()
    opener = _FakeOpener(
        [HTTPError("https://hf.example/data", 404, "missing", HTTPMessage(), None)]
    )
    monkeypatch.setattr(importer, "getproxies", lambda: {})
    monkeypatch.setattr(importer, "_ensure_public_http_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(importer, "build_opener", lambda *_args: opener)

    try:
        importer._fetch_public_bytes(
            "https://hf.example/data",
            importer.ImportConfig(),
            max_bytes=10,
            accept="application/json",
        )
    except HTTPError as error:
        assert error.code == 404
    else:  # pragma: no cover - assertion branch
        raise AssertionError("404 should be terminal")

    assert len(opener.opened_urls) == 1


def test_fetch_public_bytes_enforces_streamed_response_size_cap(monkeypatch: Any) -> None:
    importer = _load_importer_module()
    opener = _FakeOpener([_FakeResponse(b"x" * 10)])
    monkeypatch.setattr(importer, "getproxies", lambda: {})
    monkeypatch.setattr(importer, "_ensure_public_http_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(importer, "build_opener", lambda *_args: opener)

    try:
        importer._fetch_public_bytes(
            "https://hf.example/data",
            importer.ImportConfig(),
            max_bytes=4,
            accept="application/json",
        )
    except importer.ResponseTooLargeError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("oversized response should fail")
