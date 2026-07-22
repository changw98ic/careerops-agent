import hashlib
import io
import json
import os
import re
import stat as stat_module
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from errno import ELOOP, ENOTDIR
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, cast
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import DBAPIError

from careerops.application.canonical_job_ingestion import (
    CanonicalJobDedupePolicy,
    CanonicalJobIngestionRecord,
    CrawlerRunProvenance,
    public_ats_row_from_mapping,
)
from careerops.auth.contracts import AuthenticatedPrincipal, AuthError
from careerops.cli import crawl_sources
from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.canonical_job_ingestion import (
    PostgresCanonicalJobIngestionRepository,
)
from careerops.infrastructure.database.crawler_source_registry import (
    CrawlerSourceLeaseConflictError,
    CrawlerSourceRegistryRepositoryError,
    PostgresCrawlerSourceRegistryRepository,
)
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.schema import (
    crawler_source_registry_sources,
    crawler_source_runs,
)
from careerops.web.routes import ConsoleAuthServicePort
from careerops.web.security import ConsoleWebSettings, OriginHostValidator, RequestOriginRejected


class SourceRegistryContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


SourceIdPath = Annotated[
    str,
    FastAPIPath(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]{0,63}$"),
]


class RegisterSourceRegistryRequest(SourceRegistryContract):
    manifest_ref: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class RegisterSourceRegistryResponse(SourceRegistryContract):
    status: Literal["registered"] = "registered"
    registry_id: UUID
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_count: int = Field(ge=1)


class SourceRegistrySummary(SourceRegistryContract):
    registry_id: UUID
    manifest_path: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_count: int = Field(ge=1)
    created_by: str
    generated_at: datetime
    created_at: datetime


class ListSourceRegistriesResponse(SourceRegistryContract):
    status: Literal["listed"] = "listed"
    registries: tuple[SourceRegistrySummary, ...]


class DueSourceSummary(SourceRegistryContract):
    registry_id: UUID
    source_id: str
    adapter: str
    enabled: bool
    input_artifact: str
    output_dir: str
    dependency_source_ids: tuple[str, ...]
    dependency_artifacts: tuple[str, ...]
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cadence_seconds: int = Field(ge=1)
    retry_rounds: int = Field(ge=0)
    cursor: str | None = None
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    last_result: str | None = None
    last_error: str | None = None


class DueSourcesResponse(SourceRegistryContract):
    status: Literal["due"] = "due"
    registry_id: UUID
    due_sources: tuple[DueSourceSummary, ...]


class ClaimSourceRequest(SourceRegistryContract):
    worker_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:@/-]+$")
    lease_seconds: int = Field(default=300, ge=30, le=3600)


class ClaimSourceResponse(SourceRegistryContract):
    status: Literal["claimed"] = "claimed"
    registry_id: UUID
    run_id: UUID
    source_row_id: UUID
    source_id: str
    lease_token: UUID
    lease_owner: str
    lease_expires_at: datetime


class CompleteSourceRequest(SourceRegistryContract):
    run_id: UUID
    source_row_id: UUID
    worker_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:@/-]+$")
    lease_token: UUID
    output_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cursor: str | None = Field(default=None, max_length=160)
    result: str = Field(min_length=1, max_length=160)


class CompleteSourceResponse(SourceRegistryContract):
    status: Literal["completed"] = "completed"
    registry_id: UUID
    run_id: UUID
    source_row_id: UUID
    source_id: str
    lease_owner: str
    output_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cursor: str | None = None


class FailSourceRequest(SourceRegistryContract):
    run_id: UUID
    source_row_id: UUID
    worker_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:@/-]+$")
    lease_token: UUID
    error: str = Field(min_length=1, max_length=500)


class FailSourceResponse(SourceRegistryContract):
    status: Literal["failed"] = "failed"
    registry_id: UUID
    run_id: UUID
    source_row_id: UUID
    source_id: str
    lease_owner: str


class IngestPublicAtsRequest(SourceRegistryContract):
    source_row_id: UUID
    max_records: int = Field(default=1_000, ge=1, le=10_000)


class IngestPublicAtsResponse(SourceRegistryContract):
    status: Literal["ingested"] = "ingested"
    registry_id: UUID
    source_row_id: UUID
    source_id: str
    run_id: UUID
    run_event_id: UUID
    observed_records: int = Field(ge=0)
    inserted_versions: int = Field(ge=0)
    reused_versions: int = Field(ge=0)


