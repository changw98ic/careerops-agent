from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Literal, NoReturn, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from careerops.api.gmail_send import (
    GMAIL_SEND_EVENT_KEY_PREFIX,
    CreateGmailSendDraftResponse,
    GmailSendAccountStatus,
    GmailSendAccountStatusResponse,
    GmailSendAccountSummary,
    GmailSendConflict,
    GmailSendNotFound,
    GmailSendUnavailable,
    ListGmailSendAccountsResponse,
    RegisterGmailSendAccountResponse,
    ReserveGmailSendIntentResponse,
    ReviewGmailSendDraftResponse,
)

TransactionFactory = Callable[[], AbstractContextManager[Connection]]

_REGISTER_ACCOUNT = sa.text(
    """
    SELECT careerops.gmail_send_register_account(
        :owner_user_id,
        :candidate_id,
        :secret_handle,
        :account_subject,
        :publishing_status,
        :credential_store_evidence_sha256,
        :release_evidence_sha256,
        :daily_send_limit,
        :reconciliation_gmail_account_id,
        :idempotency_key,
        :trace_id
    ) AS account_id
    """
)
_LIST_ACCOUNTS = sa.text("SELECT * FROM careerops.gmail_send_list_accounts(:owner_user_id, :limit)")
_ACCOUNT_STATUS = sa.text("SELECT * FROM careerops.gmail_send_status(:owner_user_id, :account_id)")
_CREATE_DRAFT = sa.text(
    """
    SELECT *
    FROM careerops.gmail_send_create_draft(
        :owner_user_id, :candidate_id, :resource_id,
        CAST(:target AS jsonb), CAST(:payload AS jsonb), CAST(:attachment_refs AS jsonb),
        :payload_hash, :ruleset_version, :idempotency_key, :trace_id,
        :decision_rule_reference, :expires_at
    )
    """
)
_REVIEW_DRAFT = sa.text(
    """
    SELECT *
    FROM careerops.gmail_send_review_draft(
        :owner_user_id, :action_intent_id, :payload_version_id, :approval_request_id,
        :campaign_id, :grant_version_id, :payload_hash, :decision,
        :reviewed_by_user_id, :authorization_id, :review_snapshot_sha256,
        :idempotency_key, :authorization_expires_at, :trace_id, :reason
    )
    """
)
_RESERVE_AND_ENQUEUE = sa.text(
    """
    SELECT *
    FROM careerops.gmail_send_reserve_and_enqueue(
        :owner_user_id, :account_id, :campaign_id, :grant_version_id, :authorization_id,
        :action_intent_id, :payload_version_id, :payload_hash, :recipient_sha256,
        :approval_request_id, :review_evidence_sha256, :review_snapshot_sha256,
        :reviewed_by_user_id, :release_qualification_id, :reservation_key,
        :reconciliation_key, :event_key, :idempotency_key, :trace_id,
        :subject_sha256, :body_sha256
    )
    """
)


