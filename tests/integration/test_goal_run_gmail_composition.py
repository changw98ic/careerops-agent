"""PostgreSQL integration proof for GoalRun Gmail composition."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from careerops.application.canonical_job_ingestion import (
    CanonicalJobDedupePolicy,
    CanonicalJobIngestionRecord,
    CrawlerRunProvenance,
    PublicAtsJobRow,
    sha256_json,
)
from careerops.infrastructure.database.canonical_job_ingestion import (
    PostgresCanonicalJobIngestionRepository,
)
from careerops.infrastructure.database.schema import (
    crawler_source_registries,
    crawler_source_registry_sources,
    crawler_source_runs,
)
from careerops.infrastructure.temporal.goal_run_repository import (
    PostgresGoalRunActivityRepository,
)
from careerops.workflows.goal_run_contracts import (
    GoalRunDomainCommand,
    GoalRunReviewRequestCommand,
    GoalRunStepOutcome,
)

pytestmark = pytest.mark.integration

_DISPOSABLE_DATABASE_PREFIX = "careerops_test_"
_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT = "CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE"
_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
_OUTPUT_MANIFEST_HASH = "a" * 64
_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


@dataclass(frozen=True, slots=True)
class GoalRunGmailFixture:
    actor_id: UUID
    candidate_id: UUID
    account_id: UUID
    campaign_id: UUID
    grant_id: UUID
    release_qualification_id: UUID
    goal_run_id: UUID
    fencing_token: UUID
    version: int
    source_row_id: UUID
    registry_id: UUID
    source_id: str
    run_id: UUID
    canonical_job_id: UUID
    job_posting_id: UUID
    job_posting_version_id: UUID
    target: dict[str, object]
    payload: dict[str, object]
    attachment_refs: list[dict[str, object]]
    payload_hash: str
    match_snapshot_sha256: str
    review_snapshot_sha256: str
    authorization_expires_at: datetime


@dataclass(frozen=True, slots=True)
class _ConnectionBoundEngine:
    connection: Connection

    def begin(self) -> nullcontext[Connection]:
        return nullcontext(self.connection)


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for GoalRun Gmail composition tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(_DISPOSABLE_DATABASE_PREFIX):
        pytest.skip(
            "GoalRun Gmail composition tests require a disposable database named "
            f"{_DISPOSABLE_DATABASE_PREFIX}*"
        )
    if os.environ.get(_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT) != "1":
        pytest.skip(f"{_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT}=1 is required")
    return value


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


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as database_connection:
        transaction = database_connection.begin()
        try:
            yield database_connection
        finally:
            transaction.rollback()


def test_record_match_requires_exact_reviewed_source_run_and_replays_one_composition(
    connection: Connection,
) -> None:
    fixture = _seed_goal_run_gmail_fixture(connection, prefix="record-match")

    first = _record_match(connection, fixture)
    replay = _record_match(connection, fixture)

    assert first["receipt_state"] == "created"
    assert first["state"] == "matched"
    assert replay["receipt_state"] == "replayed"
    assert replay["composition_id"] == first["composition_id"]
    assert (
        _count_where(
            connection,
            "careerops.goal_run_gmail_compositions",
            "goal_run_id = :goal_run_id",
            {"goal_run_id": fixture.goal_run_id},
        )
        == 1
    )
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(
            sa.text(
                """
                SELECT careerops.goal_run_record_gmail_match(
                    :actor_id, :goal_run_id, :source_row_id, :run_id,
                    :canonical_job_id, :job_posting_id, :job_posting_version_id,
                    0.91, '["forged"]'::jsonb, :match_snapshot_sha256
                )
                """
            ),
            {
                "actor_id": fixture.actor_id,
                "goal_run_id": fixture.goal_run_id,
                "source_row_id": uuid4(),
                "run_id": fixture.run_id,
                "canonical_job_id": fixture.canonical_job_id,
                "job_posting_id": fixture.job_posting_id,
                "job_posting_version_id": fixture.job_posting_version_id,
                "match_snapshot_sha256": fixture.match_snapshot_sha256,
            },
        )


def test_dynamic_source_goal_run_binds_the_claimed_source_provenance(
    connection: Connection,
) -> None:
    fixture = _seed_goal_run_gmail_fixture(
        connection,
        prefix="dynamic-source",
        pin_source=False,
    )
    assert (
        connection.scalar(
            sa.text("SELECT source_id FROM careerops.goal_runs WHERE id = :goal_run_id"),
            {"goal_run_id": fixture.goal_run_id},
        )
        is None
    )
    repository = PostgresGoalRunActivityRepository(cast(Engine, _ConnectionBoundEngine(connection)))
    matched = repository.match_jobs(
        GoalRunDomainCommand(
            goal_run_id=fixture.goal_run_id,
            owner_user_id=fixture.actor_id,
            registry_id=fixture.registry_id,
            source_id=fixture.source_id,
            source_row_id=fixture.source_row_id,
            crawler_run_id=fixture.run_id,
            max_records=100,
        )
    )

    assert matched.outcome is GoalRunStepOutcome.SUCCEEDED
    assert matched.reason_code == "GMAIL_MATCH_RECORDED"
    assert matched.details["canonical_job_id"] == str(fixture.canonical_job_id)


def test_prepare_creates_one_exact_gmail_payload_for_simple_review(connection: Connection) -> None:
    fixture = _seed_goal_run_gmail_fixture(connection, prefix="prepare")
    _record_match(connection, fixture)
    fixture = _checkpoint(connection, fixture, phase="draft_preparation")

    prepared = _prepare_gmail(connection, fixture)
    replay = _prepare_gmail(connection, fixture)

    assert prepared["receipt_state"] == "created"
    assert replay["receipt_state"] == "replayed"
    assert replay["action_intent_id"] == prepared["action_intent_id"]
    assert replay["payload_version_id"] == prepared["payload_version_id"]
    payload = _payload_version(connection, cast(UUID | str, prepared["payload_version_id"]))
    payload_json = cast(dict[str, object], payload["payload"])
    assert payload["payload_hash"] == fixture.payload_hash
    assert payload["target"] == fixture.target
    assert payload_json["subject"] == fixture.payload["subject"]
    intent = (
        connection.execute(
            sa.text(
                "SELECT resource_type, resource_id FROM careerops.action_intents "
                "WHERE id = :action_intent_id"
            ),
            {"action_intent_id": prepared["action_intent_id"]},
        )
        .mappings()
        .one()
    )
    assert intent["resource_type"] == "candidate"
    assert intent["resource_id"] == fixture.candidate_id
    assert (
        _count_where(
            connection,
            "careerops.approval_requests",
            "id = :approval_request_id AND requested_for = 'gmail_send_exact_payload'",
            {"approval_request_id": prepared["approval_request_id"]},
        )
        == 1
    )


def test_approved_goal_run_review_dispatches_once_to_gmail_outbox(connection: Connection) -> None:
    fixture = _prepared_fixture(connection, prefix="dispatch")
    fixture = _request_goal_run_review(connection, fixture)
    _approve_goal_run_review(connection, fixture)
    _seed_grant(connection, fixture, max_total=5)
    _seed_release_qualification(connection, fixture)

    first = _dispatch_gmail(connection, fixture)
    replay = _dispatch_gmail(connection, fixture)

    assert first["receipt_state"] == "created"
    assert replay["receipt_state"] == "replayed"
    assert replay["outbox_event_id"] == first["outbox_event_id"]
    assert (
        _count_where(
            connection,
            "careerops.outbox_events",
            "id = :outbox_event_id AND event_type = 'workflow_signal'",
            {"outbox_event_id": first["outbox_event_id"]},
        )
        == 1
    )
    assert (
        _count_where(
            connection,
            "careerops.autopilot_cap_reservations",
            "action_intent_id = :action_intent_id",
            {"action_intent_id": first["action_intent_id"]},
        )
        == 1
    )


def test_dispatch_rejects_review_whose_visible_email_diverges_from_exact_payload(
    connection: Connection,
) -> None:
    fixture = _prepared_fixture(connection, prefix="tampered-review")
    composition = _composition(connection, fixture)
    review_payload = _goal_run_review_payload(fixture, composition)
    visible_email = cast(dict[str, object], review_payload["email"])
    visible_email["subject"] = "A different subject than the reviewed Gmail payload"
    fixture = _request_goal_run_review(
        connection,
        fixture,
        review_payload=review_payload,
    )
    _approve_goal_run_review(connection, fixture)
    _seed_grant(connection, fixture, max_total=5)
    _seed_release_qualification(connection, fixture)

    assert _raises_db_error(
        connection,
        "SELECT careerops.goal_run_dispatch_gmail(:actor_id, :goal_run_id)",
        {"actor_id": fixture.actor_id, "goal_run_id": fixture.goal_run_id},
    )
    assert (
        _count_where(
            connection,
            "careerops.outbox_events",
            "action_intent_id = :action_intent_id",
            {"action_intent_id": composition["action_intent_id"]},
        )
        == 0
    )


def test_rejected_goal_run_review_cannot_dispatch(connection: Connection) -> None:
    fixture = _prepared_fixture(connection, prefix="reject")
    fixture = _request_goal_run_review(connection, fixture)
    _reject_goal_run_review(connection, fixture)
    _seed_grant(connection, fixture, max_total=5)
    _seed_release_qualification(connection, fixture)

    assert _raises_db_error(
        connection,
        "SELECT careerops.goal_run_dispatch_gmail(:actor_id, :goal_run_id)",
        {"actor_id": fixture.actor_id, "goal_run_id": fixture.goal_run_id},
    )
    assert (
        _count_where(
            connection,
            "careerops.outbox_events",
            "action_intent_id = :action_intent_id",
            {"action_intent_id": _composition(connection, fixture)["action_intent_id"]},
        )
        == 0
    )


@pytest.mark.parametrize(
    ("prefix", "breaker"),
    (
        ("revoked-grant", "grant_revoked"),
        ("provider-kill", "provider_kill_switch"),
        ("exhausted-cap", "exhausted_cap"),
    ),
)
def test_dispatch_stops_before_outbox_when_safety_boundary_blocks(
    connection: Connection,
    prefix: str,
    breaker: str,
) -> None:
    fixture = _prepared_fixture(connection, prefix=prefix)
    fixture = _request_goal_run_review(connection, fixture)
    _approve_goal_run_review(connection, fixture)
    _seed_grant(connection, fixture, max_total=1 if breaker == "exhausted_cap" else 5)
    _seed_release_qualification(connection, fixture)
    if breaker == "grant_revoked":
        _revoke_grant(connection, fixture)
    if breaker == "provider_kill_switch":
        _set_kill_switch(connection, fixture)
    if breaker == "exhausted_cap":
        _seed_existing_reservation(connection, fixture)

    assert _raises_db_error(
        connection,
        "SELECT careerops.goal_run_dispatch_gmail(:actor_id, :goal_run_id)",
        {"actor_id": fixture.actor_id, "goal_run_id": fixture.goal_run_id},
    )
    assert (
        _count_where(
            connection,
            "careerops.outbox_events",
            "action_intent_id = :action_intent_id",
            {"action_intent_id": _composition(connection, fixture)["action_intent_id"]},
        )
        == 0
    )


def test_inspect_reports_pending_confirmed_and_reconciliation_states(
    connection: Connection,
) -> None:
    fixture = _prepared_fixture(connection, prefix="inspect")
    fixture = _request_goal_run_review(connection, fixture)
    _approve_goal_run_review(connection, fixture)
    _seed_grant(connection, fixture, max_total=5)
    _seed_release_qualification(connection, fixture)
    dispatched = _dispatch_gmail(connection, fixture)

    pending = _inspect_gmail(connection, fixture)
    assert pending["state"] == "dispatch_enqueued"
    assert pending["outbox_status"] == "pending"
    assert pending["provider_state"] is None
    assert pending["reconciliation_status"] is None

    _insert_side_effect_attempt(connection, dispatched, ordinal=1, state="failed")
    attempt_id = _insert_side_effect_attempt(connection, dispatched, ordinal=2, state="succeeded")
    _insert_provider_receipt(connection, fixture, attempt_id)
    confirmed = _inspect_gmail(connection, fixture)
    assert confirmed["provider_state"] == "sent_confirmed"
    assert confirmed["provider_message_id"] == "gmail-message-inspect"
    assert confirmed["provider_thread_id"] == "gmail-thread-inspect"

    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.gmail_send_reconciliation_jobs (
                outbox_event_id, status, available_at, last_error_code
            )
            VALUES (:outbox_event_id, 'queued', CURRENT_TIMESTAMP, 'AMBIGUOUS_RECEIPT')
            """
        ),
        {"outbox_event_id": dispatched["outbox_event_id"]},
    )
    reconciliation = _inspect_gmail(connection, fixture)
    assert reconciliation["reconciliation_status"] == "queued"
    assert reconciliation["reconciliation_last_error_code"] == "AMBIGUOUS_RECEIPT"


