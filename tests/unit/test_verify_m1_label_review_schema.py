from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).parents[2]
SCHEMA_PATH = ROOT / "datasets" / "schemas" / "d0_label_review.schema.json"


def _load_verify_m1_module() -> ModuleType:
    script = ROOT / "scripts" / "verify_m1.py"
    spec = importlib.util.spec_from_file_location("verify_m1_label_schema", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema() -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(schema, dict)
    return schema


def _row(
    *,
    primary_value: Any = None,
    secondary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "sample_id": "sample-1",
        "primary": {
            "annotator_id": "primary-reviewer",
            "label": "review",
            "value": primary_value,
        },
        "adjudication_status": "not_required",
    }
    if secondary is not None:
        row["secondary"] = secondary
    return row


def _validate(value: Any) -> list[str]:
    verifier = _load_verify_m1_module()
    errors: list[str] = []
    verifier.validate_json_schema(value, _schema(), errors, "label review")
    return errors


def _validate_row(value: Any) -> list[str]:
    verifier = _load_verify_m1_module()
    schema = _schema()
    row_schema = schema["$defs"]["row"]
    errors: list[str] = []
    verifier.validate_json_schema(
        value,
        row_schema,
        errors,
        "label review row",
        root_schema=schema,
    )
    return errors


def test_real_schema_accepts_valid_envelope() -> None:
    payload = {
        "rows": [_row(primary_value={"accepted": True})],
        "metrics": {"ignored_by_schema": True},
    }

    assert _validate(payload) == []


def test_real_schema_accepts_valid_jsonl_rows() -> None:
    text = "\n".join(
        json.dumps(row)
        for row in (
            _row(primary_value={"rank": 1}),
            {
                **_row(primary_value=["structured", 2]),
                "sample_id": "sample-2",
            },
        )
    )

    errors = [error for line in text.splitlines() for error in _validate_row(json.loads(line))]

    assert errors == []


def test_real_schema_rejects_object_without_rows_envelope() -> None:
    errors = _validate({"not_rows": []})

    assert errors
    assert any("exactly one schema branch" in error for error in errors)


def test_real_schema_rejects_row_without_primary() -> None:
    row = _row()
    del row["primary"]

    errors = _validate_row(row)

    assert errors
    assert any("exactly one schema branch" in error for error in errors)


def test_real_schema_rejects_secondary_without_value() -> None:
    row = _row(
        secondary={
            "annotator_id": "secondary-reviewer",
            "label": "review",
        }
    )

    errors = _validate_row(row)

    assert errors
    assert any("exactly one schema branch" in error for error in errors)


def test_real_schema_accepts_boolean_label_value() -> None:
    row = _row(
        primary_value=False,
        secondary={
            "annotator_id": "secondary-reviewer",
            "label": "review",
            "value": True,
        },
    )

    assert _validate([row]) == []
