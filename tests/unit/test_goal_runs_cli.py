from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from careerops.api.goal_runs import (
    GoalRunStatusResponse,
    GoalRunSummary,
    GoalRunUnavailable,
    ListGoalRunsResponse,
)
from careerops.cli import goal_runs

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000b01")
GOAL_RUN_ID = UUID("00000000-0000-0000-0000-000000000b02")
REGISTRY_ID = UUID("00000000-0000-0000-0000-000000000b03")


class RecordingReadOnlyProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def list_goal_runs(self, *, limit: int) -> ListGoalRunsResponse:
        self.calls.append(("list", limit))
        return ListGoalRunsResponse(goal_runs=(_summary(),))

    async def goal_run_status(self, *, goal_run_id: UUID) -> GoalRunStatusResponse:
        self.calls.append(("status", goal_run_id))
        return GoalRunStatusResponse(goal_run=_summary(goal_run_id=goal_run_id))


class UnavailableReadOnlyProvider(RecordingReadOnlyProvider):
    async def list_goal_runs(self, *, limit: int) -> ListGoalRunsResponse:
        self.calls.append(("list", limit))
        raise GoalRunUnavailable("temporal unavailable")


def test_goal_runs_cli_is_read_only_and_outputs_json(capsys: pytest.CaptureFixture[str]) -> None:
    provider = RecordingReadOnlyProvider()

    list_returncode = goal_runs.main(["list", "--limit", "5", "--json"], provider=provider)
    list_payload = json.loads(capsys.readouterr().out)
    status_returncode = goal_runs.main(
        ["status", "--goal-run-id", str(GOAL_RUN_ID), "--json"],
        provider=provider,
    )
    status_payload = json.loads(capsys.readouterr().out)

    assert list_returncode == 0
    assert status_returncode == 0
    assert list_payload["goal_runs"][0]["goal_run_id"] == str(GOAL_RUN_ID)
    assert status_payload["goal_run"]["goal_run_id"] == str(GOAL_RUN_ID)
    assert provider.calls == [("list", 5), ("status", GOAL_RUN_ID)]


def test_goal_runs_cli_exposes_no_mutating_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    provider = RecordingReadOnlyProvider()

    with pytest.raises(SystemExit) as exc_info:
        goal_runs.main(["approve", "--goal-run-id", str(GOAL_RUN_ID)], provider=provider)

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert provider.calls == []


def test_goal_runs_cli_rejects_unbounded_limit_before_provider_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = RecordingReadOnlyProvider()

    returncode = goal_runs.main(["list", "--limit", "0"], provider=provider)

    assert returncode == 1
    assert "--limit must be between 1 and 500" in capsys.readouterr().err
    assert provider.calls == []


def test_goal_runs_cli_maps_unavailable_to_error(capsys: pytest.CaptureFixture[str]) -> None:
    provider = UnavailableReadOnlyProvider()

    returncode = goal_runs.main(["list", "--json"], provider=provider)
    captured = capsys.readouterr()

    assert returncode == 1
    assert json.loads(captured.err) == {"errors": ["temporal unavailable"], "ok": False}
    assert provider.calls == [("list", 100)]


def _summary(goal_run_id: UUID = GOAL_RUN_ID) -> GoalRunSummary:
    return GoalRunSummary(
        goal_run_id=goal_run_id,
        owner_user_id=ACTOR_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        status="waiting_review",
        phase="review",
        created_at=NOW,
        updated_at=NOW,
        status_message="waiting for operator review",
        pending_review_id=UUID("00000000-0000-0000-0000-000000000b04"),
    )
