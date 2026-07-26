"""Integration test for the e2e-career-loop backfill (task 2.10).

Asserts:
- Idempotency: a second run writes nothing.
- No silent reassignment: every application's ``candidate_id`` is unchanged;
  legacy job / application / email row counts are unchanged.
- Ownership-aware routing: applications owned by the active candidate get an
  active ``application_cycle``; ambiguous applications (different candidate)
  are quarantined (state -> ``on_hold``, append-only lifecycle event) and never
  reassigned.

Mirrors the project pattern (skip when ``CAREEROPS_TEST_DATABASE_URL`` unset).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from backfill_e2e_career_loop import run_backfill  # noqa: E402  # type: ignore[import-not-found]

from careerops.infrastructure.database.schema import (  # noqa: E402
    application_cycles,
    application_lifecycle_events,
    applications,
    candidates,
    canonical_jobs,
    companies,
    console_users,
    email_accounts,
    email_messages,
    email_threads,
    job_postings,
    job_sources,
    oauth_credential_references,
)

_ARGON2_DUMMY = (
    "$argon2id$v=19$m=19456,t=2,p=1$c29tZXNhbHQ$RfKgAVR+gMjp0V1YqP3Q0cxDzGMC+QVQvzbGQ9oLgrI"
)


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for backfill tests")
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    eng = sa.create_engine(database_url)
    yield eng
    eng.dispose()


def _purge_seed_rows(engine: Engine, ids: Mapping[str, Any]) -> None:
    """Delete ONLY the rows this test created, by id, in FK-safe order.

    Does NOT touch console_users / candidates / etc. rows owned by other
    integration tests in the shared DB. ``application_lifecycle_events`` is
    append-only (DELETE rejected by trigger); this is the only integration test
    that writes to it, so TRUNCATE is safe and bypasses the trigger.
    """
    application_ids = ids.get("application_ids") or ()
    with engine.begin() as conn:
        # Only this integration test writes to application_lifecycle_events, so
        # TRUNCATE is safe and bypasses the append-only trigger that rejects
        # row-level DELETE.
        conn.execute(sa.text("TRUNCATE careerops.application_lifecycle_events"))
        if application_ids:
            conn.execute(sa.delete(applications).where(applications.c.id.in_(application_ids)))
        if ids.get("cycle_ids"):
            conn.execute(
                sa.delete(application_cycles).where(application_cycles.c.id.in_(ids["cycle_ids"]))
            )
        # Console user: if we created it, delete it; otherwise restore the
        # original candidate_id so other integration tests see their own user.
        if ids.get("created_user"):
            conn.execute(sa.delete(console_users).where(console_users.c.id == ids["user_id"]))
        elif ids.get("restore_candidate_id") is not None:
            conn.execute(
                sa.update(console_users)
                .where(console_users.c.id == ids["user_id"])
                .values(candidate_id=ids["restore_candidate_id"])
            )
        if ids.get("email_message_ids"):
            conn.execute(
                sa.delete(email_messages).where(email_messages.c.id.in_(ids["email_message_ids"]))
            )
        if ids.get("thread_id"):
            conn.execute(sa.delete(email_threads).where(email_threads.c.id == ids["thread_id"]))
        if ids.get("account_id"):
            conn.execute(sa.delete(email_accounts).where(email_accounts.c.id == ids["account_id"]))
        if ids.get("credential_id"):
            conn.execute(
                sa.delete(oauth_credential_references).where(
                    oauth_credential_references.c.id == ids["credential_id"]
                )
            )
        if ids.get("posting_ids"):
            conn.execute(sa.delete(job_postings).where(job_postings.c.id.in_(ids["posting_ids"])))
        for job_key in ("job_1", "job_2"):
            if ids.get(job_key):
                conn.execute(sa.delete(canonical_jobs).where(canonical_jobs.c.id == ids[job_key]))
        if ids.get("source_id"):
            conn.execute(sa.delete(job_sources).where(job_sources.c.id == ids["source_id"]))
        if ids.get("company_id"):
            conn.execute(sa.delete(companies).where(companies.c.id == ids["company_id"]))
        for cand_key in ("candidate_a", "candidate_b"):
            if ids.get(cand_key):
                conn.execute(sa.delete(candidates).where(candidates.c.id == ids[cand_key]))


def _count(connection: sa.engine.Connection, table: sa.Table) -> int:
    return int(connection.scalar(sa.select(sa.func.count()).select_from(table)) or 0)


def _seed_owner_and_orphans(engine: Engine) -> dict[str, Any]:
    """Seed one active console user (candidate A), one orphan candidate (B),
    two jobs, and matching + ambiguous applications + email history. Returns
    every created id the assertions and the scoped cleanup need.
    """
    candidate_a = uuid4()
    candidate_b = uuid4()
    user_id = uuid4()
    job_1 = uuid4()
    job_2 = uuid4()
    company_id = uuid4()
    source_id = uuid4()
    posting_1 = uuid4()
    posting_2 = uuid4()
    credential_id = uuid4()
    account_id = uuid4()
    thread_id = uuid4()
    message_id = uuid4()
    app_owned_1 = uuid4()
    app_owned_2 = uuid4()
    app_ambiguous = uuid4()
    with engine.begin() as conn:
        conn.execute(
            sa.insert(candidates).values(
                [
                    {"id": candidate_a, "display_name": "Active User"},
                    {"id": candidate_b, "display_name": "Orphan"},
                ]
            )
        )
        # The single active console user, linked to candidate A (server-side
        # candidate ownership — the backfill resolves the active candidate here).
        # ``console_users.singleton_key`` is a singleton (UNIQUE = 1), so if
        # another integration test already inserted a user we REUSE that row by
        # repointing its candidate_id to ours and restoring on cleanup. Otherwise
        # we insert our own and delete it on cleanup.
        existing_user = conn.execute(
            sa.select(console_users.c.id, console_users.c.candidate_id).where(
                console_users.c.singleton_key == 1
            )
        ).first()
        if existing_user is not None:
            user_id = existing_user.id
            ids_restore_candidate = existing_user.candidate_id
            conn.execute(
                sa.update(console_users)
                .where(console_users.c.id == user_id)
                .values(candidate_id=candidate_a)
            )
            created_user = False
        else:
            ids_restore_candidate = None
            conn.execute(
                sa.insert(console_users).values(
                    id=user_id,
                    candidate_id=candidate_a,
                    singleton_key=1,
                    username=f"backfillowner-{uuid4().hex[:8]}",
                    password_hash=_ARGON2_DUMMY,
                    password_algorithm="argon2id",
                    password_parameters={},
                    password_changed_at=datetime.now(UTC),
                )
            )
            created_user = True
        conn.execute(
            sa.insert(companies).values(
                id=company_id, name="Backfill Co", normalized_name=f"bf-{uuid4().hex}"
            )
        )
        conn.execute(
            sa.insert(job_sources).values(
                id=source_id,
                company_id=company_id,
                source_type="career_page",
                source_identifier=f"bf-{uuid4().hex}",
                base_url="https://example.invalid/jobs",
            )
        )
        conn.execute(
            sa.insert(canonical_jobs).values(
                [
                    {
                        "id": job_1,
                        "company_id": company_id,
                        "canonical_title": "Job 1",
                        "normalized_title": "job 1",
                    },
                    {
                        "id": job_2,
                        "company_id": company_id,
                        "canonical_title": "Job 2",
                        "normalized_title": "job 2",
                    },
                ]
            )
        )
        # Legacy job postings — must be preserved (count unchanged) and never
        # reassigned.
        conn.execute(
            sa.insert(job_postings).values(
                [
                    {
                        "id": posting_1,
                        "source_id": source_id,
                        "external_id": f"ext-{posting_1.hex}",
                        "canonical_url": f"https://example.invalid/jobs/{posting_1.hex}",
                        "first_seen_at": datetime.now(UTC),
                        "last_seen_at": datetime.now(UTC),
                    },
                    {
                        "id": posting_2,
                        "source_id": source_id,
                        "external_id": f"ext-{posting_2.hex}",
                        "canonical_url": f"https://example.invalid/jobs/{posting_2.hex}",
                        "first_seen_at": datetime.now(UTC),
                        "last_seen_at": datetime.now(UTC),
                    },
                ]
            )
        )
        # Email history: must be preserved (count unchanged), never reassigned.
        # Seed the OAuth credential chain so email_accounts can be inserted.
        conn.execute(
            sa.insert(oauth_credential_references).values(
                id=credential_id,
                provider="google",
                account_subject=f"owner-{uuid4().hex}",
                secret_handle=f"projects/x/secrets/{uuid4().hex}",
                issued_at=datetime.now(UTC),
            )
        )
        conn.execute(
            sa.insert(email_accounts).values(
                id=account_id,
                email_address=f"owner-{uuid4().hex}@example.invalid",
                credential_reference_id=credential_id,
            )
        )
        conn.execute(
            sa.insert(email_threads).values(
                id=thread_id,
                account_id=account_id,
                provider_thread_id=f"thread-{uuid4().hex}",
            )
        )
        conn.execute(
            sa.insert(email_messages).values(
                id=message_id,
                thread_id=thread_id,
                account_id=account_id,
                provider_message_id=f"msg-{uuid4().hex}",
                received_at=datetime.now(UTC),
            )
        )
        # Three applications:
        #   app_owned_1 -> candidate A, job 1 (must link to an active cycle)
        #   app_owned_2 -> candidate A, job 2 (must link to an active cycle)
        #   app_ambiguous -> candidate B, job 1 (must quarantine, NOT reassign)
        conn.execute(
            sa.insert(applications).values(
                [
                    {
                        "id": app_owned_1,
                        "candidate_id": candidate_a,
                        "canonical_job_id": job_1,
                        "state": "submitted",
                    },
                    {
                        "id": app_owned_2,
                        "candidate_id": candidate_a,
                        "canonical_job_id": job_2,
                        "state": "favorited",
                    },
                    {
                        "id": app_ambiguous,
                        "candidate_id": candidate_b,
                        "canonical_job_id": job_1,
                        "state": "preparing",
                    },
                ]
            )
        )
    return {
        "candidate_a": candidate_a,
        "candidate_b": candidate_b,
        "user_id": user_id,
        "created_user": created_user,
        "restore_candidate_id": ids_restore_candidate,
        "company_id": company_id,
        "source_id": source_id,
        "job_1": job_1,
        "job_2": job_2,
        "posting_ids": [posting_1, posting_2],
        "credential_id": credential_id,
        "account_id": account_id,
        "thread_id": thread_id,
        "email_message_ids": [message_id],
        "application_ids": [app_owned_1, app_owned_2, app_ambiguous],
        "cycle_ids": [],  # populated by the backfill; cleanup queries them out
    }


def _snapshot_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            "applications": _count(conn, applications),
            "application_cycles": _count(conn, application_cycles),
            "application_lifecycle_events": _count(conn, application_lifecycle_events),
            "candidates": _count(conn, candidates),
            "canonical_jobs": _count(conn, canonical_jobs),
            "job_postings": _count(conn, job_postings),
            "email_threads": _count(conn, email_threads),
            "email_messages": _count(conn, email_messages),
            "console_users": _count(conn, console_users),
        }


def _application_owner_map(engine: Engine) -> dict[UUID, UUID]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(applications.c.id, applications.c.candidate_id)).all()
    return {row.id: row.candidate_id for row in rows}


def test_backfill_is_idempotent_and_preserves_history(engine: Engine) -> None:
    ids = _seed_owner_and_orphans(engine)

    before = _snapshot_counts(engine)
    before_owners = _application_owner_map(engine)

    # --- First run: writes expected. ---
    report = run_backfill(engine)
    assert report.active_candidate_id == ids["candidate_a"]
    assert report.applications_total == 3
    assert report.applications_linked == 2
    assert report.applications_quarantined == 1
    assert report.applications_skipped == 0

    after_first = _snapshot_counts(engine)
    after_first_owners = _application_owner_map(engine)

    # Capture the cycles the backfill created so cleanup is scoped to them.
    with engine.connect() as conn:
        ids["cycle_ids"] = [
            row.id
            for row in conn.execute(
                sa.select(application_cycles.c.id).where(
                    application_cycles.c.candidate_id.in_([ids["candidate_a"], ids["candidate_b"]])
                )
            )
        ]

    # Two active cycles created, one per (candidate, job) pair owned by A.
    assert after_first["application_cycles"] == before["application_cycles"] + 2
    with engine.connect() as conn:
        active_cycles = conn.execute(
            sa.select(
                application_cycles.c.candidate_id,
                application_cycles.c.canonical_job_id,
            ).where(application_cycles.c.active.is_(True))
        ).all()
        assert {(row.candidate_id, row.canonical_job_id) for row in active_cycles} == {
            (ids["candidate_a"], ids["job_1"]),
            (ids["candidate_a"], ids["job_2"]),
        }
        # Every application owned by A is linked; the ambiguous one is not.
        linked = conn.execute(
            sa.select(applications.c.id, applications.c.cycle_id).where(
                applications.c.id.in_(ids["application_ids"])
            )
        ).all()
        by_candidate: dict[UUID, list] = {}
        for row in linked:
            by_candidate.setdefault(row.cycle_id, [])
        cycle_id_none = [row.id for row in linked if row.cycle_id is None]
        assert len(cycle_id_none) == 1  # only the ambiguous app is unlinked
        # The quarantined application moved to on_hold and emitted one
        # note_added lifecycle event tagged with the backfill marker.
        quarantined_row = conn.execute(
            sa.select(applications.c.id, applications.c.state).where(
                applications.c.candidate_id == ids["candidate_b"]
            )
        ).one()
        assert quarantined_row.state == "on_hold"
        marker_events = conn.execute(
            sa.select(application_lifecycle_events.c.event_type).where(
                application_lifecycle_events.c.application_id == quarantined_row.id,
                application_lifecycle_events.c.event_data.op("@>")(
                    sa.type_coerce({"marker": "ambiguous_ownership"}, JSONB)
                ),
            )
        ).all()
        assert len(marker_events) == 1
        assert marker_events[0].event_type == "note_added"

    # NO silent reassignment: every application keeps its original candidate_id.
    assert after_first_owners == before_owners

    # Legacy history is untouched (counts unchanged except the two new-write
    # targets the backfill owns: cycles + lifecycle events).
    for table in (
        "applications",
        "candidates",
        "canonical_jobs",
        "job_postings",
        "email_threads",
        "email_messages",
        "console_users",
    ):
        assert after_first[table] == before[table], table
    assert after_first["application_lifecycle_events"] == before["application_lifecycle_events"] + 1

    # --- Second run: idempotent — zero writes. ---
    second_report = run_backfill(engine)
    assert second_report.applications_linked == 0
    assert second_report.applications_quarantined == 0
    assert second_report.applications_skipped == 3

    after_second = _snapshot_counts(engine)
    after_second_owners = _application_owner_map(engine)

    # Counts and ownership identical to the post-first-run state.
    assert after_second == after_first
    assert after_second_owners == after_first_owners

    # --- Cleanup so re-running the test module doesn't accumulate rows. ---
    _purge_seed_rows(engine, ids)
