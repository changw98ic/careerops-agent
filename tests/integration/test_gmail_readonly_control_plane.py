# ruff: noqa: E501

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
DISPOSABLE_PREFIX = "careerops_test_"


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for Gmail readonly DB tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(DISPOSABLE_PREFIX):
        pytest.skip("Gmail readonly DB tests require a disposable careerops_test_* database")
    if os.environ.get("CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE") != "1":
        pytest.skip("CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE=1 is required")
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


def test_gmail_readonly_owner_scope_exact_scope_and_mailbox_lease_flow(
    connection: Connection,
) -> None:
    owner_id = uuid4()
    candidate_id = uuid4()
    _seed_owner_candidate(connection, owner_id=owner_id, candidate_id=candidate_id)

    account_id = connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_readonly_register_account(
                :owner_id, :candidate_id, 'vault://gmail-readonly-primary',
                'recruiting@example.com', 'testing', :credential_evidence,
                'register-1', 'trace-1'
            )
            """
        ),
        {
            "owner_id": owner_id,
            "candidate_id": candidate_id,
            "credential_evidence": "a" * 64,
        },
    )
    assert isinstance(account_id, UUID)
    status = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.gmail_readonly_account_status(:owner_id, :account_id)"
            ),
            {"owner_id": owner_id, "account_id": account_id},
        )
        .mappings()
        .one()
    )
    assert status["account_id"] == account_id
    assert status["owner_user_id"] == owner_id
    assert status["status"] == "active"
    assert isinstance(status["snapshot_sha256"], str)
    assert len(status["snapshot_sha256"]) == 64
    assert connection.scalar(
        sa.text(
            """
                SELECT granted_scopes
                FROM careerops.oauth_credential_references
                WHERE account_subject = 'recruiting@example.com'
                """
        )
    ) == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert _raises_db_error(
        connection,
        """
        INSERT INTO careerops.oauth_credential_references (
            id, provider, account_subject, secret_handle, granted_scopes, status, issued_at
        )
        VALUES (
            :credential_id, 'gmail', 'metadata@example.com', 'vault://metadata',
            '["https://www.googleapis.com/auth/gmail.metadata"]'::jsonb, 'active', :now
        );
        SELECT careerops.gmail_readonly_register_account(
            :owner_id, :candidate_id, 'vault://metadata', 'metadata@example.com',
            'testing', :credential_evidence, 'register-metadata', 'trace-metadata'
        );
        """,
        {
            "credential_id": uuid4(),
            "owner_id": owner_id,
            "candidate_id": candidate_id,
            "credential_evidence": "b" * 64,
            "now": NOW,
        },
    )

    assert _raises_db_error(
        connection,
        """
        INSERT INTO careerops.oauth_credential_references (
            id, provider, account_subject, secret_handle, granted_scopes, status, issued_at
        )
        VALUES (
            :credential_id, 'gmail', 'send@example.com', 'vault://send',
            '["https://www.googleapis.com/auth/gmail.readonly","https://www.googleapis.com/auth/gmail.send"]'::jsonb,
            'active', :now
        );
        SELECT careerops.gmail_readonly_register_account(
            :owner_id, :candidate_id, 'vault://send', 'send@example.com',
            'testing', :credential_evidence, 'register-send', 'trace-send'
        );
        """,
        {
            "credential_id": uuid4(),
            "owner_id": owner_id,
            "candidate_id": candidate_id,
            "credential_evidence": "c" * 64,
            "now": NOW,
        },
    )

    run_id = _request_sync(connection, owner_id=owner_id, account_id=account_id, key="sync-1")
    assert isinstance(run_id, UUID)
    assert _raises_db_error(
        connection,
        "SELECT careerops.gmail_readonly_request_sync(:other_owner, :account_id, 'manual', 'sync-cross', 'trace-cross')",
        {"other_owner": uuid4(), "account_id": account_id},
    )

    claimed = (
        connection.execute(
            sa.text("SELECT * FROM careerops.gmail_readonly_claim_sync_runs('mailbox-it', 300, 10)")
        )
        .mappings()
        .one()
    )
    assert claimed["sync_run_id"] == run_id
    assert claimed["credential_handle"] == "vault://gmail-readonly-primary"
    lease_token = claimed["lease_token"]
    assert isinstance(lease_token, UUID)
    assert _raises_db_error(
        connection,
        """
        SELECT careerops.gmail_readonly_complete_sync_run(
            :run_id, :wrong_lease, '101', NULL, 0
        )
        """,
        {"run_id": run_id, "wrong_lease": uuid4()},
    )

    first_signal_id = _record_signal(
        connection,
        run_id=run_id,
        lease_token=lease_token,
        message_id="msg-1",
        history_id="100",
        signal_hash="1" * 64,
        proposal_hash="2" * 64,
    )
    replay_signal_id = _record_signal(
        connection,
        run_id=run_id,
        lease_token=lease_token,
        message_id="msg-1",
        history_id="100",
        signal_hash="1" * 64,
        proposal_hash="2" * 64,
    )
    assert first_signal_id == replay_signal_id
    with pytest.raises(DBAPIError) as signal_replay_error, connection.begin_nested():
        _record_signal(
            connection,
            run_id=run_id,
            lease_token=lease_token,
            message_id="msg-1",
            history_id="101",
            signal_hash="3" * 64,
            proposal_hash="4" * 64,
        )
    assert _sqlstate(signal_replay_error.value) == "23505"
    with pytest.raises(DBAPIError) as no_proposal_replay_error, connection.begin_nested():
        _record_signal(
            connection,
            run_id=run_id,
            lease_token=lease_token,
            message_id="msg-1",
            history_id="100",
            signal_hash="1" * 64,
            proposal_hash=None,
            proposal_payload=None,
        )
    assert _sqlstate(no_proposal_replay_error.value) == "23505"
    with pytest.raises(DBAPIError) as proposal_payload_replay_error, connection.begin_nested():
        _record_signal(
            connection,
            run_id=run_id,
            lease_token=lease_token,
            message_id="msg-1",
            history_id="100",
            signal_hash="1" * 64,
            proposal_hash="2" * 64,
            proposal_payload='{"proposal_kind":"review_only","review_priority":"high"}',
        )
    assert _sqlstate(proposal_payload_replay_error.value) == "23505"
    assert (
        connection.scalar(
            sa.text(
                """
                SELECT count(*)
                FROM careerops.gmail_message_signals
                WHERE gmail_account_id = :account_id AND provider_message_id = 'msg-1'
                """
            ),
            {"account_id": account_id},
        )
        == 1
    )
    connection.execute(
        sa.text(
            "SELECT careerops.gmail_readonly_complete_sync_run(:run_id, :lease_token, '101', NULL, 1)"
        ),
        {"run_id": run_id, "lease_token": lease_token},
    )
    assert (
        connection.scalar(
            sa.text("SELECT last_history_id FROM careerops.gmail_accounts WHERE id = :account_id"),
            {"account_id": account_id},
        )
        == "101"
    )

    snapshot_sha = _account_snapshot_sha(connection, account_id=account_id)
    reset_run_id = connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_readonly_reset_history(
                :owner_id, :account_id, 'history expired', 'reset-1',
                'trace-reset', :snapshot_sha
            )
            """
        ),
        {
            "owner_id": owner_id,
            "account_id": account_id,
            "snapshot_sha": snapshot_sha,
        },
    )
    assert isinstance(reset_run_id, UUID)
    assert (
        connection.scalar(
            sa.text(
                """
                SELECT careerops.gmail_readonly_reset_history(
                    :owner_id, :account_id, 'history expired', 'reset-1',
                    'trace-reset-replay', :snapshot_sha
                )
                """
            ),
            {"owner_id": owner_id, "account_id": account_id, "snapshot_sha": snapshot_sha},
        )
        == reset_run_id
    )
    assert _raises_db_error(
        connection,
        """
        SELECT careerops.gmail_readonly_reset_history(
            :owner_id, :account_id, 'different reason', 'reset-1',
            'trace-reset-collision', :snapshot_sha
        )
        """,
        {"owner_id": owner_id, "account_id": account_id, "snapshot_sha": snapshot_sha},
    )
    account = (
        connection.execute(
            sa.text(
                """
                SELECT status, last_history_id, next_page_token
                FROM careerops.gmail_accounts
                WHERE id = :account_id
                """
            ),
            {"account_id": account_id},
        )
        .mappings()
        .one()
    )
    assert account == {
        "status": "sync_required",
        "last_history_id": None,
        "next_page_token": None,
    }

    proposal = (
        connection.execute(
            sa.text(
                """
                SELECT id, payload_sha256
                FROM careerops.gmail_signal_proposals
                WHERE gmail_account_id = :account_id
                """
            ),
            {"account_id": account_id},
        )
        .mappings()
        .one()
    )
    proposals = (
        connection.execute(
            sa.text(
                """
                SELECT *
                FROM careerops.gmail_readonly_list_proposals(:owner_id, :account_id, 10)
                """
            ),
            {"owner_id": owner_id, "account_id": account_id},
        )
        .mappings()
        .all()
    )
    assert len(proposals) == 1
    assert proposals[0]["proposal_id"] == proposal["id"]
    assert proposals[0]["owner_user_id"] == owner_id
    assert proposals[0]["payload_sha256"] == proposal["payload_sha256"]
    canonical_job_id, job_posting_id = _seed_job(connection)
    decision_id = connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_readonly_review_proposal(
                :owner_id, :proposal_id, 'approve', :canonical_job_id, :job_posting_id,
                'looks right', :payload_sha, 'review-1', 'trace-review'
            )
            """
        ),
        {
            "owner_id": owner_id,
            "proposal_id": proposal["id"],
            "canonical_job_id": canonical_job_id,
            "job_posting_id": job_posting_id,
            "payload_sha": proposal["payload_sha256"],
        },
    )
    assert isinstance(decision_id, UUID)
    assert (
        connection.scalar(
            sa.text(
                """
                SELECT careerops.gmail_readonly_review_proposal(
                    :owner_id, :proposal_id, 'approve', :canonical_job_id, :job_posting_id,
                    'looks right', :payload_sha, 'review-1', 'trace-review-replay'
                )
                """
            ),
            {
                "owner_id": owner_id,
                "proposal_id": proposal["id"],
                "canonical_job_id": canonical_job_id,
                "job_posting_id": job_posting_id,
                "payload_sha": proposal["payload_sha256"],
            },
        )
        == decision_id
    )
    with pytest.raises(DBAPIError) as review_replay_error, connection.begin_nested():
        connection.execute(
            sa.text(
                """
                SELECT careerops.gmail_readonly_review_proposal(
                    :owner_id, :proposal_id, 'approve', :canonical_job_id, :job_posting_id,
                    'changed reason', :payload_sha, 'review-1', 'trace-review-changed'
                )
                """
            ),
            {
                "owner_id": owner_id,
                "proposal_id": proposal["id"],
                "canonical_job_id": canonical_job_id,
                "job_posting_id": job_posting_id,
                "payload_sha": proposal["payload_sha256"],
            },
        )
    assert _sqlstate(review_replay_error.value) == "23505"
    assert _raises_db_error(
        connection,
        """
        SELECT careerops.gmail_readonly_review_proposal(
            :other_owner, :proposal_id, 'reject', NULL, NULL,
            'not mine', :payload_sha, 'review-cross', 'trace-cross'
        )
        """,
        {
            "other_owner": uuid4(),
            "proposal_id": proposal["id"],
            "payload_sha": proposal["payload_sha256"],
        },
    )
    assert _raises_db_error(
        connection,
        "INSERT INTO careerops.outbox_events (id, event_key, action_intent_id, payload_version_id, event_type, status, available_at) VALUES (:id, 'gmail-forbidden', :id, :id, 'workflow_signal', 'pending', :now)",
        {"id": uuid4(), "now": NOW},
        role="careerops_mailbox",
    )

    connection.execute(
        sa.text(
            """
            SELECT careerops.gmail_readonly_revoke_account(
                :owner_id, :account_id, 'operator requested', 'revoke-1', 'trace-revoke'
            )
            """
        ),
        {"owner_id": owner_id, "account_id": account_id},
    )
    connection.execute(
        sa.text(
            """
            SELECT careerops.gmail_readonly_revoke_account(
                :owner_id, :account_id, 'operator requested', 'revoke-1', 'trace-revoke-replay'
            )
            """
        ),
        {"owner_id": owner_id, "account_id": account_id},
    )
    assert _raises_db_error(
        connection,
        """
        SELECT careerops.gmail_readonly_revoke_account(
            :owner_id, :account_id, 'different reason', 'revoke-1', 'trace-revoke-collision'
        )
        """,
        {"owner_id": owner_id, "account_id": account_id},
    )
    revoked = (
        connection.execute(
            sa.text(
                """
                SELECT account.status AS account_status, credential.status AS credential_status
                FROM careerops.gmail_accounts AS account
                JOIN careerops.oauth_credential_references AS credential
                  ON credential.id = account.oauth_credential_reference_id
                WHERE account.id = :account_id
                """
            ),
            {"account_id": account_id},
        )
        .mappings()
        .one()
    )
    assert revoked == {"account_status": "revoked", "credential_status": "revoked"}


def test_gmail_readonly_page_checkpoint_and_history_expiry_are_fenced(
    connection: Connection,
) -> None:
    owner_id = uuid4()
    candidate_id = uuid4()
    _seed_owner_candidate(connection, owner_id=owner_id, candidate_id=candidate_id)
    account_id = _register_account(
        connection,
        owner_id=owner_id,
        candidate_id=candidate_id,
        handle="vault://gmail-page-checkpoint",
        subject="page-checkpoint@example.com",
        key="register-page-checkpoint",
    )
    run_id = _request_sync(
        connection,
        owner_id=owner_id,
        account_id=account_id,
        key="sync-page-checkpoint",
    )
    assert isinstance(run_id, UUID)

    first_claim = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.gmail_readonly_claim_sync_runs('mailbox-page', 300, 1)"
            )
        )
        .mappings()
        .one()
    )
    connection.execute(
        sa.text(
            """
            SELECT careerops.gmail_readonly_defer_sync_run(
                :run_id, :lease_token, '250', 'next-page-2', 3, 'GMAIL_PAGE_INCOMPLETE'
            )
            """
        ),
        {"run_id": run_id, "lease_token": first_claim["lease_token"]},
    )
    checkpoint = (
        connection.execute(
            sa.text(
                """
                SELECT account.last_history_id, account.next_page_token,
                       run.history_end_id, run.next_page_token AS run_next_page_token,
                       run.status
                FROM careerops.gmail_accounts AS account
                JOIN careerops.gmail_sync_runs AS run ON run.gmail_account_id = account.id
                WHERE run.id = :run_id
                """
            ),
            {"run_id": run_id},
        )
        .mappings()
        .one()
    )
    assert checkpoint == {
        "last_history_id": None,
        "next_page_token": "next-page-2",
        "history_end_id": "250",
        "run_next_page_token": "next-page-2",
        "status": "queued",
    }

    second_claim = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.gmail_readonly_claim_sync_runs('mailbox-page', 300, 1)"
            )
        )
        .mappings()
        .one()
    )
    assert second_claim["anchor_history_id"] == "250"
    assert second_claim["run_next_page_token"] == "next-page-2"
    connection.execute(
        sa.text(
            "UPDATE careerops.gmail_sync_runs SET lease_until = clock_timestamp() - interval '1 second' WHERE id = :run_id"
        ),
        {"run_id": run_id},
    )
    assert _raises_db_error(
        connection,
        "SELECT careerops.gmail_readonly_complete_sync_run(:run_id, :lease_token, '250', NULL, 0)",
        {"run_id": run_id, "lease_token": second_claim["lease_token"]},
    )

    connection.execute(
        sa.text(
            "UPDATE careerops.gmail_sync_runs SET lease_until = clock_timestamp() + interval '5 minutes' WHERE id = :run_id"
        ),
        {"run_id": run_id},
    )
    connection.execute(
        sa.text(
            "SELECT careerops.gmail_readonly_fail_sync_run(:run_id, :lease_token, 'GMAIL_HISTORY_EXPIRED')"
        ),
        {"run_id": run_id, "lease_token": second_claim["lease_token"]},
    )
    failed = (
        connection.execute(
            sa.text(
                """
                SELECT account.status AS account_status, account.last_history_id,
                       run.status AS run_status, run.lease_token, run.lease_until
                FROM careerops.gmail_accounts AS account
                JOIN careerops.gmail_sync_runs AS run ON run.gmail_account_id = account.id
                WHERE run.id = :run_id
                """
            ),
            {"run_id": run_id},
        )
        .mappings()
        .one()
    )
    assert failed == {
        "account_status": "sync_required",
        "last_history_id": None,
        "run_status": "failed",
        "lease_token": None,
        "lease_until": None,
    }


def test_concurrent_readonly_signal_exact_replay_returns_one_signal(engine: Engine) -> None:
    _clear_engine_test_data(engine)
    release_first_commit = Event()
    try:
        with engine.begin() as connection:
            owner_id, candidate_id = _seed_or_reuse_owner_candidate(
                connection, display_name="Gmail Concurrent Signal Test"
            )
            account_id = _register_account(
                connection,
                owner_id=owner_id,
                candidate_id=candidate_id,
                handle=f"vault://gmail-concurrent-signal/{uuid4().hex}",
                subject=f"concurrent-signal-{uuid4().hex}@example.com",
                key=f"register-concurrent-signal-{uuid4()}",
            )
            run_id = _request_sync(
                connection,
                owner_id=owner_id,
                account_id=account_id,
                key=f"sync-concurrent-signal-{uuid4()}",
            )
            assert isinstance(run_id, UUID)
            claim = (
                connection.execute(
                    sa.text(
                        "SELECT * FROM careerops.gmail_readonly_claim_sync_runs('mailbox-concurrent-signal', 300, 1)"
                    )
                )
                .mappings()
                .one()
            )
            lease_token = claim["lease_token"]
            assert isinstance(lease_token, UUID)

        first_call_completed = Event()
        second_call_started = Event()
        first_backend_pid: list[int] = []
        second_backend_pid: list[int] = []

        def record_and_hold_commit() -> UUID | None:
            with engine.begin() as thread_connection:
                backend_pid = thread_connection.scalar(sa.text("SELECT pg_backend_pid()"))
                assert isinstance(backend_pid, int)
                first_backend_pid.append(backend_pid)
                signal_id = _record_signal(
                    thread_connection,
                    run_id=run_id,
                    lease_token=lease_token,
                    message_id="msg-concurrent-signal",
                    history_id="200",
                    signal_hash="1" * 64,
                    proposal_hash="2" * 64,
                )
                first_call_completed.set()
                assert release_first_commit.wait(timeout=30)
                return signal_id

        def record_while_first_is_uncommitted() -> UUID | None:
            with engine.begin() as thread_connection:
                backend_pid = thread_connection.scalar(sa.text("SELECT pg_backend_pid()"))
                assert isinstance(backend_pid, int)
                second_backend_pid.append(backend_pid)
                second_call_started.set()
                return _record_signal(
                    thread_connection,
                    run_id=run_id,
                    lease_token=lease_token,
                    message_id="msg-concurrent-signal",
                    history_id="200",
                    signal_hash="1" * 64,
                    proposal_hash="2" * 64,
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(record_and_hold_commit)
            try:
                assert first_call_completed.wait(timeout=10)
                second_future = executor.submit(record_while_first_is_uncommitted)
                assert second_call_started.wait(timeout=10)
                _assert_backend_is_blocked_by(
                    engine,
                    blocked_backend_pid=second_backend_pid[0],
                    blocker_backend_pid=first_backend_pid[0],
                )
            finally:
                release_first_commit.set()
            first_signal_id = first_future.result(timeout=20)
            second_signal_id = second_future.result(timeout=20)

        assert isinstance(first_signal_id, UUID)
        assert isinstance(second_signal_id, UUID)
        with engine.connect() as connection:
            persisted_signal_ids = list(
                connection.scalars(
                    sa.text(
                        """
                        SELECT id
                        FROM careerops.gmail_message_signals
                        WHERE gmail_account_id = :account_id
                          AND provider_message_id = 'msg-concurrent-signal'
                        """
                    ),
                    {"account_id": account_id},
                )
            )
        assert persisted_signal_ids == [first_signal_id]
        assert second_signal_id == persisted_signal_ids[0]
    finally:
        release_first_commit.set()
        _clear_engine_test_data(engine)


def test_concurrent_readonly_review_exact_replay_returns_one_decision(engine: Engine) -> None:
    _clear_engine_test_data(engine)
    release_first_commit = Event()
    try:
        with engine.begin() as connection:
            owner_id, candidate_id = _seed_or_reuse_owner_candidate(
                connection, display_name="Gmail Concurrent Review Test"
            )
            account_id = _register_account(
                connection,
                owner_id=owner_id,
                candidate_id=candidate_id,
                handle=f"vault://gmail-concurrent-review/{uuid4().hex}",
                subject=f"concurrent-review-{uuid4().hex}@example.com",
                key=f"register-concurrent-review-{uuid4()}",
            )
            run_id = _request_sync(
                connection,
                owner_id=owner_id,
                account_id=account_id,
                key=f"sync-concurrent-review-{uuid4()}",
            )
            assert isinstance(run_id, UUID)
            claim = (
                connection.execute(
                    sa.text(
                        "SELECT * FROM careerops.gmail_readonly_claim_sync_runs('mailbox-concurrent-review', 300, 1)"
                    )
                )
                .mappings()
                .one()
            )
            proposal_signal_id = _record_signal(
                connection,
                run_id=run_id,
                lease_token=claim["lease_token"],
                message_id="msg-concurrent-review",
                history_id="201",
                signal_hash="3" * 64,
                proposal_hash="4" * 64,
            )
            assert isinstance(proposal_signal_id, UUID)
            proposal = (
                connection.execute(
                    sa.text(
                        """
                        SELECT id, payload_sha256
                        FROM careerops.gmail_signal_proposals
                        WHERE gmail_account_id = :account_id
                        """
                    ),
                    {"account_id": account_id},
                )
                .mappings()
                .one()
            )
            canonical_job_id, job_posting_id = _seed_job(connection)

        review_sql = """
            SELECT careerops.gmail_readonly_review_proposal(
                :owner_id, :proposal_id, 'approve', :canonical_job_id, :job_posting_id,
                'concurrent replay reason', :payload_sha, :idempotency_key, :trace_id
            )
        """
        params = {
            "owner_id": owner_id,
            "proposal_id": proposal["id"],
            "canonical_job_id": canonical_job_id,
            "job_posting_id": job_posting_id,
            "payload_sha": proposal["payload_sha256"],
            "idempotency_key": f"review-concurrent-{uuid4()}",
        }
        first_call_completed = Event()
        second_call_started = Event()
        first_backend_pid: list[int] = []
        second_backend_pid: list[int] = []

        def review_and_hold_commit() -> UUID | None:
            with engine.begin() as thread_connection:
                backend_pid = thread_connection.scalar(sa.text("SELECT pg_backend_pid()"))
                assert isinstance(backend_pid, int)
                first_backend_pid.append(backend_pid)
                decision_id = thread_connection.scalar(
                    sa.text(review_sql),
                    params | {"trace_id": "trace-review-concurrent-first"},
                )
                first_call_completed.set()
                assert release_first_commit.wait(timeout=30)
                return decision_id

        def review_while_first_is_uncommitted() -> UUID | None:
            with engine.begin() as thread_connection:
                backend_pid = thread_connection.scalar(sa.text("SELECT pg_backend_pid()"))
                assert isinstance(backend_pid, int)
                second_backend_pid.append(backend_pid)
                second_call_started.set()
                return thread_connection.scalar(
                    sa.text(review_sql),
                    params | {"trace_id": "trace-review-concurrent-second"},
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(review_and_hold_commit)
            try:
                assert first_call_completed.wait(timeout=10)
                second_future = executor.submit(review_while_first_is_uncommitted)
                assert second_call_started.wait(timeout=10)
                _assert_backend_is_blocked_by(
                    engine,
                    blocked_backend_pid=second_backend_pid[0],
                    blocker_backend_pid=first_backend_pid[0],
                )
            finally:
                release_first_commit.set()
            first_decision_id = first_future.result(timeout=20)
            second_decision_id = second_future.result(timeout=20)

        assert isinstance(first_decision_id, UUID)
        assert isinstance(second_decision_id, UUID)
        with engine.connect() as connection:
            persisted_decision_ids = list(
                connection.scalars(
                    sa.text(
                        """
                        SELECT id
                        FROM careerops.gmail_signal_review_decisions
                        WHERE proposal_id = :proposal_id
                        """
                    ),
                    {"proposal_id": proposal["id"]},
                )
            )
        assert persisted_decision_ids == [first_decision_id]
        assert second_decision_id == persisted_decision_ids[0]
    finally:
        release_first_commit.set()
        _clear_engine_test_data(engine)


def test_gmail_readonly_expired_worker_leases_stop_after_five_attempts(
    connection: Connection,
) -> None:
    owner_id = uuid4()
    candidate_id = uuid4()
    _seed_owner_candidate(connection, owner_id=owner_id, candidate_id=candidate_id)
    account_id = _register_account(
        connection,
        owner_id=owner_id,
        candidate_id=candidate_id,
        handle="vault://gmail-lease-cap",
        subject="lease-cap@example.com",
        key="register-lease-cap",
    )
    run_id = _request_sync(
        connection,
        owner_id=owner_id,
        account_id=account_id,
        key="sync-lease-cap",
    )
    assert isinstance(run_id, UUID)

    for expected_attempt in range(1, 6):
        claim = (
            connection.execute(
                sa.text(
                    "SELECT * FROM careerops.gmail_readonly_claim_sync_runs('mailbox-crash-loop', 300, 1)"
                )
            )
            .mappings()
            .one()
        )
        assert claim["sync_run_id"] == run_id
        assert claim["attempt_count"] == expected_attempt
        connection.execute(
            sa.text(
                "UPDATE careerops.gmail_sync_runs SET lease_until = clock_timestamp() - interval '1 second' WHERE id = :run_id"
            ),
            {"run_id": run_id},
        )

    assert (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.gmail_readonly_claim_sync_runs('mailbox-crash-loop', 300, 1)"
            )
        )
        .mappings()
        .all()
        == []
    )
    exhausted = (
        connection.execute(
            sa.text(
                """
                SELECT account.status AS account_status, account.last_error_code,
                       run.status AS run_status, run.attempt_count,
                       run.lease_owner, run.lease_token, run.lease_until
                FROM careerops.gmail_accounts AS account
                JOIN careerops.gmail_sync_runs AS run ON run.gmail_account_id = account.id
                WHERE run.id = :run_id
                """
            ),
            {"run_id": run_id},
        )
        .mappings()
        .one()
    )
    assert exhausted == {
        "account_status": "paused",
        "last_error_code": "GMAIL_WORKER_LEASE_EXHAUSTED",
        "run_status": "failed",
        "attempt_count": 5,
        "lease_owner": None,
        "lease_token": None,
        "lease_until": None,
    }


def _seed_owner_candidate(connection: Connection, *, owner_id: UUID, candidate_id: UUID) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.console_users (
                id, username, password_hash, password_algorithm,
                password_parameters, password_changed_at
            )
            VALUES (
                :owner_id, :username, '$argon2id$gmail-readonly-test',
                'argon2id', '{}'::jsonb, :now
            )
            """
        ),
        {"owner_id": owner_id, "username": f"gmail-{owner_id.hex}", "now": NOW},
    )
    connection.execute(
        sa.text("INSERT INTO careerops.candidates (id, display_name) VALUES (:id, 'Gmail Test')"),
        {"id": candidate_id},
    )


