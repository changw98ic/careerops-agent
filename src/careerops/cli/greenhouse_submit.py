from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from typing import Protocol, cast

from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.greenhouse.worker import (
    GreenhouseSubmitWorker,
    GreenhouseSubmitWorkerRunResult,
    GreenhouseSubmitWorkerStatus,
    UnixGreenhouseAttachmentResolver,
    create_runtime_greenhouse_submit_worker,
    run_worker_forever,
)


class GreenhouseSubmitWorkerPort(Protocol):
    def status(self) -> GreenhouseSubmitWorkerStatus: ...

    def run_once(self, *, limit: int = 10) -> GreenhouseSubmitWorkerRunResult: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the reviewed Greenhouse submit worker.")
    parser.add_argument(
        "--owner",
        default="greenhouse-submit-worker",
        help="bounded Greenhouse submit worker owner identifier",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    status = subcommands.add_parser("status")
    status.add_argument("--json", action="store_true")

    run_once = subcommands.add_parser("run-once")
    run_once.add_argument("--limit", type=int, default=10)
    run_once.add_argument("--json", action="store_true")

    worker = subcommands.add_parser("worker")
    worker.add_argument("--limit", type=int, default=10)
    worker.add_argument("--poll-seconds", type=float, default=5)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    worker: GreenhouseSubmitWorkerPort | None = None,
) -> int:
    args = _parser().parse_args(argv)
    json_output = bool(getattr(args, "json", False))
    engine = None
    try:
        resolved_worker = worker
        if resolved_worker is None:
            settings = Settings(database_role=DatabaseCapabilityRole.GREENHOUSE_SENDER)
            engine = create_database_engine(settings)
            attachment_resolver = None
            if settings.greenhouse_submit_attachment_broker_socket is not None:
                attachment_resolver = UnixGreenhouseAttachmentResolver(
                    socket_path=settings.greenhouse_submit_attachment_broker_socket
                )
            resolved_worker = create_runtime_greenhouse_submit_worker(
                settings=settings,
                engine=engine,
                owner=args.owner,
                attachment_resolver=attachment_resolver,
            )
        result = _run(args, worker=resolved_worker)
    except (ValueError, RuntimeError) as exc:
        _emit_error(str(exc), json_output=json_output)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    if result is None:
        return 0
    _emit(result, json_output=json_output)
    if isinstance(result, GreenhouseSubmitWorkerRunResult) and result.failed:
        return 1
    return 0


def _run(
    args: argparse.Namespace,
    *,
    worker: GreenhouseSubmitWorkerPort,
) -> GreenhouseSubmitWorkerStatus | GreenhouseSubmitWorkerRunResult | None:
    if args.command == "status":
        return worker.status()
    if args.command == "run-once":
        return worker.run_once(limit=args.limit)
    if args.command == "worker":
        run_worker_forever(
            cast("GreenhouseSubmitWorker", worker),
            poll_seconds=args.poll_seconds,
            limit=args.limit,
        )
        return None
    raise ValueError("unknown Greenhouse submit command")


def _emit(
    result: GreenhouseSubmitWorkerStatus | GreenhouseSubmitWorkerRunResult,
    *,
    json_output: bool,
) -> None:
    payload = asdict(result)
    if "outcome" in payload:
        payload["outcome"] = result.outcome.value  # type: ignore[union-attr]
    if json_output:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    if isinstance(result, GreenhouseSubmitWorkerStatus):
        if result.enabled:
            print("greenhouse submit worker enabled")
        else:
            print(f"greenhouse submit worker disabled: {result.disabled_reason}")
        return
    print(
        "greenhouse submit: "
        f"outcome={result.outcome.value} claimed={result.claimed} "
        f"accepted_unverified={result.accepted_unverified} rejected={result.rejected} "
        f"ambiguous={result.ambiguous} deferred_prepost={result.deferred_prepost} "
        f"failed={result.failed}"
    )


def _emit_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"errors": [message], "ok": False}, sort_keys=True), file=sys.stderr)
    else:
        print(f"ERROR: {message}", file=sys.stderr)


__all__: Sequence[str] = ("main",)


if __name__ == "__main__":
    raise SystemExit(main())
