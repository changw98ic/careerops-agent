"""Run the supported public recruitment crawlers from a declarative local manifest.

This is a local operator interface, not a public URL-fetching API. A manifest can only select
registered crawler adapters and pre-existing JSONL seed artifacts under ``datasets/``. It cannot
provide a URL, proxy, headers, browser setting, script path, or sandbox-egress override.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess  # nosec B404
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TextIO, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.engine import RowMapping

from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.crawler_source_registry import (
    PostgresCrawlerSourceRegistryRepository,
)
from careerops.infrastructure.database.engine import create_database_engine

MANIFEST_VERSION = 1
RECEIPT_FILENAME = "configured_crawl_receipt.json"
EXECUTION_REQUEST_VERSION = 1
EXECUTION_REQUEST_KIND = "configured_crawl_execution_request"
EXECUTION_APPROVAL_KIND = "configured_crawl_execution_approval"
EXECUTION_CLAIM_KIND = "configured_crawl_execution_claim"
SOURCE_REGISTRY_VERSION = 1
SOURCE_REGISTRY_KIND = "configured_crawl_source_registry"
_REVIEW_ARTIFACT_DIRECTORY = Path("datasets/private/crawler-execution-reviews")
_EXECUTION_PRIVATE_DIRECTORIES = (
    "crawler-execution-claims",
    "crawler-execution-output-locks",
    "crawler-execution-reviews",
    "crawler-execution-snapshots",
)
_OUTPUT_RESERVATION_FILENAME = ".careerops-execution-reservation.json"
SOURCE_ID_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"
_SOURCE_ID = re.compile(SOURCE_ID_PATTERN)
_ACTOR = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

ADAPTER_SITEMAP = "recruitment.sitemap_discovery"
ADAPTER_COMMONCRAWL = "recruitment.commoncrawl_discovery"
ADAPTER_PUBLIC_ATS = "recruitment.public_ats_feed"
ADAPTER_PAGES = "recruitment.page_collection"

_ADAPTER_SCRIPTS = {
    ADAPTER_SITEMAP: "discover_recruitment_sitemaps.py",
    ADAPTER_COMMONCRAWL: "discover_recruitment_commoncrawl.py",
    ADAPTER_PUBLIC_ATS: "discover_public_ats_jobs.py",
    ADAPTER_PAGES: "collect_recruitment_pages.py",
}
_SEED_OUTPUT_FILENAMES = {
    ADAPTER_SITEMAP: "discovered_recruitment_sitemap_urls.jsonl",
    ADAPTER_COMMONCRAWL: "discovered_recruitment_commoncrawl_urls.jsonl",
}
_PROXY_ENV_NAMES = frozenset(
    {
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_CHILD_ENV_NAMES = frozenset({"LANG", "LC_ALL", "PATH", "TZ"})
_PYTHON_ISOLATION_FLAGS = ("-Es",)

_BUDGET_CONTROL_BOUNDS: dict[str, tuple[int | float, int | float]] = {
    "depth": (0, 1),
    "max_discovered_urls": (1, 50_000),
    "max_feed_bytes": (1, 10_000_000),
    "max_indexes": (1, 3),
    "max_response_bytes": (1, 5_000_000),
    "max_results_per_query": (1, 500),
    "max_sitemap_bytes": (1, 10_000_000),
    "max_stored_bytes": (1, 10_737_418_240),
    "timeout_seconds": (1.0, 60.0),
}
_RATE_LIMIT_CONTROL_BOUNDS: dict[str, tuple[int | float, int | float]] = {
    "concurrency": (1, 8),
    "host_failure_cooldown_seconds": (0.0, 300.0),
    "host_failure_cooldown_threshold": (1, 10),
    "max_concurrency_per_host": (1, 2),
    "max_retry_backoff_seconds": (0.1, 30.0),
    "provider_circuit_breaker_failures": (1, 10),
    "retry_backoff_seconds": (0.1, 5.0),
}
_INTEGER_CONTROL_KEYS = frozenset(
    {
        "concurrency",
        "depth",
        "host_failure_cooldown_threshold",
        "max_concurrency_per_host",
        "max_discovered_urls",
        "max_feed_bytes",
        "max_indexes",
        "max_response_bytes",
        "max_results_per_query",
        "max_sitemap_bytes",
        "max_stored_bytes",
        "provider_circuit_breaker_failures",
    }
)

type JsonObject = dict[str, Any]
Runner = Callable[..., subprocess.CompletedProcess[str]]


class CrawlSourceError(ValueError):
    """Raised for a manifest that cannot safely describe a configured crawl."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _BaseLimits(_StrictModel):
    concurrency: int = Field(default=4, ge=1, le=8)
    retry_rounds: int = Field(default=2, ge=0, le=4)
    timeout_seconds: float = Field(default=20.0, ge=1.0, le=60.0)


class SitemapLimits(_BaseLimits):
    max_sitemap_bytes: int = Field(default=5_000_000, ge=1, le=10_000_000)


class CommonCrawlLimits(_BaseLimits):
    concurrency: int = Field(default=2, ge=1, le=4)
    max_indexes: int = Field(default=2, ge=1, le=3)
    max_results_per_query: int = Field(default=100, ge=1, le=500)
    max_discovered_urls: int = Field(default=10_000, ge=1, le=50_000)
    provider_circuit_breaker_failures: int = Field(default=5, ge=1, le=10)
    retry_backoff_seconds: float = Field(default=0.5, ge=0.1, le=5.0)
    max_retry_backoff_seconds: float = Field(default=8.0, ge=0.1, le=30.0)


class PublicAtsLimits(_BaseLimits):
    max_feed_bytes: int = Field(default=10_000_000, ge=1, le=10_000_000)


class PageCollectionLimits(_BaseLimits):
    max_stored_bytes: int = Field(default=1_073_741_824, ge=1, le=10_737_418_240)
    max_response_bytes: int = Field(default=1_000_000, ge=1, le=5_000_000)
    max_concurrency_per_host: int = Field(default=2, ge=1, le=2)
    host_failure_cooldown_threshold: int = Field(default=3, ge=1, le=10)
    host_failure_cooldown_seconds: float = Field(default=30.0, ge=0.0, le=300.0)
    depth: int = Field(default=1, ge=0, le=1)


class SourceScheduleSpec(_StrictModel):
    cursor: str | None = Field(default=None, max_length=160)
    cadence_seconds: int = Field(default=86_400, ge=1, le=604_800)
    retry_rounds: int = Field(default=2, ge=0, le=10)
    budget: dict[str, int | float] = Field(default_factory=dict)
    rate_limits: dict[str, int | float] = Field(default_factory=dict)
    robots_terms_policy: Literal["respect", "review_required", "blocked"] = "respect"
    canonical_ingestion_policy: Literal["canonical_job_ingestion", "dedupe_only"] = "dedupe_only"
    provenance_policy: Literal["preserve", "compact"] = "preserve"
    dedupe_policy: Literal["hash", "url", "hybrid"] = "hybrid"

    def model_post_init(self, __context: object) -> None:
        _validate_control_caps(self.budget, "budget", _BUDGET_CONTROL_BOUNDS)
        _validate_control_caps(
            self.rate_limits,
            "rate_limits",
            _RATE_LIMIT_CONTROL_BOUNDS,
        )


class _BaseSource(_StrictModel):
    source_id: str = Field(min_length=1, max_length=64, pattern=SOURCE_ID_PATTERN)
    input_artifact: str = Field(min_length=1)
    output_dir: str = Field(min_length=1)
    enabled: bool = True
    schedule: SourceScheduleSpec = Field(default_factory=SourceScheduleSpec)


class SitemapSource(_BaseSource):
    adapter: Literal["recruitment.sitemap_discovery"]
    resume: bool = True
    limits: SitemapLimits = Field(default_factory=SitemapLimits)


class CommonCrawlSource(_BaseSource):
    adapter: Literal["recruitment.commoncrawl_discovery"]
    resume: bool = True
    limits: CommonCrawlLimits = Field(default_factory=CommonCrawlLimits)


class PublicAtsSource(_BaseSource):
    adapter: Literal["recruitment.public_ats_feed"]
    limits: PublicAtsLimits = Field(default_factory=PublicAtsLimits)


class PageCollectionSource(_BaseSource):
    adapter: Literal["recruitment.page_collection"]
    resume: bool = True
    additional_seed_sources: list[str] = Field(default_factory=list, max_length=16)
    limits: PageCollectionLimits = Field(default_factory=PageCollectionLimits)


type SourceSpec = SitemapSource | CommonCrawlSource | PublicAtsSource | PageCollectionSource

_BUDGET_FIELDS_BY_ADAPTER: dict[str, tuple[str, ...]] = {
    ADAPTER_SITEMAP: ("timeout_seconds", "max_sitemap_bytes"),
    ADAPTER_COMMONCRAWL: (
        "timeout_seconds",
        "max_indexes",
        "max_results_per_query",
        "max_discovered_urls",
    ),
    ADAPTER_PUBLIC_ATS: ("timeout_seconds", "max_feed_bytes"),
    ADAPTER_PAGES: (
        "timeout_seconds",
        "max_stored_bytes",
        "max_response_bytes",
        "depth",
    ),
}
_RATE_LIMIT_FIELDS_BY_ADAPTER: dict[str, tuple[str, ...]] = {
    ADAPTER_SITEMAP: ("concurrency",),
    ADAPTER_COMMONCRAWL: (
        "concurrency",
        "provider_circuit_breaker_failures",
        "retry_backoff_seconds",
        "max_retry_backoff_seconds",
    ),
    ADAPTER_PUBLIC_ATS: ("concurrency",),
    ADAPTER_PAGES: (
        "concurrency",
        "max_concurrency_per_host",
        "host_failure_cooldown_threshold",
        "host_failure_cooldown_seconds",
    ),
}