def _clear_engine_test_data(engine: Engine) -> None:
    # Engine-level concurrency tests commit by design and are permitted only on the
    # disposable, module-reset database guarded by ``database_url`` above.
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                TRUNCATE careerops.console_users,
                         careerops.candidates,
                         careerops.companies,
                         careerops.job_sources,
                         careerops.canonical_jobs,
                         careerops.job_postings
                CASCADE
                """
            )
        )


def _assert_backend_is_blocked_by(
    engine: Engine,
    *,
    blocked_backend_pid: int,
    blocker_backend_pid: int,
    timeout_seconds: float = 10,
) -> None:
    deadline = monotonic() + timeout_seconds
    last_state: dict[str, object] | None = None
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer_connection:
        while monotonic() < deadline:
            wait_state = (
                observer_connection.execute(
                    sa.text(
                        """
                        SELECT state, wait_event_type, wait_event,
                               pg_blocking_pids(pid) AS blocking_pids
                        FROM pg_stat_activity
                        WHERE pid = :blocked_backend_pid
                        """
                    ),
                    {"blocked_backend_pid": blocked_backend_pid},
                )
                .mappings()
                .one_or_none()
            )
            if wait_state is not None:
                last_state = dict(wait_state)
                blocking_pids = tuple(wait_state["blocking_pids"] or ())
                if (
                    wait_state["state"] == "active"
                    and wait_state["wait_event_type"] == "Lock"
                    and blocker_backend_pid in blocking_pids
                ):
                    return
            sleep(0.01)
    raise AssertionError(
        f"backend {blocked_backend_pid} did not wait on backend "
        f"{blocker_backend_pid}; last pg_stat_activity state: {last_state!r}"
    )


def _seed_or_reuse_owner_candidate(
    connection: Connection,
    *,
    display_name: str,
) -> tuple[UUID, UUID]:
    owner_id = connection.scalar(sa.text("SELECT id FROM careerops.console_users LIMIT 1"))
    candidate_id = uuid4()
    if not isinstance(owner_id, UUID):
        owner_id = uuid4()
        _seed_owner_candidate(connection, owner_id=owner_id, candidate_id=candidate_id)
        return owner_id, candidate_id
    connection.execute(
        sa.text("INSERT INTO careerops.candidates (id, display_name) VALUES (:id, :display_name)"),
        {"id": candidate_id, "display_name": display_name},
    )
    return owner_id, candidate_id


def _register_account(
    connection: Connection,
    *,
    owner_id: UUID,
    candidate_id: UUID,
    handle: str,
    subject: str,
    key: str,
) -> UUID:
    account_id = connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_readonly_register_account(
                :owner_id, :candidate_id, :handle, :subject, 'testing',
                :credential_evidence, :key, :trace
            )
            """
        ),
        {
            "owner_id": owner_id,
            "candidate_id": candidate_id,
            "handle": handle,
            "subject": subject,
            "credential_evidence": "f" * 64,
            "key": key,
            "trace": f"trace-{key}",
        },
    )
    assert isinstance(account_id, UUID)
    return account_id


