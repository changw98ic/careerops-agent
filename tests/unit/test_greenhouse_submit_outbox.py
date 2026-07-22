from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from careerops.application.greenhouse_submit import (
    GreenhouseAttachmentRef,
    GreenhouseJobSchemaSnapshot,
    GreenhouseSubmissionPayload,
    GreenhouseTarget,
)
from careerops.application.greenhouse_submit_outbox import (
    GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX,
    GreenhouseSubmitOutboxSink,
    PreparedGreenhouseSubmitDispatch,
    PreparedGreenhouseSubmitState,
)
from careerops.application.outbox import ClaimedOutboxEvent, InternalDeliveryError, OutboxPublisher
from careerops.infrastructure.greenhouse.client import (
    GreenhouseApiError,
    GreenhouseBrokerJournalState,
    GreenhouseBrokerPrePostUnavailable,
    GreenhouseResolvedAttachment,
    GreenhouseSubmissionEvidence,
    GreenhouseSubmissionOutcome,
)
from careerops.infrastructure.greenhouse.credentials import (
    GREENHOUSE_SUBMIT_OPERATION,
    GreenhouseCredentialHandle,
)

NOW = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 7, 21, 9, 1, tzinfo=UTC)
OWNER_ID = UUID("00000000-0000-0000-0000-000000000111")
RESUME_BYTES = b"x" * 1024
RESUME_HASH = hashlib.sha256(RESUME_BYTES).hexdigest()
BASE_HASH = "a" * 64
SOURCE_HASH = "b" * 64


def _event() -> ClaimedOutboxEvent:
    reservation_key = f"reservation-{uuid4().hex}"
    return ClaimedOutboxEvent(
        event_id=uuid4(),
        event_key=f"{GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX}{reservation_key}",
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        event_type="workflow_signal",
        available_at=NOW,
        attempt_count=1,
        lease_token=uuid4(),
        lease_until=NOW + timedelta(seconds=30),
    )


def _schema(*, hard_stop: bool = False) -> GreenhouseJobSchemaSnapshot:
    target = GreenhouseTarget(board_token="example", job_id=123456)
    document: dict[str, object] = {
        "id": 123456,
        "internal_job_id": 987654,
        "title": "Senior Agent Engineer",
        "company_name": "Example AI",
        "updated_at": "2026-07-21T08:00:00Z",
        "application_deadline": None,
        "absolute_url": "https://job-boards.greenhouse.io/example/jobs/123456",
        "questions": [
            _question("First name", True, "first_name"),
            _question("Last name", True, "last_name"),
            _question("Email", True, "email"),
            _question("Resume", True, "resume", kind="input_file"),
        ],
    }
    if hard_stop:
        questions = document["questions"]
        assert isinstance(questions, list)
        questions.append(_question("Portfolio website", False, "website"))
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return GreenhouseJobSchemaSnapshot.from_json_bytes(target=target, raw_response=raw)


def _question(
    label: str,
    required: bool,
    name: str,
    *,
    kind: str = "input_text",
) -> dict[str, object]:
    return {"label": label, "required": required, "fields": [{"name": name, "type": kind}]}


def _payload(schema: GreenhouseJobSchemaSnapshot) -> GreenhouseSubmissionPayload:
    return GreenhouseSubmissionPayload(
        target=schema.target,
        schema_hash=schema.schema_hash,
        fields={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
        },
        source_draft_payload_hash=SOURCE_HASH,
        approved_material_hashes=(BASE_HASH,),
        attachment_refs=(_attachment_ref(),),
    )


def _attachment_ref() -> GreenhouseAttachmentRef:
    return GreenhouseAttachmentRef(
        field_name="resume",
        object_key="materials/resume/reviewed.pdf",
        filename="resume.pdf",
        content_type="application/pdf",
        size_bytes=len(RESUME_BYTES),
        sha256=RESUME_HASH,
    )


def _handle(schema: GreenhouseJobSchemaSnapshot) -> GreenhouseCredentialHandle:
    return GreenhouseCredentialHandle(
        opaque_handle="vault://greenhouse/example/profile-v1",
        owner_user_id=OWNER_ID,
        employer_key="example-ai",
        board_token=schema.target.board_token,
        credential_profile_version="profile-v1",
        credential_profile_hash=BASE_HASH,
        profile_status="active",
        profile_expires_at=NOW + timedelta(days=1),
        allowed_operations=(GREENHOUSE_SUBMIT_OPERATION,),
    )


