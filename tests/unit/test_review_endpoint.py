"""Review endpoint security + lifecycle tests (plan v0.4 §2.7 / §5 Stage 3).

Verifies the full gate chain on ``POST /api/v1/review/{approval_id}``:

- unauthenticated -> 401
- missing/wrong CSRF -> 403
- wrong Origin -> 403
- owner mismatch (``requested_for`` != authenticated user) -> 403
- rate limited -> 429
- duplicate decision (same action re-submitted) -> 200 cached
- conflicting decision (different action on a completed approval) -> 409
- PRODUCTION does not mount the router -> 404
- edit flow re-interrupts on a new approval

Also covers ``SettingsCapabilityResolver`` (the v1 production resolver): trusted
facts are derived from settings + draft state, evidence stays non-empty, and the
resource id is deterministic.

The graph is driven to ``review_gate`` with the production
``SettingsCapabilityResolver`` so the endpoint test exercises the real
authorization chain end-to-end (capability -> policy -> approval -> interrupt ->
resume -> send).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from careerops.api.app import create_app
from careerops.api.routes.review import install_review_endpoint
from careerops.application.side_effect_kernel import SideEffectKernel
from careerops.auth.contracts import (
    AuthAction,
    AuthenticatedPrincipal,
    AuthRateLimiter,
    CsrfRejected,
    InvalidSession,
)
from careerops.config import RuntimeEnvironment, Settings
from careerops.infrastructure.database.side_effect_memory import InMemorySideEffectStore
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
from careerops.web import ConsoleWebSettings

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_USER_ID = UUID("00000000-0000-0000-0000-000000000002")
SESSION_TOKEN = "authenticated-session"
CSRF_TOKEN = "authenticated-csrf"
THREAD_ID = "t-review-endpoint"
WEB_SETTINGS = ConsoleWebSettings(
    allowed_hosts=frozenset({"testserver"}),
    allowed_origins=frozenset({"http://testserver"}),
)


# ---------------------------------------------------------------------------
# Inline fixtures (no conftest)
# ---------------------------------------------------------------------------


class StubAuthService:
    """Authenticates exactly one principal; CSRF token is a known constant."""

    def authenticate(self, session_token: str, *, now: datetime) -> AuthenticatedPrincipal:
        del now
        if session_token != SESSION_TOKEN:
            raise InvalidSession("invalid session")
        return AuthenticatedPrincipal(
            user_id=USER_ID,
            username="owner",
            session_id=UUID("00000000-0000-0000-0000-000000000011"),
            csrf_token_hash="not-exposed",
            absolute_expires_at=NOW + timedelta(days=7),
        )

    def validate_csrf(self, principal: AuthenticatedPrincipal, csrf_token: str) -> None:
        del principal
        if csrf_token != CSRF_TOKEN:
            raise CsrfRejected("bad csrf")


class AllowAllRateLimiter:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_actions: list[AuthAction] = []

    def check(self, action: AuthAction, subject_hash: str, *, now: datetime) -> bool:
        del subject_hash, now
        self.calls += 1
        self.seen_actions.append(action)
        return True


class DenyAllRateLimiter:
    def check(self, action: AuthAction, subject_hash: str, *, now: datetime) -> bool:
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
    cfg: dict[str, object] = {"configurable": {"thread_id": thread_id}}
    graph.invoke({"requested_for": requested_for}, cfg)  # type: ignore[attr-defined]
    state = graph.get_state(cfg)  # type: ignore[attr-defined]
    assert state.next == ("review_gate",), state.next
    interrupt = state.tasks[0].interrupts[0].value
    assert isinstance(interrupt, dict)
    return {"approval_id": interrupt["approval_id"], "intent_id": interrupt["intent_id"]}  # type: ignore[index]


def _make_client(
    graph: object,
    kernel: SideEffectKernel,
    review_mapping: InMemoryReviewMappingStore,
    *,
    rate_limiter: AuthRateLimiter | None = None,
    auth_service: object | None = None,
) -> TestClient:
    app = FastAPI()
    install_review_endpoint(
        app,
        auth_service=(auth_service or StubAuthService()),  # type: ignore[arg-type]
        rate_limiter=rate_limiter or AllowAllRateLimiter(),
        review_mapping=review_mapping,
        career_graph=graph,
        side_effect_kernel=kernel,
        web_settings=WEB_SETTINGS,
        now_provider=lambda: NOW,
    )
    return TestClient(app, follow_redirects=False)


def _auth_post(
    client: TestClient,
    approval_id: str,
    payload: dict[str, object],
    *,
    csrf: str = CSRF_TOKEN,
    origin: str = "http://testserver",
    session: str = SESSION_TOKEN,
) -> TestClient.post:  # type: ignore[name-defined]
    return client.post(
        f"/api/v1/review/{approval_id}",
        json=payload,
        cookies={"careerops_session": session},
        headers={"X-CSRF-Token": csrf, "Origin": origin},
    )


# ---------------------------------------------------------------------------
# Auth / CSRF / origin gates
# ---------------------------------------------------------------------------


class TestAuthenticationAndOrigin:
    def test_unauthenticated_returns_401(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        response = client.post(f"/api/v1/review/{ids['approval_id']}", json={"action": "approve"})

        assert response.status_code == 401
        assert response.json()["error"] == "authentication required"

    def test_missing_csrf_returns_403(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        response = client.post(
            f"/api/v1/review/{ids['approval_id']}",
            json={"action": "approve"},
            cookies={"careerops_session": SESSION_TOKEN},
            headers={"Origin": "http://testserver"},
        )

        assert response.status_code == 403
        assert "csrf" in response.json()["error"]

    def test_wrong_csrf_returns_403(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        response = _auth_post(client, ids["approval_id"], {"action": "approve"}, csrf="bad-token")

        assert response.status_code == 403

    def test_wrong_origin_returns_403(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        response = _auth_post(
            client, ids["approval_id"], {"action": "approve"}, origin="https://evil.example"
        )

        assert response.status_code == 403
        assert "origin" in response.json()["error"]


# ---------------------------------------------------------------------------
# Owner check
# ---------------------------------------------------------------------------


class TestOwnerCheck:
    def test_owner_mismatch_returns_403(self) -> None:
        graph, kernel, mapping = _build_stack()
        # The thread belongs to OTHER_USER; the authenticated principal is USER.
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(OTHER_USER_ID))
        client = _make_client(graph, kernel, mapping)

        response = _auth_post(client, ids["approval_id"], {"action": "approve"})

        assert response.status_code == 403
        assert "different user" in response.json()["error"]


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_rate_limited_returns_429(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping, rate_limiter=DenyAllRateLimiter())

        response = _auth_post(client, ids["approval_id"], {"action": "approve"})

        assert response.status_code == 429
        assert "rate limit" in response.json()["error"]

    def test_rate_limiter_keys_on_review_action_and_user(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        limiter = AllowAllRateLimiter()
        client = _make_client(graph, kernel, mapping, rate_limiter=limiter)

        _auth_post(client, ids["approval_id"], {"action": "approve"})

        assert limiter.seen_actions == [AuthAction.REVIEW]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Decision lifecycle: approve / duplicate / conflict
# ---------------------------------------------------------------------------


class TestDecisionLifecycle:
    def test_approve_runs_send_and_returns_receipt(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        response = _auth_post(client, ids["approval_id"], {"action": "approve"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "decided"
        assert body["decision"] == "approved"
        assert body["approval_id"] == ids["approval_id"]
        assert body["receipt"]["final_state"] == "confirmed"

    def test_duplicate_approve_returns_cached_200(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        first = _auth_post(client, ids["approval_id"], {"action": "approve"})
        second = _auth_post(client, ids["approval_id"], {"action": "approve"})

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["status"] == "already_decided"
        assert second.json()["decision"] == "approve"

    def test_conflicting_decision_returns_409(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        approve = _auth_post(client, ids["approval_id"], {"action": "approve"})
        reject = _auth_post(client, ids["approval_id"], {"action": "reject"})

        assert approve.status_code == 200
        assert reject.status_code == 409
        body = reject.json()
        assert body["status"] == "conflict"
        assert body["existing_decision"] == "approve"

    def test_reject_after_reject_is_duplicate(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        first = _auth_post(client, ids["approval_id"], {"action": "reject"})
        second = _auth_post(client, ids["approval_id"], {"action": "reject"})

        assert first.status_code == 200
        assert first.json()["decision"] == "rejected"
        assert second.status_code == 200
        assert second.json()["status"] == "already_decided"

    def test_unknown_approval_returns_404(self) -> None:
        graph, kernel, mapping = _build_stack()
        client = _make_client(graph, kernel, mapping)

        response = _auth_post(client, "00000000-0000-0000-0000-000000000099", {"action": "approve"})

        assert response.status_code == 404


class TestEditFlow:
    def test_edit_reinterrupts_on_new_approval(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        # Discover the draft id produced by the draft node.
        cfg: dict[str, object] = {"configurable": {"thread_id": THREAD_ID}}
        drafts = graph.get_state(cfg).values.get("drafts")  # type: ignore[attr-defined]
        draft_id = drafts[0]["id"]  # type: ignore[index]

        response = _auth_post(
            client,
            ids["approval_id"],
            {
                "action": "edit",
                "edited_drafts": [{"id": draft_id, "subject": "Edited", "body": "Edited body"}],
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "edit_pending"
        assert body["decision"] == "rejected"
        new_approval = body["new_approval_id"]
        assert new_approval is not None
        assert new_approval != ids["approval_id"]

    def test_edit_then_approve_new_approval_succeeds(self) -> None:
        graph, kernel, mapping = _build_stack()
        ids = _drive_to_review(graph, thread_id=THREAD_ID, requested_for=str(USER_ID))
        client = _make_client(graph, kernel, mapping)

        cfg: dict[str, object] = {"configurable": {"thread_id": THREAD_ID}}
        drafts = graph.get_state(cfg).values.get("drafts")  # type: ignore[attr-defined]
        draft_id = drafts[0]["id"]  # type: ignore[index]

        edit_resp = _auth_post(
            client,
            ids["approval_id"],
            {
                "action": "edit",
                "edited_drafts": [{"id": draft_id, "subject": "Edited", "body": "Edited body"}],
            },
        )
        new_approval = edit_resp.json()["new_approval_id"]

        approve_resp = _auth_post(client, new_approval, {"action": "approve"})
        assert approve_resp.status_code == 200
        assert approve_resp.json()["decision"] == "approved"


# ---------------------------------------------------------------------------
# PRODUCTION fail-closed: router not mounted
# ---------------------------------------------------------------------------


class TestProductionGuard:
    def test_production_does_not_mount_review_router(self) -> None:
        prod_settings = Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            environment=RuntimeEnvironment.PRODUCTION,
            console_cookie_secure=True,
            console_allowed_hosts=("prod.example",),
            console_allowed_origins=("https://prod.example",),
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
            console_cookie_secure=True,
            console_allowed_hosts=("prod.example",),
            console_allowed_origins=("https://prod.example",),
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
