from pathlib import Path

import pytest

import careerops.infrastructure.runtime as runtime
from careerops.application.ports.readiness import ReadinessReport, ReadinessState
from careerops.config import RuntimeEnvironment, Settings
from careerops.infrastructure.runtime import RuntimeResources


def test_readiness_report_requires_all_declared_components() -> None:
    assert ReadinessReport(checks={}).ready is False
    assert (
        ReadinessReport(checks={"database": ReadinessState.OK, "redis": ReadinessState.OK}).ready
        is True
    )


@pytest.mark.asyncio
async def test_runtime_readiness_isolates_component_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class FakeAsyncRedis:
        closed = False

        async def aclose(self, close_connection_pool: bool | None = None) -> None:
            self.closed = close_connection_pool is True

    class FakeSyncRedis:
        closed = False

        def close(self) -> None:
            self.closed = True

    fake_engine = FakeEngine()
    fake_async_redis = FakeAsyncRedis()
    fake_sync_redis = FakeSyncRedis()
    monkeypatch.setattr(runtime, "create_database_engine", lambda _settings: fake_engine)
    monkeypatch.setattr(runtime.AsyncRedis, "from_url", lambda *_args, **_kwargs: fake_async_redis)
    monkeypatch.setattr(runtime.SyncRedis, "from_url", lambda *_args, **_kwargs: fake_sync_redis)
    resources = RuntimeResources(
        Settings.model_validate(
            {
                "environment": RuntimeEnvironment.TEST,
                "storage_root": tmp_path / "objects",
                "readiness_timeout_seconds": 0.1,
            }
        )
    )

    async def ok() -> None:
        return None

    async def fail() -> None:
        raise RuntimeError("must remain inside the readiness boundary")

    monkeypatch.setattr(resources, "_check_database", ok)
    monkeypatch.setattr(resources, "_check_redis", fail)
    monkeypatch.setattr(resources, "_check_temporal", ok)
    monkeypatch.setattr(resources, "_check_storage", ok)

    report = await resources.check()

    assert report.ready is False
    assert report.checks == {
        "database": ReadinessState.OK,
        "redis": ReadinessState.NOT_READY,
        "temporal": ReadinessState.OK,
        "storage": ReadinessState.OK,
    }
    await resources.close()
    assert fake_async_redis.closed is True
    assert fake_sync_redis.closed is True
    assert fake_engine.disposed is True
    assert all(
        state is ReadinessState.NOT_READY for state in (await resources.check()).checks.values()
    )
    assert (
        ReadinessReport(
            checks={
                "database": ReadinessState.OK,
                "redis": ReadinessState.NOT_READY,
            }
        ).ready
        is False
    )