def _prepared(
    event: ClaimedOutboxEvent,
    *,
    schema: GreenhouseJobSchemaSnapshot | None = None,
    state: PreparedGreenhouseSubmitState = PreparedGreenhouseSubmitState.READY,
    terminal_evidence: GreenhouseSubmissionEvidence | None = None,
) -> PreparedGreenhouseSubmitDispatch:
    schema = schema or _schema()
    return PreparedGreenhouseSubmitDispatch(
        event_id=event.event_id,
        event_key=event.event_key,
        action_intent_id=event.action_intent_id,
        payload_version_id=event.payload_version_id,
        reservation_key=event.event_key.removeprefix(GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX),
        reconciliation_key=f"reconcile:{uuid4().hex}",
        account_id=uuid4(),
        credential_handle=_handle(schema),
        schema=schema,
        payload=_payload(schema),
        state=state,
        terminal_evidence=terminal_evidence,
    )


def _evidence(
    dispatch: PreparedGreenhouseSubmitDispatch,
    *,
    outcome: GreenhouseSubmissionOutcome = GreenhouseSubmissionOutcome.ACCEPTED_UNVERIFIED,
    reason_code: str = "GREENHOUSE_PROVIDER_ACCEPTED_UNVERIFIED",
) -> GreenhouseSubmissionEvidence:
    status_code = 202
    if outcome is GreenhouseSubmissionOutcome.REJECTED:
        status_code = 422
    if outcome is GreenhouseSubmissionOutcome.AMBIGUOUS:
        status_code = None
    return GreenhouseSubmissionEvidence(
        outcome=outcome,
        reason_code=reason_code,
        submission_identity=dispatch.payload.submission_identity,
        reconciliation_key=dispatch.reconciliation_key,
        broker_request_sha256="c" * 64,
        broker_response_sha256="d" * 64,
        status_code=status_code,
        journal_receipt_hash="e" * 64,
        journal_state=GreenhouseBrokerJournalState.RESPONSE_OBSERVED,
        journal_sequence=1,
        expected_schema_hash=dispatch.schema.schema_hash,
        payload_hash=dispatch.payload.payload_hash,
        material_hash=dispatch.payload.material_hash,
        provider_request_sha256="f" * 64 if status_code is not None else None,
        provider_response_sha256="1" * 64 if status_code is not None else None,
        observed_schema_hash=dispatch.schema.schema_hash,
        observed_schema_raw_response_sha256=dispatch.schema.raw_response_sha256,
    )


class _Coordinator:
    def __init__(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        persistence_error: Exception | None = None,
    ) -> None:
        self.dispatch = dispatch
        self.persistence_error = persistence_error
        self.prepared_events: list[ClaimedOutboxEvent] = []
        self.accepted: list[
            tuple[PreparedGreenhouseSubmitDispatch, GreenhouseSubmissionEvidence]
        ] = []
        self.rejected: list[
            tuple[
                PreparedGreenhouseSubmitDispatch,
                GreenhouseSubmissionEvidence | None,
                str,
                tuple[str, ...],
            ]
        ] = []
        self.ambiguous: list[
            tuple[PreparedGreenhouseSubmitDispatch, GreenhouseSubmissionEvidence | None, str]
        ] = []
        self.prepost_failures: list[tuple[PreparedGreenhouseSubmitDispatch, str]] = []

    def prepare(self, event: ClaimedOutboxEvent) -> PreparedGreenhouseSubmitDispatch:
        self.prepared_events.append(event)
        return self.dispatch

    def record_accepted_unverified(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence,
    ) -> None:
        self._maybe_raise()
        self.accepted.append((dispatch, evidence))

    def record_rejected(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence | None,
        error_code: str,
        reason_codes: Sequence[str],
    ) -> None:
        self._maybe_raise()
        self.rejected.append((dispatch, evidence, error_code, tuple(reason_codes)))

    def record_ambiguous(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence | None,
        error_code: str,
    ) -> None:
        self._maybe_raise()
        self.ambiguous.append((dispatch, evidence, error_code))

    def record_prepost_failure(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        error_code: str,
    ) -> None:
        self.prepost_failures.append((dispatch, error_code))

    def _maybe_raise(self) -> None:
        if self.persistence_error is not None:
            raise self.persistence_error