def test_activity_repository_composes_reviewed_crawler_job_and_reconciles_gmail_receipt(
    connection: Connection,
) -> None:
    fixture = _seed_goal_run_gmail_fixture(connection, prefix="activity-full-chain")
    repository = PostgresGoalRunActivityRepository(cast(Engine, _ConnectionBoundEngine(connection)))
    domain = GoalRunDomainCommand(
        goal_run_id=fixture.goal_run_id,
        owner_user_id=fixture.actor_id,
        registry_id=fixture.registry_id,
        source_id=fixture.source_id,
        source_row_id=fixture.source_row_id,
        crawler_run_id=fixture.run_id,
        max_records=100,
    )

    matched = repository.match_jobs(domain)
    assert matched.outcome is GoalRunStepOutcome.SUCCEEDED
    assert matched.reason_code == "GMAIL_MATCH_RECORDED"
    assert matched.details["canonical_job_id"] == str(fixture.canonical_job_id)
    assert matched.details["receipt_state"] == "created"

    fixture = _checkpoint(connection, fixture, phase="draft_preparation")
    prepared = repository.prepare_drafts(domain)
    assert prepared.outcome is GoalRunStepOutcome.SUCCEEDED
    assert prepared.reason_code == "GMAIL_DRAFT_PREPARED"
    assert prepared.output_sha256 is not None
    for identity_key in (
        "composition_id",
        "action_intent_id",
        "payload_version_id",
        "approval_request_id",
        "payload_hash",
    ):
        assert prepared.details[identity_key]

    review = repository.request_review(
        GoalRunReviewRequestCommand(
            goal_run_id=fixture.goal_run_id,
            owner_user_id=fixture.actor_id,
            expected_version=fixture.version,
            fencing_token=fixture.fencing_token,
            review_kind="goal_run_application_review.v1",
            review_payload=prepared.details,
            snapshot_sha256=prepared.output_sha256,
            idempotency_key=f"activity-review-{uuid4()}",
            trace_id=f"trace-activity-review-{uuid4().hex[:12]}",
        )
    )
    fixture = replace(
        fixture,
        version=review.version,
        review_snapshot_sha256=review.snapshot_sha256,
    )
    _approve_goal_run_review(connection, fixture)

    composition = _composition(connection, fixture)
    persisted_payload = _payload_version(
        connection,
        cast(UUID | str, composition["payload_version_id"]),
    )
    fixture = replace(
        fixture,
        target=cast(dict[str, object], persisted_payload["target"]),
        payload=cast(dict[str, object], persisted_payload["payload"]),
        attachment_refs=cast(list[dict[str, object]], persisted_payload["attachment_refs"]),
        payload_hash=cast(str, persisted_payload["payload_hash"]),
    )
    _seed_grant(connection, fixture, max_total=5)
    _seed_release_qualification(connection, fixture)

    dispatched = repository.inspect_dispatch(domain)
    assert dispatched.outcome is GoalRunStepOutcome.SUCCEEDED
    assert dispatched.reason_code == "GMAIL_DISPATCH_ENQUEUED"
    assert dispatched.details["provider_io_performed"] is False
    assert dispatched.details["outbox_event_id"]
    assert (
        _count_where(
            connection,
            "careerops.outbox_events",
            "id = :outbox_event_id AND event_type = 'workflow_signal'",
            {"outbox_event_id": dispatched.details["outbox_event_id"]},
        )
        == 1
    )

    pending = repository.inspect_reconciliation(domain)
    assert pending.outcome is GoalRunStepOutcome.PENDING_EXTERNAL
    assert pending.reason_code == "GMAIL_SEND_RECEIPT_PENDING"

    attempt_id = _insert_side_effect_attempt(
        connection,
        cast(dict[str, object], dispatched.details),
    )
    _insert_provider_receipt(connection, fixture, attempt_id)
    confirmed = repository.inspect_reconciliation(domain)
    assert confirmed.outcome is GoalRunStepOutcome.SUCCEEDED
    assert confirmed.reason_code == "GMAIL_SEND_CONFIRMED"
    assert confirmed.details["provider_state"] == "sent_confirmed"


