from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Literal, NoReturn, Protocol, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from careerops.api.greenhouse_submit import (
    GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX,
    CreateGreenhouseSubmitDraftResponse,
    GreenhouseSubmitAccountStatusResponse,
    GreenhouseSubmitAccountSummary,
    GreenhouseSubmitConflict,
    GreenhouseSubmitNotFound,
    GreenhouseSubmitReconciliationCaseStatusResponse,
    GreenhouseSubmitReconciliationCaseSummary,
    GreenhouseSubmitUnavailable,
    ListGreenhouseSubmitAccountsResponse,
    ListGreenhouseSubmitReconciliationCasesResponse,
    RegisterGreenhouseSubmitAccountResponse,
    ReserveGreenhouseSubmitIntentResponse,
    ReviewGreenhouseReconciliationEvidenceResponse,
    ReviewGreenhouseSubmitDraftResponse,
    SubmitGreenhouseReconciliationEvidenceResponse,
)
from careerops.application.greenhouse_submit import (
    GREENHOUSE_ADAPTER_ID,
    GREENHOUSE_BOARD_API_HOST,
    GREENHOUSE_CHANNEL,
    GreenhouseAttachmentRef,
    GreenhouseJobSchemaSnapshot,
    GreenhouseSchemaError,
    GreenhouseSubmissionPayload,
    GreenhouseTarget,
    qualify_greenhouse_submission,
)
from careerops.infrastructure.database.greenhouse_submit import (
    get_greenhouse_submit_account_statement,
    get_greenhouse_submit_reconciliation_case_statement,
    greenhouse_submit_create_draft_statement,
    greenhouse_submit_register_account_statement,
    greenhouse_submit_reserve_and_enqueue_statement,
    greenhouse_submit_review_draft_statement,
    list_greenhouse_submit_accounts_statement,
    list_greenhouse_submit_reconciliation_cases_statement,
    review_greenhouse_submit_reconciliation_evidence_statement,
    submit_greenhouse_submit_reconciliation_evidence_statement,
)
from careerops.infrastructure.greenhouse.client import (
    GreenhouseApiError,
    GreenhouseJobBoardHttpClient,
)

TransactionFactory = Callable[[], AbstractContextManager[Connection]]

_AUTHORIZED_INTEGRATION_SOURCE = "employer_api_profile"
_FIXTURE_ID = "greenhouse-submit.v1"


class GreenhouseJobBoardHttpClientPort(Protocol):
    def fetch_job_schema(self, target: GreenhouseTarget) -> GreenhouseJobSchemaSnapshot: ...


