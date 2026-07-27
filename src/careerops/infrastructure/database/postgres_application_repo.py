"""PostgreSQL-backed implementations of M3 application repository protocols.

Satisfies ``ApplicationRepository``, ``ResumeRepository``,
``PackageRepository``, and ``FollowUpRepository`` from
``careerops.application.applications``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.domain.application_packages import (
    ApplicationPackageVersion,
    PackageAttachment,
    PackageClaimVersion,
    PackageDiffEntry,
    RequirementGap,
)
from careerops.domain.applications import (
    Application,
    ApplicationEvent,
    ApplicationEventSource,
    ApplicationEventType,
    ApplicationPackage,
    ApplicationState,
    ConfirmationStatus,
    FollowUpReminder,
    FollowUpState,
    PackageApprovalState,
    PackageClaim,
    ResumeParseStatus,
    ResumeVersion,
    SubmissionChannel,
)
from careerops.infrastructure.database.schema import (
    application_lifecycle_events,
    application_package_versions,
    application_packages,
    applications,
    follow_up_reminders,
    resume_versions,
)


def _encode_cursor(created_at: datetime, row_id: UUID) -> str:
    """Encode (created_at, id) into an opaque cursor string."""
    import base64

    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{row_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode an opaque cursor back to (created_at, id)."""
    import base64

    decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_str, id_str = decoded.rsplit("|", 1)
    return datetime.fromisoformat(ts_str), UUID(id_str)