def test_workflow_role_only_receives_goal_run_gmail_wrappers(engine: Engine) -> None:
    if not _role_exists(engine, "careerops_workflow"):
        pytest.skip("careerops_workflow role is not installed")

    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_workflow"))
        for signature in (
            "careerops.goal_run_record_gmail_match("
            "uuid, uuid, uuid, uuid, uuid, uuid, uuid, numeric, jsonb, text)",
            "careerops.goal_run_prepare_gmail(uuid, uuid, jsonb, jsonb, jsonb, text)",
            "careerops.goal_run_dispatch_gmail(uuid, uuid)",
            "careerops.goal_run_inspect_gmail(uuid, uuid)",
        ):
            assert connection.scalar(
                sa.text("SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"),
                {"signature": signature},
            )
        assert not connection.scalar(
            sa.text(
                "SELECT has_table_privilege(current_user, "
                "'careerops.goal_run_gmail_compositions', 'INSERT')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_function_privilege(current_user, "
                "'careerops.gmail_send_reserve_and_enqueue("
                "uuid, uuid, uuid, uuid, uuid, uuid, uuid, text, text, uuid, "
                "text, text, uuid, uuid, text, text, text, text, text, text, text)', "
                "'EXECUTE')"
            )
        )


def _seed_goal_run_gmail_fixture(
    connection: Connection,
    *,
    prefix: str,
    pin_source: bool = True,
) -> GoalRunGmailFixture:
    actor_id, candidate_id = _seed_owner_candidate_pair(connection, prefix)
    account_id = _register_send_account(connection, actor_id, candidate_id, prefix)
    campaign_id = _seed_campaign(connection, actor_id, prefix)
    grant_id = uuid4()
    release_qualification_id = uuid4()
    source_row_id, registry_id, source_id, run_id, run_event_id = _succeeded_crawler_run(connection)
    ingested = PostgresCanonicalJobIngestionRepository(connection).ingest_public_ats_job(
        _canonical_record(source_row_id, registry_id, source_id, run_id, run_event_id, prefix)
    )
    target, payload, attachment_refs = _gmail_payload(prefix)
    payload_hash = _postgres_payload_hash(connection, target, payload, attachment_refs)
    authorization_expires_at = _future(hours=2)
    created = _create_goal_run(
        connection,
        actor_id=actor_id,
        registry_id=registry_id,
        source_id=source_id if pin_source else None,
        context={
            "match_config": {
                "include_keywords": ["agent", "platform", "engineer"],
                "exclude_keywords": [],
                "min_score": 1.0,
                "max_applications": 1,
            },
            "gmail_dispatch": {
                "account_id": str(account_id),
                "candidate_id": str(candidate_id),
                "sender": f"{prefix}@example.com",
                "recipient": payload["recipient"],
                "subject": payload["subject"],
                "text_body": payload["text_body"],
                "attachment_refs": attachment_refs,
                "campaign_id": str(campaign_id),
                "grant_version_id": str(grant_id),
                "release_qualification_id": str(release_qualification_id),
                "authorization_expires_at": authorization_expires_at.isoformat(),
                "review_evidence_sha256": _sha(f"review-evidence:{prefix}"),
                "review_reason": "GoalRun simple review approved exact Gmail payload",
                "ruleset_version": "goal-run-gmail.v1",
                "decision_rule_reference": "goal-run-simple-review",
                "draft_expires_at": _future(hours=1).isoformat(),
            },
        },
    )
    fixture = GoalRunGmailFixture(
        actor_id=actor_id,
        candidate_id=candidate_id,
        account_id=account_id,
        campaign_id=campaign_id,
        grant_id=grant_id,
        release_qualification_id=release_qualification_id,
        goal_run_id=cast(UUID, created["goal_run_id"]),
        fencing_token=cast(UUID, created["fencing_token"]),
        version=cast(int, created["version"]),
        source_row_id=source_row_id,
        registry_id=registry_id,
        source_id=source_id,
        run_id=run_id,
        canonical_job_id=ingested.canonical_job_id,
        job_posting_id=ingested.posting_id,
        job_posting_version_id=ingested.version_id,
        target=target,
        payload=payload,
        attachment_refs=attachment_refs,
        payload_hash=payload_hash,
        match_snapshot_sha256=_sha(f"match:{prefix}"),
        review_snapshot_sha256=payload_hash,
        authorization_expires_at=authorization_expires_at,
    )
    for phase in (
        "selecting_source",
        "claiming_source",
        "discovery",
        "complete_source",
        "canonical_ingest",
        "matching",
    ):
        fixture = _checkpoint(connection, fixture, phase=phase)
    return fixture


