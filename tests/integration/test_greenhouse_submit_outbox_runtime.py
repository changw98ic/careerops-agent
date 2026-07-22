from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from time import sleep
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine

from careerops.application.greenhouse_submit import (
    GreenhouseAttachmentRef,
    GreenhouseJobSchemaSnapshot,
    GreenhouseSubmissionPayload,
    GreenhouseTarget,
)
from careerops.application.greenhouse_submit_outbox import (
    GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX,
    GreenhouseSubmitOutboxSink,
)
from careerops.application.outbox import OutboxPublisher
from careerops.infrastructure.database.greenhouse_submit import (
    PostgresGreenhouseSubmitCoordinator,
    PostgresGreenhouseSubmitOutboxStore,
)
from careerops.infrastructure.greenhouse.client import (
    GreenhouseBrokerJournalState,
    GreenhouseBrokerPrePostUnavailable,
    GreenhouseResolvedAttachment,
    GreenhouseSubmissionEvidence,
    GreenhouseSubmissionOutcome,
)
from careerops.infrastructure.greenhouse.credentials import GreenhouseCredentialHandle

_CONTROL_HELPERS_SPEC = spec_from_file_location(
    "greenhouse_submit_control_plane_helpers",
    Path(__file__).with_name("test_greenhouse_submit_control_plane.py"),
)
assert _CONTROL_HELPERS_SPEC is not None
assert _CONTROL_HELPERS_SPEC.loader is not None
_CONTROL_HELPERS = module_from_spec(_CONTROL_HELPERS_SPEC)
sys.modules[_CONTROL_HELPERS_SPEC.name] = _CONTROL_HELPERS
_CONTROL_HELPERS_SPEC.loader.exec_module(_CONTROL_HELPERS)

NOW = _CONTROL_HELPERS.NOW
_json = _CONTROL_HELPERS._json
_register_account = _CONTROL_HELPERS._register_account
_reserve_params = _CONTROL_HELPERS._reserve_params
_reserve_sql = _CONTROL_HELPERS._reserve_sql
_review_sql = _CONTROL_HELPERS._review_sql
_review_snapshot_sha256 = _CONTROL_HELPERS._review_snapshot_sha256
_seed_campaign = _CONTROL_HELPERS._seed_campaign
_seed_grant = _CONTROL_HELPERS._seed_grant
_seed_owner_candidate_pair = _CONTROL_HELPERS._seed_owner_candidate_pair
_seed_release_qualification = _CONTROL_HELPERS._seed_release_qualification

pytestmark = pytest.mark.integration

OWNER = "greenhouse-submit-runtime-test"
RESUME_BYTES = b"x" * 10
RESUME_SHA256 = hashlib.sha256(RESUME_BYTES).hexdigest()
SOURCE_DRAFT_SHA256 = hashlib.sha256(b"reviewed-greenhouse-source-draft").hexdigest()
APPROVED_MATERIAL_SHA256 = hashlib.sha256(b"reviewed-candidate-material").hexdigest()


@pytest.fixture(scope="module")
def database_url() -> str:
    return _CONTROL_HELPERS.database_url.__wrapped__()  # type: ignore[attr-defined,no-any-return]


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    database_engine = sa.create_engine(database_url)
    try:
        yield database_engine
    finally:
        database_engine.dispose()