def _row_to_application(row: sa.RowMapping) -> Application:
    # 2.7: submission_channel may be NULL for legacy rows; coerce to None
    # rather than letting SubmissionChannel(None) raise.
    raw_channel = row.get("submission_channel")
    submission_channel: SubmissionChannel | None = None
    if raw_channel:
        submission_channel = SubmissionChannel(str(raw_channel))
    return Application(
        id=row["id"],
        candidate_id=row["candidate_id"],
        canonical_job_id=row["canonical_job_id"],
        state=ApplicationState(row["state"]),
        apply_url=row["apply_url"],
        # 2.6/2.7 additive linkage — all nullable for backfill.
        cycle_id=row.get("cycle_id"),
        submission_channel=submission_channel,
        package_version_id=row.get("package_version_id"),
        payload_hash=row.get("payload_hash"),
        provider_kind=row.get("provider_kind"),
        provider_message_id=row.get("provider_message_id"),
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
        # 2.4 additive lifecycle fields. Defaults keep older callers valid
        # if they read from a row that pre-dates the columns, but the new
        # schema always returns these.
        parse_status=ResumeParseStatus(str(row.get("parse_status") or "pending")),
        confirmation_status=ConfirmationStatus(
            str(row.get("confirmation_status") or "unconfirmed")
        ),
        source_reference=str(row.get("source_reference") or ""),
        parsed_at=row.get("parsed_at"),
        confirmed_at=row.get("confirmed_at"),
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


def _package_version_claims_to_json(
    claims: tuple[PackageClaimVersion, ...],
) -> list[dict[str, object]]:
    return [
        {"claim_text": c.claim_text, "evidence_ids": [str(e) for e in c.evidence_ids]}
        for c in claims
    ]


def _attachments_to_json(attachments: tuple[PackageAttachment, ...]) -> list[dict[str, object]]:
    return [
        {
            "name": a.name,
            "content_hash": a.content_hash,
            "media_type": a.media_type,
            "size_bytes": a.size_bytes,
        }
        for a in attachments
    ]


def _diff_to_json(diff: tuple[PackageDiffEntry, ...]) -> list[dict[str, object]]:
    return [
        {
            "section": d.section,
            "original_text": d.original_text,
            "proposed_text": d.proposed_text,
            "evidence_ids": [str(e) for e in d.evidence_ids],
            "source": d.source,
        }
        for d in diff
    ]


def _requirement_gaps_to_json(gaps: tuple[RequirementGap, ...]) -> list[dict[str, object]]:
    return [
        {
            "requirement_name": g.requirement_name,
            "match_level": g.match_level,
            "reason": g.reason,
            "rules_version": g.rules_version,
        }
        for g in gaps
    ]


def _row_to_package_version(row: sa.RowMapping) -> ApplicationPackageVersion:
    raw_claims: list[dict[str, Any]] = row["claims"] or []
    claims = tuple(
        PackageClaimVersion(
            claim_text=c["claim_text"],
            evidence_ids=tuple(UUID(e) for e in c.get("evidence_ids", ())),
        )
        for c in raw_claims
    )
    raw_attachments: list[dict[str, Any]] = row["attachments"] or []
    attachments = tuple(
        PackageAttachment(
            name=a["name"],
            content_hash=a["content_hash"],
            media_type=a.get("media_type", ""),
            size_bytes=a.get("size_bytes", 0),
        )
        for a in raw_attachments
    )
    raw_diff: list[dict[str, Any]] = row["diff"] or []
    diff = tuple(
        PackageDiffEntry(
            section=d["section"],
            original_text=d.get("original_text", ""),
            proposed_text=d.get("proposed_text", ""),
            evidence_ids=tuple(UUID(e) for e in d.get("evidence_ids", ())),
            source=d.get("source", "user"),
        )
        for d in raw_diff
    )
    raw_gaps: list[dict[str, Any]] = row["requirement_gaps"] or []
    gaps = tuple(
        RequirementGap(
            requirement_name=g["requirement_name"],
            match_level=g["match_level"],
            reason=g.get("reason", ""),
            rules_version=g.get("rules_version", ""),
        )
        for g in raw_gaps
    )
    return ApplicationPackageVersion(
        id=row["id"],
        application_id=row["application_id"],
        version_number=row["version_number"],
        resume_version_id=row["resume_version_id"],
        job_version_id=row.get("job_version_id"),
        profile_version_id=row.get("profile_version_id"),
        cover_letter_text=row["cover_letter_text"],
        notes=row["notes"],
        answers=row["answers"] or {},
        claims=claims,
        attachments=attachments,
        diff=diff,
        requirement_gaps=gaps,
        payload_hash=row.get("payload_hash"),
        approval_state=PackageApprovalState(row["approval_state"]),
        approved_at=row["approved_at"],
        approved_by=row.get("approved_by", ""),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


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
            # 2.6/2.7 additive linkage. All nullable; legacy rows keep NULL.
            "cycle_id": application.cycle_id,
            "submission_channel": (
                application.submission_channel.value
                if application.submission_channel is not None
                else None
            ),
            "package_version_id": application.package_version_id,
            "payload_hash": application.payload_hash,
            "provider_kind": application.provider_kind,
            "provider_message_id": application.provider_message_id,
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
                    "cycle_id": values["cycle_id"],
                    "submission_channel": values["submission_channel"],
                    "package_version_id": values["package_version_id"],
                    "payload_hash": values["payload_hash"],
                    "provider_kind": values["provider_kind"],
                    "provider_message_id": values["provider_message_id"],
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
                # 2.4 lifecycle fields
                parse_status=version.parse_status.value,
                confirmation_status=version.confirmation_status.value,
                source_reference=version.source_reference,
                parsed_at=version.parsed_at,
                confirmed_at=version.confirmed_at,
            )
            .on_conflict_do_update(
                index_elements=[resume_versions.c.id],
                set_={
                    "version_number": version.version_number,
                    "file_reference": version.file_reference,
                    "content_hash": version.content_hash,
                    "target_type": version.target_type,
                    "human_confirmed": version.human_confirmed,
                    "parse_status": version.parse_status.value,
                    "confirmation_status": version.confirmation_status.value,
                    "source_reference": version.source_reference,
                    "parsed_at": version.parsed_at,
                    "confirmed_at": version.confirmed_at,
                },
            )
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    # -- ResumeRepository: content-addressed dedupe (task 2.4) --------------

    def find_resume_by_content_hash(
        self, candidate_id: UUID, content_hash: str
    ) -> ResumeVersion | None:
        """Return the existing resume version for a content hash, if any.

        Backs content-addressed dedupe (task 2.4): when the same resume bytes
        are registered twice for the same candidate, the existing version is
        returned instead of creating a duplicate row + duplicate blob. The
        service layer decides whether to surface the existing version or
        register a linked duplicate (new version_number, same file_reference).
        """
        if not content_hash:
            return None
        stmt = (
            sa.select(resume_versions)
            .where(
                sa.and_(
                    resume_versions.c.candidate_id == candidate_id,
                    resume_versions.c.content_hash == content_hash,
                )
            )
            .order_by(resume_versions.c.version_number.desc())
            .limit(1)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_resume(row) if row else None

    def find_resume_by_id(self, candidate_id: UUID, version_id: UUID) -> ResumeVersion | None:
        """Return one resume version, scoped by candidate ownership."""
        stmt = sa.select(resume_versions).where(
            sa.and_(
                resume_versions.c.id == version_id,
                resume_versions.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_resume(row) if row else None

    def find_eligible_resumes(self, candidate_id: UUID, *, limit: int = 50) -> list[ResumeVersion]:
        """Return resumes eligible for application packages.

        A resume is eligible only when ``parse_status = parsed`` and
        ``confirmation_status = confirmed`` (career-profile-and-resume spec).
        Model output cannot change either status (Iron Rule 6).
        """
        stmt = (
            sa.select(resume_versions)
            .where(
                sa.and_(
                    resume_versions.c.candidate_id == candidate_id,
                    resume_versions.c.parse_status == ResumeParseStatus.PARSED.value,
                    resume_versions.c.confirmation_status == ConfirmationStatus.CONFIRMED.value,
                )
            )
            .order_by(resume_versions.c.version_number.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_resume(row) for row in rows]

    def list_resumes(self, candidate_id: UUID, *, limit: int = 50) -> list[ResumeVersion]:
        """Return all resume versions for the candidate, newest version_number first.

        Additive read (Section 3 task 3.3) consumed by the resume service's
        history view; scoped by the server-resolved ``candidate_id`` like every
        other resume read.
        """
        stmt = (
            sa.select(resume_versions)
            .where(resume_versions.c.candidate_id == candidate_id)
            .order_by(resume_versions.c.version_number.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_resume(row) for row in rows]

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

    # -- Application package versions (Section 8) --------------------------

    def find_latest_package_version(self, application_id: UUID) -> ApplicationPackageVersion | None:
        """Return the highest-numbered package version for the application."""
        stmt = (
            sa.select(application_package_versions)
            .where(application_package_versions.c.application_id == application_id)
            .order_by(application_package_versions.c.version_number.desc())
            .limit(1)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_package_version(row) if row else None

    def find_package_version(
        self, application_id: UUID, version_id: UUID
    ) -> ApplicationPackageVersion | None:
        stmt = sa.select(application_package_versions).where(
            sa.and_(
                application_package_versions.c.application_id == application_id,
                application_package_versions.c.id == version_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_package_version(row) if row else None

    def list_package_versions(
        self, application_id: UUID, *, limit: int = 50
    ) -> list[ApplicationPackageVersion]:
        stmt = (
            sa.select(application_package_versions)
            .where(application_package_versions.c.application_id == application_id)
            .order_by(application_package_versions.c.version_number.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_package_version(row) for row in rows]

    def save_package_version(self, version: ApplicationPackageVersion) -> None:
        """Insert a new package version row (immutable; copy-on-write).

        Version numbers are assigned by the service (max+1) and enforced
        unique per application by ``uq_application_package_versions_app_version``.
        ``on_conflict_do_update`` only reconcils the same ``id`` on retry; it
        never rewrites a different version in place.
        """
        stmt = pg_insert(application_package_versions).values(
            id=version.id,
            application_id=version.application_id,
            version_number=version.version_number,
            resume_version_id=version.resume_version_id,
            job_version_id=version.job_version_id,
            profile_version_id=version.profile_version_id,
            cover_letter_text=version.cover_letter_text,
            notes=version.notes,
            answers=version.answers,
            claims=_package_version_claims_to_json(version.claims),
            attachments=_attachments_to_json(version.attachments),
            diff=_diff_to_json(version.diff),
            requirement_gaps=_requirement_gaps_to_json(version.requirement_gaps),
            payload_hash=version.payload_hash,
            approval_state=version.approval_state.value,
            approved_at=version.approved_at,
            approved_by=version.approved_by,
            updated_at=version.updated_at,
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
    ) -> dict[str, object]:
        # Build filter conditions.
        filters: list[ColumnElement[bool]] = []
        if candidate_id is not None:
            filters.append(applications.c.candidate_id == UUID(candidate_id))
        if state is not None:
            filters.append(applications.c.state == state)

        with self._engine.begin() as conn:
            # Total count with filters.
            count_stmt = select(func.count()).select_from(applications)
            if filters:
                count_stmt = count_stmt.where(*filters)
            total = conn.execute(count_stmt).scalar_one()

            # Fetch limit+1 for has_more detection.
            # Descending order: created_at DESC, id DESC.  Cursor encodes
            # (created_at, id) of the last row; next page is rows strictly
            # "less than" the cursor in the same ordering.
            stmt = (
                select(applications)
                .order_by(applications.c.created_at.desc(), applications.c.id.desc())
                .limit(limit + 1)
            )
            if filters:
                stmt = stmt.where(*filters)
            if cursor is not None:
                cur_created, cur_id = _decode_cursor(cursor)
                stmt = stmt.where(
                    or_(
                        applications.c.created_at < cur_created,
                        and_(
                            applications.c.created_at == cur_created,
                            applications.c.id < cur_id,
                        ),
                    )
                )
            rows = conn.execute(stmt).mappings().all()

        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more else None

        return {
            "items": [
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
            ],
            "total": total,
            "next_cursor": next_cursor,
        }