def _prepared_fixture(connection: Connection, *, prefix: str) -> GoalRunGmailFixture:
    fixture = _seed_goal_run_gmail_fixture(connection, prefix=prefix)
    _record_match(connection, fixture)
    fixture = _checkpoint(connection, fixture, phase="draft_preparation")
    _prepare_gmail(connection, fixture)
    return fixture


def _create_goal_run(
    connection: Connection,
    *,
    actor_id: UUID,
    registry_id: UUID,
    source_id: str | None,
    context: dict[str, object],
) -> dict[str, object]:
    result = connection.scalar(
        sa.text(
            """
            SELECT careerops.goal_run_create(
                :actor_id, :key, 'job-search', 'Find one reviewed Gmail job application.',
                CAST(:context AS jsonb), :registry_id, :source_id, 1,
                :workflow_id, :trace_id
            )
            """
        ),
        {
            "actor_id": actor_id,
            "key": f"goal-create-{uuid4()}",
            "context": _json(context),
            "registry_id": registry_id,
            "source_id": source_id,
            "workflow_id": f"goal-workflow-{uuid4().hex[:12]}",
            "trace_id": f"trace-goal-{uuid4().hex[:12]}",
        },
    )
    assert isinstance(result, dict)
    record = result["record"]
    assert isinstance(record, dict)
    return record


def _checkpoint(
    connection: Connection,
    fixture: GoalRunGmailFixture,
    *,
    phase: str,
) -> GoalRunGmailFixture:
    checkpoint = connection.scalar(
        sa.text(
            """
            SELECT careerops.goal_run_checkpoint(
                :actor_id, :goal_run_id, :version, :fencing_token,
                :phase, 'running', 'progress', '{}'::jsonb,
                NULL, :key, :trace_id
            )
            """
        ),
        {
            "actor_id": fixture.actor_id,
            "goal_run_id": fixture.goal_run_id,
            "version": fixture.version,
            "fencing_token": fixture.fencing_token,
            "phase": phase,
            "key": f"goal-checkpoint-{phase}-{uuid4()}",
            "trace_id": f"trace-checkpoint-{phase}",
        },
    )
    assert isinstance(checkpoint, dict)
    record = checkpoint["record"]
    assert isinstance(record, dict)
    return replace(fixture, version=cast(int, record["version"]))


def _record_match(connection: Connection, fixture: GoalRunGmailFixture) -> dict[str, object]:
    result = connection.scalar(
        sa.text(
            """
            SELECT careerops.goal_run_record_gmail_match(
                :actor_id, :goal_run_id, :source_row_id, :run_id,
                :canonical_job_id, :job_posting_id, :job_posting_version_id,
                0.91, '["title_keyword", "remote"]'::jsonb, :match_snapshot_sha256
            )
            """
        ),
        {
            "actor_id": fixture.actor_id,
            "goal_run_id": fixture.goal_run_id,
            "source_row_id": fixture.source_row_id,
            "run_id": fixture.run_id,
            "canonical_job_id": fixture.canonical_job_id,
            "job_posting_id": fixture.job_posting_id,
            "job_posting_version_id": fixture.job_posting_version_id,
            "match_snapshot_sha256": fixture.match_snapshot_sha256,
        },
    )
    assert isinstance(result, dict)
    return result


def _prepare_gmail(connection: Connection, fixture: GoalRunGmailFixture) -> dict[str, object]:
    result = connection.scalar(
        sa.text(
            """
            SELECT careerops.goal_run_prepare_gmail(
                :actor_id, :goal_run_id, CAST(:target AS jsonb), CAST(:payload AS jsonb),
                CAST(:attachment_refs AS jsonb), :payload_hash
            )
            """
        ),
        {
            "actor_id": fixture.actor_id,
            "goal_run_id": fixture.goal_run_id,
            "target": _json(fixture.target),
            "payload": _json(fixture.payload),
            "attachment_refs": _json(fixture.attachment_refs),
            "payload_hash": fixture.payload_hash,
        },
    )
    assert isinstance(result, dict)
    return result


