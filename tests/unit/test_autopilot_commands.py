from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from careerops.application import autopilot_commands
from careerops.application.autopilot_commands import (
    AutopilotGrantCommandService,
    AutopilotGrantScopeDraft,
    CampaignDraft,
    CreateCampaignGrantCommand,
    RevokeGrantCommand,
    SupersedeGrantCommand,
)
from careerops.infrastructure.database import autopilot_commands as db_commands
from careerops.infrastructure.database.autopilot_commands import (
    _insert_revocation_once_statement,
    _lock_grant_revocation_state_statement,
    _lock_owned_campaign_statement,
    _lock_owned_grant_campaign_statement,
    compile_query_for_test,
)

NOW = datetime(2026, 7, 19, 9, 30, tzinfo=UTC)
HASH = "a" * 64


def scope(
    *,
    subject_actor: str = "agent:careerops",
    expires_at: datetime = NOW + timedelta(days=7),
) -> AutopilotGrantScopeDraft:
    return AutopilotGrantScopeDraft(
        subject_actor=subject_actor,
        allowed_action_kinds=("submit_application",),
        allowed_channels=("ats",),
        allowed_target_hosts=("greenhouse.io",),
        material_hashes=(HASH,),
        max_total_submissions=10,
        max_daily_submissions=2,
        max_per_company=1,
        policy_ruleset_version="policy-2026-07-19",
        release_version="m2-g002",
        expires_at=expires_at,
    )


def test_grant_scope_freezes_and_validates_authority_boundary() -> None:
    draft = scope()

    assert draft.allowed_action_kinds == ("submit_application",)
    assert isinstance(draft.allowed_target_hosts, tuple)
    with pytest.raises(FrozenInstanceError):
        draft.max_total_submissions = 20  # type: ignore[misc]

    with pytest.raises(ValueError, match="material_hashes"):
        AutopilotGrantScopeDraft(
            subject_actor="agent:careerops",
            allowed_action_kinds=("submit_application",),
            allowed_channels=("ats",),
            allowed_target_hosts=("greenhouse.io",),
            material_hashes=("not-a-hash",),
            max_total_submissions=10,
            max_daily_submissions=2,
            max_per_company=1,
            policy_ruleset_version="policy-2026-07-19",
            release_version="m2-g002",
            expires_at=NOW + timedelta(days=7),
        )

    with pytest.raises(ValueError, match="timezone-aware"):
        scope(expires_at=datetime(2026, 7, 19, 9, 30))


def test_campaign_commands_validate_trace_and_reason_inputs() -> None:
    actor_id = uuid4()
    with pytest.raises(ValueError, match="trace_id"):
        CreateCampaignGrantCommand(
            actor_id=actor_id,
            campaign=CampaignDraft(name="授权", objective="后端岗位"),
            grant_scope=scope(subject_actor=str(actor_id)),
            trace_id="bad trace with spaces",
        )

    with pytest.raises(ValueError, match="reason"):
        RevokeGrantCommand(
            actor_id=uuid4(),
            grant_version_id=uuid4(),
            reason="",
            trace_id="trace-1",
        )

    with pytest.raises(ValueError, match="reason"):
        SupersedeGrantCommand(
            actor_id=actor_id,
            campaign_id=uuid4(),
            current_grant_version_id=uuid4(),
            replacement_scope=scope(subject_actor=str(actor_id)),
            reason=" ",
            trace_id="trace-1",
        )


def test_campaign_commands_reject_scope_for_different_actor() -> None:
    actor_id = uuid4()
    other_actor_id = uuid4()

    with pytest.raises(ValueError, match="subject_actor"):
        CreateCampaignGrantCommand(
            actor_id=actor_id,
            campaign=CampaignDraft(name="授权", objective="后端岗位"),
            grant_scope=scope(subject_actor=str(other_actor_id)),
            trace_id="trace-1",
        )

    with pytest.raises(ValueError, match="subject_actor"):
        SupersedeGrantCommand(
            actor_id=actor_id,
            campaign_id=uuid4(),
            current_grant_version_id=uuid4(),
            replacement_scope=scope(subject_actor=str(other_actor_id)),
            reason="收窄岗位范围",
            trace_id="trace-2",
        )


