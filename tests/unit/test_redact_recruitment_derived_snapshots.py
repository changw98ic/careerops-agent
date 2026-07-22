from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_redactor_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "redact_recruitment_derived_snapshots.py"
    spec = importlib.util.spec_from_file_location("redact_recruitment_derived_snapshots", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_redacts_response_text_and_credentials_without_leaking_tokens(tmp_path: Path) -> None:
    redactor = _load_redactor_module()
    secret = "AK" + "IA1234567890ABCDEF"
    publishable = "pk" + "_live_" + "A" * 24
    source = tmp_path / "snapshot.jsonl"
    output = tmp_path / "snapshot.redacted.jsonl"
    summary_path = tmp_path / "snapshot.redacted.summary.json"
    source.write_text(
        json.dumps(
            {
                "links": [{"href": f"https://example.com/?token={publishable}"}],
                "metadata": {"nested": {"response_text": f"body contains {secret}"}},
                "response_text": f"top-level body contains {publishable}",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    summary = redactor.redact_jsonl_file(
        input_path=source,
        output_path=output,
        summary_path=summary_path,
    )

    rows = _jsonl(output)
    assert len(rows) == 1
    assert "response_text" not in rows[0]
    assert "response_text" not in rows[0]["metadata"]["nested"]  # type: ignore[index]
    serialized = output.read_text(encoding="utf-8")
    assert secret not in serialized
    assert publishable not in serialized
    assert "[REDACTED_CREDENTIAL:stripe_live_key:prefix=pk_live:length=32]" in serialized
    assert "AKIA" not in serialized
    assert summary["response_text_keys_removed"] == 2
    assert summary["credential_replacements"] == {"stripe_live_key": 1}
    saved_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert saved_summary["input_records"] == 1


def test_redacts_google_and_fine_grained_github_tokens(tmp_path: Path) -> None:
    redactor = _load_redactor_module()
    google_api_key = "AIza" + "A" * 35
    github_token = "github_pat_" + "b" * 22
    source = tmp_path / "snapshot.jsonl"
    output = tmp_path / "snapshot.redacted.jsonl"
    source.write_text(
        json.dumps(
            {
                "metadata": {
                    "google_key": google_api_key,
                    "github_token": github_token,
                },
                "response_text": google_api_key,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = redactor.redact_jsonl_file(input_path=source, output_path=output)

    serialized = output.read_text(encoding="utf-8")
    assert google_api_key not in serialized
    assert github_token not in serialized
    assert "response_text" not in serialized
    assert summary["credential_replacements"] == {
        "github_fine_grained_token": 1,
        "google_api_key": 1,
    }


def test_redaction_validation_rejects_residual_response_text() -> None:
    redactor = _load_redactor_module()

    with pytest.raises(ValueError, match="response_text key"):
        redactor._require_valid_redaction({"response_text": "unexpected"})


def test_rejects_overwriting_existing_output(tmp_path: Path) -> None:
    redactor = _load_redactor_module()
    source = tmp_path / "snapshot.jsonl"
    output = tmp_path / "existing.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    output.write_text("existing\n", encoding="utf-8")

    with pytest.raises(ValueError, match="output path already exists"):
        redactor.redact_jsonl_file(input_path=source, output_path=output)

    assert output.read_text(encoding="utf-8") == "existing\n"


def test_redacts_audited_snapshots_into_new_output_tree(tmp_path: Path) -> None:
    redactor = _load_redactor_module()
    snapshot_root = tmp_path / "inputs"
    first = (
        snapshot_root / "priority_sharded_v1_filter_v1" / "priority_sharded_v1_raw_snapshot.jsonl"
    )
    second = (
        snapshot_root / "priority_sharded_v2_filter_v1" / "priority_sharded_v1_raw_snapshot.jsonl"
    )
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(json.dumps({"response_text": "one"}) + "\n", encoding="utf-8")
    second.write_text(json.dumps({"safe": "two"}) + "\n", encoding="utf-8")

    aggregate = redactor.redact_audited_snapshots(
        output_dir=tmp_path / "outputs",
        snapshot_paths=[first, second],
    )

    assert aggregate["snapshot_count"] == 2
    first_output = (
        tmp_path
        / "outputs"
        / "priority_sharded_v1_filter_v1"
        / "priority_sharded_v1_raw_snapshot.redacted.jsonl"
    )
    second_output = (
        tmp_path
        / "outputs"
        / "priority_sharded_v2_filter_v1"
        / "priority_sharded_v1_raw_snapshot.redacted.jsonl"
    )
    assert _jsonl(first_output) == [{}]
    assert _jsonl(second_output) == [{"safe": "two"}]
    assert (tmp_path / "outputs" / "redaction_summary.json").exists()
