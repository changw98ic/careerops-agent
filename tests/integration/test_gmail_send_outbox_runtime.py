from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from careerops.application.gmail_send import GmailSendPayload, GmailSendProviderState
from careerops.application.gmail_send_outbox import (
    GMAIL_SEND_EVENT_KEY_PREFIX,
    GmailResolvedAttachment,
    GmailSendAccessToken,
    GmailSendCredentialError,
    GmailSendOutboxSink,
    GmailSendProviderReceipt,
    GmailSendReconciliationJob,
    GmailSendReconciliationService,
    GmailSentMetadata,
    PreparedGmailSendDispatch,
)
from careerops.application.outbox import OutboxPublisher
from careerops.infrastructure.database.gmail_send import (
    PostgresGmailSendCoordinator,
    PostgresGmailSendOutboxStore,
    PostgresGmailSendReconciliationRepository,
)

_CONTROL_HELPERS_SPEC = spec_from_file_location(
    "gmail_send_control_plane_helpers",
    Path(__file__).with_name("test_gmail_send_control_plane.py"),
)
assert _CONTROL_HELPERS_SPEC is not None
assert _CONTROL_HELPERS_SPEC.loader is not None
_CONTROL_HELPERS = module_from_spec(_CONTROL_HELPERS_SPEC)
sys.modules[_CONTROL_HELPERS_SPEC.name] = _CONTROL_HELPERS
_CONTROL_HELPERS_SPEC.loader.exec_module(_CONTROL_HELPERS)

NOW = _CONTROL_HELPERS.NOW
SendFixture = _CONTROL_HELPERS.SendFixture
_json = _CONTROL_HELPERS._json
_register_send_account = _CONTROL_HELPERS._register_send_account
_review_draft_sql = _CONTROL_HELPERS._review_draft_sql
_review_params = _CONTROL_HELPERS._review_params
_seed_campaign = _CONTROL_HELPERS._seed_campaign
_seed_grant = _CONTROL_HELPERS._seed_grant
_seed_owner_candidate_pair = _CONTROL_HELPERS._seed_owner_candidate_pair

pytestmark = pytest.mark.integration

OWNER = "gmail-send-runtime-test"
RECONCILED_AT = NOW + timedelta(minutes=5)
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


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


