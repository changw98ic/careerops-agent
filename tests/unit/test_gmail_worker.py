from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from careerops.application.gmail_readonly import GmailMetadataSignal
from careerops.infrastructure.gmail.client import (
    GmailHistoryPage,
    GmailListedMessage,
    GmailMessageListPage,
    GmailMetadataMessage,
    GmailProfile,
    GmailReadOnlyHttpClient,
)
from careerops.infrastructure.gmail.credentials import GmailAccessToken
from careerops.infrastructure.gmail.worker import (
    GMAIL_READONLY_SCOPE,
    GmailCredentialError,
    GmailDomainProcessingResult,
    GmailHistoryExpired,
    GmailMailboxSyncJob,
    GmailMessageBatch,
    GmailReadOnlyWorker,
    GmailWorkerOutcome,
    HttpGmailReadOnlyProvider,
)

MAILBOX_ID = UUID("00000000-0000-0000-0000-00000000f001")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-00000000f002")
OWNER_ID = UUID("00000000-0000-0000-0000-00000000f003")
RUN_ID = UUID("00000000-0000-0000-0000-00000000f004")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-00000000f005")
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _job() -> GmailMailboxSyncJob:
    return GmailMailboxSyncJob(
        mailbox_id=MAILBOX_ID,
        owner_user_id=OWNER_ID,
        candidate_id=CANDIDATE_ID,
        credential_handle="gmail:opaque-readonly",
        account_subject="candidate@example.com",
        last_history_id="100",
        resync_required=False,
        sync_run_id=RUN_ID,
        lease_token=LEASE_TOKEN,
    )


def _message(
    *, history_id: str = "101", subject: str = "Interview availability"
) -> GmailMetadataSignal:
    return GmailMetadataSignal(
        provider_message_id=f"msg-{history_id}",
        provider_thread_id=f"thread-{history_id}",
        provider_history_id=history_id,
        account_subject="candidate@example.com",
        headers={"subject": subject, "from": "recruiter@greenhouse.io"},
        label_ids=("INBOX",),
        snippet="Can you schedule a call with our recruiter?",
        received_at=NOW,
    )


@dataclass
class RecordingRepository:
    jobs: tuple[GmailMailboxSyncJob, ...] = (_job(),)
    succeeded: list[GmailMessageBatch] | None = None
    incomplete: list[GmailMessageBatch] | None = None
    retries: list[str] | None = None
    reauth: list[str] | None = None
    resync: list[str] | None = None
    failures: list[str] | None = None

    def __post_init__(self) -> None:
        self.succeeded = []
        self.incomplete = []
        self.retries = []
        self.reauth = []
        self.resync = []
        self.failures = []

    def claim_due(
        self, *, owner: str, now: datetime, limit: int
    ) -> tuple[GmailMailboxSyncJob, ...]:
        assert owner == "unit-gmail"
        assert now == NOW
        assert limit == 10
        return self.jobs

    def count_due(self, *, now: datetime) -> int:
        assert now == NOW
        return len(self.jobs)

    def mark_succeeded(
        self,
        *,
        job: GmailMailboxSyncJob,
        batch: GmailMessageBatch,
        processing: GmailDomainProcessingResult,
        owner: str,
        now: datetime,
    ) -> None:
        assert job == _job()
        assert owner == "unit-gmail"
        assert now == NOW
        assert processing == GmailDomainProcessingResult(processed_messages=1, review_proposals=1)
        assert self.succeeded is not None
        self.succeeded.append(batch)

    def mark_incomplete_page(
        self,
        *,
        job: GmailMailboxSyncJob,
        batch: GmailMessageBatch,
        processing: GmailDomainProcessingResult,
        now: datetime,
    ) -> None:
        assert job == _job()
        assert now == NOW
        assert processing == GmailDomainProcessingResult(processed_messages=1, review_proposals=1)
        assert self.incomplete is not None
        self.incomplete.append(batch)

    def mark_reauth_required(
        self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str
    ) -> None:
        assert job == _job()
        assert self.reauth is not None
        self.reauth.append(error_code)

    def mark_resync_required(
        self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str
    ) -> None:
        assert job == _job()
        assert self.resync is not None
        self.resync.append(error_code)

    def mark_retry(self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str) -> None:
        assert job == _job()
        assert self.retries is not None
        self.retries.append(error_code)

    def mark_failed(self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str) -> None:
        assert job == _job()
        assert now == NOW
        assert self.failures is not None
        self.failures.append(error_code)


