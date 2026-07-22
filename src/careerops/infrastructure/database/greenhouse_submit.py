from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from careerops.application.greenhouse_submit import (
    GreenhouseAttachmentRef,
    GreenhouseJobSchemaSnapshot,
    GreenhouseSubmissionPayload,
    GreenhouseTarget,
)
from careerops.application.greenhouse_submit_outbox import (
    GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX,
    GreenhouseSubmitOutboxError,
    PreparedGreenhouseSubmitDispatch,
    PreparedGreenhouseSubmitState,
)
from careerops.application.outbox import ClaimedOutboxEvent
from careerops.infrastructure.greenhouse.client import (
    GreenhouseBrokerJournalState,
    GreenhouseSubmissionEvidence,
    GreenhouseSubmissionOutcome,
)
from careerops.infrastructure.greenhouse.credentials import (
    GREENHOUSE_SUBMIT_OPERATION,
    GreenhouseCredentialHandle,
)

_OWNER = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")


def greenhouse_submit_register_account_statement(
    *,
    owner_user_id: UUID,
    candidate_id: UUID,
    account_subject: str,
    opaque_broker_handle: str,
    authorized_integration_source: str,
    employer_id: str,
    board_token: str,
    operator_user_id: UUID,
    credential_profile_id: str,
    credential_profile_version: int,
    credential_fingerprint_sha256: str,
    credential_profile_status: str,
    credential_profile_expires_at: datetime,
    credential_profile_revoked_at: datetime | None,
    employer_authorization_evidence_sha256: str,
    credential_store_evidence_sha256: str,
    release_evidence_sha256: str,
    status: str,
    daily_submit_limit: int,
    idempotency_key: str,
    trace_id: str,
) -> sa.TextClause:
    return sa.text(
        """
        SELECT *
        FROM careerops.greenhouse_submit_register_account(
            :owner_user_id, :candidate_id, :account_subject, :opaque_broker_handle,
            :authorized_integration_source, :employer_id, :board_token,
            :operator_user_id, :credential_profile_id,
            :credential_profile_version, :credential_fingerprint_sha256,
            :credential_profile_status, :credential_profile_expires_at,
            :credential_profile_revoked_at, :employer_authorization_evidence_sha256,
            :credential_store_evidence_sha256,
            :release_evidence_sha256, :status, :daily_submit_limit,
            :idempotency_key, :trace_id
        )
        """
    ).bindparams(
        owner_user_id=owner_user_id,
        candidate_id=candidate_id,
        account_subject=account_subject,
        opaque_broker_handle=opaque_broker_handle,
        authorized_integration_source=authorized_integration_source,
        employer_id=employer_id,
        board_token=board_token,
        operator_user_id=operator_user_id,
        credential_profile_id=credential_profile_id,
        credential_profile_version=credential_profile_version,
        credential_fingerprint_sha256=credential_fingerprint_sha256,
        credential_profile_status=credential_profile_status,
        credential_profile_expires_at=credential_profile_expires_at,
        credential_profile_revoked_at=credential_profile_revoked_at,
        employer_authorization_evidence_sha256=employer_authorization_evidence_sha256,
        credential_store_evidence_sha256=credential_store_evidence_sha256,
        release_evidence_sha256=release_evidence_sha256,
        status=status,
        daily_submit_limit=daily_submit_limit,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )


def greenhouse_submit_create_draft_statement(
    *,
    owner_user_id: UUID,
    candidate_id: UUID,
    resource_id: UUID,
    target_json: str,
    payload_json: str,
    attachment_refs_json: str,
    payload_hash: str | None,
    ruleset_version: str,
    idempotency_key: str,
    trace_id: str,
    decision_rule_reference: str,
    expires_at: datetime,
) -> sa.TextClause:
    return sa.text(
        """
        SELECT *
        FROM careerops.greenhouse_submit_create_draft(
            :owner_user_id, :candidate_id, :resource_id,
            CAST(:target_json AS jsonb), CAST(:payload_json AS jsonb),
            CAST(:attachment_refs_json AS jsonb), :payload_hash,
            :ruleset_version, :idempotency_key, :trace_id,
            :decision_rule_reference, :expires_at
        )
        """
    ).bindparams(
        owner_user_id=owner_user_id,
        candidate_id=candidate_id,
        resource_id=resource_id,
        target_json=target_json,
        payload_json=payload_json,
        attachment_refs_json=attachment_refs_json,
        payload_hash=payload_hash,
        ruleset_version=ruleset_version,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        decision_rule_reference=decision_rule_reference,
        expires_at=expires_at,
    )