def test_service_delegates_without_side_effect_capability() -> None:
    class RecordingStore:
        def __init__(self) -> None:
            self.commands: list[object] = []

        def create_campaign_with_grant(self, command: CreateCampaignGrantCommand) -> object:
            self.commands.append(command)
            return "created"

        def supersede_grant(self, command: SupersedeGrantCommand) -> object:
            self.commands.append(command)
            return "superseded"

        def revoke_grant(self, command: RevokeGrantCommand) -> object:
            self.commands.append(command)
            return "revoked"

    store = RecordingStore()
    service = AutopilotGrantCommandService(store)  # type: ignore[arg-type]
    create_actor_id = uuid4()
    supersede_actor_id = uuid4()
    create = CreateCampaignGrantCommand(
        actor_id=create_actor_id,
        campaign=CampaignDraft(name="授权", objective="后端岗位"),
        grant_scope=scope(subject_actor=str(create_actor_id)),
        trace_id="trace-1",
    )
    supersede = SupersedeGrantCommand(
        actor_id=supersede_actor_id,
        campaign_id=uuid4(),
        current_grant_version_id=uuid4(),
        replacement_scope=scope(subject_actor=str(supersede_actor_id)),
        reason="收窄岗位范围",
        trace_id="trace-2",
    )
    revoke = RevokeGrantCommand(
        actor_id=uuid4(),
        grant_version_id=uuid4(),
        reason="停止 campaign",
        trace_id="trace-3",
    )

    assert service.create_campaign_with_grant(create) == "created"
    assert service.supersede_grant(supersede) == "superseded"
    assert service.revoke_grant(revoke) == "revoked"
    assert store.commands == [create, supersede, revoke]


def test_postgres_store_lock_statements_bind_actor_ownership_and_for_update() -> None:
    campaign_id = uuid4()
    actor_id = uuid4()
    grant_version_id = uuid4()

    campaign_sql = compile_query_for_test(_lock_owned_campaign_statement(campaign_id, actor_id))
    grant_sql = compile_query_for_test(
        _lock_owned_grant_campaign_statement(grant_version_id, actor_id)
    )
    grant_state_lock_sql = compile_query_for_test(
        _lock_grant_revocation_state_statement(grant_version_id)
    )

    assert "careerops.autopilot_campaigns.owner_user_id" in campaign_sql
    assert str(actor_id) in campaign_sql
    assert "FOR UPDATE OF autopilot_campaigns" in campaign_sql
    assert "JOIN careerops.autopilot_campaigns" in grant_sql
    assert "careerops.autopilot_campaigns.owner_user_id" in grant_sql
    assert "careerops.autopilot_grant_versions.id" in grant_sql
    assert str(grant_version_id) in grant_sql
    assert "FOR UPDATE OF autopilot_campaigns" in grant_sql
    assert "pg_advisory_xact_lock" in grant_state_lock_sql
    assert f"careerops:autopilot-grant-state:{grant_version_id}" in grant_state_lock_sql


def test_postgres_revocation_insert_is_postgresql_idempotent_without_integrity_error() -> None:
    revocation_id = uuid4()
    grant_version_id = uuid4()
    actor_id = uuid4()

    sql = compile_query_for_test(
        _insert_revocation_once_statement(
            revocation_id=revocation_id,
            grant_version_id=grant_version_id,
            revoked_by_user_id=actor_id,
            superseded_by_grant_version_id=None,
            reason="停止 campaign",
        )
    )

    assert "INSERT INTO careerops.autopilot_grant_revocations" in sql
    assert "ON CONFLICT (grant_version_id) DO NOTHING" in sql
    assert "RETURNING careerops.autopilot_grant_revocations.id" in sql
    assert str(revocation_id) in sql
    assert str(grant_version_id) in sql


class _FakeMappingResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def first(self) -> dict[str, object] | None:
        return self._row


class _FakeExecuteResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> _FakeMappingResult:
        return _FakeMappingResult(self._row)