def _request_goal_run_review(
    connection: Connection,
    fixture: GoalRunGmailFixture,
    *,
    review_payload: dict[str, object] | None = None,
) -> GoalRunGmailFixture:
    composition = _composition(connection, fixture)
    payload = review_payload or _goal_run_review_payload(fixture, composition)
    review_snapshot_sha256 = _sha(_json(payload))
    review = connection.scalar(
        sa.text(
            """
            SELECT careerops.goal_run_request_review(
                :actor_id, :goal_run_id, :version, :fencing_token,
                :review_item_id, :snapshot, 'goal_run_application_review.v1',
                CAST(:payload AS jsonb), :key, :trace_id
            )
            """
        ),
        {
            "actor_id": fixture.actor_id,
            "goal_run_id": fixture.goal_run_id,
            "version": fixture.version,
            "fencing_token": fixture.fencing_token,
            "review_item_id": uuid4(),
            "snapshot": review_snapshot_sha256,
            "payload": _json(payload),
            "key": f"goal-review-request-{uuid4()}",
            "trace_id": f"trace-goal-review-{uuid4().hex[:12]}",
        },
    )
    assert isinstance(review, dict)
    record = review["record"]
    assert isinstance(record, dict)
    return replace(
        fixture,
        version=cast(int, record["version"]),
        review_snapshot_sha256=review_snapshot_sha256,
    )


def _goal_run_review_payload(
    fixture: GoalRunGmailFixture,
    composition: dict[str, object],
) -> dict[str, object]:
    return {
        "version": "goal_run_application_review.v1",
        "review_kind": "goal_run_application_review.v1",
        "composition_id": str(composition["id"]),
        "action_intent_id": str(composition["action_intent_id"]),
        "payload_version_id": str(composition["payload_version_id"]),
        "approval_request_id": str(composition["approval_request_id"]),
        "payload_hash": composition["payload_hash"],
        "selected_application_id": str(fixture.canonical_job_id),
        "channel": "gmail:send",
        "email": {
            "recipient": fixture.payload["recipient"],
            "subject": fixture.payload["subject"],
            "text_body": fixture.payload["text_body"],
        },
        "attachments": [
            {
                "filename": attachment["filename"],
                "content_type": attachment["content_type"],
                "size_bytes": attachment["size_bytes"],
                "sha256": attachment["sha256"],
            }
            for attachment in fixture.attachment_refs
        ],
        "exact_payload": {
            "action_payload_hash": composition["payload_hash"],
            "recipient_sha256": composition["recipient_snapshot_sha256"],
            "subject_sha256": composition["subject_sha256"],
            "body_sha256": composition["body_sha256"],
            "attachment_manifest_sha256": composition["attachment_manifest_sha256"],
        },
        "snapshots": {
            "match_snapshot_sha256": composition["match_snapshot_sha256"],
        },
        "safety": {
            "contains_credentials": False,
            "external_io_performed": False,
            "human_review_required": True,
            "model_output_is_not_execution_authority": True,
        },
        "human_actions": {"approve": "批准", "reject": "拒绝"},
    }


def _approve_goal_run_review(connection: Connection, fixture: GoalRunGmailFixture) -> None:
    _record_goal_run_review_decision(connection, fixture, "approve")


def _reject_goal_run_review(connection: Connection, fixture: GoalRunGmailFixture) -> None:
    _record_goal_run_review_decision(connection, fixture, "reject")


def _record_goal_run_review_decision(
    connection: Connection,
    fixture: GoalRunGmailFixture,
    decision: str,
) -> None:
    review_item_id = connection.scalar(
        sa.text("SELECT review_item_id FROM careerops.goal_runs WHERE id = :goal_run_id"),
        {"goal_run_id": fixture.goal_run_id},
    )
    assert isinstance(review_item_id, UUID)
    result = connection.scalar(
        sa.text(
            """
            SELECT careerops.goal_run_record_review_decision(
                :actor_id, :goal_run_id, :version, :fencing_token,
                :review_item_id, :snapshot, :decision, :reason, :key, :trace_id
            )
            """
        ),
        {
            "actor_id": fixture.actor_id,
            "goal_run_id": fixture.goal_run_id,
            "version": fixture.version,
            "fencing_token": fixture.fencing_token,
            "review_item_id": review_item_id,
            "snapshot": fixture.review_snapshot_sha256,
            "decision": decision,
            "reason": f"human simple review {decision}",
            "key": f"goal-review-decision-{decision}-{uuid4()}",
            "trace_id": f"trace-goal-decision-{uuid4().hex[:12]}",
        },
    )
    assert isinstance(result, dict)


def _dispatch_gmail(connection: Connection, fixture: GoalRunGmailFixture) -> dict[str, object]:
    result = connection.scalar(
        sa.text("SELECT careerops.goal_run_dispatch_gmail(:actor_id, :goal_run_id)"),
        {"actor_id": fixture.actor_id, "goal_run_id": fixture.goal_run_id},
    )
    assert isinstance(result, dict)
    return result


def _inspect_gmail(connection: Connection, fixture: GoalRunGmailFixture) -> dict[str, object]:
    result = connection.scalar(
        sa.text("SELECT careerops.goal_run_inspect_gmail(:actor_id, :goal_run_id)"),
        {"actor_id": fixture.actor_id, "goal_run_id": fixture.goal_run_id},
    )
    assert isinstance(result, dict)
    return result


def _payload_version(connection: Connection, payload_version_id: UUID | str) -> dict[str, object]:
    row = (
        connection.execute(
            sa.text(
                """
                SELECT target, payload, attachment_refs, payload_hash
                FROM careerops.action_payload_versions
                WHERE id = :payload_version_id
                """
            ),
            {"payload_version_id": payload_version_id},
        )
        .mappings()
        .one()
    )
    return dict(row)


def _composition(connection: Connection, fixture: GoalRunGmailFixture) -> dict[str, object]:
    row = (
        connection.execute(
            sa.text(
                """
                SELECT *
                FROM careerops.goal_run_gmail_compositions
                WHERE goal_run_id = :goal_run_id
                """
            ),
            {"goal_run_id": fixture.goal_run_id},
        )
        .mappings()
        .one()
    )
    return dict(row)