def _seed_job(connection: Connection) -> tuple[UUID, UUID]:
    company_id = uuid4()
    source_id = uuid4()
    canonical_job_id = uuid4()
    posting_id = uuid4()
    connection.execute(
        sa.text(
            "INSERT INTO careerops.companies (id, name, normalized_name) VALUES (:id, 'Acme', :norm)"
        ),
        {"id": company_id, "norm": f"acme-{company_id}"},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.job_sources (id, company_id, source_type, source_identifier, base_url)
            VALUES (:id, :company_id, 'ats', :identifier, 'https://jobs.example.test')
            """
        ),
        {"id": source_id, "company_id": company_id, "identifier": f"src-{source_id}"},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.canonical_jobs (
                id, company_id, canonical_title, normalized_title
            )
            VALUES (:id, :company_id, 'Backend Engineer', 'backend engineer')
            """
        ),
        {"id": canonical_job_id, "company_id": company_id},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.job_postings (
                id, source_id, external_id, canonical_url, first_seen_at, last_seen_at
            )
            VALUES (:id, :source_id, :external_id, 'https://jobs.example.test/1', :now, :now)
            """
        ),
        {"id": posting_id, "source_id": source_id, "external_id": f"job-{posting_id}", "now": NOW},
    )
    return canonical_job_id, posting_id


def _request_sync(
    connection: Connection, *, owner_id: UUID, account_id: UUID, key: str
) -> UUID | None:
    return connection.scalar(
        sa.text(
            "SELECT careerops.gmail_readonly_request_sync(:owner_id, :account_id, 'manual', :key, :trace)"
        ),
        {"owner_id": owner_id, "account_id": account_id, "key": key, "trace": f"trace-{key}"},
    )


def _account_snapshot_sha(connection: Connection, *, account_id: UUID) -> str:
    value = connection.scalar(
        sa.text(
            """
            SELECT encode(public.digest(jsonb_build_object(
                'id', id,
                'version', version,
                'status', status,
                'last_history_id', last_history_id,
                'next_page_token', next_page_token
            )::text, 'sha256'), 'hex')
            FROM careerops.gmail_accounts
            WHERE id = :account_id
            """
        ),
        {"account_id": account_id},
    )
    assert isinstance(value, str)
    return value


def _record_signal(
    connection: Connection,
    *,
    run_id: UUID,
    lease_token: UUID,
    message_id: str,
    history_id: str,
    signal_hash: str,
    proposal_hash: str | None,
    proposal_payload: str | None = (
        '{"proposal_kind":"application_status_update","review_priority":"high"}'
    ),
) -> UUID | None:
    return connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_readonly_record_message_signal(
                :run_id, :lease_token, :message_id, 'thread-1', :history_id,
                :message_hash, :thread_hash, NULL, :subject_hash, :snippet_hash,
                :received_at, 'interview_invitation', 'relevant', 0.930,
                CAST(:provenance AS jsonb),
                :signal_hash, 'Interview redacted excerpt', :label_hash,
                CAST(:proposal_payload AS jsonb), :proposal_hash, 'trace-signal'
            )
            """
        ),
        {
            "run_id": run_id,
            "lease_token": lease_token,
            "message_id": message_id,
            "history_id": history_id,
            "message_hash": "a" * 64,
            "thread_hash": "b" * 64,
            "subject_hash": "c" * 64,
            "snippet_hash": "d" * 64,
            "received_at": NOW,
            "provenance": '{"source":"gmail.readonly","metadata_only":true}',
            "signal_hash": signal_hash,
            "label_hash": "e" * 64,
            "proposal_payload": proposal_payload,
            "proposal_hash": proposal_hash,
        },
    )


def _raises_db_error(
    connection: Connection,
    statement: str,
    params: dict[str, object],
    *,
    role: str | None = None,
) -> bool:
    nested = connection.begin_nested()
    try:
        if role is not None:
            connection.execute(sa.text(f"SET LOCAL ROLE {role}"))
        connection.execute(sa.text(statement), params)
    except Exception:
        nested.rollback()
        return True
    else:
        nested.rollback()
        return False


def _sqlstate(error: DBAPIError) -> str | None:
    original = getattr(error, "orig", None)
    return getattr(original, "pgcode", None) or getattr(original, "sqlstate", None)