class SourceRegistryOperatorProvider(Protocol):
    async def register(  # pyright: ignore[reportUnusedFunction]
        self,
        *,
        manifest_ref: str,
        actor_id: UUID,
        now: datetime,
    ) -> RegisterSourceRegistryResponse: ...

    async def list_registries(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListSourceRegistriesResponse: ...

    async def due_sources(
        self,
        *,
        registry_id: UUID,
        actor_id: UUID,
        now: datetime,
        limit: int,
    ) -> DueSourcesResponse: ...

    async def claim_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        worker_id: str,
        lease_for: timedelta,
    ) -> ClaimSourceResponse: ...

    async def complete_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        run_id: UUID,
        source_row_id: UUID,
        worker_id: str,
        lease_token: UUID,
        output_manifest_sha256: str | None,
        cursor: str | None,
        result: str,
    ) -> CompleteSourceResponse: ...

    async def fail_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        run_id: UUID,
        source_row_id: UUID,
        worker_id: str,
        lease_token: UUID,
        error: str,
    ) -> FailSourceResponse: ...

    async def ingest_public_ats(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        run_id: UUID,
        source_row_id: UUID,
        actor_id: UUID,
        max_records: int,
    ) -> IngestPublicAtsResponse: ...


@dataclass(frozen=True, slots=True)
class DisabledSourceRegistryOperatorProvider:
    reason: str = "source registry operator provider is not configured"

    async def register(self, **_kwargs: object) -> RegisterSourceRegistryResponse:
        raise SourceRegistryUnavailable(self.reason)

    async def list_registries(self, **_kwargs: object) -> ListSourceRegistriesResponse:
        raise SourceRegistryUnavailable(self.reason)

    async def due_sources(self, **_kwargs: object) -> DueSourcesResponse:
        raise SourceRegistryUnavailable(self.reason)

    async def claim_source(self, **_kwargs: object) -> ClaimSourceResponse:
        raise SourceRegistryUnavailable(self.reason)

    async def complete_source(self, **_kwargs: object) -> CompleteSourceResponse:
        raise SourceRegistryUnavailable(self.reason)

    async def fail_source(self, **_kwargs: object) -> FailSourceResponse:
        raise SourceRegistryUnavailable(self.reason)

    async def ingest_public_ats(self, **_kwargs: object) -> IngestPublicAtsResponse:
        raise SourceRegistryUnavailable(self.reason)


class SourceRegistryUnavailable(RuntimeError):
    pass


class SourceRegistryConflict(RuntimeError):
    pass


_PUBLIC_ATS_ADAPTER = "recruitment.public_ats_feed"
_PUBLIC_ATS_INGESTION_POLICY = "canonical_job_ingestion"
_PUBLIC_ATS_MANIFEST = "public_ats_job_feed_manifest.json"
_PUBLIC_ATS_JSONL = "discovered_public_ats_jobs.jsonl"
_REGISTERED_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")
_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_OPEN_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 1_048_576
_MAX_JSONL_LINE_BYTES = 1_048_576
_MAX_JSONL_BYTES = 64 * 1_048_576
_CONFLICT_DETAIL = "source registry operation conflicts with persisted state"
_INVALID_DATABASE_REQUEST_DETAIL = "source registry request violates database constraints"