class StaticResolver:
    def resolve(self, job: GmailMailboxSyncJob) -> GmailAccessToken:
        assert job == _job()
        return GmailAccessToken(
            access_token="ya29.runtime-token",
            account_subject="candidate@example.com",
            granted_scopes=(GMAIL_READONLY_SCOPE,),
            expires_at=NOW + timedelta(minutes=5),
        )


class FailingResolver:
    def resolve(self, job: GmailMailboxSyncJob) -> GmailAccessToken:
        assert job == _job()
        raise GmailCredentialError("GMAIL_CREDENTIAL_BROKER_UNAVAILABLE")


class StaticProvider:
    def __init__(self, result: GmailMessageBatch | Exception) -> None:
        self.result = result
        self.called = False

    def fetch_batch(
        self,
        *,
        token: GmailAccessToken,
        job: GmailMailboxSyncJob,
        max_results: int,
    ) -> GmailMessageBatch:
        assert token.value == "ya29.runtime-token"
        assert job == _job()
        assert max_results == 100
        self.called = True
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class SequenceProvider:
    def __init__(self, results: tuple[GmailMessageBatch, ...]) -> None:
        self._results = list(results)

    def fetch_batch(
        self,
        *,
        token: GmailAccessToken,
        job: GmailMailboxSyncJob,
        max_results: int,
    ) -> GmailMessageBatch:
        assert token.value == "ya29.runtime-token"
        assert job == _job()
        assert max_results == 100
        return self._results.pop(0)


class StaticDomainProcessor:
    def process(
        self,
        *,
        job: GmailMailboxSyncJob,
        batch: GmailMessageBatch,
        now: datetime,
    ) -> GmailDomainProcessingResult:
        assert job == _job()
        assert now == NOW
        return GmailDomainProcessingResult(
            processed_messages=len(batch.messages), review_proposals=1
        )


def _worker(
    repository: RecordingRepository,
    provider: StaticProvider | SequenceProvider,
    *,
    enabled: bool = True,
    resolver: StaticResolver | FailingResolver | None = None,
) -> GmailReadOnlyWorker:
    return GmailReadOnlyWorker(
        repository,
        resolver or StaticResolver(),
        provider,
        StaticDomainProcessor(),
        owner="unit-gmail",
        enabled=enabled,
        disabled_reason=None if enabled else "GOOGLE_OAUTH_DISABLED",
        clock=lambda: NOW,
    )


def test_disabled_worker_is_healthy_and_does_not_claim_or_call_provider() -> None:
    repository = RecordingRepository()
    provider = StaticProvider(GmailMessageBatch(messages=(), high_water_history_id="101"))

    result = _worker(repository, provider, enabled=False).run_once()

    assert result.outcome is GmailWorkerOutcome.DISABLED
    assert result.claimed == 0
    assert provider.called is False
    assert repository.succeeded == []


def test_broker_failure_defers_without_calling_provider_or_persisting() -> None:
    repository = RecordingRepository()
    provider = StaticProvider(GmailMessageBatch(messages=(), high_water_history_id="101"))

    result = _worker(repository, provider, resolver=FailingResolver()).run_once()

    assert result.outcome is GmailWorkerOutcome.DEFERRED
    assert result.deferred == 1
    assert provider.called is False
    assert repository.retries == ["GMAIL_CREDENTIAL_BROKER_UNAVAILABLE"]
    assert repository.succeeded == []
    assert repository.incomplete == []


def test_worker_advances_cursor_only_after_complete_batch_is_persisted() -> None:
    repository = RecordingRepository()
    batch = GmailMessageBatch(messages=(_message(),), high_water_history_id="101")
    provider = StaticProvider(batch)

    result = _worker(repository, provider).run_once(limit=10, max_results=100)

    assert result.outcome is GmailWorkerOutcome.SYNCED
    assert result.processed_messages == 1
    assert result.review_proposals == 1
    assert repository.succeeded == [batch]
    assert repository.incomplete == []
    assert repository.retries == []


def test_worker_does_not_advance_cursor_when_gmail_page_is_incomplete() -> None:
    repository = RecordingRepository()
    batch = GmailMessageBatch(
        messages=(_message(),),
        high_water_history_id="101",
        next_page_token="next-page",
    )

    result = _worker(repository, StaticProvider(batch)).run_once()

    assert result.outcome is GmailWorkerOutcome.DEFERRED
    assert result.deferred == 1
    assert result.processed_messages == 1
    assert result.review_proposals == 1
    assert repository.succeeded == []
    assert repository.incomplete == [batch]
    assert repository.retries == []