class RuntimeGreenhouseSubmitOperatorProvider:
    """Authenticated Greenhouse submit adapter for reviewed exact payloads."""

    def __init__(
        self,
        *,
        transaction_factory: TransactionFactory,
        job_board_client: GreenhouseJobBoardHttpClientPort | None = None,
    ) -> None:
        self._transaction_factory = transaction_factory
        self._job_board_client = job_board_client or GreenhouseJobBoardHttpClient()

    async def register_account(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        employer_id: str,
        board_token: str,
        account_subject: str,
        opaque_credential_handle: str,
        credential_profile_id: str,
        credential_profile_version: int,
        credential_fingerprint_sha256: str,
        credential_profile_status: str,
        credential_profile_expires_at: datetime,
        employer_authorization_evidence_sha256: str,
        credential_store_evidence_sha256: str,
        release_evidence_sha256: str,
        requested_status: str,
        daily_submit_limit: int,
        now: datetime,
    ) -> RegisterGreenhouseSubmitAccountResponse:
        del now
        target = GreenhouseTarget(board_token=board_token, job_id=1)

        def execute(connection: Connection) -> RegisterGreenhouseSubmitAccountResponse:
            row = _one_row(
                connection,
                greenhouse_submit_register_account_statement(
                    owner_user_id=actor_id,
                    candidate_id=candidate_id,
                    account_subject=account_subject,
                    opaque_broker_handle=opaque_credential_handle,
                    authorized_integration_source=_AUTHORIZED_INTEGRATION_SOURCE,
                    employer_id=employer_id,
                    board_token=target.board_token,
                    operator_user_id=actor_id,
                    credential_profile_id=credential_profile_id,
                    credential_profile_version=credential_profile_version,
                    credential_fingerprint_sha256=credential_fingerprint_sha256,
                    credential_profile_status=credential_profile_status,
                    credential_profile_expires_at=credential_profile_expires_at,
                    credential_profile_revoked_at=None,
                    employer_authorization_evidence_sha256=(employer_authorization_evidence_sha256),
                    credential_store_evidence_sha256=credential_store_evidence_sha256,
                    release_evidence_sha256=release_evidence_sha256,
                    status=requested_status,
                    daily_submit_limit=daily_submit_limit,
                    **_command_parameters(command_id),
                ),
            )
            account_id = _row_uuid(row, "account_id")
            account = _account_from_row(_account_status_row(connection, actor_id, account_id))
            return RegisterGreenhouseSubmitAccountResponse(
                account=account,
                receipt_state=cast(
                    "Literal['created', 'replayed']",
                    row["receipt_state"],
                ),
            )

        return await self._run(execute)

    async def list_accounts(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListGreenhouseSubmitAccountsResponse:
        def execute(connection: Connection) -> ListGreenhouseSubmitAccountsResponse:
            rows = _rows(
                connection,
                list_greenhouse_submit_accounts_statement(owner_user_id=actor_id),
            )
            return ListGreenhouseSubmitAccountsResponse(
                accounts=tuple(_account_from_row(row) for row in rows[:limit])
            )

        return await self._run(execute)

    async def account_status(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
    ) -> GreenhouseSubmitAccountStatusResponse:
        def execute(connection: Connection) -> GreenhouseSubmitAccountStatusResponse:
            return GreenhouseSubmitAccountStatusResponse(
                account=_account_from_row(_account_status_row(connection, actor_id, account_id))
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
        core_fields: Mapping[str, object],
        attachment_refs: tuple[Mapping[str, object], ...],
        source_draft_payload_hash: str,
        approved_material_hashes: tuple[str, ...],
        ruleset_version: str,
        expires_at: datetime,
        requested_for: Literal["greenhouse_submit_exact_payload"],
        now: datetime,
    ) -> CreateGreenhouseSubmitDraftResponse:
        del now
        if requested_for != "greenhouse_submit_exact_payload":
            raise GreenhouseSubmitConflict()
        greenhouse_target = _target_from_request(target)
        schema = await asyncio.to_thread(self._fetch_schema, greenhouse_target)
        attachments = tuple(_attachment_from_mapping(item) for item in attachment_refs)
        payload = _submission_payload(
            target=greenhouse_target,
            schema=schema,
            core_fields=core_fields,
            attachment_refs=attachments,
            source_draft_payload_hash=source_draft_payload_hash,
            approved_material_hashes=approved_material_hashes,
        )
        _require_qualified(schema=schema, payload=payload)
        target_json = _target_json(schema=schema, payload=payload)
        payload_json = _payload_json(schema=schema, payload=payload, target_json=target_json)
        attachment_refs_json = [attachment.canonical() for attachment in payload.attachment_refs]
        review_snapshot_sha256 = _review_snapshot_sha256(
            target_json=target_json,
            payload_json=payload_json,
        )

        def execute(connection: Connection) -> CreateGreenhouseSubmitDraftResponse:
            row = _one_row(
                connection,
                greenhouse_submit_create_draft_statement(
                    owner_user_id=actor_id,
                    candidate_id=candidate_id,
                    resource_id=resource_id,
                    target_json=_json_text(target_json),
                    payload_json=_json_text(payload_json),
                    attachment_refs_json=_json_text(attachment_refs_json),
                    payload_hash=payload.payload_hash,
                    ruleset_version=ruleset_version,
                    decision_rule_reference=review_snapshot_sha256,
                    expires_at=expires_at,
                    **_command_parameters(command_id),
                ),
            )
            return CreateGreenhouseSubmitDraftResponse.model_validate(
                dict(row)
                | {
                    "payload_hash": payload.payload_hash,
                    "material_hash": payload.material_hash,
                    "submission_identity_sha256": payload.submission_identity,
                    "review_snapshot_sha256": review_snapshot_sha256,
                    "requested_for": "greenhouse_submit_exact_payload",
                }
            )

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
        material_hash: str,
        submission_identity_sha256: str,
        decision: str,
        authorization_id: UUID,
        review_snapshot_sha256: str,
        authorization_expires_at: datetime,
        reason: str,
        requested_for: Literal["greenhouse_submit_exact_payload"],
        now: datetime,
    ) -> ReviewGreenhouseSubmitDraftResponse:
        del now
        if requested_for != "greenhouse_submit_exact_payload":
            raise GreenhouseSubmitConflict()

        def execute(connection: Connection) -> ReviewGreenhouseSubmitDraftResponse:
            row = _one_row(
                connection,
                greenhouse_submit_review_draft_statement(
                    owner_user_id=actor_id,
                    action_intent_id=action_intent_id,
                    payload_version_id=payload_version_id,
                    approval_request_id=approval_request_id,
                    campaign_id=campaign_id,
                    grant_version_id=grant_version_id,
                    payload_hash=payload_hash,
                    material_hash=material_hash,
                    submission_identity_sha256=submission_identity_sha256,
                    decision=decision,
                    reviewed_by_user_id=actor_id,
                    authorization_id=authorization_id,
                    review_snapshot_sha256=review_snapshot_sha256,
                    authorization_expires_at=authorization_expires_at,
                    reason=reason,
                    **_command_parameters(command_id),
                ),
            )
            return ReviewGreenhouseSubmitDraftResponse.model_validate(
                dict(row) | {"requested_for": "greenhouse_submit_exact_payload"}
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
        material_hash: str,
        submission_identity_sha256: str,
        board_token_sha256: str,
        job_id_sha256: str,
        schema_sha256: str,
        approval_request_id: UUID,
        review_evidence_sha256: str,
        review_snapshot_sha256: str,
        reviewed_by_user_id: UUID,
        release_qualification_id: UUID,
        reservation_key: str,
        reconciliation_key: str,
        now: datetime,
    ) -> ReserveGreenhouseSubmitIntentResponse:
        del now
        event_key = _event_key(reservation_key)

        def execute(connection: Connection) -> ReserveGreenhouseSubmitIntentResponse:
            row = _one_row(
                connection,
                greenhouse_submit_reserve_and_enqueue_statement(
                    owner_user_id=actor_id,
                    account_id=account_id,
                    campaign_id=campaign_id,
                    grant_version_id=grant_version_id,
                    authorization_id=authorization_id,
                    action_intent_id=action_intent_id,
                    payload_version_id=payload_version_id,
                    payload_hash=payload_hash,
                    material_hash=material_hash,
                    submission_identity_sha256=submission_identity_sha256,
                    board_token_sha256=board_token_sha256,
                    job_id_sha256=job_id_sha256,
                    schema_sha256=schema_sha256,
                    approval_request_id=approval_request_id,
                    review_evidence_sha256=review_evidence_sha256,
                    review_snapshot_sha256=review_snapshot_sha256,
                    reviewed_by_user_id=reviewed_by_user_id,
                    release_qualification_id=release_qualification_id,
                    reservation_key=reservation_key,
                    reconciliation_key=reconciliation_key,
                    event_key=event_key,
                    **_command_parameters(command_id),
                ),
            )
            return ReserveGreenhouseSubmitIntentResponse.model_validate(
                dict(row) | {"event_key": event_key}
            )

        return await self._run(execute)

    async def list_reconciliation_cases(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListGreenhouseSubmitReconciliationCasesResponse:
        def execute(connection: Connection) -> ListGreenhouseSubmitReconciliationCasesResponse:
            rows = _rows(
                connection,
                list_greenhouse_submit_reconciliation_cases_statement(owner_user_id=actor_id),
            )
            return ListGreenhouseSubmitReconciliationCasesResponse(
                cases=tuple(_reconciliation_case_from_row(row) for row in rows[:limit])
            )

        return await self._run(execute)

    async def reconciliation_case_status(
        self,
        *,
        actor_id: UUID,
        reconciliation_case_id: UUID,
    ) -> GreenhouseSubmitReconciliationCaseStatusResponse:
        def execute(connection: Connection) -> GreenhouseSubmitReconciliationCaseStatusResponse:
            row = _one_row(
                connection,
                get_greenhouse_submit_reconciliation_case_statement(
                    owner_user_id=actor_id,
                    event_id=reconciliation_case_id,
                ),
            )
            return GreenhouseSubmitReconciliationCaseStatusResponse(
                case=_reconciliation_case_from_row(row)
            )

        return await self._run(execute)

    async def submit_reconciliation_evidence(
        self,
        *,
        actor_id: UUID,
        reconciliation_case_id: UUID,
        command_id: UUID,
        evidence_source: str,
        evidence_sha256: str,
        observed_status: str,
        observed_at: datetime,
        reason_code: str,
        now: datetime,
    ) -> SubmitGreenhouseReconciliationEvidenceResponse:
        del now

        def execute(connection: Connection) -> SubmitGreenhouseReconciliationEvidenceResponse:
            row = _one_row(
                connection,
                submit_greenhouse_submit_reconciliation_evidence_statement(
                    owner_user_id=actor_id,
                    event_id=reconciliation_case_id,
                    evidence_source=evidence_source,
                    evidence_sha256=evidence_sha256,
                    observed_status=observed_status,
                    observed_at=observed_at,
                    reason_code=reason_code,
                    **_command_parameters(command_id),
                ),
            )
            return SubmitGreenhouseReconciliationEvidenceResponse.model_validate(dict(row))

        return await self._run(execute)

    async def review_reconciliation_evidence(
        self,
        *,
        actor_id: UUID,
        reconciliation_case_id: UUID,
        command_id: UUID,
        evidence_review_id: UUID,
        evidence_sha256: str,
        reviewed_evidence_source: str,
        reviewed_observed_status: str,
        decision: str,
        reviewed_employer_authorization_evidence_sha256: str,
        review_snapshot_sha256: str,
        reason: str,
        now: datetime,
    ) -> ReviewGreenhouseReconciliationEvidenceResponse:
        del now
        if decision == "confirmed" and reviewed_evidence_source not in {
            "employer_admin",
            "recruiting_webhook",
            "manual_employer_system",
        }:
            raise GreenhouseSubmitConflict()

        def execute(connection: Connection) -> ReviewGreenhouseReconciliationEvidenceResponse:
            row = _one_row(
                connection,
                review_greenhouse_submit_reconciliation_evidence_statement(
                    owner_user_id=actor_id,
                    event_id=reconciliation_case_id,
                    evidence_review_id=evidence_review_id,
                    evidence_sha256=evidence_sha256,
                    reviewed_evidence_source=reviewed_evidence_source,
                    reviewed_observed_status=reviewed_observed_status,
                    decision=decision,
                    reviewed_employer_authorization_evidence_sha256=(
                        reviewed_employer_authorization_evidence_sha256
                    ),
                    reviewed_by_user_id=actor_id,
                    review_snapshot_sha256=review_snapshot_sha256,
                    reason=reason,
                    **_command_parameters(command_id),
                ),
            )
            return ReviewGreenhouseReconciliationEvidenceResponse.model_validate(dict(row))

        return await self._run(execute)

    def _fetch_schema(self, target: GreenhouseTarget) -> GreenhouseJobSchemaSnapshot:
        try:
            return self._job_board_client.fetch_job_schema(target)
        except (GreenhouseApiError, GreenhouseSchemaError, ValueError) as exc:
            raise GreenhouseSubmitConflict("Greenhouse schema is not eligible") from exc

    async def _run[T](self, operation: Callable[[Connection], T]) -> T:
        return await asyncio.to_thread(self._run_sync, operation)

    def _run_sync[T](self, operation: Callable[[Connection], T]) -> T:
        try:
            with self._transaction_factory() as connection:
                return operation(connection)
        except DBAPIError as exc:
            _raise_database_error(exc)


def _command_parameters(command_id: UUID) -> dict[str, str]:
    return {
        "idempotency_key": str(command_id),
        "trace_id": f"greenhouse-submit-command:{command_id}",
    }


def _one_row(connection: Connection, statement: sa.TextClause) -> Mapping[str, object]:
    row = connection.execute(statement).mappings().one()
    return cast("Mapping[str, object]", row)


def _rows(connection: Connection, statement: sa.TextClause) -> Sequence[Mapping[str, object]]:
    rows = connection.execute(statement).mappings().all()
    return cast("Sequence[Mapping[str, object]]", rows)


def _account_status_row(
    connection: Connection,
    actor_id: UUID,
    account_id: UUID,
) -> Mapping[str, object]:
    return _one_row(
        connection,
        get_greenhouse_submit_account_statement(owner_user_id=actor_id, account_id=account_id),
    )


def _account_from_row(
    row: Mapping[str, object],
) -> GreenhouseSubmitAccountSummary:
    return GreenhouseSubmitAccountSummary.model_validate(dict(row))


def _reconciliation_case_from_row(
    row: Mapping[str, object],
) -> GreenhouseSubmitReconciliationCaseSummary:
    return GreenhouseSubmitReconciliationCaseSummary.model_validate(
        {
            "reconciliation_case_id": row["reconciliation_case_id"],
            "account_id": row["account_id"],
            "action_intent_id": row["action_intent_id"],
            "payload_version_id": row["payload_version_id"],
            "reservation_key": row["reservation_key"],
            "reconciliation_key": row["reconciliation_key"],
            "status": row["status"],
            "evidence_sha256": row.get("evidence_sha256"),
            "evidence_source": _api_evidence_source(row.get("evidence_source")),
            "updated_at": row.get("updated_at"),
        }
    )


def _api_evidence_source(value: object) -> object:
    return value


def _row_uuid(row: Mapping[str, object], field_name: str) -> UUID:
    value = row.get(field_name)
    if not isinstance(value, UUID):
        raise GreenhouseSubmitUnavailable("greenhouse submit database result invalid")
    return value


def _target_from_request(target: Mapping[str, object]) -> GreenhouseTarget:
    if target.get("target_host", GREENHOUSE_BOARD_API_HOST) != GREENHOUSE_BOARD_API_HOST:
        raise GreenhouseSubmitConflict()
    if target.get("host", GREENHOUSE_BOARD_API_HOST) != GREENHOUSE_BOARD_API_HOST:
        raise GreenhouseSubmitConflict()
    return GreenhouseTarget(
        board_token=cast("str", target["board_token"]),
        job_id=cast("int", target["job_id"]),
    )


def _attachment_from_mapping(value: Mapping[str, object]) -> GreenhouseAttachmentRef:
    return GreenhouseAttachmentRef(
        field_name=cast("str", value["field_name"]),
        object_key=cast("str", value["object_key"]),
        filename=cast("str", value["filename"]),
        content_type=cast("str", value["content_type"]),
        size_bytes=cast("int", value["size_bytes"]),
        sha256=cast("str", value["sha256"]),
    )


def _submission_payload(
    *,
    target: GreenhouseTarget,
    schema: GreenhouseJobSchemaSnapshot,
    core_fields: Mapping[str, object],
    attachment_refs: tuple[GreenhouseAttachmentRef, ...],
    source_draft_payload_hash: str,
    approved_material_hashes: tuple[str, ...],
) -> GreenhouseSubmissionPayload:
    field_names = set(core_fields)
    if not field_names <= {"first_name", "last_name", "email", "phone"}:
        raise GreenhouseSubmitConflict()
    return GreenhouseSubmissionPayload(
        target=target,
        schema_hash=schema.schema_hash,
        fields=core_fields,
        source_draft_payload_hash=source_draft_payload_hash,
        approved_material_hashes=approved_material_hashes,
        attachment_refs=attachment_refs,
    )


def _require_qualified(
    *,
    schema: GreenhouseJobSchemaSnapshot,
    payload: GreenhouseSubmissionPayload,
) -> None:
    qualification = qualify_greenhouse_submission(schema=schema, payload=payload)
    if not qualification.can_submit:
        raise GreenhouseSubmitConflict("Greenhouse submission requires unsupported fields")


def _target_json(
    *,
    schema: GreenhouseJobSchemaSnapshot,
    payload: GreenhouseSubmissionPayload,
) -> Mapping[str, object]:
    board_token_sha256 = _hash_text(payload.target.board_token)
    job_id_sha256 = _hash_text(str(payload.target.job_id))
    job_identity_sha256 = _hash_json(
        {
            "board_token_sha256": board_token_sha256,
            "job_id_sha256": job_id_sha256,
            "internal_job_id": schema.internal_job_id,
            "updated_at": schema.updated_at,
            "schema_sha256": schema.schema_hash,
        }
    )
    return {
        "host": GREENHOUSE_BOARD_API_HOST,
        "board_token": payload.target.board_token,
        "job_id": payload.target.job_id,
        "target_host": GREENHOUSE_BOARD_API_HOST,
        "channel": GREENHOUSE_CHANNEL,
        "adapter_id": GREENHOUSE_ADAPTER_ID,
        "fixture_id": _FIXTURE_ID,
        "board_token_sha256": board_token_sha256,
        "job_id_sha256": job_id_sha256,
        "schema_sha256": schema.schema_hash,
        "raw_response_sha256": schema.raw_response_sha256,
        "normalized_schema_sha256": schema.schema_hash,
        "job_identity_sha256": job_identity_sha256,
        "candidate_material_sha256": payload.material_hash,
        "job_post_id": str(payload.target.job_id),
        "internal_job_id": str(schema.internal_job_id),
        "job_updated_at": schema.updated_at,
        "application_deadline": schema.application_deadline,
        "schema_snapshot": _schema_snapshot_json(schema),
    }


def _schema_snapshot_json(schema: GreenhouseJobSchemaSnapshot) -> Mapping[str, object]:
    """Persist the complete normalized public GET needed to rebuild the reviewed schema.

    The raw response itself can contain untrusted presentation text and is not persisted.  Its
    digest remains bound separately, while this lossless normalized shape lets the sender rebuild
    the exact domain snapshot before the broker performs its mandatory public re-GET.
    """

    questions: list[Mapping[str, object]] = []
    for question in schema.questions:
        fields: list[Mapping[str, object]] = []
        for field in question.fields:
            encoded_field: dict[str, object] = {
                "name": field.name,
                "type": field.kind.value,
            }
            if field.option_values:
                encoded_field["values"] = [{"value": option} for option in field.option_values]
            fields.append(encoded_field)
        questions.append(
            {
                "label": question.label,
                "required": question.required,
                "fields": fields,
            }
        )
    return {
        "id": schema.target.job_id,
        "internal_job_id": schema.internal_job_id,
        "title": schema.title,
        "company_name": schema.company_name,
        "updated_at": schema.updated_at,
        "application_deadline": schema.application_deadline,
        "absolute_url": schema.absolute_url,
        "questions": questions,
    }


def _payload_json(
    *,
    schema: GreenhouseJobSchemaSnapshot,
    payload: GreenhouseSubmissionPayload,
    target_json: Mapping[str, object],
) -> Mapping[str, object]:
    return {
        "version": "greenhouse-submit-payload.v1",
        "fields": dict(payload.fields),
        "source_draft_payload_hash": payload.source_draft_payload_hash,
        "approved_material_hashes": list(payload.approved_material_hashes),
        "answer_sha256": _hash_json(dict(payload.fields)),
        "payload_hash": payload.payload_hash,
        "material_hash": payload.material_hash,
        "submission_identity_sha256": payload.submission_identity,
        "schema_hash": schema.schema_hash,
        "target": dict(target_json),
        **{
            key: target_json[key]
            for key in (
                "board_token_sha256",
                "job_id_sha256",
                "schema_sha256",
                "raw_response_sha256",
                "normalized_schema_sha256",
                "job_identity_sha256",
                "job_post_id",
                "internal_job_id",
                "job_updated_at",
                "application_deadline",
            )
        },
    }


def _json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash_json(value: object) -> str:
    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _review_snapshot_sha256(
    *,
    target_json: Mapping[str, object],
    payload_json: Mapping[str, object],
) -> str:
    """Bind review to stable domain identities without JSON serializer drift."""

    return _hash_text(
        "\n".join(
            (
                "greenhouse-submit-review-snapshot.v1",
                str(payload_json["payload_hash"]),
                str(payload_json["material_hash"]),
                str(payload_json["submission_identity_sha256"]),
                str(target_json["schema_sha256"]),
                str(target_json["job_identity_sha256"]),
            )
        )
    )


def _event_key(reservation_key: str) -> str:
    if reservation_key.startswith(GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX):
        raise GreenhouseSubmitConflict("reservation key must not include greenhouse-submit prefix")
    return f"{GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX}{reservation_key}"


def _raise_database_error(exc: DBAPIError) -> NoReturn:
    code = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if code in {"P0002", "23503"}:
        raise GreenhouseSubmitNotFound() from None
    if code in {"22004", "22023", "23505", "23514", "55000", "P0001"}:
        raise GreenhouseSubmitConflict() from None
    if exc.connection_invalidated or (
        isinstance(code, str) and (code.startswith("08") or code in {"57P01", "57P02", "57P03"})
    ):
        raise GreenhouseSubmitUnavailable("greenhouse submit database unavailable") from None
    raise exc


__all__: Sequence[str] = ("RuntimeGreenhouseSubmitOperatorProvider", "TransactionFactory")