class RuntimeSourceRegistryOperatorProvider:
    """Database-backed registry surface that never executes crawler commands."""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings

    async def register(  # pyright: ignore[reportUnusedFunction]
        self,
        *,
        manifest_ref: str,
        actor_id: UUID,
        now: datetime,
    ) -> RegisterSourceRegistryResponse:
        root = self._settings.crawler_workspace_root.resolve()
        manifests_root = (root / "datasets" / "manifests").resolve()
        raw_config_path = manifests_root / manifest_ref
        if raw_config_path.suffix != ".json":
            raw_config_path = raw_config_path.with_suffix(".json")
        if raw_config_path.is_symlink():
            raise ValueError("manifest_ref must not resolve through a symbolic link")
        config_path = raw_config_path.resolve()
        if not config_path.is_relative_to(manifests_root):
            raise ValueError("manifest_ref must stay inside datasets/manifests")
        manifest = crawl_sources.load_manifest(config_path)
        plans = crawl_sources.plan_manifest(manifest, root=root)
        registry_document = crawl_sources.create_source_registry(
            manifest,
            plans,
            root=root,
            manifest_path=config_path,
            now=now,
        )
        with self._engine.begin() as connection:
            registry_id = PostgresCrawlerSourceRegistryRepository(connection).register_registry(
                manifest,
                plans,
                registry_document,
                root=root,
                created_by=str(actor_id),
                now=now,
            )
        return RegisterSourceRegistryResponse(
            registry_id=registry_id,
            manifest_sha256=registry_document.manifest_sha256,
            registry_sha256=registry_document.registry_sha256,
            source_count=len(plans),
        )

    async def list_registries(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListSourceRegistriesResponse:
        del actor_id
        with self._engine.begin() as connection:
            from careerops.infrastructure.database.schema import crawler_source_registries

            rows = (
                connection.execute(
                    crawler_source_registries.select()
                    .order_by(crawler_source_registries.c.created_at.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return ListSourceRegistriesResponse(
            registries=tuple(_registry_summary(row) for row in rows)
        )

    async def due_sources(
        self,
        *,
        registry_id: UUID,
        actor_id: UUID,
        now: datetime,
        limit: int,
    ) -> DueSourcesResponse:
        del actor_id
        with self._engine.begin() as connection:
            sources = PostgresCrawlerSourceRegistryRepository(connection).list_due_sources(
                registry_id,
                now=now,
                limit=limit,
            )
        return DueSourcesResponse(
            registry_id=registry_id,
            due_sources=tuple(_due_source(row) for row in sources),
        )

    async def claim_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        worker_id: str,
        lease_for: timedelta,
    ) -> ClaimSourceResponse:
        del actor_id
        with self._engine.begin() as connection:
            row = PostgresCrawlerSourceRegistryRepository(connection).claim_source(
                registry_id,
                source_id,
                worker_id=worker_id,
                lease_for=lease_for,
            )
            response = _claim_response(row)
            if response.registry_id != registry_id or response.source_id != source_id:
                raise SourceRegistryUnavailable("claimed source does not match requested route")
        return response

    async def complete_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        run_id: UUID,
        source_row_id: UUID,
        worker_id: str,
        lease_token: UUID,
        output_manifest_sha256: str | None,
        cursor: str | None,
        result: str,
    ) -> CompleteSourceResponse:
        del actor_id
        with self._engine.begin() as connection:
            _require_route_source(
                connection,
                registry_id=registry_id,
                source_id=source_id,
                source_row_id=source_row_id,
            )
            row = PostgresCrawlerSourceRegistryRepository(connection).complete_source(
                source_row_id=source_row_id,
                run_id=run_id,
                worker_id=worker_id,
                lease_token=lease_token,
                output_manifest_sha256=output_manifest_sha256,
                cursor=cursor,
                result=result,
            )
            response = _complete_response(registry_id, row)
            if (
                response.source_row_id != source_row_id
                or response.run_id != run_id
                or response.source_id != source_id
            ):
                raise SourceRegistryUnavailable("completed source fencing identifiers do not match")
        return response

    async def fail_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        run_id: UUID,
        source_row_id: UUID,
        worker_id: str,
        lease_token: UUID,
        error: str,
    ) -> FailSourceResponse:
        del actor_id
        with self._engine.begin() as connection:
            _require_route_source(
                connection,
                registry_id=registry_id,
                source_id=source_id,
                source_row_id=source_row_id,
            )
            row = PostgresCrawlerSourceRegistryRepository(connection).fail_source(
                source_row_id=source_row_id,
                run_id=run_id,
                worker_id=worker_id,
                lease_token=lease_token,
                error=error,
            )
            response = _fail_response(registry_id, row)
            if (
                response.source_row_id != source_row_id
                or response.run_id != run_id
                or response.source_id != source_id
            ):
                raise SourceRegistryUnavailable("failed source fencing identifiers do not match")
        return response

    async def ingest_public_ats(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        run_id: UUID,
        source_row_id: UUID,
        actor_id: UUID,
        max_records: int,
    ) -> IngestPublicAtsResponse:
        del actor_id
        if not 1 <= max_records <= 10_000:
            raise ValueError("max_records must be between 1 and 10000")

        with self._engine.begin() as connection:
            source_row = (
                connection.execute(
                    sa.select(crawler_source_registry_sources).where(
                        crawler_source_registry_sources.c.id == source_row_id,
                        crawler_source_registry_sources.c.registry_id == registry_id,
                        crawler_source_registry_sources.c.source_id == source_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if source_row is None:
                raise ValueError("source_row_id does not match the requested registry and source")
            if (
                source_row["id"] != source_row_id
                or source_row["registry_id"] != registry_id
                or source_row["source_id"] != source_id
            ):
                raise SourceRegistryUnavailable("selected crawler source identity does not match")
            if source_row["adapter"] != _PUBLIC_ATS_ADAPTER:
                raise ValueError("source adapter does not support public ATS canonical ingestion")
            if source_row["canonical_ingestion_policy"] != _PUBLIC_ATS_INGESTION_POLICY:
                raise ValueError("source does not authorize canonical job ingestion")

            run_events = (
                connection.execute(
                    sa.select(crawler_source_runs)
                    .where(
                        crawler_source_runs.c.registry_id == registry_id,
                        crawler_source_runs.c.source_id == source_id,
                        crawler_source_runs.c.source_row_id == source_row_id,
                        crawler_source_runs.c.run_id == run_id,
                        crawler_source_runs.c.event_kind == "succeeded",
                    )
                    .limit(2)
                )
                .mappings()
                .all()
            )
            if len(run_events) != 1:
                raise ValueError("exactly one matching succeeded crawler run event is required")
            run_event = run_events[0]
            if (
                run_event["registry_id"] != registry_id
                or run_event["source_id"] != source_id
                or run_event["source_row_id"] != source_row_id
                or run_event["run_id"] != run_id
                or run_event["event_kind"] != "succeeded"
            ):
                raise SourceRegistryUnavailable(
                    "selected crawler run event identity does not match"
                )
            output_manifest_sha256 = run_event["output_manifest_sha256"]
            if not isinstance(output_manifest_sha256, str):
                raise ValueError("succeeded crawler run is missing output manifest evidence")

            workspace_root = self._settings.crawler_workspace_root.resolve()
            output_dir = source_row["output_dir"]
            if not isinstance(output_dir, str):
                raise SourceRegistryUnavailable("registered output_dir is invalid")
            manifest_bytes = _read_registered_artifact(
                workspace_root,
                output_dir,
                _PUBLIC_ATS_MANIFEST,
                max_bytes=_MAX_MANIFEST_BYTES,
                artifact="public ATS manifest",
            )
            if hashlib.sha256(manifest_bytes).hexdigest() != output_manifest_sha256:
                raise ValueError("local public ATS manifest does not match succeeded run evidence")
            discovered_jobs_sha256, discovered_jobs_bytes = _public_ats_discovered_jobs_artifact(
                manifest_bytes
            )
            jsonl_bytes = _read_registered_artifact(
                workspace_root,
                output_dir,
                _PUBLIC_ATS_JSONL,
                max_bytes=_MAX_JSONL_BYTES,
                artifact="public ATS JSONL artifact",
            )
            if len(jsonl_bytes) != discovered_jobs_bytes:
                raise ValueError("public ATS JSONL byte count does not match its manifest")
            if hashlib.sha256(jsonl_bytes).hexdigest() != discovered_jobs_sha256:
                raise ValueError("public ATS JSONL does not match its manifest digest")

            run_event_id = cast(UUID, run_event["event_id"])
            provenance = CrawlerRunProvenance(
                crawler_source_row_id=source_row_id,
                crawler_run_id=run_id,
                run_event_id=run_event_id,
                registry_id=registry_id,
                source_id=source_id,
                adapter=_PUBLIC_ATS_ADAPTER,
                output_artifact_sha256=output_manifest_sha256,
            )
            dedupe_policy = CanonicalJobDedupePolicy(cast(str, source_row["dedupe_policy"]))
            provenance_policy = cast(str, source_row["provenance_policy"])
            repository = PostgresCanonicalJobIngestionRepository(connection)
            observed_records = 0
            inserted_versions = 0
            for line_number, payload in _iter_jsonl_objects(
                jsonl_bytes,
                max_records=max_records,
            ):
                try:
                    row = public_ats_row_from_mapping(payload)
                except ValueError as exc:
                    raise ValueError(f"public ATS JSONL line {line_number}: {exc}") from exc
                result = repository.ingest_public_ats_job(
                    CanonicalJobIngestionRecord(
                        provenance=provenance,
                        row=row,
                        dedupe_policy=dedupe_policy,
                        provenance_policy=provenance_policy,
                    )
                )
                observed_records += 1
                inserted_versions += int(result.inserted_version)

        return IngestPublicAtsResponse(
            registry_id=registry_id,
            source_row_id=source_row_id,
            source_id=source_id,
            run_id=run_id,
            run_event_id=run_event_id,
            observed_records=observed_records,
            inserted_versions=inserted_versions,
            reused_versions=observed_records - inserted_versions,
        )


def install_source_registry_operator_api(
    app: FastAPI,
    *,
    auth_service: ConsoleAuthServicePort,
    settings: ConsoleWebSettings,
    provider: SourceRegistryOperatorProvider,
    now_provider: Callable[[], datetime] | None = None,
) -> None:
    app.include_router(
        create_source_registry_operator_router(
            auth_service=auth_service,
            settings=settings,
            provider=provider,
            now_provider=now_provider,
        )
    )


def create_runtime_source_registry_operator_provider(
    settings: Settings,
) -> RuntimeSourceRegistryOperatorProvider:
    if settings.database_role is not DatabaseCapabilityRole.API:
        raise ValueError("source registry operator requires the API database capability")
    return RuntimeSourceRegistryOperatorProvider(create_database_engine(settings), settings)


def create_source_registry_operator_router(
    *,
    auth_service: ConsoleAuthServicePort,
    settings: ConsoleWebSettings,
    provider: SourceRegistryOperatorProvider,
    now_provider: Callable[[], datetime] | None = None,
) -> APIRouter:
    clock = now_provider or (lambda: datetime.now(UTC))
    origin_validator = OriginHostValidator(settings)
    router = APIRouter(
        prefix="/api/v1/internal/source-registries",
        tags=["source-registry-operator"],
    )

    def require_principal(request: Request) -> AuthenticatedPrincipal:
        try:
            origin_validator.validate_host(request)
        except RequestOriginRejected:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
        raw_session = request.cookies.get(settings.session_cookie_name, "")
        raw_csrf = request.cookies.get(settings.csrf_cookie_name, "")
        try:
            principal = auth_service.authenticate(raw_session, now=clock())
            auth_service.validate_csrf(principal, raw_csrf)
        except (AuthError, ValueError):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from None
        return principal

    def require_mutation_csrf(
        request: Request,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_principal)],
        x_csrf_token: Annotated[str, Header(min_length=1, max_length=512)],
    ) -> AuthenticatedPrincipal:
        try:
            origin_validator.validate_mutation(request)
        except RequestOriginRejected:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
        try:
            auth_service.validate_csrf(principal, x_csrf_token)
        except (AuthError, ValueError):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
        return principal

    @router.post(
        "/register",
        response_model=RegisterSourceRegistryResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def register(  # pyright: ignore[reportUnusedFunction]
        body: RegisterSourceRegistryRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_mutation_csrf)],
    ) -> RegisterSourceRegistryResponse:
        return await _translate_provider_errors(
            provider.register(
                manifest_ref=body.manifest_ref,
                actor_id=principal.user_id,
                now=clock(),
            )
        )

    @router.get("", response_model=ListSourceRegistriesResponse)
    async def list_registries(  # pyright: ignore[reportUnusedFunction]
        principal: Annotated[AuthenticatedPrincipal, Depends(require_principal)],
        limit: int = 100,
    ) -> ListSourceRegistriesResponse:
        _require_limit(limit)
        return await _translate_provider_errors(
            provider.list_registries(actor_id=principal.user_id, limit=limit)
        )

    @router.get("/{registry_id}/due", response_model=DueSourcesResponse)
    async def due_sources(  # pyright: ignore[reportUnusedFunction]
        registry_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_principal)],
        limit: int = 100,
    ) -> DueSourcesResponse:
        _require_limit(limit)
        return await _translate_provider_errors(
            provider.due_sources(
                registry_id=registry_id,
                actor_id=principal.user_id,
                now=clock(),
                limit=limit,
            )
        )

    @router.post("/{registry_id}/sources/{source_id}/claim", response_model=ClaimSourceResponse)
    async def claim_source(  # pyright: ignore[reportUnusedFunction]
        registry_id: UUID,
        source_id: SourceIdPath,
        body: ClaimSourceRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_mutation_csrf)],
    ) -> ClaimSourceResponse:
        return await _translate_provider_errors(
            provider.claim_source(
                registry_id=registry_id,
                source_id=source_id,
                actor_id=principal.user_id,
                worker_id=body.worker_id,
                lease_for=timedelta(seconds=body.lease_seconds),
            )
        )

    @router.post(
        "/{registry_id}/sources/{source_id}/complete",
        response_model=CompleteSourceResponse,
    )
    async def complete_source(  # pyright: ignore[reportUnusedFunction]
        registry_id: UUID,
        source_id: SourceIdPath,
        body: CompleteSourceRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_mutation_csrf)],
    ) -> CompleteSourceResponse:
        return await _translate_provider_errors(
            provider.complete_source(
                registry_id=registry_id,
                source_id=source_id,
                actor_id=principal.user_id,
                run_id=body.run_id,
                source_row_id=body.source_row_id,
                worker_id=body.worker_id,
                lease_token=body.lease_token,
                output_manifest_sha256=body.output_manifest_sha256,
                cursor=body.cursor,
                result=body.result,
            )
        )

    @router.post("/{registry_id}/sources/{source_id}/fail", response_model=FailSourceResponse)
    async def fail_source(  # pyright: ignore[reportUnusedFunction]
        registry_id: UUID,
        source_id: SourceIdPath,
        body: FailSourceRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_mutation_csrf)],
    ) -> FailSourceResponse:
        return await _translate_provider_errors(
            provider.fail_source(
                registry_id=registry_id,
                source_id=source_id,
                actor_id=principal.user_id,
                run_id=body.run_id,
                source_row_id=body.source_row_id,
                worker_id=body.worker_id,
                lease_token=body.lease_token,
                error=body.error,
            )
        )

    @router.post(
        "/{registry_id}/sources/{source_id}/runs/{run_id}/ingest-public-ats",
        response_model=IngestPublicAtsResponse,
    )
    async def ingest_public_ats(  # pyright: ignore[reportUnusedFunction]
        registry_id: UUID,
        source_id: SourceIdPath,
        run_id: UUID,
        body: IngestPublicAtsRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_mutation_csrf)],
    ) -> IngestPublicAtsResponse:
        return await _translate_provider_errors(
            provider.ingest_public_ats(
                registry_id=registry_id,
                source_id=source_id,
                run_id=run_id,
                source_row_id=body.source_row_id,
                actor_id=principal.user_id,
                max_records=body.max_records,
            )
        )

    return router


