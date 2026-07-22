from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine, RowMapping

from careerops.application.gmail_readonly import (
    GmailEvidenceRedactor,
    GmailMetadataSignal,
    GmailRecruitingClassification,
    GmailRecruitingClassifier,
)
from careerops.infrastructure.database.schema import gmail_accounts, gmail_sync_runs
from careerops.infrastructure.gmail.worker import (
    GmailDomainProcessingResult,
    GmailMailboxSyncJob,
    GmailMessageBatch,
)

_OWNER = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")


class PostgresGmailMailboxSyncRepository:
    """Mailbox-role repository backed by 0012 SECURITY DEFINER functions."""

    def __init__(self, engine: Engine, *, lease_seconds: int = 300) -> None:
        if lease_seconds < 1 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        self._engine = engine
        self._lease_seconds = lease_seconds

    def claim_due(
        self, *, owner: str, now: datetime, limit: int
    ) -> tuple[GmailMailboxSyncJob, ...]:
        _validate_owner(owner)
        _validate_aware(now, "now")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with self._engine.begin() as connection:
            rows = (
                connection.execute(
                    sa.text(
                        "SELECT * FROM careerops.gmail_readonly_claim_sync_runs("
                        ":owner, :lease_seconds, :limit)"
                    ),
                    {
                        "owner": owner,
                        "lease_seconds": self._lease_seconds,
                        "limit": limit,
                    },
                )
                .mappings()
                .all()
            )
        return tuple(_row_to_job(row) for row in rows)

    def count_due(self, *, now: datetime) -> int:
        _validate_aware(now, "now")
        with self._engine.begin() as connection:
            value = connection.scalar(
                sa.select(sa.func.count())
                .select_from(
                    gmail_sync_runs.join(
                        gmail_accounts,
                        gmail_sync_runs.c.gmail_account_id == gmail_accounts.c.id,
                    )
                )
                .where(
                    gmail_accounts.c.provider == "gmail",
                    gmail_accounts.c.status.in_(("active", "sync_required")),
                    gmail_sync_runs.c.status == "queued",
                )
            )
        return int(value or 0)

    def mark_succeeded(
        self,
        *,
        job: GmailMailboxSyncJob,
        batch: GmailMessageBatch,
        processing: GmailDomainProcessingResult,
        owner: str,
        now: datetime,
    ) -> None:
        del owner
        _validate_aware(now, "now")
        if not batch.complete:
            raise ValueError("cannot advance Gmail history while a page token remains")
        with self._engine.begin() as connection:
            connection.execute(
                sa.text(
                    "SELECT careerops.gmail_readonly_complete_sync_run("
                    ":run_id, :lease_token, :history_end_id, :next_page_token, :message_count)"
                ),
                {
                    "run_id": job.sync_run_id,
                    "lease_token": job.lease_token,
                    "history_end_id": batch.high_water_history_id,
                    "next_page_token": batch.next_page_token,
                    "message_count": processing.processed_messages,
                },
            )

    def mark_incomplete_page(
        self,
        *,
        job: GmailMailboxSyncJob,
        batch: GmailMessageBatch,
        processing: GmailDomainProcessingResult,
        now: datetime,
    ) -> None:
        _validate_aware(now, "now")
        self._defer(
            job=job,
            history_end_id=batch.high_water_history_id,
            next_page_token=batch.next_page_token,
            message_count=processing.processed_messages,
            error_code="GMAIL_PAGE_INCOMPLETE",
        )

    def mark_reauth_required(
        self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str
    ) -> None:
        _validate_aware(now, "now")
        self._fail(job=job, error_code=error_code)

    def mark_resync_required(
        self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str
    ) -> None:
        _validate_aware(now, "now")
        self._fail(job=job, error_code=error_code)

    def mark_retry(self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str) -> None:
        _validate_aware(now, "now")
        self._defer(
            job=job,
            history_end_id=job.anchor_history_id,
            next_page_token=job.next_page_token,
            message_count=0,
            error_code=error_code,
        )

    def mark_failed(self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str) -> None:
        _validate_aware(now, "now")
        self._fail(job=job, error_code=error_code)

    def _defer(
        self,
        *,
        job: GmailMailboxSyncJob,
        history_end_id: str | None,
        next_page_token: str | None,
        message_count: int,
        error_code: str,
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sa.text(
                    "SELECT careerops.gmail_readonly_defer_sync_run("
                    ":run_id, :lease_token, :history_end_id, :next_page_token, "
                    ":message_count, :error_code)"
                ),
                {
                    "run_id": job.sync_run_id,
                    "lease_token": job.lease_token,
                    "history_end_id": history_end_id,
                    "next_page_token": next_page_token,
                    "message_count": message_count,
                    "error_code": _bounded_error_code(error_code),
                },
            )

    def _fail(self, *, job: GmailMailboxSyncJob, error_code: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sa.text(
                    "SELECT careerops.gmail_readonly_fail_sync_run("
                    ":run_id, :lease_token, :error_code)"
                ),
                {
                    "run_id": job.sync_run_id,
                    "lease_token": job.lease_token,
                    "error_code": _bounded_error_code(error_code),
                },
            )