def greenhouse_submit_review_draft_statement(
    *,
    owner_user_id: UUID,
    action_intent_id: UUID,
    payload_version_id: UUID,
    approval_request_id: UUID,
    campaign_id: UUID,
    grant_version_id: UUID,
    payload_hash: str,
    material_hash: str,
    submission_identity_sha256: str,
    decision: str,
    reviewed_by_user_id: UUID,
    authorization_id: UUID,
    review_snapshot_sha256: str,
    idempotency_key: str,
    authorization_expires_at: datetime,
    trace_id: str,
    reason: str,
) -> sa.TextClause:
    return sa.text(
        """
        SELECT *
        FROM careerops.greenhouse_submit_review_draft(
            :owner_user_id, :action_intent_id, :payload_version_id,
            :approval_request_id, :campaign_id, :grant_version_id,
            :payload_hash, :material_hash, :submission_identity_sha256,
            :decision, :reviewed_by_user_id,
            :authorization_id, :review_snapshot_sha256,
            :idempotency_key, :authorization_expires_at, :trace_id, :reason
        )
        """
    ).bindparams(**locals())


def greenhouse_submit_reserve_and_enqueue_statement(
    *,
    owner_user_id: UUID,
    account_id: UUID,
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
    event_key: str,
    idempotency_key: str,
    trace_id: str,
) -> sa.TextClause:
    return sa.text(
        """
        SELECT *
        FROM careerops.greenhouse_submit_reserve_and_enqueue(
            :owner_user_id, :account_id, :campaign_id, :grant_version_id,
            :authorization_id, :action_intent_id, :payload_version_id,
            :payload_hash, :material_hash, :submission_identity_sha256,
            :board_token_sha256, :job_id_sha256,
            :schema_sha256, :approval_request_id, :review_evidence_sha256,
            :review_snapshot_sha256, :reviewed_by_user_id,
            :release_qualification_id, :reservation_key, :reconciliation_key,
            :event_key, :idempotency_key, :trace_id
        )
        """
    ).bindparams(**locals())


def list_greenhouse_submit_accounts_statement(*, owner_user_id: UUID) -> sa.TextClause:
    return sa.text(
        """
        SELECT *
        FROM careerops.list_greenhouse_submit_accounts(:owner_user_id)
        """
    ).bindparams(owner_user_id=owner_user_id)


def get_greenhouse_submit_account_statement(
    *,
    owner_user_id: UUID,
    account_id: UUID,
) -> sa.TextClause:
    return sa.text(
        """
        SELECT *
        FROM careerops.get_greenhouse_submit_account(:owner_user_id, :account_id)
        """
    ).bindparams(owner_user_id=owner_user_id, account_id=account_id)


def list_greenhouse_submit_reconciliation_cases_statement(
    *,
    owner_user_id: UUID,
) -> sa.TextClause:
    return sa.text(
        """
        SELECT *
        FROM careerops.list_greenhouse_submit_reconciliation_cases(:owner_user_id)
        """
    ).bindparams(owner_user_id=owner_user_id)


def get_greenhouse_submit_reconciliation_case_statement(
    *,
    owner_user_id: UUID,
    event_id: UUID,
) -> sa.TextClause:
    return sa.text(
        """
        SELECT *
        FROM careerops.get_greenhouse_submit_reconciliation_case(:owner_user_id, :event_id)
        """
    ).bindparams(owner_user_id=owner_user_id, event_id=event_id)


def prepare_greenhouse_submit_outbox_event_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
) -> sa.TextClause:
    _validate_owner(owner)
    return sa.text(
        """
        SELECT *
        FROM careerops.greenhouse_submit_prepare_outbox_event(
            :event_id, :owner, :lease_token
        )
        """
    ).bindparams(event_id=event_id, owner=owner, lease_token=lease_token)