def test_worker_persists_each_page_and_advances_cursor_only_on_final_page() -> None:
    repository = RecordingRepository()
    first = GmailMessageBatch(
        messages=(_message(history_id="101"),),
        high_water_history_id="150",
        next_page_token="next-page",
    )
    second = GmailMessageBatch(
        messages=(_message(history_id="102"),),
        high_water_history_id="150",
    )
    worker = _worker(repository, SequenceProvider((first, second)))

    first_result = worker.run_once(limit=10, max_results=100)
    second_result = worker.run_once(limit=10, max_results=100)

    assert first_result.outcome is GmailWorkerOutcome.DEFERRED
    assert first_result.processed_messages == 1
    assert second_result.outcome is GmailWorkerOutcome.SYNCED
    assert second_result.processed_messages == 1
    assert repository.incomplete == [first]
    assert repository.succeeded == [second]


def test_credential_401_or_403_marks_reauth_required_fail_closed() -> None:
    repository = RecordingRepository()

    result = _worker(
        repository,
        StaticProvider(GmailCredentialError("GMAIL_403", status_code=403)),
    ).run_once()

    assert result.outcome is GmailWorkerOutcome.FAILED
    assert result.failed == 1
    assert repository.reauth == ["GMAIL_REAUTH_REQUIRED"]
    assert repository.succeeded == []


def test_history_404_marks_resync_required_without_cursor_advance() -> None:
    repository = RecordingRepository()

    result = _worker(repository, StaticProvider(GmailHistoryExpired())).run_once()

    assert result.outcome is GmailWorkerOutcome.DEFERRED
    assert repository.resync == ["GMAIL_HISTORY_EXPIRED"]
    assert repository.succeeded == []


def test_transient_429_and_5xx_are_bounded_retries_not_reauth() -> None:
    for status_code in (429, 500):
        repository = RecordingRepository()
        result = _worker(
            repository,
            StaticProvider(
                GmailCredentialError(f"GMAIL_HTTP_{status_code}", status_code=status_code)
            ),
        ).run_once()

        assert result.outcome is GmailWorkerOutcome.DEFERRED
        assert repository.retries == [f"GMAIL_HTTP_{status_code}"]
        assert repository.reauth == []
        assert repository.succeeded == []


def test_deterministic_contract_failure_is_terminal_not_retried() -> None:
    repository = RecordingRepository()

    result = _worker(repository, StaticProvider(ValueError("invalid provider contract"))).run_once()

    assert result.outcome is GmailWorkerOutcome.FAILED
    assert result.failed == 1
    assert repository.failures == ["GMAIL_INTERNAL_CONTRACT_FAILED"]
    assert repository.retries == []


def test_unexpected_runtime_failure_is_not_masked_as_retryable() -> None:
    repository = RecordingRepository()

    with pytest.raises(RuntimeError, match="unexpected provider failure"):
        _worker(
            repository,
            StaticProvider(RuntimeError("unexpected provider failure")),
        ).run_once()

    assert repository.failures == []
    assert repository.retries == []