class _Broker:
    def __init__(
        self,
        evidence: GreenhouseSubmissionEvidence | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.evidence = evidence
        self.error = error
        self.calls: list[
            tuple[
                GreenhouseCredentialHandle,
                GreenhouseJobSchemaSnapshot,
                GreenhouseSubmissionPayload,
                tuple[GreenhouseResolvedAttachment, ...],
                str,
            ]
        ] = []

    def submit(
        self,
        handle: GreenhouseCredentialHandle,
        *,
        schema: GreenhouseJobSchemaSnapshot,
        payload: GreenhouseSubmissionPayload,
        attachments: Sequence[GreenhouseResolvedAttachment],
        reconciliation_key: str,
    ) -> GreenhouseSubmissionEvidence:
        self.calls.append((handle, schema, payload, tuple(attachments), reconciliation_key))
        if self.error is not None:
            raise self.error
        assert self.evidence is not None
        return self.evidence


class _AttachmentResolver:
    def __init__(self, *, bad_ref: bool = False, bad_hash: bool = False) -> None:
        self.bad_ref = bad_ref
        self.bad_hash = bad_hash
        self.resolved: list[GreenhouseAttachmentRef] = []

    def resolve(self, ref: object) -> GreenhouseResolvedAttachment:
        assert isinstance(ref, GreenhouseAttachmentRef)
        self.resolved.append(ref)
        resolved_ref = ref
        data = RESUME_BYTES
        if self.bad_ref:
            resolved_ref = replace(ref, field_name="cover_letter", object_key="cover")
        if self.bad_hash:
            data = b"wrong"
        return GreenhouseResolvedAttachment(ref=resolved_ref, data=data)


class _Store:
    def __init__(self, event: ClaimedOutboxEvent) -> None:
        self.event = event
        self.published: list[tuple[UUID, UUID]] = []
        self.releases: list[tuple[UUID, str, bool, UUID]] = []

    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        del owner, now, lease_for, limit
        assert event_key_prefix == GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX
        return (self.event,)

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None:
        del owner, now
        self.published.append((event_id, lease_token))

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
        del owner, now, retry_at
        self.releases.append((event_id, error_code, terminal, lease_token))


def test_accepted_unverified_records_reconciliation_required_and_does_not_retry() -> None:
    event = _event()
    dispatch = _prepared(event)
    evidence = _evidence(dispatch)
    coordinator = _Coordinator(dispatch)
    broker = _Broker(evidence)
    store = _Store(event)

    result = _publisher(store, coordinator, broker).publish_batch(owner="greenhouse", now=NOW)

    assert result.published == 1
    assert result.deferred == 0
    assert result.failed == 0
    assert coordinator.accepted == [(dispatch, evidence)]
    assert evidence.outcome is GreenhouseSubmissionOutcome.ACCEPTED_UNVERIFIED
    assert evidence.reconciliation_required is True
    assert evidence.next_attempt_allowed is False
    assert store.published == [(event.event_id, event.lease_token)]
    assert store.releases == []


def test_ambiguous_broker_outcome_records_terminal_ambiguity_and_clears_lease() -> None:
    event = _event()
    dispatch = _prepared(event)
    evidence = _evidence(
        dispatch,
        outcome=GreenhouseSubmissionOutcome.AMBIGUOUS,
        reason_code="GREENHOUSE_BROKER_TRANSPORT_AMBIGUOUS",
    )
    coordinator = _Coordinator(dispatch)
    store = _Store(event)

    result = _publisher(store, coordinator, _Broker(evidence)).publish_batch(
        owner="greenhouse",
        now=NOW,
    )

    assert result.published == 1
    assert result.deferred == 0
    assert result.failed == 0
    assert coordinator.ambiguous == [(dispatch, evidence, evidence.reason_code)]
    assert store.releases == []
    assert store.published == [(event.event_id, event.lease_token)]


def test_rejected_broker_outcome_records_deterministic_rejection_without_retry() -> None:
    event = _event()
    dispatch = _prepared(event)
    evidence = _evidence(
        dispatch,
        outcome=GreenhouseSubmissionOutcome.REJECTED,
        reason_code="GREENHOUSE_PROVIDER_422_REJECTED",
    )
    coordinator = _Coordinator(dispatch)
    store = _Store(event)

    result = _publisher(store, coordinator, _Broker(evidence)).publish_batch(
        owner="greenhouse",
        now=NOW,
    )

    assert result.published == 1
    assert coordinator.rejected == [
        (
            dispatch,
            evidence,
            "GREENHOUSE_PROVIDER_422_REJECTED",
            ("GREENHOUSE_PROVIDER_422_REJECTED",),
        )
    ]
    assert coordinator.accepted == []
    assert coordinator.ambiguous == []
    assert store.releases == []


def test_prepost_broker_failure_is_recorded_and_retryable() -> None:
    event = _event()
    dispatch = _prepared(event)
    coordinator = _Coordinator(dispatch)
    broker = _Broker(error=GreenhouseBrokerPrePostUnavailable("GREENHOUSE_BROKER_PREPOST_DOWN"))
    store = _Store(event)

    result = _publisher(store, coordinator, broker).publish_batch(owner="greenhouse", now=NOW)

    assert result.deferred == 1
    assert coordinator.prepost_failures == [(dispatch, "GREENHOUSE_BROKER_PREPOST_DOWN")]
    assert store.releases == [
        (event.event_id, "GREENHOUSE_BROKER_PREPOST_DOWN", False, event.lease_token)
    ]
    assert store.published == []


def test_typed_greenhouse_api_error_records_terminal_prepost_rejection() -> None:
    event = _event()
    dispatch = _prepared(event)
    coordinator = _Coordinator(dispatch)
    broker = _Broker(
        error=GreenhouseApiError(
            "GREENHOUSE_CREDENTIAL_PROFILE_EXPIRED",
            status_code=403,
        )
    )
    store = _Store(event)

    result = _publisher(store, coordinator, broker).publish_batch(owner="greenhouse", now=NOW)

    assert result.published == 1
    assert result.deferred == 0
    assert result.failed == 0
    assert coordinator.rejected == [
        (
            dispatch,
            None,
            "GREENHOUSE_CREDENTIAL_PROFILE_EXPIRED",
            ("GREENHOUSE_CREDENTIAL_PROFILE_EXPIRED",),
        )
    ]
    assert coordinator.ambiguous == []
    assert store.releases == []
    assert store.published == [(event.event_id, event.lease_token)]


def test_receipt_persistence_failure_is_terminal_ambiguous_with_broker_journal_backstop() -> None:
    event = _event()
    dispatch = _prepared(event)
    evidence = _evidence(dispatch)
    coordinator = _Coordinator(dispatch, persistence_error=RuntimeError("database unavailable"))
    store = _Store(event)

    result = _publisher(store, coordinator, _Broker(evidence)).publish_batch(
        owner="greenhouse",
        now=NOW,
    )

    assert result.failed == 1
    assert coordinator.accepted == []
    assert evidence.journal_committed is True
    assert store.releases == [
        (
            event.event_id,
            "GREENHOUSE_SUBMIT_OUTCOME_PERSISTENCE_AMBIGUOUS",
            True,
            event.lease_token,
        )
    ]
    assert store.published == []


def test_exact_event_payload_credential_schema_and_attachment_binding() -> None:
    event = _event()
    dispatch = _prepared(event)
    evidence = _evidence(dispatch)
    coordinator = _Coordinator(dispatch)
    broker = _Broker(evidence)
    attachment_resolver = _AttachmentResolver()

    GreenhouseSubmitOutboxSink(
        coordinator,
        broker,
        attachment_resolver,
        clock=lambda: COMPLETED_AT,
    ).deliver(event)

    assert coordinator.prepared_events == [event]
    assert attachment_resolver.resolved == list(dispatch.payload.attachment_refs)
    assert len(broker.calls) == 1
    handle, schema, payload, attachments, reconciliation_key = broker.calls[0]
    assert handle is dispatch.credential_handle
    assert "api_key" not in handle.canonical_binding()
    assert schema is dispatch.schema
    assert payload is dispatch.payload
    assert reconciliation_key == dispatch.reconciliation_key
    assert attachments == (GreenhouseResolvedAttachment(ref=_attachment_ref(), data=RESUME_BYTES),)


def test_post_start_broker_evidence_mismatch_records_ambiguity_before_terminal_stop() -> None:
    event = _event()
    dispatch = _prepared(event)
    mismatched = replace(_evidence(dispatch), payload_hash="9" * 64)
    coordinator = _Coordinator(dispatch)

    with pytest.raises(
        InternalDeliveryError,
        match="GREENHOUSE_SUBMIT_BROKER_EVIDENCE_MISMATCH_AMBIGUOUS",
    ) as caught:
        GreenhouseSubmitOutboxSink(
            coordinator,
            _Broker(mismatched),
            _AttachmentResolver(),
            clock=lambda: COMPLETED_AT,
        ).deliver(event)

    assert caught.value.retryable is False
    assert coordinator.ambiguous == [
        (
            dispatch,
            None,
            "GREENHOUSE_SUBMIT_BROKER_EVIDENCE_MISMATCH_AMBIGUOUS",
        )
    ]


def test_resolved_attachment_rehash_mismatch_is_retryable_before_broker_call() -> None:
    event = _event()
    dispatch = _prepared(event)
    coordinator = _Coordinator(dispatch)
    broker = _Broker(_evidence(dispatch))
    store = _Store(event)

    result = _publisher(
        store,
        coordinator,
        broker,
        attachment_resolver=_AttachmentResolver(bad_hash=True),
    ).publish_batch(owner="greenhouse", now=NOW)

    assert result.deferred == 1
    assert broker.calls == []
    assert coordinator.ambiguous == []
    assert coordinator.prepost_failures == [(dispatch, "GREENHOUSE_SUBMIT_ATTACHMENT_UNAVAILABLE")]
    assert store.releases == [
        (
            event.event_id,
            "GREENHOUSE_SUBMIT_ATTACHMENT_UNAVAILABLE",
            False,
            event.lease_token,
        )
    ]


def test_resolved_attachment_ref_mismatch_records_prepost_failure_before_retry() -> None:
    event = _event()
    dispatch = _prepared(event)
    coordinator = _Coordinator(dispatch)
    broker = _Broker(_evidence(dispatch))
    store = _Store(event)

    result = _publisher(
        store,
        coordinator,
        broker,
        attachment_resolver=_AttachmentResolver(bad_ref=True),
    ).publish_batch(owner="greenhouse", now=NOW)

    assert result.deferred == 1
    assert broker.calls == []
    assert coordinator.prepost_failures == [
        (dispatch, "GREENHOUSE_SUBMIT_ATTACHMENT_BINDING_MISMATCH")
    ]
    assert store.releases == [
        (
            event.event_id,
            "GREENHOUSE_SUBMIT_ATTACHMENT_BINDING_MISMATCH",
            False,
            event.lease_token,
        )
    ]


def test_already_terminal_prepared_state_does_not_call_broker() -> None:
    event = _event()
    prepared = _prepared(event)
    dispatch = replace(
        prepared,
        state=PreparedGreenhouseSubmitState.ALREADY_TERMINAL,
        terminal_evidence=_evidence(prepared),
    )
    broker = _Broker(_evidence(prepared))

    GreenhouseSubmitOutboxSink(
        _Coordinator(dispatch),
        broker,
        _AttachmentResolver(),
        clock=lambda: COMPLETED_AT,
    ).deliver(event)

    assert broker.calls == []


def test_unqualified_dispatch_records_local_rejection_without_broker_call() -> None:
    event = _event()
    item_schema = _schema(hard_stop=True)
    dispatch = _prepared(event, schema=item_schema)
    coordinator = _Coordinator(dispatch)
    broker = _Broker(_evidence(dispatch))

    GreenhouseSubmitOutboxSink(
        coordinator,
        broker,
        _AttachmentResolver(),
        clock=lambda: COMPLETED_AT,
    ).deliver(event)

    assert broker.calls == []
    assert coordinator.rejected[0][0] == dispatch
    assert coordinator.rejected[0][1] is None
    assert coordinator.rejected[0][2] == "GREENHOUSE_SUBMIT_NOT_QUALIFIED"
    assert "GREENHOUSE_HARD_STOP_UNKNOWN_FIELD" in coordinator.rejected[0][3]


def test_greenhouse_outbox_contract_has_no_confirmed_state() -> None:
    states = {item.value for item in PreparedGreenhouseSubmitState}

    assert states == {"ready", "already_terminal"}
    assert all("confirmed" not in state for state in states)
    assert not hasattr(PreparedGreenhouseSubmitDispatch, "confirmed_receipt")


def _publisher(
    store: _Store,
    coordinator: _Coordinator,
    broker: _Broker,
    *,
    attachment_resolver: _AttachmentResolver | None = None,
) -> OutboxPublisher:
    return OutboxPublisher(
        store,
        GreenhouseSubmitOutboxSink(
            coordinator,
            broker,
            attachment_resolver or _AttachmentResolver(),
            clock=lambda: COMPLETED_AT,
        ),
        event_key_prefix=GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX,
        clock=lambda: COMPLETED_AT,
    )
