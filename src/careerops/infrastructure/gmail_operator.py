from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from decimal import Decimal
from typing import Literal, NoReturn, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from careerops.api.gmail_readonly import (
    GmailAccountStatusResponse,
    GmailAccountSummary,
    GmailProposalSummary,
    GmailPublishingStatus,
    GmailReadonlyConflict,
    GmailReadonlyNotFound,
    GmailReadonlyUnavailable,
    GmailSyncReason,
    GmailSyncRunSummary,
    ListGmailAccountsResponse,
    ListGmailProposalsResponse,
    ListGmailSyncRunsResponse,
    RegisterGmailAccountResponse,
    RequestGmailSyncResponse,
    ResetGmailHistoryResponse,
    ReviewGmailProposalResponse,
    RevokeGmailAccountResponse,
)

TransactionFactory = Callable[[], AbstractContextManager[Connection]]

_REGISTER_ACCOUNT = sa.text(
    """
    SELECT careerops.gmail_readonly_register_account(
        :owner_user_id, :candidate_id, :credential_handle, :account_subject,
        :publishing_status, :credential_store_evidence_sha256,
        :idempotency_key, :trace_id
    ) AS account_id
    """
)
_LIST_ACCOUNTS = sa.text(
    """
    SELECT *
    FROM careerops.gmail_readonly_list_accounts(:owner_user_id, :limit)
    """
)
_ACCOUNT_STATUS = sa.text(
    """
    SELECT *
    FROM careerops.gmail_readonly_account_status(:owner_user_id, :account_id)
    """
)
_REQUEST_SYNC = sa.text(
    """
    SELECT careerops.gmail_readonly_request_sync(
        :owner_user_id, :account_id, :reason, :idempotency_key, :trace_id
    ) AS run_id
    """
)
_LIST_SYNC_RUNS = sa.text(
    """
    SELECT *
    FROM careerops.gmail_readonly_list_sync_runs(:owner_user_id, :account_id, :limit)
    """
)
_LIST_PROPOSALS = sa.text(
    """
    SELECT *
    FROM careerops.gmail_readonly_list_proposals(:owner_user_id, :account_id, :limit)
    """
)
_REVIEW_PROPOSAL = sa.text(
    """
    SELECT careerops.gmail_readonly_review_proposal(
        :owner_user_id, :proposal_id, :decision, :canonical_job_id,
        :job_posting_id, :reason, :payload_sha256, :idempotency_key, :trace_id
    ) AS decision_id
    """
)
_RESET_HISTORY = sa.text(
    """
    SELECT careerops.gmail_readonly_reset_history(
        :owner_user_id, :account_id, :reason, :idempotency_key,
        :trace_id, :snapshot_sha256
    ) AS run_id
    """
)
_REVOKE_ACCOUNT = sa.text(
    """
    SELECT careerops.gmail_readonly_revoke_account(
        :owner_user_id, :account_id, :reason, :idempotency_key, :trace_id
    )
    """
)