def test_confirmed_runtime_send_publishes_once_and_replay_does_not_post(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_send_fixture(engine, id_prefix="runtime-confirmed")
    send_client = _RecordingSendClient()
    publisher = _publisher(engine, send_client)

    first = publisher.publish_batch(owner=OWNER, now=NOW, lease_for=timedelta(minutes=5), limit=10)
    replay = publisher.publish_batch(owner=OWNER, now=NOW, lease_for=timedelta(minutes=5), limit=10)

    assert first.claimed == 1
    assert first.published == 1
    assert first.failed == 0
    assert replay.claimed == 0
    assert send_client.posts == [fixture.payload.message_id_header]
    with engine.connect() as connection:
        assert _event_status(connection, fixture.outbox_event_id) == "published"
        assert _provider_receipt_count(connection, fixture.action_intent_id) == 1


def test_post_boundary_ambiguity_is_terminal_and_not_reclaimed_for_blind_resend(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_send_fixture(engine, id_prefix="runtime-ambiguous")
    send_client = _RecordingSendClient(raise_after_post=True)
    publisher = _publisher(engine, send_client)

    first = publisher.publish_batch(owner=OWNER, now=NOW, lease_for=timedelta(minutes=5), limit=10)
    replay = publisher.publish_batch(owner=OWNER, now=NOW, lease_for=timedelta(minutes=5), limit=10)

    assert first.claimed == 1
    assert first.failed == 1
    assert replay.claimed == 0
    assert send_client.posts == [fixture.payload.message_id_header]
    with engine.connect() as connection:
        event = (
            connection.execute(
                sa.text(
                    """
                SELECT event.status, event.last_error_code, intent.status AS intent_status
                FROM careerops.outbox_events AS event
                JOIN careerops.action_intents AS intent ON intent.id = event.action_intent_id
                WHERE event.id = :event_id
                """
                ),
                {"event_id": fixture.outbox_event_id},
            )
            .mappings()
            .one()
        )
        assert event == {
            "status": "failed",
            "last_error_code": "GMAIL_SEND_POST_OUTCOME_AMBIGUOUS",
            "intent_status": "reconciliation_required",
        }
        assert _provider_receipt_count(connection, fixture.action_intent_id) == 0
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                UPDATE careerops.gmail_send_reconciliation_jobs
                SET status = 'blocked',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE outbox_event_id = :event_id
                """
            ),
            {"event_id": fixture.outbox_event_id},
        )


def test_terminal_pre_post_release_does_not_queue_reconciliation_job(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_send_fixture(engine, id_prefix="runtime-terminal-before-post")
    store = PostgresGmailSendOutboxStore(engine)
    claimed = store.claim(owner=OWNER, now=NOW, lease_for=timedelta(minutes=5), limit=1)
    assert [event.event_id for event in claimed] == [fixture.outbox_event_id]

    store.release(
        fixture.outbox_event_id,
        owner=OWNER,
        lease_token=claimed[0].lease_token,
        now=NOW,
        retry_at=NOW,
        error_code="GMAIL_SEND_RELEASE_EVIDENCE_EXPIRED",
        terminal=True,
    )

    with engine.connect() as connection:
        state = (
            connection.execute(
                sa.text(
                    """
                    SELECT event.status AS event_status,
                           event.last_error_code AS event_error_code,
                           (
                               SELECT count(*)
                               FROM careerops.side_effect_attempts AS attempt
                               WHERE attempt.outbox_event_id = event.id
                           ) AS attempt_count,
                           (
                               SELECT count(*)
                               FROM careerops.gmail_send_reconciliation_jobs AS job
                               WHERE job.outbox_event_id = event.id
                           ) AS reconciliation_job_count
                    FROM careerops.outbox_events AS event
                    WHERE event.id = :event_id
                    """
                ),
                {"event_id": fixture.outbox_event_id},
            )
            .mappings()
            .one()
        )
        assert state == {
            "event_status": "failed",
            "event_error_code": "GMAIL_SEND_RELEASE_EVIDENCE_EXPIRED",
            "attempt_count": 0,
            "reconciliation_job_count": 0,
        }
        assert _provider_receipt_count(connection, fixture.action_intent_id) == 0


def test_retryable_pre_post_failure_then_retry_posts_once_and_publishes(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_send_fixture(engine, id_prefix="runtime-retryable-before-post")
    resolver = _FailOnceSendCredentialResolver("GMAIL_SEND_CREDENTIAL_EXPIRED")
    send_client = _RecordingSendClient()
    publisher = _publisher(
        engine,
        send_client,
        credential_resolver=resolver,
        retry_delay=timedelta(microseconds=1),
        publisher_clock=lambda: datetime(2000, 1, 1, tzinfo=UTC),
    )

    deferred = publisher.publish_batch(
        owner=OWNER,
        now=NOW,
        lease_for=timedelta(minutes=5),
        limit=10,
    )
    retried = publisher.publish_batch(
        owner=OWNER,
        now=NOW + timedelta(minutes=10),
        lease_for=timedelta(minutes=5),
        limit=10,
    )

    assert deferred.claimed == 1
    assert deferred.deferred == 1
    assert deferred.failed == 0
    assert retried.claimed == 1
    assert retried.published == 1
    assert retried.failed == 0
    assert send_client.posts == [fixture.payload.message_id_header]
    with engine.connect() as connection:
        assert _event_status(connection, fixture.outbox_event_id) == "published"
        assert _provider_receipt_count(connection, fixture.action_intent_id) == 1
        assert _reconciliation_job_count(connection, fixture.outbox_event_id) == 0


def test_ambiguous_post_recovers_from_sent_metadata_without_second_post(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_send_fixture(engine, id_prefix="runtime-ambiguity-recovered")
    sent_metadata = _sent_metadata_for_payload(fixture.payload)
    sent_metadata_client = _SentMetadataClient(
        find_by_rfc_message_id={fixture.payload.message_id_header: sent_metadata}
    )
    send_client = _RecordingSendClient(raise_after_post=True)
    publisher = _publisher(engine, send_client, sent_metadata_client=sent_metadata_client)

    ambiguous = publisher.publish_batch(
        owner=OWNER,
        now=NOW,
        lease_for=timedelta(minutes=5),
        limit=10,
    )
    service = GmailSendReconciliationService(
        PostgresGmailSendReconciliationRepository(engine),
        _CredentialResolver(),
        sent_metadata_client,
        clock=lambda: RECONCILED_AT,
    )
    reconciled = service.reconcile_batch(
        owner="gmail-send-runtime-reconciliation-test",
        now=NOW + timedelta(minutes=10),
        limit=10,
    )
    replay = publisher.publish_batch(
        owner=OWNER,
        now=NOW + timedelta(minutes=15),
        lease_for=timedelta(minutes=5),
        limit=10,
    )

    assert ambiguous.claimed == 1
    assert ambiguous.failed == 1
    assert reconciled == (1, 1, 0)
    assert replay.claimed == 0
    assert send_client.posts == [fixture.payload.message_id_header]
    assert sent_metadata_client.lookups == [fixture.payload.message_id_header]
    with engine.connect() as connection:
        assert _provider_receipt_count(connection, fixture.action_intent_id) == 1
        state = (
            connection.execute(
                sa.text(
                    """
                    SELECT event.status AS event_status,
                           intent.status AS intent_status,
                           job.status AS reconciliation_status
                    FROM careerops.outbox_events AS event
                    JOIN careerops.action_intents AS intent ON intent.id = event.action_intent_id
                    JOIN careerops.gmail_send_reconciliation_jobs AS job
                      ON job.outbox_event_id = event.id
                    WHERE event.id = :event_id
                    """
                ),
                {"event_id": fixture.outbox_event_id},
            )
            .mappings()
            .one()
        )
        assert state == {
            "event_status": "published",
            "intent_status": "confirmed",
            "reconciliation_status": "resolved",
        }


def test_hostile_sent_metadata_mismatch_stays_unpublished_without_receipt(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_send_fixture(engine, id_prefix="runtime-hostile-sent-metadata")
    sent_metadata = replace(
        _sent_metadata_for_payload(fixture.payload),
        headers={
            "From": fixture.payload.sender,
            "To": f"attacker-{uuid4().hex[:8]}@example.com",
            "Subject": fixture.payload.subject,
            "Message-ID": fixture.payload.message_id_header,
        },
    )
    sent_metadata_client = _SentMetadataClient(
        find_by_rfc_message_id={fixture.payload.message_id_header: sent_metadata}
    )
    send_client = _RecordingSendClient(raise_after_post=True)
    publisher = _publisher(engine, send_client, sent_metadata_client=sent_metadata_client)

    ambiguous = publisher.publish_batch(
        owner=OWNER,
        now=NOW,
        lease_for=timedelta(minutes=5),
        limit=10,
    )
    service = GmailSendReconciliationService(
        PostgresGmailSendReconciliationRepository(engine),
        _CredentialResolver(),
        sent_metadata_client,
        clock=lambda: RECONCILED_AT,
    )
    reconciled = service.reconcile_batch(
        owner="gmail-send-runtime-reconciliation-test",
        now=NOW + timedelta(minutes=10),
        limit=10,
    )
    replay = publisher.publish_batch(
        owner=OWNER,
        now=NOW + timedelta(minutes=15),
        lease_for=timedelta(minutes=5),
        limit=10,
    )

    assert ambiguous.claimed == 1
    assert ambiguous.failed == 1
    assert reconciled == (1, 0, 1)
    assert replay.claimed == 0
    assert send_client.posts == [fixture.payload.message_id_header]
    assert sent_metadata_client.lookups == [fixture.payload.message_id_header]
    with engine.connect() as connection:
        assert _provider_receipt_count(connection, fixture.action_intent_id) == 0
        state = (
            connection.execute(
                sa.text(
                    """
                    SELECT event.status AS event_status,
                           event.last_error_code AS event_error_code,
                           intent.status AS intent_status,
                           job.status AS reconciliation_status,
                           job.last_error_code AS reconciliation_error_code
                    FROM careerops.outbox_events AS event
                    JOIN careerops.action_intents AS intent ON intent.id = event.action_intent_id
                    JOIN careerops.gmail_send_reconciliation_jobs AS job
                      ON job.outbox_event_id = event.id
                    WHERE event.id = :event_id
                    """
                ),
                {"event_id": fixture.outbox_event_id},
            )
            .mappings()
            .one()
        )
        assert state == {
            "event_status": "failed",
            "event_error_code": "GMAIL_SEND_POST_OUTCOME_AMBIGUOUS",
            "intent_status": "reconciliation_required",
            "reconciliation_status": "queued",
            "reconciliation_error_code": "GMAIL_SEND_SENT_TO_MISMATCH",
        }


def test_expired_prepared_lease_never_blind_replays_but_pre_post_release_reclaims(
    engine: Engine,
) -> None:
    crashed = _seed_runtime_send_fixture(engine, id_prefix="runtime-crashed-after-prepare")
    store = PostgresGmailSendOutboxStore(engine)
    claimed = store.claim(owner=OWNER, now=NOW, lease_for=timedelta(seconds=1), limit=1)
    assert [event.event_id for event in claimed] == [crashed.outbox_event_id]

    dispatch = PostgresGmailSendCoordinator(engine, owner=OWNER).prepare(claimed[0])
    assert dispatch.event_id == crashed.outbox_event_id
    with engine.begin() as connection:
        connection.execute(sa.text("SELECT pg_sleep(1.1)"))

    send_client = _RecordingSendClient()
    replay = _publisher(engine, send_client).publish_batch(
        owner=OWNER,
        now=NOW + timedelta(minutes=10),
        lease_for=timedelta(minutes=5),
        limit=10,
    )

    assert replay.claimed == 0
    assert send_client.posts == []
    with engine.connect() as connection:
        crash_state = (
            connection.execute(
                sa.text(
                    """
                    SELECT event.status AS event_status,
                           event.last_error_code AS event_error_code,
                           intent.status AS intent_status,
                           attempt.state AS attempt_state,
                           attempt.error_code AS attempt_error_code,
                           job.status AS reconciliation_status,
                           job.last_error_code AS reconciliation_error_code
                    FROM careerops.outbox_events AS event
                    JOIN careerops.action_intents AS intent
                      ON intent.id = event.action_intent_id
                    JOIN careerops.side_effect_attempts AS attempt
                      ON attempt.outbox_event_id = event.id
                    JOIN careerops.gmail_send_reconciliation_jobs AS job
                      ON job.outbox_event_id = event.id
                    WHERE event.id = :event_id
                    """
                ),
                {"event_id": crashed.outbox_event_id},
            )
            .mappings()
            .one()
        )
        assert crash_state == {
            "event_status": "failed",
            "event_error_code": "GMAIL_SEND_PREPARED_LEASE_EXPIRED",
            "intent_status": "reconciliation_required",
            "attempt_state": "reconciliation_required",
            "attempt_error_code": "GMAIL_SEND_PREPARED_LEASE_EXPIRED",
            "reconciliation_status": "queued",
            "reconciliation_error_code": "GMAIL_SEND_PREPARED_LEASE_EXPIRED",
        }

    reconciliation_jobs = PostgresGmailSendReconciliationRepository(engine).claim_due(
        owner="gmail-send-runtime-reconciliation-test",
        now=NOW + timedelta(minutes=10),
        limit=100,
    )
    assert crashed.outbox_event_id in {job.event_id for job in reconciliation_jobs}

    retryable = _seed_runtime_send_fixture(engine, id_prefix="runtime-pre-post-release")
    first_claim = store.claim(owner=OWNER, now=NOW, lease_for=timedelta(minutes=5), limit=1)
    assert [event.event_id for event in first_claim] == [retryable.outbox_event_id]
    store.release(
        retryable.outbox_event_id,
        owner=OWNER,
        lease_token=first_claim[0].lease_token,
        now=NOW,
        retry_at=datetime(2000, 1, 1, tzinfo=UTC),
        error_code="GMAIL_SEND_CREDENTIAL_UNAVAILABLE",
        terminal=False,
    )

    reclaimed = store.claim(
        owner=OWNER,
        now=NOW + timedelta(minutes=10),
        lease_for=timedelta(minutes=5),
        limit=1,
    )
    assert [event.event_id for event in reclaimed] == [retryable.outbox_event_id]
    assert reclaimed[0].attempt_count == 2
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    """
                    SELECT count(*)
                    FROM careerops.side_effect_attempts
                    WHERE outbox_event_id = :event_id
                    """
                ),
                {"event_id": retryable.outbox_event_id},
            )
            == 0
        )
    store.release(
        retryable.outbox_event_id,
        owner=OWNER,
        lease_token=reclaimed[0].lease_token,
        now=NOW,
        retry_at=datetime(2099, 1, 1, tzinfo=UTC),
        error_code="GMAIL_SEND_TEST_CLEANUP",
        terminal=False,
    )


def test_mail_sender_runtime_role_can_claim_prepare_and_cannot_mutate_receipts_directly(
    engine: Engine,
) -> None:
    fixture = _seed_runtime_send_fixture(engine, id_prefix="runtime-permission")
    store = PostgresGmailSendOutboxStore(engine)
    claimed = store.claim(owner=OWNER, now=NOW, lease_for=timedelta(minutes=5), limit=1)
    assert len(claimed) == 1

    coordinator = PostgresGmailSendCoordinator(engine, owner=OWNER)
    dispatch = coordinator.prepare(claimed[0])
    assert dispatch.event_id == fixture.outbox_event_id
    with pytest.raises(DBAPIError) as error, engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_mail_sender"))
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.provider_receipts (
                    id, side_effect_attempt_id, provider, provider_resource_id,
                    reconciliation_key, final_state, provider_timestamp
                )
                VALUES (:id, :attempt_id, 'gmail', 'forged', 'forged', 'confirmed', :now)
                """
            ),
            {"id": uuid4(), "attempt_id": uuid4(), "now": NOW},
        )
    assert _sqlstate(error.value) == "42501"