def claim_greenhouse_submit_outbox_events_statement(
    *,
    owner: str,
    lease_seconds: int,
    limit: int,
) -> sa.TextClause:
    _validate_owner(owner)
    if not 1 <= lease_seconds <= 600:
        raise ValueError("lease_seconds must be between 1 and 600")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    return sa.text(
        """
        SELECT *
        FROM careerops.claim_greenhouse_submit_outbox_events(
            :owner, :lease_seconds, :limit
        )
        """
    ).bindparams(owner=owner, lease_seconds=lease_seconds, limit=limit)


def mark_greenhouse_submit_outbox_published_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
) -> sa.TextClause:
    _validate_owner(owner)
    return sa.text(
        """
        SELECT careerops.mark_greenhouse_submit_outbox_published(
            :event_id, :owner, :lease_token
        )
        """
    ).bindparams(event_id=event_id, owner=owner, lease_token=lease_token)


def release_greenhouse_submit_outbox_event_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    retry_at: datetime,
    error_code: str,
    terminal: bool,
) -> sa.TextClause:
    _validate_owner(owner)
    if not _ERROR_CODE.fullmatch(error_code):
        raise ValueError("error_code must be an uppercase symbolic code")
    return sa.text(
        """
        SELECT careerops.release_greenhouse_submit_outbox_event(
            :event_id, :owner, :lease_token, :retry_at, :error_code, :terminal
        )
        """
    ).bindparams(
        event_id=event_id,
        owner=owner,
        lease_token=lease_token,
        retry_at=retry_at,
        error_code=error_code,
        terminal=terminal,
    )


def record_greenhouse_submit_accepted_unverified_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    reconciliation_key: str,
    provider_timestamp: datetime,
    receipt_id: UUID,
    http_status: int,
    broker_request_sha256: str,
    broker_response_sha256: str,
    provider_request_sha256: str,
    provider_response_sha256: str,
    observed_raw_response_sha256: str,
    observed_normalized_schema_sha256: str,
    journal_receipt_sha256: str,
    journal_state: str,
    journal_sequence: int,
    evidence_sha256: str,
) -> sa.TextClause:
    _validate_owner(owner)
    return sa.text(
        """
        SELECT careerops.greenhouse_submit_record_accepted_unverified(
            :event_id, :owner, :lease_token, :reconciliation_key,
            :provider_timestamp, :receipt_id, :http_status,
            :broker_request_sha256, :broker_response_sha256,
            :provider_request_sha256, :provider_response_sha256,
            :observed_raw_response_sha256, :observed_normalized_schema_sha256,
            :journal_receipt_sha256, :journal_state, :journal_sequence,
            :evidence_sha256
        )
        """
    ).bindparams(**locals())


def submit_greenhouse_submit_reconciliation_evidence_statement(
    *,
    owner_user_id: UUID,
    event_id: UUID,
    evidence_source: str,
    evidence_sha256: str,
    observed_status: str,
    observed_at: datetime,
    reason_code: str,
    idempotency_key: str,
    trace_id: str,
) -> sa.TextClause:
    if evidence_source not in {"employer_admin", "recruiting_webhook", "manual_employer_system"}:
        raise ValueError("evidence_source must be employer-side evidence")
    if not _ERROR_CODE.fullmatch(reason_code):
        raise ValueError("reason_code must be an uppercase symbolic code")
    return sa.text(
        """
        SELECT *
        FROM careerops.submit_greenhouse_submit_reconciliation_evidence(
            :owner_user_id, :event_id, :evidence_source, :evidence_sha256,
            :observed_status, :observed_at, :reason_code, :idempotency_key,
            :trace_id
        )
        """
    ).bindparams(**locals())