def _validate_control_caps(
    values: Mapping[str, int | float],
    field: str,
    bounds: Mapping[str, tuple[int | float, int | float]],
) -> None:
    for key, value in values.items():
        if key not in bounds:
            raise ValueError(f"{field} contains an unsupported control")
        if isinstance(value, bool):
            raise ValueError(f"{field} values must be numeric")
        if key in _INTEGER_CONTROL_KEYS and not isinstance(value, int):
            raise ValueError(f"{field}.{key} must be an integer")
        lower, upper = bounds[key]
        if value < lower or value > upper:
            raise ValueError(f"{field}.{key} is outside its safe bound")


def _validate_source_control_caps(source: SourceSpec) -> None:
    if (
        source.adapter != ADAPTER_PUBLIC_ATS
        and source.schedule.canonical_ingestion_policy == "canonical_job_ingestion"
    ):
        raise ValueError("canonical_job_ingestion is only supported by the public ATS adapter")
    supported_budget = frozenset(_BUDGET_FIELDS_BY_ADAPTER[source.adapter])
    supported_rate_limits = frozenset(_RATE_LIMIT_FIELDS_BY_ADAPTER[source.adapter])
    unsupported_budget = set(source.schedule.budget) - supported_budget
    unsupported_rate_limits = set(source.schedule.rate_limits) - supported_rate_limits
    if unsupported_budget:
        raise ValueError("budget contains a control unsupported by the selected adapter")
    if unsupported_rate_limits:
        raise ValueError("rate_limits contains a control unsupported by the selected adapter")
    for field, cap in source.schedule.budget.items():
        if cap > cast("int | float", getattr(source.limits, field)):
            raise ValueError(f"budget.{field} cannot weaken the adapter limit")
    for field, cap in source.schedule.rate_limits.items():
        if cap > cast("int | float", getattr(source.limits, field)):
            raise ValueError(f"rate_limits.{field} cannot weaken the adapter limit")


def _effective_control_value(
    source: SourceSpec,
    field: str,
    configured: Mapping[str, int | float],
) -> int | float:
    baseline = cast("int | float", getattr(source.limits, field))
    cap = configured.get(field)
    return baseline if cap is None else min(baseline, cap)


def _effective_schedule(source: SourceSpec) -> JsonObject:
    schedule = source.schedule.model_dump(mode="json")
    schedule["budget"] = {
        field: _effective_control_value(source, field, source.schedule.budget)
        for field in _BUDGET_FIELDS_BY_ADAPTER[source.adapter]
    }
    schedule["budget"]["retry_rounds"] = min(
        source.limits.retry_rounds,
        source.schedule.retry_rounds,
    )
    schedule["rate_limits"] = {
        field: _effective_control_value(source, field, source.schedule.rate_limits)
        for field in _RATE_LIMIT_FIELDS_BY_ADAPTER[source.adapter]
    }
    return schedule


class CrawlExecutionRequestDocument(_StrictModel):
    version: Literal[1]
    kind: Literal["configured_crawl_execution_request"]
    request_id: str
    manifest_sha256: str
    plan_sha256: str
    reviewed_plan: list[dict[str, Any]] = Field(min_length=1, max_length=64)
    source_ids: list[str] = Field(min_length=1, max_length=64)
    requested_at: str
    expires_at: str
    reason: str = Field(min_length=1, max_length=500)

    def model_post_init(self, __context: object) -> None:
        _validate_execution_document(
            request_id=self.request_id,
            manifest_sha256=self.manifest_sha256,
            plan_sha256=self.plan_sha256,
            source_ids=self.source_ids,
            requested_at=self.requested_at,
            expires_at=self.expires_at,
        )
        if _sha256_json(self.reviewed_plan) != self.plan_sha256:
            raise ValueError("reviewed_plan must match plan_sha256")
        reviewed_source_ids: list[str] = []
        for binding in self.reviewed_plan:
            source_id = binding.get("source_id")
            if not isinstance(source_id, str) or _SOURCE_ID.fullmatch(source_id) is None:
                raise ValueError("reviewed_plan must contain valid source identifiers")
            reviewed_source_ids.append(source_id)
        if tuple(reviewed_source_ids) != tuple(self.source_ids):
            raise ValueError("reviewed_plan source identifiers must match source_ids")

    @property
    def fingerprint(self) -> str:
        return _sha256_json(self.model_dump(mode="json"))


class CrawlExecutionApprovalDocument(_StrictModel):
    version: Literal[1]
    kind: Literal["configured_crawl_execution_approval"]
    request_id: str
    request_sha256: str
    manifest_sha256: str
    plan_sha256: str
    source_ids: list[str] = Field(min_length=1, max_length=64)
    approved_by: str
    approved_at: str
    expires_at: str
    note: str = Field(default="", max_length=500)

    def model_post_init(self, __context: object) -> None:
        _validate_execution_document(
            request_id=self.request_id,
            manifest_sha256=self.manifest_sha256,
            plan_sha256=self.plan_sha256,
            source_ids=self.source_ids,
            requested_at=self.approved_at,
            expires_at=self.expires_at,
        )
        if _SHA256.fullmatch(self.request_sha256) is None:
            raise ValueError("request_sha256 must be a lowercase sha256 hex digest")
        if _ACTOR.fullmatch(self.approved_by) is None:
            raise ValueError("approved_by must be a bounded actor identifier")


@dataclass(frozen=True, slots=True)
class CrawlSourceManifest:
    version: int
    sources: tuple[SourceSpec, ...]


@dataclass(frozen=True, slots=True)
class CrawlSourceRegistryDocument:
    version: int
    kind: str
    manifest_path: str
    manifest_sha256: str
    registry_sha256: str
    generated_at: str
    manifest: JsonObject
    resolved_plans: tuple[JsonObject, ...]


