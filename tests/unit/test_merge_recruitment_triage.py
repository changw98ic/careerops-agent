from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_merge_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "merge_recruitment_triage.py"
    spec = importlib.util.spec_from_file_location("merge_recruitment_triage", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(record_id: str, score: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "record_id": record_id,
        "candidate_entity_name": record_id,
        "classification": "no_recruitment_evidence",
        "employer_hiring_assessment": "no_recruitment_evidence",
        "score": score,
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


def test_merge_preserves_input_order_for_unique_record_ids(tmp_path: Path) -> None:
    merger = _load_merge_module()
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _write_jsonl(first, [_row("a", 10), _row("b", 20)])
    _write_jsonl(second, [_row("c", 30)])

    rows, summary = merger.merge_triage_rows([first, second])

    assert [row["record_id"] for row in rows] == ["a", "b", "c"]
    assert summary["rows_read"] == 3
    assert summary["output_rows"] == 3
    assert summary["duplicate_rows"] == 0


def test_merge_replaces_duplicate_only_when_later_score_is_higher(tmp_path: Path) -> None:
    merger = _load_merge_module()
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _write_jsonl(
        first,
        [
            _row("a", 10, candidate_entity_name="low"),
            _row("b", 50, candidate_entity_name="kept"),
        ],
    )
    _write_jsonl(
        second,
        [
            _row("a", 80, candidate_entity_name="high"),
            _row("b", 40, candidate_entity_name="lower"),
        ],
    )

    rows, summary = merger.merge_triage_rows([first, second])

    assert [row["record_id"] for row in rows] == ["a", "b"]
    assert rows[0]["candidate_entity_name"] == "high"
    assert rows[1]["candidate_entity_name"] == "kept"
    assert summary["duplicate_rows"] == 2
    assert summary["duplicate_record_ids"] == 2
    assert summary["replacements_by_higher_score"] == 1
    assert summary["duplicate_rows_kept_by_existing_score"] == 1
    assert summary["duplicate_record_id_examples"] == ["a", "b"]


def test_write_merged_triage_emits_jsonl_and_summary(tmp_path: Path) -> None:
    merger = _load_merge_module()
    source = tmp_path / "source.jsonl"
    output = tmp_path / "out" / "merged.jsonl"
    summary_path = tmp_path / "out" / "summary.json"
    _write_jsonl(
        source,
        [
            _row(
                "a",
                100,
                classification="open_roles",
                employer_hiring_assessment="likely_self_hiring",
            )
        ],
    )

    summary = merger.write_merged_triage([source], output, summary_path)

    assert _read_jsonl(output) == [
        _row(
            "a",
            100,
            classification="open_roles",
            employer_hiring_assessment="likely_self_hiring",
        )
    ]
    persisted = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["output_jsonl"] == str(output)
    assert persisted["summary_json"] == str(summary_path)
    assert persisted["classification_counts"] == {"open_roles": 1}
    assert persisted["assessment_counts"] == {"likely_self_hiring": 1}
    assert persisted["selected_row_counts_by_source"] == {str(source): 1}


def test_merge_rejects_rows_without_record_id(tmp_path: Path) -> None:
    merger = _load_merge_module()
    bad = tmp_path / "bad.jsonl"
    _write_jsonl(bad, [{"score": 10}])

    try:
        merger.merge_triage_rows([bad])
    except ValueError as error:
        assert "expected a non-empty record_id" in str(error)
    else:
        raise AssertionError("expected ValueError")
