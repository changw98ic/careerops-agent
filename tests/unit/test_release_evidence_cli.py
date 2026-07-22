from __future__ import annotations

import json
from argparse import Namespace
from typing import cast
from uuid import uuid4

from sqlalchemy.engine import Engine

from careerops.cli import release_evidence
from careerops.config import DatabaseCapabilityRole, Settings


def test_release_evidence_cli_requires_json_subcommands() -> None:
    parser = release_evidence._parser()

    assert (
        parser.parse_args(("show", "--qualification-id", str(uuid4()), "--json")).command == "show"
    )
    assert (
        parser.parse_args(("trace-intent", "--intent-id", str(uuid4()), "--json")).command
        == "trace-intent"
    )


def test_release_evidence_cli_uses_readonly_database_role(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakeEngine:
        def dispose(self) -> None:
            observed["disposed"] = True

    def fake_create_database_engine(settings: Settings) -> FakeEngine:
        observed["database_role"] = settings.database_role
        return FakeEngine()

    def fake_run(engine: FakeEngine, args: Namespace) -> int:
        observed["engine"] = engine
        observed["command"] = args.command
        return 0

    monkeypatch.setattr(release_evidence, "create_database_engine", fake_create_database_engine)
    monkeypatch.setattr(release_evidence, "_run", fake_run)

    assert release_evidence.main(("trace-intent", "--intent-id", str(uuid4()), "--json")) == 0
    assert observed["database_role"] is DatabaseCapabilityRole.READONLY
    assert observed["command"] == "trace-intent"
    assert observed["disposed"] is True


def test_release_evidence_cli_missing_resource_returns_json_error(capsys, monkeypatch) -> None:
    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

    class FakeEngine:
        def connect(self) -> FakeConnection:
            return FakeConnection()

    class FakeReader:
        def __init__(self, connection: FakeConnection) -> None:
            self.connection = connection

        def show(self, qualification_id):
            return None

        def trace_intent(self, intent_id):
            return None

    monkeypatch.setattr(release_evidence, "PostgresReleaseEvidenceReader", FakeReader)

    result = release_evidence._run(
        cast("Engine", FakeEngine()),
        Namespace(command="show", qualification_id=uuid4()),
    )

    assert result == 1
    assert json.loads(capsys.readouterr().out) == {"error": "release_qualification_not_found"}