@dataclass(frozen=True, slots=True)
class CrawlSourcePlan:
    source_id: str
    adapter: str
    enabled: bool
    input_artifact: Path
    output_dir: Path
    schedule: JsonObject
    dependency_artifacts: tuple[Path, ...]
    dependency_source_ids: tuple[str, ...]
    command: tuple[str, ...]
    source_sha256: str

    def as_dict(self, *, root: Path) -> JsonObject:
        return {
            "adapter": self.adapter,
            "command": [str(_relative_to_root(root, Path(item))) for item in self.command],
            "dependency_artifacts": [
                str(_relative_to_root(root, path)) for path in self.dependency_artifacts
            ],
            "dependency_source_ids": list(self.dependency_source_ids),
            "enabled": self.enabled,
            "input_artifact": str(_relative_to_root(root, self.input_artifact)),
            "output_dir": str(_relative_to_root(root, self.output_dir)),
            "schedule": self.schedule,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class CrawlSourceRunResult:
    source_id: str
    adapter: str
    status: str
    returncode: int | None
    receipt_path: Path

    def as_dict(self, *, root: Path) -> JsonObject:
        return {
            "adapter": self.adapter,
            "receipt": str(_relative_to_root(root, self.receipt_path)),
            "returncode": self.returncode,
            "source_id": self.source_id,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class _DynamicDependencySnapshot:
    producer_source_id: str
    consumer_source_id: str
    source_path: Path
    target_path: Path


@dataclass(slots=True)
class _OutputExecutionLock:
    path: Path
    handle: TextIO


@dataclass(slots=True)
class CrawlExecutionSnapshot:
    directory: Path
    reviewed_plan_path: Path
    reviewed_plan_sha256: str
    commands_by_source_id: dict[str, tuple[str, ...]]
    dynamic_dependency_targets: dict[tuple[str, Path], Path]
    dynamic_dependencies_by_producer: dict[str, tuple[_DynamicDependencySnapshot, ...]]
    fresh_output_source_ids: frozenset[str]
    output_reservations: dict[str, Path]
    output_locks: dict[str, _OutputExecutionLock]

    def reserve_output_directories(
        self,
        plans: Sequence[CrawlSourcePlan],
        *,
        root: Path,
        request_id: str,
    ) -> None:
        try:
            import fcntl
        except ModuleNotFoundError as error:  # pragma: no cover - non-POSIX runtime guard
            raise CrawlSourceError("reviewed execution requires POSIX output locking") from error
        try:
            for plan in plans:
                output_dir = _safe_dataset_path(
                    root,
                    plan.output_dir,
                    field=f"source {plan.source_id} output_dir",
                )
                output_path_sha256 = _sha256_json(str(_relative_to_root(root, output_dir)))
                lock_path = (
                    root
                    / "datasets"
                    / "private"
                    / "crawler-execution-output-locks"
                    / f"{output_path_sha256}.lock"
                )
                _safe_dataset_path(root, lock_path, field=f"source {plan.source_id} output lock")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = lock_path.open("a+", encoding="utf-8")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    handle.close()
                    raise CrawlSourceError(
                        f"source {plan.source_id} output_dir is already reserved by "
                        "an active reviewed execution"
                    ) from error
                handle.seek(0)
                handle.truncate()
                handle.write(
                    json.dumps(
                        {
                            "execution_snapshot": str(_relative_to_root(root, self.directory)),
                            "kind": "configured_crawl_output_lock",
                            "request_id": request_id,
                            "source_id": plan.source_id,
                            "version": EXECUTION_REQUEST_VERSION,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
                handle.flush()
                os.chmod(lock_path, 0o600)
                self.output_locks[plan.source_id] = _OutputExecutionLock(
                    path=lock_path,
                    handle=handle,
                )
                if plan.source_id not in self.fresh_output_source_ids:
                    continue
                try:
                    output_dir.mkdir(parents=True, exist_ok=False)
                except FileExistsError as error:
                    raise CrawlSourceError(
                        f"source {plan.source_id} output_dir must be absent before "
                        "a reviewed execution"
                    ) from error
                except OSError as error:
                    raise CrawlSourceError(
                        f"source {plan.source_id} output_dir could not be reserved"
                    ) from error
                reservation_path = output_dir / _OUTPUT_RESERVATION_FILENAME
                _write_new_json(
                    reservation_path,
                    {
                        "execution_snapshot": str(_relative_to_root(root, self.directory)),
                        "kind": "configured_crawl_output_reservation",
                        "request_id": request_id,
                        "source_id": plan.source_id,
                        "version": EXECUTION_REQUEST_VERSION,
                    },
                    already_exists_message="crawler output reservation already exists",
                )
                try:
                    os.chmod(reservation_path, 0o400)
                except OSError as error:
                    raise CrawlSourceError(
                        f"source {plan.source_id} output reservation permissions could not be set"
                    ) from error
                self.output_reservations[plan.source_id] = reservation_path
        except CrawlSourceError:
            self.release_output_locks()
            raise
        except OSError as error:
            self.release_output_locks()
            raise CrawlSourceError("crawler output locks could not be reserved") from error

    def release_output_locks(self) -> None:
        for lock in self.output_locks.values():
            with suppress(OSError):
                lock.handle.close()
        self.output_locks.clear()

    def require_output_lock(self, plan: CrawlSourcePlan) -> None:
        if plan.source_id not in self.output_locks:
            raise CrawlSourceError(f"source {plan.source_id} output_dir was not locked")

    def require_fresh_output_reservation(self, plan: CrawlSourcePlan, *, root: Path) -> bool:
        if plan.source_id not in self.fresh_output_source_ids:
            return False
        reservation_path = self.output_reservations.get(plan.source_id)
        if reservation_path is None:
            raise CrawlSourceError(f"source {plan.source_id} output_dir was not reserved")
        expected_path = _safe_dataset_path(
            root,
            plan.output_dir / _OUTPUT_RESERVATION_FILENAME,
            field=f"source {plan.source_id} output reservation",
        )
        if reservation_path != expected_path or not reservation_path.is_file():
            raise CrawlSourceError(f"source {plan.source_id} output reservation is missing")
        return True

    def capture_producer_artifacts(
        self,
        plan: CrawlSourcePlan,
        *,
        root: Path,
    ) -> list[JsonObject]:
        snapshots = self.dynamic_dependencies_by_producer.get(plan.source_id, ())
        expected_sha256_by_path: dict[Path, str] = {}
        artifacts: list[JsonObject] = []
        for snapshot in snapshots:
            expected_sha256 = expected_sha256_by_path.get(snapshot.source_path)
            if expected_sha256 is None:
                source = _safe_dataset_path(
                    root,
                    snapshot.source_path,
                    field=f"source {plan.source_id} produced dependency artifact",
                )
                expected_sha256 = _sha256_file(
                    source,
                    field=f"source {plan.source_id} produced dependency artifact",
                )
                expected_sha256_by_path[source] = expected_sha256
                artifacts.append(
                    {
                        "artifact": str(_relative_to_root(root, source)),
                        "sha256": expected_sha256,
                    }
                )
            _copy_verified_file(
                snapshot.source_path,
                snapshot.target_path,
                expected_sha256=expected_sha256,
                field=f"source {plan.source_id} produced dependency artifact",
            )
        return artifacts

    def command_for(self, plan: CrawlSourcePlan, *, root: Path) -> tuple[str, ...]:
        command = self.commands_by_source_id[plan.source_id]
        for dependency_path in plan.dependency_artifacts:
            target = self.dynamic_dependency_targets.get((plan.source_id, dependency_path))
            if target is None:
                continue
            if not target.is_file():
                raise CrawlSourceError(
                    f"source {plan.source_id} dependency snapshot is missing after its producer run"
                )
            command = _replace_command_path(command, dependency_path, target)
        self.commands_by_source_id[plan.source_id] = command
        return command


_SOURCE_MODELS: Mapping[str, type[_BaseSource]] = {
    ADAPTER_SITEMAP: SitemapSource,
    ADAPTER_COMMONCRAWL: CommonCrawlSource,
    ADAPTER_PUBLIC_ATS: PublicAtsSource,
    ADAPTER_PAGES: PageCollectionSource,
}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path, *, field: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(65_536):
                digest.update(chunk)
    except OSError as error:
        raise CrawlSourceError(f"{field} could not be hashed") from error
    return digest.hexdigest()


def _manifest_sha256(manifest: CrawlSourceManifest) -> str:
    return _sha256_json(
        {
            "version": manifest.version,
            "sources": [source.model_dump(mode="json") for source in manifest.sources],
        }
    )


def _runtime_implementation_paths(*, root: Path, plan: CrawlSourcePlan) -> tuple[Path, ...]:
    primary = _safe_registered_script_path(
        root,
        Path(plan.command[1 + len(_PYTHON_ISOLATION_FLAGS)]),
        field=f"source {plan.source_id} crawler implementation",
    )
    if plan.adapter == ADAPTER_PAGES:
        return (primary,)
    support = _script_path(root, ADAPTER_PAGES)
    return (primary, support)


def _plan_bindings(plans: Sequence[CrawlSourcePlan], *, root: Path) -> list[JsonObject]:
    selected_source_ids = {plan.source_id for plan in plans}

    def dependency_bindings(plan: CrawlSourcePlan) -> list[JsonObject]:
        bindings: list[JsonObject] = []
        for source_id, path in zip(
            plan.dependency_source_ids,
            plan.dependency_artifacts,
            strict=True,
        ):
            dependency_path = _safe_dataset_path(
                root,
                path,
                field=f"source {plan.source_id} dependency artifact",
            )
            binding: JsonObject = {
                "artifact": str(_relative_to_root(root, dependency_path)),
                "source_id": source_id,
            }
            if source_id in selected_source_ids:
                binding["produced_in_execution"] = True
            elif dependency_path.is_file():
                binding["artifact_sha256"] = _sha256_file(
                    dependency_path,
                    field=f"source {plan.source_id} dependency artifact",
                )
            else:
                raise CrawlSourceError(
                    f"source {plan.source_id} dependency artifact is missing for review"
                )
            bindings.append(binding)
        return bindings

    def plan_binding(plan: CrawlSourcePlan) -> JsonObject:
        input_artifact = _safe_dataset_path(
            root,
            plan.input_artifact,
            field=f"source {plan.source_id} input artifact",
        )
        return {
            "adapter": plan.adapter,
            "command_sha256": _sha256_json(plan.command),
            "dependencies": dependency_bindings(plan),
            "enabled": plan.enabled,
            "input_artifact": str(_relative_to_root(root, input_artifact)),
            "input_sha256": _sha256_file(
                input_artifact,
                field=f"source {plan.source_id} input artifact",
            ),
            "implementation_files": [
                {
                    "path": str(_relative_to_root(root, implementation)),
                    "sha256": _sha256_file(
                        implementation,
                        field=f"source {plan.source_id} crawler implementation",
                    ),
                }
                for implementation in _runtime_implementation_paths(root=root, plan=plan)
            ],
            "schedule": plan.schedule,
            "output_dir": str(_relative_to_root(root, plan.output_dir)),
            "source_id": plan.source_id,
            "source_sha256": plan.source_sha256,
        }

    return [plan_binding(plan) for plan in plans]


def _iso_timestamp(value: datetime, *, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CrawlSourceError(f"{field} must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(raw: str, *, field: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise CrawlSourceError(f"{field} must be an ISO-8601 timestamp") from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise CrawlSourceError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_execution_document(
    *,
    request_id: str,
    manifest_sha256: str,
    plan_sha256: str,
    source_ids: Sequence[str],
    requested_at: str,
    expires_at: str,
) -> None:
    try:
        UUID(request_id)
    except (TypeError, ValueError) as error:
        raise ValueError("request_id must be a UUID") from error
    if _SHA256.fullmatch(manifest_sha256) is None:
        raise ValueError("manifest_sha256 must be a lowercase sha256 hex digest")
    if _SHA256.fullmatch(plan_sha256) is None:
        raise ValueError("plan_sha256 must be a lowercase sha256 hex digest")
    frozen_source_ids = tuple(source_ids)
    if len(frozen_source_ids) != len(set(frozen_source_ids)):
        raise ValueError("source_ids must not contain duplicates")
    if any(_SOURCE_ID.fullmatch(source_id) is None for source_id in frozen_source_ids):
        raise ValueError("source_ids must contain valid source identifiers")
    try:
        requested = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("execution document timestamps must be ISO-8601") from error
    if (
        requested.tzinfo is None
        or requested.utcoffset() is None
        or expires.tzinfo is None
        or expires.utcoffset() is None
    ):
        raise ValueError("execution document timestamps must be timezone-aware")
    if expires <= requested:
        raise ValueError("execution document expiry must be after its timestamp")


def create_execution_request(
    manifest: CrawlSourceManifest,
    plans: Sequence[CrawlSourcePlan],
    *,
    root: Path,
    reason: str,
    now: datetime,
    expires_in: timedelta,
) -> CrawlExecutionRequestDocument:
    if not plans:
        raise CrawlSourceError("execution request requires at least one enabled crawler source")
    if not reason.strip() or len(reason) > 500:
        raise CrawlSourceError(
            "execution request reason must be non-empty and at most 500 characters"
        )
    if expires_in < timedelta(hours=1) or expires_in > timedelta(days=7):
        raise CrawlSourceError("execution request expiry must be between one hour and seven days")
    requested_at = _iso_timestamp(now, field="execution request now")
    expires_at = _iso_timestamp(now + expires_in, field="execution request expiry")
    reviewed_plan = _plan_bindings(plans, root=root)
    return CrawlExecutionRequestDocument(
        version=EXECUTION_REQUEST_VERSION,
        kind=EXECUTION_REQUEST_KIND,
        request_id=str(uuid4()),
        manifest_sha256=_manifest_sha256(manifest),
        plan_sha256=_sha256_json(reviewed_plan),
        reviewed_plan=reviewed_plan,
        source_ids=[plan.source_id for plan in plans],
        requested_at=requested_at,
        expires_at=expires_at,
        reason=reason.strip(),
    )


def create_source_registry(
    manifest: CrawlSourceManifest,
    plans: Sequence[CrawlSourcePlan],
    *,
    root: Path,
    manifest_path: Path,
    now: datetime,
) -> CrawlSourceRegistryDocument:
    manifest_binding = {
        "version": manifest.version,
        "sources": [source.model_dump(mode="json") for source in manifest.sources],
    }
    resolved_plans = tuple(plan.as_dict(root=root) for plan in plans)
    registry_binding = {
        "version": SOURCE_REGISTRY_VERSION,
        "kind": SOURCE_REGISTRY_KIND,
        "manifest_path": str(_relative_to_root(root, manifest_path.resolve())),
        "manifest_sha256": _manifest_sha256(manifest),
        "manifest": manifest_binding,
        "resolved_plans": list(resolved_plans),
    }
    return CrawlSourceRegistryDocument(
        version=SOURCE_REGISTRY_VERSION,
        kind=SOURCE_REGISTRY_KIND,
        manifest_path=str(_relative_to_root(root, manifest_path.resolve())),
        manifest_sha256=_manifest_sha256(manifest),
        registry_sha256=_sha256_json(registry_binding),
        generated_at=_iso_timestamp(now, field="source registry now"),
        manifest=cast(JsonObject, manifest_binding),
        resolved_plans=resolved_plans,
    )


def persist_source_registry(
    manifest: CrawlSourceManifest,
    plans: Sequence[CrawlSourcePlan],
    *,
    root: Path,
    registry_document: CrawlSourceRegistryDocument,
    now: datetime,
) -> UUID:
    settings = Settings(database_role=DatabaseCapabilityRole.API)
    engine = create_database_engine(settings)
    try:
        with engine.begin() as connection:
            return PostgresCrawlerSourceRegistryRepository(connection).register_registry(
                manifest,
                plans,
                registry_document,
                root=root,
                created_by="careerops-crawl-sources",
                now=now,
            )
    finally:
        engine.dispose()


def approve_execution_request(
    request: CrawlExecutionRequestDocument,
    *,
    approved_by: str,
    note: str,
    now: datetime,
) -> CrawlExecutionApprovalDocument:
    if _ACTOR.fullmatch(approved_by) is None:
        raise CrawlSourceError("approved_by must be a bounded actor identifier")
    if len(note) > 500:
        raise CrawlSourceError("approval note must be at most 500 characters")
    approved_at = _parse_timestamp(_iso_timestamp(now, field="approval now"), field="approval now")
    requested_at = _parse_timestamp(request.requested_at, field="execution request requested_at")
    expires_at = _parse_timestamp(request.expires_at, field="execution request expires_at")
    if approved_at < requested_at:
        raise CrawlSourceError("execution request is not active yet")
    if approved_at >= expires_at:
        raise CrawlSourceError("execution request has expired")
    return CrawlExecutionApprovalDocument(
        version=EXECUTION_REQUEST_VERSION,
        kind=EXECUTION_APPROVAL_KIND,
        request_id=request.request_id,
        request_sha256=request.fingerprint,
        manifest_sha256=request.manifest_sha256,
        plan_sha256=request.plan_sha256,
        source_ids=list(request.source_ids),
        approved_by=approved_by,
        approved_at=_iso_timestamp(approved_at, field="approval timestamp"),
        expires_at=request.expires_at,
        note=note.strip(),
    )


def verify_execution_approval(
    request: CrawlExecutionRequestDocument,
    approval: CrawlExecutionApprovalDocument,
    *,
    now: datetime,
) -> None:
    current = _parse_timestamp(_iso_timestamp(now, field="execution now"), field="execution now")
    if (
        approval.request_id != request.request_id
        or approval.request_sha256 != request.fingerprint
        or approval.manifest_sha256 != request.manifest_sha256
        or approval.plan_sha256 != request.plan_sha256
        or tuple(approval.source_ids) != tuple(request.source_ids)
        or approval.expires_at != request.expires_at
    ):
        raise CrawlSourceError("execution approval does not match the execution request")
    requested_at = _parse_timestamp(request.requested_at, field="execution request requested_at")
    approved_at = _parse_timestamp(approval.approved_at, field="approval approved_at")
    if approved_at < requested_at:
        raise CrawlSourceError("execution approval predates the execution request")
    if current < approved_at:
        raise CrawlSourceError("execution approval is not active yet")
    if current >= _parse_timestamp(request.expires_at, field="execution request expires_at"):
        raise CrawlSourceError("execution request has expired")
    if current >= _parse_timestamp(approval.expires_at, field="approval expires_at"):
        raise CrawlSourceError("execution approval has expired")


def _safe_validation_message(error: ValidationError) -> str:
    locations = [
        ".".join(str(part) for part in cast(tuple[object, ...], detail["loc"]))
        for detail in error.errors()
    ]
    return f"invalid manifest fields: {', '.join(sorted(set(locations)))}"


def load_manifest(path: Path) -> CrawlSourceManifest:
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CrawlSourceError("crawler manifest file is missing") from error
    except OSError as error:
        raise CrawlSourceError("crawler manifest could not be read") from error
    except json.JSONDecodeError as error:
        raise CrawlSourceError("crawler manifest is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise CrawlSourceError("crawler manifest must be a JSON object")
    manifest_object = cast(dict[str, object], decoded)
    if set(manifest_object) != {"version", "sources"}:
        raise CrawlSourceError("crawler manifest must contain only version and sources")
    if manifest_object.get("version") != MANIFEST_VERSION:
        raise CrawlSourceError(f"crawler manifest version must be {MANIFEST_VERSION}")
    raw_sources_value = manifest_object.get("sources")
    if not isinstance(raw_sources_value, list) or not raw_sources_value:
        raise CrawlSourceError("crawler manifest sources must be a non-empty array")
    raw_sources = cast(list[object], raw_sources_value)
    if len(raw_sources) > 64:
        raise CrawlSourceError("crawler manifest has too many sources")

    sources: list[SourceSpec] = []
    for index, raw_source_value in enumerate(raw_sources):
        if not isinstance(raw_source_value, dict):
            raise CrawlSourceError(f"crawler source at index {index} must be an object")
        raw_source = cast(dict[str, Any], raw_source_value)
        adapter = raw_source.get("adapter")
        model = _SOURCE_MODELS.get(adapter) if isinstance(adapter, str) else None
        if model is None:
            raise CrawlSourceError(f"crawler source at index {index} has an unsupported adapter")
        try:
            source = model.model_validate(raw_source)
            _validate_source_control_caps(cast(SourceSpec, source))
        except ValidationError as error:
            raise CrawlSourceError(
                f"crawler source at index {index}: {_safe_validation_message(error)}"
            ) from error
        except ValueError as error:
            raise CrawlSourceError(f"crawler source at index {index}: {error}") from error
        sources.append(cast(SourceSpec, source))

    ids = [source.source_id for source in sources]
    if len(ids) != len(set(ids)):
        raise CrawlSourceError("crawler source_id values must be unique")
    if any(_SOURCE_ID.fullmatch(source_id) is None for source_id in ids):
        raise CrawlSourceError("crawler source_id is invalid")
    return CrawlSourceManifest(version=MANIFEST_VERSION, sources=tuple(sources))


def _load_execution_document(
    path: Path,
    *,
    label: str,
    model: type[CrawlExecutionRequestDocument] | type[CrawlExecutionApprovalDocument],
) -> CrawlExecutionRequestDocument | CrawlExecutionApprovalDocument:
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CrawlSourceError(f"{label} file is missing") from error
    except OSError as error:
        raise CrawlSourceError(f"{label} could not be read") from error
    except json.JSONDecodeError as error:
        raise CrawlSourceError(f"{label} is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise CrawlSourceError(f"{label} must be a JSON object")
    try:
        return model.model_validate(cast(dict[str, Any], decoded))
    except ValidationError as error:
        raise CrawlSourceError(
            f"invalid {label} fields: {_safe_validation_message(error)}"
        ) from error


def load_execution_request(path: Path) -> CrawlExecutionRequestDocument:
    document = _load_execution_document(
        path,
        label="execution request",
        model=CrawlExecutionRequestDocument,
    )
    if not isinstance(document, CrawlExecutionRequestDocument):
        raise CrawlSourceError("execution request has the wrong document kind")
    return document


def load_execution_approval(path: Path) -> CrawlExecutionApprovalDocument:
    document = _load_execution_document(
        path,
        label="execution approval",
        model=CrawlExecutionApprovalDocument,
    )
    if not isinstance(document, CrawlExecutionApprovalDocument):
        raise CrawlSourceError("execution approval has the wrong document kind")
    return document


def _datasets_root(root: Path) -> Path:
    raw_datasets_root = root / "datasets"
    if raw_datasets_root.is_symlink():
        raise CrawlSourceError("datasets directory must not be a symbolic link")
    if not raw_datasets_root.is_dir():
        raise CrawlSourceError("datasets directory is missing")
    datasets_root = raw_datasets_root.resolve()
    try:
        datasets_root.relative_to(root)
    except ValueError as error:
        raise CrawlSourceError("datasets directory escapes the repository root") from error
    return datasets_root


def _validate_data_path(root: Path, raw_path: str, *, field: str) -> tuple[Path, Path]:
    path = Path(raw_path)
    if path.is_absolute():
        raise CrawlSourceError(f"{field} must be a relative datasets path")
    if not path.parts or path.parts[0] != "datasets" or len(path.parts) < 2:
        raise CrawlSourceError(f"{field} must be inside datasets/")
    current = root
    for part in path.parts:
        if part == "..":
            raise CrawlSourceError(f"{field} must not contain parent path segments")
        current /= part
        if current.is_symlink():
            raise CrawlSourceError(f"{field} must not traverse symbolic links")
    datasets_root = _datasets_root(root)
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(datasets_root)
    except ValueError as error:
        raise CrawlSourceError(f"{field} must stay inside datasets/") from error
    return path, candidate


def _resolve_data_path(root: Path, raw_path: str, *, field: str, must_exist: bool) -> Path:
    _path, candidate = _validate_data_path(root, raw_path, field=field)
    if must_exist and not candidate.is_file():
        raise CrawlSourceError(f"{field} must name an existing regular file")
    if not must_exist and candidate.exists() and not candidate.is_dir():
        raise CrawlSourceError(f"{field} cannot replace a non-directory path")
    return candidate


def _resolve_new_data_file(root: Path, raw_path: str, *, field: str) -> Path:
    path, candidate = _validate_data_path(root, raw_path, field=field)
    if path.suffix != ".json":
        raise CrawlSourceError(f"{field} must use a .json filename")
    if candidate.exists():
        raise CrawlSourceError(f"{field} must name a new file")
    return candidate


def _review_artifacts_root(root: Path) -> Path:
    return root / _REVIEW_ARTIFACT_DIRECTORY


def _safe_review_artifact_path(root: Path, path: Path, *, field: str) -> Path:
    review_root = _review_artifacts_root(root)
    try:
        path.relative_to(review_root)
    except ValueError as error:
        raise CrawlSourceError(
            f"{field} must be inside datasets/private/crawler-execution-reviews/"
        ) from error
    return _safe_dataset_path(root, path, field=field)


def _resolve_review_artifact(root: Path, raw_path: str, *, field: str) -> Path:
    return _safe_review_artifact_path(
        root,
        _resolve_data_path(root, raw_path, field=field, must_exist=True),
        field=field,
    )


def _resolve_new_review_artifact(root: Path, raw_path: str, *, field: str) -> Path:
    return _safe_review_artifact_path(
        root,
        _resolve_new_data_file(root, raw_path, field=field),
        field=field,
    )


def _reject_reserved_execution_output_dir(root: Path, output_dir: Path, *, field: str) -> None:
    for directory_name in _EXECUTION_PRIVATE_DIRECTORIES:
        reserved_directory = root / "datasets" / "private" / directory_name
        try:
            output_dir.relative_to(reserved_directory)
        except ValueError:
            continue
        raise CrawlSourceError(
            f"{field} must not be inside {reserved_directory.relative_to(root)}/"
        )


def _safe_dataset_path(root: Path, path: Path, *, field: str) -> Path:
    datasets_root = _datasets_root(root)
    try:
        path.relative_to(datasets_root)
    except ValueError as error:
        raise CrawlSourceError(f"{field} must stay inside datasets/") from error
    if path.is_symlink():
        raise CrawlSourceError(f"{field} must not be a symbolic link")
    resolved = path.resolve()
    try:
        resolved.relative_to(datasets_root)
    except ValueError as error:
        raise CrawlSourceError(f"{field} must stay inside datasets/") from error
    if resolved != path:
        raise CrawlSourceError(f"{field} must not traverse symbolic links")
    return path


def _safe_registered_script_path(root: Path, path: Path, *, field: str) -> Path:
    raw_scripts_root = root / "scripts"
    if raw_scripts_root.is_symlink():
        raise CrawlSourceError("scripts directory must not be a symbolic link")
    scripts_root = raw_scripts_root.resolve()
    try:
        path.relative_to(scripts_root)
    except ValueError as error:
        raise CrawlSourceError(f"{field} must stay inside scripts/") from error
    if path.is_symlink():
        raise CrawlSourceError(f"{field} must not be a symbolic link")
    resolved = path.resolve()
    try:
        resolved.relative_to(scripts_root)
    except ValueError as error:
        raise CrawlSourceError(f"{field} must stay inside scripts/") from error
    if resolved != path:
        raise CrawlSourceError(f"{field} must not traverse symbolic links")
    return path


def _replace_command_path(
    command: Sequence[str],
    original: Path,
    replacement: Path,
) -> tuple[str, ...]:
    original_value = str(original)
    replacement_value = str(replacement)
    return tuple(replacement_value if item == original_value else item for item in command)


def _copy_verified_file(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    field: str,
) -> None:
    if _SHA256.fullmatch(expected_sha256) is None:
        raise CrawlSourceError(f"{field} has an invalid expected sha256")
    if not source.is_file():
        raise CrawlSourceError(f"{field} is missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
            while chunk := source_handle.read(65_536):
                digest.update(chunk)
                destination_handle.write(chunk)
        os.chmod(destination, 0o400)
    except FileExistsError as error:
        raise CrawlSourceError("execution snapshot already exists") from error
    except OSError as error:
        raise CrawlSourceError(f"{field} could not be snapshotted") from error
    if (
        digest.hexdigest() == expected_sha256
        and _sha256_file(source, field=field) == expected_sha256
    ):
        return
    with suppress(OSError):
        destination.unlink()
    raise CrawlSourceError(f"{field} changed while its execution snapshot was created")


def stage_execution_snapshot(
    manifest: CrawlSourceManifest,
    plans: Sequence[CrawlSourcePlan],
    bindings: Sequence[JsonObject],
    *,
    root: Path,
    request_id: str,
) -> CrawlExecutionSnapshot:
    if len(plans) != len(bindings):
        raise CrawlSourceError("execution snapshot bindings do not match the selected plans")
    snapshot_parent = root / "datasets" / "private" / "crawler-execution-snapshots" / request_id
    _safe_dataset_path(root, snapshot_parent, field="execution snapshot directory")
    snapshot_directory = snapshot_parent / uuid4().hex
    _safe_dataset_path(root, snapshot_directory, field="execution snapshot directory")
    try:
        snapshot_directory.mkdir(parents=True, exist_ok=False)
        os.chmod(snapshot_parent, 0o700)
        os.chmod(snapshot_directory, 0o700)
    except FileExistsError as error:
        raise CrawlSourceError("execution snapshot already exists") from error
    except OSError as error:
        raise CrawlSourceError("execution snapshot directory could not be created") from error

    selected_source_ids = {plan.source_id for plan in plans}
    input_snapshots: dict[Path, Path] = {}
    implementation_snapshots: dict[Path, Path] = {}
    external_dependency_snapshots: dict[Path, Path] = {}
    dynamic_dependency_targets: dict[tuple[str, Path], Path] = {}
    dynamic_dependencies_by_producer: dict[str, list[_DynamicDependencySnapshot]] = {}
    commands_by_source_id: dict[str, tuple[str, ...]] = {}

    for plan, binding in zip(plans, bindings, strict=True):
        input_artifact = _safe_dataset_path(
            root,
            plan.input_artifact,
            field=f"source {plan.source_id} input artifact",
        )
        input_sha256 = cast(str, binding["input_sha256"])
        input_snapshot = input_snapshots.get(input_artifact)
        if input_snapshot is None:
            input_path_sha256 = _sha256_json(str(_relative_to_root(root, input_artifact)))
            input_snapshot = (
                snapshot_directory / "inputs" / f"{input_path_sha256[:16]}-{input_sha256}.jsonl"
            )
            _copy_verified_file(
                input_artifact,
                input_snapshot,
                expected_sha256=input_sha256,
                field=f"source {plan.source_id} input artifact",
            )
            input_snapshots[input_artifact] = input_snapshot

        command = _replace_command_path(plan.command, input_artifact, input_snapshot)
        implementation_files = cast(list[JsonObject], binding["implementation_files"])
        implementations = _runtime_implementation_paths(root=root, plan=plan)
        if len(implementation_files) != len(implementations):
            raise CrawlSourceError("execution snapshot implementation bindings are invalid")
        for implementation, implementation_binding in zip(
            implementations,
            implementation_files,
            strict=True,
        ):
            expected_sha256 = cast(str, implementation_binding["sha256"])
            implementation_snapshot = implementation_snapshots.get(implementation)
            if implementation_snapshot is None:
                implementation_snapshot = snapshot_directory / "scripts" / implementation.name
                _copy_verified_file(
                    implementation,
                    implementation_snapshot,
                    expected_sha256=expected_sha256,
                    field=f"source {plan.source_id} crawler implementation",
                )
                implementation_snapshots[implementation] = implementation_snapshot
            command = _replace_command_path(command, implementation, implementation_snapshot)

        dependency_bindings = cast(list[JsonObject], binding["dependencies"])
        if len(dependency_bindings) != len(plan.dependency_artifacts):
            raise CrawlSourceError("execution snapshot dependency bindings are invalid")
        for index, (dependency_path, dependency_binding) in enumerate(
            zip(plan.dependency_artifacts, dependency_bindings, strict=True)
        ):
            dependency_source_id = cast(str, dependency_binding["source_id"])
            if dependency_source_id in selected_source_ids:
                dependency = _safe_dataset_path(
                    root,
                    dependency_path,
                    field=f"source {plan.source_id} dependency artifact",
                )
                if dependency.exists():
                    raise CrawlSourceError(
                        f"source {plan.source_id} dependency artifact must be absent before "
                        "a reviewed execution"
                    )
                target = (
                    snapshot_directory
                    / "dependencies"
                    / f"{plan.source_id}-{index}-{dependency_source_id}.jsonl"
                )
                dynamic_dependency_targets[(plan.source_id, dependency_path)] = target
                dynamic_dependencies_by_producer.setdefault(dependency_source_id, []).append(
                    _DynamicDependencySnapshot(
                        producer_source_id=dependency_source_id,
                        consumer_source_id=plan.source_id,
                        source_path=dependency,
                        target_path=target,
                    )
                )
                continue
            expected_sha256 = cast(str, dependency_binding["artifact_sha256"])
            dependency_snapshot = external_dependency_snapshots.get(dependency_path)
            if dependency_snapshot is None:
                dependency = _safe_dataset_path(
                    root,
                    dependency_path,
                    field=f"source {plan.source_id} dependency artifact",
                )
                dependency_snapshot = (
                    snapshot_directory
                    / "dependencies"
                    / f"{plan.source_id}-{index}-{expected_sha256}.jsonl"
                )
                _copy_verified_file(
                    dependency,
                    dependency_snapshot,
                    expected_sha256=expected_sha256,
                    field=f"source {plan.source_id} dependency artifact",
                )
                external_dependency_snapshots[dependency_path] = dependency_snapshot
            command = _replace_command_path(command, dependency_path, dependency_snapshot)
        commands_by_source_id[plan.source_id] = command

    reviewed_plan_path = snapshot_directory / "reviewed-plan.json"
    _write_new_json(
        reviewed_plan_path,
        {
            "manifest": {
                "sources": [source.model_dump(mode="json") for source in manifest.sources],
                "version": manifest.version,
            },
            "manifest_sha256": _manifest_sha256(manifest),
            "plan_bindings": list(bindings),
            "plan_sha256": _sha256_json(bindings),
            "request_id": request_id,
            "source_ids": [plan.source_id for plan in plans],
            "version": EXECUTION_REQUEST_VERSION,
        },
    )
    reviewed_plan_sha256 = _sha256_file(
        reviewed_plan_path,
        field="execution snapshot reviewed plan",
    )
    try:
        os.chmod(snapshot_directory, 0o700)
        os.chmod(reviewed_plan_path, 0o400)
    except OSError as error:
        raise CrawlSourceError("execution snapshot permissions could not be set") from error

    return CrawlExecutionSnapshot(
        directory=snapshot_directory,
        reviewed_plan_path=reviewed_plan_path,
        reviewed_plan_sha256=reviewed_plan_sha256,
        commands_by_source_id=commands_by_source_id,
        dynamic_dependency_targets=dynamic_dependency_targets,
        dynamic_dependencies_by_producer={
            source_id: tuple(snapshots)
            for source_id, snapshots in dynamic_dependencies_by_producer.items()
        },
        fresh_output_source_ids=frozenset(dynamic_dependencies_by_producer),
        output_reservations={},
        output_locks={},
    )


def _relative_to_root(root: Path, path: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def _script_path(root: Path, adapter: str) -> Path:
    script = root / "scripts" / _ADAPTER_SCRIPTS[adapter]
    if not script.is_file():
        raise CrawlSourceError("registered crawler implementation is missing")
    return _safe_registered_script_path(
        root,
        script,
        field="registered crawler implementation",
    )


def _command_prefix(script: Path, input_artifact: Path, output_dir: Path) -> list[str]:
    return [
        sys.executable,
        *_PYTHON_ISOLATION_FLAGS,
        str(script),
        "--triage-input",
        str(input_artifact),
        "--output-dir",
        str(output_dir),
    ]


def _option(command: list[str], name: str, value: int | float) -> None:
    command.extend([f"--{name.replace('_', '-')}", str(value)])


def _command_for_source(
    *,
    root: Path,
    source: SourceSpec,
    input_artifact: Path,
    output_dir: Path,
    dependency_artifacts: tuple[Path, ...],
) -> tuple[str, ...]:
    command = _command_prefix(_script_path(root, source.adapter), input_artifact, output_dir)
    execution_retry_rounds = min(source.limits.retry_rounds, source.schedule.retry_rounds)
    if isinstance(source, SitemapSource):
        _option(
            command,
            "concurrency",
            _effective_control_value(source, "concurrency", source.schedule.rate_limits),
        )
        _option(command, "retry_rounds", execution_retry_rounds)
        _option(
            command,
            "timeout_seconds",
            _effective_control_value(source, "timeout_seconds", source.schedule.budget),
        )
        _option(
            command,
            "max_sitemap_bytes",
            _effective_control_value(source, "max_sitemap_bytes", source.schedule.budget),
        )
    elif isinstance(source, CommonCrawlSource):
        _option(
            command,
            "concurrency",
            _effective_control_value(source, "concurrency", source.schedule.rate_limits),
        )
        _option(command, "retry_rounds", execution_retry_rounds)
        for field in (
            "timeout_seconds",
            "max_indexes",
            "max_results_per_query",
            "max_discovered_urls",
        ):
            _option(
                command,
                field,
                _effective_control_value(source, field, source.schedule.budget),
            )
        _option(
            command,
            "provider_circuit_breaker_failures",
            _effective_control_value(
                source,
                "provider_circuit_breaker_failures",
                source.schedule.rate_limits,
            ),
        )
        _option(
            command,
            "retry_backoff_seconds",
            _effective_control_value(
                source,
                "retry_backoff_seconds",
                source.schedule.rate_limits,
            ),
        )
        _option(
            command,
            "max_retry_backoff_seconds",
            _effective_control_value(
                source,
                "max_retry_backoff_seconds",
                source.schedule.rate_limits,
            ),
        )
    elif isinstance(source, PublicAtsSource):
        _option(
            command,
            "concurrency",
            _effective_control_value(source, "concurrency", source.schedule.rate_limits),
        )
        _option(command, "retry_rounds", execution_retry_rounds)
        _option(
            command,
            "timeout_seconds",
            _effective_control_value(source, "timeout_seconds", source.schedule.budget),
        )
        _option(
            command,
            "max_feed_bytes",
            _effective_control_value(source, "max_feed_bytes", source.schedule.budget),
        )
    else:
        for dependency in dependency_artifacts:
            command.extend(["--additional-seeds", str(dependency)])
        _option(
            command,
            "concurrency",
            _effective_control_value(source, "concurrency", source.schedule.rate_limits),
        )
        _option(command, "retry_rounds", execution_retry_rounds)
        for field in ("timeout_seconds", "max_stored_bytes", "max_response_bytes"):
            _option(
                command,
                field,
                _effective_control_value(source, field, source.schedule.budget),
            )
        _option(
            command,
            "max_concurrency_per_host",
            _effective_control_value(
                source,
                "max_concurrency_per_host",
                source.schedule.rate_limits,
            ),
        )
        _option(
            command,
            "host_failure_cooldown_threshold",
            _effective_control_value(
                source,
                "host_failure_cooldown_threshold",
                source.schedule.rate_limits,
            ),
        )
        _option(
            command,
            "host_failure_cooldown_seconds",
            _effective_control_value(
                source,
                "host_failure_cooldown_seconds",
                source.schedule.rate_limits,
            ),
        )
        _option(
            command,
            "depth",
            _effective_control_value(source, "depth", source.schedule.budget),
        )
    if (
        isinstance(source, (SitemapSource, CommonCrawlSource, PageCollectionSource))
        and not source.resume
    ):
        command.append("--no-resume")
    return tuple(command)


def plan_manifest(manifest: CrawlSourceManifest, *, root: Path) -> tuple[CrawlSourcePlan, ...]:
    root = root.resolve()
    if not root.is_dir():
        raise CrawlSourceError("repository root must be an existing directory")
    source_by_id = {source.source_id: source for source in manifest.sources}
    plans: list[CrawlSourcePlan] = []
    output_paths: set[Path] = set()
    prior_source_ids: set[str] = set()

    for source in manifest.sources:
        input_artifact = _resolve_data_path(
            root,
            source.input_artifact,
            field=f"source {source.source_id} input_artifact",
            must_exist=True,
        )
        output_dir = _resolve_data_path(
            root,
            source.output_dir,
            field=f"source {source.source_id} output_dir",
            must_exist=False,
        )
        _reject_reserved_execution_output_dir(
            root,
            output_dir,
            field=f"source {source.source_id} output_dir",
        )
        if output_dir in output_paths:
            raise CrawlSourceError("crawler sources must use distinct output_dir values")
        if any(
            output_dir.is_relative_to(existing_output) or existing_output.is_relative_to(output_dir)
            for existing_output in output_paths
        ):
            raise CrawlSourceError("crawler source output_dir values must not overlap")
        if output_dir == input_artifact:
            raise CrawlSourceError("crawler output_dir cannot be the input artifact")
        output_paths.add(output_dir)

        dependency_artifacts: tuple[Path, ...] = ()
        dependency_source_ids: tuple[str, ...] = ()
        if isinstance(source, PageCollectionSource):
            dependencies: list[Path] = []
            dependency_ids: list[str] = []
            if len(source.additional_seed_sources) != len(set(source.additional_seed_sources)):
                raise CrawlSourceError("additional_seed_sources must be unique")
            for dependency_id in source.additional_seed_sources:
                dependency = source_by_id.get(dependency_id)
                if dependency is None:
                    raise CrawlSourceError("additional_seed_sources references an unknown source")
                if dependency_id not in prior_source_ids:
                    raise CrawlSourceError(
                        "additional_seed_sources must reference an earlier source"
                    )
                filename = _SEED_OUTPUT_FILENAMES.get(dependency.adapter)
                if filename is None:
                    raise CrawlSourceError(
                        "additional_seed_sources must reference a seed discovery source"
                    )
                dependency_output = _resolve_data_path(
                    root,
                    dependency.output_dir,
                    field=f"source {dependency_id} output_dir",
                    must_exist=False,
                )
                dependencies.append(
                    _safe_dataset_path(
                        root,
                        dependency_output / filename,
                        field=f"source {dependency_id} dependency artifact",
                    )
                )
                dependency_ids.append(dependency_id)
            dependency_artifacts = tuple(dependencies)
            dependency_source_ids = tuple(dependency_ids)

        plans.append(
            CrawlSourcePlan(
                source_id=source.source_id,
                adapter=source.adapter,
                enabled=source.enabled,
                input_artifact=input_artifact,
                output_dir=output_dir,
                schedule=_effective_schedule(source),
                dependency_artifacts=dependency_artifacts,
                dependency_source_ids=dependency_source_ids,
                command=_command_for_source(
                    root=root,
                    source=source,
                    input_artifact=input_artifact,
                    output_dir=output_dir,
                    dependency_artifacts=dependency_artifacts,
                ),
                source_sha256=_sha256_json(source.model_dump(mode="json")),
            )
        )
        prior_source_ids.add(source.source_id)
    return tuple(plans)


def _default_runner(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    # Commands come only from the registered adapter table; shell remains disabled.
    return subprocess.run(  # nosec B603
        list(args),
        cwd=cwd,
        env=dict(env),
        check=False,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _crawler_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environment.items()
        if key in _CHILD_ENV_NAMES and key.lower() not in _PROXY_ENV_NAMES
    }


def _write_json_atomic(path: Path, value: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_new_json(
    path: Path,
    value: JsonObject,
    *,
    already_exists_message: str = "execution artifact already exists",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except FileExistsError as error:
        raise CrawlSourceError(already_exists_message) from error
    except OSError as error:
        raise CrawlSourceError("execution artifact could not be written") from error


def _execution_claim_path(root: Path, request_id: str) -> Path:
    claim_path = root / "datasets" / "private" / "crawler-execution-claims" / f"{request_id}.json"
    _safe_dataset_path(root, claim_path, field="execution claim")
    return claim_path


def _claim_execution_request(
    request: CrawlExecutionRequestDocument,
    approval: CrawlExecutionApprovalDocument,
    *,
    root: Path,
    now: datetime,
    snapshot: CrawlExecutionSnapshot,
) -> Path:
    claim_path = _execution_claim_path(root, request.request_id)
    _write_new_json(
        claim_path,
        {
            "approved_by": approval.approved_by,
            "approval_sha256": _sha256_json(approval.model_dump(mode="json")),
            "claimed_at": _iso_timestamp(now, field="execution claim now"),
            "execution_snapshot": str(_relative_to_root(root, snapshot.directory)),
            "reviewed_plan": str(_relative_to_root(root, snapshot.reviewed_plan_path)),
            "reviewed_plan_sha256": snapshot.reviewed_plan_sha256,
            "kind": EXECUTION_CLAIM_KIND,
            "manifest_sha256": request.manifest_sha256,
            "plan_sha256": request.plan_sha256,
            "request_id": request.request_id,
            "request_sha256": request.fingerprint,
            "source_ids": list(request.source_ids),
            "version": EXECUTION_REQUEST_VERSION,
        },
        already_exists_message="execution request has already been claimed",
    )
    return claim_path


def run_plans(
    plans: Sequence[CrawlSourcePlan],
    *,
    root: Path,
    environment: Mapping[str, str] | None = None,
    runner: Runner = _default_runner,
    execution_request_id: str | None = None,
    execution_snapshot: CrawlExecutionSnapshot | None = None,
) -> tuple[CrawlSourceRunResult, ...]:
    root = root.resolve()
    if any(not plan.enabled for plan in plans):
        raise CrawlSourceError("disabled crawler source must be filtered before execution")
    receipt_context: JsonObject = {}
    if execution_request_id is not None:
        try:
            UUID(execution_request_id)
        except (TypeError, ValueError) as error:
            raise CrawlSourceError("execution_request_id must be a UUID") from error
        receipt_context["execution_request_id"] = execution_request_id
    if execution_snapshot is not None:
        receipt_context["execution_snapshot"] = str(
            _relative_to_root(root, execution_snapshot.directory)
        )
        receipt_context["reviewed_plan_sha256"] = execution_snapshot.reviewed_plan_sha256
    execution_environment = _crawler_environment(os.environ if environment is None else environment)
    planned_source_ids = {plan.source_id for plan in plans}
    source_statuses: dict[str, str] = {}
    results: list[CrawlSourceRunResult] = []
    for plan in plans:
        output_dir = _safe_dataset_path(
            root,
            plan.output_dir,
            field=f"source {plan.source_id} output_dir",
        )
        if execution_snapshot is None:
            output_dir.mkdir(parents=True, exist_ok=True)
        else:
            execution_snapshot.require_output_lock(plan)
            if not execution_snapshot.require_fresh_output_reservation(plan, root=root):
                output_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = output_dir / RECEIPT_FILENAME
        started_at = _timestamp()
        selected_producers = tuple(
            source_id for source_id in plan.dependency_source_ids if source_id in planned_source_ids
        )
        incomplete_producers = tuple(
            source_id
            for source_id in selected_producers
            if source_statuses.get(source_id) != "completed"
        )
        if incomplete_producers:
            _write_json_atomic(
                receipt_path,
                {
                    "adapter": plan.adapter,
                    "command_sha256": _sha256_json(plan.command),
                    "completed_at": _timestamp(),
                    "incomplete_producer_count": len(incomplete_producers),
                    "source_id": plan.source_id,
                    "source_sha256": plan.source_sha256,
                    "started_at": started_at,
                    "status": "blocked_upstream_source",
                    **receipt_context,
                },
            )
            results.append(
                CrawlSourceRunResult(
                    source_id=plan.source_id,
                    adapter=plan.adapter,
                    status="blocked_upstream_source",
                    returncode=None,
                    receipt_path=receipt_path,
                )
            )
            source_statuses[plan.source_id] = "blocked_upstream_source"
            continue
        dependency_artifacts = tuple(
            _safe_dataset_path(
                root,
                path,
                field=f"source {plan.source_id} dependency artifact",
            )
            for path in plan.dependency_artifacts
        )
        missing_dependencies = [path for path in dependency_artifacts if not path.is_file()]
        if missing_dependencies:
            _write_json_atomic(
                receipt_path,
                {
                    "adapter": plan.adapter,
                    "command_sha256": _sha256_json(plan.command),
                    "completed_at": _timestamp(),
                    "missing_dependency_count": len(missing_dependencies),
                    "source_id": plan.source_id,
                    "source_sha256": plan.source_sha256,
                    "started_at": started_at,
                    "status": "blocked_missing_dependency",
                    **receipt_context,
                },
            )
            results.append(
                CrawlSourceRunResult(
                    source_id=plan.source_id,
                    adapter=plan.adapter,
                    status="blocked_missing_dependency",
                    returncode=None,
                    receipt_path=receipt_path,
                )
            )
            source_statuses[plan.source_id] = "blocked_missing_dependency"
            continue
        command = (
            execution_snapshot.command_for(plan, root=root)
            if execution_snapshot is not None
            else plan.command
        )
        try:
            completed = runner(command, cwd=root, env=execution_environment)
        except OSError as error:
            _write_json_atomic(
                receipt_path,
                {
                    "adapter": plan.adapter,
                    "command_sha256": _sha256_json(command),
                    "completed_at": _timestamp(),
                    "error_type": type(error).__name__,
                    "source_id": plan.source_id,
                    "source_sha256": plan.source_sha256,
                    "started_at": started_at,
                    "status": "execution_error",
                    **receipt_context,
                },
            )
            results.append(
                CrawlSourceRunResult(
                    source_id=plan.source_id,
                    adapter=plan.adapter,
                    status="execution_error",
                    returncode=None,
                    receipt_path=receipt_path,
                )
            )
            source_statuses[plan.source_id] = "execution_error"
            continue
        status = "completed" if completed.returncode == 0 else "failed"
        produced_dependency_artifacts: list[JsonObject] = []
        output_snapshot_error: str | None = None
        if status == "completed" and execution_snapshot is not None:
            try:
                produced_dependency_artifacts = execution_snapshot.capture_producer_artifacts(
                    plan,
                    root=root,
                )
            except CrawlSourceError as error:
                status = "failed_output_snapshot"
                output_snapshot_error = type(error).__name__
        _write_json_atomic(
            receipt_path,
            {
                "adapter": plan.adapter,
                "command_sha256": _sha256_json(command),
                "completed_at": _timestamp(),
                "returncode": completed.returncode,
                "produced_dependency_artifacts": produced_dependency_artifacts,
                "source_id": plan.source_id,
                "source_sha256": plan.source_sha256,
                "started_at": started_at,
                "status": status,
                **(
                    {"output_snapshot_error": output_snapshot_error}
                    if output_snapshot_error is not None
                    else {}
                ),
                **receipt_context,
            },
        )
        results.append(
            CrawlSourceRunResult(
                source_id=plan.source_id,
                adapter=plan.adapter,
                status=status,
                returncode=completed.returncode,
                receipt_path=receipt_path,
            )
        )
        source_statuses[plan.source_id] = status
    return tuple(results)


def _select_plans(
    plans: Sequence[CrawlSourcePlan], source_ids: Sequence[str]
) -> tuple[CrawlSourcePlan, ...]:
    if not source_ids:
        return tuple(plan for plan in plans if plan.enabled)
    requested = set(source_ids)
    known = {plan.source_id for plan in plans}
    unknown = requested - known
    if unknown:
        raise CrawlSourceError("requested crawler source is not present in the manifest")
    selected = tuple(plan for plan in plans if plan.source_id in requested)
    if any(not plan.enabled for plan in selected):
        raise CrawlSourceError("requested crawler source is disabled in the manifest")
    return selected


def select_plans_for_review(
    plans: Sequence[CrawlSourcePlan],
    source_ids: Sequence[str],
) -> tuple[CrawlSourcePlan, ...]:
    """Select enabled manifest plans for a reviewed execution request.

    This is the public, bounded selector for higher-level control-plane wrappers. The caller still
    cannot provide URLs, commands, script paths, or output paths; those remain owned by the parsed
    manifest and registered adapter table.
    """

    return _select_plans(plans, source_ids)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root containing scripts/ and datasets/ (default: current directory)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan", "request"):
        subcommand = subcommands.add_parser(name)
        subcommand.add_argument("--config", type=Path, required=True)
        subcommand.add_argument(
            "--source",
            action="append",
            default=[],
            dest="source_ids",
            help="source_id to include; repeat to select several sources",
        )
        subcommand.add_argument("--json", action="store_true", help="emit deterministic JSON")
        if name == "request":
            subcommand.add_argument(
                "--request-output",
                required=True,
                help="new JSON file under datasets/private/crawler-execution-reviews/",
            )
            subcommand.add_argument(
                "--reason",
                required=True,
                help="bounded operator reason for the reviewed crawl",
            )
            subcommand.add_argument(
                "--expires-in-hours",
                type=int,
                default=24,
                help="approval lifetime from 1 through 168 hours (default: 24)",
            )

    register = subcommands.add_parser("register")
    register.add_argument("--config", type=Path, required=True)
    register.add_argument(
        "--output",
        type=Path,
        help=(
            "new JSON file under datasets/private/recruitment/source-registry/ "
            "(default: manifest-hash keyed snapshot)"
        ),
    )
    register.add_argument("--json", action="store_true", help="emit deterministic JSON")

    due = subcommands.add_parser("due")
    due.add_argument("--manifest-sha256", required=True)
    due.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum due source rows to return (default: 100)",
    )
    due.add_argument("--json", action="store_true", help="emit deterministic JSON")

    approve = subcommands.add_parser("approve")
    approve.add_argument(
        "--request",
        required=True,
        help="existing request under datasets/private/crawler-execution-reviews/",
    )
    approve.add_argument(
        "--approval-output",
        required=True,
        help="new JSON file under datasets/private/crawler-execution-reviews/",
    )
    approve.add_argument("--approved-by", required=True, help="local reviewer identifier")
    approve.add_argument("--note", default="", help="optional bounded reviewer note")
    approve.add_argument("--json", action="store_true", help="emit deterministic JSON")

    execute = subcommands.add_parser("execute")
    execute.add_argument("--config", type=Path, required=True)
    execute.add_argument(
        "--request",
        required=True,
        help="existing request under datasets/private/crawler-execution-reviews/",
    )
    execute.add_argument(
        "--approval",
        required=True,
        help="existing approval under datasets/private/crawler-execution-reviews/",
    )
    execute.add_argument("--json", action="store_true", help="emit deterministic JSON")
    return parser


def _emit(payload: JsonObject, *, json_output: bool) -> None:
    if json_output:
        print(_stable_json(payload))
        return
    if payload.get("status") == "validated":
        print(f"validated {payload['source_count']} crawler source(s)")
    elif payload.get("status") == "planned":
        print(f"planned {payload['source_count']} crawler source(s)")
    elif payload.get("status") == "review_requested":
        print(f"created execution review request {payload['request_id']}")
    elif payload.get("status") == "registered":
        print(f"registered crawler source registry {payload['registry_sha256']}")
    elif payload.get("status") == "approved":
        print(f"approved execution request {payload['request_id']}")
    else:
        print(f"ran {payload['source_count']} crawler source(s): {payload['status']}")


def _emit_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(_stable_json({"errors": [message], "ok": False}), file=sys.stderr)
    else:
        print(f"ERROR: {message}", file=sys.stderr)


def _registry_source_row_payload(row: RowMapping | Mapping[str, Any]) -> JsonObject:
    return {
        "adapter": row["adapter"],
        "budget": row["budget_json"],
        "canonical_ingestion_policy": row["canonical_ingestion_policy"],
        "cadence_seconds": row["cadence_seconds"],
        "command_sha256": row["command_sha256"],
        "created_at": cast(datetime, row["created_at"]).isoformat(),
        "cursor": row["cursor"],
        "dependency_artifacts": row["dependency_artifacts"],
        "dependency_source_ids": row["dependency_source_ids"],
        "dedupe_policy": row["dedupe_policy"],
        "enabled": row["enabled"],
        "input_artifact": row["input_artifact"],
        "last_error": row["last_error"],
        "last_result": row["last_result"],
        "last_run_at": row["last_run_at"].isoformat() if row["last_run_at"] is not None else None,
        "next_run_at": row["next_run_at"].isoformat() if row["next_run_at"] is not None else None,
        "output_dir": row["output_dir"],
        "provenance_policy": row["provenance_policy"],
        "rate_limits": row["rate_limit_json"],
        "registry_id": str(row["registry_id"]),
        "robots_terms_policy": row["robots_terms_policy"],
        "retry_rounds": row["retry_rounds"],
        "source_id": row["source_id"],
        "source_sha256": row["source_sha256"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = args.root.resolve()
        if args.command == "approve":
            request_path = _resolve_review_artifact(
                root,
                args.request,
                field="execution request",
            )
            approval_path = _resolve_new_review_artifact(
                root,
                args.approval_output,
                field="approval output",
            )
            request = load_execution_request(request_path)
            approval = approve_execution_request(
                request,
                approved_by=args.approved_by,
                note=args.note,
                now=datetime.now(UTC),
            )
            _write_new_json(approval_path, approval.model_dump(mode="json"))
            _emit(
                {
                    "approval": str(_relative_to_root(root, approval_path)),
                    "approved_by": approval.approved_by,
                    "request_id": approval.request_id,
                    "status": "approved",
                },
                json_output=args.json,
            )
            return 0

        manifest = load_manifest(args.config)
        all_plans = plan_manifest(manifest, root=root)
        if args.command == "register":
            registry_document = create_source_registry(
                manifest,
                all_plans,
                root=root,
                manifest_path=args.config,
                now=datetime.now(UTC),
            )
            registry_output = (
                str(cast(Path, args.output))
                if args.output is not None
                else str(
                    Path("datasets")
                    / "private"
                    / "recruitment"
                    / "source-registry"
                    / f"{registry_document.manifest_sha256}.json"
                )
            )
            registry_path = _resolve_new_data_file(
                root,
                registry_output,
                field="source registry output",
            )
            _write_new_json(
                registry_path,
                {
                    "version": registry_document.version,
                    "kind": registry_document.kind,
                    "manifest_path": registry_document.manifest_path,
                    "manifest_sha256": registry_document.manifest_sha256,
                    "registry_sha256": registry_document.registry_sha256,
                    "generated_at": registry_document.generated_at,
                    "manifest": registry_document.manifest,
                    "resolved_plans": list(registry_document.resolved_plans),
                },
            )
            database_registry_id = persist_source_registry(
                manifest,
                all_plans,
                root=root,
                registry_document=registry_document,
                now=datetime.now(UTC),
            )
            _emit(
                {
                    "database_registry_id": str(database_registry_id),
                    "manifest_sha256": registry_document.manifest_sha256,
                    "registry": str(_relative_to_root(root, registry_path)),
                    "registry_sha256": registry_document.registry_sha256,
                    "source_count": len(all_plans),
                    "status": "registered",
                },
                json_output=args.json,
            )
            return 0
        if args.command == "due":
            settings = Settings(database_role=DatabaseCapabilityRole.API)
            engine = create_database_engine(settings)
            try:
                with engine.begin() as connection:
                    repository = PostgresCrawlerSourceRegistryRepository(connection)
                    registry = repository.get_registry_by_manifest_sha256(args.manifest_sha256)
                    if registry is None:
                        raise CrawlSourceError("source registry was not found")
                    due_sources = repository.list_due_sources(
                        UUID(str(registry["id"])),
                        now=datetime.now(UTC),
                        limit=args.limit,
                    )
                    _emit(
                        {
                            "due_count": len(due_sources),
                            "due_sources": [
                                _registry_source_row_payload(row) for row in due_sources
                            ],
                            "manifest_sha256": args.manifest_sha256,
                            "registry_id": str(registry["id"]),
                            "status": "due",
                        },
                        json_output=args.json,
                    )
                    return 0
            finally:
                engine.dispose()
        if args.command == "execute":
            request_path = _resolve_review_artifact(
                root,
                args.request,
                field="execution request",
            )
            approval_path = _resolve_review_artifact(
                root,
                args.approval,
                field="execution approval",
            )
            request = load_execution_request(request_path)
            approval = load_execution_approval(approval_path)
            plans = _select_plans(all_plans, request.source_ids)
            if request.manifest_sha256 != _manifest_sha256(manifest):
                raise CrawlSourceError("execution request no longer matches the crawler manifest")
            bindings = _plan_bindings(plans, root=root)
            if request.plan_sha256 != _sha256_json(bindings) or bindings != request.reviewed_plan:
                raise CrawlSourceError("execution request no longer matches the crawler plan")
            now = datetime.now(UTC)
            verify_execution_approval(request, approval, now=now)
            if _execution_claim_path(root, request.request_id).exists():
                raise CrawlSourceError("execution request has already been claimed")
            snapshot = stage_execution_snapshot(
                manifest,
                plans,
                request.reviewed_plan,
                root=root,
                request_id=request.request_id,
            )
            claim_path = _claim_execution_request(
                request,
                approval,
                root=root,
                now=now,
                snapshot=snapshot,
            )
            snapshot.reserve_output_directories(
                plans,
                root=root,
                request_id=request.request_id,
            )
            try:
                results = run_plans(
                    plans,
                    root=root,
                    execution_request_id=request.request_id,
                    execution_snapshot=snapshot,
                )
            finally:
                snapshot.release_output_locks()
            succeeded = all(result.status == "completed" for result in results)
            _emit(
                {
                    "execution_claim": str(_relative_to_root(root, claim_path)),
                    "execution_snapshot": str(_relative_to_root(root, snapshot.directory)),
                    "request_id": request.request_id,
                    "results": [result.as_dict(root=root) for result in results],
                    "source_count": len(results),
                    "status": "completed" if succeeded else "failed",
                },
                json_output=args.json,
            )
            return 0 if succeeded else 1

        plans = _select_plans(all_plans, args.source_ids)
        if args.command == "validate":
            _emit(
                {
                    "source_count": len(plans),
                    "source_ids": [plan.source_id for plan in plans],
                    "status": "validated",
                },
                json_output=args.json,
            )
            return 0
        if args.command == "plan":
            _emit(
                {
                    "source_count": len(plans),
                    "sources": [plan.as_dict(root=args.root.resolve()) for plan in plans],
                    "status": "planned",
                },
                json_output=args.json,
            )
            return 0
        if args.command == "request":
            request_path = _resolve_new_review_artifact(
                root,
                args.request_output,
                field="execution request output",
            )
            request = create_execution_request(
                manifest,
                plans,
                root=root,
                reason=args.reason,
                now=datetime.now(UTC),
                expires_in=timedelta(hours=args.expires_in_hours),
            )
            _write_new_json(request_path, request.model_dump(mode="json"))
            _emit(
                {
                    "expires_at": request.expires_at,
                    "request": str(_relative_to_root(root, request_path)),
                    "request_id": request.request_id,
                    "source_count": len(plans),
                    "source_ids": request.source_ids,
                    "status": "review_requested",
                },
                json_output=args.json,
            )
            return 0
        raise CrawlSourceError("unsupported crawler command")
    except CrawlSourceError as error:
        _emit_error(str(error), json_output=bool(getattr(args, "json", False)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
