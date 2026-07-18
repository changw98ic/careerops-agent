from __future__ import annotations

import hashlib
import importlib.util
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

Fixture = tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]


def _load_schema_fixture_module() -> ModuleType:
    fixture_path = Path(__file__).with_name("test_verify_m1_schema_enforcement.py")
    spec = importlib.util.spec_from_file_location("verify_m1_schema_fixture", fixture_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_artifact(root: Path) -> list[dict[str, Any]]:
    artifact_path = root / "datasets/artifacts/contact-v1.jsonl"
    return [json.loads(line) for line in artifact_path.read_text(encoding="utf-8").splitlines()]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_evidence(
    fixture: Fixture,
    rows: list[dict[str, Any]],
) -> None:
    root, pilot, manifest, scan_report = fixture
    artifact_path = root / "datasets/artifacts/contact-v1.jsonl"
    artifact_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    artifact_sha256 = _sha256(artifact_path)

    split_counts = Counter(cast(str, row["split"]) for row in rows)
    group_counts = Counter((cast(str, row["split"]), cast(str, row["group_id"])) for row in rows)
    manifest["artifact"].update(
        {
            "sha256": artifact_sha256,
            "row_count": len(rows),
            "real_count": sum(row["synthetic"] is False for row in rows),
            "synthetic_count": sum(row["synthetic"] is True for row in rows),
        }
    )
    manifest["splits"] = {
        split: split_counts[split] for split in ("development", "validation", "holdout")
    }
    manifest["groups"] = {
        "total_count": len({cast(str, row["group_id"]) for row in rows}),
        "split_counts": {
            split: sum(1 for row_split, _group_id in group_counts if row_split == split)
            for split in ("development", "validation", "holdout")
        },
    }
    pilot["datasets"][0]["pilot_actual"] = manifest["artifact"]["real_count"]

    scan_report["artifact_sha256"] = artifact_sha256
    scan_path = root / "datasets/scans/contact-v1.scan.json"
    _write_json(scan_path, scan_report)
    manifest["pii_scan"]["sha256"] = _sha256(scan_path)

    _write_json(root / "datasets/manifests/d0-pilot-plan.json", pilot)
    _write_json(root / "datasets/manifests/contact-v1.manifest.json", manifest)


def _allow_nested_provenance(root: Path) -> None:
    schema_path = root / "datasets/schemas/contact.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["source"]["properties"]["provenance"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["content_sha256", "source_url"],
        "properties": {
            "content_sha256": {
                "type": "string",
                "pattern": "^[a-f0-9]{64}$",
            },
            "source_url": {"type": "string", "format": "uri"},
        },
    }
    _write_json(schema_path, schema)


def _base_root(tmp_path: Path) -> Fixture:
    fixture_module = _load_schema_fixture_module()
    fixture_builder = cast(Callable[[Path], Fixture], fixture_module._base_root)
    fixture = fixture_builder(tmp_path)
    root, _pilot, _manifest, _scan_report = fixture

    label_schema = Path(__file__).parents[2] / "datasets/schemas/d0_label_review.schema.json"
    label_schema_target = root / "datasets/schemas/d0_label_review.schema.json"
    if label_schema.is_file() and not label_schema_target.exists():
        label_schema_target.write_text(label_schema.read_text(encoding="utf-8"), encoding="utf-8")

    _allow_nested_provenance(root)
    rows = _read_artifact(root)
    for row in rows:
        sample_id = cast(str, row["sample_id"])
        row["synthetic"] = False
        row["source"]["content_sha256"] = hashlib.sha256(sample_id.encode()).hexdigest()
        row["candidate"]["address"] = f"redacted-contact-token-{sample_id}"
    _write_evidence(fixture, rows)
    return fixture


def _run_full(root: Path) -> tuple[list[str], dict[str, Any]]:
    fixture_module = _load_schema_fixture_module()
    runner = cast(
        Callable[[Path], tuple[list[str], dict[str, Any]]],
        fixture_module._run_full,
    )
    return runner(root)


def _add_collision(rows: list[dict[str, Any]], *, same_split: bool) -> tuple[str, str]:
    content_sha256 = "d15ea5ed" * 8
    source_url = "https://private.example.test/provenance/do-not-log-this-value"
    if same_split:
        rows[1]["split"] = rows[0]["split"]
    for row in rows[:2]:
        row["source"]["provenance"] = {
            "content_sha256": content_sha256,
            "source_url": source_url,
        }
    return content_sha256, source_url


def test_full_verifier_rejects_synthetic_row_outside_development(tmp_path: Path) -> None:
    fixture = _base_root(tmp_path)
    root = fixture[0]
    rows = _read_artifact(root)
    nondevelopment_row = next(row for row in rows if row["split"] != "development")
    nondevelopment_row["synthetic"] = True
    _write_evidence(fixture, rows)

    errors, _details = _run_full(root)

    assert any("synthetic rows are only allowed in development" in error for error in errors), (
        errors
    )


def test_full_verifier_rejects_nested_provenance_collisions_across_splits(
    tmp_path: Path,
) -> None:
    fixture = _base_root(tmp_path)
    root = fixture[0]
    rows = _read_artifact(root)
    _add_collision(rows, same_split=False)
    _write_evidence(fixture, rows)

    errors, _details = _run_full(root)
    rendered = "\n".join(errors)

    assert "content_sha256" in rendered, errors
    assert "source_url" in rendered, errors
    assert "development" in rendered, errors
    assert "holdout" in rendered, errors


def test_full_verifier_allows_same_split_provenance_reuse(tmp_path: Path) -> None:
    fixture = _base_root(tmp_path)
    root = fixture[0]
    rows = _read_artifact(root)
    _add_collision(rows, same_split=True)
    _write_evidence(fixture, rows)

    errors, details = _run_full(root)

    leakage_errors = [
        error
        for error in errors
        if "leakage" in error or "content_sha256" in error or "source_url" in error
    ]
    assert leakage_errors == []
    assert details["pilot_rows_actual"] == 3


def test_leakage_errors_redact_colliding_provenance_values(tmp_path: Path) -> None:
    fixture = _base_root(tmp_path)
    root = fixture[0]
    rows = _read_artifact(root)
    content_sha256, source_url = _add_collision(rows, same_split=False)
    _write_evidence(fixture, rows)

    errors, _details = _run_full(root)
    rendered = "\n".join(errors)

    assert errors
    assert content_sha256 not in rendered
    assert source_url not in rendered
