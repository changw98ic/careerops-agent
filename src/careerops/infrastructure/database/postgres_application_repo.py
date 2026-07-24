"""PostgreSQL-backed implementations of M3 application repository protocols.

Satisfies ``ApplicationRepository``, ``ResumeRepository``,
``PackageRepository``, and ``FollowUpRepository`` from
``careerops.application.applications``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.domain.applications import (
    Application,
    ApplicationEvent,
    ApplicationEventSource,
    ApplicationEventType,
    ApplicationPackage,
    ApplicationState,
    FollowUpReminder,
    FollowUpState,
    PackageApprovalState,
    PackageClaim,
    ResumeVersion,
)
from careerops.infrastructure.database.schema import (
    application_lifecycle_events,
    application_packages,
    applications,
    follow_up_reminders,
    resume_versions,
)


def _row_to_application(row: sa.RowMapping) -> Application:
    return Application(
        id=row["id"],
        candidate_id=row["candidate_id"],
        canonical_job_id=row["canonical_job_id"],
        state=ApplicationState(row["state"]),
        apply_url=row["apply_url"],
        submitted_at=row["submitted_at"],
        follow_up_due_at=row["follow_up_due_at"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_event(row: sa.RowMapping) -> ApplicationEvent:
    return ApplicationEvent(
        id=row["id"],
        application_id=row["application_id"],
        event_type=ApplicationEventType(row["event_type"]),
        from_state=ApplicationState(row["from_state"]) if row["from_state"] else None,
        to_state=ApplicationState(row["to_state"]) if row["to_state"] else None,
        source=ApplicationEventSource(row["source"]),
        actor_id=row["actor_id"],
        note=row["note"],
        event_data=row["event_data"] or {},
        occurred_at=row["occurred_at"],
        created_at=row["created_at"],
    )


def _row_to_resume(row: sa.RowMapping) -> ResumeVersion:
    return ResumeVersion(
        id=row["id"],
        candidate_id=row["candidate_id"],
        version_number=row["version_number"],
        file_reference=row["file_reference"],
        content_hash=row["content_hash"],
        target_type=row["target_type"],
        human_confirmed=row["human_confirmed"],
        created_at=row["created_at"],
    )


def _row_to_package(row: sa.RowMapping) -> ApplicationPackage:
    raw_claims: list[dict[str, Any]] = row["claims"] or []
    claims = tuple(
        PackageClaim(
            claim_text=c["claim_text"],
            evidence_ids=tuple(UUID(e) for e in c.get("evidence_ids", ())),
        )
        for c in raw_claims
    )
    return ApplicationPackage(
        id=row["id"],
        application_id=row["application_id"],
        resume_version_id=row["resume_version_id"],
        cover_letter_text=row["cover_letter_text"],
        notes=row["notes"],
        answers=row["answers"] or {},
        claims=claims,
        approval_state=PackageApprovalState(row["approval_state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_follow_up(row: sa.RowMapping) -> FollowUpReminder:
    return FollowUpReminder(
        id=row["id"],
        application_id=row["application_id"],
        rule_version=row["rule_version"],
        state=FollowUpState(row["state"]),
        due_at=row["due_at"],
        snoozed_until=row["snoozed_until"],
        cancelled_reason=row["cancelled_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _claims_to_json(claims: tuple[PackageClaim, ...]) -> list[dict[str, object]]:
    return [
        {"claim_text": c.claim_text, "evidence_ids": [str(e) for e in c.evidence_ids]}
        for c in claims
    ]


class PostgresApplicationRepository:
    """PostgreSQL-backed application, event, resume, package, and follow-up store.

    Satisfies the ``ApplicationRepository``, ``ResumeRepository``,
    ``PackageRepository``, and ``FollowUpRepository`` protocols.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- ApplicationRepository protocol ------------------------------------

    def find_by_id(self, application_id: UUID) -> Application | None:
        stmt = sa.select(applications).where(applications.c.id == application_id)
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_application(row) if row else None

    def find_by_candidate_and_job(
        self, candidate_id: UUID, canonical_job_id: UUID
    ) -> Application | None:
        stmt = sa.select(applications).where(
            sa.and_(
                applications.c.candidate_id == candidate_id,
                applications.c.canonical_job_id == canonical_job_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_application(row) if row else None

    def save(self, application: Application) -> None:
        values = {
            "id": application.id,
            "candidate_id": application.candidate_id,
            "canonical_job_id": application.canonical_job_id,
            "state": application.state.value,
            "apply_url": application.apply_url,
            "submitted_at": application.submitted_at,
            "follow_up_due_at": application.follow_up_due_at,
            "version": application.version,
            "updated_at": application.updated_at,
        }
        stmt = (
            pg_insert(applications)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[applications.c.id],
                set_={
                    "state": values["state"],
                    "apply_url": values["apply_url"],
                    "submitted_at": values["submitted_at"],
                    "follow_up_due_at": values["follow_up_due_at"],
                    "version": values["version"],
                    "updated_at": values["updated_at"],
                },
            )
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def append_event(self, event: ApplicationEvent) -> None:
        stmt = application_lifecycle_events.insert().values(
            id=event.id,
            application_id=event.application_id,
            event_type=event.event_type.value,
            from_state=event.from_state.value if event.from_state else None,
            to_state=event.to_state.value if event.to_state else None,
            source=event.source.value,
            actor_id=event.actor_id,
            note=event.note,
            event_data=event.event_data,
            occurred_at=event.occurred_at,
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def get_events(self, application_id: UUID) -> list[ApplicationEvent]:
        stmt = (
            sa.select(application_lifecycle_events)
            .where(application_lifecycle_events.c.application_id == application_id)
            .order_by(application_lifecycle_events.c.occurred_at)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_event(r) for r in rows]

    # -- ResumeRepository protocol -----------------------------------------

    def find_latest_version(self, candidate_id: UUID) -> ResumeVersion | None:
        stmt = (
            sa.select(resume_versions)
            .where(resume_versions.c.candidate_id == candidate_id)
            .order_by(resume_versions.c.version_number.desc())
            .limit(1)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_resume(row) if row else None

    def save_resume(self, version: ResumeVersion) -> None:
        stmt = (
            pg_insert(resume_versions)
            .values(
                id=version.id,
                candidate_id=version.candidate_id,
                version_number=version.version_number,
                file_reference=version.file_reference,
                content_hash=version.content_hash,
                target_type=version.target_type,
                human_confirmed=version.human_confirmed,
            )
            .on_conflict_do_update(
                index_elements=[resume_versions.c.id],
                set_={
                    "version_number": version.version_number,
                    "file_reference": version.file_reference,
                    "content_hash": version.content_hash,
                    "target_type": version.target_type,
                    "human_confirmed": version.human_confirmed,
                },
            )
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    # -- PackageRepository protocol ----------------------------------------

    def find_by_application(self, application_id: UUID) -> ApplicationPackage | None:
        stmt = sa.select(application_packages).where(
            application_packages.c.application_id == application_id
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_package(row) if row else None

    def save_package(self, package: ApplicationPackage) -> None:
        stmt = (
            pg_insert(application_packages)
            .values(
                id=package.id,
                application_id=package.application_id,
                resume_version_id=package.resume_version_id,
                cover_letter_text=package.cover_letter_text,
                notes=package.notes,
                answers=package.answers,
                claims=_claims_to_json(package.claims),
                approval_state=package.approval_state.value,
                updated_at=package.updated_at,
            )
            .on_conflict_do_update(
                index_elements=[application_packages.c.id],
                set_={
                    "resume_version_id": package.resume_version_id,
                    "cover_letter_text": package.cover_letter_text,
                    "notes": package.notes,
                    "answers": package.answers,
                    "claims": _claims_to_json(package.claims),
                    "approval_state": package.approval_state.value,
                    "updated_at": package.updated_at,
                },
            )
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    # -- FollowUpRepository protocol ---------------------------------------

    def find_follow_up_by_id(self, reminder_id: UUID) -> FollowUpReminder | None:
        stmt = sa.select(follow_up_reminders).where(follow_up_reminders.c.id == reminder_id)
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_follow_up(row) if row else None

    def find_active_by_application_and_rule(
        self, application_id: UUID, rule_version: str
    ) -> FollowUpReminder | None:
        stmt = sa.select(follow_up_reminders).where(
            sa.and_(
                follow_up_reminders.c.application_id == application_id,
                follow_up_reminders.c.rule_version == rule_version,
                follow_up_reminders.c.state == FollowUpState.ACTIVE.value,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_follow_up(row) if row else None

    def save_follow_up(self, reminder: FollowUpReminder) -> None:
        stmt = (
            pg_insert(follow_up_reminders)
            .values(
                id=reminder.id,
                application_id=reminder.application_id,
                rule_version=reminder.rule_version,
                state=reminder.state.value,
                due_at=reminder.due_at,
                snoozed_until=reminder.snoozed_until,
                cancelled_reason=reminder.cancelled_reason,
                updated_at=reminder.updated_at,
            )
            .on_conflict_do_update(
                index_elements=[follow_up_reminders.c.id],
                set_={
                    "state": reminder.state.value,
                    "due_at": reminder.due_at,
                    "snoozed_until": reminder.snoozed_until,
                    "cancelled_reason": reminder.cancelled_reason,
                    "updated_at": reminder.updated_at,
                },
            )
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    # -- Route-facing helpers (returns dicts for API responses) -------------

    def list_applications(
        self,
        *,
        candidate_id: str | None = None,
        state: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        stmt = sa.select(applications)
        if candidate_id is not None:
            stmt = stmt.where(applications.c.candidate_id == UUID(candidate_id))
        if state is not None:
            stmt = stmt.where(applications.c.state == state)
        stmt = stmt.order_by(applications.c.created_at.desc()).limit(limit)

        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()

        return [
            {
                "id": r["id"],
                "candidate_id": r["candidate_id"],
                "canonical_job_id": r["canonical_job_id"],
                "state": r["state"],
                "apply_url": r["apply_url"],
                "submitted_at": r["submitted_at"].isoformat() if r["submitted_at"] else None,
                "follow_up_due_at": (
                    r["follow_up_due_at"].isoformat() if r["follow_up_due_at"] else None
                ),
                "version": r["version"],
            }
            for r in rows
        ]
