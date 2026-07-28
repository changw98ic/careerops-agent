#!/usr/bin/env python3
"""Fail-closed structural checks for the Agent-console OpenSpec change.

This validates the design artifacts only. It does not claim that the runtime
deliverables named by the plan have been implemented.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, SchemaError

EXPECTED_COUNTS = {"AO": 22, "AR": 20, "LL": 28, "CL": 36}
RELEASE_GATES = ("MODEL-02", "CRAWL-02", "AUTH-02", "DB-02", "AUDIT-02", "TEMP-02", "PRIV-02")
REQUIRED_ROUTES = (
    "GET /api/v1/agent-console/actions",
    "POST /api/v1/agent-console/actions/{action_key}/accept",
    "POST /api/v1/agent-console/actions/{action_key}/snooze",
    "POST /api/v1/agent-console/actions/{action_key}/dismiss",
    "POST /api/v1/agent-console/actions/{action_key}/complete",
    "POST /api/v1/agent-console/contexts",
    "POST /api/v1/agent-console/preflight",
    "POST /api/v1/agents/job-matching",
    "POST /api/v1/agents/resume-review",
    "POST /api/v1/agents/interview-preparation",
    "GET /api/v1/agents/runs/{run_id}/stages",
    "POST /api/v1/agents/runs/{run_id}/retry",
    "POST /api/v1/agents/runs/{run_id}/stop",
    "POST /api/v1/agents/runs/{run_id}/review",
    "GET /api/v1/smart-intake/previews",
    "POST /api/v1/smart-intake/previews",
    "POST /api/v1/smart-intake/previews/{preview_id}/apply",
    "GET /api/v1/crawl-plans",
    "POST /api/v1/crawl-plans/versions",
    "POST /api/v1/crawl-plans/versions/{version_id}/activate",
    "POST /api/v1/crawl-plans/pause",
    "POST /api/v1/crawl-plans/resume",
    "POST /api/v1/crawl-plans/run-now",
    "GET /api/v1/crawl-runs/{run_id}/stages",
    "POST /api/v1/crawl-runs/{run_id}/retry",
    "POST /api/v1/crawl-runs/{run_id}/stop",
    "GET /api/v1/candidates",
    "GET /api/v1/candidates/{candidate_id}/evidence",
    "POST /api/v1/evidence/import",
    "GET /api/v1/jobs/{job_id}/remote-eligibility",
    "GET /api/v1/jobs/{job_id}/compensation",
    "GET /api/v1/matches",
    "POST /api/v1/matches/run",
    "GET /api/v1/profile",
    "POST /api/v1/profile",
    "POST /api/v1/profile/versions/{version_id}/activate",
    "GET /api/v1/resumes",
    "POST /api/v1/resumes",
    "POST /api/v1/resumes/{version_id}/confirm",
    "GET /api/v1/evidence",
    "POST /api/v1/evidence/{evidence_id}/confirm",
    "POST /api/v1/evidence/{evidence_id}/reject",
    "GET /api/v1/companies",
    "GET /api/v1/jobs",
    "GET /api/v1/jobs/{job_id}",
    "GET /api/v1/inbox",
    "GET /api/v1/inbox/{job_id}",
    "POST /api/v1/inbox/{job_id}/favorite",
    "POST /api/v1/inbox/{job_id}/ignore",
    "POST /api/v1/inbox/{job_id}/snooze",
    "GET /api/v1/applications",
    "POST /api/v1/applications",
    "POST /api/v1/applications/{application_id}/package",
    "POST /api/v1/resume-versions",
    "POST /api/v1/application-packages",
    "POST /api/v1/follow-ups",
)


def fail(message: str) -> None:
    raise SystemExit(f"agent-console spec check failed: {message}")


def validate_schemas(docs: Path) -> None:
    for path in sorted(docs.glob("*.schema.json")):
        try:
            schema = json.loads(path.read_text())
            Draft202012Validator.check_schema(schema)
        except (OSError, json.JSONDecodeError, SchemaError) as exc:
            fail(f"invalid schema {path}: {exc}")

    scenario_schema = json.loads((docs / "scenario-evidence.schema.json").read_text())
    if scenario_schema.get("x-validation") != {
        "artifact_path_basename_without_extension_equals": "scenario_id",
        "artifact_path_root_must_equal_register_evidence_root": True,
        "screenshot_paths_required_for": ["UX-01", "A11Y-01"],
        "screenshot_paths_must_be_under": "artifacts/ego/",
    }:
        fail("scenario evidence x-validation contract drifted")

    release_schema = json.loads((docs / "release-gate-evidence.schema.json").read_text())
    if release_schema.get("x-validation") != {
        "artifact_path_commit_suffix_equals": "commit",
        "artifact_path_gate_prefix_equals": "gate",
    }:
        fail("release gate x-validation contract drifted")


def validate_register(change: Path) -> dict[str, dict[str, str]]:
    docs = change / "docs/agent-console"
    register_path = docs / "scenario-register.json"
    register = json.loads(register_path.read_text())
    scenarios = register.get("scenarios", [])
    if register.get("scenario_count") != 106 or len(scenarios) != 106:
        fail("scenario register must contain exactly 106 entries")

    ids = [entry.get("id") for entry in scenarios]
    if len(set(ids)) != 106 or any(not isinstance(value, str) for value in ids):
        fail("scenario IDs must be unique strings")

    by_prefix: dict[str, int] = {}
    for entry in scenarios:
        scenario_id = entry["id"]
        prefix = scenario_id.split("-", 1)[0]
        by_prefix[prefix] = by_prefix.get(prefix, 0) + 1
        expected_path = entry["evidence"]
        if Path(expected_path).name != f"{scenario_id}.json":
            fail(f"evidence basename does not match {scenario_id}")
        expected_root = (
            "artifacts/ego/"
            if entry["gate"] in {"UX-01", "A11Y-01"}
            else "artifacts/agent-console/"
        )
        if not expected_path.startswith(expected_root):
            fail(f"evidence root does not match gate for {scenario_id}")
        if entry["gate"] in {"UX-01", "A11Y-01"} and entry.get("viewports") != [
            "1440x900",
            "375x812",
        ]:
            fail(f"UI scenario must register both viewports: {scenario_id}")
        source = change / entry["source"]
        heading_count = len(re.findall(r"^#### Scenario:", source.read_text(), re.MULTILINE))
        if heading_count == 0:
            fail(f"no scenario headings in {source}")
    if by_prefix != EXPECTED_COUNTS:
        fail(f"scenario prefix counts differ: {by_prefix}")
    return {entry["id"]: entry for entry in scenarios}


def validate_present_evidence_records(change: Path, register: dict[str, dict[str, str]]) -> None:
    """Apply the dynamic path rules when CI artifacts are present."""
    scenario_schema = json.loads(
        (change / "docs/agent-console/scenario-evidence.schema.json").read_text()
    )
    release_schema = json.loads(
        (change / "docs/agent-console/release-gate-evidence.schema.json").read_text()
    )
    scenario_validator = Draft202012Validator(scenario_schema)
    release_validator = Draft202012Validator(release_schema)
    for path in sorted((change / "artifacts").glob("**/*.json")):
        record = json.loads(path.read_text())
        if "scenario_id" in record:
            scenario_validator.validate(record)
            expected = register.get(record["scenario_id"])
            if expected is None:
                fail(f"unknown scenario artifact: {path}")
            if record["artifact_path"] != expected["evidence"]:
                fail(f"scenario artifact path does not match register: {path}")
            if record.get("gate") != expected["gate"]:
                fail(f"scenario artifact gate does not match register: {path}")
            if path.relative_to(change).as_posix() != record["artifact_path"]:
                fail(f"scenario declared path does not match actual path: {path}")
        elif "gate" in record:
            release_validator.validate(record)
            expected_name = f"{record['gate']}-{record['commit']}.json"
            if path.name != expected_name or path.parent != change / "artifacts/agent-console":
                fail(f"release artifact path does not bind gate/commit: {path}")


def validate_required_artifacts(
    change: Path, register: dict[str, dict[str, str]], commit: str
) -> None:
    """Require the full evidence set in a release/CI invocation."""
    for scenario_id, expected in register.items():
        path = change / expected["evidence"]
        if not path.is_file():
            fail(f"missing scenario evidence artifact: {path}")
        record = json.loads(path.read_text())
        if record.get("scenario_id") != scenario_id:
            fail(f"scenario artifact identity mismatch: {path}")
        if record.get("artifact_path") != expected["evidence"]:
            fail(f"scenario artifact declared path mismatch: {path}")
        if record.get("gate") != expected["gate"]:
            fail(f"scenario artifact gate mismatch: {path}")
        if path.relative_to(change).as_posix() != record["artifact_path"]:
            fail(f"scenario artifact actual path mismatch: {path}")
        if record.get("commit") != commit:
            fail(f"scenario artifact commit mismatch: {path}")
        for screenshot in record.get("screenshot_paths", []):
            screenshot_path = change / screenshot
            if not screenshot_path.is_file():
                fail(f"missing screenshot artifact: {screenshot_path}")

    release_root = change / "artifacts/agent-console"
    for gate in RELEASE_GATES:
        path = release_root / f"{gate}-{commit}.json"
        if not path.is_file():
            fail(f"missing release-gate evidence artifact: {path}")
        record = json.loads(path.read_text())
        if record.get("gate") != gate or record.get("commit") != commit:
            fail(f"release-gate artifact identity mismatch: {path}")
        expected_path = path.relative_to(change).as_posix()
        if record.get("artifact_path") != expected_path:
            fail(f"release-gate declared path mismatch: {path}")
        if record.get("passed") is not True or record.get("score", 0) < 95:
            fail(f"release-gate did not pass at score >=95: {path}")


def validate_routes(change: Path) -> None:
    docs = change / "docs/agent-console"
    api = (docs / "api-contract.md").read_text()
    auth = (docs / "authorization-matrix.md").read_text()
    for route in REQUIRED_ROUTES:
        if route not in api:
            fail(f"route missing from api-contract.md: {route}")
        if route not in auth:
            fail(f"route missing from authorization-matrix.md: {route}")
    for token in ("{id}", "{key}", "{operation}"):
        if token in api or token in auth:
            fail(f"non-canonical route placeholder remains: {token}")
    route_pattern = re.compile(r"\b(GET|POST|PATCH|PUT|DELETE)\s+(/api/v1/[^\s`|,;)]*)")

    def route_set(text: str) -> set[str]:
        return {f"{method} {path.split('?', 1)[0]}" for method, path in route_pattern.findall(text)}

    api_routes = route_set(api)
    auth_routes = route_set(auth)
    # The API annex mentions this route only in the explicit denial sentence;
    # it must not be treated as an allowed route-set member.
    api_routes.discard("POST /api/v1/crawl-plans")
    if api_routes != auth_routes:
        fail(
            "API/authorization route-set drift: "
            f"api-only={sorted(api_routes - auth_routes)}, "
            f"auth-only={sorted(auth_routes - api_routes)}"
        )
    root_mutation = re.compile(r"^\|\s*`POST /api/v1/crawl-plans`(?:\||\s)", re.MULTILINE)
    if root_mutation.search(api) or root_mutation.search(auth):
        fail("forbidden root POST /api/v1/crawl-plans appears as an allowed route")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--change", type=Path, required=True)
    parser.add_argument("--require-artifacts", action="store_true")
    parser.add_argument("--commit", help="release commit required with --require-artifacts")
    args = parser.parse_args()
    change = args.change.resolve()
    if not (change / "docs/agent-console").is_dir():
        fail(f"not an Agent-console change: {change}")
    validate_schemas(change / "docs/agent-console")
    register = validate_register(change)
    validate_present_evidence_records(change, register)
    if args.require_artifacts:
        if not args.commit or not re.fullmatch(r"[0-9a-f]{7,64}", args.commit):
            fail("--require-artifacts needs a lowercase hex --commit")
        validate_required_artifacts(change, register, args.commit)
    validate_routes(change)
    print("agent-console spec/register/schema/route checks: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
