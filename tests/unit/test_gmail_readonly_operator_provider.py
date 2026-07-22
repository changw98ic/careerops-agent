from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from careerops.api.gmail_readonly import (
    GmailReadonlyConflict,
    GmailReadonlyNotFound,
    GmailReadonlyUnavailable,
)
from careerops.infrastructure.gmail_operator import (
    RuntimeGmailReadonlyOperatorProvider,
    TransactionFactory,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000002001")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000002002")
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000002003")
RUN_ID = UUID("00000000-0000-0000-0000-000000002004")
SIGNAL_ID = UUID("00000000-0000-0000-0000-000000002005")
PROPOSAL_ID = UUID("00000000-0000-0000-0000-000000002006")
DECISION_ID = UUID("00000000-0000-0000-0000-000000002007")
COMMAND_ID = UUID("00000000-0000-0000-0000-000000002008")
CANONICAL_JOB_ID = UUID("00000000-0000-0000-0000-000000002009")
JOB_POSTING_ID = UUID("00000000-0000-0000-0000-00000000200a")
SNAPSHOT_SHA256 = "a" * 64
EVIDENCE_SHA256 = "b" * 64

_UNSET = object()


class FakeResult:
    def __init__(
        self,
        *,
        scalar: object = _UNSET,
        rows: tuple[Mapping[str, object], ...] = (),
    ) -> None:
        self._scalar = scalar
        self._rows = rows

    def scalar_one(self) -> object:
        assert self._scalar is not _UNSET
        return self._scalar

    def mappings(self) -> FakeResult:
        return self

    def one(self) -> Mapping[str, object]:
        assert len(self._rows) == 1
        return self._rows[0]

    def all(self) -> list[Mapping[str, object]]:
        return list(self._rows)


class FakeConnection:
    def __init__(self, outcomes: list[FakeResult | Exception]) -> None:
        self._outcomes = deque(outcomes)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(
        self,
        statement: object,
        parameters: Mapping[str, object] | None = None,
    ) -> FakeResult:
        self.calls.append((str(statement), dict(parameters or {})))
        outcome = self._outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeTransaction(AbstractContextManager[Connection]):
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> Connection:
        return cast(Connection, self._connection)

    def __exit__(self, *_args: object) -> None:
        return None


class FakeTransactionFactory:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def __call__(self) -> AbstractContextManager[Connection]:
        return FakeTransaction(self._connection)


