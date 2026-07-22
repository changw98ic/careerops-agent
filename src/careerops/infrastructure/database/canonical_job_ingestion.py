from __future__ import annotations

from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection

from careerops.application.canonical_job_ingestion import (
    CanonicalJobIngestionRecord,
    CanonicalJobIngestionResult,
)
from careerops.infrastructure.database.schema import (
    canonical_jobs,
    companies,
    crawler_job_deduplication_keys,
    crawler_job_ingestion_evidence,
    job_merge_decisions,
    job_posting_assignments,
    job_posting_versions,
    job_postings,
    job_sources,
)

_ALGORITHM_VERSION = "canonical-job-ingestion:v1"


class CanonicalJobIngestionRepositoryError(RuntimeError):
    pass


class PostgresCanonicalJobIngestionRepository:
    """Canonical job ingestion operations scoped to one explicit transaction."""

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("canonical job ingestion requires an explicit transaction")
        self._connection = connection

    def ingest_public_ats_job(
        self,
        record: CanonicalJobIngestionRecord,
    ) -> CanonicalJobIngestionResult:
        self._lock_dedupe_key(record.dedupe_key)
        company_id = self._upsert_company(record)
        source_id = self._upsert_source(record, company_id=company_id)
        posting_id = self._upsert_posting(record, source_id=source_id)
        version_id, inserted_version = self._upsert_version(record, posting_id=posting_id)
        existing_assignment = self._existing_assignment(posting_id)
        if existing_assignment is None:
            canonical_job_id = self._canonical_job_for_dedupe_key(
                record,
                company_id=company_id,
            )
            merge_decision_id = self._ensure_assignment(
                record,
                posting_id=posting_id,
                canonical_job_id=canonical_job_id,
            )
        else:
            canonical_job_id, merge_decision_id = existing_assignment
            self._bind_dedupe_key(
                record,
                company_id=company_id,
                canonical_job_id=canonical_job_id,
            )
        evidence_id = self._insert_evidence(
            record,
            posting_id=posting_id,
            version_id=version_id,
            canonical_job_id=canonical_job_id,
        )
        return CanonicalJobIngestionResult(
            company_id=company_id,
            source_id=source_id,
            posting_id=posting_id,
            version_id=version_id,
            canonical_job_id=canonical_job_id,
            merge_decision_id=merge_decision_id,
            evidence_id=evidence_id,
            dedupe_key=record.dedupe_key,
            inserted_version=inserted_version,
        )

    def _upsert_company(self, record: CanonicalJobIngestionRecord) -> UUID:
        company_id = uuid4()
        inserted = self._connection.scalar(
            postgresql.insert(companies)
            .values(
                id=company_id,
                name=record.row.company_name,
                normalized_name=record.row.normalized_company_name,
                official_domains=[record.row.company_domain],
                terms_status="unknown",
            )
            .on_conflict_do_nothing(index_elements=[companies.c.normalized_name])
            .returning(companies.c.id)
        )
        if isinstance(inserted, UUID):
            return inserted
        row = self._connection.scalar(
            sa.select(companies.c.id).where(
                companies.c.normalized_name == record.row.normalized_company_name
            )
        )
        if not isinstance(row, UUID):
            raise CanonicalJobIngestionRepositoryError("company upsert did not yield a row")
        return row

    def _upsert_source(self, record: CanonicalJobIngestionRecord, *, company_id: UUID) -> UUID:
        source_id = uuid4()
        inserted = self._connection.scalar(
            postgresql.insert(job_sources)
            .values(
                id=source_id,
                company_id=company_id,
                source_type=record.row.source_type,
                source_identifier=record.row.source_identifier,
                base_url=record.row.base_url,
                state="pending_review",
                verified_at=None,
                last_discovery_at=record.row.captured_at,
            )
            .on_conflict_do_update(
                constraint="uq_job_sources_company_type_identifier",
                set_={
                    "last_discovery_at": record.row.captured_at,
                    "updated_at": sa.func.now(),
                },
            )
            .returning(job_sources.c.id)
        )
        if not isinstance(inserted, UUID):
            raise CanonicalJobIngestionRepositoryError("source upsert did not yield a row")
        return inserted

    def _upsert_posting(self, record: CanonicalJobIngestionRecord, *, source_id: UUID) -> UUID:
        posting_id = uuid4()
        inserted = self._connection.scalar(
            postgresql.insert(job_postings)
            .values(
                id=posting_id,
                source_id=source_id,
                external_id=record.row.external_id,
                canonical_url=record.row.canonical_url,
                source_state="active",
                first_seen_at=record.row.captured_at,
                last_seen_at=record.row.captured_at,
            )
            .on_conflict_do_update(
                constraint="uq_job_postings_source_external_id",
                set_={
                    "source_state": "active",
                    "last_seen_at": record.row.captured_at,
                    "closed_at": None,
                    "updated_at": sa.func.now(),
                },
            )
            .returning(job_postings.c.id)
        )
        if not isinstance(inserted, UUID):
            raise CanonicalJobIngestionRepositoryError("posting upsert did not yield a row")
        return inserted

    def _upsert_version(
        self,
        record: CanonicalJobIngestionRecord,
        *,
        posting_id: UUID,
    ) -> tuple[UUID, bool]:
        version_id = uuid4()
        inserted = self._connection.scalar(
            postgresql.insert(job_posting_versions)
            .values(
                id=version_id,
                job_posting_id=posting_id,
                raw_snapshot_id=None,
                content_hash=record.row.effective_content_hash,
                source_url=record.row.source_url,
                parser_version=record.row.parser_version,
                structured_data=dict(record.structured_payload),
                changed_fields=[],
                captured_at=record.row.captured_at,
            )
            .on_conflict_do_nothing(constraint="uq_job_posting_versions_posting_hash")
            .returning(job_posting_versions.c.id)
        )
        if isinstance(inserted, UUID):
            return inserted, True
        existing = self._connection.scalar(
            sa.select(job_posting_versions.c.id).where(
                job_posting_versions.c.job_posting_id == posting_id,
                job_posting_versions.c.content_hash == record.row.effective_content_hash,
            )
        )
        if not isinstance(existing, UUID):
            raise CanonicalJobIngestionRepositoryError("version upsert did not yield a row")
        return existing, False

    def _canonical_job_for_dedupe_key(
        self,
        record: CanonicalJobIngestionRecord,
        *,
        company_id: UUID,
    ) -> UUID:
        existing = (
            self._connection.execute(
                sa.select(
                    crawler_job_deduplication_keys.c.company_id,
                    crawler_job_deduplication_keys.c.canonical_job_id,
                ).where(
                    crawler_job_deduplication_keys.c.dedupe_policy == record.dedupe_policy.value,
                    crawler_job_deduplication_keys.c.dedupe_key_sha256 == record.dedupe_key,
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["company_id"] != company_id:
                raise CanonicalJobIngestionRepositoryError(
                    "dedupe key is already bound to a different company"
                )
            canonical_job_id = existing["canonical_job_id"]
            if not isinstance(canonical_job_id, UUID):
                raise CanonicalJobIngestionRepositoryError(
                    "dedupe key has an invalid canonical job identity"
                )
            return canonical_job_id
        canonical_job_id = uuid4()
        self._connection.execute(
            sa.insert(canonical_jobs).values(
                id=canonical_job_id,
                company_id=company_id,
                canonical_title=record.row.title,
                normalized_title=record.row.normalized_title,
                aggregate_state="active",
            )
        )
        self._bind_dedupe_key(
            record,
            company_id=company_id,
            canonical_job_id=canonical_job_id,
        )
        return canonical_job_id

    def _bind_dedupe_key(
        self,
        record: CanonicalJobIngestionRecord,
        *,
        company_id: UUID,
        canonical_job_id: UUID,
    ) -> None:
        self._connection.execute(
            postgresql.insert(crawler_job_deduplication_keys)
            .values(
                id=uuid4(),
                dedupe_policy=record.dedupe_policy.value,
                dedupe_key_sha256=record.dedupe_key,
                company_id=company_id,
                canonical_job_id=canonical_job_id,
                rule=record.dedupe_rule,
                algorithm_version=_ALGORITHM_VERSION,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    crawler_job_deduplication_keys.c.dedupe_policy,
                    crawler_job_deduplication_keys.c.dedupe_key_sha256,
                ]
            )
        )
        binding = (
            self._connection.execute(
                sa.select(
                    crawler_job_deduplication_keys.c.company_id,
                    crawler_job_deduplication_keys.c.canonical_job_id,
                ).where(
                    crawler_job_deduplication_keys.c.dedupe_policy == record.dedupe_policy.value,
                    crawler_job_deduplication_keys.c.dedupe_key_sha256 == record.dedupe_key,
                )
            )
            .mappings()
            .one_or_none()
        )
        if binding is None:
            raise CanonicalJobIngestionRepositoryError("dedupe key did not yield a binding")
        if binding["company_id"] != company_id or binding["canonical_job_id"] != canonical_job_id:
            raise CanonicalJobIngestionRepositoryError(
                "dedupe key conflicts with an existing canonical job"
            )

    def _existing_assignment(self, posting_id: UUID) -> tuple[UUID, UUID] | None:
        existing = (
            self._connection.execute(
                sa.select(
                    job_posting_assignments.c.canonical_job_id,
                    job_posting_assignments.c.decision_id,
                ).where(job_posting_assignments.c.job_posting_id == posting_id)
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            return None
        canonical_job_id = existing["canonical_job_id"]
        decision_id = existing["decision_id"]
        if not isinstance(canonical_job_id, UUID) or not isinstance(decision_id, UUID):
            raise CanonicalJobIngestionRepositoryError("posting assignment has invalid identity")
        return canonical_job_id, decision_id

    def _ensure_assignment(
        self,
        record: CanonicalJobIngestionRecord,
        *,
        posting_id: UUID,
        canonical_job_id: UUID,
    ) -> UUID | None:
        existing = (
            self._connection.execute(
                sa.select(
                    job_posting_assignments.c.canonical_job_id,
                    job_posting_assignments.c.decision_id,
                ).where(job_posting_assignments.c.job_posting_id == posting_id)
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["canonical_job_id"] != canonical_job_id:
                raise CanonicalJobIngestionRepositoryError(
                    "posting is already assigned to a different canonical job"
                )
            return cast("UUID", existing["decision_id"])
        decision_id = uuid4()
        self._connection.execute(
            sa.insert(job_merge_decisions).values(
                id=decision_id,
                job_posting_id=posting_id,
                from_canonical_job_id=None,
                to_canonical_job_id=canonical_job_id,
                decision_kind="merge",
                rule=record.dedupe_rule,
                reason="deterministic crawler ingestion",
                score=1,
                actor_type="rule",
                actor_id="canonical-job-ingestion",
                algorithm_version=_ALGORITHM_VERSION,
            )
        )
        self._connection.execute(
            sa.insert(job_posting_assignments).values(
                job_posting_id=posting_id,
                canonical_job_id=canonical_job_id,
                decision_id=decision_id,
                assigned_at=record.row.captured_at,
            )
        )
        self._connection.execute(
            sa.update(canonical_jobs)
            .where(
                canonical_jobs.c.id == canonical_job_id,
                canonical_jobs.c.primary_posting_id.is_(None),
            )
            .values(primary_posting_id=posting_id, updated_at=sa.func.now())
        )
        return decision_id

    def _insert_evidence(
        self,
        record: CanonicalJobIngestionRecord,
        *,
        posting_id: UUID,
        version_id: UUID,
        canonical_job_id: UUID,
    ) -> UUID:
        evidence = record.evidence
        provenance_json = {
            "adapter": record.provenance.adapter,
            "output_artifact_sha256": evidence.output_artifact_sha256,
            "provenance_policy": evidence.provenance_policy,
            "registry_id": str(record.provenance.registry_id),
            "source_id": record.provenance.source_id,
            "structured_data": dict(record.structured_payload),
        }
        evidence_id = uuid4()
        inserted = self._connection.scalar(
            postgresql.insert(crawler_job_ingestion_evidence)
            .values(
                id=evidence_id,
                source_row_id=evidence.crawler_source_row_id,
                run_id=evidence.crawler_run_id,
                run_event_id=evidence.run_event_id,
                job_posting_id=posting_id,
                job_posting_version_id=version_id,
                canonical_job_id=canonical_job_id,
                source_url=evidence.source_url,
                captured_at=evidence.captured_at,
                content_hash=evidence.content_hash,
                parser_version=evidence.parser_version,
                dedupe_policy=evidence.dedupe_policy.value,
                dedupe_key_sha256=evidence.dedupe_key,
                dedupe_rule=evidence.rule,
                provenance_json=provenance_json,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    crawler_job_ingestion_evidence.c.run_id,
                    crawler_job_ingestion_evidence.c.job_posting_version_id,
                    crawler_job_ingestion_evidence.c.dedupe_key_sha256,
                ]
            )
            .returning(crawler_job_ingestion_evidence.c.id)
        )
        if isinstance(inserted, UUID):
            return inserted
        existing = self._connection.scalar(
            sa.select(crawler_job_ingestion_evidence.c.id).where(
                crawler_job_ingestion_evidence.c.run_id == evidence.crawler_run_id,
                crawler_job_ingestion_evidence.c.job_posting_version_id == version_id,
                crawler_job_ingestion_evidence.c.dedupe_key_sha256 == evidence.dedupe_key,
            )
        )
        if not isinstance(existing, UUID):
            raise CanonicalJobIngestionRepositoryError("evidence insert did not yield a row")
        return existing

    def _lock_dedupe_key(self, dedupe_key: str) -> None:
        self._connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:dedupe_key, 0))"),
            {"dedupe_key": dedupe_key},
        )


def upsert_public_ats_core_statements(
    record: CanonicalJobIngestionRecord,
    *,
    company_id: UUID,
    source_id: UUID,
    posting_id: UUID,
    version_id: UUID,
    canonical_job_id: UUID,
    merge_decision_id: UUID,
    evidence_id: UUID,
) -> tuple[sa.ClauseElement, ...]:
    return (
        postgresql.insert(companies).values(
            id=company_id,
            name=record.row.company_name,
            normalized_name=record.row.normalized_company_name,
            official_domains=[record.row.company_domain],
            terms_status="unknown",
        ),
        postgresql.insert(job_sources).values(
            id=source_id,
            company_id=company_id,
            source_type=record.row.source_type,
            source_identifier=record.row.source_identifier,
            base_url=record.row.base_url,
            state="pending_review",
            verified_at=None,
            last_discovery_at=record.row.captured_at,
        ),
        postgresql.insert(job_postings).values(
            id=posting_id,
            source_id=source_id,
            external_id=record.row.external_id,
            canonical_url=record.row.canonical_url,
            source_state="active",
            first_seen_at=record.row.captured_at,
            last_seen_at=record.row.captured_at,
        ),
        postgresql.insert(job_posting_versions).values(
            id=version_id,
            job_posting_id=posting_id,
            content_hash=record.row.effective_content_hash,
            source_url=record.row.source_url,
            parser_version=record.row.parser_version,
            structured_data=dict(record.structured_payload),
            changed_fields=[],
            captured_at=record.row.captured_at,
        ),
        sa.insert(canonical_jobs).values(
            id=canonical_job_id,
            company_id=company_id,
            canonical_title=record.row.title,
            normalized_title=record.row.normalized_title,
            aggregate_state="active",
        ),
        postgresql.insert(crawler_job_deduplication_keys).values(
            id=uuid4(),
            dedupe_policy=record.dedupe_policy.value,
            dedupe_key_sha256=record.dedupe_key,
            company_id=company_id,
            canonical_job_id=canonical_job_id,
            rule=record.dedupe_rule,
            algorithm_version=_ALGORITHM_VERSION,
        ),
        sa.insert(job_merge_decisions).values(
            id=merge_decision_id,
            job_posting_id=posting_id,
            to_canonical_job_id=canonical_job_id,
            decision_kind="merge",
            rule=record.dedupe_rule,
            reason="deterministic crawler ingestion",
            score=1,
            actor_type="rule",
            actor_id="canonical-job-ingestion",
            algorithm_version="canonical-job-ingestion:v1",
        ),
        sa.insert(crawler_job_ingestion_evidence).values(
            id=evidence_id,
            source_row_id=record.provenance.crawler_source_row_id,
            run_id=record.provenance.crawler_run_id,
            run_event_id=record.provenance.run_event_id,
            job_posting_id=posting_id,
            job_posting_version_id=version_id,
            canonical_job_id=canonical_job_id,
            source_url=record.row.source_url,
            captured_at=record.row.captured_at,
            content_hash=record.row.effective_content_hash,
            parser_version=record.row.parser_version,
            dedupe_policy=record.dedupe_policy.value,
            dedupe_key_sha256=record.dedupe_key,
            dedupe_rule=record.dedupe_rule,
            provenance_json={
                "adapter": record.provenance.adapter,
                "output_artifact_sha256": record.provenance.output_artifact_sha256,
                "provenance_policy": record.provenance_policy,
                "registry_id": str(record.provenance.registry_id),
                "source_id": record.provenance.source_id,
                "structured_data": dict(record.structured_payload),
            },
        ),
    )


def compile_query_for_test(
    statement: sa.ClauseElement,
    *,
    literal_binds: bool = True,
) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": literal_binds},
        )
    )


__all__ = [
    "CanonicalJobIngestionRepositoryError",
    "PostgresCanonicalJobIngestionRepository",
    "compile_query_for_test",
    "crawler_job_deduplication_keys",
    "crawler_job_ingestion_evidence",
    "upsert_public_ats_core_statements",
]