@dataclass(frozen=True, slots=True)
class RuntimeFixture:
    owner_id: UUID
    candidate_id: UUID
    account_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    outbox_event_id: UUID
    payload: GmailSendPayload


def _seed_qualified_gmail_release(
    connection: Connection,
    *,
    owner_id: UUID,
    account_id: UUID,
    id_prefix: str,
) -> UUID:
    qualification_id = uuid4()
    evidence_id = uuid4()
    credential_ref_hash = connection.scalar(
        sa.text(
            """
            SELECT credential_store_evidence_sha256
            FROM careerops.gmail_send_accounts
            WHERE id = :account_id
              AND owner_user_id = :owner_id
            """
        ),
        {"account_id": account_id, "owner_id": owner_id},
    )
    assert isinstance(credential_ref_hash, str)
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.release_qualifications (
                id, capability, action_name, rollout_mode, provider, adapter_id,
                adapter_version, implementation_hash, config_hash, policy_hash,
                dataset_hash, git_commit, migration_revision, oauth_scope_hash,
                credential_ref_hash, network_policy_hash, reconcile_policy_hash,
                hard_stop_hash, sensitive_field_hash, kill_switch_hash,
                fixture_manifest_sha256, fault_manifest_sha256, status,
                requested_by_user_id, created_by, expires_at
            ) VALUES (
                :id, 'gmail_send', 'send_email', 'review_required',
                'gmail', 'gmail', 'gmail-send.v1',
                :implementation_hash, :config_hash, :policy_hash, :dataset_hash,
                :git_commit, '0013', :oauth_scope_hash, :credential_ref_hash,
                :network_policy_hash, :reconcile_policy_hash, :hard_stop_hash,
                :sensitive_field_hash, :kill_switch_hash, :fixture_manifest_sha256,
                :fault_manifest_sha256, 'draft', :owner_id, :created_by,
                :expires_at
            )
            """
        ),
        {
            "id": qualification_id,
            "implementation_hash": _sha_text(f"implementation:{id_prefix}"),
            "config_hash": _sha_text(f"config:{id_prefix}"),
            "policy_hash": _sha_text(f"policy:{id_prefix}"),
            "dataset_hash": _sha_text(f"dataset:{id_prefix}"),
            "git_commit": "a" * 40,
            "oauth_scope_hash": _sha_text(GMAIL_SEND_SCOPE),
            "credential_ref_hash": credential_ref_hash,
            "network_policy_hash": _sha_text(f"network:{id_prefix}"),
            "reconcile_policy_hash": _sha_text(f"reconcile-policy:{id_prefix}"),
            "hard_stop_hash": _sha_text(f"hard-stop:{id_prefix}"),
            "sensitive_field_hash": _sha_text(f"sensitive:{id_prefix}"),
            "kill_switch_hash": _sha_text(f"kill-switch:{id_prefix}"),
            "fixture_manifest_sha256": _sha_text(f"fixture:{id_prefix}"),
            "fault_manifest_sha256": _sha_text(f"fault:{id_prefix}"),
            "owner_id": owner_id,
            "created_by": f"{id_prefix}-runtime-test",
            "expires_at": NOW + timedelta(hours=1),
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.release_qualification_decisions (
                id, qualification_id, from_status, to_status, decision_role,
                actor_id, decided_by_user_id, reason, evidence_sha256, evidence_ids
            ) VALUES (
                :id, :qualification_id, 'draft', 'evaluating', 'runner',
                :actor_id, NULL, 'runtime test evaluation opened',
                :evidence_sha256, ARRAY[]::uuid[]
            )
            """
        ),
        {
            "id": uuid4(),
            "qualification_id": qualification_id,
            "actor_id": f"{id_prefix}-runner",
            "evidence_sha256": _sha_text(f"open-evidence:{id_prefix}"),
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.release_qualification_evidence (
                id, qualification_id, evidence_kind, artifact_uri, artifact_sha256,
                run_id, runner_user_id, runner_actor, metrics, sample_manifest_sha256
            ) VALUES (
                :id, :qualification_id, 'metrics', :artifact_uri, :artifact_sha256,
                :run_id, :owner_id, :runner_actor, CAST(:metrics AS jsonb),
                :sample_manifest_sha256
            )
            """
        ),
        {
            "id": evidence_id,
            "qualification_id": qualification_id,
            "artifact_uri": f"datasets/derived/runtime/{id_prefix}/metrics.json",
            "artifact_sha256": _sha_text(f"artifact:{id_prefix}"),
            "run_id": f"gmail-send-runtime-{uuid4()}",
            "owner_id": owner_id,
            "runner_actor": f"{id_prefix}-runner",
            "metrics": _json(
                {
                    "total_observations": 50,
                    "external_provider_calls": 0,
                    "false_negative": 0,
                    "autonomous_provider_write_attempts": 0,
                    "no_autonomous_writes": True,
                }
            ),
            "sample_manifest_sha256": _sha_text(f"sample:{id_prefix}"),
        },
    )
    for from_status, to_status, role, actor_id in (
        ("evaluating", "pending_independent_review", "reviewer", f"{id_prefix}-reviewer"),
        ("pending_independent_review", "qualified", "operator", f"{id_prefix}-operator"),
    ):
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.release_qualification_decisions (
                    id, qualification_id, from_status, to_status, decision_role,
                    actor_id, decided_by_user_id, reason, evidence_sha256, evidence_ids
                ) VALUES (
                    :id, :qualification_id, :from_status, :to_status, :role,
                    :actor_id, :owner_id, :reason, :evidence_sha256,
                    CAST(:evidence_ids AS uuid[])
                )
                """
            ),
            {
                "id": uuid4(),
                "qualification_id": qualification_id,
                "from_status": from_status,
                "to_status": to_status,
                "role": role,
                "actor_id": actor_id,
                "owner_id": owner_id,
                "reason": f"runtime test {to_status}",
                "evidence_sha256": _sha_text(f"qualified-evidence:{id_prefix}"),
                "evidence_ids": [evidence_id],
            },
        )
    return qualification_id


