from __future__ import annotations

import importlib.util
import json
import socket
import sys
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_collector_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "collect_agent_homepages.py"
    spec = importlib.util.spec_from_file_location("collect_agent_homepages", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _record(record_id: str, url: str) -> dict[str, str]:
    return {
        "record_id": record_id,
        "candidate_entity_name": record_id,
        "registrable_domain": "example.test",
        "representative_website": url,
    }


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str,
        final_url: str = "https://public.example/ok",
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


def _patch_public_dns(monkeypatch: Any, collector: ModuleType) -> None:
    def fake_getaddrinfo(host: str, port: int | None, *, type: int) -> list[Any]:
        assert type == socket.SOCK_STREAM
        if host == "public.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]
        if host == "private.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port or 443))]
        if host == "mapped.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.10", port or 443))]
        if host == "arbitrary-nonglobal.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.10", port or 443))]
        raise socket.gaierror(f"unexpected host {host}")

    monkeypatch.setattr(collector.socket, "getaddrinfo", fake_getaddrinfo)


def _patch_proxy_config(monkeypatch: Any, collector: ModuleType, proxies: dict[str, str]) -> None:
    monkeypatch.setattr(collector, "getproxies", lambda: proxies)
    monkeypatch.setattr(collector, "proxy_bypass", lambda _hostname: False)


def test_fetch_candidate_hashes_raw_body_from_public_target(monkeypatch: Any) -> None:
    collector = _load_collector_module()
    _patch_public_dns(monkeypatch, collector)
    _patch_proxy_config(monkeypatch, collector, {})
    opener = _FakeOpener(
        [_FakeResponse(b"<html><body>Agent homepage</body></html>", content_type="text/html")]
    )
    monkeypatch.setattr(collector, "build_opener", lambda *_args: opener)

    outcome = collector.fetch_candidate(
        _record("public", "https://public.example/ok"),
        0,
        collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
    )

    assert outcome.receipt["capture_status"] == "captured"
    assert outcome.receipt["final_url"].endswith("/ok")
    assert outcome.raw_row is not None
    assert outcome.raw_row["response_text"] == "<html><body>Agent homepage</body></html>"
    assert len(outcome.raw_row["body_sha256"]) == 64
    assert opener.opened_urls == ["https://public.example/ok"]


def test_fetch_candidate_marks_truncation_and_unsupported_content_type(monkeypatch: Any) -> None:
    collector = _load_collector_module()
    _patch_public_dns(monkeypatch, collector)
    _patch_proxy_config(monkeypatch, collector, {})
    opener = _FakeOpener(
        [
            _FakeResponse(b"x" * 32, content_type="text/plain; charset=utf-8"),
            _FakeResponse(b"\x89PNG\r\n", content_type="image/png"),
        ]
    )
    monkeypatch.setattr(collector, "build_opener", lambda *_args: opener)

    truncated = collector.fetch_candidate(
        _record("large", "https://public.example/large"),
        0,
        collector.CollectorConfig(timeout_seconds=2, max_bytes=8),
    )
    binary = collector.fetch_candidate(
        _record("binary", "https://public.example/binary"),
        1,
        collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
    )

    assert truncated.receipt["capture_status"] == "captured"
    assert truncated.receipt["body_truncated"] is True
    assert truncated.raw_row is not None
    assert truncated.raw_row["response_text"] == "x" * 8
    assert binary.raw_row is None
    assert binary.receipt["capture_status"] == "unsupported_content_type"


