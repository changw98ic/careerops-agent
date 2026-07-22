from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from careerops.api.source_registry import (
    ClaimSourceResponse,
    CompleteSourceResponse,
    DueSourcesResponse,
    FailSourceResponse,
    IngestPublicAtsResponse,
    ListSourceRegistriesResponse,
    RegisterSourceRegistryResponse,
    SourceRegistryOperatorProvider,
    SourceRegistryUnavailable,
    create_runtime_source_registry_operator_provider,
)
from careerops.config import Settings

_MANIFEST_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate the configured crawler source registry without running crawlers.",
    )
    parser.add_argument("--actor-id", type=UUID, required=True)
    subcommands = parser.add_subparsers(dest="command", required=True)

    register = subcommands.add_parser("register")
    register.add_argument("--manifest-ref", required=True)
    register.add_argument("--json", action="store_true")

    list_registries = subcommands.add_parser("list")
    list_registries.add_argument("--limit", type=int, default=100)
    list_registries.add_argument("--json", action="store_true")

    due = subcommands.add_parser("due")
    due.add_argument("--registry-id", type=UUID, required=True)
    due.add_argument("--limit", type=int, default=100)
    due.add_argument("--json", action="store_true")

    claim = subcommands.add_parser("claim")
    claim.add_argument("--registry-id", type=UUID, required=True)
    claim.add_argument("--source-id", required=True)
    claim.add_argument("--worker-id", required=True)
    claim.add_argument("--lease-seconds", type=int, default=300)
    claim.add_argument("--json", action="store_true")

    complete = subcommands.add_parser("complete")
    complete.add_argument("--registry-id", type=UUID, required=True)
    complete.add_argument("--source-id", required=True)
    complete.add_argument("--run-id", type=UUID, required=True)
    complete.add_argument("--source-row-id", type=UUID, required=True)
    complete.add_argument("--worker-id", required=True)
    complete.add_argument("--lease-token", type=UUID, required=True)
    complete.add_argument("--output-manifest-sha256")
    complete.add_argument("--cursor")
    complete.add_argument("--result", required=True)
    complete.add_argument("--json", action="store_true")

    fail = subcommands.add_parser("fail")
    fail.add_argument("--registry-id", type=UUID, required=True)
    fail.add_argument("--source-id", required=True)
    fail.add_argument("--run-id", type=UUID, required=True)
    fail.add_argument("--source-row-id", type=UUID, required=True)
    fail.add_argument("--worker-id", required=True)
    fail.add_argument("--lease-token", type=UUID, required=True)
    fail.add_argument("--error", required=True)
    fail.add_argument("--json", action="store_true")

    ingest = subcommands.add_parser("ingest-public-ats")
    ingest.add_argument("--registry-id", type=UUID, required=True)
    ingest.add_argument("--source-id", required=True)
    ingest.add_argument("--run-id", type=UUID, required=True)
    ingest.add_argument("--source-row-id", type=UUID, required=True)
    ingest.add_argument("--max-records", type=int, default=1_000)
    ingest.add_argument("--json", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    provider: SourceRegistryOperatorProvider | None = None,
    now: datetime | None = None,
) -> int:
    args = _parser().parse_args(argv)
    json_output = bool(getattr(args, "json", False))
    resolved_now = now or datetime.now(UTC)
    resolved_provider = provider or create_runtime_source_registry_operator_provider(Settings())
    try:
        result = asyncio.run(_run(args, provider=resolved_provider, now=resolved_now))
    except (SourceRegistryUnavailable, ValueError) as exc:
        _emit_error(str(exc), json_output=json_output)
        return 1
    _emit(result, json_output=json_output)
    return 0


