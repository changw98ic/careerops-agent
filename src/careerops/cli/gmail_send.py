from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from typing import Protocol, cast

from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.gmail.send_worker import (
    GmailSendReconciliationRunResult,
    GmailSendWorker,
    GmailSendWorkerRunResult,
    GmailSendWorkerStatus,
    UnixAttachmentResolver,
    create_runtime_gmail_send_worker,
    run_worker_forever,
)


class GmailSendWorkerPort(Protocol):
    def status(self) -> GmailSendWorkerStatus: ...

    def run_once(self, *, limit: int = 10) -> GmailSendWorkerRunResult: ...

    def reconcile_once(self, *, limit: int = 10) -> GmailSendReconciliationRunResult: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the reviewed Gmail send worker.")
    parser.add_argument(
        "--owner",
        default="gmail-send-worker",
        help="bounded Gmail send worker owner identifier",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    status = subcommands.add_parser("status")
    status.add_argument("--json", action="store_true")

    run_once = subcommands.add_parser("run-once")
    run_once.add_argument("--limit", type=int, default=10)
    run_once.add_argument("--json", action="store_true")

    reconcile_once = subcommands.add_parser("reconcile-once")
    reconcile_once.add_argument("--limit", type=int, default=10)
    reconcile_once.add_argument("--json", action="store_true")

    worker = subcommands.add_parser("worker")
    worker.add_argument("--limit", type=int, default=10)
    worker.add_argument("--poll-seconds", type=float, default=5)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    worker: GmailSendWorkerPort | None = None,
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
                    "database_role": DatabaseCapabilityRole.MAIL_SENDER,
                }
            )
            engine = create_database_engine(settings)
            attachment_resolver = None
            if settings.gmail_send_attachment_broker_socket is not None:
                attachment_resolver = UnixAttachmentResolver(
                    socket_path=settings.gmail_send_attachment_broker_socket
                )
            resolved_worker = create_runtime_gmail_send_worker(
                settings=settings,
                engine=engine,
                owner=args.owner,
                attachment_resolver=attachment_resolver,
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
    if isinstance(result, GmailSendWorkerRunResult) and result.failed:
        return 1
    return 0


def _run(
    args: argparse.Namespace,
    *,
    worker: GmailSendWorkerPort,
) -> GmailSendWorkerStatus | GmailSendWorkerRunResult | GmailSendReconciliationRunResult | None:
    if args.command == "status":
        return worker.status()
    if args.command == "run-once":
        return worker.run_once(limit=args.limit)
    if args.command == "reconcile-once":
        return worker.reconcile_once(limit=args.limit)
    if args.command == "worker":
        run_worker_forever(
            cast("GmailSendWorker", worker),
            poll_seconds=args.poll_seconds,
            limit=args.limit,
        )
        return None
    raise ValueError("unknown gmail send command")


def _emit(
    result: GmailSendWorkerStatus | GmailSendWorkerRunResult | GmailSendReconciliationRunResult,
    *,
    json_output: bool,
) -> None:
    payload = asdict(result)
    if "outcome" in payload:
        payload["outcome"] = result.outcome.value  # type: ignore[union-attr]
    if json_output:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    if isinstance(result, GmailSendWorkerStatus):
        if result.enabled:
            print("gmail send worker enabled")
        else:
            print(f"gmail send worker disabled: {result.disabled_reason}")
        return
    if isinstance(result, GmailSendReconciliationRunResult):
        print(
            "gmail send reconcile: "
            f"outcome={result.outcome.value} claimed={result.claimed} "
            f"confirmed={result.confirmed} ambiguous={result.ambiguous}"
        )
        return
    print(
        "gmail send: "
        f"outcome={result.outcome.value} claimed={result.claimed} published={result.published} "
        f"deferred={result.deferred} failed={result.failed}"
    )


def _emit_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"errors": [message], "ok": False}, sort_keys=True), file=sys.stderr)
    else:
        print(f"ERROR: {message}", file=sys.stderr)


__all__: Sequence[str] = ("main",)


if __name__ == "__main__":
    raise SystemExit(main())