def _seed_owner_candidate_pair(connection: Connection, prefix: str) -> tuple[UUID, UUID]:
    actor_id = uuid4()
    candidate_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.console_users (
                id, username, password_hash, password_algorithm,
                password_parameters, password_changed_at
            )
            VALUES (
                :actor_id, :username, '$argon2id$goal-run-gmail-test',
                'argon2id', '{}'::jsonb, :now
            )
            """
        ),
        {"actor_id": actor_id, "username": f"{prefix}-{actor_id.hex}", "now": _NOW},
    )
    connection.execute(
        sa.text("INSERT INTO careerops.candidates (id, display_name) VALUES (:id, :name)"),
        {"id": candidate_id, "name": f"Candidate {prefix}"},
    )
    return actor_id, candidate_id


def _register_send_account(
    connection: Connection,
    actor_id: UUID,
    candidate_id: UUID,
    prefix: str,
) -> UUID:
    readonly_id = connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_readonly_register_account(
                :owner_id, :candidate_id, :handle, :subject, 'testing',
                :credential_evidence, :key, :trace
            )
            """
        ),
        {
            "owner_id": actor_id,
            "candidate_id": candidate_id,
            "handle": f"vault://{prefix}/readonly/{uuid4().hex}",
            "subject": f"{prefix}@example.com",
            "credential_evidence": _sha(f"readonly:{prefix}"),
            "key": f"readonly-{prefix}-{uuid4()}",
            "trace": f"trace-readonly-{prefix}",
        },
    )
    assert isinstance(readonly_id, UUID)
    account_id = connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_send_register_account(
                :owner_id, :candidate_id, :handle, :subject, 'testing',
                :credential_evidence, :release_evidence, 50,
                :readonly_id, :key, :trace
            )
            """
        ),
        {
            "owner_id": actor_id,
            "candidate_id": candidate_id,
            "handle": f"vault://{prefix}/send/{uuid4().hex}",
            "subject": f"{prefix}@example.com",
            "credential_evidence": _sha(f"send:{prefix}"),
            "release_evidence": _sha(f"send-release:{prefix}"),
            "readonly_id": readonly_id,
            "key": f"send-{prefix}-{uuid4()}",
            "trace": f"trace-send-{prefix}",
        },
    )
    assert isinstance(account_id, UUID)
    return account_id


def _seed_campaign(connection: Connection, actor_id: UUID, prefix: str) -> UUID:
    campaign_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_campaigns (
                id, owner_user_id, name, objective, criteria, exclusions, created_by
            )
            VALUES (
                :campaign_id, :owner_id, :name, 'send reviewed GoalRun Gmail applications',
                '{}'::jsonb, '[]'::jsonb, :created_by
            )
            """
        ),
        {
            "campaign_id": campaign_id,
            "owner_id": actor_id,
            "name": f"GoalRun Gmail {prefix}",
            "created_by": f"user:{actor_id}",
        },
    )
    return campaign_id


def _seed_grant(
    connection: Connection,
    fixture: GoalRunGmailFixture,
    *,
    max_total: int,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_grant_versions (
                id, campaign_id, version, subject_actor, allowed_action_kinds,
                allowed_channels, allowed_target_hosts, material_hashes,
                max_total_submissions, max_daily_submissions, max_per_company,
                policy_ruleset_version, release_version, expires_at
            )
            VALUES (
                :grant_id, :campaign_id, 1, :subject_actor,
                '["send_email"]'::jsonb, '["gmail:send"]'::jsonb,
                '["gmail.googleapis.com"]'::jsonb, CAST(:material_hashes AS jsonb),
                :max_total, 10, 10,
                'gmail-send-policy.v1', 'gmail-send-release.v1', :expires_at
            )
            """
        ),
        {
            "grant_id": fixture.grant_id,
            "campaign_id": fixture.campaign_id,
            "subject_actor": str(fixture.actor_id),
            "material_hashes": _json(
                [
                    fixture.payload_hash,
                    fixture.target["body_sha256"],
                    fixture.target["grant_material_hash"],
                    fixture.attachment_refs[0]["sha256"],
                ]
            ),
            "max_total": max_total,
            "expires_at": _future(days=1),
        },
    )


def _seed_release_qualification(connection: Connection, fixture: GoalRunGmailFixture) -> None:
    evidence_id = uuid4()
    release_evidence_hash = _sha(f"qualified-evidence:{fixture.goal_run_id}")
    credential_ref_hash = connection.scalar(
        sa.text(
            """
            SELECT credential_store_evidence_sha256
            FROM careerops.gmail_send_accounts
            WHERE id = :account_id
            """
        ),
        {"account_id": fixture.account_id},
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
            "id": fixture.release_qualification_id,
            "implementation_hash": _sha(f"implementation:{fixture.goal_run_id}"),
            "config_hash": _sha(f"config:{fixture.goal_run_id}"),
            "policy_hash": _sha(f"policy:{fixture.goal_run_id}"),
            "dataset_hash": _sha(f"dataset:{fixture.goal_run_id}"),
            "git_commit": "a" * 40,
            "oauth_scope_hash": _sha(_GMAIL_SEND_SCOPE),
            "credential_ref_hash": credential_ref_hash,
            "network_policy_hash": _sha(f"network:{fixture.goal_run_id}"),
            "reconcile_policy_hash": _sha(f"reconcile-policy:{fixture.goal_run_id}"),
            "hard_stop_hash": _sha(f"hard-stop:{fixture.goal_run_id}"),
            "sensitive_field_hash": _sha(f"sensitive:{fixture.goal_run_id}"),
            "kill_switch_hash": _sha(f"kill-switch:{fixture.goal_run_id}"),
            "fixture_manifest_sha256": _sha(f"fixture:{fixture.goal_run_id}"),
            "fault_manifest_sha256": _sha(f"fault:{fixture.goal_run_id}"),
            "owner_id": fixture.actor_id,
            "created_by": "goal-run-gmail-test",
            "expires_at": _future(hours=1),
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
                :run_id, :owner_id, 'goal-run-gmail-runner', CAST(:metrics AS jsonb),
                :sample_manifest_sha256
            )
            """
        ),
        {
            "id": evidence_id,
            "qualification_id": fixture.release_qualification_id,
            "artifact_uri": f"datasets/goal-run-gmail/{fixture.goal_run_id}/metrics.json",
            "artifact_sha256": _sha(f"artifact:{fixture.goal_run_id}"),
            "run_id": f"goal-run-gmail-{uuid4()}",
            "owner_id": fixture.actor_id,
            "metrics": _json(
                {
                    "total_observations": 50,
                    "external_provider_calls": 0,
                    "false_negative": 0,
                    "autonomous_provider_write_attempts": 0,
                    "no_autonomous_writes": True,
                }
            ),
            "sample_manifest_sha256": _sha(f"sample:{fixture.goal_run_id}"),
        },
    )
    for from_status, to_status, role in (
        ("draft", "evaluating", "runner"),
        ("evaluating", "pending_independent_review", "reviewer"),
        ("pending_independent_review", "qualified", "operator"),
    ):
        evidence_ids = [] if to_status == "evaluating" else [evidence_id]
        decided_by_user_id = fixture.actor_id if role == "operator" else None
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
                "qualification_id": fixture.release_qualification_id,
                "from_status": from_status,
                "to_status": to_status,
                "role": role,
                "actor_id": f"goal-run-gmail-{role}",
                "owner_id": decided_by_user_id,
                "reason": f"test {to_status}",
                "evidence_sha256": release_evidence_hash,
                "evidence_ids": evidence_ids,
            },
        )