async def _translate_provider_errors[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except SourceRegistryUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from None
    except (SourceRegistryConflict, CrawlerSourceLeaseConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_CONFLICT_DETAIL,
        ) from None
    except CrawlerSourceRegistryRepositoryError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_CONFLICT_DETAIL,
        ) from None
    except DBAPIError as exc:
        sqlstate = _database_sqlstate(exc)
        if sqlstate in {"23505", "55000"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_CONFLICT_DETAIL,
            ) from None
        if sqlstate == "22023":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_INVALID_DATABASE_REQUEST_DETAIL,
            ) from None
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


def _database_sqlstate(exc: DBAPIError) -> str | None:
    original = exc.orig
    for attribute in ("sqlstate", "pgcode"):
        value = getattr(original, attribute, None)
        if isinstance(value, str):
            return value
    diagnostic = getattr(original, "diag", None)
    value = getattr(diagnostic, "sqlstate", None)
    return value if isinstance(value, str) else None


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)


def _registry_summary(row: RowMapping | Mapping[str, Any]) -> SourceRegistrySummary:
    return SourceRegistrySummary(
        registry_id=cast(UUID, row["id"]),
        manifest_path=cast(str, row["manifest_path"]),
        manifest_sha256=cast(str, row["manifest_sha256"]),
        registry_sha256=cast(str, row["registry_sha256"]),
        source_count=cast(int, row["source_count"]),
        created_by=cast(str, row["created_by"]),
        generated_at=cast(datetime, row["generated_at"]),
        created_at=cast(datetime, row["created_at"]),
    )