def _reserve_runtime(
    connection: Connection,
    fixture: SendFixture,
    *,
    release_qualification_id: UUID,
    key: str,
) -> dict[str, object]:
    return dict(
        connection.execute(
            sa.text(
                """
                SELECT *
                FROM careerops.gmail_send_reserve_and_enqueue(
                    :owner_id, :account_id, :campaign_id, :grant_id, :authorization_id,
                    :action_intent_id, :payload_version_id, :payload_hash,
                    :recipient_sha256, :approval_request_id, :review_evidence_sha256,
                    :review_snapshot_sha256, :reviewed_by_user_id,
                    :release_qualification_id, :reservation_key, :reconciliation_key,
                    :event_key, :idempotency_key, :trace_id,
                    :subject_sha256, :body_sha256
                )
                """
            ),
            {
                "owner_id": fixture.owner_id,
                "account_id": fixture.account_id,
                "campaign_id": fixture.campaign_id,
                "grant_id": fixture.grant_id,
                "authorization_id": fixture.authorization_id,
                "action_intent_id": fixture.action_intent_id,
                "payload_version_id": fixture.payload_version_id,
                "payload_hash": fixture.payload_hash,
                "recipient_sha256": fixture.recipient_sha256,
                "approval_request_id": fixture.approval_request_id,
                "review_evidence_sha256": fixture.review_evidence_sha256,
                "review_snapshot_sha256": fixture.review_snapshot_sha256,
                "reviewed_by_user_id": fixture.owner_id,
                "release_qualification_id": release_qualification_id,
                "reservation_key": fixture.reservation_key,
                "reconciliation_key": fixture.reconciliation_key,
                "event_key": f"gmail-send:{fixture.reservation_key}",
                "idempotency_key": key,
                "trace_id": f"trace-{key}",
                "subject_sha256": fixture.subject_sha256,
                "body_sha256": fixture.body_sha256,
            },
        )
        .mappings()
        .one()
    )


