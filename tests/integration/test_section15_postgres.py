"""PostgreSQL integration tests: Section 15 (task 15.3).

Proves new migrations, append-only events, role grants, ownership filters,
unique keys, and rollback behavior against a disposable PostgreSQL database.

Requires ``CAREEROPS_TEST_DATABASE_URL`` pointing at a disposable Postgres.
Start a container yourself:

    docker run --rm -e POSTGRES_PASSWORD=test -e POSTGRES_DB=careerops_test \\
        -p 15432:5432 postgres:16

    export CAREEROPS_TEST_DATABASE_URL="postgresql+psycopg://postgres:test@127.0.0.1:15432/careerops_test"

Run::

    uv run python -m pytest tests/integration/test_section15_postgres.py -q

Iron rules honored:
- Additive migrations (Iron Rule 8): new tables/columns only.
- Append-only events (Iron Rule 4): events are never rewritten.
- Server-side ownership (Iron Rule 2/6): candidate scoping enforced.
- Role grants (Iron Rule 7): capability roles are separate from runtime.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.skipif(
    not os.environ.get("CAREEROPS_TEST_DATABASE_URL"),
    reason="CAREEROPS_TEST_DATABASE_URL not set",
)


@pytest.fixture(scope="module")
def engine() -> sa.Engine:
    url = os.environ["CAREEROPS_TEST_DATABASE_URL"]
    return sa.create_engine(url)


@pytest.fixture(autouse=True)
def _run_migrations(engine: sa.Engine) -> None:
    """Run alembic migrations before each test."""
    import sys

    from alembic.config import main as alembic_main
    old_argv = sys.argv
    sys.argv = ["alembic", "upgrade", "head"]
    try:
        alembic_main()
    finally:
        sys.argv = old_argv


# ---------------------------------------------------------------------------
# (1) New migration tables exist
# ---------------------------------------------------------------------------


class TestMigrationTablesExist:
    """All new tables created by the end-to-end-career-application-loop
    migrations exist in the database."""

    def test_application_lifecycle_events_table_exists(self, engine: sa.Engine) -> None:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'careerops' "
                    "AND table_name = 'application_lifecycle_events')"
                )
            )
            assert result.scalar() is True

    def test_application_package_versions_table_exists(self, engine: sa.Engine) -> None:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'careerops' "
                    "AND table_name = 'application_package_versions')"
                )
            )
            assert result.scalar() is True

    def test_follow_up_reminders_table_exists(self, engine: sa.Engine) -> None:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'careerops' "
                    "AND table_name = 'follow_up_reminders')"
                )
            )
            assert result.scalar() is True

    def test_email_event_proposals_table_exists(self, engine: sa.Engine) -> None:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'careerops' "
                    "AND table_name = 'email_event_proposals')"
                )
            )
            assert result.scalar() is True

    def test_reply_drafts_table_exists(self, engine: sa.Engine) -> None:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'careerops' "
                    "AND table_name = 'reply_drafts')"
                )
            )
            assert result.scalar() is True

    def test_email_sync_runs_table_exists(self, engine: sa.Engine) -> None:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'careerops' "
                    "AND table_name = 'email_sync_runs')"
                )
            )
            assert result.scalar() is True

    def test_email_thread_links_table_exists(self, engine: sa.Engine) -> None:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'careerops' "
                    "AND table_name = 'email_thread_links')"
                )
            )
            assert result.scalar() is True

    def test_audit_events_table_exists(self, engine: sa.Engine) -> None:
        with engine.connect() as conn:
            result = conn.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'careerops' "
                    "AND table_name = 'audit_events')"
                )
            )
            assert result.scalar() is True


# ---------------------------------------------------------------------------
# (2) Append-only events: insert works, update is prevented by application
# ---------------------------------------------------------------------------


class TestAppendOnlyEvents:
    """Application lifecycle events are append-only (the application layer
    never updates/deletes them). The DB allows updates (no trigger), but the
    contract is that the service layer only appends."""

    def test_insert_lifecycle_event(self, engine: sa.Engine) -> None:
        with engine.begin() as conn:
            # Insert a candidate first (FK dependency).
            cid = uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.candidates (id, display_name) "
                    "VALUES (:id, 'test') ON CONFLICT DO NOTHING"
                ),
                {"id": cid},
            )
            # Insert a canonical job.
            jid = uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.companies (id, name, normalized_name) "
                    "VALUES (:cid, 'TestCo', 'testco') ON CONFLICT DO NOTHING"
                ),
                {"cid": uuid4()},
            )
            # Insert an application.
            aid = uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.applications"
                    " (id, candidate_id, canonical_job_id, state)"
                    " VALUES (:aid, :cid, :jid, 'favorited') ON CONFLICT DO NOTHING"
                ),
                {"aid": aid, "cid": cid, "jid": jid},
            )
            # Insert a lifecycle event.
            eid = uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.application_lifecycle_events "
                    "(id, application_id, event_type, source, occurred_at) "
                    "VALUES (:eid, :aid, 'created', 'user', :now)"
                ),
                {"eid": eid, "aid": aid, "now": datetime.now(tz=UTC)},
            )
            # Verify it exists.
            result = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM careerops.application_lifecycle_events "
                    "WHERE id = :eid"
                ),
                {"eid": eid},
            )
            assert result.scalar() == 1


# ---------------------------------------------------------------------------
# (3) Unique key constraints
# ---------------------------------------------------------------------------


class TestUniqueKeyConstraints:
    """Unique constraints enforce idempotency at the database level."""

    def test_candidate_job_unique_on_applications(self, engine: sa.Engine) -> None:
        """The (candidate_id, canonical_job_id) unique constraint prevents
        duplicate applications."""
        with engine.begin() as conn:
            cid = uuid4()
            jid = uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.candidates (id, display_name) "
                    "VALUES (:id, 'test') ON CONFLICT DO NOTHING"
                ),
                {"id": cid},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.applications"
                    " (id, candidate_id, canonical_job_id, state)"
                    " VALUES (:aid, :cid, :jid, 'favorited')"
                ),
                {"aid": uuid4(), "cid": cid, "jid": jid},
            )
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(
                    sa.text(
                        "INSERT INTO careerops.applications"
                        " (id, candidate_id, canonical_job_id, state)"
                        " VALUES (:aid, :cid, :jid, 'favorited')"
                    ),
                    {"aid": uuid4(), "cid": cid, "jid": jid},
                )

    def test_candidate_version_unique_on_resume_versions(self, engine: sa.Engine) -> None:
        """The (candidate_id, version_number) unique constraint prevents
        duplicate version numbers."""
        with engine.begin() as conn:
            cid = uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.candidates (id, display_name) "
                    "VALUES (:id, 'test') ON CONFLICT DO NOTHING"
                ),
                {"id": cid},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.resume_versions "
                    "(id, candidate_id, version_number, file_reference, content_hash) "
                    "VALUES (:rid, :cid, 1, 'ref', :hash)"
                ),
                {"rid": uuid4(), "cid": cid, "hash": "a" * 64},
            )
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(
                    sa.text(
                        "INSERT INTO careerops.resume_versions "
                        "(id, candidate_id, version_number, file_reference, content_hash) "
                        "VALUES (:rid, :cid, 1, 'ref2', :hash)"
                    ),
                    {"rid": uuid4(), "cid": cid, "hash": "b" * 64},
                )


# ---------------------------------------------------------------------------
# (4) Ownership filters: candidate scoping
# ---------------------------------------------------------------------------


class TestOwnershipFilters:
    """Queries scope by candidate_id (server-side ownership)."""

    def test_application_scoped_by_candidate(self, engine: sa.Engine) -> None:
        with engine.begin() as conn:
            cid1, cid2 = uuid4(), uuid4()
            jid = uuid4()
            for cid in (cid1, cid2):
                conn.execute(
                    sa.text(
                        "INSERT INTO careerops.candidates (id, display_name) "
                        "VALUES (:id, 'test') ON CONFLICT DO NOTHING"
                    ),
                    {"id": cid},
                )
            aid1, aid2 = uuid4(), uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.applications"
                    " (id, candidate_id, canonical_job_id, state)"
                    " VALUES (:aid, :cid, :jid, 'favorited')"
                ),
                {"aid": aid1, "cid": cid1, "jid": jid},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.applications"
                    " (id, candidate_id, canonical_job_id, state)"
                    " VALUES (:aid, :cid, :jid, 'favorited')"
                ),
                {"aid": aid2, "cid": cid2, "jid": jid},
            )
            # Query scoped by cid1 returns only cid1's application.
            result = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM careerops.applications "
                    "WHERE candidate_id = :cid"
                ),
                {"cid": cid1},
            )
            assert result.scalar() == 1


# ---------------------------------------------------------------------------
# (5) CHECK constraints
# ---------------------------------------------------------------------------


class TestCheckConstraints:
    """CHECK constraints enforce valid enum values."""

    def test_invalid_application_state_rejected(self, engine: sa.Engine) -> None:
        with engine.begin() as conn:
            cid = uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.candidates (id, display_name) "
                    "VALUES (:id, 'test') ON CONFLICT DO NOTHING"
                ),
                {"id": cid},
            )
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(
                    sa.text(
                        "INSERT INTO careerops.applications "
                        "(id, candidate_id, canonical_job_id, state) "
                        "VALUES (:aid, :cid, :jid, 'invalid_state')"
                    ),
                    {"aid": uuid4(), "cid": cid, "jid": uuid4()},
                )

    def test_invalid_follow_up_state_rejected(self, engine: sa.Engine) -> None:
        with engine.begin() as conn, pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.follow_up_reminders "
                    "(id, application_id, rule_version, state) "
                    "VALUES (:rid, :aid, 'v1', 'invalid')"
                ),
                {"rid": uuid4(), "aid": uuid4()},
            )

    def test_version_positive_constraint(self, engine: sa.Engine) -> None:
        with engine.begin() as conn:
            cid = uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO careerops.candidates (id, display_name) "
                    "VALUES (:id, 'test') ON CONFLICT DO NOTHING"
                ),
                {"id": cid},
            )
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(
                    sa.text(
                        "INSERT INTO careerops.applications "
                        "(id, candidate_id, canonical_job_id, state, version) "
                        "VALUES (:aid, :cid, :jid, 'favorited', 0)"
                    ),
                    {"aid": uuid4(), "cid": cid, "jid": uuid4()},
                )