def _due_source(row: RowMapping | Mapping[str, Any]) -> DueSourceSummary:
    return DueSourceSummary(
        registry_id=cast(UUID, row["registry_id"]),
        source_id=cast(str, row["source_id"]),
        adapter=cast(str, row["adapter"]),
        enabled=cast(bool, row["enabled"]),
        input_artifact=cast(str, row["input_artifact"]),
        output_dir=cast(str, row["output_dir"]),
        dependency_source_ids=tuple(cast(Sequence[str], row["dependency_source_ids"])),
        dependency_artifacts=tuple(cast(Sequence[str], row["dependency_artifacts"])),
        command_sha256=cast(str, row["command_sha256"]),
        source_sha256=cast(str, row["source_sha256"]),
        cadence_seconds=cast(int, row["cadence_seconds"]),
        retry_rounds=cast(int, row["retry_rounds"]),
        cursor=cast(str | None, row["cursor"]),
        last_run_at=cast(datetime | None, row["last_run_at"]),
        next_run_at=cast(datetime | None, row["next_run_at"]),
        last_result=cast(str | None, row["last_result"]),
        last_error=cast(str | None, row["last_error"]),
    )


def _claim_response(row: RowMapping | Mapping[str, Any]) -> ClaimSourceResponse:
    return ClaimSourceResponse(
        registry_id=cast(UUID, row["registry_id"]),
        run_id=cast(UUID, row["run_id"]),
        source_row_id=cast(UUID, row["source_row_id"]),
        source_id=cast(str, row["source_id"]),
        lease_token=cast(UUID, row["lease_token"]),
        lease_owner=cast(str, row["lease_owner"]),
        lease_expires_at=cast(datetime, row["lease_expires_at"]),
    )