def _seed_runtime_send_fixture(engine: Engine, *, id_prefix: str) -> RuntimeFixture:
    with engine.begin() as connection:
        owner_id, candidate_id = _seed_owner_candidate_pair(connection, id_prefix)
        payload = GmailSendPayload(
            sender=f"{id_prefix}-{uuid4().hex[:8]}@example.com",
            recipient=f"recruiter-{uuid4().hex[:8]}@example.com",
            subject=f"Reviewed application follow-up {id_prefix}",
            text_body="Hello, this reviewed email is approved for a bounded Gmail send.",
        )
        campaign_id = _seed_campaign(connection, owner_id=owner_id)
        grant_id = _seed_grant(
            connection,
            campaign_id=campaign_id,
            allowed_materials=(
                payload.payload_hash,
                payload.body_sha256,
                *(ref.sha256 for ref in payload.attachment_refs),
                payload.grant_material_hash,
            ),
        )
        account_id = _register_send_account(
            connection,
            owner_id=owner_id,
            candidate_id=candidate_id,
            subject=payload.sender,
            handle=f"vault://{id_prefix}/send/{uuid4().hex}",
            key=f"{id_prefix}-register-{uuid4()}",
        )["account_id"]
        draft = (
            connection.execute(
                sa.text(
                    """
                    SELECT *
                    FROM careerops.gmail_send_create_draft(
                        :owner_id, :candidate_id, :candidate_id,
                        CAST(:target AS jsonb), CAST(:payload_json AS jsonb),
                        '[]'::jsonb, NULL, 'gmail-send-runtime-test.v1',
                        :idempotency_key, :trace_id, 'runtime exact review',
                        :expires_at
                    )
                    """
                ),
                {
                    "owner_id": owner_id,
                    "candidate_id": candidate_id,
                    "target": _json(payload.canonical_target()),
                    "payload_json": _json(_reviewed_payload_json(payload)),
                    "idempotency_key": f"draft-{id_prefix}-{uuid4()}",
                    "trace_id": f"trace-draft-{id_prefix}",
                    "expires_at": NOW + timedelta(hours=1),
                },
            )
            .mappings()
            .one()
        )
        fixture = SendFixture(
            id_prefix=id_prefix,
            owner_id=owner_id,
            candidate_id=candidate_id,
            account_id=account_id,
            campaign_id=campaign_id,
            grant_id=grant_id,
            authorization_id=uuid4(),
            action_intent_id=draft["action_intent_id"],
            payload_version_id=draft["payload_version_id"],
            approval_request_id=draft["approval_request_id"],
            payload_hash=draft["payload_hash"],
            recipient_sha256=payload.recipient_sha256,
            subject_sha256=_sha_text(payload.subject),
            body_sha256=payload.body_sha256,
            review_evidence_sha256=_sha_text(f"review-evidence:{id_prefix}"),
            review_snapshot_sha256=_sha_text(f"review-snapshot:{id_prefix}"),
            release_qualification_id=uuid4(),
            release_evidence_hash=_sha_text(f"release-evidence:{id_prefix}"),
            release_evidence_expires_at=NOW + timedelta(hours=1),
            reservation_key=f"reservation-{id_prefix}-{uuid4().hex}",
            reconciliation_key=f"reconcile/{id_prefix}/{uuid4().hex}",
        )
        reviewed = (
            connection.execute(
                sa.text(_review_draft_sql()),
                _review_params(fixture, key=f"review-{id_prefix}-{uuid4()}"),
            )
            .mappings()
            .one()
        )
        fixture = SendFixture(
            id_prefix=fixture.id_prefix,
            owner_id=fixture.owner_id,
            candidate_id=fixture.candidate_id,
            account_id=fixture.account_id,
            campaign_id=fixture.campaign_id,
            grant_id=fixture.grant_id,
            authorization_id=reviewed["authorization_id"],
            action_intent_id=fixture.action_intent_id,
            payload_version_id=fixture.payload_version_id,
            approval_request_id=fixture.approval_request_id,
            payload_hash=fixture.payload_hash,
            recipient_sha256=fixture.recipient_sha256,
            subject_sha256=fixture.subject_sha256,
            body_sha256=fixture.body_sha256,
            review_evidence_sha256=fixture.review_evidence_sha256,
            review_snapshot_sha256=fixture.review_snapshot_sha256,
            release_qualification_id=fixture.release_qualification_id,
            release_evidence_hash=fixture.release_evidence_hash,
            release_evidence_expires_at=fixture.release_evidence_expires_at,
            reservation_key=fixture.reservation_key,
            reconciliation_key=fixture.reconciliation_key,
        )
        release_qualification_id = _seed_qualified_gmail_release(
            connection,
            owner_id=owner_id,
            account_id=account_id,
            id_prefix=id_prefix,
        )
        reserved = _reserve_runtime(
            connection,
            fixture,
            release_qualification_id=release_qualification_id,
            key=f"reserve-{id_prefix}-{uuid4()}",
        )
        return RuntimeFixture(
            owner_id=owner_id,
            candidate_id=candidate_id,
            account_id=account_id,
            action_intent_id=fixture.action_intent_id,
            payload_version_id=fixture.payload_version_id,
            outbox_event_id=cast(UUID, reserved["outbox_event_id"]),
            payload=payload,
        )


