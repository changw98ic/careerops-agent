"""Review endpoint lifecycle tests (plan v0.4 §2.7 / §5 Stage 3).

Verifies the post-login ``POST /api/v1/review/{approval_id}`` behavior
(auth-rm Task 10: the session/CSRF/origin gates are gone; the endpoint trusts
the loopback reviewer):

- rate limited -> 429
- duplicate decision (same action re-submitted) -> 200 cached
- conflicting decision (different action on a completed approval) -> 409
- approval records the fixed ``local-reviewer`` actor (actor_type=USER)
- PRODUCTION does not mount the router -> 404
- edit flow records a reject and does not send

Also covers ``SettingsCapabilityResolver`` (the v1 production resolver): trusted
facts are derived from settings + draft state, evidence stays non-empty, and the
resource id is deterministic.

The graph is driven to ``review_gate`` with the production
``SettingsCapabilityResolver`` so the endpoint test exercises the real
authorization chain end-to-end (capability -> policy -> approval -> send).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from careerops.api.app import create_app
from careerops.api.routes.review import install_review_endpoint
from careerops.application.audit import AuditActorType
from careerops.application.side_effect_kernel import SideEffectKernel
from careerops.config import RuntimeEnvironment, Settings
from careerops.domain.side_effects import ApprovalDecision
from careerops.infrastructure.database.side_effect_memory import InMemorySideEffectStore
from careerops.infrastructure.rate_limit import RateLimitAction, RateLimiter
from careerops.integrations.fake_side_effect_provider import FakeSideEffectProvider
from careerops.model_gateway.base import DisabledModelAdapter
from careerops.orchestration import (
    ContactDTO,
    DraftDTO,
    InMemoryReviewMappingStore,
    RawJobDTO,
    build_graph,
)
from careerops.orchestration.capability_resolver import SettingsCapabilityResolver

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
THREAD_ID = "t-review-endpoint"
REVIEWER_ACTOR = "local-reviewer"


# ---------------------------------------------------------------------------
# Inline fixtures (no conftest)
# ---------------------------------------------------------------------------


class AllowAllRateLimiter:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_actions: list[RateLimitAction] = []

    def check(self, action: RateLimitAction, subject_hash: str, *, now: datetime) -> bool:
        del subject_hash, now
        self.calls += 1
        self.seen_actions.append(action)
        return True


class DenyAllRateLimiter:
    def check(self, action: RateLimitAction, subject_hash: str, *, now: datetime) -> bool:
        del action, subject_hash, now
        return False


def _job() -> RawJobDTO:
    return RawJobDTO(
        external_id="job-1",
        title="Backend Engineer",
        location="SF",
        url="https://example.com/jobs/1",
        description="Python and FastAPI role",
        source_url="https://example.com/jobs/1",
        fetched_at=NOW.isoformat(),
        response_hash="abc",
        parser_version="test-v1",
        raw_data={"description": "Python and FastAPI role"},
    )


def _contact() -> ContactDTO:
    return ContactDTO(
        email="hiring@example.com",
        platform="greenhouse",
        company_hint="Example",
        post_type="hiring",
        context="We are hiring",
        source_file="greenhouse.json",
        publicly_listed=True,
        extracted_at=NOW.isoformat(),
    )


def _build_stack(
    *, settings: Settings | None = None
) -> tuple[object, SideEffectKernel, InMemoryReviewMappingStore]:
    resolved = settings or Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    store = InMemorySideEffectStore()
    provider = FakeSideEffectProvider()
    kernel = SideEffectKernel(store, provider, approval_ttl_seconds=3600)
    review_mapping = InMemoryReviewMappingStore()

    def crawler() -> tuple[RawJobDTO, ...]:
        return (_job(),)

    def extractor(jobs: tuple[RawJobDTO, ...]) -> tuple[ContactDTO, ...]:
        del jobs
        return (_contact(),)

    graph = build_graph(
        crawler=crawler,
        extractor=extractor,
        resume_text="I know python, fastapi, postgres, react, 5+ years",
        model_client=DisabledModelAdapter(),
        kernel=kernel,
        review_mapping=review_mapping,
        capability_resolver=SettingsCapabilityResolver(resolved),
        checkpointer=MemorySaver(),
    )
    return graph, kernel, review_mapping


def _drive_to_review(graph: object, *, thread_id: str, requested_for: str) -> dict[str, str]:
    """Run the graph to completion and return the (escalated) approval/intent ids.

    With the autonomous A/B loop and a DISABLED model, review_gate escalates
    every draft (default-deny), leaving one PENDING approval for the human
    fallback. The ids are read from terminal state — there is no interrupt to
    resume. (Method name retained for brevity; it no longer "drives to" an
    interrupt.)
    """
    cfg: dict[str, object] = {"configurable": {"thread_id": thread_id}}
    graph.invoke({"requested_for": requested_for}, cfg)  # type: ignore[attr-defined]
    state = graph.get_state(cfg)  # type: ignore[attr-defined]
    assert state.next == (), state.next
    values = state.values
    return {
        "approval_id": str(values["pending_approval_id"]),
        "intent_id": str(values["pending_intent_id"]),
    }  # type: ignore[index]


def _make_client(
    graph: object,
    kernel: SideEffectKernel,
    review_mapping: InMemoryReviewMappingStore,
    *,
    rate_limiter: RateLimiter | None = None,
) -> TestClient:
    app = FastAPI()
    install_review_endpoint(
        app,
        rate_limiter=rate_limiter or AllowAllRateLimiter(),
        review_mapping=review_mapping,
        career_graph=graph,
        side_effect_kernel=kernel,
        now_provider=lambda: NOW,
    )
    return TestClient(app, follow_redirects=False)


def _review_post(
    client: TestClient,
    approval_id: str,
    payload: dict[str, object],
) -> TestClient.post:  # type: ignore[name-defined]
    return client.post(f"/api/v1/review/{approval_id}", json=payload)


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_rate_limited_returns_429(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping, rate_limiter=DenyAllRateLimiter())

        response = _review_post(client, ids["approval_id"], {"action": "approve"})

        assert response.status_code == 429
        assert "rate limit" in response.json()["error"]

    def test_rate_limiter_keys_on_review_action(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        limiter = AllowAllRateLimiter()
        client = _make_client(graph, kernel, mapping, rate_limiter=limiter)

        _review_post(client, ids["approval_id"], {"action": "approve"})

        assert limiter.seen_actions == [RateLimitAction.REVIEW]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Decision lifecycle: approve / duplicate / conflict / audit actor
# ---------------------------------------------------------------------------


class TestDecisionLifecycle:
    def test_approve_runs_send_and_returns_receipt(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        response = _review_post(client, ids["approval_id"], {"action": "approve"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "decided"
        assert body["decision"] == "approved"
        assert body["approval_id"] == ids["approval_id"]
        assert body["receipt"]["final_state"] == "confirmed"

    def test_approve_records_local_reviewer_actor(self) -> None:
        """The loopback reviewer's approval is audited as USER/local-reviewer."""
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        response = _review_post(client, ids["approval_id"], {"action": "approve"})

        assert response.status_code == 200
        replay = kernel.replay(UUID(ids["intent_id"]))
        approved = [
            event
            for event in replay.audit_events
            if event.event_type == "side_effect_approved"
        ]
        assert len(approved) == 1
        assert approved[0].actor_type is AuditActorType.USER
        assert approved[0].actor_id == REVIEWER_ACTOR

    def test_duplicate_approve_returns_cached_200(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        first = _review_post(client, ids["approval_id"], {"action": "approve"})
        second = _review_post(client, ids["approval_id"], {"action": "approve"})

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["status"] == "already_decided"
        assert second.json()["decision"] == "approve"

    def test_conflicting_decision_returns_409(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        approve = _review_post(client, ids["approval_id"], {"action": "approve"})
        reject = _review_post(client, ids["approval_id"], {"action": "reject"})

        assert approve.status_code == 200
        assert reject.status_code == 409
        body = reject.json()
        assert body["status"] == "conflict"
        assert body["existing_decision"] == "approve"

    def test_reject_after_reject_is_duplicate(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        first = _review_post(client, ids["approval_id"], {"action": "reject"})
        second = _review_post(client, ids["approval_id"], {"action": "reject"})

        assert first.status_code == 200
        assert first.json()["decision"] == "rejected"
        assert second.status_code == 200
        assert second.json()["status"] == "already_decided"

    def test_unknown_approval_returns_404(self) -> None:
        graph, kernel, mapping = _build_stack()
        client = _make_client(graph, kernel, mapping)

        response = _review_post(client, "00000000-0000-0000-0000-000000000099", {"action": "approve"})

        assert response.status_code == 404


class TestEditFlow:
    def test_edit_records_reject_and_does_not_send(self) -> None:
        """The graph-level interrupt edit loop is gone (A/B revises internally).
        A human ``edit`` on an escalated approval is recorded as a REJECT (the
        current approval is invalidated); no send occurs and no new approval is
        created."""
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        # Discover the draft id produced by the draft node.
        cfg: dict[str, object] = {"configurable": {"thread_id": THREAD_ID}}
        drafts = graph.get_state(cfg).values.get("drafts")  # type: ignore[attr-defined]
        draft_id = drafts[0]["id"]  # type: ignore[index]

        response = _review_post(
            client,
            ids["approval_id"],
            {
                "action": "edit",
                "edited_drafts": [{"id": draft_id, "subject": "Edited", "body": "Edited body"}],
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "edit_recorded"
        assert body["decision"] == "rejected"
        # No new approval is minted by the human-edit fallback.
        assert "new_approval_id" not in body

        # The approval is REJECTED in the kernel; nothing was sent.
        replay = kernel.replay(UUID(ids["intent_id"]))
        assert replay.approvals[0].decision is ApprovalDecision.REJECTED
        assert len(replay.receipts) == 0


# ---------------------------------------------------------------------------
# PRODUCTION fail-closed: router not mounted
# ---------------------------------------------------------------------------


class TestProductionGuard:
    def test_production_does_not_mount_review_router(self) -> None:
        prod_settings = Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            environment=RuntimeEnvironment.PRODUCTION,
        )
        app = create_app(prod_settings)

        client = TestClient(app, follow_redirects=False)
        response = client.post(
            "/api/v1/review/00000000-0000-0000-0000-000000000001",
            json={"action": "approve"},
        )

        # Router is absent in PRODUCTION -> 404, never a silent in-memory resume.
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# SettingsCapabilityResolver unit tests
# ---------------------------------------------------------------------------


def _draft(recipient: str = "hiring@example.com") -> DraftDTO:
    return DraftDTO(
        id="d1",
        job_external_id="job-1",
        recipient=recipient,
        subject="Application",
        body="Body",
        revision=0,
    )


class TestSettingsCapabilityResolver:
    def test_dev_environment_with_recipient_releases_capability(self) -> None:
        settings = Settings(_env_file=None, environment=RuntimeEnvironment.DEVELOPMENT)  # pyright: ignore[reportCallIssue]
        resolver = SettingsCapabilityResolver(settings)

        capability = resolver.for_send_batch((_draft(),))

        assert capability.trusted_facts["capability_released"] is True
        assert capability.trusted_facts["target_allowlisted"] is True
        assert capability.evidence_refs  # non-empty (fail-closed bar)
        assert capability.authenticated is True

    def test_production_never_releases_capability(self) -> None:
        settings = Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            environment=RuntimeEnvironment.PRODUCTION,
        )
        resolver = SettingsCapabilityResolver(settings)

        capability = resolver.for_send_batch((_draft(),))

        # PRODUCTION keeps the gate fail-closed via this v1 path.
        assert capability.trusted_facts["capability_released"] is False

    def test_empty_recipient_is_not_allowlisted(self) -> None:
        settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
        resolver = SettingsCapabilityResolver(settings)

        capability = resolver.for_send_batch((_draft(recipient=""),))

        assert capability.trusted_facts["target_allowlisted"] is False
        assert capability.target == {"to": ""}

    def test_any_missing_recipient_blocks_batch(self) -> None:
        settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
        resolver = SettingsCapabilityResolver(settings)

        capability = resolver.for_send_batch((_draft(), _draft(recipient="")))

        assert capability.trusted_facts["target_allowlisted"] is False

    def test_resource_id_is_deterministic_for_same_recipients(self) -> None:
        settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
        resolver = SettingsCapabilityResolver(settings)

        first = resolver.for_send_batch((_draft(),))
        second = resolver.for_send_batch((_draft(),))

        assert first.resource_id == second.resource_id

    def test_empty_batch_is_rejected(self) -> None:
        settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
        resolver = SettingsCapabilityResolver(settings)

        try:
            resolver.for_send_batch(())
        except ValueError:
            return
        raise AssertionError("empty batch must be rejected")
