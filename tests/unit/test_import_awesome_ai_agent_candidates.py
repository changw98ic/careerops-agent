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
    script = Path(__file__).parents[2] / "scripts" / "import_awesome_ai_agent_candidates.py"
    scripts_dir = str(script.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("import_awesome_ai_agent_candidates", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fetch(module: ModuleType) -> Any:
    return module.ReadmeFetch(
        text="",
        url=module.SOURCE_README_URL,
        final_url=module.SOURCE_README_URL,
        http_status=200,
        content_type="text/plain",
        body_bytes=123,
        body_truncated=False,
        body_sha256="a" * 64,
        attempts=1,
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "text/plain; charset=utf-8",
        final_url: str = "https://raw.githubusercontent.com/repo/commit/README.md",
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

    def read(self, _size: int = -1) -> bytes:
        return self._body

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


def test_extract_markdown_links_parses_http_links_with_line_numbers() -> None:
    importer = _load_importer_module()
    markdown = """
    # Agents
    - [Acme Agent](https://www.acme.ai/?utm_source=list)
    - <https://example.com/product>
    - bare https://contoso.ai
    - [Mail](mailto:hello@example.com)
    """

    links = importer.extract_markdown_links(markdown)

    assert [link.canonical_url for link in links] == [
        "https://www.acme.ai/",
        "https://example.com/product",
        "https://contoso.ai/",
    ]
    assert links[0].label == "Acme Agent"
    assert links[0].source_line == 3


def test_filter_excludes_platform_social_docs_and_cdn_hosts() -> None:
    importer = _load_importer_module()
    kept = importer.canonicalize_url("https://www.acme.ai/")
    excluded = [
        "https://github.com/org/project",
        "https://huggingface.co/spaces/org/demo",
        "https://twitter.com/acme",
        "https://docs.example.com/guide",
        "https://cdn.jsdelivr.net/npm/pkg",
        "https://project.github.io/",
    ]

    assert importer.is_company_link_candidate(kept)
    excluded_results = [
        importer.is_company_link_candidate(importer.canonicalize_url(url)) for url in excluded
    ]
    assert excluded_results == [
        False,
        False,
        False,
        False,
        False,
        False,
    ]


def test_select_new_candidates_dedupes_existing_record_domain_and_hostname() -> None:
    importer = _load_importer_module()
    links = importer.extract_markdown_links(
        "\n".join(
            [
                "[Existing record](https://existing-record.ai)",
                "[Existing domain](https://app.existing-domain.ai)",
                "[Existing host](https://seen.example.com)",
                "[New](https://www.newco.ai)",
                "[Duplicate new](https://app.newco.ai)",
            ]
        )
    )
    rows, summary = importer.select_new_candidates(
        links,
        _fetch(importer),
        existing_record_ids={"agent-ecosystem-domain--existing-record.ai"},
        existing_hostnames={"seen.example.com"},
        existing_domains={"existing-domain.ai"},
    )

    assert [row["registrable_domain"] for row in rows] == ["newco.ai"]
    assert summary["deduped_existing"] == 4
    assert rows[0]["record_id"] == "agent-ecosystem-domain--newco.ai"


def test_candidate_rows_are_provisional_with_source_provenance(tmp_path: Path) -> None:
    importer = _load_importer_module()
    readme = "- [NewCo Agent](https://www.newco.ai)"
    summary = importer.import_awesome_candidates(
        readme,
        _fetch(importer),
        tmp_path / "awesome.jsonl",
        existing_record_ids=set(),
        existing_hostnames=set(),
        existing_domains=set(),
    )

    rows = _read_jsonl(tmp_path / "awesome.jsonl")

    assert summary["candidate_written"] == 1
    assert rows[0]["candidate_entity_name"] == "NewCo Agent"
    assert rows[0]["candidate_type"] == "provisional_public_awesome_ai_agent_link_candidate"
    assert rows[0]["candidate_state"] == "unverified"
    assert rows[0]["representative_website"] == "https://www.newco.ai/"
    assert rows[0]["source_evidence"][0]["source_commit"] == importer.SOURCE_COMMIT
    assert rows[0]["source_evidence"][0]["source_license"] == "Apache-2.0"
    assert "No linked product/company site was fetched" in rows[0]["collection_boundary"]


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


def test_fetch_readme_uses_public_guard_and_sandbox_alias_flag(monkeypatch: Any) -> None:
    importer = _load_importer_module()
    calls: list[bool] = []
    opener = _FakeOpener([_FakeResponse(b"# README\n")])

    def fake_guard(
        _url: str, _proxies: dict[str, str], *, allow_sandbox_egress_alias: bool
    ) -> None:
        calls.append(allow_sandbox_egress_alias)

    monkeypatch.setattr(importer, "getproxies", lambda: {})
    monkeypatch.setattr(importer, "_ensure_public_http_url", fake_guard)
    monkeypatch.setattr(importer, "build_opener", lambda *_args: opener)

    fetch = importer.fetch_readme(
        importer.SOURCE_README_URL,
        importer.ImportConfig(allow_sandbox_egress_alias=True),
    )

    assert calls == [True]
    assert fetch.text == "# README\n"
    assert fetch.attempts == 1
    assert opener.opened_urls == [importer.SOURCE_README_URL]


def test_fetch_readme_retries_retryable_http_and_url_errors(monkeypatch: Any) -> None:
    importer = _load_importer_module()
    opener = _FakeOpener(
        [
            HTTPError("https://raw.example/readme", 503, "unavailable", HTTPMessage(), None),
            URLError("temporary"),
            _FakeResponse(b"ok"),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(importer, "getproxies", lambda: {})
    monkeypatch.setattr(importer, "_ensure_public_http_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(importer, "build_opener", lambda *_args: opener)
    monkeypatch.setattr(importer.time, "sleep", lambda seconds: sleeps.append(seconds))

    fetch = importer.fetch_readme(
        "https://raw.example/readme",
        importer.ImportConfig(retry_backoff_seconds=0.25),
    )

    assert fetch.text == "ok"
    assert fetch.attempts == 3
    assert sleeps == [0.25, 0.5]


def test_fetch_readme_does_not_retry_non_retryable_http(monkeypatch: Any) -> None:
    importer = _load_importer_module()
    opener = _FakeOpener(
        [HTTPError("https://raw.example/readme", 404, "missing", HTTPMessage(), None)]
    )
    monkeypatch.setattr(importer, "getproxies", lambda: {})
    monkeypatch.setattr(importer, "_ensure_public_http_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(importer, "build_opener", lambda *_args: opener)

    try:
        importer.fetch_readme("https://raw.example/readme", importer.ImportConfig())
    except HTTPError as error:
        assert error.code == 404
    else:  # pragma: no cover - assertion branch
        raise AssertionError("404 should be terminal")

    assert len(opener.opened_urls) == 1


def test_fetch_readme_enforces_response_size_cap(monkeypatch: Any) -> None:
    importer = _load_importer_module()
    opener = _FakeOpener([_FakeResponse(b"x" * 10)])
    monkeypatch.setattr(importer, "getproxies", lambda: {})
    monkeypatch.setattr(importer, "_ensure_public_http_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(importer, "build_opener", lambda *_args: opener)

    try:
        importer.fetch_readme(
            "https://raw.example/readme",
            importer.ImportConfig(max_response_bytes=4),
        )
    except importer.ResponseTooLargeError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("oversized README should fail")