def _publisher(
    engine: Engine,
    send_client: _RecordingSendClient,
    *,
    credential_resolver: _CredentialResolver | None = None,
    sent_metadata_client: _SentMetadataClient | None = None,
    retry_delay: timedelta = timedelta(minutes=5),
    publisher_clock: Callable[[], datetime] | None = None,
) -> OutboxPublisher:
    clock = publisher_clock if publisher_clock is not None else (lambda: RECONCILED_AT)
    return OutboxPublisher(
        PostgresGmailSendOutboxStore(engine),
        GmailSendOutboxSink(
            PostgresGmailSendCoordinator(engine, owner=OWNER),
            credential_resolver or _CredentialResolver(),
            send_client,
            sent_metadata_client or _SentMetadataClient(),
            _AttachmentResolver(),
            clock=lambda: RECONCILED_AT,
        ),
        retry_delay=retry_delay,
        event_key_prefix=GMAIL_SEND_EVENT_KEY_PREFIX,
        clock=clock,
    )


class _Token:
    def __init__(self, account_subject: str) -> None:
        self.account_subject = account_subject


class _CredentialResolver:
    def resolve_send(self, dispatch: PreparedGmailSendDispatch) -> GmailSendAccessToken:
        return _Token(dispatch.account_subject)

    def resolve_readonly(self, dispatch: PreparedGmailSendDispatch) -> GmailSendAccessToken:
        return _Token(dispatch.account_subject)