def _revoke_grant(connection: Connection, fixture: GoalRunGmailFixture) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_grant_revocations (
                id, grant_version_id, revoked_by_user_id, reason
            )
            VALUES (:id, :grant_id, :actor_id, 'test revoked before GoalRun dispatch')
            """
        ),
        {"id": uuid4(), "grant_id": fixture.grant_id, "actor_id": fixture.actor_id},
    )


def _set_kill_switch(connection: Connection, fixture: GoalRunGmailFixture) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_kill_switch_events (
                id, scope_type, provider, active, reason,
                actor_user_id, idempotency_key, trace_id
            )
            VALUES (
                :id, 'provider', 'gmail', true, 'test GoalRun Gmail kill switch',
                :actor_id, :key, :trace_id
            )
            """
        ),
        {
            "id": uuid4(),
            "actor_id": fixture.actor_id,
            "key": f"goal-run-gmail-kill-{uuid4()}",
            "trace_id": f"trace-goal-run-gmail-kill-{uuid4().hex[:12]}",
        },
    )


def _seed_existing_reservation(connection: Connection, fixture: GoalRunGmailFixture) -> None:
    composition = _composition(connection, fixture)
    policy_decision_id = uuid4()
    authorization_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.policy_decisions (
                id, action_intent_id, payload_version_id, ruleset_version,
                decision, reason_codes, payload_hash, expires_at
            )
            VALUES (
                :policy_decision_id, :action_intent_id, :payload_version_id,
                'gmail-send-policy.v1', 'allow_autopilot_submission',
                '[]'::jsonb, :payload_hash, CURRENT_TIMESTAMP + interval '1 hour'
            )
            """
        ),
        {
            "policy_decision_id": policy_decision_id,
            "action_intent_id": composition["action_intent_id"],
            "payload_version_id": composition["payload_version_id"],
            "payload_hash": fixture.payload_hash,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_intent_authorizations (
                id, policy_decision_id, campaign_id, grant_version_id,
                action_intent_id, payload_version_id, payload_hash,
                authorization_outcome, reason_codes, authorized_at, expires_at
            )
            VALUES (
                :authorization_id, :policy_decision_id, :campaign_id, :grant_id,
                :action_intent_id, :payload_version_id, :payload_hash,
                'allow_autopilot_submission', '[]'::jsonb, CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP + interval '1 hour'
            )
            """
        ),
        {
            "authorization_id": authorization_id,
            "policy_decision_id": policy_decision_id,
            "campaign_id": fixture.campaign_id,
            "grant_id": fixture.grant_id,
            "action_intent_id": composition["action_intent_id"],
            "payload_version_id": composition["payload_version_id"],
            "payload_hash": fixture.payload_hash,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_cap_reservations (
                id, campaign_id, grant_version_id, authorization_id,
                action_intent_id, payload_version_id, payload_hash,
                target_host, channel, release_version,
                company_key, adapter_id, fixture_id, reservation_key,
                reconciliation_key, release_evidence_hash, release_evidence_expires_at
            )
            VALUES (
                :reservation_id, :campaign_id, :grant_id, :authorization_id,
                :action_intent_id, :payload_version_id, :payload_hash,
                'gmail.googleapis.com', 'gmail:send', 'gmail-send-release.v1',
                :company_key, 'gmail', 'gmail-send.v1', :reservation_key,
                :reconciliation_key, :release_evidence_hash,
                CURRENT_TIMESTAMP + interval '1 hour'
            )
            """
        ),
        {
            "reservation_id": uuid4(),
            "campaign_id": fixture.campaign_id,
            "grant_id": fixture.grant_id,
            "authorization_id": authorization_id,
            "action_intent_id": composition["action_intent_id"],
            "payload_version_id": composition["payload_version_id"],
            "payload_hash": fixture.payload_hash,
            "company_key": fixture.target["recipient_sha256"],
            "reservation_key": f"preexisting-{uuid4()}",
            "reconciliation_key": f"preexisting/reconcile/{uuid4()}",
            "release_evidence_hash": _sha(f"release-preexisting:{fixture.goal_run_id}"),
        },
    )


def _insert_side_effect_attempt(
    connection: Connection,
    dispatched: dict[str, object],
    *,
    ordinal: int = 1,
    state: str = "succeeded",
) -> UUID:
    attempt_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.side_effect_attempts (
                id, action_intent_id, outbox_event_id, ordinal, state,
                request_fingerprint, started_at, finished_at
            )
            VALUES (
                :attempt_id, :action_intent_id, :outbox_event_id, :ordinal, :state,
                :request_fingerprint, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "attempt_id": attempt_id,
            "action_intent_id": dispatched["action_intent_id"],
            "outbox_event_id": dispatched["outbox_event_id"],
            "ordinal": ordinal,
            "state": state,
            "request_fingerprint": _sha(
                f"attempt:{dispatched['outbox_event_id']}:{ordinal}:{state}"
            ),
        },
    )
    return attempt_id


def _insert_provider_receipt(
    connection: Connection,
    fixture: GoalRunGmailFixture,
    attempt_id: UUID,
) -> None:
    composition = _composition(connection, fixture)
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.provider_receipts (
                id, side_effect_attempt_id, provider, provider_resource_id,
                reconciliation_key, final_state, provider_timestamp, received_at,
                receipt_metadata
            )
            VALUES (
                :id, :attempt_id, 'gmail', 'gmail-message-inspect',
                :reconciliation_key, 'confirmed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                '{"provider_thread_id":"gmail-thread-inspect","provider_state":"sent_confirmed"}'::jsonb
            )
            """
        ),
        {
            "id": uuid4(),
            "attempt_id": attempt_id,
            "reconciliation_key": composition["reconciliation_key"],
        },
    )