def _complete_response(
    registry_id: UUID,
    row: RowMapping | Mapping[str, Any],
) -> CompleteSourceResponse:
    return CompleteSourceResponse(
        registry_id=registry_id,
        run_id=cast(UUID, row["run_id"]),
        source_row_id=cast(UUID, row["source_row_id"]),
        source_id=cast(str, row["source_id"]),
        lease_owner=cast(str, row["lease_owner"]),
        output_manifest_sha256=cast(str | None, row["output_manifest_sha256"]),
        cursor=cast(str | None, row["cursor"]),
    )


def _fail_response(
    registry_id: UUID,
    row: RowMapping | Mapping[str, Any],
) -> FailSourceResponse:
    return FailSourceResponse(
        registry_id=registry_id,
        run_id=cast(UUID, row["run_id"]),
        source_row_id=cast(UUID, row["source_row_id"]),
        source_id=cast(str, row["source_id"]),
        lease_owner=cast(str, row["lease_owner"]),
    )


def _require_route_source(
    connection: Connection,
    *,
    registry_id: UUID,
    source_id: str,
    source_row_id: UUID,
) -> None:
    row = (
        connection.execute(
            sa.select(crawler_source_registry_sources.c.id).where(
                crawler_source_registry_sources.c.id == source_row_id,
                crawler_source_registry_sources.c.registry_id == registry_id,
                crawler_source_registry_sources.c.source_id == source_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["id"] != source_row_id:
        raise ValueError("source_row_id does not match the requested registry and source")


def _read_registered_artifact(
    workspace_root: Path,
    output_dir: str,
    filename: str,
    *,
    max_bytes: int,
    artifact: str,
) -> bytes:
    root = workspace_root.resolve()
    output_path = Path(output_dir)
    if not _safe_registered_output_path(output_path):
        raise ValueError("registered output_dir is not a safe relative path")
    if not output_path.parts or output_path.parts[0] != "datasets":
        raise ValueError("registered output_dir must stay inside the crawler datasets root")

    try:
        root_fd = _open_directory_path(root, description="crawler workspace root")
    except OSError as exc:
        raise ValueError("crawler workspace root is unavailable") from exc
    owned_fds: list[int] = []
    try:
        parent_fd = root_fd
        for part in output_path.parts:
            next_fd = _open_child_directory(parent_fd, part)
            owned_fds.append(next_fd)
            parent_fd = next_fd
        file_fd = _open_child_file(parent_fd, filename)
        try:
            return _read_bounded_fd(file_fd, max_bytes=max_bytes, artifact=artifact)
        finally:
            os.close(file_fd)
    except OSError as exc:
        if exc.errno in {ELOOP, ENOTDIR}:
            raise ValueError(
                "public ATS artifacts must not resolve through symbolic links"
            ) from exc
        raise ValueError(f"{artifact} could not be read") from exc
    finally:
        for fd in reversed(owned_fds):
            os.close(fd)
        os.close(root_fd)


def _safe_registered_output_path(output_path: Path) -> bool:
    return (
        bool(output_path.parts)
        and _REGISTERED_RELATIVE_PATH.fullmatch(output_path.as_posix()) is not None
        and not output_path.is_absolute()
        and ".." not in output_path.parts
        and all(part not in {"", "."} for part in output_path.parts)
    )


def _open_directory_path(path: Path, *, description: str) -> int:
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _OPEN_NOFOLLOW | _OPEN_CLOEXEC,
    )
    if not stat_module.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"{description} is not a directory")
    return fd


def _open_child_directory(parent_fd: int, name: str) -> int:
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _OPEN_NOFOLLOW | _OPEN_CLOEXEC,
        dir_fd=parent_fd,
    )
    if not stat_module.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("public ATS artifact path component is not a directory")
    return fd


