from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts/evaluate_bounded_autopilot_rollout.py"


def _load_evaluator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("evaluate_bounded_autopilot_rollout", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evaluator = _load_evaluator()
MANIFEST = REPOSITORY_ROOT / evaluator.DEFAULT_MANIFEST


def test_versioned_rollout_fixture_proves_fifty_no_write_observations() -> None:
    result = evaluator.evaluate_manifest(MANIFEST)
    summary = result["summary"]

    assert isinstance(summary, dict)
    assert summary["total_observations"] == 50
    assert summary["shadow_observations"] == 25
    assert summary["review_required_observations"] == 25
    assert summary["false_positive"] == 0
    assert summary["false_negative"] == 0
    assert summary["autonomous_provider_write_attempts"] == 0
    assert summary["no_autonomous_writes"] is True
    assert result["external_provider_calls"] == 0
    assert result["supports_real_provider_qualification"] is False
    assert result["scenario_categories"] == [
        "ambiguous_policy",
        "cap_boundary",
        "duplicate_like",
        "expired_grant",
        "hard_stop",
        "kill_switch",
        "payload_drift",
        "revoked_grant",
        "stale_evidence",
        "would_allow",
    ]
    assert len(str(result["manifest_sha256"])) == 64


def test_rollout_fixture_rejects_any_external_provider_call(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["external_provider_calls"] = 1
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(evaluator.RolloutEvaluationError, match="provider calls"):
        evaluator.evaluate_manifest(path)


def test_rollout_fixture_rejects_fail_open_shadow_decision(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["cases"][0]["actual_decision"] = "allow_write"
    path = tmp_path / "fail-open.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(evaluator.RolloutEvaluationError, match="autonomous write"):
        evaluator.evaluate_manifest(path)


def test_rollout_fixture_rejects_missing_mandatory_scenario_category(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["cases"][0]["category"] = "hard_stop"
    path = tmp_path / "missing-category.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(evaluator.RolloutEvaluationError, match="mandatory scenario category"):
        evaluator.evaluate_manifest(path)


def test_rollout_fixture_rejects_manifest_controlled_lower_minimum(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["minimum_observations"] = 10
    path = tmp_path / "lowered-minimum.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(evaluator.RolloutEvaluationError, match="evaluator contract"):
        evaluator.evaluate_manifest(path)


def test_rollout_fixture_rejects_more_than_fifty_observations(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["cases"][0]["repeat"] = 6
    path = tmp_path / "extra-observation.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(evaluator.RolloutEvaluationError, match="exactly 50 observations"):
        evaluator.evaluate_manifest(path)
