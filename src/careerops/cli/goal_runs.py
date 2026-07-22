from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from careerops.api.goal_runs import (
    GoalRunStatusResponse,
    GoalRunUnavailable,
    ListGoalRunsResponse,
)


class GoalRunReadOnlyProvider(Protocol):
    async def list_goal_runs(self, *, limit: int) -> ListGoalRunsResponse: ...

    async def goal_run_status(self, *, goal_run_id: UUID) -> GoalRunStatusResponse: ...


class DisabledGoalRunReadOnlyProvider:
    async def list_goal_runs(self, *, limit: int) -> ListGoalRunsResponse:
        del limit
        raise GoalRunUnavailable("goal run operator runtime is not configured")

    async def goal_run_status(self, *, goal_run_id: UUID) -> GoalRunStatusResponse:
        del goal_run_id
        raise GoalRunUnavailable("goal run operator runtime is not configured")


def create_runtime_goal_run_read_only_provider() -> GoalRunReadOnlyProvider:
    # Runtime storage/Temporal adapters are intentionally localized behind this factory.
    # Expected adapter interface: list_goal_runs(limit=...) and goal_run_status(goal_run_id=...).
    return DisabledGoalRunReadOnlyProvider()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read GoalRun operator status.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    list_runs = subcommands.add_parser("list")
    list_runs.add_argument("--limit", type=int, default=100)
    list_runs.add_argument("--json", action="store_true")

    status = subcommands.add_parser("status")
    status.add_argument("--goal-run-id", type=UUID, required=True)
    status.add_argument("--json", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    provider: GoalRunReadOnlyProvider | None = None,
) -> int:
    args = _parser().parse_args(argv)
    json_output = bool(getattr(args, "json", False))
    resolved_provider = provider or create_runtime_goal_run_read_only_provider()
    try:
        result = asyncio.run(_run(args, provider=resolved_provider))
    except (GoalRunUnavailable, ValueError) as exc:
        _emit_error(str(exc), json_output=json_output)
        return 1
    _emit(result, json_output=json_output)
    return 0


async def _run(
    args: argparse.Namespace,
    *,
    provider: GoalRunReadOnlyProvider,
) -> ListGoalRunsResponse | GoalRunStatusResponse:
    if args.command == "list":
        _require_limit(args.limit)
        return await provider.list_goal_runs(limit=args.limit)
    if args.command == "status":
        return await provider.goal_run_status(goal_run_id=args.goal_run_id)
    raise ValueError("unknown goal run command")


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("--limit must be between 1 and 500")


def _emit(result: ListGoalRunsResponse | GoalRunStatusResponse, *, json_output: bool) -> None:
    payload = result.model_dump(mode="json")
    if json_output:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    if payload["status"] == "listed":
        print(f"listed {len(payload['goal_runs'])} goal run(s)")
    elif payload["status"] == "found":
        print(f"goal run {payload['goal_run']['goal_run_id']} is {payload['goal_run']['status']}")


def _emit_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"errors": [message], "ok": False}, sort_keys=True), file=sys.stderr)
    else:
        print(f"ERROR: {message}", file=sys.stderr)


__all__: Sequence[str] = (
    "DisabledGoalRunReadOnlyProvider",
    "GoalRunReadOnlyProvider",
    "create_runtime_goal_run_read_only_provider",
    "main",
)


if __name__ == "__main__":
    raise SystemExit(main())
