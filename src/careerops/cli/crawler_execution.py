from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from careerops.application.crawler_execution import (
    CrawlerExecutionApprovalStore,
    CrawlerExecutionRequestDraft,
    CrawlerExecutionRequestStore,
    CrawlerExecutionService,
    crawler_request_payload_hash,
)
from careerops.cli import crawl_sources
from careerops.cli.crawl_sources import CrawlSourceError
from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.crawler_execution import PostgresCrawlerExecutionRepository
from careerops.infrastructure.database.engine import create_database_engine

_REVIEW_ARTIFACT_DIR = Path("datasets/private/crawler-execution-reviews")


class CrawlerExecutionCliError(ValueError):
    """Raised when the trusted crawler execution control CLI rejects input."""


@dataclass(frozen=True, slots=True)
class ReviewedCrawlerExecutionRequest:
    request_id: UUID
    request_artifact_path: Path
    request_artifact_sha256: str
    draft: CrawlerExecutionRequestDraft


class _CreateOnlyApprovalStore:
    def approve_and_enqueue(self, *args: object) -> UUID:
        raise RuntimeError("crawler execution approval is only available through the console")

    def reject(self, *args: object) -> UUID:
        raise RuntimeError("crawler execution rejection is only available through the console")


def create_reviewed_request(
    *,
    root: Path,
    config_path: Path,
    source_ids: Sequence[str],
    owner_user_id: UUID,
    reason: str,
    expires_in: timedelta,
    now: datetime,
) -> ReviewedCrawlerExecutionRequest:
    """Create local review evidence and the matching durable request draft.

    This function deliberately accepts only a manifest path plus source identifiers. Raw crawl
    URLs, script paths, headers, proxies, browser settings, and output paths can only appear if the
    trusted manifest parser accepts them; the parser forbids those fields.
    """

    root = root.resolve()
    manifest_relative_path, manifest_file = _existing_manifest_file(
        root,
        config_path,
        field="crawler manifest",
    )
    manifest = crawl_sources.load_manifest(manifest_file)
    plans = crawl_sources.select_plans_for_review(
        crawl_sources.plan_manifest(manifest, root=root),
        source_ids,
    )
    request = crawl_sources.create_execution_request(
        manifest,
        plans,
        root=root,
        reason=reason,
        now=now,
        expires_in=expires_in,
    )
    request_artifact_path = _generated_review_artifact_path(root, UUID(request.request_id))
    _write_new_json(request_artifact_path, request.model_dump(mode="json"))
    request_artifact_sha256 = _sha256_file(request_artifact_path)
    request_id = UUID(request.request_id)
    relative_request_path = str(request_artifact_path.relative_to(root))
    target = {
        "manifest_path": str(manifest_relative_path),
        "request_artifact_path": relative_request_path,
    }
    payload = {
        "version": "crawler-execution-request.v1",
        "provider_execution": "disabled_until_outbox_worker",
        "manifest_sha256": request.manifest_sha256,
        "request_sha256": request_artifact_sha256,
        "reviewed_plan_sha256": request.plan_sha256,
        "source_ids": list(request.source_ids),
        "reason": request.reason,
    }
    payload_hash = crawler_request_payload_hash(target=target, payload=payload)
    draft = CrawlerExecutionRequestDraft(
        request_id=request_id,
        owner_user_id=owner_user_id,
        action_intent_id=request_id,
        payload_version_id=uuid5(
            NAMESPACE_URL,
            f"careerops:crawler-execution-request:{request.fingerprint}",
        ),
        payload_hash=payload_hash,
        manifest_path=str(manifest_relative_path),
        request_artifact_path=relative_request_path,
        manifest_sha256=request.manifest_sha256,
        request_sha256=request_artifact_sha256,
        reviewed_plan_sha256=request.plan_sha256,
        source_ids=request.source_ids,
        reason=request.reason,
        created_at=_parse_request_timestamp(request.requested_at),
        expires_at=_parse_request_timestamp(request.expires_at),
    )
    return ReviewedCrawlerExecutionRequest(
        request_id=UUID(request.request_id),
        request_artifact_path=request_artifact_path,
        request_artifact_sha256=request_artifact_sha256,
        draft=draft,
    )


