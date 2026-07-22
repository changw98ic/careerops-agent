from __future__ import annotations

from typing import cast

from sqlalchemy.engine import Engine

from careerops.cli import submission_outbox
from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.submission_outbox import (
    PostgresSyntheticSubmissionCoordinator,
)


def test_submission_outbox_cli_uses_database_coordinator_not_file_metadata() -> None:
    parser = submission_outbox._parser()

    options = {action.dest for action in parser._actions}
    assert "owner" in options
    assert "limit" in options
    assert "lease_seconds" in options
    assert "poll_seconds" in options
    assert "root" not in options
    assert "metadata_dir" not in options
    assert "credentials_path" not in options


def test_submission_outbox_cli_defaults_to_outbox_database_role(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakeResult:
        claimed = 0
        published = 0
        deferred = 0
        failed = 0

    class FakePublisher:
        def publish_batch(self, **kwargs):
            observed["publish_kwargs"] = kwargs
            return FakeResult()

    class FakeEngine:
        def dispose(self) -> None:
            observed["disposed"] = True

    def fake_create_database_engine(settings: Settings) -> FakeEngine:
        observed["database_role"] = settings.database_role
        return FakeEngine()

    def fake_publisher(
        engine: FakeEngine,
        *,
        owner: str,
        max_attempts: int,
    ) -> FakePublisher:
        observed["engine"] = engine
        observed["publisher_owner"] = owner
        observed["max_attempts"] = max_attempts
        return FakePublisher()

    monkeypatch.setattr(submission_outbox, "create_database_engine", fake_create_database_engine)
    monkeypatch.setattr(submission_outbox, "_publisher", fake_publisher)

    assert submission_outbox.main(("--owner", "unit-synthetic", "--limit", "7")) == 0
    assert observed["database_role"] is DatabaseCapabilityRole.OUTBOX
    assert observed["publisher_owner"] == "unit-synthetic"
    assert observed["max_attempts"] == 3
    assert observed["disposed"] is True
    publish_kwargs = observed["publish_kwargs"]
    assert isinstance(publish_kwargs, dict)
    assert publish_kwargs["owner"] == "unit-synthetic"
    assert publish_kwargs["limit"] == 7


def test_publisher_wires_the_production_coordinator_protocol_directly() -> None:
    publisher = submission_outbox._publisher(
        cast("Engine", object()),
        owner="unit-synthetic",
        max_attempts=3,
    )

    sink = publisher._sink
    assert sink is not None
    coordinator = sink._coordinator  # type: ignore[attr-defined]
    assert isinstance(coordinator, PostgresSyntheticSubmissionCoordinator)
    assert callable(coordinator.prepare)
    assert callable(coordinator.record_receipt)
    assert callable(coordinator.record_ambiguous)