class _FailOnceSendCredentialResolver(_CredentialResolver):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        self.failed = False

    def resolve_send(self, dispatch: PreparedGmailSendDispatch) -> GmailSendAccessToken:
        if not self.failed:
            self.failed = True
            raise GmailSendCredentialError(self.error_code)
        return super().resolve_send(dispatch)


class _RecordingSendClient:
    def __init__(self, *, raise_after_post: bool = False) -> None:
        self.raise_after_post = raise_after_post
        self.posts: list[str] = []

    def send_message(
        self,
        *,
        token: GmailSendAccessToken,
        dispatch: PreparedGmailSendDispatch,
        attachments: Sequence[GmailResolvedAttachment],
        now: datetime,
    ) -> GmailSendProviderReceipt:
        del token, attachments, now
        self.posts.append(dispatch.payload.message_id_header)
        if self.raise_after_post:
            raise RuntimeError("provider timed out after POST")
        return GmailSendProviderReceipt(
            provider_message_id=f"gmail-{dispatch.payload.payload_hash[:16]}",
            provider_thread_id=f"thread-{dispatch.payload.payload_hash[:16]}",
            rfc_message_id=dispatch.payload.message_id_header,
            provider_state=GmailSendProviderState.SENT_CONFIRMED,
            received_at=RECONCILED_AT,
        )