class ProfileMismatchClient:
    def get_profile(self, token: GmailAccessToken) -> GmailProfile:
        assert token.value == "ya29.runtime-token"
        return GmailProfile(
            email_address="other@example.com",
            messages_total=0,
            threads_total=0,
            history_id="101",
        )

    def list_history(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("history must not be read after account subject mismatch")

    def list_messages(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("messages must not be read after account subject mismatch")

    def get_message(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("message metadata must not be read after account subject mismatch")


def test_http_provider_rejects_token_bound_to_different_gmail_subject() -> None:
    provider = HttpGmailReadOnlyProvider(
        client=cast("GmailReadOnlyHttpClient", ProfileMismatchClient())
    )

    try:
        provider.fetch_batch(token=StaticResolver().resolve(_job()), job=_job(), max_results=100)
    except GmailCredentialError as exc:
        assert exc.error_code == "GMAIL_ACCOUNT_SUBJECT_MISMATCH"
        assert exc.status_code == 403
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected account-subject mismatch to fail closed")


class HistoryDedupeClient:
    def __init__(self) -> None:
        self.message_ids: list[str] = []

    def get_profile(self, token: GmailAccessToken) -> GmailProfile:
        assert token.value == "ya29.runtime-token"
        return GmailProfile(
            email_address="candidate@example.com",
            messages_total=2,
            threads_total=2,
            history_id="201",
        )

    def list_history(
        self,
        token: GmailAccessToken,
        *,
        start_history_id: str,
        page_token: str | None = None,
        max_results: int = 100,
    ) -> GmailHistoryPage:
        assert token.value == "ya29.runtime-token"
        assert start_history_id == "100"
        assert page_token is None
        assert max_results == 100
        return GmailHistoryPage(
            history=(
                {
                    "messages": [{"id": "msg-1"}, {"id": "msg-2"}],
                    "messagesAdded": [
                        {"message": {"id": "msg-1"}},
                        {"message": {"id": "msg-2"}},
                    ],
                },
            ),
            next_page_token=None,
            checkpoint_history_id="201",
        )

    def list_messages(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("history jobs must not call messages.list")

    def get_message(
        self,
        token: GmailAccessToken,
        *,
        message_id: str,
        metadata_headers: tuple[str, ...],
    ) -> GmailMetadataMessage:
        assert token.value == "ya29.runtime-token"
        assert metadata_headers == ("Subject", "From", "Date")
        self.message_ids.append(message_id)
        return GmailMetadataMessage(
            id=message_id,
            thread_id=f"thread-{message_id}",
            history_id="201",
            headers={"subject": "Interview", "from": "recruiter@greenhouse.io"},
            label_ids=("INBOX",),
            snippet="Please schedule an interview.",
            internal_date=NOW,
        )


def test_history_provider_dedupes_message_ids_across_history_shapes() -> None:
    client = HistoryDedupeClient()
    provider = HttpGmailReadOnlyProvider(client=cast("GmailReadOnlyHttpClient", client))

    batch = provider.fetch_batch(
        token=StaticResolver().resolve(_job()), job=_job(), max_results=100
    )

    assert client.message_ids == ["msg-1", "msg-2"]
    assert [message.provider_message_id for message in batch.messages] == ["msg-1", "msg-2"]
    assert batch.high_water_history_id == "201"


class InitialAnchorClient:
    def __init__(self) -> None:
        self.profile_history_ids = ["999"]

    def get_profile(self, token: GmailAccessToken) -> GmailProfile:
        assert token.value == "ya29.runtime-token"
        return GmailProfile(
            email_address="candidate@example.com",
            messages_total=1,
            threads_total=1,
            history_id=self.profile_history_ids.pop(0),
        )

    def list_messages(
        self,
        token: GmailAccessToken,
        *,
        page_token: str | None = None,
        max_results: int = 100,
    ) -> GmailMessageListPage:
        assert token.value == "ya29.runtime-token"
        assert page_token == "next-page"
        assert max_results == 100
        return GmailMessageListPage(messages=(GmailListedMessage(id="msg-1", thread_id="t-1"),))

    def list_history(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("initial full sync pages must not switch to history.list")

    def get_message(
        self,
        token: GmailAccessToken,
        *,
        message_id: str,
        metadata_headers: tuple[str, ...],
    ) -> GmailMetadataMessage:
        assert token.value == "ya29.runtime-token"
        assert message_id == "msg-1"
        assert metadata_headers == ("Subject", "From", "Date")
        return GmailMetadataMessage(
            id="msg-1",
            thread_id="t-1",
            history_id="998",
            headers={"subject": "Interview", "from": "recruiter@greenhouse.io"},
            label_ids=("INBOX",),
            snippet="Please schedule an interview.",
            internal_date=NOW,
        )


def test_initial_provider_reuses_stored_anchor_across_multipage_full_sync() -> None:
    client = InitialAnchorClient()
    provider = HttpGmailReadOnlyProvider(client=cast("GmailReadOnlyHttpClient", client))
    job = GmailMailboxSyncJob(
        mailbox_id=MAILBOX_ID,
        owner_user_id=OWNER_ID,
        candidate_id=CANDIDATE_ID,
        credential_handle="gmail:opaque-readonly",
        account_subject="candidate@example.com",
        last_history_id=None,
        resync_required=True,
        sync_run_id=RUN_ID,
        lease_token=LEASE_TOKEN,
        anchor_history_id="150",
        next_page_token="next-page",
    )

    batch = provider.fetch_batch(token=StaticResolver().resolve(_job()), job=job, max_results=100)

    assert batch.high_water_history_id == "150"
    assert [message.provider_message_id for message in batch.messages] == ["msg-1"]
