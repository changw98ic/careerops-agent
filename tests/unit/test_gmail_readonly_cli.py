from __future__ import annotations

import json
from typing import cast
from uuid import UUID

import pytest

from careerops.cli import gmail_readonly
from careerops.infrastructure.gmail.worker import (
    GmailMailboxSyncResult,
    GmailWorkerOutcome,
    GmailWorkerRunResult,
    GmailWorkerStatus,
)


class RecordingWorker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def status(self) -> GmailWorkerStatus:
        self.calls.append(("status", None))
        return GmailWorkerStatus(
            enabled=False,
            owner="unit-gmail",
            disabled_reason="GOOGLE_OAUTH_DISABLED",
        )

    def run_once(self, *, limit: int = 10, max_results: int = 100) -> GmailWorkerRunResult:
        self.calls.append(("run_once", {"limit": limit, "max_results": max_results}))
        return GmailWorkerRunResult(
            outcome=GmailWorkerOutcome.SYNCED,
            claimed=1,
            synced=1,
            deferred=0,
            failed=0,
            processed_messages=1,
            review_proposals=1,
            mailbox_results=(
                GmailMailboxSyncResult(
                    mailbox_id=UUID("00000000-0000-0000-0000-00000000f001"),
                    outcome=GmailWorkerOutcome.SYNCED,
                    processed_messages=1,
                    review_proposals=1,
                ),
            ),
        )


def test_gmail_readonly_cli_status_json_never_exposes_secret_handles(capsys) -> None:
    worker = RecordingWorker()

    assert gmail_readonly.main(("--owner", "unit-gmail", "status", "--json"), worker=worker) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "disabled_reason": "GOOGLE_OAUTH_DISABLED",
        "enabled": False,
        "owner": "unit-gmail",
        "pending_mailboxes": 0,
    }
    assert "secret" not in json.dumps(payload).lower()
    assert worker.calls == [("status", None)]


def test_gmail_readonly_cli_run_once_has_no_send_command_and_wires_bounds(capsys) -> None:
    parser = gmail_readonly._parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    choices = cast("dict[str, object]", subparsers.choices)
    assert "send" not in choices
    assert "worker" in choices
    assert "run-once" in choices
    assert "status" in choices

    worker = RecordingWorker()
    assert (
        gmail_readonly.main(
            ("--owner", "unit-gmail", "run-once", "--limit", "7", "--max-results", "55", "--json"),
            worker=worker,
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "synced"
    assert payload["processed_messages"] == 1
    assert "secret" not in json.dumps(payload).lower()
    assert worker.calls == [("run_once", {"limit": 7, "max_results": 55})]


def test_gmail_readonly_cli_worker_interrupt_is_quiet(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupt_worker(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(gmail_readonly, "run_worker_forever", interrupt_worker)

    assert gmail_readonly.main(("worker",), worker=RecordingWorker()) == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_gmail_readonly_cli_disables_dotenv_for_host_worker_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, object]] = []

    class RecordingSettings:
        @classmethod
        def model_validate(cls, payload: dict[str, object]) -> object:
            payloads.append(payload)
            return object()

    class FakeEngine:
        def dispose(self) -> None:
            pass

    worker = RecordingWorker()
    monkeypatch.setattr(gmail_readonly, "Settings", RecordingSettings)
    monkeypatch.setattr(gmail_readonly, "create_database_engine", lambda settings: FakeEngine())
    monkeypatch.setattr(
        gmail_readonly,
        "create_runtime_gmail_worker",
        lambda **kwargs: worker,
    )

    assert gmail_readonly.main(("status",)) == 0
    assert payloads == [
        {
            "_env_file": None,
            "database_role": gmail_readonly.DatabaseCapabilityRole.MAILBOX,
        }
    ]
