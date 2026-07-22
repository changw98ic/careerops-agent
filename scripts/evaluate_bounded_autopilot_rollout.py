#!/usr/bin/env python3
"""Evaluate the versioned synthetic shadow/review rollout fixture.

This command never calls a provider.  Its output is reproducible sandbox evidence and must not
be promoted to a real provider Release Qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from careerops.application.release_qualification import (
    AutopilotReleaseStage,
    RolloutActualDecision,
    RolloutExpectedDecision,
    RolloutObservation,
    summarize_rollout_observations,
)

DEFAULT_MANIFEST = Path("datasets/manifests/bounded-autopilot-rollout-evaluation.v1.json")
REQUIRED_OBSERVATIONS = 50
_EXPECTED_ROOT_KEYS = {
    "cases",
    "external_provider_calls",
    "kind",
    "minimum_observations",
    "scope",
    "version",
}
_EXPECTED_CASE_KEYS = {
    "actual_decision",
    "category",
    "expected_decision",
    "hard_stop_present",
    "human_override_required",
    "name",
    "repeat",
    "stage",
}
_REQUIRED_SCENARIO_CATEGORIES = frozenset(
    {
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
    }
)


class RolloutEvaluationError(ValueError):
    """Raised when rollout evidence is malformed or cannot prove the sandbox boundary."""


def evaluate_manifest(path: Path) -> dict[str, object]:
    raw_bytes = path.read_bytes()
    try:
        document = json.loads(raw_bytes)
    except json.JSONDecodeError as error:
        raise RolloutEvaluationError("rollout manifest must be valid JSON") from error
    root = _mapping(document, "rollout manifest")
    _require_exact_keys(root, _EXPECTED_ROOT_KEYS, "rollout manifest")
    if root["kind"] != "synthetic_rollout_evaluation" or root["version"] != 1:
        raise RolloutEvaluationError("unsupported rollout manifest kind or version")
    if root["scope"] != "synthetic_only_no_real_provider_evidence":
        raise RolloutEvaluationError("rollout manifest must remain explicitly synthetic-only")
    if root["external_provider_calls"] != 0:
        raise RolloutEvaluationError("synthetic rollout evidence cannot include provider calls")
    minimum = _positive_int(root["minimum_observations"], "minimum_observations")
    if minimum != REQUIRED_OBSERVATIONS:
        raise RolloutEvaluationError(
            f"minimum_observations must equal the evaluator contract ({REQUIRED_OBSERVATIONS})"
        )
    cases = root["cases"]
    if not isinstance(cases, list) or not cases:
        raise RolloutEvaluationError("rollout manifest cases must be a non-empty array")

    observations: list[RolloutObservation] = []
    scenario_categories: set[str] = set()
    for raw_case in cases:
        item = _mapping(raw_case, "rollout case")
        _require_exact_keys(item, _EXPECTED_CASE_KEYS, "rollout case")
        repeat = _positive_int(item["repeat"], "case repeat")
        name = _bounded_identifier(item["name"], "case name")
        scenario_categories.add(_scenario_category(item["category"]))
        stage = _stage(item["stage"])
        actual = _actual_decision(item["actual_decision"])
        expected = _expected_decision(item["expected_decision"])
        hard_stop = _bool(item["hard_stop_present"], "hard_stop_present")
        override = _bool(item["human_override_required"], "human_override_required")
        for ordinal in range(1, repeat + 1):
            observations.append(
                RolloutObservation(
                    observation_id=f"{name}-{ordinal:02d}",
                    stage=stage,
                    expected_decision=expected,
                    actual_decision=actual,
                    human_override_required=override,
                    hard_stop_present=hard_stop,
                    autonomous_provider_write_attempted=False,
                )
            )

    if scenario_categories != _REQUIRED_SCENARIO_CATEGORIES:
        raise RolloutEvaluationError("rollout manifest is missing a mandatory scenario category")
    summary = summarize_rollout_observations(observations)
    if summary.total_observations != REQUIRED_OBSERVATIONS:
        raise RolloutEvaluationError(
            f"rollout manifest must contain exactly {REQUIRED_OBSERVATIONS} observations"
        )
    if not summary.no_autonomous_writes:
        raise RolloutEvaluationError("shadow/review evidence contains an autonomous write")
    return {
        "evidence_kind": "synthetic_rollout_evaluation.v1",
        "manifest_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "scope": root["scope"],
        "external_provider_calls": 0,
        "scenario_categories": sorted(scenario_categories),
        "summary": asdict(summary),
        "supports_real_provider_qualification": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = evaluate_manifest(args.manifest)
    except (OSError, RolloutEvaluationError, ValueError) as error:
        print(json.dumps({"error": str(error), "ok": False}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RolloutEvaluationError(f"{label} must be an object with string keys")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RolloutEvaluationError(f"{label} has missing or unknown fields")


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 1_000:
        raise RolloutEvaluationError(f"{label} must be an integer from 1 through 1000")
    return value


def _bounded_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 120:
        raise RolloutEvaluationError(f"{label} must be a bounded identifier")
    if not all(character.isalnum() or character in "-_." for character in value):
        raise RolloutEvaluationError(f"{label} contains unsupported characters")
    return value


def _scenario_category(value: object) -> str:
    category = _bounded_identifier(value, "case category")
    if category not in _REQUIRED_SCENARIO_CATEGORIES:
        raise RolloutEvaluationError("rollout case category is not in the mandatory vocabulary")
    return category


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise RolloutEvaluationError(f"{label} must be boolean")
    return value


def _stage(value: object) -> AutopilotReleaseStage:
    try:
        stage = AutopilotReleaseStage(value)
    except (TypeError, ValueError) as error:
        raise RolloutEvaluationError("rollout case stage is invalid") from error
    if stage not in {AutopilotReleaseStage.SHADOW, AutopilotReleaseStage.REVIEW_REQUIRED}:
        raise RolloutEvaluationError("rollout case stage must be shadow or review_required")
    return stage


def _actual_decision(value: object) -> RolloutActualDecision:
    try:
        return RolloutActualDecision(value)
    except (TypeError, ValueError) as error:
        raise RolloutEvaluationError("rollout case actual_decision is invalid") from error


def _expected_decision(value: object) -> RolloutExpectedDecision:
    try:
        return RolloutExpectedDecision(value)
    except (TypeError, ValueError) as error:
        raise RolloutEvaluationError("rollout case expected_decision is invalid") from error


if __name__ == "__main__":
    raise SystemExit(main())