@pytest.mark.asyncio
async def test_runtime_provider_maps_all_owner_scoped_functions_to_safe_responses() -> None:
    pending_proposal = _proposal_row()
    approved_proposal = _proposal_row(
        decision="approve",
        canonical_job_id=CANONICAL_JOB_ID,
        job_posting_id=JOB_POSTING_ID,
    )
    connection = FakeConnection(
        [
            FakeResult(scalar=ACCOUNT_ID),
            FakeResult(rows=(_account_row(),)),
            FakeResult(rows=(_account_row(),)),
            FakeResult(rows=(_account_row(),)),
            FakeResult(scalar=RUN_ID),
            FakeResult(rows=(_run_row(),)),
            FakeResult(rows=(_run_row(),)),
            FakeResult(rows=(pending_proposal,)),
            FakeResult(rows=(pending_proposal,)),
            FakeResult(scalar=DECISION_ID),
            FakeResult(rows=(approved_proposal,)),
            FakeResult(scalar=RUN_ID),
            FakeResult(rows=(_account_row(status="sync_required"),)),
            FakeResult(),
            FakeResult(rows=(_account_row(status="revoked"),)),
        ]
    )
    provider = _provider(connection)

    registered = await provider.register_account(
        actor_id=ACTOR_ID,
        command_id=COMMAND_ID,
        candidate_id=CANDIDATE_ID,
        credential_handle="vault:gmail/operator@example.com",
        account_subject="operator@example.com",
        dedicated=True,
        oauth_client_mode="byo",
        publishing_status="testing",
        credential_store_evidence_sha256=EVIDENCE_SHA256,
        now=NOW,
    )
    accounts = await provider.list_accounts(actor_id=ACTOR_ID, limit=25)
    status = await provider.account_status(actor_id=ACTOR_ID, account_id=ACCOUNT_ID)
    queued = await provider.request_sync(
        actor_id=ACTOR_ID,
        account_id=ACCOUNT_ID,
        command_id=COMMAND_ID,
        reason="manual",
        now=NOW,
    )
    runs = await provider.list_sync_runs(actor_id=ACTOR_ID, account_id=ACCOUNT_ID, limit=20)
    proposals = await provider.list_proposals(
        actor_id=ACTOR_ID,
        account_id=ACCOUNT_ID,
        limit=15,
    )
    reviewed = await provider.review_proposal(
        actor_id=ACTOR_ID,
        account_id=ACCOUNT_ID,
        proposal_id=PROPOSAL_ID,
        command_id=COMMAND_ID,
        decision="approve",
        snapshot_sha256=SNAPSHOT_SHA256,
        canonical_job_id=CANONICAL_JOB_ID,
        job_posting_id=JOB_POSTING_ID,
        reason="matched to reviewed job",
        now=NOW,
    )
    reset = await provider.reset_history(
        actor_id=ACTOR_ID,
        account_id=ACCOUNT_ID,
        command_id=COMMAND_ID,
        snapshot_sha256=SNAPSHOT_SHA256,
        reason="history cursor expired",
        now=NOW,
    )
    revoked = await provider.revoke_account(
        actor_id=ACTOR_ID,
        account_id=ACCOUNT_ID,
        command_id=COMMAND_ID,
        reason="operator disconnected Gmail",
        now=NOW,
    )

    assert registered.account.snapshot_sha256 == SNAPSHOT_SHA256
    assert accounts.accounts == (registered.account,)
    assert status.account.account_id == ACCOUNT_ID
    assert queued.run.run_id == RUN_ID
    assert runs.runs == (queued.run,)
    assert proposals.proposals[0].proposal_status == "pending_review"
    assert reviewed.proposal.proposal_status == "approved"
    assert reviewed.proposal.canonical_job_id == CANONICAL_JOB_ID
    assert reset.account.status == "sync_required"
    assert revoked.account.status == "revoked"

    function_names = {
        "gmail_readonly_register_account",
        "gmail_readonly_list_accounts",
        "gmail_readonly_account_status",
        "gmail_readonly_request_sync",
        "gmail_readonly_list_sync_runs",
        "gmail_readonly_list_proposals",
        "gmail_readonly_review_proposal",
        "gmail_readonly_reset_history",
        "gmail_readonly_revoke_account",
    }
    rendered_sql = "\n".join(statement for statement, _parameters in connection.calls)
    assert all(name in rendered_sql for name in function_names)
    assert "careerops.gmail_accounts" not in rendered_sql
    assert "careerops.gmail_sync_runs" not in rendered_sql
    assert "careerops.gmail_signal_proposals" not in rendered_sql

    review_parameters = next(
        parameters
        for statement, parameters in connection.calls
        if "gmail_readonly_review_proposal" in statement
    )
    assert review_parameters["owner_user_id"] == ACTOR_ID
    assert review_parameters["payload_sha256"] == SNAPSHOT_SHA256
    assert review_parameters["idempotency_key"] == str(COMMAND_ID)
    assert review_parameters["trace_id"] == f"gmail-command:{COMMAND_ID}"

    response_payloads = (
        registered.model_dump(mode="json"),
        accounts.model_dump(mode="json"),
        status.model_dump(mode="json"),
        queued.model_dump(mode="json"),
        runs.model_dump(mode="json"),
        proposals.model_dump(mode="json"),
        reviewed.model_dump(mode="json"),
        reset.model_dump(mode="json"),
        revoked.model_dump(mode="json"),
    )
    assert all(_contains_no_internal_fields(payload) for payload in response_payloads)
    assert not connection._outcomes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("23503", GmailReadonlyNotFound),
        ("23505", GmailReadonlyConflict),
        ("23514", GmailReadonlyConflict),
        ("08006", GmailReadonlyUnavailable),
    ],
)
async def test_runtime_provider_maps_database_errors_without_raw_details(
    sqlstate: str,
    expected: type[Exception],
) -> None:
    error = DBAPIError(
        "SELECT credential_handle FROM secret",
        {"credential_handle": "plaintext-secret"},
        FakeDatabaseError(sqlstate),
    )
    provider = _provider(FakeConnection([error]))

    with pytest.raises(expected) as raised:
        await provider.account_status(actor_id=ACTOR_ID, account_id=ACCOUNT_ID)

    assert "plaintext-secret" not in str(raised.value)
    assert "credential_handle" not in str(raised.value)


