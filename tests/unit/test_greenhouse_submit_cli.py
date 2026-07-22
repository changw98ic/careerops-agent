from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from careerops.cli import greenhouse_submit
from careerops.config import DatabaseCapabilityRole
from careerops.infrastructure.greenhouse.worker import (
    GreenhouseSubmitWorkerOutcome,
    GreenhouseSubmitWorkerRunResult,
    GreenhouseSubmitWorkerStatus,
)


class RecordingWorker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def status(self) -> GreenhouseSubmitWorkerStatus:
        self.calls.append(("status", None))
        return GreenhouseSubmitWorkerStatus(
            enabled=False,
            owner="unit-greenhouse-submit",
            disabled_reason="GREENHOUSE_SUBMIT_DISABLED",
        )

    def run_once(self, *, limit: int = 10) -> GreenhouseSubmitWorkerRunResult:
        self.calls.append(("run_once", {"limit": limit}))
        return GreenhouseSubmitWorkerRunResult(
            outcome=GreenhouseSubmitWorkerOutcome.AMBIGUOUS,
            claimed=1,
            accepted_unverified=0,
            rejected=0,
            ambiguous=1,
            deferred_prepost=0,
            failed=0,
        )


def test_greenhouse_submit_cli_status_json_uses_dedicated_worker(capsys) -> None:
    worker = RecordingWorker()

    assert (
        greenhouse_submit.main(
            ("--owner", "unit-greenhouse-submit", "status", "--json"),
            worker=worker,
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "disabled_reason": "GREENHOUSE_SUBMIT_DISABLED",
        "enabled": False,
        "owner": "unit-greenhouse-submit",
    }
    assert "secret" not in json.dumps(payload).lower()
    assert worker.calls == [("status", None)]


def test_greenhouse_submit_cli_has_no_reconcile_or_raw_submit_command(capsys) -> None:
    parser = greenhouse_submit._parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    choices = cast("dict[str, object]", subparsers.choices)
    assert set(choices) == {"status", "run-once", "worker"}
    assert "reconcile" not in choices
    assert "reconcile-once" not in choices
    assert "submit" not in choices

    worker = RecordingWorker()
    assert greenhouse_submit.main(("run-once", "--limit", "7", "--json"), worker=worker) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "ambiguous"
    assert "confirmed" not in json.dumps(payload).lower()
    assert worker.calls == [("run_once", {"limit": 7})]


def test_greenhouse_submit_cli_worker_runs_the_long_lived_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = RecordingWorker()
    called: list[tuple[object, float, int]] = []

    def record_worker_forever(
        resolved_worker: object,
        *,
        poll_seconds: float,
        limit: int,
    ) -> None:
        called.append((resolved_worker, poll_seconds, limit))

    monkeypatch.setattr(greenhouse_submit, "run_worker_forever", record_worker_forever)

    assert (
        greenhouse_submit.main(
            ("worker", "--poll-seconds", "0.25", "--limit", "7"),
            worker=worker,
        )
        == 0
    )
    assert called == [(worker, 0.25, 7)]


def test_greenhouse_submit_cli_uses_sender_role_and_attachment_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeSettings:
        greenhouse_submit_attachment_broker_socket = Path("/tmp/greenhouse-attachments.sock")

        def __init__(self, *, database_role: DatabaseCapabilityRole) -> None:
            calls["database_role"] = database_role

    class FakeEngine:
        def dispose(self) -> None:
            calls["disposed"] = True

    class FakeAttachmentResolver:
        def __init__(self, *, socket_path: Path) -> None:
            calls["attachment_socket"] = socket_path

    def fake_create_database_engine(settings: object) -> FakeEngine:
        calls["engine_settings"] = settings
        return FakeEngine()

    def fake_create_runtime_greenhouse_submit_worker(
        *,
        settings: object,
        engine: FakeEngine,
        owner: str,
        attachment_resolver: object | None = None,
    ) -> RecordingWorker:
        calls["worker_settings"] = settings
        calls["worker_engine"] = engine
        calls["owner"] = owner
        calls["attachment_resolver"] = attachment_resolver
        return RecordingWorker()

    monkeypatch.setattr(greenhouse_submit, "Settings", FakeSettings)
    monkeypatch.setattr(greenhouse_submit, "create_database_engine", fake_create_database_engine)
    monkeypatch.setattr(
        greenhouse_submit,
        "UnixGreenhouseAttachmentResolver",
        FakeAttachmentResolver,
    )
    monkeypatch.setattr(
        greenhouse_submit,
        "create_runtime_greenhouse_submit_worker",
        fake_create_runtime_greenhouse_submit_worker,
    )

    assert greenhouse_submit.main(("--owner", "greenhouse-owner", "status", "--json")) == 0

    assert calls["database_role"] is DatabaseCapabilityRole.GREENHOUSE_SENDER
    assert calls["attachment_socket"] == Path("/tmp/greenhouse-attachments.sock")
    assert calls["owner"] == "greenhouse-owner"
    assert calls["disposed"] is True
