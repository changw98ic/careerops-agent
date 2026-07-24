"""Integration tests for ``PostgresSideEffectStore``.

Requires ``CAREEROPS_TEST_DATABASE_URL`` pointing to a disposable PostgreSQL.
Runs the full M5A authorization chain: propose -> approve -> execute -> receipt.

``@pytest.mark.integration`` marks this as a DB-dependent test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from careerops.application.side_effect_kernel import (
    ProposalInput,
    SideEffectKernel,
)
from careerops.domain.side_effects import (
    ApprovalDecision,
    IntentStatus,
)
from careerops.infrastructure.database.side_effect_postgres import (
    PostgresSideEffectStore,
)
from careerops.integrations.fake_side_effect_provider import FakeSideEffectProvider

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required")
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = create_engine(database_url)
    # Run migrations to ensure tables exist.
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.attributes["database_url"] = database_url
    command.upgrade(alembic_cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture()
def store(engine: Engine) -> PostgresSideEffectStore:
    return PostgresSideEffectStore(engine)


@pytest.fixture()
def provider() -> FakeSideEffectProvider:
    return FakeSideEffectProvider()


@pytest.fixture()
def kernel(
    store: PostgresSideEffectStore,
    provider: FakeSideEffectProvider,
) -> SideEffectKernel:
    return SideEffectKernel(store, provider)


def _make_proposal(**overrides: object) -> ProposalInput:
    defaults = {
        "action_kind": "send_email",
        "resource_type": "email_thread",
        "resource_id": uuid4(),
        "idempotency_key": f"key-{uuid4().hex}",
        "created_by": "test",
        "target": {"to": "recruiter@example.com"},
        "payload": {"subject": "Hello", "body": "World"},
        "evidence_refs": ("evidence:1",),
        "trusted_facts": {"capability_released": True, "target_allowlisted": True},
        "authenticated": True,
    }
    defaults.update(overrides)
    return ProposalInput(**defaults)  # type: ignore[arg-type]


class TestPropose:
    def test_propose_creates_intent_and_payload(self, kernel: SideEffectKernel) -> None:
        proposal = _make_proposal()
        result = kernel.propose(proposal, now=NOW)

        assert result.intent.id is not None
        assert result.payload_version.version == 1
        assert result.policy_decision is not None

    def test_propose_idempotent(self, kernel: SideEffectKernel) -> None:
        proposal = _make_proposal()
        r1 = kernel.propose(proposal, now=NOW)
        r2 = kernel.propose(proposal, now=NOW)
        assert r1.intent.id == r2.intent.id
        assert r1.payload_version.id == r2.payload_version.id

    def test_propose_finds_by_idempotency_key(
        self, store: PostgresSideEffectStore, kernel: SideEffectKernel
    ) -> None:
        proposal = _make_proposal()
        result = kernel.propose(proposal, now=NOW)
        found = store.find_intent_by_idempotency_key(proposal.idempotency_key)
        assert found is not None
        assert found.id == result.intent.id


class TestApproval:
    def test_get_or_create_pending_approval(self, kernel: SideEffectKernel) -> None:
        proposal = _make_proposal()
        result = kernel.propose(proposal, now=NOW)
        approval = kernel.get_or_create_pending_approval(
            result.intent.id, requested_for="reviewer", now=NOW
        )
        assert approval.decision is ApprovalDecision.PENDING

    def test_get_or_create_idempotent(self, kernel: SideEffectKernel) -> None:
        proposal = _make_proposal()
        result = kernel.propose(proposal, now=NOW)
        a1 = kernel.get_or_create_pending_approval(
            result.intent.id, requested_for="reviewer", now=NOW
        )
        a2 = kernel.get_or_create_pending_approval(
            result.intent.id, requested_for="reviewer", now=NOW
        )
        assert a1.id == a2.id

    def test_decide_approve(self, kernel: SideEffectKernel) -> None:
        proposal = _make_proposal()
        result = kernel.propose(proposal, now=NOW)
        approval = kernel.get_or_create_pending_approval(
            result.intent.id, requested_for="reviewer", now=NOW
        )
        decided = kernel.decide_approval(
            approval.id, action="approve", requested_for="reviewer", now=NOW
        )
        assert decided.decision is ApprovalDecision.APPROVED

    def test_decide_reject(self, kernel: SideEffectKernel) -> None:
        proposal = _make_proposal()
        result = kernel.propose(proposal, now=NOW)
        approval = kernel.get_or_create_pending_approval(
            result.intent.id, requested_for="reviewer", now=NOW
        )
        decided = kernel.decide_approval(
            approval.id, action="reject", requested_for="reviewer", now=NOW
        )
        assert decided.decision is ApprovalDecision.REJECTED


class TestExecute:
    def test_execute_after_approve(self, kernel: SideEffectKernel) -> None:
        proposal = _make_proposal()
        result = kernel.propose(proposal, now=NOW)
        approval = kernel.get_or_create_pending_approval(
            result.intent.id, requested_for="reviewer", now=NOW
        )
        kernel.decide_approval(approval.id, action="approve", requested_for="reviewer", now=NOW)
        outcome = kernel.execute(result.intent.id, now=NOW)
        assert outcome.status is IntentStatus.CONFIRMED
        assert outcome.receipt is not None

    def test_replay_after_execute(self, kernel: SideEffectKernel) -> None:
        proposal = _make_proposal()
        result = kernel.propose(proposal, now=NOW)
        approval = kernel.get_or_create_pending_approval(
            result.intent.id, requested_for="reviewer", now=NOW
        )
        kernel.decide_approval(approval.id, action="approve", requested_for="reviewer", now=NOW)
        kernel.execute(result.intent.id, now=NOW)
        replay = kernel.replay(result.intent.id)
        assert replay.intent.id == result.intent.id
        assert len(replay.approvals) >= 1
        assert replay.approvals[0].decision is ApprovalDecision.APPROVED
        assert len(replay.receipts) >= 1


class TestPayloadVersions:
    def test_list_payload_versions(
        self, store: PostgresSideEffectStore, kernel: SideEffectKernel
    ) -> None:
        proposal = _make_proposal()
        result = kernel.propose(proposal, now=NOW)
        versions = store.list_payload_versions(result.intent.id)
        assert len(versions) >= 1
        assert versions[0].version == 1
