from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy.engine import Engine

from careerops.application.gmail_readonly import GmailMetadataSignal
from careerops.infrastructure.database.gmail_readonly import PostgresGmailReadonlyDomainProcessor
from careerops.infrastructure.gmail.worker import GmailMailboxSyncJob, GmailMessageBatch

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _job() -> GmailMailboxSyncJob:
    return GmailMailboxSyncJob(
        mailbox_id=UUID("00000000-0000-0000-0000-00000000f101"),
        owner_user_id=UUID("00000000-0000-0000-0000-00000000f102"),
        candidate_id=UUID("00000000-0000-0000-0000-00000000f103"),
        credential_handle="00000000-0000-0000-0000-00000000f104",
        account_subject="candidate@example.com",
        last_history_id="100",
        resync_required=False,
        sync_run_id=UUID("00000000-0000-0000-0000-00000000f105"),
        lease_token=UUID("00000000-0000-0000-0000-00000000f106"),
    )


def _signal(*, subject: str, sender: str, snippet: str) -> GmailMetadataSignal:
    return GmailMetadataSignal(
        provider_message_id="msg-101",
        provider_thread_id="thread-101",
        provider_history_id="101",
        account_subject="candidate@example.com",
        headers={"subject": subject, "from": sender},
        label_ids=("INBOX",),
        snippet=snippet,
        received_at=NOW,
    )


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, object]] = []

    def execute(self, statement: object, parameters: Mapping[str, object] | None = None) -> None:
        del statement
        self.calls.append(parameters or {})


class RecordingBegin:
    def __init__(self, connection: RecordingConnection) -> None:
        self._connection = connection

    def __enter__(self) -> RecordingConnection:
        return self._connection

    def __exit__(self, *exc: object) -> None:
        return None


class RecordingEngine:
    def __init__(self) -> None:
        self.connection = RecordingConnection()

    def begin(self) -> RecordingBegin:
        return RecordingBegin(self.connection)


def test_domain_processor_does_not_persist_unrelated_messages() -> None:
    engine = RecordingEngine()
    processor = PostgresGmailReadonlyDomainProcessor(cast("Engine", engine))
    batch = GmailMessageBatch(
        messages=(
            _signal(
                subject="Weekend dinner",
                sender="friend@example.com",
                snippet="See you tonight at 7.",
            ),
        ),
        high_water_history_id="101",
    )

    result = processor.process(job=_job(), batch=batch, now=NOW)

    assert result.processed_messages == 0
    assert result.review_proposals == 0
    assert engine.connection.calls == []


def test_domain_processor_persists_recruiting_signal_as_pending_review() -> None:
    engine = RecordingEngine()
    processor = PostgresGmailReadonlyDomainProcessor(cast("Engine", engine))
    batch = GmailMessageBatch(
        messages=(
            _signal(
                subject="Interview availability",
                sender="recruiter@greenhouse.io",
                snippet="Can you schedule a call with our recruiter?",
            ),
        ),
        high_water_history_id="101",
    )

    result = processor.process(job=_job(), batch=batch, now=NOW)

    assert result.processed_messages == 1
    assert result.review_proposals == 1
    assert len(engine.connection.calls) == 1
    parameters = cast("dict[str, Any]", engine.connection.calls[0])
    assert parameters["classification"] == "interview_invitation"
    assert parameters["relevance"] == "relevant"
    proposal = cast("dict[str, Any]", parameters["proposal_payload_json"])
    assert proposal["proposal_kind"] == "review_only"
    assert proposal["review_priority"] == "high"
