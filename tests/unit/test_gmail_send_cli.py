from __future__ import annotations

import json
from typing import cast

import pytest

from careerops.cli import gmail_send
from careerops.infrastructure.gmail.send_worker import (
    GmailSendReconciliationRunResult,
    GmailSendWorkerOutcome,
    GmailSendWorkerRunResult,
    GmailSendWorkerStatus,
)


class RecordingWorker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def status(self) -> GmailSendWorkerStatus:
        self.calls.append(("status", None))
        return GmailSendWorkerStatus(
            enabled=False,
            owner="unit-gmail-send",
            disabled_reason="GMAIL_SEND_DISABLED",
        )

    def run_once(self, *, limit: int = 10) -> GmailSendWorkerRunResult:
        self.calls.append(("run_once", {"limit": limit}))
        return GmailSendWorkerRunResult(
            outcome=GmailSendWorkerOutcome.DISABLED,
            claimed=0,
            published=0,
            deferred=0,
            failed=0,
            disabled_reason="GMAIL_SEND_DISABLED",
        )

    def reconcile_once(self, *, limit: int = 10) -> GmailSendReconciliationRunResult:
        self.calls.append(("reconcile_once", {"limit": limit}))
        return GmailSendReconciliationRunResult(
            outcome=GmailSendWorkerOutcome.RECONCILED,
            claimed=1,
            confirmed=1,
            ambiguous=0,
        )


def test_gmail_send_cli_status_json_uses_dedicated_worker_without_secret_output(capsys) -> None:
    worker = RecordingWorker()

    assert gmail_send.main(("--owner", "unit-gmail-send", "status", "--json"), worker=worker) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "disabled_reason": "GMAIL_SEND_DISABLED",
        "enabled": False,
        "owner": "unit-gmail-send",
    }
    assert "secret" not in json.dumps(payload).lower()
    assert worker.calls == [("status", None)]


def test_gmail_send_cli_has_no_raw_send_command_and_wires_run_and_reconcile(capsys) -> None:
    parser = gmail_send._parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    choices = cast("dict[str, object]", subparsers.choices)
    assert "send" not in choices
    assert "run-once" in choices
    assert "reconcile-once" in choices
    assert "status" in choices
    assert "worker" in choices

    worker = RecordingWorker()
    assert gmail_send.main(("run-once", "--limit", "7", "--json"), worker=worker) == 0
    assert gmail_send.main(("reconcile-once", "--limit", "3", "--json"), worker=worker) == 0

    output = capsys.readouterr().out.strip().splitlines()
    assert json.loads(output[0])["outcome"] == "disabled"
    assert json.loads(output[1])["confirmed"] == 1
    assert worker.calls == [
        ("run_once", {"limit": 7}),
        ("reconcile_once", {"limit": 3}),
    ]


def test_gmail_send_cli_worker_runs_the_long_lived_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = RecordingWorker()
    called: list[tuple[object, float, int]] = []

    def record_worker_forever(
        resolved_worker: object,
        *,
        poll_seconds: float,
        limit: int,
    ) -> None:
        called.append((resolved_worker, poll_seconds, limit))

    monkeypatch.setattr(gmail_send, "run_worker_forever", record_worker_forever)

    assert (
        gmail_send.main(
            ("worker", "--poll-seconds", "0.25", "--limit", "7"),
            worker=worker,
        )
        == 0
    )
    assert called == [(worker, 0.25, 7)]


def test_gmail_send_cli_worker_interrupt_is_quiet(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupt_worker(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(gmail_send, "run_worker_forever", interrupt_worker)

    assert gmail_send.main(("worker",), worker=RecordingWorker()) == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_gmail_send_cli_disables_dotenv_for_host_worker_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, object]] = []

    class SettingsValue:
        gmail_send_attachment_broker_socket = None

    class RecordingSettings:
        @classmethod
        def model_validate(cls, payload: dict[str, object]) -> SettingsValue:
            payloads.append(payload)
            return SettingsValue()

    class FakeEngine:
        def dispose(self) -> None:
            pass

    worker = RecordingWorker()
    monkeypatch.setattr(gmail_send, "Settings", RecordingSettings)
    monkeypatch.setattr(gmail_send, "create_database_engine", lambda settings: FakeEngine())
    monkeypatch.setattr(gmail_send, "create_runtime_gmail_send_worker", lambda **kwargs: worker)

    assert gmail_send.main(("status",)) == 0
    assert payloads == [
        {
            "_env_file": None,
            "database_role": gmail_send.DatabaseCapabilityRole.MAIL_SENDER,
        }
    ]