class _SentMetadataClient:
    def __init__(
        self,
        *,
        find_by_rfc_message_id: dict[str, GmailSentMetadata] | None = None,
    ) -> None:
        self.find_by_rfc_message_id = find_by_rfc_message_id or {}
        self.lookups: list[str] = []

    def get_sent_metadata(
        self,
        *,
        token: GmailSendAccessToken,
        dispatch: PreparedGmailSendDispatch,
        receipt: GmailSendProviderReceipt,
    ) -> GmailSentMetadata:
        del token
        return GmailSentMetadata(
            provider_message_id=receipt.provider_message_id,
            provider_thread_id=receipt.provider_thread_id,
            label_ids=("SENT",),
            headers={
                "From": dispatch.payload.sender,
                "To": dispatch.payload.recipient,
                "Subject": dispatch.payload.subject,
                "Message-ID": dispatch.payload.message_id_header,
            },
        )

    def find_sent_by_rfc_message_id(
        self,
        *,
        token: GmailSendAccessToken,
        job: GmailSendReconciliationJob,
    ) -> GmailSentMetadata | None:
        del token
        message_id = job.payload.message_id_header
        self.lookups.append(message_id)
        return self.find_by_rfc_message_id.get(message_id)


def _sent_metadata_for_payload(payload: GmailSendPayload) -> GmailSentMetadata:
    return GmailSentMetadata(
        provider_message_id=f"gmail-reconciled-{payload.payload_hash[:16]}",
        provider_thread_id=f"thread-reconciled-{payload.payload_hash[:16]}",
        label_ids=("SENT",),
        headers={
            "From": payload.sender,
            "To": payload.recipient,
            "Subject": payload.subject,
            "Message-ID": payload.message_id_header,
        },
    )


class _AttachmentResolver:
    def resolve(self, ref: object) -> GmailResolvedAttachment:
        raise AssertionError(f"runtime test fixture does not include attachments: {ref!r}")


def _reviewed_payload_json(payload: GmailSendPayload) -> dict[str, object]:
    data = dict(payload.canonical_without_hash())
    data["payload_hash"] = payload.payload_hash
    data["message_id_header"] = payload.message_id_header
    return data


def _event_status(connection: Connection, event_id: UUID) -> str:
    status = connection.scalar(
        sa.text("SELECT status FROM careerops.outbox_events WHERE id = :event_id"),
        {"event_id": event_id},
    )
    assert isinstance(status, str)
    return status


def _provider_receipt_count(connection: Connection, action_intent_id: UUID) -> int:
    return int(
        connection.scalar(
            sa.text(
                """
                SELECT count(*)
                FROM careerops.provider_receipts AS receipt
                JOIN careerops.side_effect_attempts AS attempt
                  ON attempt.id = receipt.side_effect_attempt_id
                WHERE attempt.action_intent_id = :action_intent_id
                """
            ),
            {"action_intent_id": action_intent_id},
        )
        or 0
    )


def _reconciliation_job_count(connection: Connection, outbox_event_id: UUID) -> int:
    return int(
        connection.scalar(
            sa.text(
                """
                SELECT count(*)
                FROM careerops.gmail_send_reconciliation_jobs
                WHERE outbox_event_id = :outbox_event_id
                """
            ),
            {"outbox_event_id": outbox_event_id},
        )
        or 0
    )


def _sqlstate(error: DBAPIError) -> str | None:
    original = getattr(error, "orig", None)
    return getattr(original, "pgcode", None) or getattr(original, "sqlstate", None)


def _sha_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()
