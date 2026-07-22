from __future__ import annotations

import ast
import hashlib
import inspect
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from careerops.application import autopilot_kill_switches
from careerops.application.autopilot_kill_switches import (
    AutopilotKillSwitchEventRef,
    AutopilotKillSwitchScope,
    AutopilotKillSwitchScopeType,
    AutopilotKillSwitchService,
    AutopilotKillSwitchState,
    CurrentAutopilotKillSwitchState,
    SetAutopilotKillSwitchCommand,
)

NOW = datetime(2026, 7, 20, 11, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")


def test_scope_shape_is_strict_and_stable() -> None:
    campaign_id = uuid4()

    assert AutopilotKillSwitchScope.global_scope().stable_key == "global"
    assert AutopilotKillSwitchScope.campaign(campaign_id).stable_key == f"campaign:{campaign_id}"
    assert AutopilotKillSwitchScope.provider_scope("greenhouse").stable_key == "provider:greenhouse"

    with pytest.raises(ValueError, match="global"):
        AutopilotKillSwitchScope(
            AutopilotKillSwitchScopeType.GLOBAL,
            campaign_id=campaign_id,
        )
    with pytest.raises(ValueError, match="campaign"):
        AutopilotKillSwitchScope(AutopilotKillSwitchScopeType.CAMPAIGN)
    with pytest.raises(ValueError, match="provider"):
        AutopilotKillSwitchScope(
            AutopilotKillSwitchScopeType.PROVIDER,
            provider="bad provider",
        )


def test_command_validates_authenticated_actor_reason_and_stable_idempotency_key() -> None:
    command = SetAutopilotKillSwitchCommand(
        actor_id=ACTOR_ID,
        scope=AutopilotKillSwitchScope.provider_scope("greenhouse"),
        state=AutopilotKillSwitchState.ACTIVE,
        reason="provider outage",
        trace_id="trace-1",
    )

    reason_hash = hashlib.sha256(b"provider outage").hexdigest()
    assert command.active
    assert command.idempotency_key == (
        f"autopilot-kill-switch:provider:greenhouse:active:{ACTOR_ID}:trace-1:{reason_hash}"
    )
    with pytest.raises(FrozenInstanceError):
        command.reason = "changed"  # type: ignore[misc]

    with pytest.raises(ValueError, match="reason"):
        SetAutopilotKillSwitchCommand(
            actor_id=ACTOR_ID,
            scope=AutopilotKillSwitchScope.global_scope(),
            state=AutopilotKillSwitchState.ACTIVE,
            reason=" ",
            trace_id="trace-1",
        )
    with pytest.raises(ValueError, match="trace_id"):
        SetAutopilotKillSwitchCommand(
            actor_id=ACTOR_ID,
            scope=AutopilotKillSwitchScope.global_scope(),
            state=AutopilotKillSwitchState.INACTIVE,
            reason="resume",
            trace_id="bad trace",
        )


def test_current_state_defaults_and_validates_server_time_shape() -> None:
    scope = AutopilotKillSwitchScope.global_scope()
    current = CurrentAutopilotKillSwitchState(
        scope=scope,
        state=AutopilotKillSwitchState.ACTIVE,
        reason="emergency stop",
        actor_id=ACTOR_ID,
        event_id=1,
        changed_at=NOW,
    )

    assert current.active
    with pytest.raises(ValueError, match="timezone-aware"):
        CurrentAutopilotKillSwitchState(
            scope=scope,
            state=AutopilotKillSwitchState.INACTIVE,
            changed_at=datetime(2026, 7, 20, 11, 0),
        )


def test_service_delegates_without_execution_or_model_capability() -> None:
    class RecordingStore:
        def __init__(self) -> None:
            self.commands: list[SetAutopilotKillSwitchCommand] = []

        def append_event(
            self,
            command: SetAutopilotKillSwitchCommand,
        ) -> AutopilotKillSwitchEventRef:
            self.commands.append(command)
            return AutopilotKillSwitchEventRef(
                event_id=uuid4(),
                scope=command.scope,
                state=command.state,
                newly_created=True,
            )

        def current_state(
            self,
            scope: AutopilotKillSwitchScope,
        ) -> CurrentAutopilotKillSwitchState:
            return CurrentAutopilotKillSwitchState(
                scope=scope,
                state=AutopilotKillSwitchState.INACTIVE,
            )

    store = RecordingStore()
    service = AutopilotKillSwitchService(store)
    activate = SetAutopilotKillSwitchCommand(
        actor_id=ACTOR_ID,
        scope=AutopilotKillSwitchScope.global_scope(),
        state=AutopilotKillSwitchState.ACTIVE,
        reason="emergency stop",
        trace_id="trace-1",
    )
    deactivate = SetAutopilotKillSwitchCommand(
        actor_id=ACTOR_ID,
        scope=AutopilotKillSwitchScope.global_scope(),
        state=AutopilotKillSwitchState.INACTIVE,
        reason="resume",
        trace_id="trace-2",
    )

    assert service.activate(activate).state is AutopilotKillSwitchState.ACTIVE
    assert service.deactivate(deactivate).state is AutopilotKillSwitchState.INACTIVE
    assert service.current_state(AutopilotKillSwitchScope.global_scope()).active is False
    assert store.commands == [activate, deactivate]

    with pytest.raises(ValueError, match="activate"):
        service.activate(deactivate)
    with pytest.raises(ValueError, match="deactivate"):
        service.deactivate(activate)


def test_application_module_does_not_import_execution_capabilities() -> None:
    tree = ast.parse(inspect.getsource(autopilot_kill_switches))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    forbidden_fragments = (
        "outbox",
        "browser",
        "credential",
        "provider",
        "requests",
        "httpx",
        "playwright",
        "side_effect",
        "openai",
    )
    assert not any(
        fragment in module for module in imported_modules for fragment in forbidden_fragments
    )
