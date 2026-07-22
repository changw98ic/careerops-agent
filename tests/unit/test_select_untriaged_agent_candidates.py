from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_selector_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "select_untriaged_agent_candidates.py"
    spec = importlib.util.spec_from_file_location("select_untriaged_agent_candidates", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _candidate(record_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "record_id": record_id,
        "candidate_entity_name": record_id,
        "normalized_name": record_id,
        "registrable_domain": f"{record_id}.example",
        "representative_website": f"https://{record_id}.example/",
        "candidate_type": "agent_ecosystem_company_candidate",
        "candidate_state": "unverified",
        "website_fetch_status": "not_fetched",
        "company_terms_status": "unknown",
        "source_usage_status": "publisher_declared_metadata_seed",
        "source_evidence": [{"source_id": "test"}],
        "personal_data_included": False,
        "collection_boundary": "metadata only",
        "captured_at": "2026-07-18T00:00:00Z",
    }
    row.update(overrides)
    return row


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_select_untriaged_candidates_preserves_candidate_rows_and_order(tmp_path: Path) -> None:
    selector = _load_selector_module()
    candidates = tmp_path / "candidates.jsonl"
    triage = tmp_path / "triage.jsonl"
    kept_a = _candidate("a")
    triaged_b = _candidate("b")
    kept_c = _candidate("c")
    _write_jsonl(candidates, [kept_a, triaged_b, kept_c])
    _write_jsonl(triage, [{"record_id": "b"}, {"record_id": "external"}])

    rows, summary = selector.select_untriaged_candidates(candidates, triage)

    assert rows == [kept_a, kept_c]
    assert summary["candidate_rows_read"] == 3
    assert summary["triage_unique_record_ids"] == 2
    assert summary["triaged_candidate_record_ids"] == 1
    assert summary["untriaged_candidate_rows"] == 2
    assert summary["triage_record_ids_not_in_candidates"] == 1
    assert summary["triage_record_ids_not_in_candidates_examples"] == ["external"]


def test_write_untriaged_candidates_emits_jsonl_and_stdout_summary_fields(tmp_path: Path) -> None:
    selector = _load_selector_module()
    candidates = tmp_path / "candidates.jsonl"
    triage = tmp_path / "triage.jsonl"
    output = tmp_path / "out" / "untriaged.jsonl"
    row = _candidate("a")
    _write_jsonl(candidates, [row])
    _write_jsonl(triage, [])

    summary = selector.write_untriaged_candidates(candidates, triage, output)

    assert _read_jsonl(output) == [row]
    assert summary["output_jsonl"] == str(output)
    assert summary["untriaged_candidate_rows"] == 1


def test_select_untriaged_candidates_rejects_missing_candidate_metadata(tmp_path: Path) -> None:
    selector = _load_selector_module()
    candidates = tmp_path / "candidates.jsonl"
    triage = tmp_path / "triage.jsonl"
    bad = _candidate("a")
    del bad["source_evidence"]
    _write_jsonl(candidates, [bad])
    _write_jsonl(triage, [])

    try:
        selector.select_untriaged_candidates(candidates, triage)
    except ValueError as error:
        assert "missing required field(s): source_evidence" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_select_untriaged_candidates_rejects_duplicate_candidate_ids(tmp_path: Path) -> None:
    selector = _load_selector_module()
    candidates = tmp_path / "candidates.jsonl"
    triage = tmp_path / "triage.jsonl"
    _write_jsonl(candidates, [_candidate("a"), _candidate("a")])
    _write_jsonl(triage, [])

    try:
        selector.select_untriaged_candidates(candidates, triage)
    except ValueError as error:
        assert "duplicate candidate record_id a" in str(error)
    else:
        raise AssertionError("expected ValueError")