async def _run(
    args: argparse.Namespace,
    *,
    provider: SourceRegistryOperatorProvider,
    now: datetime,
) -> (
    RegisterSourceRegistryResponse
    | ListSourceRegistriesResponse
    | DueSourcesResponse
    | ClaimSourceResponse
    | CompleteSourceResponse
    | FailSourceResponse
    | IngestPublicAtsResponse
):
    if args.command == "register":
        _require_manifest_ref(args.manifest_ref)
        return await provider.register(
            manifest_ref=args.manifest_ref,
            actor_id=args.actor_id,
            now=now,
        )
    if args.command == "list":
        _require_limit(args.limit)
        return await provider.list_registries(actor_id=args.actor_id, limit=args.limit)
    if args.command == "due":
        _require_limit(args.limit)
        return await provider.due_sources(
            registry_id=args.registry_id,
            actor_id=args.actor_id,
            now=now,
            limit=args.limit,
        )
    if args.command == "claim":
        if not 30 <= args.lease_seconds <= 3600:
            raise ValueError("--lease-seconds must be between 30 and 3600")
        return await provider.claim_source(
            registry_id=args.registry_id,
            source_id=args.source_id,
            actor_id=args.actor_id,
            worker_id=args.worker_id,
            lease_for=_timedelta_seconds(args.lease_seconds),
        )
    if args.command == "complete":
        return await provider.complete_source(
            registry_id=args.registry_id,
            source_id=args.source_id,
            actor_id=args.actor_id,
            run_id=args.run_id,
            source_row_id=args.source_row_id,
            worker_id=args.worker_id,
            lease_token=args.lease_token,
            output_manifest_sha256=_parse_optional_sha256(
                args.output_manifest_sha256,
                field="--output-manifest-sha256",
            ),
            cursor=args.cursor,
            result=args.result,
        )
    if args.command == "fail":
        return await provider.fail_source(
            registry_id=args.registry_id,
            source_id=args.source_id,
            actor_id=args.actor_id,
            run_id=args.run_id,
            source_row_id=args.source_row_id,
            worker_id=args.worker_id,
            lease_token=args.lease_token,
            error=args.error,
        )
    if args.command == "ingest-public-ats":
        _require_max_records(args.max_records)
        return await provider.ingest_public_ats(
            registry_id=args.registry_id,
            source_id=args.source_id,
            run_id=args.run_id,
            source_row_id=args.source_row_id,
            actor_id=args.actor_id,
            max_records=args.max_records,
        )
    raise ValueError("unknown source registry command")


def _timedelta_seconds(seconds: int) -> Any:
    from datetime import timedelta

    return timedelta(seconds=seconds)


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("--limit must be between 1 and 500")


def _require_max_records(max_records: int) -> None:
    if not 1 <= max_records <= 10_000:
        raise ValueError("--max-records must be between 1 and 10000")


def _require_manifest_ref(manifest_ref: str) -> None:
    if _MANIFEST_REF.fullmatch(manifest_ref) is None:
        raise ValueError("--manifest-ref must be a bounded manifest identifier")


def _parse_optional_sha256(raw: str | None, *, field: str) -> str | None:
    if raw is None:
        return None
    if _SHA256.fullmatch(raw) is None:
        raise ValueError(f"{field} must be a lowercase sha256 hex digest")
    return raw


def _emit(
    result: (
        RegisterSourceRegistryResponse
        | ListSourceRegistriesResponse
        | DueSourcesResponse
        | ClaimSourceResponse
        | CompleteSourceResponse
        | FailSourceResponse
        | IngestPublicAtsResponse
    ),
    *,
    json_output: bool,
) -> None:
    payload = result.model_dump(mode="json")
    if json_output:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    status = payload["status"]
    if status == "registered":
        print(f"registered source registry {payload['registry_id']}")
    elif status == "listed":
        print(f"listed {len(payload['registries'])} source registries")
    elif status == "due":
        print(f"found {len(payload['due_sources'])} due source(s)")
    elif status == "claimed":
        print(f"claimed source {payload['source_id']}")
    elif status == "completed":
        print(f"completed source {payload['source_id']}")
    elif status == "failed":
        print(f"failed source {payload['source_id']}")
    elif status == "ingested":
        print(
            "ingested "
            f"{payload['observed_records']} public ATS record(s) for {payload['source_id']}"
        )


def _emit_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"errors": [message], "ok": False}, sort_keys=True), file=sys.stderr)
    else:
        print(f"ERROR: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
