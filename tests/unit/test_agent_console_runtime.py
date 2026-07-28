"""Runtime contract tests for the agent-console execution seams.

These tests intentionally exercise the bounded, fail-closed edges added by
the agent-first console change: the ego sidecar wire contract, crawl
projection, Temporal activity receipts, and preflight decisions.  They use
in-memory fakes and HTTPX's mock transport; no credentials, network, or raw
page content are involved.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from temporalio.exceptions import ApplicationError

from careerops.agent_console.context_service import ContextService
from careerops.agent_console.contracts import (
    CreateContext,
    ModelOperation,
    PreflightRequest,
)
from careerops.agent_console.crawl_adapter import CrawlAdapter, _compute_scope_digest
from careerops.agent_console.errors import AgentConsoleErrorCode
from careerops.agent_console.preflight_service import PreflightService
from careerops.agent_console.sidecar_client import (
    PartialReason,
    PostingRecord,
    ReadyResponse,
    ResultState,
    RunFailureResponse,
    RunRequest,
    RunSuccessResponse,
    SidecarClient,
    SidecarClientConfig,
    SidecarContractViolation,
    SidecarUnavailableError,
    SignatureBundle,
    _NonceTracker,
    _validate_ready_response,
    _validate_run_failure,
    _validate_run_success,
    canonicalize_url,
    compute_signature,
)
from careerops.domain.crawl import CrawlRunState
from careerops.domain.crawl_plans import (
    CrawlExecutorMode,
    CrawlPlanVersion,
    CrawlRun,
    CrawlRunCounters,
    CrawlSource,
    CrawlSourceType,
)
from careerops.infrastructure.database import agent_console_repo as repo_module
from careerops.infrastructure.temporal.agent_activities import (
    AgentActivities,
    NoOpAgentContextResolver,
    NoOpAgentRunReconciler,
    NoOpAgentStagePersister,
    NoOpDeterministicStageRunner,
    NoOpModelStageInvoker,
)
from careerops.workflows import agent_workflows as workflow_module
from careerops.workflows.agent_contracts import (
    ActivityReceipt,
    AgentRunWorkflowInput,
    InputRef,
    InvokeModelStageInput,
    PersistAgentStageInput,
    ReconcileAgentRunInput,
    ResolveAgentContextInput,
    RunDeterministicStageInput,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
CANDIDATE_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _ready_payload(*, ready: bool = True, reason_code: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ready": ready,
        "service": "careerops-ego-sidecar",
        "protocol_version": "browser-v1",
        "image_digest": DIGEST_A,
        "policy_bundle_digest": DIGEST_B,
        "egress_proxy_mode": "forced_interceptor",
        "supported_schemes": ["http", "https"],
        "key_id": "test-key",
        "checked_at": NOW.isoformat(),
    }
    if reason_code is not None:
        payload["reason_code"] = reason_code
    return payload


def _posting_payload() -> dict[str, Any]:
    return {
        "source_id": "source-1",
        "source_url": "https://jobs.example.test/1",
        "external_id": "job-1",
        "title": "Backend Engineer",
        "organization": "Example",
        "description_digest": DIGEST_A,
        "observed_at": NOW.isoformat(),
    }


def _success_payload(*, partial: bool = False) -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "result_state": "succeeded",
        "partial": partial,
        "partial_reason": "SOURCE_PARTIAL" if partial else None,
        "postings": [_posting_payload()],
        "counts": {"requests": 1, "redirects": 0, "bytes": 128},
        "provenance_digest": DIGEST_A,
        "policy_bundle_digest": DIGEST_B,
        "trace_id": "trace-1",
    }


def _failure_payload() -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "result_state": "blocked",
        "reason_code": "DEPENDENCY_NOT_READY",
        "retryable": True,
        "safe_message": "Browser dependency is not ready.",
        "trace_id": "trace-1",
    }


def _client_with_handler(handler: Any) -> SidecarClient:
    client = SidecarClient(SidecarClientConfig(base_url="http://sidecar.test"))
    transport = httpx.MockTransport(handler)
    client._client = httpx.Client(transport=transport, base_url="http://sidecar.test")
    return client


def _ready_client(handler: Any) -> SidecarClient:
    client = _client_with_handler(handler)
    client._last_ready = _validate_ready_response(_ready_payload())
    return client


def _run_request(*, nonce: str = "nonce-1", run_id: str = "run-1") -> RunRequest:
    return RunRequest(
        run_id=run_id,
        attempt_id="attempt-1",
        candidate_scope_digest="scope-1",
        source_id="source-1",
        canonical_start_url="https://jobs.example.test/start",
        allowlist_version="allowlist-v1",
        task_space_nonce=nonce,
    )


def _signature(*, nonce: str = "nonce-1", timestamp: str | None = None) -> SignatureBundle:
    return SignatureBundle(
        key_id="test-key",
        nonce=nonce,
        timestamp=timestamp or datetime.now(UTC).isoformat(),
        signature="test-signature",
    )


class TestSidecarWireContract:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (" HTTPS://Example.Test.:443/path ", "https://example.test:443/path"),
            ("http://example.test", "http://example.test"),
            ("https://[2001:db8::1]", "https://2001:db8::1"),
        ],
    )
    def test_canonicalize_url_accepts_safe_urls(self, value: str, expected: str) -> None:
        assert canonicalize_url(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "example.test",
            "ftp://example.test",
            "https://user:pass@example.test",
            "https://example.test/#fragment",
            "https://*.example.test",
            "https://example.test:8443",
            "http://127.0.0.1",
            "https://[::1]",
            "https://[2001:db8::1",
            "https://example.test:not-a-port",
        ],
    )
    def test_canonicalize_url_rejects_unsafe_or_malformed_urls(self, value: str) -> None:
        with pytest.raises(ValueError):
            canonicalize_url(value)

    def test_signature_is_hmac_over_the_canonical_request(self) -> None:
        signature = compute_signature(
            method="POST",
            path="/v1/browser/run",
            key_id="key",
            nonce="nonce",
            timestamp="2026-07-28T12:00:00+00:00",
            body=b"{}",
            private_key=b"secret",
        )
        canonical = "POST\n/v1/browser/run\nkey\nnonce\n2026-07-28T12:00:00+00:00\n"
        canonical += hashlib.sha256(b"{}").hexdigest()
        expected = base64.b64encode(
            hmac.new(b"secret", canonical.encode(), hashlib.sha256).digest()
        )
        assert signature == expected.decode()

    def test_nonce_tracker_rejects_replay_and_evicts_expired_values(self, monkeypatch) -> None:
        tracker = _NonceTracker()
        assert tracker.check_and_record("key", "nonce")
        assert not tracker.check_and_record("key", "nonce")
        tracker._seen["old:nonce"] = 0
        tracker._evict(1)
        assert "old:nonce" not in tracker._seen

    def test_ready_validation_accepts_ready_and_blocked_shapes(self) -> None:
        ready = _validate_ready_response(_ready_payload())
        blocked = _validate_ready_response(
            _ready_payload(ready=False, reason_code="DEPENDENCY_NOT_READY")
        )
        assert ready.ready and ready.reason_code is None
        assert not blocked.ready and blocked.reason_code == "DEPENDENCY_NOT_READY"

    @pytest.mark.parametrize(
        "mutator",
        [
            lambda p: p.pop("service"),
            lambda p: p.update(service="wrong"),
            lambda p: p.update(protocol_version="wrong"),
            lambda p: p.update(egress_proxy_mode="direct"),
            lambda p: p.update(supported_schemes=["https"]),
            lambda p: p.update(image_digest="bad"),
            lambda p: p.update(key_id="bad key"),
            lambda p: p.update(ready=True, reason_code="DEPENDENCY_NOT_READY"),
            lambda p: p.update(ready=False),
            lambda p: p.update(ready=False, reason_code="UNKNOWN"),
        ],
    )
    def test_ready_validation_rejects_contract_drift(self, mutator) -> None:
        payload = _ready_payload()
        mutator(payload)
        with pytest.raises(SidecarContractViolation):
            _validate_ready_response(payload)

    def test_run_validation_covers_success_partial_and_failure(self) -> None:
        success = _validate_run_success(_success_payload())
        partial = _validate_run_success(_success_payload(partial=True))
        failure = _validate_run_failure(_failure_payload())
        assert success.postings[0].location == ""
        assert partial.partial_reason == PartialReason.SOURCE_PARTIAL
        assert failure.result_state == ResultState.BLOCKED

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {**_success_payload(), "result_state": "failed"},
            {**_success_payload(), "partial": True, "partial_reason": None},
            {**_success_payload(), "partial": False, "partial_reason": "SOURCE_PARTIAL"},
            {**_success_payload(), "postings": [_posting_payload()] * 1001},
            {**_success_payload(), "provenance_digest": "bad"},
            {**_failure_payload(), "result_state": "succeeded"},
            {key: value for key, value in _failure_payload().items() if key != "trace_id"},
        ],
    )
    def test_run_validation_rejects_contract_drift(self, payload: dict[str, Any]) -> None:
        validator = _validate_run_failure if payload.get("reason_code") else _validate_run_success
        with pytest.raises((SidecarContractViolation, KeyError)):
            validator(payload)


class TestSidecarReady:
    def test_check_ready_validates_headers_and_digest_pins(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update({key: value for key, value in request.headers.items()})
            return httpx.Response(200, json=_ready_payload(), request=request)

        client = _client_with_handler(handler)
        try:
            response = client.check_ready()
            assert response.ready and client.is_ready
            assert seen["cache-control"] == "no-store"
            assert client.last_ready == response
        finally:
            client.close()


class _FakeSidecar:
    """Small typed-enough fake for the crawl adapter seam."""

    def __init__(
        self, result: object = None, *, ready: bool = True, fail_ready: bool = False
    ) -> None:
        self.is_ready = ready
        self.last_ready = _validate_ready_response(_ready_payload()) if ready else None
        self.result = result
        self.fail_ready = fail_ready
        self.checked = False
        self.requests: list[RunRequest] = []

    def check_ready(self) -> ReadyResponse:
        self.checked = True
        if self.fail_ready:
            raise SidecarUnavailableError("not ready")
        self.is_ready = True
        self.last_ready = _validate_ready_response(_ready_payload())
        return self.last_ready

    def build_run_request(self, **kwargs: object) -> RunRequest:
        request = RunRequest(
            run_id=str(kwargs["run_id"]),
            attempt_id=str(kwargs["attempt_id"]),
            candidate_scope_digest=str(kwargs["candidate_scope_digest"]),
            source_id=str(kwargs["source_id"]),
            canonical_start_url=str(kwargs["canonical_start_url"]),
            allowlist_version=str(kwargs["allowlist_version"]),
            task_space_nonce="adapter-nonce",
        )
        self.requests.append(request)
        return request

    def execute_run(self, request: RunRequest, *, signature_bundle: SignatureBundle) -> object:
        del signature_bundle
        return self.result


class _FakeInbox:
    def __init__(self, counters: dict[str, int] | None = None, *, fail: bool = False) -> None:
        self.counters = counters or {"discovered": 1, "updated": 2, "closed": 3, "failed": 0}
        self.fail = fail
        self.calls: list[tuple[UUID, tuple[PostingRecord, ...]]] = []

    def project_postings(
        self,
        owner_id: UUID,
        postings: tuple[PostingRecord, ...],
        *,
        plan_version_id: UUID,
        run_id: UUID,
        source_id: str,
        provenance_digest: str,
        now: datetime | None = None,
    ) -> dict[str, int]:
        del plan_version_id, run_id, source_id, provenance_digest, now
        if self.fail:
            raise RuntimeError("projection failed")
        self.calls.append((owner_id, postings))
        return self.counters


def _crawl_fixture(
    *, executor_mode: CrawlExecutorMode = CrawlExecutorMode.EGO
) -> tuple[UUID, CrawlPlanVersion, CrawlSource, CrawlRun]:
    owner_id = CANDIDATE_ID
    source_id = uuid4()
    plan_id = uuid4()
    source = CrawlSource(
        id=source_id,
        owner_id=owner_id,
        company_id=uuid4(),
        source_type=CrawlSourceType.OFFICIAL,
        source_identifier="example-careers",
        base_url="https://jobs.example.test",
        executor_mode=executor_mode,
        enabled=True,
    )
    plan = CrawlPlanVersion(
        id=plan_id,
        owner_id=owner_id,
        version=2,
        sources=(source_id,),
        rules_version="rules-v2",
    )
    run = CrawlRun(
        id=uuid4(),
        plan_version_id=plan_id,
        run_identity="run-identity",
        source_set=(source_id,),
        state=CrawlRunState.PENDING,
    )
    return owner_id, plan, source, run


class TestCrawlAdapter:
    def test_http_sources_are_blocked_by_the_ego_adapter(self) -> None:
        owner_id, plan, source, run = _crawl_fixture(executor_mode=CrawlExecutorMode.HTTP)
        result = CrawlAdapter().execute_source(owner_id, plan, source, run, now=NOW)
        assert result.result_state == "blocked"
        assert result.counters == CrawlRunCounters(failed=1)

    def test_ego_source_blocks_without_sidecar_or_when_readiness_fails(self) -> None:
        owner_id, plan, source, run = _crawl_fixture()
        no_sidecar = CrawlAdapter().execute_source(owner_id, plan, source, run, now=NOW)
        assert no_sidecar.result_state == "blocked"

        unavailable = _FakeSidecar(ready=False, fail_ready=True)
        result = CrawlAdapter(sidecar_client=cast(SidecarClient, unavailable)).execute_source(
            owner_id, plan, source, run, now=NOW
        )
        assert unavailable.checked and result.counters.failed == 1

    def test_ego_source_projects_success_and_partial_results(self) -> None:
        owner_id, plan, source, run = _crawl_fixture()
        success = _validate_run_success(_success_payload(partial=True))
        inbox = _FakeInbox()
        sidecar = _FakeSidecar(success)
        result = CrawlAdapter(
            sidecar_client=cast(SidecarClient, sidecar), inbox_projection=inbox
        ).execute_source(owner_id, plan, source, run, now=NOW)
        assert result.partial and result.partial_reason == "SOURCE_PARTIAL"
        assert result.postings_count == 1
        assert result.counters.discovered == 1
        assert inbox.calls[0][0] == owner_id

    def test_ego_source_handles_failure_unavailable_and_projection_failure(self) -> None:
        owner_id, plan, source, run = _crawl_fixture()
        failure = _validate_run_failure(_failure_payload())
        failed = CrawlAdapter(
            sidecar_client=cast(SidecarClient, _FakeSidecar(failure))
        ).execute_source(owner_id, plan, source, run, now=NOW)
        assert failed.result_state == "blocked"

        class RaisingSidecar(_FakeSidecar):
            def execute_run(
                self, request: RunRequest, *, signature_bundle: SignatureBundle
            ) -> object:
                del request, signature_bundle
                raise SidecarUnavailableError("sidecar disappeared")

        unavailable = CrawlAdapter(
            sidecar_client=cast(SidecarClient, RaisingSidecar())
        ).execute_source(owner_id, plan, source, run, now=NOW)
        assert unavailable.counters.failed == 1

        success = _validate_run_success(_success_payload())
        projected = CrawlAdapter(
            sidecar_client=cast(SidecarClient, _FakeSidecar(success)),
            inbox_projection=_FakeInbox(fail=True),
        ).execute_source(owner_id, plan, source, run, now=NOW)
        assert projected.counters.failed == 1

        no_inbox = CrawlAdapter(
            sidecar_client=cast(SidecarClient, _FakeSidecar(success))
        ).execute_source(owner_id, plan, source, run, now=NOW)
        assert no_inbox.counters == CrawlRunCounters()

    def test_scope_digest_is_bound_to_owner_plan_version_and_sources(self) -> None:
        owner_id, plan, _, _ = _crawl_fixture()
        first = _compute_scope_digest(plan, owner_id)
        changed = CrawlPlanVersion(
            id=plan.id,
            owner_id=plan.owner_id,
            version=plan.version + 1,
            sources=plan.sources,
            rules_version=plan.rules_version,
        )
        assert first == _compute_scope_digest(plan, owner_id)
        assert first != _compute_scope_digest(changed, owner_id)


class _RaisingResolver:
    async def resolve(self, context_id: str, operation: str, expected_digest: str):
        del context_id, operation, expected_digest
        raise RuntimeError("stale")


class _RaisingRunner:
    async def run_stage(self, stage: str, context_digest: str, input_refs: list[dict[str, Any]]):
        del stage, context_digest, input_refs
        raise RuntimeError("runner unavailable")


class _RaisingInvoker:
    async def invoke(
        self,
        operation: str,
        context_digest: str,
        consent_id: str,
        preflight_id: str,
        field_set_hash: str,
        provider_policy_version: str,
    ):
        del (
            operation,
            context_digest,
            consent_id,
            preflight_id,
            field_set_hash,
            provider_policy_version,
        )
        raise RuntimeError("model unavailable")


class _RaisingPersister:
    async def persist(
        self,
        stage: str,
        event_key: str,
        sequence: int,
        outcome: str,
        redacted_payload: dict[str, Any],
        result_digest: str,
    ):
        del stage, event_key, sequence, outcome, redacted_payload, result_digest
        raise RuntimeError("persist unavailable")


class _RaisingReconciler:
    async def reconcile(
        self,
        expected_attempt_id: str,
        expected_lease_epoch: int,
        expected_cancel_epoch: int,
        terminal_state: str,
    ):
        del expected_attempt_id, expected_lease_epoch, expected_cancel_epoch, terminal_state
        raise RuntimeError("reconcile unavailable")


def _activity_inputs() -> tuple[
    ResolveAgentContextInput,
    RunDeterministicStageInput,
    InvokeModelStageInput,
    PersistAgentStageInput,
    ReconcileAgentRunInput,
]:
    return (
        ResolveAgentContextInput("context-1", "resume_review", "digest-1"),
        RunDeterministicStageInput("normalize", "digest-1", [InputRef("job", "job-1", 1)]),
        InvokeModelStageInput(
            "resume_review", "digest-1", "consent-1", "preflight-1", "hash-1", "policy-1"
        ),
        PersistAgentStageInput(
            "invoke_model_stage", "event-1", 3, "completed", {"safe": True}, "digest-1"
        ),
        ReconcileAgentRunInput("attempt-1", 1, 0, "succeeded"),
    )


class TestAgentActivities:
    @pytest.mark.asyncio
    async def test_noop_activity_bundle_returns_typed_receipts(self) -> None:
        activities = AgentActivities(
            context_resolver=NoOpAgentContextResolver(),
            deterministic_runner=NoOpDeterministicStageRunner(),
            model_invoker=NoOpModelStageInvoker(),
            stage_persister=NoOpAgentStagePersister(),
            run_reconciler=NoOpAgentRunReconciler(),
        )
        context, deterministic, model, persist, reconcile = _activity_inputs()
        receipts = [
            await activities.resolve_agent_context(context),
            await activities.run_deterministic_stage(deterministic),
            await activities.invoke_model_stage(model),
            await activities.persist_agent_stage(persist),
            await activities.reconcile_agent_run(reconcile),
        ]
        assert [receipt.outcome for receipt in receipts] == ["completed"] * 5
        assert receipts[0].output["state"] == "active"
        assert receipts[1].output["verdict"] == "pass"
        assert receipts[2].output["provider_id"] == "noop-provider"
        assert receipts[3].output["stored_state"] == "completed"
        assert receipts[4].next_state == "succeeded"

    @pytest.mark.asyncio
    async def test_missing_activity_wiring_fails_closed(self) -> None:
        activities = AgentActivities()
        context, deterministic, model, persist, reconcile = _activity_inputs()
        requests = [
            (activities.resolve_agent_context, context, "CONTEXT_STALE"),
            (activities.run_deterministic_stage, deterministic, "RuntimeError"),
            (activities.invoke_model_stage, model, "RuntimeError"),
            (activities.persist_agent_stage, persist, "RuntimeError"),
            (activities.reconcile_agent_run, reconcile, "RuntimeError"),
        ]
        for method, request, error_type in requests:
            with pytest.raises(ApplicationError) as error:
                await method(request)
            assert error_type in str(error.value) or error.value.type == error_type

    @pytest.mark.asyncio
    async def test_injected_activity_failures_are_classified(self) -> None:
        context, deterministic, model, persist, reconcile = _activity_inputs()
        activities = AgentActivities(
            context_resolver=_RaisingResolver(),
            deterministic_runner=_RaisingRunner(),
            model_invoker=_RaisingInvoker(),
            stage_persister=_RaisingPersister(),
            run_reconciler=_RaisingReconciler(),
        )
        for method, request, retryable in [
            (activities.resolve_agent_context, context, False),
            (activities.run_deterministic_stage, deterministic, True),
            (activities.invoke_model_stage, model, True),
            (activities.persist_agent_stage, persist, True),
            (activities.reconcile_agent_run, reconcile, False),
        ]:
            with pytest.raises(ApplicationError) as error:
                await method(request)
            assert error.value.non_retryable is (not retryable)


class _ReleasedDecision:
    def __init__(self, released: bool) -> None:
        self.released = released


class _CapabilityResolver:
    def __init__(self, decision: object = None, *, fail: bool = False) -> None:
        self.decision = decision
        self.fail = fail

    def decide(self, operation: ModelOperation) -> object:
        del operation
        if self.fail:
            raise RuntimeError("resolver down")
        return self.decision


class TestPreflightService:
    def test_capability_resolution_covers_default_disabled_and_unavailable(self) -> None:
        default = PreflightService().resolve_capability(CANDIDATE_ID, ModelOperation.RESUME_REVIEW)
        disabled = PreflightService(
            capability_resolver=_CapabilityResolver(_ReleasedDecision(False))
        ).resolve_capability(CANDIDATE_ID, ModelOperation.RESUME_REVIEW)
        unavailable = PreflightService(
            capability_resolver=_CapabilityResolver(fail=True)
        ).resolve_capability(CANDIDATE_ID, ModelOperation.RESUME_REVIEW)
        assert default.may_start
        assert disabled.reason_code == "MODEL_PROVIDER_DISABLED"
        assert unavailable.reason_code == "RESOLVER_UNAVAILABLE"

    def test_preflight_ready_record_is_owned_and_retrievable(self) -> None:
        context_service = ContextService()
        context = context_service.create(
            CANDIDATE_ID,
            CreateContext(operation=ModelOperation.RESUME_REVIEW, job_id="job-1", job_version=1),
            now=NOW,
        )
        service = PreflightService(context_service=context_service)
        preflight = service.create_preflight(
            CANDIDATE_ID,
            PreflightRequest(context_id=context.context_id, operation=ModelOperation.RESUME_REVIEW),
            now=NOW,
        )
        record = service.get_preflight(CANDIDATE_ID, UUID(preflight.preflight_id))
        assert preflight.decision == "ready"
        assert record is not None and record.consent_issued
        assert service.get_preflight(uuid4(), UUID(preflight.preflight_id)) is None

    def test_preflight_blocks_stale_context_and_disabled_capability(self) -> None:
        context_service = ContextService()
        stale = PreflightService(context_service=context_service).create_preflight(
            CANDIDATE_ID,
            PreflightRequest(context_id=str(uuid4()), operation=ModelOperation.RESUME_REVIEW),
            now=NOW,
        )
        context = context_service.create(
            CANDIDATE_ID,
            CreateContext(operation=ModelOperation.RESUME_REVIEW, job_id="job-1", job_version=1),
            now=NOW,
        )
        disabled_service = PreflightService(
            capability_resolver=_CapabilityResolver(_ReleasedDecision(False)),
            context_service=context_service,
        )
        disabled = disabled_service.create_preflight(
            CANDIDATE_ID,
            PreflightRequest(context_id=context.context_id, operation=ModelOperation.RESUME_REVIEW),
            now=NOW,
        )
        assert stale.decision == "blocked" and stale.reason_code == "CONTEXT_STALE"
        assert disabled.decision == "blocked"
        assert disabled.reason_code == "MODEL_PROVIDER_DISABLED"


def _workflow_request() -> AgentRunWorkflowInput:
    return AgentRunWorkflowInput(
        candidate_id=str(CANDIDATE_ID),
        logical_run_id="logical-run-1",
        context_id="context-1",
        operation="resume_review",
        context_digest="context-digest",
        policy_version="policy-v1",
        consent_id="consent-1",
        trace_id="trace-1",
        request_fingerprint="field-set-hash",
    )


def _workflow_receipt(activity_name: str) -> ActivityReceipt:
    outputs: dict[str, dict[str, object]] = {
        "careerops.agent.resolve_agent_context": {
            "context_id": "context-1",
            "state": "active",
        },
        "careerops.agent.run_deterministic_stage": {
            "stage": "normalize",
            "result_digest": "deterministic-digest",
        },
        "careerops.agent.invoke_model_stage": {
            "response_digest": "response-digest",
            "provider_id": "provider",
        },
        "careerops.agent.persist_agent_stage": {
            "stage_event_id": "stage-event-1",
        },
        "careerops.agent.reconcile_agent_run": {
            "execution_state": "succeeded",
            "capability_state": "enabled",
            "review_state": "not_required",
            "current_attempt": 1,
            "reconciled_event_key": "event-final",
        },
    }
    stage = activity_name.rsplit(".", 1)[-1]
    return ActivityReceipt(
        attempt_id="attempt-1",
        lease_epoch=1,
        cancel_epoch=0,
        stage=stage,
        event_key=f"event-{stage}",
        sequence=1,
        outcome="completed",
        next_state="running",
        output=outputs[activity_name],
    )


class TestAgentRunWorkflow:
    @pytest.mark.asyncio
    async def test_stage_helpers_and_terminal_projection(self, monkeypatch) -> None:
        async def execute_activity(name: str, request: object, **kwargs: object) -> ActivityReceipt:
            del request, kwargs
            return _workflow_receipt(name)

        monkeypatch.setattr(workflow_module.workflow, "execute_activity", execute_activity)
        workflow = workflow_module.AgentRunWorkflow()
        request = _workflow_request()

        resolve = await workflow._execute_resolve_context(request, "attempt-1", 1, 0)
        deterministic = await workflow._execute_deterministic_stage(request, "attempt-1", 1, 0)
        model = await workflow._execute_model_stage(request, "attempt-1", 1, 0)
        persist = await workflow._execute_persist_stage(request, "attempt-1", 1, 0, model)
        result = await workflow._reconcile_terminal(request, "attempt-1", 1, 0, "succeeded")

        workflow._advance(
            "failed-stage",
            ActivityReceipt(
                "a", 1, 0, "failed-stage", "e", 1, "failed", "failed", {"failure_code": "X"}
            ),
        )
        assert resolve.output["context_id"] == "context-1"
        assert deterministic.output["stage"] == "normalize"
        assert persist.output["stage_event_id"] == "stage-event-1"
        assert result.execution_state == "succeeded"
        assert workflow.status().completed == 2
        assert workflow.status().failure_code == "X"

    def test_signals_are_idempotent_and_refresh_only_while_active(self) -> None:
        workflow = workflow_module.AgentRunWorkflow()
        workflow.stop(workflow_module.StopSignal(reason_code="USER_STOPPED", request_id="r1"))
        workflow.stop(workflow_module.StopSignal(reason_code="OPERATOR_STOPPED", request_id="r2"))
        assert workflow.status().execution_state == "cancel_requested"
        workflow.refresh_context(
            workflow_module.RefreshContextSignal("context-2", "digest-2", "event-2")
        )
        assert workflow._refresh is None


class TestAgentConsoleMappers:
    def test_row_mappers_round_trip_defaults_and_optional_values(self) -> None:
        context_id = uuid4()
        candidate_id = uuid4()
        action_id = uuid4()
        event_key = uuid4()
        attempt_id = uuid4()
        run_id = uuid4()
        base = {
            "id": context_id,
            "candidate_id": candidate_id,
            "operation": "resume_review",
            "schema_version": "v1",
            "digest_algorithm": "sha256",
            "source_version_snapshot": {"job": 1},
            "source_digests": {"job": DIGEST_A},
            "allowed_operation": "resume_review",
            "state": "active",
            "created_at": NOW,
            "expires_at": NOW + timedelta(hours=1),
        }
        context = repo_module._context(cast(Any, base))
        assert context.source_version_snapshot.job == 1
        assert context.source_digests.job == DIGEST_A

        action = repo_module._action(
            cast(
                Any,
                {
                    "id": action_id,
                    "candidate_id": candidate_id,
                    "action_key": "action-1",
                    "source_event_key": event_key,
                    "queue_version": 2,
                    "state": "proposed",
                    "kind": "resume_review",
                    "reason_code": "NEW_JOB",
                    "deterministic_rank": 1,
                    "source_refs": [{"type": "job", "id": "job-1"}, "ignored"],
                },
            )
        )
        attempt = repo_module._attempt(
            cast(
                Any,
                {
                    "id": attempt_id,
                    "run_id": run_id,
                    "candidate_id": candidate_id,
                    "attempt_no": 1,
                    "workflow_id": "workflow-1",
                    "worker_id": None,
                    "lease_epoch": 1,
                    "cancel_epoch": 0,
                    "state": "queued",
                    "retry_budget": 2,
                },
            )
        )
        stage = repo_module._stage_event(
            cast(
                Any,
                {
                    "id": uuid4(),
                    "attempt_id": attempt_id,
                    "run_id": run_id,
                    "candidate_id": candidate_id,
                    "event_key": event_key,
                    "sequence": 1,
                    "schema_version": "v1",
                    "stage": "normalize",
                    "status": "completed",
                    "terminal": True,
                    "retryable": False,
                    "occurred_at": NOW,
                    "duration_ms": 12,
                    "redacted_payload": {"safe": True},
                },
            )
        )
        fallback_context = repo_module._context(
            cast(
                Any,
                {
                    **base,
                    "source_version_snapshot": None,
                    "source_digests": None,
                },
            )
        )
        assert action.source_refs == ({"type": "job", "id": "job-1"},)
        assert attempt.worker_id is None
        assert stage.redacted_payload == {"safe": True}
        assert fallback_context.source_version_snapshot.job is None

    def test_error_code_enum_is_closed_and_stable(self) -> None:
        values = {code.value for code in AgentConsoleErrorCode}
        assert "DEPENDENCY_NOT_READY" in values
        assert "INTERNAL_SAFE_FAILURE" in values


class TestSidecarClient:
    @pytest.mark.parametrize(
        "handler",
        [
            lambda request: httpx.Response(404, json={}, request=request),
            lambda request: httpx.Response(200, content=b"not-json", request=request),
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline", request=request)),
        ],
    )
    def test_check_ready_fails_closed(self, handler) -> None:
        client = _client_with_handler(handler)
        try:
            with pytest.raises((SidecarUnavailableError, SidecarContractViolation)):
                client.check_ready()
        finally:
            client.close()

    def test_check_ready_returns_not_ready_and_rejects_digest_mismatch(self) -> None:
        not_ready = _client_with_handler(
            lambda request: httpx.Response(
                503,
                json=_ready_payload(ready=False, reason_code="IMAGE_NOT_PINNED"),
                request=request,
            )
        )
        try:
            response = not_ready.check_ready()
            assert not response.ready and not not_ready.is_ready
        finally:
            not_ready.close()

        mismatch = SidecarClient(
            SidecarClientConfig(base_url="http://sidecar.test", expected_image_digest=DIGEST_B)
        )
        mismatch._client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=_ready_payload(), request=request)
            ),
            base_url="http://sidecar.test",
        )
        try:
            with pytest.raises(SidecarUnavailableError, match="image_digest"):
                mismatch.check_ready()
        finally:
            mismatch.close()

    def test_execute_run_success_failure_and_headers(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_success_payload(), request=request)

        client = _ready_client(handler)
        request = _run_request()
        try:
            result = client.execute_run(request, signature_bundle=_signature(), trace_id="trace-x")
            assert isinstance(result, RunSuccessResponse)
            assert result.postings[0].title == "Backend Engineer"
            assert requests[0].headers["x-careerops-trace-id"] == "trace-x"
            assert json.loads(requests[0].content)["task_space_nonce"] == "nonce-1"
        finally:
            client.close()

        failure_client = _ready_client(
            lambda req: httpx.Response(403, json=_failure_payload(), request=req)
        )
        try:
            failure = failure_client.execute_run(
                _run_request(nonce="nonce-failure"),
                signature_bundle=_signature(nonce="nonce-failure"),
            )
            assert isinstance(failure, RunFailureResponse)
        finally:
            failure_client.close()

    def test_execute_run_rejects_not_ready_replay_and_old_or_invalid_timestamp(self) -> None:
        not_ready = SidecarClient(SidecarClientConfig(base_url="http://sidecar.test"))
        with pytest.raises(SidecarUnavailableError):
            not_ready.execute_run(_run_request(), signature_bundle=_signature())

        client = _ready_client(
            lambda req: httpx.Response(200, json=_success_payload(), request=req)
        )
        try:
            client.execute_run(_run_request(), signature_bundle=_signature())
            with pytest.raises(SidecarContractViolation, match="replay"):
                client.execute_run(_run_request(), signature_bundle=_signature())
            with pytest.raises(SidecarContractViolation, match="drift"):
                client.execute_run(
                    _run_request(nonce="nonce-old"),
                    signature_bundle=_signature(
                        nonce="nonce-old", timestamp=(NOW - timedelta(hours=2)).isoformat()
                    ),
                )
            with pytest.raises(SidecarContractViolation, match="invalid timestamp"):
                client.execute_run(
                    _run_request(nonce="nonce-bad"),
                    signature_bundle=_signature(nonce="nonce-bad", timestamp="not-a-date"),
                )
        finally:
            client.close()

    @pytest.mark.parametrize(
        "case",
        ["request_error", "large_response", "invalid_json", "unexpected_status"],
    )
    def test_execute_run_fail_closed_on_transport_size_json_and_status(self, case: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if case == "request_error":
                raise httpx.ReadTimeout("timed out", request=request)
            if case == "large_response":
                return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1), request=request)
            if case == "invalid_json":
                return httpx.Response(200, content=b"not-json", request=request)
            return httpx.Response(500, json={}, request=request)

        client = _ready_client(handler)
        try:
            with pytest.raises((SidecarUnavailableError, SidecarContractViolation)):
                client.execute_run(
                    _run_request(nonce=f"nonce-{case}"),
                    signature_bundle=_signature(nonce=f"nonce-{case}"),
                )
        finally:
            client.close()

    def test_execute_run_rejects_oversized_request_and_builds_canonical_request(self) -> None:
        client = _ready_client(
            lambda req: httpx.Response(200, json=_success_payload(), request=req)
        )
        try:
            with pytest.raises(SidecarContractViolation, match="request body"):
                client.execute_run(
                    _run_request(nonce="nonce-large", run_id="x" * 70_000),
                    signature_bundle=_signature(nonce="nonce-large"),
                )
            built = client.build_run_request(
                run_id=uuid4(),
                attempt_id=uuid4(),
                candidate_scope_digest="scope",
                source_id="source",
                canonical_start_url="HTTPS://Example.Test:443/start",
                allowlist_version="v1",
            )
            assert built.canonical_start_url == "https://example.test:443/start"
            assert built.budgets["max_requests"] == 60
        finally:
            client.close()