def review_greenhouse_submit_reconciliation_evidence_statement(
    *,
    owner_user_id: UUID,
    event_id: UUID,
    evidence_review_id: UUID,
    evidence_sha256: str,
    reviewed_evidence_source: str,
    reviewed_observed_status: str,
    decision: str,
    reviewed_employer_authorization_evidence_sha256: str,
    reviewed_by_user_id: UUID,
    review_snapshot_sha256: str,
    reason: str,
    idempotency_key: str,
    trace_id: str,
) -> sa.TextClause:
    if decision not in {"confirmed", "resolved_absent", "blocked"}:
        raise ValueError("decision must be a reconciliation terminal state")
    if reviewed_evidence_source not in {
        "employer_admin",
        "recruiting_webhook",
        "manual_employer_system",
    }:
        raise ValueError("reviewed_evidence_source must be employer-side evidence")
    if reviewed_observed_status not in {
        "accepted_unverified",
        "ambiguous",
        "provider_rejected",
        "reconciliation_required",
    }:
        raise ValueError("reviewed_observed_status must be a reviewable provider state")
    return sa.text(
        """
        SELECT *
        FROM careerops.review_greenhouse_submit_reconciliation_evidence(
            :owner_user_id, :event_id, :evidence_review_id, :evidence_sha256,
            :reviewed_evidence_source, :reviewed_observed_status, :decision,
            :reviewed_employer_authorization_evidence_sha256,
            :reviewed_by_user_id, :review_snapshot_sha256, :reason,
            :idempotency_key, :trace_id
        )
        """
    ).bindparams(**locals())


def record_greenhouse_submit_ambiguity_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    error_code: str,
    evidence: GreenhouseSubmissionEvidence | None = None,
) -> sa.TextClause:
    _validate_owner(owner)
    if not _ERROR_CODE.fullmatch(error_code):
        raise ValueError("error_code must be an uppercase symbolic code")
    if evidence is not None and evidence.outcome is not GreenhouseSubmissionOutcome.AMBIGUOUS:
        raise ValueError("ambiguity record requires ambiguous broker evidence")
    return sa.text(
        """
        SELECT careerops.greenhouse_submit_record_ambiguity(
            :event_id, :owner, :lease_token, :error_code, :reconciliation_key,
            :broker_request_sha256, :broker_response_sha256,
            :provider_request_sha256, :provider_response_sha256,
            :observed_raw_response_sha256, :observed_normalized_schema_sha256,
            :journal_receipt_sha256, :journal_state, :journal_sequence,
            :evidence_sha256, :submission_identity, :payload_hash, :material_hash
        )
        """
    ).bindparams(
        event_id=event_id,
        owner=owner,
        lease_token=lease_token,
        error_code=error_code,
        reconciliation_key=evidence.reconciliation_key if evidence else None,
        **_evidence_bindings(evidence),
    )


def record_greenhouse_submit_rejected_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    error_code: str,
    reason_codes: Sequence[str],
    evidence: GreenhouseSubmissionEvidence | None = None,
) -> sa.TextClause:
    _validate_owner(owner)
    if not _ERROR_CODE.fullmatch(error_code):
        raise ValueError("error_code must be an uppercase symbolic code")
    if not reason_codes or any(_ERROR_CODE.fullmatch(code) is None for code in reason_codes):
        raise ValueError("reason_codes must contain bounded uppercase symbolic codes")
    if evidence is not None and evidence.outcome is not GreenhouseSubmissionOutcome.REJECTED:
        raise ValueError("rejected record requires rejected broker evidence")
    return sa.text(
        """
        SELECT careerops.greenhouse_submit_record_rejected(
            :event_id, :owner, :lease_token, :error_code,
            CAST(:reason_codes AS text[]), :reconciliation_key, :http_status,
            :broker_request_sha256, :broker_response_sha256,
            :provider_request_sha256, :provider_response_sha256,
            :observed_raw_response_sha256, :observed_normalized_schema_sha256,
            :journal_receipt_sha256, :journal_state, :journal_sequence,
            :evidence_sha256, :submission_identity, :payload_hash, :material_hash
        )
        """
    ).bindparams(
        event_id=event_id,
        owner=owner,
        lease_token=lease_token,
        error_code=error_code,
        reason_codes=list(reason_codes),
        reconciliation_key=evidence.reconciliation_key if evidence else None,
        http_status=evidence.status_code if evidence else None,
        **_evidence_bindings(evidence),
    )


