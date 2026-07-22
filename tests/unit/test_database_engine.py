from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database import engine as database_engine


class RecordingCursor:
    def __init__(self, validation_row: tuple[object, ...]) -> None:
        self.statements: list[str] = []
        self.closed = False
        self.validation_row = validation_row

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def close(self) -> None:
        self.closed = True

    def fetchone(self) -> tuple[object, ...]:
        return self.validation_row


class RecordingDbapiConnection:
    def __init__(self, validation_row: tuple[object, ...]) -> None:
        self._autocommit = False
        self.autocommit_changes: list[bool] = []
        self.cursor_instance = RecordingCursor(validation_row)

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self.autocommit_changes.append(value)
        self._autocommit = value

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance


@pytest.mark.parametrize(
    ("role", "expected_role_sql"),
    [
        (DatabaseCapabilityRole.API, "SET ROLE careerops_api"),
        (DatabaseCapabilityRole.WORKFLOW, "SET ROLE careerops_workflow"),
        (DatabaseCapabilityRole.MAILBOX, "SET ROLE careerops_mailbox"),
        (DatabaseCapabilityRole.RETENTION, "SET ROLE careerops_retention"),
        (DatabaseCapabilityRole.OUTBOX, "SET ROLE careerops_outbox"),
        (DatabaseCapabilityRole.MAIL_SENDER, "SET ROLE careerops_mail_sender"),
        (
            DatabaseCapabilityRole.GREENHOUSE_SENDER,
            "SET ROLE careerops_greenhouse_sender",
        ),
        (DatabaseCapabilityRole.READONLY, "SET ROLE careerops_readonly"),
    ],
)
def test_engine_revalidates_every_checkout_with_fixed_role_and_search_path(
    monkeypatch: pytest.MonkeyPatch,
    role: DatabaseCapabilityRole,
    expected_role_sql: str,
) -> None:
    engine = object()
    listener: Callable[[Any, Any, Any], None] | None = None

    def fake_create_engine(url: str, **kwargs: object) -> object:
        assert url.startswith("postgresql+psycopg://")
        assert kwargs == {"pool_pre_ping": True, "pool_recycle": 300}
        return engine

    def fake_listen(
        target: object,
        event_name: str,
        callback: Callable[[Any, Any, Any], None],
    ) -> None:
        nonlocal listener
        assert target is engine
        assert event_name == "checkout"
        listener = callback

    monkeypatch.setattr(database_engine, "create_engine", fake_create_engine)
    monkeypatch.setattr(database_engine.event, "listen", fake_listen)

    returned = database_engine.create_database_engine(
        Settings.model_validate({"database_role": role})
    )

    assert returned is engine
    assert listener is not None
    expected_role = expected_role_sql.removeprefix("SET ROLE ")
    connection = RecordingDbapiConnection(
        (
            expected_role,
            f"{expected_role}_login",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            [expected_role],
        )
    )
    listener(connection, object(), object())
    assert connection.cursor_instance.statements[:3] == [
        "RESET ROLE",
        expected_role_sql,
        "SET search_path TO pg_catalog, careerops",
    ]
    assert "FROM pg_catalog.pg_roles AS login" in connection.cursor_instance.statements[3]
    assert connection.cursor_instance.closed is True
    assert connection.autocommit_changes == [True, False]
    assert connection.autocommit is False


def test_engine_rejects_elevated_or_inheriting_session_login() -> None:
    expected_role = "careerops_api"
    connection = RecordingDbapiConnection(
        (
            expected_role,
            "unsafe_runtime_login",
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            [expected_role, "careerops_outbox"],
        )
    )
    listener = database_engine._runtime_session_initializer(DatabaseCapabilityRole.API)

    with pytest.raises(database_engine.UnsafeDatabaseIdentity):
        listener(connection, object(), object())  # type: ignore[arg-type]

    assert connection.cursor_instance.closed is True
    assert connection.autocommit_changes == [True, False]