def _open_child_file(parent_fd: int, name: str) -> int:
    fd = os.open(name, os.O_RDONLY | _OPEN_NOFOLLOW | _OPEN_CLOEXEC, dir_fd=parent_fd)
    if not stat_module.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("required public ATS artifact is not a file")
    return fd


def _read_bounded_fd(fd: int, *, max_bytes: int, artifact: str) -> bytes:
    payload = b""
    while len(payload) <= max_bytes:
        chunk = os.read(fd, min(1_048_576, max_bytes + 1 - len(payload)))
        if not chunk:
            break
        payload += chunk
    if len(payload) > max_bytes:
        raise ValueError(f"{artifact} exceeds the total size limit")
    return payload


def _public_ats_discovered_jobs_artifact(manifest_bytes: bytes) -> tuple[str, int]:
    try:
        decoded = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("public ATS manifest is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("public ATS manifest must be a JSON object")
    manifest = cast(dict[object, object], decoded)
    if manifest.get("kind") != "public_ats_job_feed_manifest":
        raise ValueError("public ATS manifest kind is unsupported")
    version = manifest.get("version")
    if type(version) is not int or version != 1:
        raise ValueError("public ATS manifest version is unsupported")
    artifact_files = manifest.get("artifact_files")
    if not isinstance(artifact_files, dict):
        raise ValueError("public ATS manifest is missing artifact_files")
    discovered_jobs = cast(dict[object, object], artifact_files).get("discovered_jobs")
    if not isinstance(discovered_jobs, dict):
        raise ValueError("public ATS manifest is missing discovered_jobs artifact evidence")
    evidence = cast(dict[object, object], discovered_jobs)
    artifact_path = evidence.get("path")
    if not isinstance(artifact_path, str) or not 1 <= len(artifact_path) <= 500:
        raise ValueError("public ATS manifest has invalid discovered_jobs path metadata")
    artifact_bytes = evidence.get("bytes")
    if type(artifact_bytes) is not int or artifact_bytes < 0 or artifact_bytes > _MAX_JSONL_BYTES:
        raise ValueError("public ATS manifest has invalid discovered_jobs byte count")
    digest = evidence.get("artifact_sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError("public ATS manifest has an invalid discovered_jobs digest")
    return digest, artifact_bytes


def _iter_jsonl_objects(
    payload: bytes,
    *,
    max_records: int,
) -> Iterator[tuple[int, dict[str, object]]]:
    observed_records = 0
    line_number = 0
    with io.BytesIO(payload) as handle:
        while True:
            raw_line = handle.readline(_MAX_JSONL_LINE_BYTES + 1)
            if not raw_line:
                break
            line_number += 1
            if len(raw_line) > _MAX_JSONL_LINE_BYTES:
                raise ValueError(f"public ATS JSONL line {line_number} exceeds the size limit")
            if not raw_line.strip():
                continue
            if observed_records >= max_records:
                raise ValueError(
                    f"public ATS JSONL contains more than max_records={max_records} records"
                )
            try:
                decoded = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"public ATS JSONL line {line_number} is invalid JSON") from exc
            if not isinstance(decoded, dict):
                raise ValueError(f"public ATS JSONL line {line_number} must be a JSON object")
            untyped_payload = cast(dict[object, object], decoded)
            if not all(isinstance(key, str) for key in untyped_payload):
                raise ValueError(f"public ATS JSONL line {line_number} has an invalid key")
            observed_records += 1
            yield line_number, cast(dict[str, object], untyped_payload)


__all__ = [
    "ClaimSourceRequest",
    "ClaimSourceResponse",
    "CompleteSourceRequest",
    "CompleteSourceResponse",
    "DisabledSourceRegistryOperatorProvider",
    "DueSourcesResponse",
    "FailSourceRequest",
    "FailSourceResponse",
    "IngestPublicAtsRequest",
    "IngestPublicAtsResponse",
    "ListSourceRegistriesResponse",
    "RegisterSourceRegistryRequest",
    "RegisterSourceRegistryResponse",
    "RuntimeSourceRegistryOperatorProvider",
    "SourceRegistryConflict",
    "SourceRegistryOperatorProvider",
    "SourceRegistrySummary",
    "SourceRegistryUnavailable",
    "create_runtime_source_registry_operator_provider",
    "create_source_registry_operator_router",
    "install_source_registry_operator_api",
]
