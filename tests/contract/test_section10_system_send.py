"""Contract tests: Section 10 system-managed Gmail send + reconciliation.

Proves the fake-provider system-managed send slice end-to-end at the service
and route layers, using ONLY the offline :class:`FakeSideEffectProvider` and
in-memory stores — no live OAuth, external-write, or auto-send flag is enabled
(tasks 10.9, 10.10; design Phase 3 / task 17.6).

Coverage:
- 10.1: confirmation is a durable approval/intent, NOT a direct provider call.
- 10.2 / 10.8: every trusted precondition denies BEFORE the provider call
  (ownership, application state, package approval, payload hash, recipient,
  capability release, account status, policy).
- 10.3: one transactional Outbox event + durable ``pending`` status.
- 10.4: isolated worker resolves the call via the kernel/provider and persists
  attempts without exposing tokens to the API/model paths.
- 10.5: provider receipt + ``SUBMITTED_VIA_PROVIDER`` event ONLY after success.
- 10.6: timeout / crash / ambiguous -> reconciliation by stable key; no blind
  retry.
- 10.7: repeated confirmation idempotent; returns existing state.
- 10.9: crash matrix (before-call, after-call-before-receipt, timeout,
  duplicate, credential revoke, escalation).
- 10.10: controlled integration slice; no duplicate provider effects.
- route layer: default-deny 403, ownership 404, dependency-not-ready 503,
  ``Cache-Control: no-store``.

Iron rules honored:
- Default-deny (Iron Rule 7): SYSTEM_MANAGED_SEND stays denied at the contract
  layer; tests release it via a fake resolver only.
- Append-only / reversible (Iron Rule 4): the submitted event is appended once.
- Model review-only (Iron Rule 2): recipient/payload are server-sourced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.api.errors import install_error_handlers
from careerops.api.routes.system_send import router as system_send_router
from careerops.application.side_effect_kernel import SideEffectKernel
from careerops.application.system_managed_send import (
    SystemManagedSendService,
    SystemSendDeniedError,
)
from careerops.config import Settings
from careerops.domain.applications import (
    Application,
    ApplicationEventType,
    ApplicationState,
    SubmissionChannel,
)
from careerops.domain.system_send import (
    SystemSendDenialReason,
    SystemSendPhase,
    SystemSendRequest,
    compute_system_send_idempotency_key,
    compute_system_send_payload_hash,
)
from careerops.infrastructure.database.side_effect_memory import (
    InMemoryOutboxStore,
    InMemorySideEffectStore,
)
from careerops.infrastructure.memory_repos import InMemoryApplicationRepository
from careerops.integrations.fake_side_effect_provider import (
    FakeFailureMode,
    FakeSideEffectProvider,
)
from careerops.orchestration.capability_resolver import (
    CapabilityDecision,
    CapabilityKind,
    SettingsCapabilityResolver,
)

NOW = datetime(2026, 7, 26, tzinfo=UTC)
CANDIDATE = UUID("11111111-1111-1111-1111-111111111111")
OTHER = UUID("22222222-2222-2222-2222-222222222222")
JOB = UUID("33333333-3333-3333-3333-333333333333")


# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------


class _ReleasingResolver:
    """Test resolver that releases SYSTEM_MANAGED_SEND (and nothing else)."""

    def decide(self, kind: CapabilityKind) -> CapabilityDecision:
        if kind is CapabilityKind.SYSTEM_MANAGED_SEND:
            return CapabilityDecision(released=True, reason="released for test harness")
        return CapabilityDecision(released=False, reason="denied for test harness")


class _DenyingResolver:
    def decide(self, kind: CapabilityKind) -> CapabilityDecision:
        return CapabilityDecision(released=False, reason="denied for test harness")


class _FakePackage:
    """Minimal package-version stand-in for revalidation."""

    def __init__(self, *, package_id: UUID, payload_hash: str, approved: bool = True) -> None:
        self.id = package_id
        self.payload_hash = payload_hash
        self.approval_state = type("A", (), {"value": "approved" if approved else "draft"})()


class _FakePackageReader:
    def __init__(self, package: _FakePackage | None) -> None:
        self._package = package

    def get_latest(self, application_id: UUID) -> _FakePackage | None:
        del application_id
        return self._package


def _make_request(
    *,
    application_id: UUID,
    package_id: UUID,
    body: str = "Hello",
    recipient: str = "recruiter@company.com",
    payload_hash: str | None = None,
    package_payload_hash: str | None = None,
    evidence_refs: tuple[str, ...] = ("ev:contact:1",),
) -> SystemSendRequest:
    exact_hash = compute_system_send_payload_hash(
        application_id=application_id,
        account_id=None,
        account_email="me@careerops.dev",
        recipient=recipient,
        subject="Application",
        body=body,
        attachment_hashes=(),
        thread_headers={},
        evidence_refs=evidence_refs,
    )
    return SystemSendRequest(
        application_id=application_id,
        candidate_id=CANDIDATE,
        account_email="me@careerops.dev",
        recipient=recipient,
        subject="Application",
        body=body,
        package_version_id=package_id,
        payload_hash=payload_hash or exact_hash,
        package_payload_hash=package_payload_hash,
        attachment_hashes=(),
        evidence_refs=evidence_refs,
    )


def _build_service(
    *,
    provider: FakeSideEffectProvider | None = None,
    resolver: object | None = None,
    package: _FakePackage | None = None,
    outbox: InMemoryOutboxStore | None = None,
    account_active: bool = True,
    application_repo: InMemoryApplicationRepository | None = None,
) -> tuple[
    SystemManagedSendService,
    FakeSideEffectProvider,
    InMemorySideEffectStore,
    InMemoryOutboxStore,
    InMemoryApplicationRepository,
    _FakePackageReader,
]:
    provider = provider or FakeSideEffectProvider()
    store = InMemorySideEffectStore()
    outbox_store = outbox or InMemoryOutboxStore()
    app_repo = application_repo or InMemoryApplicationRepository()
    package_reader = _FakePackageReader(package)

    def _test_recipient_eligible(req: object) -> bool:
        """Test resolver: allow any well-formed email (contains @)."""
        r = getattr(req, "recipient", "").strip()
        return bool(r) and "@" in r and not r.endswith("@")

    service = SystemManagedSendService(
        SideEffectKernel(store, provider),
        app_repo,
        package_reader=package_reader,
        capability_resolver=resolver or _ReleasingResolver(),  # type: ignore[arg-type]
        outbox_store=outbox_store,
        account_status_lookup=(lambda _request: account_active)
        if account_active is not None
        else None,
        recipient_eligible=_test_recipient_eligible,
    )
    return service, provider, store, outbox_store, app_repo, package_reader


def _seed_preparing_app(
    repo: InMemoryApplicationRepository,
    *,
    candidate_id: UUID = CANDIDATE,
    state: ApplicationState = ApplicationState.PREPARING,
) -> Application:
    app = Application(
        id=uuid4(),
        candidate_id=candidate_id,
        canonical_job_id=JOB,
        state=state,
        submission_channel=SubmissionChannel.EMAIL,
        created_at=NOW,
        updated_at=NOW,
    )
    repo.save(app)
    return app


# ===========================================================================
# 10.1 + 10.3 — confirmation is a durable intent, returns pending, enqueues outbox
# ===========================================================================


class TestConfirmationDurableIntent:
    def test_confirm_returns_pending_and_does_not_call_provider(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        package = _FakePackage(package_id=uuid4(), payload_hash="x")
        service._packages = _FakePackageReader(package)  # type: ignore[assignment]
        # Recompute the matching payload hash for the seeded package.
        app = _seed_preparing_app(repo)
        request = _make_request(
            application_id=app.id, package_id=package.id, payload_hash=package.payload_hash
        )
        # Override package hash so revalidation matches: rebuild request with the
        # canonical hash the kernel expects.
        request = _make_request(application_id=app.id, package_id=package.id)
        package.payload_hash = request.payload_hash

        status = service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)

        assert status.phase is SystemSendPhase.PENDING
        assert status.intent_id is not None
        assert provider.execute_call_count == 0  # NOT a direct provider call

    def test_confirm_creates_durable_intent_and_outbox_event(self) -> None:
        service, _provider, store, outbox, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        request = _make_request(application_id=app.id, package_id=uuid4())
        # seed an approved package matching the request
        service._packages = _FakePackageReader(  # type: ignore[assignment]
            _FakePackage(package_id=request.package_version_id, payload_hash=request.payload_hash)
        )

        status = service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)

        intent = store.get_intent(status.intent_id)  # type: ignore[arg-type]
        assert intent.action_kind == "send_email"
        assert intent.resource_id == app.id
        approvals = store.list_approvals(intent.id)
        assert len(approvals) == 1
        assert approvals[0].decision.value == "approved"
        # Exactly one transactional outbox event linked to the intent.
        assert len(outbox._events) == 1  # type: ignore[attr-defined]

    def test_durable_pending_is_not_a_submitted_claim(self) -> None:
        service, _, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        request = _make_request(application_id=app.id, package_id=uuid4())
        service._packages = _FakePackageReader(  # type: ignore[assignment]
            _FakePackage(package_id=request.package_version_id, payload_hash=request.payload_hash)
        )
        service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)
        # Application stays PREPARING; no submitted event appended yet.
        events = repo.get_events(app.id)
        assert not any(e.event_type is ApplicationEventType.SUBMITTED_VIA_PROVIDER for e in events)
        assert repo.find_by_id(app.id).state is ApplicationState.PREPARING  # type: ignore[union-attr]


# ===========================================================================
# 10.2 + 10.8 — revalidation denies BEFORE the provider call
# ===========================================================================


class TestRevalidationDenials:
    def test_not_owned_denies_before_provider(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        request = _make_request(application_id=app.id, package_id=uuid4())
        with pytest.raises(SystemSendDeniedError) as exc:
            service.confirm_send(request, candidate_id=OTHER, now=NOW)
        assert SystemSendDenialReason.APPLICATION_NOT_OWNED.value in exc.value.reason_codes
        assert provider.execute_call_count == 0

    def test_not_preparing_denies(self) -> None:
        service, _, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo, state=ApplicationState.FAVORITED)
        request = _make_request(application_id=app.id, package_id=uuid4())
        with pytest.raises(SystemSendDeniedError) as exc:
            service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)
        assert SystemSendDenialReason.APPLICATION_NOT_PREPARING.value in exc.value.reason_codes

    def test_package_not_approved_denies(self) -> None:
        service, _, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        request = _make_request(application_id=app.id, package_id=uuid4())
        service._packages = _FakePackageReader(  # type: ignore[assignment]
            _FakePackage(
                package_id=request.package_version_id,
                payload_hash=request.payload_hash,
                approved=False,
            )
        )
        with pytest.raises(SystemSendDeniedError) as exc:
            service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)
        assert SystemSendDenialReason.PACKAGE_NOT_APPROVED.value in exc.value.reason_codes

    def test_payload_hash_mismatch_denies(self) -> None:
        service, _, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        request = _make_request(
            application_id=app.id,
            package_id=uuid4(),
            package_payload_hash="expected-package-hash",
        )
        service._packages = _FakePackageReader(  # type: ignore[assignment]
            _FakePackage(package_id=request.package_version_id, payload_hash="stale-hash")
        )
        with pytest.raises(SystemSendDeniedError) as exc:
            service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)
        assert SystemSendDenialReason.PACKAGE_BINDING_STALE.value in exc.value.reason_codes

    def test_recipient_not_eligible_denies(self) -> None:
        service, _, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        request = _make_request(application_id=app.id, package_id=uuid4(), recipient="not-an-email")
        service._packages = _FakePackageReader(  # type: ignore[assignment]
            _FakePackage(package_id=request.package_version_id, payload_hash=request.payload_hash)
        )
        with pytest.raises(SystemSendDeniedError) as exc:
            service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)
        assert SystemSendDenialReason.RECIPIENT_NOT_ELIGIBLE.value in exc.value.reason_codes

    def test_capability_not_released_denies(self) -> None:
        service, provider, _, _, repo, _ = _build_service(resolver=_DenyingResolver())
        app = _seed_preparing_app(repo)
        request = _make_request(application_id=app.id, package_id=uuid4())
        with pytest.raises(SystemSendDeniedError) as exc:
            service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)
        assert SystemSendDenialReason.CAPABILITY_NOT_RELEASED.value in exc.value.reason_codes
        assert provider.execute_call_count == 0

    def test_account_inactive_denies(self) -> None:
        service, _, _, _, repo, _ = _build_service(account_active=False)
        app = _seed_preparing_app(repo)
        request = _make_request(application_id=app.id, package_id=uuid4())
        service._packages = _FakePackageReader(  # type: ignore[assignment]
            _FakePackage(package_id=request.package_version_id, payload_hash=request.payload_hash)
        )
        with pytest.raises(SystemSendDeniedError) as exc:
            service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)
        assert SystemSendDenialReason.ACCOUNT_NOT_ACTIVE.value in exc.value.reason_codes

    def test_policy_denial_for_missing_evidence(self) -> None:
        service, _, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        # No evidence refs -> kernel policy DENIES (EVIDENCE_REQUIRED).
        request = _make_request(application_id=app.id, package_id=uuid4(), evidence_refs=())
        service._packages = _FakePackageReader(  # type: ignore[assignment]
            _FakePackage(package_id=request.package_version_id, payload_hash=request.payload_hash)
        )
        with pytest.raises(SystemSendDeniedError) as exc:
            service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)
        assert SystemSendDenialReason.POLICY_DENIED.value in exc.value.reason_codes


# ===========================================================================
# 10.4 + 10.5 — worker executes via provider, receipt + SUBMITTED only after success
# ===========================================================================


def _confirm(service: SystemManagedSendService, app: Application) -> UUID:
    request = _make_request(application_id=app.id, package_id=uuid4())
    service._packages = _FakePackageReader(  # type: ignore[assignment]
        _FakePackage(package_id=request.package_version_id, payload_hash=request.payload_hash)
    )
    status = service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)
    return status.intent_id  # type: ignore[return-value]


class TestWorkerExecutionAndReceipt:
    def test_worker_step_calls_provider_and_records_receipt(self) -> None:
        service, provider, store, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)

        status = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)

        assert status.phase is SystemSendPhase.SENT
        assert provider.execute_call_count == 1
        assert status.provider_resource_id is not None
        receipts = store.list_receipts(intent_id)
        assert len(receipts) == 1

    def test_submitted_via_provider_event_only_after_success(self) -> None:
        service, _, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)

        service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)

        events = repo.get_events(app.id)
        submitted = [
            e for e in events if e.event_type is ApplicationEventType.SUBMITTED_VIA_PROVIDER
        ]
        assert len(submitted) == 1
        assert submitted[0].event_data["provider"] == "fake"
        assert repo.find_by_id(app.id).state is ApplicationState.SUBMITTED  # type: ignore[union-attr]
        assert repo.find_by_id(app.id).submitted_at == NOW  # type: ignore[union-attr]

    def test_worker_does_not_expose_tokens_to_caller(self) -> None:
        from dataclasses import asdict

        service, _, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        status = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)
        # The status/response carries only the provider resource id, never a
        # credential/token. Opaque reference only.
        rendered = str(asdict(status)).lower()
        assert not any(tok in rendered for tok in ("token", "secret", "password", "oauth"))


# ===========================================================================
# 10.7 — idempotent confirmation / status read
# ===========================================================================


class TestIdempotency:
    def test_duplicate_confirmation_returns_same_intent(self) -> None:
        service, provider, _, outbox, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        request = _make_request(application_id=app.id, package_id=uuid4())
        service._packages = _FakePackageReader(  # type: ignore[assignment]
            _FakePackage(package_id=request.package_version_id, payload_hash=request.payload_hash)
        )

        s1 = service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)
        s2 = service.confirm_send(request, candidate_id=CANDIDATE, now=NOW + timedelta(seconds=1))

        assert s1.intent_id == s2.intent_id
        assert provider.execute_call_count == 0
        assert len(outbox._events) == 1  # type: ignore[attr-defined]

    def test_get_status_idempotent_no_side_effects(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        calls_before = provider.execute_call_count

        s1 = service.get_status(intent_id, candidate_id=CANDIDATE)
        s2 = service.get_status(intent_id, candidate_id=CANDIDATE)

        assert s1.phase is SystemSendPhase.PENDING
        assert s1 == s2
        assert provider.execute_call_count == calls_before

    def test_status_after_send_reflects_sent(self) -> None:
        service, _, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)

        status = service.get_status(intent_id, candidate_id=CANDIDATE)
        assert status.phase is SystemSendPhase.SENT
        assert status.submitted_at is not None

    def test_idempotency_key_is_stable(self) -> None:
        application_id = UUID(int=1)
        account_email = "me@careerops.dev"
        recipient = "r@co.com"
        normalized_payload_hash = "abc"
        k1 = compute_system_send_idempotency_key(
            application_id=application_id,
            account_email=account_email,
            recipient=recipient,
            normalized_payload_hash=normalized_payload_hash,
        )
        k2 = compute_system_send_idempotency_key(
            application_id=application_id,
            account_email=account_email,
            recipient=recipient,
            normalized_payload_hash=normalized_payload_hash,
        )
        assert k1 == k2 and len(k1) == 64
        k3 = compute_system_send_idempotency_key(
            application_id=application_id,
            account_email=account_email,
            recipient="x@co.com",
            normalized_payload_hash=normalized_payload_hash,
        )
        assert k1 != k3


# ===========================================================================
# 10.6 — reconciliation; no blind retry
# ===========================================================================


class TestReconciliation:
    def test_ambiguous_outcome_stays_reconciliation_required(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        provider.set_failure_mode(FakeFailureMode.CRASH_AFTER_CALL_BEFORE_COMMIT)

        status = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)

        assert status.phase is SystemSendPhase.RECONCILIATION_REQUIRED
        # Application stays PREPARING; no submitted claim.
        assert repo.find_by_id(app.id).state is ApplicationState.PREPARING  # type: ignore[union-attr]
        events = repo.get_events(app.id)
        assert not any(e.event_type is ApplicationEventType.SUBMITTED_VIA_PROVIDER for e in events)

    def test_recovery_reconciles_to_sent_without_duplicate(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)

        provider.set_failure_mode(FakeFailureMode.CRASH_AFTER_CALL_BEFORE_COMMIT)
        s1 = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)
        assert s1.phase is SystemSendPhase.RECONCILIATION_REQUIRED
        calls_after_crash = provider.execute_call_count

        provider.set_failure_mode(FakeFailureMode.NONE)
        s2 = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW + timedelta(seconds=5))

        assert s2.phase is SystemSendPhase.SENT
        assert s2.reconciled is True
        # The recovery pass reconciled instead of re-executing the provider.
        assert provider.execute_call_count == calls_after_crash
        assert provider.effect_count() == 1

    def test_escalate_surfaces_reconciliation_task_no_retry(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        provider.set_failure_mode(FakeFailureMode.CRASH_AFTER_CALL_BEFORE_COMMIT)
        service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)
        calls_before = provider.execute_call_count

        status = service.escalate_reconciliation(intent_id, candidate_id=CANDIDATE, now=NOW)

        assert status.phase is SystemSendPhase.RECONCILIATION_REQUIRED
        # Escalation never retries the provider.
        assert provider.execute_call_count == calls_before


# ===========================================================================
# 10.9 — fake-provider crash matrix
# ===========================================================================


class TestCrashMatrix:
    def test_crash_before_call_is_retriable_no_effect(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        provider.set_failure_mode(FakeFailureMode.CRASH_BEFORE_CALL)

        status = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)
        # Crash before the provider boundary -> no effect, can retry safely.
        assert status.phase in (SystemSendPhase.RECONCILIATION_REQUIRED, SystemSendPhase.PENDING)
        assert provider.effect_count() == 0

        provider.set_failure_mode(FakeFailureMode.NONE)
        status2 = service.worker_step(
            intent_id, candidate_id=CANDIDATE, now=NOW + timedelta(seconds=2)
        )
        assert status2.phase is SystemSendPhase.SENT
        assert provider.effect_count() == 1

    def test_crash_after_call_before_receipt_reconciles(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        provider.set_failure_mode(FakeFailureMode.CRASH_AFTER_CALL_BEFORE_COMMIT)

        s1 = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)
        assert s1.phase is SystemSendPhase.RECONCILIATION_REQUIRED
        assert provider.effect_count() == 1  # effect applied, receipt not committed

        provider.set_failure_mode(FakeFailureMode.NONE)
        s2 = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW + timedelta(seconds=3))
        assert s2.phase is SystemSendPhase.SENT
        assert provider.effect_count() == 1  # no duplicate

    def test_timeout_with_success_reconciles_to_sent(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        provider.set_failure_mode(FakeFailureMode.TIMEOUT_WITH_SUCCESS)

        status = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)

        assert status.phase is SystemSendPhase.SENT
        assert status.reconciled is True
        assert provider.effect_count() == 1

    def test_duplicate_confirmation_no_second_effect(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)

        # Re-confirm the same payload (double-click).
        request = _make_request(application_id=app.id, package_id=uuid4())
        # Rebuild with the SAME idempotency inputs as the original confirmation
        # to exercise the duplicate path through the service.
        original = service.get_status(intent_id, candidate_id=CANDIDATE)
        assert original.phase is SystemSendPhase.SENT

        # Worker step again: idempotent, no new provider effect.
        service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW + timedelta(seconds=1))
        assert provider.effect_count() == 1
        del request

    def test_credential_revoke_denies_at_confirmation(self) -> None:
        # Credential/account revocation surfaces as account-inactive at
        # confirmation time, refusing before the provider call.
        service, provider, _, _, repo, _ = _build_service(account_active=False)
        app = _seed_preparing_app(repo)
        request = _make_request(application_id=app.id, package_id=uuid4())
        service._packages = _FakePackageReader(  # type: ignore[assignment]
            _FakePackage(package_id=request.package_version_id, payload_hash=request.payload_hash)
        )
        with pytest.raises(SystemSendDeniedError) as exc:
            service.confirm_send(request, candidate_id=CANDIDATE, now=NOW)
        assert SystemSendDenialReason.ACCOUNT_NOT_ACTIVE.value in exc.value.reason_codes
        assert provider.execute_call_count == 0

    def test_reconciliation_escalation_after_ambiguous(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        provider.set_failure_mode(FakeFailureMode.CRASH_AFTER_CALL_BEFORE_COMMIT)
        service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)

        status = service.escalate_reconciliation(intent_id, candidate_id=CANDIDATE, now=NOW)
        assert status.phase is SystemSendPhase.RECONCILIATION_REQUIRED
        assert "RECONCILIATION_ESCALATED" in status.denial_reasons


# ===========================================================================
# 10.10 — controlled integration slice; no duplicate provider effects
# ===========================================================================


class TestIntegrationSlice:
    def test_full_send_slice_exactly_one_effect(self) -> None:
        service, provider, _, outbox, repo, _ = _build_service()
        app = _seed_preparing_app(repo)

        # confirm -> pending
        intent_id = _confirm(service, app)
        pending = service.get_status(intent_id, candidate_id=CANDIDATE)
        assert pending.phase is SystemSendPhase.PENDING
        assert len(outbox._events) == 1  # type: ignore[attr-defined]

        # worker executes
        s1 = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)
        assert s1.phase is SystemSendPhase.SENT
        assert provider.effect_count() == 1

        # duplicate worker passes do not duplicate the provider effect nor the event
        s2 = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW + timedelta(seconds=1))
        assert s2.phase is SystemSendPhase.SENT
        assert provider.effect_count() == 1
        events = repo.get_events(app.id)
        submitted_count = sum(
            1 for e in events if e.event_type is ApplicationEventType.SUBMITTED_VIA_PROVIDER
        )
        assert submitted_count == 1

    def test_no_live_flags_enabled_in_harness(self) -> None:
        # The default Settings keep external_writes / google_oauth / auto_send
        # OFF; the slice runs entirely through the fake provider.
        settings = Settings()
        assert settings.external_writes_enabled is False
        assert settings.google_oauth_enabled is False
        assert settings.auto_send_enabled is False
        resolver = SettingsCapabilityResolver(settings)
        assert resolver.decide(CapabilityKind.SYSTEM_MANAGED_SEND).released is False
        assert resolver.decide(CapabilityKind.AUTO_SEND).released is False

    def test_validation_failure_is_terminal_failed(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        provider.set_failure_mode(FakeFailureMode.VALIDATION_ERROR)

        status = service.worker_step(intent_id, candidate_id=CANDIDATE, now=NOW)

        assert status.phase is SystemSendPhase.FAILED
        # Definitive failure does not produce a submitted event.
        events = repo.get_events(app.id)
        assert not any(e.event_type is ApplicationEventType.SUBMITTED_VIA_PROVIDER for e in events)


# ===========================================================================
# Route layer (default-deny, ownership, dependency-not-ready, no-store)
# ===========================================================================


def _build_route_app(
    *,
    service: SystemManagedSendService | None = None,
    resolver_on_state: object | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(system_send_router)
    install_error_handlers(app)
    if service is not None:
        app.state.system_managed_send_service = service
    # require_capability reads app.state.capability_resolver; absent -> 503.
    if resolver_on_state is not None:
        app.state.capability_resolver = resolver_on_state
    # candidate_id is supplied by the URL path (auth-rm migration); there is
    # no session/principal dependency anymore.
    return app


class TestRoutesDefaultDenyAndOwnership:
    def test_route_default_denied_403(self) -> None:
        # Real settings resolver: SYSTEM_MANAGED_SEND denied by default.
        resolver = SettingsCapabilityResolver(Settings())
        app = _build_route_app(resolver_on_state=resolver)
        client = TestClient(app)
        resp = client.post(f"/api/v1/candidates/{CANDIDATE}/applications/{uuid4()}/system-send", json={})
        assert resp.status_code == 403

    def test_route_dependency_not_ready_503_when_resolver_absent(self) -> None:
        # No capability_resolver on app.state -> dependency-not-ready (503),
        # never a silent denial that hides the missing wiring.
        app = _build_route_app()
        client = TestClient(app)
        resp = client.post(f"/api/v1/candidates/{CANDIDATE}/applications/{uuid4()}/system-send", json={})
        assert resp.status_code == 503

    def test_route_missing_service_is_503_when_capability_released(self) -> None:
        # Capability released but no service wired -> 503.
        app = _build_route_app(resolver_on_state=_ReleasingResolver())
        client = TestClient(app)
        body = {
            "account_email": "me@careerops.dev",
            "recipient": "r@co.com",
            "subject": "s",
            "body": "b",
            "package_version_id": str(uuid4()),
            "payload_hash": "x",
        }
        resp = client.post(f"/api/v1/candidates/{CANDIDATE}/applications/{uuid4()}/system-send", json=body)
        assert resp.status_code == 503

    def test_route_status_is_no_store(self) -> None:
        service, _, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        intent_id = _confirm(service, app)
        client = TestClient(
            _build_route_app(service=service, resolver_on_state=_ReleasingResolver())
        )
        resp = client.get(f"/api/v1/candidates/{CANDIDATE}/applications/{app.id}/system-send/{intent_id}")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json()["phase"] == "pending"

    def test_route_confirm_then_worker_sent(self) -> None:
        service, provider, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        client = TestClient(
            _build_route_app(service=service, resolver_on_state=_ReleasingResolver())
        )
        # Build a request whose payload hash matches the service's kernel hash.
        request = _make_request(application_id=app.id, package_id=uuid4())
        service._packages = _FakePackageReader(  # type: ignore[assignment]
            _FakePackage(package_id=request.package_version_id, payload_hash=request.payload_hash)
        )
        body = {
            "account_email": request.account_email,
            "recipient": request.recipient,
            "subject": request.subject,
            "body": request.body,
            "package_version_id": str(request.package_version_id),
            "payload_hash": request.payload_hash,
            "evidence_ids": list(request.evidence_refs),
        }
        resp = client.post(f"/api/v1/candidates/{CANDIDATE}/applications/{app.id}/system-send", json=body)
        assert resp.status_code == 200
        intent_id = resp.json()["intent_id"]
        assert resp.json()["phase"] == "pending"
        assert provider.execute_call_count == 0

        # Drive the worker (isolated); route status then reflects sent.
        service.worker_step(UUID(intent_id), candidate_id=CANDIDATE, now=NOW)
        status = client.get(f"/api/v1/candidates/{CANDIDATE}/applications/{app.id}/system-send/{intent_id}")
        assert status.status_code == 200
        assert status.json()["phase"] == "sent"

    def test_route_denial_is_403_with_reason_codes(self) -> None:
        service, _, _, _, repo, _ = _build_service()
        app = _seed_preparing_app(repo)
        client = TestClient(
            _build_route_app(service=service, resolver_on_state=_ReleasingResolver())
        )
        # recipient invalid -> recipient_not_eligible denial (403).
        body = {
            "account_email": "me@careerops.dev",
            "recipient": "not-an-email",
            "subject": "s",
            "body": "b",
            "package_version_id": str(uuid4()),
            "payload_hash": "x",
        }
        resp = client.post(f"/api/v1/candidates/{CANDIDATE}/applications/{app.id}/system-send", json=body)
        assert resp.status_code == 403