def record_greenhouse_submit_prepost_failure_statement(
    *,
    event_id: UUID,
    owner: str,
    lease_token: UUID,
    error_code: str,
) -> sa.TextClause:
    _validate_owner(owner)
    if not _ERROR_CODE.fullmatch(error_code):
        raise ValueError("error_code must be an uppercase symbolic code")
    return sa.text(
        """
        SELECT careerops.greenhouse_submit_record_prepost_failure(
            :event_id, :owner, :lease_token, :error_code
        )
        """
    ).bindparams(
        event_id=event_id,
        owner=owner,
        lease_token=lease_token,
        error_code=error_code,
    )


def prepared_greenhouse_submit_from_row(row: RowMapping) -> PreparedGreenhouseSubmitDispatch:
    reason_code = row["reason_code"]
    if reason_code is not None:
        raise GreenhouseSubmitOutboxError(
            cast("str", reason_code),
            f"greenhouse submit prepare stopped: {reason_code}",
        )
    schema_payload = _json_mapping(row["schema_json"])
    payload_json = _json_mapping(row["payload_json"])
    target_json = _json_mapping(payload_json["target"])
    target = GreenhouseTarget(
        board_token=cast("str", target_json["board_token"]),
        job_id=cast("int", target_json["job_id"]),
    )
    schema = GreenhouseJobSchemaSnapshot.from_mapping(
        target=target,
        payload=schema_payload,
        raw_response_sha256=cast("str", row["raw_response_sha256"]),
    )
    stored_schema_hash = cast("str", payload_json["schema_hash"])
    stored_target_schema_hash = cast("str", target_json["schema_sha256"])
    if schema.schema_hash != stored_schema_hash or schema.schema_hash != stored_target_schema_hash:
        raise RuntimeError("greenhouse submit prepare schema hash drift")
    payload = GreenhouseSubmissionPayload(
        target=target,
        schema_hash=schema.schema_hash,
        fields=_json_mapping(payload_json["fields"]),
        source_draft_payload_hash=cast("str", payload_json["source_draft_payload_hash"]),
        approved_material_hashes=tuple(
            cast("tuple[str, ...]", tuple(payload_json["approved_material_hashes"]))
        ),
        attachment_refs=_attachment_refs(row["attachment_refs_json"]),
    )
    if (
        payload.payload_hash != cast("str", payload_json["payload_hash"])
        or payload.material_hash != cast("str", payload_json["material_hash"])
        or payload.submission_identity != cast("str", payload_json["submission_identity_sha256"])
    ):
        raise RuntimeError("greenhouse submit prepare payload hash drift")
    credential = GreenhouseCredentialHandle(
        opaque_handle=cast("str", row["opaque_broker_handle"]),
        owner_user_id=cast("UUID", row["owner_user_id"]),
        employer_key=cast("str", row["employer_id"]),
        board_token=target.board_token,
        credential_profile_version=str(row["credential_profile_version"]),
        credential_profile_hash=cast("str", row["credential_fingerprint_sha256"]),
        profile_status=cast("str", row["credential_profile_status"]),
        profile_expires_at=cast("datetime", row["credential_profile_expires_at"]),
        allowed_operations=(GREENHOUSE_SUBMIT_OPERATION,),
    )
    return PreparedGreenhouseSubmitDispatch(
        event_id=cast("UUID", row["event_id"]),
        event_key=cast("str", row["event_key"]),
        action_intent_id=cast("UUID", row["action_intent_id"]),
        payload_version_id=cast("UUID", row["payload_version_id"]),
        reservation_key=cast("str", row["reservation_key"]),
        reconciliation_key=cast("str", row["reconciliation_key"]),
        account_id=cast("UUID", row["account_id"]),
        credential_handle=credential,
        schema=schema,
        payload=payload,
        state=PreparedGreenhouseSubmitState(cast("str", row["prepare_state"])),
    )


_dispatch_from_row = prepared_greenhouse_submit_from_row