class _FakeConnection:
    def __init__(
        self,
        *,
        grant_row: dict[str, object],
        first_revocation_row: dict[str, object] | None = None,
        insert_revocation_row: dict[str, object] | None = None,
        selected_revocation_row: dict[str, object] | None = None,
    ) -> None:
        self._grant_row = grant_row
        self._first_revocation_row = first_revocation_row
        self._insert_revocation_row = insert_revocation_row
        self._selected_revocation_row = selected_revocation_row
        self.statements: list[str] = []
        self.revocation_select_count = 0

    def in_transaction(self) -> bool:
        return True

    def execute(self, statement: object) -> _FakeExecuteResult:
        sql = compile_query_for_test(statement)  # type: ignore[arg-type]
        self.statements.append(sql)
        if "pg_advisory_xact_lock" in sql:
            return _FakeExecuteResult(None)
        if "FOR UPDATE OF autopilot_campaigns" in sql:
            return _FakeExecuteResult(self._grant_row)
        if "INSERT INTO careerops.autopilot_grant_revocations" in sql:
            return _FakeExecuteResult(self._insert_revocation_row)
        if "FROM careerops.autopilot_grant_revocations" in sql:
            self.revocation_select_count += 1
            if self.revocation_select_count == 1:
                return _FakeExecuteResult(self._first_revocation_row)
            return _FakeExecuteResult(self._selected_revocation_row)
        raise AssertionError(f"unexpected SQL: {sql}")


def test_revoke_grant_appends_audit_only_when_revocation_insert_returns_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = uuid4()
    campaign_id = uuid4()
    grant_version_id = uuid4()
    inserted_revocation_id = uuid4()
    connection = _FakeConnection(
        grant_row={"campaign_id": campaign_id, "grant_version_id": grant_version_id},
        insert_revocation_row={
            "id": inserted_revocation_id,
            "superseded_by_grant_version_id": None,
        },
    )
    store = db_commands.PostgresAutopilotCommandStore(connection)  # type: ignore[arg-type]
    audit_events: list[dict[str, object]] = []
    monkeypatch.setattr(store, "_append_audit", lambda **kwargs: audit_events.append(kwargs))

    result = store.revoke_grant(
        RevokeGrantCommand(
            actor_id=actor_id,
            grant_version_id=grant_version_id,
            reason="停止 campaign",
            trace_id="trace-revoke-created",
        )
    )

    assert result.newly_created is True
    assert result.grant_version_id == grant_version_id
    assert result.campaign_id == campaign_id
    assert len(audit_events) == 1
    assert audit_events[0]["event_type"] == "autopilot.campaign_grant.revoked"
    assert "pg_advisory_xact_lock" in connection.statements[0]
    assert "FOR UPDATE OF autopilot_campaigns" in connection.statements[1]
    assert any("ON CONFLICT (grant_version_id) DO NOTHING" in sql for sql in connection.statements)


def test_revoke_grant_conflict_path_selects_existing_without_audit_or_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign_id = uuid4()
    grant_version_id = uuid4()
    existing_revocation_id = uuid4()
    superseded_by_grant_version_id = uuid4()
    connection = _FakeConnection(
        grant_row={"campaign_id": campaign_id, "grant_version_id": grant_version_id},
        insert_revocation_row=None,
        selected_revocation_row={
            "id": existing_revocation_id,
            "superseded_by_grant_version_id": superseded_by_grant_version_id,
        },
    )
    store = db_commands.PostgresAutopilotCommandStore(connection)  # type: ignore[arg-type]
    audit_events: list[dict[str, object]] = []
    monkeypatch.setattr(store, "_append_audit", lambda **kwargs: audit_events.append(kwargs))

    result = store.revoke_grant(
        RevokeGrantCommand(
            actor_id=uuid4(),
            grant_version_id=grant_version_id,
            reason="停止 campaign",
            trace_id="trace-revoke-conflict",
        )
    )

    assert result.newly_created is False
    assert result.revocation_id == existing_revocation_id
    assert result.superseded_by_grant_version_id == superseded_by_grant_version_id
    assert "pg_advisory_xact_lock" in connection.statements[0]
    assert "FOR UPDATE OF autopilot_campaigns" in connection.statements[1]
    assert audit_events == []
    assert connection.revocation_select_count == 2


def test_autopilot_command_modules_do_not_import_execution_capabilities() -> None:
    forbidden_fragments = (
        "outbox",
        "browser",
        "credential",
        "provider",
        "requests",
        "httpx",
        "playwright",
        "side_effect",
    )
    for module in (autopilot_commands, db_commands):
        import_lines = [
            line
            for line in inspect.getsource(module).splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        for fragment in forbidden_fragments:
            assert all(fragment not in line for line in import_lines)