class PostgresGmailReadonlyDomainProcessor:
    """Persists Gmail metadata-only signals and review proposals via mailbox functions."""

    def __init__(
        self,
        engine: Engine,
        *,
        classifier: GmailRecruitingClassifier | None = None,
        redactor: GmailEvidenceRedactor | None = None,
    ) -> None:
        self._engine = engine
        self._classifier = classifier or GmailRecruitingClassifier()
        self._redactor = redactor or GmailEvidenceRedactor()

    def process(
        self,
        *,
        job: GmailMailboxSyncJob,
        batch: GmailMessageBatch,
        now: datetime,
    ) -> GmailDomainProcessingResult:
        _validate_aware(now, "now")
        processed = 0
        proposals = 0
        with self._engine.begin() as connection:
            for signal in batch.messages:
                decision = self._classifier.classify(signal, candidate_jobs=())
                if decision.classification is GmailRecruitingClassification.UNRELATED:
                    continue
                redacted = self._redactor.redact(signal)
                proposal_payload = _proposal_payload(
                    signal=signal,
                    decision_classification=_db_classification(decision.classification),
                    decision_confidence=decision.confidence,
                    reason_codes=decision.reason_codes,
                    review_priority=decision.review_priority.value,
                    redacted_excerpt=redacted.redacted_snippet,
                )
                if proposal_payload is not None:
                    proposals += 1
                connection.execute(
                    _record_message_statement(),
                    _record_message_parameters(
                        job=job,
                        signal=signal,
                        classification=_db_classification(decision.classification),
                        confidence=decision.confidence,
                        reason_codes=decision.reason_codes,
                        relevance=_relevance(decision.classification, decision.confidence),
                        redacted_excerpt=redacted.redacted_snippet,
                        proposal_payload=proposal_payload,
                    ),
                )
                processed += 1
        return GmailDomainProcessingResult(
            processed_messages=processed,
            review_proposals=proposals,
        )


def _row_to_job(row: RowMapping) -> GmailMailboxSyncJob:
    return GmailMailboxSyncJob(
        mailbox_id=cast("UUID", row["mailbox_id"]),
        owner_user_id=cast("UUID", row["owner_user_id"]),
        candidate_id=cast("UUID", row["candidate_id"]),
        credential_handle=cast("str", row["credential_handle"]),
        account_subject=cast("str", row["account_subject"]),
        last_history_id=cast("str | None", row["history_start_id"]),
        anchor_history_id=cast("str | None", row["anchor_history_id"]),
        resync_required=row["account_status"] == "sync_required",
        sync_run_id=cast("UUID", row["sync_run_id"]),
        lease_token=cast("UUID", row["lease_token"]),
        attempt_count=cast("int", row["attempt_count"]),
        next_page_token=cast("str | None", row["run_next_page_token"])
        or cast("str | None", row["account_next_page_token"]),
    )


def _record_message_statement() -> sa.TextClause:
    return sa.text(
        """
        SELECT careerops.gmail_readonly_record_message_signal(
            :run_id, :lease_token, :provider_message_id, :provider_thread_id,
            :provider_history_id, :message_sha256, :thread_sha256, :sender_sha256,
            :subject_sha256, :snippet_sha256, :received_at, :classification,
            :relevance, :confidence, :provenance_json, :signal_sha256,
            :redacted_excerpt, :label_ids_hash, :proposal_payload_json,
            :proposal_payload_sha256, :trace_id
        )
        """
    ).bindparams(
        sa.bindparam("provenance_json", type_=postgresql.JSONB),
        sa.bindparam("proposal_payload_json", type_=postgresql.JSONB),
    )


