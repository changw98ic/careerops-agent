from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Protocol, cast

from redis import Redis as SyncRedis
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import Engine, text
from temporalio.client import Client

from careerops.application.ports.readiness import (
    ReadinessReport,
    ReadinessState,
)
from careerops.config import RuntimeEnvironment, Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.storage.local import LocalContentAddressedStorage
from careerops.orchestration.mapping_store import ReviewMappingStore

_COMPONENTS = ("database", "redis", "temporal", "storage")


class AsyncRedisClient(Protocol):
    async def ping(self) -> bool: ...

    async def aclose(self, close_connection_pool: bool | None = None) -> None: ...


class SyncRedisClient(Protocol):
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...

    def close(self) -> None: ...


class RuntimeResources:
    """Process-owned clients and bounded, non-sensitive readiness probes."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.database: Engine = create_database_engine(settings)
        self.redis = cast(
            "AsyncRedisClient",
            AsyncRedis.from_url(  # pyright: ignore[reportUnknownMemberType]
                settings.redis_url.get_secret_value(),
                decode_responses=False,
                socket_connect_timeout=settings.readiness_timeout_seconds,
                socket_timeout=settings.readiness_timeout_seconds,
            ),
        )
        self.redis_sync = cast(
            "SyncRedisClient",
            SyncRedis.from_url(  # pyright: ignore[reportUnknownMemberType]
                settings.redis_url.get_secret_value(),
                decode_responses=False,
                socket_connect_timeout=settings.readiness_timeout_seconds,
                socket_timeout=settings.readiness_timeout_seconds,
            ),
        )
        self.storage = LocalContentAddressedStorage(
            settings.storage_root,
            max_object_bytes=settings.storage_max_object_bytes,
        )
        self._temporal: Client | None = None
        self._temporal_lock = asyncio.Lock()
        self._closed = False
        # LangGraph review stack (plan v0.4 §2.4 / §3 Stage 3). v1 is single-
        # process (MemorySaver + InMemorySideEffectStore + InMemoryReviewMapping
        # Store share this process's lifetime). PRODUCTION is fail-closed: the
        # in-memory stack is NEVER built there, so the review endpoint cannot
        # silently degrade durable state. The attributes stay ``None`` in
        # PRODUCTION and ``app.py`` declines to mount the review router.
        self.career_graph: object | None = None
        self.review_mapping: ReviewMappingStore | None = None
        self.side_effect_kernel: object | None = None
        if settings.environment is not RuntimeEnvironment.PRODUCTION:
            self._build_career_graph_stack()

    async def check(self) -> ReadinessReport:
        if self._closed:
            return ReadinessReport(
                checks={component: ReadinessState.NOT_READY for component in _COMPONENTS}
            )

        checks = await asyncio.gather(
            self._bounded(self._check_database),
            self._bounded(self._check_redis),
            self._bounded(self._check_temporal),
            self._bounded(self._check_storage),
        )
        return ReadinessReport(checks=dict(zip(_COMPONENTS, checks, strict=True)))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.redis.aclose(close_connection_pool=True)
        await asyncio.to_thread(self.redis_sync.close)
        await asyncio.to_thread(self.database.dispose)
        self._temporal = None

    async def _bounded(
        self,
        check: Callable[[], Awaitable[None]],
    ) -> ReadinessState:
        try:
            async with asyncio.timeout(self._settings.readiness_timeout_seconds):
                await check()
        except Exception:
            return ReadinessState.NOT_READY
        return ReadinessState.OK

    async def _check_database(self) -> None:
        expected_role = f"careerops_{self._settings.database_role.value}"

        def execute() -> None:
            with self.database.connect() as connection:
                row = connection.execute(
                    text("SELECT 1, current_user::text, current_setting('search_path')")
                ).one()
                if row[0] != 1 or row[1] != expected_role:
                    raise RuntimeError("database capability is not active")
                if tuple(part.strip() for part in row[2].split(",")) != (
                    "pg_catalog",
                    "careerops",
                ):
                    raise RuntimeError("database search path is not bounded")

        await asyncio.to_thread(execute)

    async def _check_redis(self) -> None:
        if await self.redis.ping() is not True:
            raise RuntimeError("Redis ping was not acknowledged")

    async def _check_temporal(self) -> None:
        if self._temporal is None:
            async with self._temporal_lock:
                if self._temporal is None:
                    self._temporal = await Client.connect(
                        self._settings.temporal_address,
                        namespace=self._settings.temporal_namespace,
                        lazy=False,
                    )
        healthy = await self._temporal.service_client.check_health(
            timeout=timedelta(seconds=self._settings.readiness_timeout_seconds),
        )
        if not healthy:
            raise RuntimeError("Temporal health check failed")

    async def _check_storage(self) -> None:
        await asyncio.to_thread(_verify_storage_directory, self._settings.storage_root)

    def _build_career_graph_stack(self) -> None:
        """Compile the v1 in-process LangGraph review stack onto this runtime.

        All collaborators are in-memory and process-owned (plan v0.4 §2.4): a
        ``MemorySaver`` checkpointer, a ``SideEffectKernel`` backed by
        ``InMemorySideEffectStore`` + ``FakeSideEffectProvider``, an
        ``InMemoryReviewMappingStore``, and the production
        ``SettingsCapabilityResolver``. The compiled graph is exposed as
        ``career_graph`` so the review endpoint can resume interrupted threads;
        ``review_mapping`` and ``side_effect_kernel`` are exposed so the endpoint
        can reverse-resolve approvals and disambiguate duplicate vs conflicting
        decisions.

        The graph's crawl/extract/resume inputs are demo defaults. v1 exposes NO
        fresh-run API path: the review endpoint only resumes threads that an
        orchestration driver (or tests) have already driven to ``review_gate``.
        A fresh invoke of this compiled graph would produce no drafts and
        ``review_gate`` would fail-closed on the empty batch; that is the
        intended posture, not a wiring gap. The model client is always
        ``DisabledModelAdapter`` (ADR 0006: v1 stays ``disabled``).
        """
        # Lazy imports keep PRODUCTION (which never calls this) free of the
        # langgraph dependency at module import time.
        from langgraph.checkpoint.memory import MemorySaver

        from careerops.application.side_effect_kernel import SideEffectKernel
        from careerops.infrastructure.database.side_effect_memory import (
            InMemorySideEffectStore,
        )
        from careerops.integrations.fake_side_effect_provider import (
            FakeSideEffectProvider,
        )
        from careerops.model_gateway.base import DisabledModelAdapter
        from careerops.orchestration.capability_resolver import (
            SettingsCapabilityResolver,
        )
        from careerops.orchestration.graph import build_graph
        from careerops.orchestration.mapping_store import InMemoryReviewMappingStore
        from careerops.orchestration.state import ContactDTO, RawJobDTO

        checkpointer = MemorySaver()
        side_effect_store = InMemorySideEffectStore()
        side_effect_provider = FakeSideEffectProvider()
        kernel = SideEffectKernel(side_effect_store, side_effect_provider)
        review_mapping = InMemoryReviewMappingStore()
        capability_resolver = SettingsCapabilityResolver(self._settings)

        def demo_crawler() -> tuple[RawJobDTO, ...]:
            # v1 exposes no fresh-run API; see docstring.
            return ()

        def demo_extractor(jobs: tuple[RawJobDTO, ...]) -> tuple[ContactDTO, ...]:
            del jobs
            return ()

        self.side_effect_kernel = kernel
        self.review_mapping = review_mapping
        self.career_graph = build_graph(
            crawler=demo_crawler,
            extractor=demo_extractor,
            resume_text="",
            model_client=DisabledModelAdapter(),
            kernel=kernel,
            review_mapping=review_mapping,
            capability_resolver=capability_resolver,
            checkpointer=checkpointer,
        )


def _verify_storage_directory(root: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("storage root is not a directory")
        if not os.access(root, os.R_OK | os.W_OK | os.X_OK, follow_symlinks=False):
            raise RuntimeError("storage root is not accessible")
    finally:
        with suppress(OSError):
            os.close(descriptor)


def create_runtime_resources(settings: Settings) -> RuntimeResources:
    return RuntimeResources(settings)