def submit_reviewed_request(
    reviewed: ReviewedCrawlerExecutionRequest,
    request_store: CrawlerExecutionRequestStore,
) -> UUID:
    service = CrawlerExecutionService(
        request_store,
        cast("CrawlerExecutionApprovalStore", _CreateOnlyApprovalStore()),
    )
    return service.create_request(reviewed.draft)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create durable crawler execution requests from trusted configured manifests. "
            "Approval and rejection are intentionally handled by the authenticated console."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root containing datasets/ and scripts/",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    request = subcommands.add_parser("request")
    request.add_argument("--config", type=Path, required=True)
    request.add_argument(
        "--source",
        action="append",
        default=[],
        dest="source_ids",
        help="manifest source_id to include; repeat to select several sources",
    )
    request.add_argument(
        "--owner-user-id",
        type=UUID,
        required=True,
        help="existing console user UUID that owns the pending review request",
    )
    request.add_argument("--reason", required=True)
    request.add_argument(
        "--expires-in-hours",
        type=int,
        default=24,
        help="review lifetime from 1 through 168 hours (default: 24)",
    )
    request.add_argument("--json", action="store_true")

    pending = subcommands.add_parser("list-pending")
    pending.add_argument("--owner-user-id", type=UUID, required=True)
    pending.add_argument("--limit", type=int, default=50)
    pending.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    json_output = bool(getattr(args, "json", False))
    try:
        root = args.root.resolve()
        settings = Settings(database_role=DatabaseCapabilityRole.API)
        engine = create_database_engine(settings)
        try:
            if args.command == "request":
                reviewed = create_reviewed_request(
                    root=root,
                    config_path=args.config,
                    source_ids=args.source_ids,
                    owner_user_id=args.owner_user_id,
                    reason=args.reason,
                    expires_in=timedelta(hours=args.expires_in_hours),
                    now=datetime.now(UTC),
                )
                with engine.begin() as connection:
                    persisted_id = submit_reviewed_request(
                        reviewed,
                        PostgresCrawlerExecutionRepository(connection),
                    )
                _emit(
                    {
                        "request": str(reviewed.request_artifact_path.relative_to(root)),
                        "request_id": str(persisted_id),
                        "request_sha256": reviewed.request_artifact_sha256,
                        "source_ids": list(reviewed.draft.source_ids),
                        "status": "pending_console_review",
                    },
                    json_output=json_output,
                )
                return 0
            if args.command == "list-pending":
                with engine.begin() as connection:
                    summaries = CrawlerExecutionService(
                        PostgresCrawlerExecutionRepository(connection),
                        cast(
                            "CrawlerExecutionApprovalStore",
                            PostgresCrawlerExecutionRepository(connection),
                        ),
                    ).list_pending_requests(owner_user_id=args.owner_user_id, limit=args.limit)
                _emit(
                    {
                        "requests": [
                            {
                                "expires_at": summary.expires_at.isoformat(),
                                "manifest_path": summary.manifest_path,
                                "manifest_sha256": summary.manifest_sha256,
                                "owner_user_id": str(summary.owner_user_id),
                                "request_artifact_path": summary.request_artifact_path,
                                "request_sha256": summary.request_sha256,
                                "request_id": str(summary.request_id),
                                "reviewed_plan_sha256": summary.reviewed_plan_sha256,
                                "created_at": summary.created_at.isoformat(),
                                "reason": summary.reason,
                                "source_ids": list(summary.source_ids),
                                "status": "pending_review",
                            }
                            for summary in summaries
                        ],
                        "status": "listed",
                    },
                    json_output=json_output,
                )
                return 0
            raise CrawlerExecutionCliError("unsupported crawler execution command")
        finally:
            engine.dispose()
    except (CrawlerExecutionCliError, CrawlSourceError, ValueError) as error:
        _emit_error(str(error), json_output=json_output)
        return 2


def _emit(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return
    if payload.get("status") == "pending_console_review":
        print(f"created crawler execution request {payload['request_id']} for console review")
    elif payload.get("status") == "listed":
        print(f"listed {len(cast('list[object]', payload['requests']))} pending crawler request(s)")
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _emit_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {"errors": [message], "ok": False},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return
    print(f"ERROR: {message}", file=sys.stderr)


def _existing_dataset_file(root: Path, path: Path, *, field: str) -> tuple[Path, Path]:
    relative, resolved = _dataset_path(root, path, field=field)
    if not resolved.is_file():
        raise CrawlerExecutionCliError(f"{field} must name an existing file under datasets/")
    return relative, resolved


def _existing_manifest_file(root: Path, path: Path, *, field: str) -> tuple[Path, Path]:
    relative, resolved = _existing_dataset_file(root, path, field=field)
    try:
        resolved.relative_to(root / "datasets" / "manifests")
    except ValueError as error:
        raise CrawlerExecutionCliError(f"{field} must be inside datasets/manifests/") from error
    return relative, resolved


def _generated_review_artifact_path(root: Path, request_id: UUID) -> Path:
    return root / _REVIEW_ARTIFACT_DIR / f"{request_id}.json"


def _dataset_path(root: Path, path: Path, *, field: str) -> tuple[Path, Path]:
    if path.is_absolute():
        try:
            relative = path.resolve().relative_to(root)
        except ValueError as error:
            raise CrawlerExecutionCliError(
                f"{field} must be repository-relative or inside the repository root"
            ) from error
    else:
        relative = path
    if not relative.parts or relative.parts[0] != "datasets":
        raise CrawlerExecutionCliError(f"{field} must be under datasets/")
    if any(part == ".." for part in relative.parts):
        raise CrawlerExecutionCliError(f"{field} must not contain parent path segments")
    candidate = (root / relative).resolve()
    datasets_root = (root / "datasets").resolve()
    try:
        candidate.relative_to(datasets_root)
    except ValueError as error:
        raise CrawlerExecutionCliError(f"{field} must stay inside datasets/") from error
    return relative, candidate


def _write_new_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except FileExistsError as error:
        raise CrawlerExecutionCliError("execution request artifact already exists") from error
    except OSError as error:
        raise CrawlerExecutionCliError("execution request artifact could not be written") from error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65_536):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_request_timestamp(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise CrawlerExecutionCliError("execution request timestamps must be timezone-aware")
    return value.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