@pytest.mark.asyncio
async def test_runtime_provider_maps_unavailable_transaction_factory_without_raw_details() -> None:
    def unavailable_factory() -> AbstractContextManager[Connection]:
        raise RuntimeError("postgresql://operator:plaintext-secret@database")

    provider = RuntimeGmailReadonlyOperatorProvider(transaction_factory=unavailable_factory)

    with pytest.raises(GmailReadonlyUnavailable) as raised:
        await provider.list_accounts(actor_id=ACTOR_ID, limit=10)

    assert str(raised.value) == "gmail read-only database unavailable"


@pytest.mark.asyncio
async def test_review_requires_proposal_to_belong_to_account_path() -> None:
    connection = FakeConnection([FakeResult(rows=())])
    provider = _provider(connection)

    with pytest.raises(GmailReadonlyNotFound):
        await provider.review_proposal(
            actor_id=ACTOR_ID,
            account_id=ACCOUNT_ID,
            proposal_id=PROPOSAL_ID,
            command_id=COMMAND_ID,
            decision="approve",
            snapshot_sha256=SNAPSHOT_SHA256,
            canonical_job_id=None,
            job_posting_id=None,
            reason=None,
            now=NOW,
        )

    assert len(connection.calls) == 1
    assert "gmail_readonly_list_proposals" in connection.calls[0][0]


class FakeDatabaseError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("raw database failure")
        self.sqlstate = sqlstate


def _provider(connection: FakeConnection) -> RuntimeGmailReadonlyOperatorProvider:
    factory = FakeTransactionFactory(connection)
    return RuntimeGmailReadonlyOperatorProvider(
        transaction_factory=cast(TransactionFactory, factory)
    )


def _account_row(
    *,
    status: str = "active",
) -> dict[str, object]:
    return {
        "account_id": ACCOUNT_ID,
        "owner_user_id": ACTOR_ID,
        "candidate_id": CANDIDATE_ID,
        "provider": "gmail",
        "account_subject": "operator@example.com",
        "sync_mode": "polling",
        "publishing_status": "testing",
        "status": status,
        "version": 1,
        "last_history_id": "42",
        "next_page_token": None,
        "last_synced_at": NOW,
        "last_full_sync_at": None,
        "last_error_code": None,
        "created_at": NOW,
        "updated_at": NOW,
        "snapshot_sha256": SNAPSHOT_SHA256,
        "oauth_credential_reference_id": UUID("00000000-0000-0000-0000-00000000200b"),
        "idempotency_key": "must-not-escape",
        "trace_id": "must-not-escape",
    }


def _run_row() -> dict[str, object]:
    return {
        "id": RUN_ID,
        "gmail_account_id": ACCOUNT_ID,
        "owner_user_id": ACTOR_ID,
        "reason": "manual",
        "status": "queued",
        "created_at": NOW,
        "started_at": None,
        "completed_at": None,
        "failed_at": None,
        "history_start_id": "42",
        "history_end_id": None,
        "next_page_token": None,
        "message_count": 0,
        "last_error_code": None,
        "fencing_token": UUID("00000000-0000-0000-0000-00000000200c"),
        "idempotency_key": "must-not-escape",
        "trace_id": "must-not-escape",
    }


def _proposal_row(
    *,
    decision: str | None = None,
    canonical_job_id: UUID | None = None,
    job_posting_id: UUID | None = None,
) -> dict[str, object]:
    return {
        "proposal_id": PROPOSAL_ID,
        "gmail_account_id": ACCOUNT_ID,
        "owner_user_id": ACTOR_ID,
        "gmail_message_signal_id": SIGNAL_ID,
        "signal_sha256": EVIDENCE_SHA256,
        "classification": "interview_invitation",
        "relevance": "relevant",
        "confidence": Decimal("0.950"),
        "redacted_excerpt": "Recruiter requested interview availability.",
        "proposal_kind": "application_status_update",
        "review_priority": "high",
        "payload_json": {
            "version": "gmail-readonly-proposal.v1",
            "classification": "interview_invitation",
        },
        "payload_sha256": SNAPSHOT_SHA256,
        "decision": decision,
        "canonical_job_id": canonical_job_id,
        "job_posting_id": job_posting_id,
        "created_at": NOW,
        "idempotency_key": "must-not-escape",
        "trace_id": "must-not-escape",
    }


def _contains_no_internal_fields(value: object) -> bool:
    forbidden = {
        "credential_handle",
        "credential_reference_id",
        "oauth_credential_reference_id",
        "idempotency_key",
        "trace_id",
        "fencing_token",
        "lease_token",
    }
    if isinstance(value, dict):
        return all(
            key not in forbidden and _contains_no_internal_fields(item)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return all(_contains_no_internal_fields(item) for item in value)
    return True
