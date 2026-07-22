from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from typing import cast

import pytest
from sqlalchemy.engine import Engine

from careerops.auth.contracts import BootstrapCredential
from careerops.cli import auth as auth_cli
from careerops.config import Settings


@dataclass
class DummyEngine:
    disposed: bool = False

    def dispose(self) -> None:
        self.disposed = True


class RecordingBootstrapService:
    def __init__(self) -> None:
        self.issue_calls = 0

    def issue_bootstrap_token(self, *, now: datetime) -> BootstrapCredential:
        assert now.tzinfo is not None
        self.issue_calls += 1
        return BootstrapCredential(
            token="bootstrap-token-for-test",
            expires_at=datetime(2026, 7, 21, 12, 30, tzinfo=UTC),
        )


def test_help_never_loads_settings_or_issues_token(capsys: pytest.CaptureFixture[str]) -> None:
    def fail_settings_loader() -> Settings:
        raise AssertionError("settings must not load for help")

    with pytest.raises(SystemExit) as exc:
        auth_cli.main(["--help"], settings_loader=fail_settings_loader)

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "issue" in captured.out
    assert "bootstrap-token-for-test" not in captured.out


def test_missing_subcommand_never_loads_settings_or_issues_token() -> None:
    def fail_settings_loader() -> Settings:
        raise AssertionError("settings must not load without a command")

    stdout = StringIO()
    stderr = StringIO()

    exit_code = auth_cli.main(
        [],
        settings_loader=fail_settings_loader,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert "issue" in stderr.getvalue()


def test_issue_subcommand_issues_once_and_disposes_engine() -> None:
    settings = Settings()
    engine = DummyEngine()
    service = RecordingBootstrapService()
    calls: list[str] = []
    stdout = StringIO()

    exit_code = auth_cli.main(
        ["issue"],
        settings_loader=lambda: settings,
        engine_factory=lambda received_settings: (
            calls.append("engine"),
            assert_same_settings(received_settings, settings),
            cast(Engine, engine),
        )[-1],
        service_factory=lambda received_engine: (
            calls.append("service"),
            assert_same_engine(received_engine, engine),
            service,
        )[-1],
        stdout=stdout,
    )

    assert exit_code == 0
    assert calls == ["engine", "service"]
    assert service.issue_calls == 1
    assert engine.disposed is True
    assert stdout.getvalue().splitlines() == [
        "bootstrap-token-for-test",
        "expires_at=2026-07-21T12:30:00+00:00",
    ]


def assert_same_settings(actual: Settings, expected: Settings) -> None:
    assert actual is expected


def assert_same_engine(actual: Engine, expected: DummyEngine) -> None:
    assert actual is expected