def _succeeded_crawler_run(connection: Connection) -> tuple[UUID, UUID, str, UUID, UUID]:
    registry_id = uuid4()
    source_id = f"public-ats-{uuid4().hex[:8]}"
    connection.execute(
        sa.insert(crawler_source_registries).values(
            id=registry_id,
            kind="configured-recruitment-crawlers",
            manifest_path="config/job-sources/public-ats.json",
            manifest_sha256=sha256_json({"registry_id": str(registry_id)}),
            registry_sha256=sha256_json({"sources": [str(registry_id)]}),
            source_count=1,
            created_by="goal-run-gmail-test",
            generated_at=_NOW,
            manifest_json={"sources": [source_id]},
        )
    )
    source_row_id = uuid4()
    connection.execute(
        sa.insert(crawler_source_registry_sources).values(
            id=source_row_id,
            registry_id=registry_id,
            source_id=source_id,
            adapter="recruitment.public_ats_feed",
            enabled=True,
            input_artifact="datasets/recruitment/sources/public_ats_seed.json",
            output_dir="datasets/recruitment/discovered",
            dependency_source_ids=[],
            dependency_artifacts=[],
            command_sha256=sha256_json({"command": source_id}),
            source_sha256=sha256_json({"source": source_id}),
            source_json={"source_id": source_id},
            cursor=None,
            cadence_seconds=3600,
            retry_rounds=2,
            budget_json={
                "timeout_seconds": 30,
                "max_feed_bytes": 10_000_000,
                "retry_rounds": 2,
            },
            rate_limit_json={"concurrency": 1},
            robots_terms_policy="respect",
            canonical_ingestion_policy="canonical_job_ingestion",
            provenance_policy="preserve",
            dedupe_policy="hybrid",
            next_run_at=_NOW - timedelta(days=1),
        )
    )
    lease_token = uuid4()
    claim = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.claim_crawler_source("
                ":registry_id, :source_id, :worker_id, :lease_token, 300)"
            ),
            {
                "registry_id": registry_id,
                "source_id": source_id,
                "worker_id": "goal-run-gmail-test",
                "lease_token": lease_token,
            },
        )
        .mappings()
        .one()
    )
    connection.execute(
        sa.text(
            "SELECT * FROM careerops.complete_crawler_source_run("
            ":source_row_id, :worker_id, :lease_token, 'cursor-after', 'succeeded', :manifest_hash)"
        ),
        {
            "source_row_id": source_row_id,
            "worker_id": "goal-run-gmail-test",
            "lease_token": lease_token,
            "manifest_hash": _OUTPUT_MANIFEST_HASH,
        },
    )
    event_id = connection.scalar(
        sa.select(crawler_source_runs.c.event_id).where(
            crawler_source_runs.c.run_id == claim["run_id"],
            crawler_source_runs.c.event_kind == "succeeded",
        )
    )
    assert isinstance(event_id, UUID)
    return source_row_id, registry_id, source_id, claim["run_id"], event_id


def _canonical_record(
    source_row_id: UUID,
    registry_id: UUID,
    source_id: str,
    run_id: UUID,
    run_event_id: UUID,
    prefix: str,
) -> CanonicalJobIngestionRecord:
    row = PublicAtsJobRow(
        company_name=f"{prefix}.example",
        company_domain=f"{prefix}.example",
        source_type="public_ats",
        source_identifier="public-ats-feed",
        base_url="https://jobs.example.com/",
        external_id=f"job-{prefix}",
        canonical_url=f"https://jobs.example.com/jobs/{prefix}",
        title="Agent Platform Engineer",
        location="Remote",
        department="Engineering",
        captured_at=_NOW,
        parser_version="public-ats-jsonl:v1",
        source_url=f"https://jobs.example.com/jobs/{prefix}",
        structured_data=MappingProxyType({"source_record_id": f"job-{prefix}"}),
        content_hash=None,
    )
    return CanonicalJobIngestionRecord(
        provenance=CrawlerRunProvenance(
            crawler_source_row_id=source_row_id,
            crawler_run_id=run_id,
            run_event_id=run_event_id,
            registry_id=registry_id,
            source_id=source_id,
            adapter="recruitment.public_ats_feed",
            output_artifact_sha256=_OUTPUT_MANIFEST_HASH,
        ),
        row=row,
        dedupe_policy=CanonicalJobDedupePolicy.HYBRID,
    )


def _gmail_payload(
    prefix: str,
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    recipient = f"recruiter-{prefix}@example.com"
    recipient_sha256 = _sha(recipient.lower().strip())
    subject = f"Application for Agent Platform Engineer ({prefix})"
    text_body = f"Hello, I would like to apply for the Agent Platform Engineer role. [{prefix}]"
    body_sha256 = _sha(text_body)
    target: dict[str, object] = {
        "target_host": "gmail.googleapis.com",
        "channel": "gmail:send",
        "adapter_id": "gmail",
        "fixture_id": "gmail-send.v1",
        "recipient_sha256": recipient_sha256,
        "subject_sha256": _sha(subject),
        "body_sha256": body_sha256,
        "grant_material_hash": _sha(f"grant-material:{prefix}"),
    }
    payload: dict[str, object] = {
        "to": [recipient],
        "recipient": recipient,
        "recipient_sha256": recipient_sha256,
        "subject": subject,
        "subject_sha256": target["subject_sha256"],
        "text_body": text_body,
        "body_sha256": body_sha256,
    }
    attachment_refs = [
        {
            "object_key": f"resume/{prefix}.pdf",
            "filename": "resume.pdf",
            "content_type": "application/pdf",
            "size_bytes": 10,
            "sha256": _sha(f"attachment:{prefix}"),
        }
    ]
    return target, payload, attachment_refs


def _postgres_payload_hash(
    connection: Connection,
    target: dict[str, object],
    payload: dict[str, object],
    attachment_refs: list[dict[str, object]],
) -> str:
    value = connection.scalar(
        sa.text(
            """
            SELECT encode(
                sha256(convert_to(jsonb_build_object(
                    'target', CAST(:target AS jsonb),
                    'payload', CAST(:payload AS jsonb),
                    'attachment_refs', CAST(:attachment_refs AS jsonb)
                )::text, 'UTF8')),
                'hex'
            )
            """
        ),
        {
            "target": _json(target),
            "payload": _json(payload),
            "attachment_refs": _json(attachment_refs),
        },
    )
    assert isinstance(value, str)
    return value


def _role_exists(engine: Engine, role: str) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                {"role": role},
            )
        )


def _raises_db_error(
    connection: Connection,
    statement: str,
    params: dict[str, object],
) -> bool:
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(sa.text(statement), params)
    return True


def _count_where(
    connection: Connection,
    table: str,
    predicate: str,
    params: dict[str, object],
) -> int:
    return int(
        connection.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE {predicate}"), params) or 0
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _future(**kwargs: float) -> datetime:
    return datetime.now(UTC) + timedelta(**kwargs)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