class PostgresGreenhouseSubmitCoordinator:
    def __init__(self, engine: Engine, *, owner: str) -> None:
        _validate_owner(owner)
        self._engine = engine
        self._owner = owner
        self._leases: dict[UUID, tuple[str, UUID]] = {}

    def prepare(self, event: ClaimedOutboxEvent) -> PreparedGreenhouseSubmitDispatch:
        lease_owner = event.lease_owner or self._owner
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    prepare_greenhouse_submit_outbox_event_statement(
                        event_id=event.event_id,
                        owner=lease_owner,
                        lease_token=event.lease_token,
                    )
                )
                .mappings()
                .one()
            )
        dispatch = prepared_greenhouse_submit_from_row(row)
        self._leases[dispatch.event_id] = (lease_owner, event.lease_token)
        return dispatch

    def record_accepted_unverified(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence,
    ) -> None:
        if evidence.outcome is not GreenhouseSubmissionOutcome.ACCEPTED_UNVERIFIED:
            raise ValueError("accepted record requires accepted_unverified evidence")
        with self._engine.begin() as connection:
            connection.execute(
                record_greenhouse_submit_accepted_unverified_statement(
                    event_id=dispatch.event_id,
                    owner=self._require_lease_owner(dispatch),
                    lease_token=self._require_lease_token(dispatch),
                    reconciliation_key=dispatch.reconciliation_key,
                    provider_timestamp=datetime.now(tz=UTC),
                    receipt_id=uuid_from_hash(evidence.evidence_hash),
                    http_status=cast("int", evidence.status_code),
                    broker_request_sha256=evidence.broker_request_sha256,
                    broker_response_sha256=evidence.broker_response_sha256,
                    provider_request_sha256=cast("str", evidence.provider_request_sha256),
                    provider_response_sha256=cast("str", evidence.provider_response_sha256),
                    observed_raw_response_sha256=cast(
                        "str", evidence.observed_schema_raw_response_sha256
                    ),
                    observed_normalized_schema_sha256=cast("str", evidence.observed_schema_hash),
                    journal_receipt_sha256=cast("str", evidence.journal_receipt_hash),
                    journal_state=cast(
                        "GreenhouseBrokerJournalState", evidence.journal_state
                    ).value,
                    journal_sequence=cast("int", evidence.journal_sequence),
                    evidence_sha256=evidence.evidence_hash,
                )
            )

    def record_rejected(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence | None,
        error_code: str,
        reason_codes: Sequence[str],
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                record_greenhouse_submit_rejected_statement(
                    event_id=dispatch.event_id,
                    owner=self._require_lease_owner(dispatch),
                    lease_token=self._require_lease_token(dispatch),
                    error_code=error_code,
                    reason_codes=reason_codes,
                    evidence=evidence,
                )
            )

    def record_ambiguous(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence | None,
        error_code: str,
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                record_greenhouse_submit_ambiguity_statement(
                    event_id=dispatch.event_id,
                    owner=self._require_lease_owner(dispatch),
                    lease_token=self._require_lease_token(dispatch),
                    error_code=error_code,
                    evidence=evidence,
                )
            )

    def record_prepost_failure(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        error_code: str,
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                record_greenhouse_submit_prepost_failure_statement(
                    event_id=dispatch.event_id,
                    owner=self._require_lease_owner(dispatch),
                    lease_token=self._require_lease_token(dispatch),
                    error_code=error_code,
                )
            )

    def _require_lease(self, dispatch: PreparedGreenhouseSubmitDispatch) -> tuple[str, UUID]:
        lease = self._leases.get(dispatch.event_id)
        if lease is None:
            raise RuntimeError("Greenhouse submit transition has no prepared lease token")
        return lease

    def _require_lease_owner(self, dispatch: PreparedGreenhouseSubmitDispatch) -> str:
        return self._require_lease(dispatch)[0]

    def _require_lease_token(self, dispatch: PreparedGreenhouseSubmitDispatch) -> UUID:
        return self._require_lease(dispatch)[1]


class PostgresGreenhouseSubmitOutboxStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        if event_key_prefix not in (None, GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX):
            raise ValueError("Greenhouse submit store only claims greenhouse-submit events")
        del now
        lease_seconds = max(1, min(600, int(lease_for.total_seconds())))
        with self._engine.begin() as connection:
            rows = (
                connection.execute(
                    claim_greenhouse_submit_outbox_events_statement(
                        owner=owner,
                        lease_seconds=lease_seconds,
                        limit=limit,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_claimed_from_row(row, owner=owner) for row in rows)

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None:
        del now
        with self._engine.begin() as connection:
            connection.execute(
                mark_greenhouse_submit_outbox_published_statement(
                    event_id=event_id,
                    owner=owner,
                    lease_token=lease_token,
                )
            )

    def release(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
        retry_at: datetime,
        error_code: str,
        terminal: bool,
    ) -> None:
        del now
        with self._engine.begin() as connection:
            connection.execute(
                release_greenhouse_submit_outbox_event_statement(
                    event_id=event_id,
                    owner=owner,
                    lease_token=lease_token,
                    retry_at=retry_at,
                    error_code=error_code,
                    terminal=terminal,
                )
            )


def _claimed_from_row(row: RowMapping, *, owner: str) -> ClaimedOutboxEvent:
    return ClaimedOutboxEvent(
        event_id=cast("UUID", row["event_id"]),
        event_key=cast("str", row["event_key"]),
        action_intent_id=cast("UUID", row["action_intent_id"]),
        payload_version_id=cast("UUID", row["payload_version_id"]),
        event_type=cast("str", row["event_type"]),
        available_at=cast("datetime", row["available_at"]),
        attempt_count=cast("int", row["attempt_count"]),
        lease_token=cast("UUID", row["lease_token"]),
        lease_until=cast("datetime", row["lease_until"]),
        lease_owner=owner,
    )


def _json_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, str):
        import json

        decoded = json.loads(value)
    else:
        decoded = value
    if not isinstance(decoded, Mapping):
        raise ValueError("expected JSON object")
    return cast("Mapping[str, Any]", decoded)


def _json_sequence(value: object) -> Sequence[Mapping[str, Any]]:
    if isinstance(value, str):
        import json

        decoded = json.loads(value)
    else:
        decoded = value
    if not isinstance(decoded, Sequence) or isinstance(decoded, (str, bytes, bytearray)):
        raise ValueError("expected JSON array")
    sequence = cast("Sequence[object]", decoded)
    return tuple(cast("Mapping[str, Any]", item) for item in sequence)


def _attachment_refs(value: object) -> tuple[GreenhouseAttachmentRef, ...]:
    return tuple(
        GreenhouseAttachmentRef(
            field_name=cast("str", item["field_name"]),
            object_key=cast("str", item["object_key"]),
            filename=cast("str", item["filename"]),
            content_type=cast("str", item["content_type"]),
            size_bytes=cast("int", item["size_bytes"]),
            sha256=cast("str", item["sha256"]),
        )
        for item in _json_sequence(value)
    )


def _evidence_bindings(evidence: GreenhouseSubmissionEvidence | None) -> dict[str, object]:
    return {
        "broker_request_sha256": evidence.broker_request_sha256 if evidence else None,
        "broker_response_sha256": evidence.broker_response_sha256 if evidence else None,
        "provider_request_sha256": evidence.provider_request_sha256 if evidence else None,
        "provider_response_sha256": evidence.provider_response_sha256 if evidence else None,
        "observed_raw_response_sha256": (
            evidence.observed_schema_raw_response_sha256 if evidence else None
        ),
        "observed_normalized_schema_sha256": evidence.observed_schema_hash if evidence else None,
        "journal_receipt_sha256": evidence.journal_receipt_hash if evidence else None,
        "journal_state": evidence.journal_state.value
        if evidence and evidence.journal_state
        else None,
        "journal_sequence": evidence.journal_sequence if evidence else None,
        "evidence_sha256": evidence.evidence_hash if evidence else None,
        "submission_identity": evidence.submission_identity if evidence else None,
        "payload_hash": evidence.payload_hash if evidence else None,
        "material_hash": evidence.material_hash if evidence else None,
    }


def uuid_from_hash(value: str) -> UUID:
    return UUID(value[:32])


def _validate_owner(owner: str) -> None:
    if not _OWNER.fullmatch(owner):
        raise ValueError("owner must be a bounded machine identifier")