def test_accepted_unverified_runtime_submit_posts_once_publishes_and_never_confirms_from_2xx(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_greenhouse_fixture(engine, id_prefix="runtime-accepted")
    broker = _RecordingBroker()
    publisher = _publisher(engine, broker)

    first = publisher.publish_batch(owner=OWNER, now=NOW, lease_for=timedelta(minutes=5), limit=10)
    replay = publisher.publish_batch(owner=OWNER, now=NOW, lease_for=timedelta(minutes=5), limit=10)

    assert first.claimed == 1
    assert first.published == 1
    assert first.failed == 0
    assert replay.claimed == 0
    assert broker.posts == [fixture.reconciliation_key]
    with engine.connect() as connection:
        assert _greenhouse_state(connection, fixture.outbox_event_id) == {
            "event_status": "published",
            "event_error_code": None,
            "intent_status": "reconciliation_required",
            "attempt_state": "reconciliation_required",
            "attempt_error_code": "GREENHOUSE_SUBMIT_ACCEPTED_UNVERIFIED",
            "receipt_final_state": "accepted_unverified",
            "receipt_count": 1,
            "reconciliation_status": "required",
            "reconciliation_error_code": "GREENHOUSE_SUBMIT_ACCEPTED_UNVERIFIED",
        }


def test_ambiguous_post_start_is_terminal_and_repeated_dispatch_never_blind_reposts(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_greenhouse_fixture(engine, id_prefix="runtime-ambiguous")
    broker = _RecordingBroker(raise_after_post=True)
    publisher = _publisher(engine, broker)

    first = publisher.publish_batch(owner=OWNER, now=NOW, lease_for=timedelta(minutes=5), limit=10)
    replay = publisher.publish_batch(
        owner=OWNER,
        now=NOW + timedelta(minutes=10),
        lease_for=timedelta(minutes=5),
        limit=10,
    )

    assert first.claimed == 1
    assert first.failed == 1
    assert replay.claimed == 0
    assert broker.posts == [fixture.reconciliation_key]
    with engine.connect() as connection:
        state = _greenhouse_state(connection, fixture.outbox_event_id)
        assert state["event_status"] == "failed"
        assert state["event_error_code"] == "GREENHOUSE_SUBMIT_BROKER_BOUNDARY_AMBIGUOUS"
        assert state["intent_status"] == "reconciliation_required"
        assert state["attempt_state"] == "reconciliation_required"
        assert state["attempt_error_code"] == "GREENHOUSE_SUBMIT_BROKER_BOUNDARY_AMBIGUOUS"
        assert state["receipt_count"] == 0
        assert state["reconciliation_status"] == "required"


def test_only_prepost_unavailable_release_can_defer_and_reclaim_before_any_post(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_greenhouse_fixture(engine, id_prefix="runtime-prepost")
    broker = _RecordingBroker(prepost_unavailable_once=True)
    publisher = _publisher(
        engine,
        broker,
        retry_delay=timedelta(microseconds=1),
        clock=lambda: datetime(2000, 1, 1, tzinfo=UTC),
    )

    deferred = publisher.publish_batch(
        owner=OWNER,
        now=NOW,
        lease_for=timedelta(minutes=5),
        limit=10,
    )
    retried = publisher.publish_batch(
        owner=OWNER + "-retry",
        now=NOW + timedelta(minutes=10),
        lease_for=timedelta(minutes=5),
        limit=10,
    )

    assert deferred.claimed == 1
    assert deferred.deferred == 1
    assert deferred.failed == 0
    assert retried.claimed == 1
    assert retried.published == 1
    assert broker.posts == [fixture.reconciliation_key]
    with engine.connect() as connection:
        assert _greenhouse_state(connection, fixture.outbox_event_id)["event_status"] == "published"


def test_mark_published_crash_after_receipt_replay_does_not_post_twice(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_greenhouse_fixture(engine, id_prefix="runtime-mark-crash")
    broker = _RecordingBroker()
    store = _CrashOnceMarkPublishedStore(PostgresGreenhouseSubmitOutboxStore(engine))
    publisher = _publisher_with_store(engine, broker, store)

    with pytest.raises(RuntimeError, match="simulated mark_published crash"):
        publisher.publish_batch(owner=OWNER, now=NOW, lease_for=timedelta(microseconds=1), limit=10)
    sleep(1.1)
    replay = publisher.publish_batch(
        owner=OWNER,
        now=NOW + timedelta(minutes=10),
        lease_for=timedelta(minutes=5),
        limit=10,
    )

    assert replay.claimed == 1
    assert replay.published == 1
    assert broker.posts == [fixture.reconciliation_key]
    with engine.connect() as connection:
        assert _greenhouse_state(connection, fixture.outbox_event_id) == {
            "event_status": "published",
            "event_error_code": None,
            "intent_status": "reconciliation_required",
            "attempt_state": "reconciliation_required",
            "attempt_error_code": "GREENHOUSE_SUBMIT_ACCEPTED_UNVERIFIED",
            "receipt_final_state": "accepted_unverified",
            "receipt_count": 1,
            "reconciliation_status": "required",
            "reconciliation_error_code": "GREENHOUSE_SUBMIT_ACCEPTED_UNVERIFIED",
        }


def test_provider_rejected_mark_published_crash_replay_does_not_post_twice(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_greenhouse_fixture(engine, id_prefix="runtime-rejected-crash")
    broker = _RecordingBroker(outcome=GreenhouseSubmissionOutcome.REJECTED)
    store = _CrashOnceMarkPublishedStore(PostgresGreenhouseSubmitOutboxStore(engine))
    publisher = _publisher_with_store(engine, broker, store)

    with pytest.raises(RuntimeError, match="simulated mark_published crash"):
        publisher.publish_batch(owner=OWNER, now=NOW, lease_for=timedelta(microseconds=1), limit=10)
    sleep(1.1)
    replay = publisher.publish_batch(
        owner=OWNER + "-recovery",
        now=NOW + timedelta(minutes=10),
        lease_for=timedelta(minutes=5),
        limit=10,
    )

    assert replay.claimed == 1
    assert replay.published == 1
    assert broker.posts == [fixture.reconciliation_key]
    with engine.connect() as connection:
        assert _greenhouse_state(connection, fixture.outbox_event_id) == {
            "event_status": "published",
            "event_error_code": None,
            "intent_status": "failed",
            "attempt_state": "rejected",
            "attempt_error_code": "GREENHOUSE_PROVIDER_VALIDATION_REJECTED",
            "receipt_final_state": "provider_rejected",
            "receipt_count": 1,
            "reconciliation_status": None,
            "reconciliation_error_code": None,
        }


@dataclass(frozen=True, slots=True)
class RuntimeFixture:
    owner_id: UUID
    candidate_id: UUID
    account_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    outbox_event_id: UUID
    reconciliation_key: str
    schema: GreenhouseJobSchemaSnapshot
    payload: GreenhouseSubmissionPayload


def _seed_runtime_greenhouse_fixture(engine: Engine, *, id_prefix: str) -> RuntimeFixture:
    with engine.begin() as connection:
        owner_id, candidate_id = _seed_owner_candidate_pair(connection, id_prefix)
        account_id = cast(
            UUID,
            _register_account(connection, owner_id, candidate_id, key=id_prefix)["account_id"],
        )
        campaign_id = _seed_campaign(connection, owner_id)
        schema = _schema(id_prefix=id_prefix)
        payload = _payload(schema)
        target_json = _target_json(schema=schema, payload=payload)
        payload_json = _payload_json(schema=schema, payload=payload, target_json=target_json)
        review_snapshot_sha256 = _review_snapshot_sha256(dict(target_json), payload_json)
        attachment_refs = [item.canonical() for item in payload.attachment_refs]
        draft = (
            connection.execute(
                sa.text(_CONTROL_HELPERS._create_draft_sql()),
                _CONTROL_HELPERS._create_draft_params(
                    owner_id,
                    candidate_id,
                    dict(target_json),
                    payload_json,
                    list(attachment_refs),
                ),
            )
            .mappings()
            .one()
        )
        grant_id = _seed_grant(
            connection,
            campaign_id=campaign_id,
            allowed_materials=(
                schema.schema_hash,
                payload.material_hash,
                RESUME_SHA256,
            ),
        )
        authorization_id = uuid4()
        reviewed = (
            connection.execute(
                sa.text(_review_sql()),
                {
                    "owner_id": owner_id,
                    "action_intent_id": draft["action_intent_id"],
                    "payload_version_id": draft["payload_version_id"],
                    "approval_request_id": draft["approval_request_id"],
                    "campaign_id": campaign_id,
                    "grant_id": grant_id,
                    "payload_hash": draft["payload_hash"],
                    "material_hash": payload.material_hash,
                    "submission_identity_sha256": payload.submission_identity,
                    "decision": "approved",
                    "reviewed_by_user_id": owner_id,
                    "authorization_id": authorization_id,
                    "review_snapshot_sha256": review_snapshot_sha256,
                    "idempotency_key": f"review-{id_prefix}-{uuid4()}",
                    "authorization_expires_at": NOW + timedelta(hours=1),
                    "trace_id": f"trace-review-{id_prefix}",
                    "reason": "exact reviewed Greenhouse runtime payload",
                },
            )
            .mappings()
            .one()
        )
        release_qualification_id, _, _ = _seed_release_qualification(
            connection, owner_id, account_id, id_prefix=id_prefix
        )
        fixture = {
            "owner_id": owner_id,
            "candidate_id": candidate_id,
            "account_id": account_id,
            "campaign_id": campaign_id,
            "grant_id": grant_id,
            "authorization_id": reviewed["authorization_id"],
            "action_intent_id": draft["action_intent_id"],
            "payload_version_id": draft["payload_version_id"],
            "payload_hash": draft["payload_hash"],
            "board_token_sha256": target_json["board_token_sha256"],
            "job_id_sha256": target_json["job_id_sha256"],
            "schema_sha256": schema.schema_hash,
            "raw_response_sha256": schema.raw_response_sha256,
            "normalized_schema_sha256": schema.schema_hash,
            "material_hash": payload.material_hash,
            "submission_identity_sha256": payload.submission_identity,
            "approval_request_id": draft["approval_request_id"],
            "review_evidence_sha256": _sha(f"review-evidence:{id_prefix}"),
            "review_snapshot_sha256": review_snapshot_sha256,
            "reviewed_by_user_id": owner_id,
            "release_qualification_id": release_qualification_id,
            "reservation_key": f"greenhouse-reservation-{id_prefix}-{uuid4().hex}",
            "reconciliation_key": f"greenhouse-reconcile:{id_prefix}:{uuid4().hex}",
        }
        reserved = (
            connection.execute(
                sa.text(_reserve_sql()),
                _reserve_params(fixture, key=f"reserve-{id_prefix}"),
            )
            .mappings()
            .one()
        )
        return RuntimeFixture(
            owner_id=owner_id,
            candidate_id=candidate_id,
            account_id=account_id,
            action_intent_id=cast(UUID, draft["action_intent_id"]),
            payload_version_id=cast(UUID, draft["payload_version_id"]),
            outbox_event_id=cast(UUID, reserved["outbox_event_id"]),
            reconciliation_key=cast(str, fixture["reconciliation_key"]),
            schema=schema,
            payload=payload,
        )


def _publisher(
    engine: Engine,
    broker: _RecordingBroker,
    *,
    retry_delay: timedelta = timedelta(minutes=5),
    clock: Callable[[], datetime] | None = None,
) -> OutboxPublisher:
    return _publisher_with_store(
        engine,
        broker,
        PostgresGreenhouseSubmitOutboxStore(engine),
        retry_delay=retry_delay,
        clock=clock,
    )


def _publisher_with_store(
    engine: Engine,
    broker: _RecordingBroker,
    store: object,
    *,
    retry_delay: timedelta = timedelta(minutes=5),
    clock: Callable[[], datetime] | None = None,
) -> OutboxPublisher:
    return OutboxPublisher(
        cast(PostgresGreenhouseSubmitOutboxStore, store),
        GreenhouseSubmitOutboxSink(
            PostgresGreenhouseSubmitCoordinator(engine, owner=OWNER),
            broker,
            _AttachmentResolver(),
        ),
        retry_delay=retry_delay,
        event_key_prefix=GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX,
        clock=clock,
    )


class _CrashOnceMarkPublishedStore:
    def __init__(self, inner: PostgresGreenhouseSubmitOutboxStore) -> None:
        self._inner = inner
        self._crash_once = True

    def claim(self, **kwargs: object) -> tuple[object, ...]:
        return self._inner.claim(**kwargs)  # type: ignore[arg-type]

    def mark_published(self, *args: object, **kwargs: object) -> None:
        if self._crash_once:
            self._crash_once = False
            raise RuntimeError("simulated mark_published crash")
        self._inner.mark_published(*args, **kwargs)  # type: ignore[arg-type]

    def release(self, *args: object, **kwargs: object) -> None:
        self._inner.release(*args, **kwargs)  # type: ignore[arg-type]


class _RecordingBroker:
    def __init__(
        self,
        *,
        raise_after_post: bool = False,
        prepost_unavailable_once: bool = False,
        outcome: GreenhouseSubmissionOutcome = GreenhouseSubmissionOutcome.ACCEPTED_UNVERIFIED,
    ) -> None:
        self._raise_after_post = raise_after_post
        self._prepost_unavailable_once = prepost_unavailable_once
        self._outcome = outcome
        self.posts: list[str] = []

    def submit(
        self,
        handle: GreenhouseCredentialHandle,
        *,
        schema: GreenhouseJobSchemaSnapshot,
        payload: GreenhouseSubmissionPayload,
        attachments: Sequence[GreenhouseResolvedAttachment],
        reconciliation_key: str,
    ) -> GreenhouseSubmissionEvidence:
        del handle, attachments
        if self._prepost_unavailable_once:
            self._prepost_unavailable_once = False
            raise GreenhouseBrokerPrePostUnavailable("GREENHOUSE_BROKER_PREPOST_UNAVAILABLE")
        self.posts.append(reconciliation_key)
        if self._raise_after_post:
            raise RuntimeError("post-start broker boundary outcome is ambiguous")
        reason_code = (
            "GREENHOUSE_PROVIDER_VALIDATION_REJECTED"
            if self._outcome is GreenhouseSubmissionOutcome.REJECTED
            else "GREENHOUSE_PROVIDER_ACCEPTED_UNVERIFIED"
        )
        return GreenhouseSubmissionEvidence(
            outcome=self._outcome,
            reason_code=reason_code,
            submission_identity=payload.submission_identity,
            reconciliation_key=reconciliation_key,
            broker_request_sha256=_sha(f"broker-request:{reconciliation_key}"),
            broker_response_sha256=_sha(f"broker-response:{reconciliation_key}"),
            status_code=422 if self._outcome is GreenhouseSubmissionOutcome.REJECTED else 202,
            journal_receipt_hash=_sha(f"journal:{reconciliation_key}"),
            journal_state=GreenhouseBrokerJournalState.RESPONSE_OBSERVED,
            journal_sequence=1,
            expected_schema_hash=schema.schema_hash,
            payload_hash=payload.payload_hash,
            material_hash=payload.material_hash,
            provider_request_sha256=_sha(f"provider-request:{reconciliation_key}"),
            provider_response_sha256=_sha(f"provider-response:{reconciliation_key}"),
            observed_schema_hash=schema.schema_hash,
            observed_schema_raw_response_sha256=schema.raw_response_sha256,
        )


class _AttachmentResolver:
    def resolve(self, ref: object) -> GreenhouseResolvedAttachment:
        assert isinstance(ref, GreenhouseAttachmentRef)
        return GreenhouseResolvedAttachment(ref=ref, data=RESUME_BYTES)


def _greenhouse_state(connection: Connection, event_id: UUID) -> dict[str, object]:
    row = (
        connection.execute(
            sa.text(
                """
                SELECT event.status AS event_status,
                       event.last_error_code AS event_error_code,
                       intent.status AS intent_status,
                       attempt.state AS attempt_state,
                       attempt.error_code AS attempt_error_code,
                       receipt.final_state AS receipt_final_state,
                       (
                           SELECT count(*)
                           FROM careerops.provider_receipts AS counted_receipt
                           JOIN careerops.side_effect_attempts AS counted_attempt
                             ON counted_attempt.id = counted_receipt.side_effect_attempt_id
                           WHERE counted_attempt.outbox_event_id = event.id
                       ) AS receipt_count,
                       job.status AS reconciliation_status,
                       job.last_error_code AS reconciliation_error_code
                FROM careerops.outbox_events AS event
                JOIN careerops.action_intents AS intent ON intent.id = event.action_intent_id
                LEFT JOIN LATERAL (
                    SELECT latest_attempt.*
                    FROM careerops.side_effect_attempts AS latest_attempt
                    WHERE latest_attempt.outbox_event_id = event.id
                    ORDER BY latest_attempt.ordinal DESC
                    LIMIT 1
                ) AS attempt ON true
                LEFT JOIN careerops.provider_receipts AS receipt
                  ON receipt.side_effect_attempt_id = attempt.id
                LEFT JOIN careerops.greenhouse_submit_reconciliation_jobs AS job
                  ON job.outbox_event_id = event.id
                WHERE event.id = :event_id
                """
            ),
            {"event_id": event_id},
        )
        .mappings()
        .one()
    )
    return dict(row)


def _schema(*, id_prefix: str) -> GreenhouseJobSchemaSnapshot:
    target = GreenhouseTarget(board_token="publicboard", job_id=123456)
    raw = json.dumps(
        _schema_snapshot_document(id_prefix=id_prefix),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return GreenhouseJobSchemaSnapshot.from_json_bytes(target=target, raw_response=raw)


def _schema_snapshot_document(*, id_prefix: str) -> dict[str, object]:
    del id_prefix
    return {
        "board_token": "publicboard",
        "id": 123456,
        "internal_job_id": 987654,
        "title": "Runtime Test Engineer",
        "company_name": "Example AI",
        "updated_at": "2026-07-21T08:00:00Z",
        "application_deadline": None,
        "absolute_url": "https://job-boards.greenhouse.io/publicboard/jobs/123456",
        "questions": [
            _question("First name", True, "first_name"),
            _question("Last name", True, "last_name"),
            _question("Email", True, "email"),
            _question("Resume", True, "resume", kind="input_file"),
        ],
    }


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
        source_draft_payload_hash=SOURCE_DRAFT_SHA256,
        approved_material_hashes=(APPROVED_MATERIAL_SHA256,),
        attachment_refs=(
            GreenhouseAttachmentRef(
                field_name="resume",
                object_key=f"resume/{uuid4().hex}.pdf",
                filename="resume.pdf",
                content_type="application/pdf",
                size_bytes=len(RESUME_BYTES),
                sha256=RESUME_SHA256,
            ),
        ),
    )


def _target_json(
    *,
    schema: GreenhouseJobSchemaSnapshot,
    payload: GreenhouseSubmissionPayload,
) -> dict[str, object]:
    board_token_sha256 = _sha(payload.target.board_token)
    job_id_sha256 = _sha(str(payload.target.job_id))
    job_identity_sha256 = _sha_json(
        {
            "board_token_sha256": board_token_sha256,
            "job_id_sha256": job_id_sha256,
            "internal_job_id": schema.internal_job_id,
            "updated_at": schema.updated_at,
            "schema_sha256": schema.schema_hash,
        }
    )
    return {
        "host": "boards-api.greenhouse.io",
        "board_token": payload.target.board_token,
        "job_id": payload.target.job_id,
        "target_host": "boards-api.greenhouse.io",
        "channel": "greenhouse:job-board",
        "adapter_id": "greenhouse-job-board",
        "fixture_id": "greenhouse-submit.v1",
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
        "schema_snapshot": _schema_snapshot_document(id_prefix="prepared"),
    }


def _payload_json(
    *,
    schema: GreenhouseJobSchemaSnapshot,
    payload: GreenhouseSubmissionPayload,
    target_json: dict[str, object],
) -> dict[str, object]:
    return {
        "version": "greenhouse-submit-payload.v1",
        "fields": dict(payload.fields),
        "source_draft_payload_hash": payload.source_draft_payload_hash,
        "approved_material_hashes": list(payload.approved_material_hashes),
        "answer_sha256": _sha_json(dict(payload.fields)),
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


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value: object) -> str:
    return _sha(_json(value))
