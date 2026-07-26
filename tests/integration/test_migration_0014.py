"""Disposable PostgreSQL migration tests for migration 0014 (end-to-end career
loop: database identity, candidate ownership, cycles).

Mirrors the project pattern from ``test_migrations`` / ``test_storage_outbox`` /
``test_temporal_m1``: skip when ``CAREEROPS_TEST_DATABASE_URL`` is unset. The
shared test database is treated as disposable — we reset to base, upgrade to
head, exercise the 0014 objects (uniqueness, runtime-role least privilege,
ownership scoping), downgrade exactly one revision to prove the additive
downgrade removes only what 0014 created, then restore to head for downstream
test modules.

Why a single test function: every assertion mutates migration state, so we keep
the whole round-trip in one function (the same shape ``test_migrations`` uses)
to avoid inter-test ordering hazards.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for migration 0014 tests")
    return value


def _cfg(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def _has_column(connection: sa.engine.Connection, table: str, column: str) -> bool:
    return column in {
        col["name"] for col in sa.inspect(connection).get_columns(table, schema="careerops")
    }


def _check_constraint_names(connection: sa.engine.Connection, table: str) -> set[str | None]:
    return {
        constraint["name"]
        for constraint in sa.inspect(connection).get_check_constraints(table, schema="careerops")
    }


def _unique_constraint_names(connection: sa.engine.Connection, table: str) -> set[str | None]:
    return {
        constraint["name"]
        for constraint in sa.inspect(connection).get_unique_constraints(table, schema="careerops")
    }


def _index_names(connection: sa.engine.Connection, table: str) -> set[str | None]:
    return {
        index["name"] for index in sa.inspect(connection).get_indexes(table, schema="careerops")
    }


def _insert_candidate(connection: sa.engine.Connection) -> object:
    candidate_id = uuid4()
    connection.execute(
        sa.text("INSERT INTO careerops.candidates (id, display_name) VALUES (:id, :name)"),
        {"id": candidate_id, "name": "Migration 0014 Candidate"},
    )
    return candidate_id


def _insert_company_and_job(connection: sa.engine.Connection) -> tuple[object, object]:
    company_id = uuid4()
    job_id = uuid4()
    connection.execute(
        sa.text(
            "INSERT INTO careerops.companies (id, name, normalized_name) VALUES (:id, :name, :norm)"
        ),
        {"id": company_id, "name": "M14 Co", "norm": f"m14-{uuid4().hex}"},
    )
    # primary_posting_id stays NULL: the composite FK to job_posting_assignments
    # is satisfied when any column is NULL, so we don't need the assignment chain.
    connection.execute(
        sa.text(
            "INSERT INTO careerops.canonical_jobs "
            "(id, company_id, canonical_title, normalized_title) "
            "VALUES (:id, :company_id, 'M14 Job', 'm14 job')"
        ),
        {"id": job_id, "company_id": company_id},
    )
    return company_id, job_id


def _profile_version_values(
    *, candidate_id: object, version: int, is_active: bool
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "candidate_id": candidate_id,
        "version": version,
        "is_active": is_active,
    }


def test_migration_0014_upgrade_downgrade_uniqueness_and_runtime_role(
    database_url: str,
) -> None:
    config = _cfg(database_url)
    engine = sa.create_engine(database_url)

    try:
        # ------------------------------------------------------------------
        # 1. From-scratch upgrade: reset to base, then upgrade to head.
        # ------------------------------------------------------------------
        command.downgrade(config, "base")
        command.upgrade(config, "head")

        # ------------------------------------------------------------------
        # 2. Assert 0014 created every new table / column / constraint.
        # ------------------------------------------------------------------
        with engine.connect() as connection:
            inspector = sa.inspect(connection)

            # New tables.
            assert inspector.has_table("profile_versions", schema="careerops")
            assert inspector.has_table("application_cycles", schema="careerops")

            # resume_versions lifecycle columns (task 2.4).
            for column in (
                "parse_status",
                "confirmation_status",
                "source_reference",
                "parsed_at",
                "confirmed_at",
            ):
                assert _has_column(connection, "resume_versions", column), column

            # evidence_items resume-derived columns (task 2.5).
            for column in (
                "extractor_version",
                "source_span",
                "confirmation_status",
                "evidence_hash",
                "resume_version_id",
            ):
                assert _has_column(connection, "evidence_items", column), column

            # applications cycle/channel/package/provider linkage (tasks 2.6/2.7).
            for column in (
                "cycle_id",
                "submission_channel",
                "package_version_id",
                "payload_hash",
                "provider_kind",
                "provider_message_id",
            ):
                assert _has_column(connection, "applications", column), column

            # 2.1 unique constraint on console_users.candidate_id.
            assert "uq_console_users_candidate_id" in _unique_constraint_names(
                connection, "console_users"
            )

            # profile_versions shape guards and one-active partial index.
            profile_checks = _check_constraint_names(connection, "profile_versions")
            for name in (
                "ck_profile_versions_version_positive",
                "ck_profile_versions_target_roles_array",
                "ck_profile_versions_locations_array",
                "ck_profile_versions_remote_rules_object",
                "ck_profile_versions_compensation_object",
                "ck_profile_versions_authorization_rules_object",
                "ck_profile_versions_compensation_range",
            ):
                assert name in profile_checks, name
            assert "ix_profile_versions_candidate_active" in _index_names(
                connection, "profile_versions"
            )

            # application_cycles one-active partial index + active/closed check.
            assert "ck_application_cycles_active_closed_consistent" in _check_constraint_names(
                connection, "application_cycles"
            )
            assert "ix_application_cycles_candidate_job_active" in _index_names(
                connection, "application_cycles"
            )

        # ------------------------------------------------------------------
        # 3. Uniqueness: exactly one active profile version per candidate.
        # ------------------------------------------------------------------
        with engine.begin() as connection:
            candidate_id = _insert_candidate(connection)
            connection.execute(
                sa.text(
                    "INSERT INTO careerops.profile_versions "
                    "(id, candidate_id, version, is_active) "
                    "VALUES (:id, :candidate_id, 1, true)"
                ),
                _profile_version_values(candidate_id=candidate_id, version=1, is_active=True),
            )
            # A second ACTIVE version for the same candidate is rejected by the
            # partial unique index (is_active = true). Wrap in a savepoint so the
            # expected failure does not abort the outer transaction.
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO careerops.profile_versions "
                        "(id, candidate_id, version, is_active) "
                        "VALUES (:id, :candidate_id, 2, true)"
                    ),
                    _profile_version_values(candidate_id=candidate_id, version=2, is_active=True),
                )
            # An INACTIVE second version is fine (history preserved).
            connection.execute(
                sa.text(
                    "INSERT INTO careerops.profile_versions "
                    "(id, candidate_id, version, is_active) "
                    "VALUES (:id, :candidate_id, 2, false)"
                ),
                _profile_version_values(candidate_id=candidate_id, version=2, is_active=False),
            )

        # ------------------------------------------------------------------
        # 4. Uniqueness: exactly one active cycle per (candidate, canonical_job).
        # ------------------------------------------------------------------
        with engine.begin() as connection:
            candidate_id = _insert_candidate(connection)
            _company_id, job_id = _insert_company_and_job(connection)
            cycle_id = uuid4()
            connection.execute(
                sa.text(
                    "INSERT INTO careerops.application_cycles "
                    "(id, candidate_id, canonical_job_id, active) "
                    "VALUES (:id, :candidate_id, :job_id, true)"
                ),
                {"id": cycle_id, "candidate_id": candidate_id, "job_id": job_id},
            )
            # Second ACTIVE cycle for the same pair is rejected.
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO careerops.application_cycles "
                        "(id, candidate_id, canonical_job_id, active) "
                        "VALUES (:id, :candidate_id, :job_id, true)"
                    ),
                    {"id": uuid4(), "candidate_id": candidate_id, "job_id": job_id},
                )
            # An INACTIVE cycle (closed) for the same pair is allowed and links
            # to the prior one (re-application history, design Decision 8).
            closed_at = datetime.now(UTC)
            connection.execute(
                sa.text(
                    "INSERT INTO careerops.application_cycles "
                    "(id, candidate_id, canonical_job_id, active, prior_cycle_id, closed_at) "
                    "VALUES (:id, :candidate_id, :job_id, false, :prior, :closed_at)"
                ),
                {
                    "id": uuid4(),
                    "candidate_id": candidate_id,
                    "job_id": job_id,
                    "prior": cycle_id,
                    "closed_at": closed_at,
                },
            )
            # The active/closed consistency guard rejects an inconsistent row:
            # active=false with closed_at NULL.
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO careerops.application_cycles "
                        "(id, candidate_id, canonical_job_id, active) "
                        "VALUES (:id, :candidate_id, :job_id, false)"
                    ),
                    {"id": uuid4(), "candidate_id": candidate_id, "job_id": job_id},
                )

        # ------------------------------------------------------------------
        # 5. Runtime role (careerops_api) can read/write the new tables but
        #    cannot do privileged ops (least privilege, iron rule 4).
        # ------------------------------------------------------------------
        with engine.begin() as connection:
            # Privilege check via PostgreSQL's privilege tester (no SET ROLE).
            for table, privs in (
                ("profile_versions", ("SELECT", "INSERT", "UPDATE")),
                ("application_cycles", ("SELECT", "INSERT", "UPDATE")),
            ):
                for priv in privs:
                    assert connection.scalar(
                        sa.text(
                            f"SELECT has_table_privilege('careerops_api', "
                            f"'careerops.{table}', '{priv}')"
                        )
                    ), f"careerops_api missing {priv} on careerops.{table}"
            # console_users.candidate_id column-level grants (task 2.8).
            assert connection.scalar(
                sa.text(
                    "SELECT has_column_privilege('careerops_api', "
                    "'careerops.console_users', 'candidate_id', 'INSERT')"
                )
            )
            # DELETE is intentionally NOT granted on the new tables.
            assert not connection.scalar(
                sa.text(
                    "SELECT has_table_privilege('careerops_api', "
                    "'careerops.profile_versions', 'DELETE')"
                )
            )

        with engine.begin() as connection:
            candidate_id = _insert_candidate(connection)
            connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
            # Read/write under the runtime role works.
            version_id = uuid4()
            connection.execute(
                sa.text(
                    "INSERT INTO careerops.profile_versions "
                    "(id, candidate_id, version, is_active) "
                    "VALUES (:id, :candidate_id, 1, true)"
                ),
                {"id": version_id, "candidate_id": candidate_id},
            )
            assert (
                connection.scalar(sa.text("SELECT count(*) FROM careerops.profile_versions")) >= 1
            )
            connection.execute(
                sa.text("UPDATE careerops.profile_versions SET seniority = 'staff' WHERE id = :id"),
                {"id": version_id},
            )
            # DELETE under careerops_api is denied (42501). Savepoint keeps the
            # outer transaction usable for the DROP check below.
            with (
                pytest.raises(DBAPIError) as delete_error,
                connection.begin_nested(),
            ):
                connection.execute(
                    sa.text("DELETE FROM careerops.profile_versions WHERE id = :id"),
                    {"id": version_id},
                )
            assert sqlstate(delete_error.value) == "42501"
            # A privileged op (DROP TABLE) is denied (42501) — careerops_api does
            # not own the table and has no DROP privilege.
            with (
                pytest.raises(DBAPIError) as drop_error,
                connection.begin_nested(),
            ):
                connection.execute(sa.text("DROP TABLE careerops.profile_versions"))
            assert sqlstate(drop_error.value) == "42501"

        # ------------------------------------------------------------------
        # 6. Downgrade exactly one revision (0014 -> 0013): only the objects
        #    0014 created are removed; every pre-existing object is untouched
        #    (additive invariant, iron rule 1).
        # ------------------------------------------------------------------
        command.downgrade(config, "0013")

        with engine.connect() as connection:
            inspector = sa.inspect(connection)

            # 0014 objects are gone.
            assert not inspector.has_table("profile_versions", schema="careerops")
            assert not inspector.has_table("application_cycles", schema="careerops")
            assert "uq_console_users_candidate_id" not in _unique_constraint_names(
                connection, "console_users"
            )
            for table, column in (
                ("resume_versions", "parse_status"),
                ("evidence_items", "extractor_version"),
                ("applications", "cycle_id"),
                ("applications", "submission_channel"),
            ):
                assert not _has_column(connection, table, column), f"{table}.{column}"

            # Pre-existing objects survive (FK/cycle chain from 0013 and earlier
            # is intact). candidate_id itself was added in 0013 and stays.
            for table in (
                "candidates",
                "console_users",
                "applications",
                "resume_versions",
                "evidence_items",
                "canonical_jobs",
                "email_messages",
            ):
                assert inspector.has_table(table, schema="careerops"), table
            assert _has_column(connection, "console_users", "candidate_id")

        # ------------------------------------------------------------------
        # 7. Restore to head for downstream test modules; alembic check proves
        #    the upgraded schema still matches schema.py byte-for-byte.
        # ------------------------------------------------------------------
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