class RuntimeGmailSendOperatorProvider:
    """Authenticated Gmail send adapter using the 0013 owner-scoped functions."""

    def __init__(self, *, transaction_factory: TransactionFactory) -> None:
        self._transaction_factory = transaction_factory

    async def register_account(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        reconciliation_gmail_account_id: UUID,
        credential_handle: str,
        account_subject: str,
        credential_store_evidence_sha256: str,
        release_evidence_sha256: str,
        requested_status: GmailSendAccountStatus,
        daily_send_limit: int,
        dedicated: Literal[True],
        oauth_client_mode: Literal["byo"],
        now: datetime,
    ) -> RegisterGmailSendAccountResponse:
        del now
        if dedicated is not True or oauth_client_mode != "byo":
            raise GmailSendConflict()

        def execute(connection: Connection) -> RegisterGmailSendAccountResponse:
            row = _one_row(
                connection,
                _REGISTER_ACCOUNT,
                {
                    "owner_user_id": actor_id,
                    "candidate_id": candidate_id,
                    "reconciliation_gmail_account_id": reconciliation_gmail_account_id,
                    "account_subject": account_subject,
                    "secret_handle": credential_handle,
                    "credential_store_evidence_sha256": credential_store_evidence_sha256,
                    "release_evidence_sha256": release_evidence_sha256,
                    "daily_send_limit": daily_send_limit,
                    "publishing_status": "testing" if requested_status == "active" else "disabled",
                    **_command_parameters(command_id),
                },
            )
            account_id = _row_uuid(row, "account_id")
            account = _account_from_row(
                _account_status_row(connection, actor_id, account_id),
                fallback_account_id=account_id,
            )
            account = account.model_copy(
                update={"reconciliation_gmail_account_id": reconciliation_gmail_account_id}
            )
            return RegisterGmailSendAccountResponse(
                account=account,
                receipt_state="created",
            )

        return await self._run(execute)

    async def list_accounts(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListGmailSendAccountsResponse:
        def execute(connection: Connection) -> ListGmailSendAccountsResponse:
            rows = _rows(connection, _LIST_ACCOUNTS, {"owner_user_id": actor_id, "limit": limit})
            return ListGmailSendAccountsResponse(
                accounts=tuple(_account_from_row(row) for row in rows)
            )

        return await self._run(execute)

    async def account_status(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
    ) -> GmailSendAccountStatusResponse:
        def execute(connection: Connection) -> GmailSendAccountStatusResponse:
            return GmailSendAccountStatusResponse(
                account=_account_from_row(
                    _account_status_row(connection, actor_id, account_id),
                    fallback_account_id=account_id,
                )
            )

        return await self._run(execute)

    async def create_draft(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        resource_id: UUID,
        target: Mapping[str, object],
        payload: Mapping[str, object],
        attachment_refs: tuple[Mapping[str, object], ...],
        payload_hash: str | None,
        ruleset_version: str,
        decision_rule_reference: str,
        expires_at: datetime,
        requested_for: Literal["gmail_send_exact_payload"],
        now: datetime,
    ) -> CreateGmailSendDraftResponse:
        del now
        if requested_for != "gmail_send_exact_payload":
            raise GmailSendConflict()

        def execute(connection: Connection) -> CreateGmailSendDraftResponse:
            row = _one_row(
                connection,
                _CREATE_DRAFT,
                {
                    "owner_user_id": actor_id,
                    "candidate_id": candidate_id,
                    "resource_id": resource_id,
                    "target": _json_text(target),
                    "payload": _json_text(payload),
                    "attachment_refs": _json_text(list(attachment_refs)),
                    "payload_hash": payload_hash,
                    "ruleset_version": ruleset_version,
                    "decision_rule_reference": decision_rule_reference,
                    "expires_at": expires_at,
                    **_command_parameters(command_id),
                },
            )
            return CreateGmailSendDraftResponse.model_validate(dict(row))

        return await self._run(execute)

    async def review_draft(
        self,
        *,
        actor_id: UUID,
        approval_request_id: UUID,
        command_id: UUID,
        action_intent_id: UUID,
        payload_version_id: UUID,
        campaign_id: UUID,
        grant_version_id: UUID,
        payload_hash: str,
        decision: Literal["approved", "rejected"],
        authorization_id: UUID,
        review_snapshot_sha256: str,
        authorization_expires_at: datetime,
        reason: str,
        requested_for: Literal["gmail_send_exact_payload"],
        now: datetime,
    ) -> ReviewGmailSendDraftResponse:
        del now
        if requested_for != "gmail_send_exact_payload":
            raise GmailSendConflict()

        def execute(connection: Connection) -> ReviewGmailSendDraftResponse:
            row = _one_row(
                connection,
                _REVIEW_DRAFT,
                {
                    "owner_user_id": actor_id,
                    "action_intent_id": action_intent_id,
                    "payload_version_id": payload_version_id,
                    "approval_request_id": approval_request_id,
                    "campaign_id": campaign_id,
                    "grant_version_id": grant_version_id,
                    "payload_hash": payload_hash,
                    "decision": decision,
                    "reviewed_by_user_id": actor_id,
                    "authorization_id": authorization_id,
                    "review_snapshot_sha256": review_snapshot_sha256,
                    "authorization_expires_at": authorization_expires_at,
                    "reason": reason,
                    **_command_parameters(command_id),
                },
            )
            return ReviewGmailSendDraftResponse.model_validate(
                dict(row) | {"requested_for": "gmail_send_exact_payload"}
            )

        return await self._run(execute)

    async def reserve_intent(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        command_id: UUID,
        campaign_id: UUID,
        grant_version_id: UUID,
        authorization_id: UUID,
        action_intent_id: UUID,
        payload_version_id: UUID,
        payload_hash: str,
        recipient_sha256: str,
        approval_request_id: UUID,
        review_evidence_sha256: str,
        review_snapshot_sha256: str,
        reviewed_by_user_id: UUID,
        release_qualification_id: UUID,
        reservation_key: str,
        reconciliation_key: str,
        subject_sha256: str,
        body_sha256: str,
        now: datetime,
    ) -> ReserveGmailSendIntentResponse:
        del now
        event_key = _event_key(reservation_key)

        def execute(connection: Connection) -> ReserveGmailSendIntentResponse:
            row = _one_row(
                connection,
                _RESERVE_AND_ENQUEUE,
                {
                    "owner_user_id": actor_id,
                    "account_id": account_id,
                    "campaign_id": campaign_id,
                    "grant_version_id": grant_version_id,
                    "authorization_id": authorization_id,
                    "action_intent_id": action_intent_id,
                    "payload_version_id": payload_version_id,
                    "payload_hash": payload_hash,
                    "recipient_sha256": recipient_sha256,
                    "approval_request_id": approval_request_id,
                    "review_evidence_sha256": review_evidence_sha256,
                    "review_snapshot_sha256": review_snapshot_sha256,
                    "reviewed_by_user_id": reviewed_by_user_id,
                    "release_qualification_id": release_qualification_id,
                    "reservation_key": reservation_key,
                    "reconciliation_key": reconciliation_key,
                    "event_key": event_key,
                    "subject_sha256": subject_sha256,
                    "body_sha256": body_sha256,
                    **_command_parameters(command_id),
                },
            )
            return ReserveGmailSendIntentResponse.model_validate(
                dict(row) | {"event_key": event_key}
            )

        return await self._run(execute)

    async def _run[T](self, operation: Callable[[Connection], T]) -> T:
        return await asyncio.to_thread(self._run_sync, operation)

    def _run_sync[T](self, operation: Callable[[Connection], T]) -> T:
        try:
            with self._transaction_factory() as connection:
                return operation(connection)
        except DBAPIError as exc:
            _raise_database_error(exc)


def _command_parameters(command_id: UUID) -> dict[str, object]:
    return {
        "idempotency_key": str(command_id),
        "trace_id": f"gmail-send-command:{command_id}",
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


def _account_from_row(
    row: Mapping[str, object],
    *,
    fallback_account_id: UUID | None = None,
) -> GmailSendAccountSummary:
    raw_account_id = row.get("account_id")
    account_id = raw_account_id if isinstance(raw_account_id, UUID) else fallback_account_id
    if account_id is None:
        raise GmailSendUnavailable("gmail send database result invalid")
    return GmailSendAccountSummary.model_validate(
        {
            "account_id": account_id,
            "reconciliation_gmail_account_id": row.get("reconciliation_gmail_account_id"),
            "account_subject": row["account_subject"],
            "status": row["status"],
            "daily_send_limit": row["daily_send_limit"],
            "credential_status": row.get("credential_status"),
            "updated_at": row.get("updated_at"),
        }
    )


def _row_uuid(row: Mapping[str, object], field_name: str) -> UUID:
    value = row.get(field_name)
    if not isinstance(value, UUID):
        raise GmailSendUnavailable("gmail send database result invalid")
    return value


def _json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _event_key(reservation_key: str) -> str:
    if reservation_key.startswith(GMAIL_SEND_EVENT_KEY_PREFIX):
        raise GmailSendConflict("reservation key must not include gmail-send prefix")
    return f"{GMAIL_SEND_EVENT_KEY_PREFIX}{reservation_key}"


def _raise_database_error(exc: DBAPIError) -> NoReturn:
    code = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if code in {"P0002", "23503"}:
        raise GmailSendNotFound() from None
    if code in {"22004", "22023", "23505", "23514", "55000", "P0001"}:
        raise GmailSendConflict() from None
    if exc.connection_invalidated or (
        isinstance(code, str) and (code.startswith("08") or code in {"57P01", "57P02", "57P03"})
    ):
        raise GmailSendUnavailable("gmail send database unavailable") from None
    raise exc


__all__: Sequence[str] = ("RuntimeGmailSendOperatorProvider",)