def test_fetch_candidate_rejects_non_http_url_without_network_request(monkeypatch: Any) -> None:
    collector = _load_collector_module()
    _patch_proxy_config(monkeypatch, collector, {})
    opened = False

    def fail_build_opener(*_args: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("network opener must not be built for invalid schemes")

    monkeypatch.setattr(collector, "build_opener", fail_build_opener)

    outcome = collector.fetch_candidate(
        _record("local-file", "file:///private/example.html"),
        0,
        collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
    )

    assert outcome.raw_row is None
    assert outcome.receipt["capture_status"] == "invalid_url"
    assert outcome.receipt["raw_data_file"] is None
    assert opened is False


def test_fetch_candidate_rejects_private_host_without_http_connection(monkeypatch: Any) -> None:
    collector = _load_collector_module()
    _patch_public_dns(monkeypatch, collector)
    _patch_proxy_config(monkeypatch, collector, {})
    opened = False

    def fail_build_opener(*_args: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("network opener must not be built for private hosts")

    monkeypatch.setattr(collector, "build_opener", fail_build_opener)

    outcome = collector.fetch_candidate(
        _record("private", "https://private.example/"),
        0,
        collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
    )

    assert outcome.raw_row is None
    assert outcome.receipt["capture_status"] == "invalid_url"
    assert opened is False


def test_fetch_candidate_allows_hostname_mapped_to_non_global_dns_when_proxy_applies(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_proxy_config(monkeypatch, collector, {"https": "http://proxy.example:8080"})
    monkeypatch.setattr(
        collector.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("DNS must not be used for proxied hostnames")
        ),
    )
    opener = _FakeOpener([_FakeResponse(b"proxied", content_type="text/html")])
    monkeypatch.setattr(collector, "build_opener", lambda *_args: opener)

    outcome = collector.fetch_candidate(
        _record("mapped", "https://mapped.example/"),
        0,
        collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
    )

    assert outcome.receipt["capture_status"] == "captured"
    assert outcome.raw_row is not None
    assert outcome.raw_row["response_text"] == "proxied"
    assert opener.opened_urls == ["https://mapped.example/"]


def test_fetch_candidate_rejects_hostname_mapped_to_non_global_dns_without_proxy(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_public_dns(monkeypatch, collector)
    _patch_proxy_config(monkeypatch, collector, {})
    opened = False

    def fail_build_opener(*_args: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("network opener must not be built for non-global DNS")

    monkeypatch.setattr(collector, "build_opener", fail_build_opener)

    outcome = collector.fetch_candidate(
        _record("mapped", "https://mapped.example/"),
        0,
        collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
    )

    assert outcome.raw_row is None
    assert outcome.receipt["capture_status"] == "invalid_url"
    assert opened is False


def test_fetch_candidate_rejects_non_global_ip_literal_even_when_proxy_applies(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_proxy_config(monkeypatch, collector, {"https": "http://proxy.example:8080"})
    opened = False

    def fail_build_opener(*_args: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("network opener must not be built for non-global IP literals")

    monkeypatch.setattr(collector, "build_opener", fail_build_opener)

    outcome = collector.fetch_candidate(
        _record("literal", "https://198.18.0.10/"),
        0,
        collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
    )

    assert outcome.raw_row is None
    assert outcome.receipt["capture_status"] == "invalid_url"
    assert opened is False


def test_fetch_candidate_rejects_invalid_port_even_when_proxy_applies(monkeypatch: Any) -> None:
    collector = _load_collector_module()
    _patch_proxy_config(monkeypatch, collector, {"https": "http://proxy.example:8080"})
    opened = False

    def fail_build_opener(*_args: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("network opener must not be built for invalid ports")

    monkeypatch.setattr(collector, "build_opener", fail_build_opener)

    outcome = collector.fetch_candidate(
        _record("bad-port", "https://public.example:notaport/"),
        0,
        collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
    )

    assert outcome.raw_row is None
    assert outcome.receipt["capture_status"] == "invalid_url"
    assert opened is False


def test_redirect_to_local_target_is_rejected_before_followup_connection(monkeypatch: Any) -> None:
    collector = _load_collector_module()
    _patch_public_dns(monkeypatch, collector)
    _patch_proxy_config(monkeypatch, collector, {})

    class RedirectingOpener:
        def __init__(self) -> None:
            self.followup_opened = False

        def open(self, request: Any, *, timeout: float) -> _FakeResponse:
            handler = collector._PublicRedirectHandler({}, allow_sandbox_egress_alias=False)
            handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/private")
            self.followup_opened = True
            return _FakeResponse(b"should not happen", content_type="text/html")

    opener = RedirectingOpener()
    monkeypatch.setattr(collector, "build_opener", lambda *_args: opener)

    outcome = collector.fetch_candidate(
        _record("redirect-private", "https://public.example/redirect"),
        0,
        collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
    )

    assert outcome.raw_row is None
    assert outcome.receipt["capture_status"] == "invalid_url"
    assert opener.followup_opened is False


def test_redirect_to_mapped_hostname_uses_proxy_aware_validation(monkeypatch: Any) -> None:
    collector = _load_collector_module()
    proxies = {"https": "http://proxy.example:8080"}
    monkeypatch.setattr(collector, "proxy_bypass", lambda _hostname: False)
    monkeypatch.setattr(
        collector.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("DNS must not be used for proxied redirect hostnames")
        ),
    )

    handler = collector._PublicRedirectHandler(proxies, allow_sandbox_egress_alias=False)
    redirect = handler.redirect_request(
        collector.Request("https://public.example/redirect"),
        None,
        302,
        "Found",
        {},
        "https://mapped.example/next",
    )

    assert redirect is not None
    assert redirect.full_url == "https://mapped.example/next"


def test_fetch_candidate_rejects_mapped_hostname_without_sandbox_alias_flag(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_public_dns(monkeypatch, collector)
    _patch_proxy_config(monkeypatch, collector, {})
    opened = False

    def fail_build_opener(*_args: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("network opener must not be built without alias opt-in")

    monkeypatch.setattr(collector, "build_opener", fail_build_opener)

    outcome = collector.fetch_candidate(
        _record("mapped", "https://mapped.example/"),
        0,
        collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
    )

    assert outcome.raw_row is None
    assert outcome.receipt["capture_status"] == "invalid_url"
    assert opened is False


def test_fetch_candidate_allows_mapped_hostname_with_sandbox_alias_flag(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_public_dns(monkeypatch, collector)
    _patch_proxy_config(monkeypatch, collector, {})
    opener = _FakeOpener([_FakeResponse(b"aliased", content_type="text/html")])
    monkeypatch.setattr(collector, "build_opener", lambda *_args: opener)

    outcome = collector.fetch_candidate(
        _record("mapped", "https://mapped.example/"),
        0,
        collector.CollectorConfig(
            timeout_seconds=2,
            max_bytes=1024,
            allow_sandbox_egress_alias=True,
        ),
    )

    assert outcome.receipt["capture_status"] == "captured"
    assert outcome.raw_row is not None
    assert outcome.raw_row["response_text"] == "aliased"
    assert opener.opened_urls == ["https://mapped.example/"]


def test_fetch_candidate_rejects_sandbox_alias_ip_literal_even_with_flag(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_proxy_config(monkeypatch, collector, {})
    opened = False

    def fail_build_opener(*_args: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("network opener must not be built for alias IP literals")

    monkeypatch.setattr(collector, "build_opener", fail_build_opener)

    outcome = collector.fetch_candidate(
        _record("literal", "https://198.18.0.10/"),
        0,
        collector.CollectorConfig(
            timeout_seconds=2,
            max_bytes=1024,
            allow_sandbox_egress_alias=True,
        ),
    )

    assert outcome.raw_row is None
    assert outcome.receipt["capture_status"] == "invalid_url"
    assert opened is False


def test_fetch_candidate_rejects_arbitrary_non_global_dns_even_with_alias_flag(
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    _patch_public_dns(monkeypatch, collector)
    _patch_proxy_config(monkeypatch, collector, {})
    opened = False

    def fail_build_opener(*_args: object) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("network opener must not be built for arbitrary non-global DNS")

    monkeypatch.setattr(collector, "build_opener", fail_build_opener)

    outcome = collector.fetch_candidate(
        _record("arbitrary", "https://arbitrary-nonglobal.example/"),
        0,
        collector.CollectorConfig(
            timeout_seconds=2,
            max_bytes=1024,
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
    _patch_public_dns(monkeypatch, collector)
    monkeypatch.setattr(collector, "proxy_bypass", lambda _hostname: False)

    handler = collector._PublicRedirectHandler({}, allow_sandbox_egress_alias=True)
    redirect = handler.redirect_request(
        collector.Request("https://public.example/redirect"),
        None,
        302,
        "Found",
        {},
        "https://mapped.example/next",
    )

    assert redirect is not None
    assert redirect.full_url == "https://mapped.example/next"


def test_collect_candidates_resumes_from_existing_receipts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    collector = _load_collector_module()
    _patch_public_dns(monkeypatch, collector)
    _patch_proxy_config(monkeypatch, collector, {})
    output_dir = tmp_path / "raw"
    records = [_record("one", "https://public.example/one"), _record("two", "ftp://example.test/")]
    opener = _FakeOpener([_FakeResponse(b"one", content_type="text/html")])
    monkeypatch.setattr(collector, "build_opener", lambda *_args: opener)
    first = collector.collect_candidates(
        records,
        output_dir,
        config=collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
        workers=2,
    )
    second = collector.collect_candidates(
        records,
        output_dir,
        config=collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
        workers=2,
    )

    receipt_path = output_dir / collector.RECEIPT_FILENAME
    receipts = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
    assert first["attempted"] == 2
    assert first["captured"] == 1
    assert first["invalid_url"] == 1
    assert second["attempted"] == 0
    assert second["skipped_existing_receipt"] == 2
    assert {receipt["record_id"] for receipt in receipts} == {"one", "two"}


def test_collect_candidates_resumes_from_existing_raw_rows(
    tmp_path: Path, monkeypatch: Any
) -> None:
    collector = _load_collector_module()
    _patch_public_dns(monkeypatch, collector)
    _patch_proxy_config(monkeypatch, collector, {})
    output_dir = tmp_path / "raw"
    output_dir.mkdir()
    raw_path = output_dir / collector.RAW_FILENAME
    raw_path.write_text(
        json.dumps({"record_id": "one", "response_text": "already captured"}) + "\n",
        encoding="utf-8",
    )
    opener = _FakeOpener([_FakeResponse(b"two", content_type="text/html")])
    monkeypatch.setattr(collector, "build_opener", lambda *_args: opener)

    summary = collector.collect_candidates(
        [
            _record("one", "https://public.example/one"),
            _record("two", "https://public.example/two"),
        ],
        output_dir,
        config=collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
        workers=1,
    )

    raw_rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    receipt_path = output_dir / collector.RECEIPT_FILENAME
    receipts = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
    assert summary["attempted"] == 1
    assert summary["skipped_existing_receipt"] == 1
    assert summary["synthesized_missing_receipts"] == 1
    assert [row["record_id"] for row in raw_rows] == ["one", "two"]
    assert [receipt["record_id"] for receipt in receipts] == ["one", "two"]
    assert receipts[0]["capture_status"] == "captured"
    assert receipts[0]["raw_data_file"] == collector.RAW_FILENAME


def test_collect_candidates_quarantines_truncated_trailing_jsonl_before_append(
    tmp_path: Path, monkeypatch: Any
) -> None:
    collector = _load_collector_module()
    output_dir = tmp_path / "raw"
    output_dir.mkdir()
    raw_path = output_dir / collector.RAW_FILENAME
    raw_path.write_text(
        json.dumps({"record_id": "one", "response_text": "already captured"})
        + "\n"
        + '{"record_id": "broken"',
        encoding="utf-8",
    )

    def fake_fetch(
        record: dict[str, Any],
        source_index: int,
        _config: Any,
        **_kwargs: Any,
    ) -> Any:
        receipt = {
            "record_id": record["record_id"],
            "source_index": source_index,
            "requested_url": record["representative_website"],
            "capture_status": "captured",
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": collector.RAW_FILENAME,
        }
        raw_row = {
            "record_id": record["record_id"],
            "source_index": source_index,
            "requested_url": record["representative_website"],
            "response_text": record["record_id"],
        }
        return collector.FetchOutcome(raw_row=raw_row, receipt=receipt)

    monkeypatch.setattr(collector, "fetch_candidate", fake_fetch)

    summary = collector.collect_candidates(
        [
            _record("one", "https://public.example/one"),
            _record("two", "https://public.example/two"),
        ],
        output_dir,
        config=collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
        workers=1,
    )

    repair_path = raw_path.with_name(f"{raw_path.name}{collector.REPAIR_FILENAME_SUFFIX}")
    raw_rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    repair_rows = [
        json.loads(line) for line in repair_path.read_text(encoding="utf-8").splitlines()
    ]
    assert summary["attempted"] == 1
    assert summary["synthesized_missing_receipts"] == 1
    assert [row["record_id"] for row in raw_rows] == ["one", "two"]
    assert repair_rows[0]["reason"] == "truncated_trailing_jsonl_line"
    assert repair_rows[0]["raw_fragment"] == '{"record_id": "broken"'


def test_append_json_line_writes_row_atomically() -> None:
    collector = _load_collector_module()

    class RecordingHandle:
        def __init__(self) -> None:
            self.writes: list[str] = []
            self.flushed = False

        def write(self, value: str) -> None:
            self.writes.append(value)

        def flush(self) -> None:
            self.flushed = True

    handle = RecordingHandle()

    collector._append_json_line(handle, {"record_id": "one"})

    assert handle.writes == ['{"record_id": "one"}\n']
    assert handle.flushed is True


def test_collect_candidates_start_index_applies_before_limit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    collector = _load_collector_module()

    def fake_fetch(
        record: dict[str, Any],
        source_index: int,
        _config: Any,
        **_kwargs: Any,
    ) -> Any:
        receipt = {
            "record_id": record["record_id"],
            "source_index": source_index,
            "requested_url": record["representative_website"],
            "capture_status": "captured",
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": collector.RAW_FILENAME,
        }
        raw_row = {
            "record_id": record["record_id"],
            "source_index": source_index,
            "requested_url": record["representative_website"],
            "response_text": record["record_id"],
        }
        return collector.FetchOutcome(raw_row=raw_row, receipt=receipt)

    monkeypatch.setattr(collector, "fetch_candidate", fake_fetch)
    records = [_record(f"record-{index}", f"https://public.example/{index}") for index in range(5)]

    summary = collector.collect_candidates(
        records,
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
        workers=1,
        limit=2,
        resume=False,
        start_index=2,
    )

    receipt_path = tmp_path / collector.RECEIPT_FILENAME
    receipts = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
    assert summary["attempted"] == 2
    assert [receipt["source_index"] for receipt in receipts] == [2, 3]
    assert [receipt["record_id"] for receipt in receipts] == ["record-2", "record-3"]


def test_parse_args_accepts_start_index() -> None:
    collector = _load_collector_module()

    args = collector._parse_args(
        [
            "--input",
            "in.jsonl",
            "--output-dir",
            "out",
            "--start-index",
            "1041",
            "--limit",
            "961",
            "--retry-rounds",
            "4",
            "--max-stored-bytes",
            "123456",
            "--allow-sandbox-egress-alias",
        ]
    )

    assert args.start_index == 1041
    assert args.limit == 961
    assert args.retry_rounds == 4
    assert args.max_stored_bytes == 123456
    assert args.allow_sandbox_egress_alias is True


def test_collect_candidates_worker_error_preserves_record_identity(
    tmp_path: Path, monkeypatch: Any
) -> None:
    collector = _load_collector_module()

    def broken_fetch(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(collector, "fetch_candidate", broken_fetch)

    summary = collector.collect_candidates(
        [_record("actual-id", "https://public.example/")],
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
        workers=1,
        resume=False,
    )

    receipt_path = tmp_path / collector.RECEIPT_FILENAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8").strip())
    assert summary["worker_error"] == 1
    assert receipt["record_id"] == "actual-id"
    assert receipt["requested_url"] == "https://public.example/"
    assert receipt["attempt_number"] == 1
    assert receipt["max_attempts"] == 5


def test_collect_candidates_retries_http_429_then_captures(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    calls: list[int] = []

    def fake_fetch(
        record: dict[str, Any],
        source_index: int,
        _config: Any,
        *,
        attempt_number: int,
        max_attempts: int,
    ) -> Any:
        calls.append(attempt_number)
        receipt = {
            "record_id": record["record_id"],
            "source_index": source_index,
            "requested_url": record["representative_website"],
            "capture_status": "http_error",
            "http_status": 429,
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": None,
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
        }
        if attempt_number == 1:
            return collector.FetchOutcome(raw_row=None, receipt=receipt)
        receipt = {**receipt, "capture_status": "captured", "raw_data_file": collector.RAW_FILENAME}
        raw_row = {
            "record_id": record["record_id"],
            "source_index": source_index,
            "requested_url": record["representative_website"],
            "captured_at": "2026-07-18T00:00:01Z",
            "response_text": "ok",
        }
        return collector.FetchOutcome(raw_row=raw_row, receipt=receipt)

    monkeypatch.setattr(collector, "fetch_candidate", fake_fetch)

    summary = collector.collect_candidates(
        [_record("retry", "https://public.example/retry")],
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
        workers=1,
        retry_rounds=4,
        resume=False,
    )

    receipts = [
        json.loads(line)
        for line in (tmp_path / collector.RECEIPT_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    assert calls == [1, 2]
    assert summary["attempted"] == 2
    assert summary["http_error"] == 1
    assert summary["retry_enqueued"] == 1
    assert summary["captured"] == 1
    assert [receipt["attempt_number"] for receipt in receipts] == [1, 2]
    assert [receipt["max_attempts"] for receipt in receipts] == [5, 5]


def test_collect_candidates_does_not_retry_terminal_http_404(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    calls = 0

    def fake_fetch(
        record: dict[str, Any],
        source_index: int,
        _config: Any,
        *,
        attempt_number: int,
        max_attempts: int,
    ) -> Any:
        nonlocal calls
        calls += 1
        receipt = {
            "record_id": record["record_id"],
            "source_index": source_index,
            "requested_url": record["representative_website"],
            "capture_status": "http_error",
            "http_status": 404,
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": None,
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
        }
        return collector.FetchOutcome(raw_row=None, receipt=receipt)

    monkeypatch.setattr(collector, "fetch_candidate", fake_fetch)

    summary = collector.collect_candidates(
        [_record("terminal", "https://public.example/missing")],
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
        workers=1,
        retry_rounds=4,
        resume=False,
    )

    assert calls == 1
    assert summary["attempted"] == 1
    assert summary["http_error"] == 1
    assert summary["retry_enqueued"] == 0


def test_collect_candidates_resumes_retryable_failure_below_threshold(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / collector.RECEIPT_FILENAME).write_text(
        json.dumps(
            {
                "record_id": "resume-retry",
                "source_index": 0,
                "requested_url": "https://public.example/retry",
                "capture_status": "network_error",
                "captured_at": "2026-07-18T00:00:00Z",
                "raw_data_file": None,
                "attempt_number": 1,
                "max_attempts": 5,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[int] = []

    def fake_fetch(
        record: dict[str, Any],
        source_index: int,
        _config: Any,
        *,
        attempt_number: int,
        max_attempts: int,
    ) -> Any:
        calls.append(attempt_number)
        receipt = {
            "record_id": record["record_id"],
            "source_index": source_index,
            "requested_url": record["representative_website"],
            "capture_status": "captured",
            "captured_at": "2026-07-18T00:00:01Z",
            "raw_data_file": collector.RAW_FILENAME,
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
        }
        raw_row = {
            "record_id": record["record_id"],
            "source_index": source_index,
            "requested_url": record["representative_website"],
            "captured_at": "2026-07-18T00:00:01Z",
            "response_text": "ok",
        }
        return collector.FetchOutcome(raw_row=raw_row, receipt=receipt)

    monkeypatch.setattr(collector, "fetch_candidate", fake_fetch)

    summary = collector.collect_candidates(
        [_record("resume-retry", "https://public.example/retry")],
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
        workers=1,
        retry_rounds=4,
    )

    assert calls == [2]
    assert summary["attempted"] == 1
    assert summary["captured"] == 1
    assert summary["skipped_existing_receipt"] == 0


def test_collect_candidates_skips_retryable_failure_after_threshold(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()
    tmp_path.mkdir(exist_ok=True)
    with (tmp_path / collector.RECEIPT_FILENAME).open("w", encoding="utf-8") as handle:
        for attempt in range(1, 6):
            handle.write(
                json.dumps(
                    {
                        "record_id": "exhausted",
                        "source_index": 0,
                        "requested_url": "https://public.example/exhausted",
                        "capture_status": "network_error",
                        "captured_at": "2026-07-18T00:00:00Z",
                        "raw_data_file": None,
                        "attempt_number": attempt,
                        "max_attempts": 5,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

    def fail_fetch(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("exhausted retryable failures must be skipped on resume")

    monkeypatch.setattr(collector, "fetch_candidate", fail_fetch)

    summary = collector.collect_candidates(
        [_record("exhausted", "https://public.example/exhausted")],
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
        workers=1,
        retry_rounds=4,
    )

    assert summary["attempted"] == 0
    assert summary["skipped_existing_receipt"] == 1


def test_collect_candidates_caps_raw_jsonl_bytes_per_run(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    collector = _load_collector_module()

    def fake_fetch(
        record: dict[str, Any],
        source_index: int,
        _config: Any,
        *,
        attempt_number: int,
        max_attempts: int,
    ) -> Any:
        receipt = {
            "record_id": record["record_id"],
            "source_index": source_index,
            "requested_url": record["representative_website"],
            "capture_status": "captured",
            "captured_at": "2026-07-18T00:00:00Z",
            "raw_data_file": collector.RAW_FILENAME,
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
        }
        raw_row = {
            "record_id": record["record_id"],
            "source_index": source_index,
            "requested_url": record["representative_website"],
            "captured_at": "2026-07-18T00:00:00Z",
            "response_text": record["record_id"],
        }
        return collector.FetchOutcome(raw_row=raw_row, receipt=receipt)

    monkeypatch.setattr(collector, "fetch_candidate", fake_fetch)
    first_record = _record("one", "https://public.example/one")
    first_outcome = fake_fetch(
        first_record,
        0,
        collector.CollectorConfig(),
        attempt_number=1,
        max_attempts=5,
    )
    first_line_bytes = len(f"{collector._json_line(first_outcome.raw_row)}\n".encode())

    summary = collector.collect_candidates(
        [first_record, _record("two", "https://public.example/two")],
        tmp_path,
        config=collector.CollectorConfig(timeout_seconds=2, max_bytes=1024),
        workers=1,
        resume=False,
        max_stored_bytes=first_line_bytes,
    )

    raw_rows = [
        json.loads(line)
        for line in (tmp_path / collector.RAW_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    receipts = [
        json.loads(line)
        for line in (tmp_path / collector.RECEIPT_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    assert summary["attempted"] == 2
    assert summary["captured"] == 1
    assert summary["storage_cap_reached"] == 1
    assert summary["run_stored_bytes"] == first_line_bytes
    assert summary["storage_remaining_bytes"] == 0
    assert [row["record_id"] for row in raw_rows] == ["one"]
    assert [receipt["capture_status"] for receipt in receipts] == [
        "captured",
        "storage_cap_reached",
    ]