def _record_message_parameters(
    *,
    job: GmailMailboxSyncJob,
    signal: GmailMetadataSignal,
    classification: str,
    confidence: float,
    reason_codes: Sequence[str],
    relevance: str,
    redacted_excerpt: str,
    proposal_payload: Mapping[str, Any] | None,
) -> dict[str, object]:
    normalized_labels = tuple(sorted(signal.label_ids))
    provenance = {
        "source": "gmail.readonly",
        "account_subject_hash": _sha256_text(signal.account_subject),
        "reason_codes": list(reason_codes),
        "metadata_only": True,
    }
    proposal_hash = _canonical_hash(proposal_payload) if proposal_payload is not None else None
    return {
        "run_id": job.sync_run_id,
        "lease_token": job.lease_token,
        "provider_message_id": signal.provider_message_id,
        "provider_thread_id": signal.provider_thread_id,
        "provider_history_id": signal.provider_history_id,
        "message_sha256": _canonical_hash(
            {
                "provider_message_id": signal.provider_message_id,
                "provider_history_id": signal.provider_history_id,
                "subject": signal.subject,
                "snippet": signal.snippet,
            }
        ),
        "thread_sha256": _sha256_text(signal.provider_thread_id),
        "sender_sha256": _sha256_text(signal.sender) if signal.sender else None,
        "subject_sha256": _sha256_text(signal.subject),
        "snippet_sha256": _sha256_text(signal.snippet),
        "received_at": signal.received_at,
        "classification": classification,
        "relevance": relevance,
        "confidence": Decimal(str(round(confidence, 3))),
        "provenance_json": provenance,
        "signal_sha256": _canonical_hash(
            {
                "message_id": signal.provider_message_id,
                "labels": normalized_labels,
                "classification": classification,
            }
        ),
        "redacted_excerpt": redacted_excerpt[:500],
        "label_ids_hash": _canonical_hash(normalized_labels),
        "proposal_payload_json": proposal_payload,
        "proposal_payload_sha256": proposal_hash,
        "trace_id": f"gmail-sync:{job.sync_run_id}",
    }


def _proposal_payload(
    *,
    signal: GmailMetadataSignal,
    decision_classification: str,
    decision_confidence: float,
    reason_codes: Sequence[str],
    review_priority: str,
    redacted_excerpt: str,
) -> Mapping[str, Any] | None:
    if decision_classification == "unrelated":
        return None
    return {
        "proposal_kind": "review_only",
        "review_priority": review_priority,
        "version": "gmail-readonly-review.v1",
        "classification": decision_classification,
        "confidence": round(decision_confidence, 3),
        "reason_codes": list(reason_codes),
        "evidence": {
            "provider_message_id": signal.provider_message_id,
            "provider_thread_id": signal.provider_thread_id,
            "provider_history_id": signal.provider_history_id,
            "redacted_excerpt": redacted_excerpt[:500],
        },
    }


def _db_classification(classification: GmailRecruitingClassification) -> str:
    if classification is GmailRecruitingClassification.UNKNOWN_RECRUITING:
        return "unknown"
    return classification.value


def _relevance(classification: GmailRecruitingClassification, confidence: float) -> str:
    if classification is GmailRecruitingClassification.UNRELATED:
        return "non_relevant"
    return "relevant" if confidence >= 0.5 else "uncertain"


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_owner(owner: str) -> None:
    if _OWNER.fullmatch(owner) is None:
        raise ValueError("owner must be a bounded actor identifier")


def _validate_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _bounded_error_code(error_code: str) -> str:
    if _ERROR_CODE.fullmatch(error_code) is None:
        raise ValueError("error_code must be a bounded machine code")
    return error_code


__all__: Sequence[str] = (
    "PostgresGmailMailboxSyncRepository",
    "PostgresGmailReadonlyDomainProcessor",
)
