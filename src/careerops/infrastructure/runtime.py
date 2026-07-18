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
from careerops.config import Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.storage.local import LocalContentAddressedStorage

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