class RuntimeGmailReadonlyOperatorProvider:
    """Authenticated Gmail operator adapter using only 0012 owner-scoped functions."""

    def __init__(self, *, transaction_factory: TransactionFactory) -> None:
        self._transaction_factory = transaction_factory

    async def register_account(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        credential_handle: str,
        account_subject: str,
        dedicated: Literal[True],
        oauth_client_mode: Literal["byo"],
        publishing_status: GmailPublishingStatus,
        credential_store_evidence_sha256: str,
        now: datetime,
    ) -> RegisterGmailAccountResponse:
        del now
        if dedicated is not True or oauth_client_mode != "byo":
            raise GmailReadonlyConflict()

        def execute(connection: Connection) -> RegisterGmailAccountResponse:
            account_id = _scalar_uuid(
                connection,
                _REGISTER_ACCOUNT,
                {
                    "owner_user_id": actor_id,
                    "candidate_id": candidate_id,
                    "credential_handle": credential_handle,
                    "account_subject": account_subject,
                    "publishing_status": publishing_status,
                    "credential_store_evidence_sha256": credential_store_evidence_sha256,
                    **_command_parameters(command_id),
                },
            )
            account = _account_from_row(_account_status_row(connection, actor_id, account_id))
            return RegisterGmailAccountResponse(account=account)

        return await self._run(execute)

    async def list_accounts(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListGmailAccountsResponse:
        def execute(connection: Connection) -> ListGmailAccountsResponse:
            rows = _rows(
                connection,
                _LIST_ACCOUNTS,
                {"owner_user_id": actor_id, "limit": limit},
            )
            return ListGmailAccountsResponse(accounts=tuple(_account_from_row(row) for row in rows))

        return await self._run(execute)

    async def account_status(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
    ) -> GmailAccountStatusResponse:
        def execute(connection: Connection) -> GmailAccountStatusResponse:
            account = _account_from_row(_account_status_row(connection, actor_id, account_id))
            return GmailAccountStatusResponse(account=account)

        return await self._run(execute)

    async def request_sync(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        command_id: UUID,
        reason: GmailSyncReason,
        now: datetime,
    ) -> RequestGmailSyncResponse:
        del now

        def execute(connection: Connection) -> RequestGmailSyncResponse:
            run_id = _scalar_uuid(
                connection,
                _REQUEST_SYNC,
                {
                    "owner_user_id": actor_id,
                    "account_id": account_id,
                    "reason": reason,
                    **_command_parameters(command_id),
                },
            )
            run = _find_sync_run(
                _sync_run_rows(connection, actor_id, account_id, limit=100),
                run_id,
            )
            return RequestGmailSyncResponse(run=_sync_run_from_row(run))

        return await self._run(execute)

    async def list_sync_runs(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        limit: int,
    ) -> ListGmailSyncRunsResponse:
        def execute(connection: Connection) -> ListGmailSyncRunsResponse:
            rows = _sync_run_rows(connection, actor_id, account_id, limit=limit)
            return ListGmailSyncRunsResponse(runs=tuple(_sync_run_from_row(row) for row in rows))

        return await self._run(execute)

    async def list_proposals(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        limit: int,
    ) -> ListGmailProposalsResponse:
        def execute(connection: Connection) -> ListGmailProposalsResponse:
            rows = _proposal_rows(connection, actor_id, account_id, limit=limit)
            return ListGmailProposalsResponse(
                proposals=tuple(_proposal_from_row(row) for row in rows)
            )

        return await self._run(execute)

    async def review_proposal(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        proposal_id: UUID,
        command_id: UUID,
        decision: Literal["approve", "reject"],
        snapshot_sha256: str,
        canonical_job_id: UUID | None,
        job_posting_id: UUID | None,
        reason: str | None,
        now: datetime,
    ) -> ReviewGmailProposalResponse:
        del now

        def execute(connection: Connection) -> ReviewGmailProposalResponse:
            visible = _proposal_rows(connection, actor_id, account_id, limit=100)
            _find_proposal(visible, proposal_id)
            _scalar_uuid(
                connection,
                _REVIEW_PROPOSAL,
                {
                    "owner_user_id": actor_id,
                    "proposal_id": proposal_id,
                    "decision": decision,
                    "canonical_job_id": canonical_job_id,
                    "job_posting_id": job_posting_id,
                    "reason": reason or f"authenticated operator {decision}",
                    "payload_sha256": snapshot_sha256,
                    **_command_parameters(command_id),
                },
            )
            updated = _find_proposal(
                _proposal_rows(connection, actor_id, account_id, limit=100),
                proposal_id,
            )
            return ReviewGmailProposalResponse(proposal=_proposal_from_row(updated))

        return await self._run(execute)

    async def reset_history(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        command_id: UUID,
        snapshot_sha256: str,
        reason: str | None,
        now: datetime,
    ) -> ResetGmailHistoryResponse:
        del now

        def execute(connection: Connection) -> ResetGmailHistoryResponse:
            _scalar_uuid(
                connection,
                _RESET_HISTORY,
                {
                    "owner_user_id": actor_id,
                    "account_id": account_id,
                    "reason": reason or "authenticated operator requested history recovery",
                    "snapshot_sha256": snapshot_sha256,
                    **_command_parameters(command_id),
                },
            )
            account = _account_from_row(_account_status_row(connection, actor_id, account_id))
            return ResetGmailHistoryResponse(account=account)

        return await self._run(execute)

    async def revoke_account(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        command_id: UUID,
        reason: str | None,
        now: datetime,
    ) -> RevokeGmailAccountResponse:
        del now

        def execute(connection: Connection) -> RevokeGmailAccountResponse:
            connection.execute(
                _REVOKE_ACCOUNT,
                {
                    "owner_user_id": actor_id,
                    "account_id": account_id,
                    "reason": reason or "authenticated operator revoked Gmail access",
                    **_command_parameters(command_id),
                },
            )
            account = _account_from_row(_account_status_row(connection, actor_id, account_id))
            return RevokeGmailAccountResponse(account=account)

        return await self._run(execute)

    async def _run[T](self, operation: Callable[[Connection], T]) -> T:
        return await asyncio.to_thread(self._run_sync, operation)

    def _run_sync[T](self, operation: Callable[[Connection], T]) -> T:
        try:
            with self._transaction_factory() as connection:
                return operation(connection)
        except (GmailReadonlyNotFound, GmailReadonlyConflict, GmailReadonlyUnavailable):
            raise
        except DBAPIError as exc:
            _raise_database_error(exc)
        except Exception:
            raise GmailReadonlyUnavailable("gmail read-only database unavailable") from None


def _command_parameters(command_id: UUID) -> dict[str, object]:
    return {
        "idempotency_key": str(command_id),
        "trace_id": f"gmail-command:{command_id}",
    }


def _account_status_row(
    connection: Connection,
    actor_id: UUID,
    account_id: UUID,
) -> Mapping[str, object]:
    return _one_row(
        connection,
        _ACCOUNT_STATUS,
        {"owner_user_id": actor_id, "account_id": account_id},
    )


def _sync_run_rows(
    connection: Connection,
    actor_id: UUID,
    account_id: UUID,
    *,
    limit: int,
) -> Sequence[Mapping[str, object]]:
    return _rows(
        connection,
        _LIST_SYNC_RUNS,
        {"owner_user_id": actor_id, "account_id": account_id, "limit": limit},
    )


def _proposal_rows(
    connection: Connection,
    actor_id: UUID,
    account_id: UUID,
    *,
    limit: int,
) -> Sequence[Mapping[str, object]]:
    return _rows(
        connection,
        _LIST_PROPOSALS,
        {"owner_user_id": actor_id, "account_id": account_id, "limit": limit},
    )


def _scalar_uuid(
    connection: Connection,
    statement: sa.TextClause,
    parameters: Mapping[str, object],
) -> UUID:
    value = connection.execute(statement, dict(parameters)).scalar_one()
    if not isinstance(value, UUID):
        raise GmailReadonlyUnavailable("gmail read-only database result invalid")
    return value


def _one_row(
    connection: Connection,
    statement: sa.TextClause,
    parameters: Mapping[str, object],
) -> Mapping[str, object]:
    row = connection.execute(statement, dict(parameters)).mappings().one()
    return cast("Mapping[str, object]", row)


def _rows(
    connection: Connection,
    statement: sa.TextClause,
    parameters: Mapping[str, object],
) -> Sequence[Mapping[str, object]]:
    rows = connection.execute(statement, dict(parameters)).mappings().all()
    return cast("Sequence[Mapping[str, object]]", rows)


def _account_from_row(row: Mapping[str, object]) -> GmailAccountSummary:
    return GmailAccountSummary.model_validate(
        {
            "account_id": row["account_id"],
            "owner_user_id": row["owner_user_id"],
            "candidate_id": row["candidate_id"],
            "provider": row["provider"],
            "account_subject": row["account_subject"],
            "sync_mode": row["sync_mode"],
            "publishing_status": row["publishing_status"],
            "status": row["status"],
            "snapshot_sha256": row["snapshot_sha256"],
            "last_history_id": row.get("last_history_id"),
            "next_page_token_present": row.get("next_page_token") is not None,
            "last_synced_at": row.get("last_synced_at"),
            "last_full_sync_at": row.get("last_full_sync_at"),
            "last_error_code": row.get("last_error_code"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


def _sync_run_from_row(row: Mapping[str, object]) -> GmailSyncRunSummary:
    return GmailSyncRunSummary.model_validate(
        {
            "run_id": row["id"],
            "account_id": row["gmail_account_id"],
            "owner_user_id": row["owner_user_id"],
            "reason": row["reason"],
            "status": row["status"],
            "requested_at": row["created_at"],
            "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at"),
            "failed_at": row.get("failed_at"),
            "history_start_id": row.get("history_start_id"),
            "history_end_id": row.get("history_end_id"),
            "next_page_token_present": row.get("next_page_token") is not None,
            "message_count": row["message_count"],
            "last_error_code": row.get("last_error_code"),
        }
    )


def _proposal_from_row(row: Mapping[str, object]) -> GmailProposalSummary:
    raw_decision = row.get("decision")
    if raw_decision is None:
        proposal_status = "pending_review"
    elif raw_decision == "approve":
        proposal_status = "approved"
    elif raw_decision == "reject":
        proposal_status = "rejected"
    else:
        raise GmailReadonlyUnavailable("gmail read-only database result invalid")
    confidence = row["confidence"]
    if isinstance(confidence, Decimal):
        confidence = float(confidence)
    payload = row["payload_json"]
    if not isinstance(payload, Mapping):
        raise GmailReadonlyUnavailable("gmail read-only database result invalid")
    return GmailProposalSummary.model_validate(
        {
            "proposal_id": row["proposal_id"],
            "account_id": row["gmail_account_id"],
            "owner_user_id": row["owner_user_id"],
            "signal_id": row["gmail_message_signal_id"],
            "proposal_status": proposal_status,
            "proposal_kind": row["proposal_kind"],
            "review_priority": row["review_priority"],
            "classification": row["classification"],
            "relevance": row["relevance"],
            "confidence": confidence,
            "signal_sha256": row["signal_sha256"],
            "payload_sha256": row["payload_sha256"],
            "redacted_excerpt": row["redacted_excerpt"],
            "payload_snapshot": dict(cast("Mapping[str, object]", payload)),
            "canonical_job_id": row.get("canonical_job_id"),
            "job_posting_id": row.get("job_posting_id"),
            "created_at": row["created_at"],
        }
    )


def _find_sync_run(
    rows: Sequence[Mapping[str, object]],
    run_id: UUID,
) -> Mapping[str, object]:
    for row in rows:
        if row.get("id") == run_id:
            return row
    raise GmailReadonlyUnavailable("gmail read-only database result invalid")


def _find_proposal(
    rows: Sequence[Mapping[str, object]],
    proposal_id: UUID,
) -> Mapping[str, object]:
    for row in rows:
        if row.get("proposal_id") == proposal_id:
            return row
    raise GmailReadonlyNotFound()


def _raise_database_error(exc: DBAPIError) -> NoReturn:
    sqlstate = _sqlstate(exc)
    if sqlstate == "23503":
        raise GmailReadonlyNotFound() from None
    if sqlstate in {"23505", "23514"}:
        raise GmailReadonlyConflict() from None
    raise GmailReadonlyUnavailable("gmail read-only database unavailable") from None


def _sqlstate(exc: DBAPIError) -> str | None:
    value = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if isinstance(value, str) and len(value) == 5:
        return value
    return None


__all__ = ["RuntimeGmailReadonlyOperatorProvider", "TransactionFactory"]
