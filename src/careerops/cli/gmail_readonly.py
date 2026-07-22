from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Protocol, cast

from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.gmail.worker import (
    GmailReadOnlyWorker,
    GmailWorkerRunResult,
    GmailWorkerStatus,
    create_runtime_gmail_worker,
    run_worker_forever,
)


class GmailReadOnlyWorkerPort(Protocol):
    def status(self) -> GmailWorkerStatus: ...

    def run_once(self, *, limit: int = 10, max_results: int = 100) -> GmailWorkerRunResult: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the read-only Gmail mailbox sync worker.")
    parser.add_argument(
        "--owner",
        default="gmail-readonly-worker",
        help="bounded mailbox worker owner identifier",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    status = subcommands.add_parser("status")
    status.add_argument("--json", action="store_true")

    run_once = subcommands.add_parser("run-once")
    run_once.add_argument("--limit", type=int, default=10)
    run_once.add_argument("--max-results", type=int, default=100)
    run_once.add_argument("--json", action="store_true")

    worker = subcommands.add_parser("worker")
    worker.add_argument("--limit", type=int, default=10)
    worker.add_argument("--max-results", type=int, default=100)
    worker.add_argument("--poll-seconds", type=float, default=5)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    worker: GmailReadOnlyWorkerPort | None = None,
) -> int:
    args = _parser().parse_args(argv)
    json_output = bool(getattr(args, "json", False))
    engine = None
    try:
        resolved_worker = worker
        if resolved_worker is None:
            settings = Settings.model_validate(
                {
                    "_env_file": None,
                    "database_role": DatabaseCapabilityRole.MAILBOX,
                }
            )
            engine = create_database_engine(settings)
            resolved_worker = create_runtime_gmail_worker(
                settings=settings,
                engine=engine,
                owner=args.owner,
            )
        result = _run(args, worker=resolved_worker)
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError) as exc:
        _emit_error(str(exc), json_output=json_output)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    if result is None:
        return 0
    _emit(result, json_output=json_output)
    if isinstance(result, GmailWorkerRunResult) and result.failed:
        return 1
    return 0


def _run(
    args: argparse.Namespace,
    *,
    worker: GmailReadOnlyWorkerPort,
) -> GmailWorkerStatus | GmailWorkerRunResult | None:
    if args.command == "status":
        return worker.status()
    if args.command == "run-once":
        return worker.run_once(limit=args.limit, max_results=args.max_results)
    if args.command == "worker":
        run_worker_forever(
            cast("GmailReadOnlyWorker", worker),
            poll_seconds=args.poll_seconds,
            limit=args.limit,
            max_results=args.max_results,
        )
        return None
    raise ValueError("unknown gmail readonly command")


def _emit(result: GmailWorkerStatus | GmailWorkerRunResult, *, json_output: bool) -> None:
    payload = _to_json(result)
    if json_output:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    if isinstance(result, GmailWorkerStatus):
        if result.enabled:
            print(f"gmail readonly worker enabled; pending_mailboxes={result.pending_mailboxes}")
        else:
            print(f"gmail readonly worker disabled: {result.disabled_reason}")
        return
    print(
        "gmail readonly sync: "
        f"outcome={result.outcome.value} claimed={result.claimed} synced={result.synced} "
        f"deferred={result.deferred} failed={result.failed} "
        f"processed_messages={result.processed_messages} review_proposals={result.review_proposals}"
    )


def _to_json(result: GmailWorkerStatus | GmailWorkerRunResult) -> dict[str, object]:
    if isinstance(result, GmailWorkerStatus):
        return {
            "enabled": result.enabled,
            "owner": result.owner,
            "pending_mailboxes": result.pending_mailboxes,
            "disabled_reason": result.disabled_reason,
        }
    return {
        "outcome": result.outcome.value,
        "claimed": result.claimed,
        "synced": result.synced,
        "deferred": result.deferred,
        "failed": result.failed,
        "processed_messages": result.processed_messages,
        "review_proposals": result.review_proposals,
        "disabled_reason": result.disabled_reason,
        "mailbox_results": [
            {
                "mailbox_id": str(item.mailbox_id),
                "outcome": item.outcome.value,
                "processed_messages": item.processed_messages,
                "review_proposals": item.review_proposals,
                "error_code": item.error_code,
            }
            for item in result.mailbox_results
        ],
    }


def _emit_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"errors": [message], "ok": False}, sort_keys=True), file=sys.stderr)
    else:
        print(f"ERROR: {message}", file=sys.stderr)


__all__: Sequence[str] = ("main",)


if __name__ == "__main__":
    raise SystemExit(main())
