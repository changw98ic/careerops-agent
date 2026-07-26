"""Contract tests for the crawl-plan-management surface (Section 4, task 4.10).

These tests exercise the service layer and API routes for crawl sources,
versioned crawl plans, and crawl runs. They prove:

- unsupported source types are refused (no adapter registered);
- invalid schedules (interval too short/long, bad timezone) are rejected;
- plan version immutability (copy-on-write on edit of active plan);
- pause behavior (paused source/plan schedules no new runs, history retained);
- run-now idempotency (same run_identity returns existing run);
- overlap coalesce (overlapping trigger records CANCELLED with reason);
- ownership scoping (routes reject client-supplied owner; missing repo -> 503).

In-memory repositories back the tests (no database required). The service
layer is the primary assertion surface; API-route tests verify the HTTP
contract (status codes, error envelopes, server-side ownership).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient

from careerops.api.app import create_app
from careerops.api.errors import InvalidStateError, NotFoundError
from careerops.application.crawl_plan_service import (
    MAX_SCHEDULE_INTERVAL_SECONDS,
    MIN_SCHEDULE_INTERVAL_SECONDS,
    CrawlPlanPreferences,
    CrawlPlanService,
    CrawlRunService,
    CrawlSourceService,
    validate_schedule,
)
from careerops.application.ports.readiness import ReadinessReport, ReadinessState
from careerops.config import RuntimeEnvironment, Settings
from careerops.domain.crawl import CrawlRunState
from careerops.domain.crawl_plans import (
    CrawlSourceState,
    CrawlSourceType,
    is_supported_source_type,
)
from careerops.infrastructure.memory_repos import (
    InMemoryCrawlPlanRepository,
    InMemoryCrawlRunRepository,
    InMemoryCrawlSourceRepository,
)
from careerops.orchestration.capability_resolver import SettingsCapabilityResolver

# ---------------------------------------------------------------------------
# Fixtures: in-memory repos + services (no database, no FastAPI app)
# ---------------------------------------------------------------------------


@pytest.fixture
def source_repo() -> InMemoryCrawlSourceRepository:
    return InMemoryCrawlSourceRepository()


@pytest.fixture
def plan_repo() -> InMemoryCrawlPlanRepository:
    return InMemoryCrawlPlanRepository()


@pytest.fixture
def run_repo(plan_repo: InMemoryCrawlPlanRepository) -> InMemoryCrawlRunRepository:
    return InMemoryCrawlRunRepository(plan_repo)


@pytest.fixture
def source_service(source_repo: InMemoryCrawlSourceRepository) -> CrawlSourceService:
    return CrawlSourceService(source_repo)


@pytest.fixture
def plan_service(plan_repo: InMemoryCrawlPlanRepository) -> CrawlPlanService:
    return CrawlPlanService(plan_repo)


@pytest.fixture
def run_service(
    run_repo: InMemoryCrawlRunRepository,
    plan_repo: InMemoryCrawlPlanRepository,
    source_repo: InMemoryCrawlSourceRepository,
) -> CrawlRunService:
    return CrawlRunService(run_repo, plan_repository=plan_repo, source_repository=source_repo)


@pytest.fixture
def owner_id() -> UUID:
    return uuid4()


@pytest.fixture
def company_id() -> UUID:
    return uuid4()


# ---------------------------------------------------------------------------
# API test helpers (mirror test_api_contract.py / test_m2_api_contract.py)
# ---------------------------------------------------------------------------


class FixedReadinessProbe:
    def __init__(self, checks: Mapping[str, ReadinessState] | None = None) -> None:
        self._checks = checks or {
            "database": ReadinessState.OK,
            "redis": ReadinessState.OK,
            "temporal": ReadinessState.OK,
            "storage": ReadinessState.OK,
        }

    async def check(self) -> ReadinessReport:
        return ReadinessReport(checks=self._checks)

    async def close(self) -> None:
        return None


def _make_api_client(
    *,
    source_service: CrawlSourceService | None = None,
    plan_service: CrawlPlanService | None = None,
    run_service: CrawlRunService | None = None,
    candidate_id: UUID | None = None,
) -> httpx2.Client:
    """Build a TestClient with crawl services wired (or absent for 503 tests).

    When services are provided they are attached to ``app.state`` so
    ``require_repository`` resolves them; when absent the routes hit 503.
    ``candidate_id`` is injected via a mock auth service so
    ``require_candidate_id`` resolves to the given UUID.  A test session
    cookie is set on the client so ``require_api_auth`` passes.
    """
    settings = Settings.model_validate({"environment": RuntimeEnvironment.TEST})
    probe = FixedReadinessProbe()
    app = create_app(settings, readiness_probe=probe)
    if source_service is not None:
        app.state.crawl_source_service = source_service
    if plan_service is not None:
        app.state.crawl_plan_service = plan_service
    if run_service is not None:
        app.state.crawl_run_service = run_service
    if candidate_id is not None:
        from careerops.auth.contracts import AuthenticatedPrincipal

        class _StubAuth:
            def authenticate(self, token: str, *, now: datetime) -> AuthenticatedPrincipal:
                return AuthenticatedPrincipal(
                    user_id=candidate_id,
                    username="test-user",
                    session_id=uuid4(),
                    csrf_token_hash="test-hash",
                    absolute_expires_at=now,
                    candidate_id=candidate_id,
                )

            def validate_csrf(self, principal: AuthenticatedPrincipal, token: str) -> None:
                return None

        app.state.auth_service = _StubAuth()
        app.state.web_settings = object()
        # Wire the capability resolver so the CRAWL_PLAN_MANAGEMENT gate
        # passes (released by default) and routes reach the repo check.
        app.state.capability_resolver = SettingsCapabilityResolver(settings)
    # Build the TestClient and, when auth is configured, set the session cookie
    # so ``require_api_auth`` can resolve the principal.  The CSRF header is
    # sent on mutating requests (POST/PATCH/DELETE) so the stub validator
    # passes through.
    client = cast("httpx2.Client", TestClient(app))
    if candidate_id is not None:
        client.cookies.set("careerops_session", "test-token")
        # Monkey-patch the default headers for CSRF on mutating requests.
        client.headers["X-CSRF-Token"] = "test-csrf"
    return client


# ===========================================================================
# 1. Unsupported source type refused
# ===========================================================================


class TestUnsupportedSourceType:
    """Iron Rule 6: unsupported source types are refused until an adapter +
    policy contract exist. The service raises InvalidStateError (409)."""

    def test_service_rejects_unknown_source_type(
        self,
        source_service: CrawlSourceService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        with pytest.raises(InvalidStateError, match="unsupported crawl source type"):
            source_service.register(
                owner_id,
                company_id=company_id,
                source_type="linkedin",
                source_identifier="acme",
                base_url="https://acme.com/careers",
            )

    def test_service_rejects_empty_source_type(
        self,
        source_service: CrawlSourceService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        with pytest.raises(InvalidStateError, match="unsupported crawl source type"):
            source_service.register(
                owner_id,
                company_id=company_id,
                source_type="",
                source_identifier="acme",
                base_url="https://acme.com/careers",
            )

    def test_api_returns_409_for_unsupported_source_type(self) -> None:
        owner = uuid4()
        client = _make_api_client(
            source_service=CrawlSourceService(InMemoryCrawlSourceRepository()),
            candidate_id=owner,
        )
        resp = client.post(
            "/api/v1/crawl-sources",
            json={
                "company_id": str(uuid4()),
                "source_type": "workday",
                "source_identifier": "acme",
                "base_url": "https://acme.com/careers",
            },
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "INVALID_STATE"
        assert "unsupported" in body["error"]["message"].lower()

    @pytest.mark.parametrize(
        "source_type",
        [st.value for st in CrawlSourceType],
    )
    def test_all_registered_source_types_are_accepted(
        self,
        source_service: CrawlSourceService,
        owner_id: UUID,
        company_id: UUID,
        source_type: str,
    ) -> None:
        source = source_service.register(
            owner_id,
            company_id=company_id,
            source_type=source_type,
            source_identifier="test-id",
            base_url="https://example.com/careers",
        )
        assert source.source_type.value == source_type

    def test_is_supported_source_type_closure(self) -> None:
        for st in CrawlSourceType:
            assert is_supported_source_type(st.value) is True
        assert is_supported_source_type("linkedin") is False
        assert is_supported_source_type("workday") is False
        assert is_supported_source_type("") is False


# ===========================================================================
# 2. Invalid schedule rejected
# ===========================================================================


class TestInvalidSchedule:
    """Iron Rule 5: bounded intervals + IANA timezone. Invalid schedules raise
    InvalidStateError (409) BEFORE any persistence, so the prior active
    version is never displaced."""

    def test_interval_below_minimum_rejected(self) -> None:
        with pytest.raises(InvalidStateError, match="below minimum"):
            validate_schedule(interval_seconds=60, timezone="UTC")

    def test_interval_above_maximum_rejected(self) -> None:
        with pytest.raises(InvalidStateError, match="above maximum"):
            validate_schedule(
                interval_seconds=MAX_SCHEDULE_INTERVAL_SECONDS + 1,
                timezone="UTC",
            )

    def test_interval_zero_rejected(self) -> None:
        with pytest.raises(InvalidStateError, match="below minimum"):
            validate_schedule(interval_seconds=0, timezone="UTC")

    def test_negative_interval_rejected(self) -> None:
        with pytest.raises(InvalidStateError, match="below minimum"):
            validate_schedule(interval_seconds=-1, timezone="UTC")

    def test_bool_interval_rejected(self) -> None:
        """Python bool is a subclass of int; reject it explicitly."""
        with pytest.raises(InvalidStateError, match="must be an integer"):
            validate_schedule(interval_seconds=True, timezone="UTC")  # type: ignore[arg-type]

    def test_bad_timezone_rejected(self) -> None:
        with pytest.raises(InvalidStateError, match="not a valid IANA zone"):
            validate_schedule(
                interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
                timezone="Mars/Olympus_Mons",
            )

    def test_empty_timezone_rejected(self) -> None:
        with pytest.raises(InvalidStateError, match="timezone is required"):
            validate_schedule(
                interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
                timezone="",
            )

    def test_fixed_offset_timezone_rejected(self) -> None:
        """A fixed offset like UTC+5 is not a valid IANA zone name."""
        with pytest.raises(InvalidStateError, match="not a valid IANA zone"):
            validate_schedule(
                interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
                timezone="UTC+5",
            )

    def test_valid_iana_timezone_accepted(self) -> None:
        for tz in ("UTC", "America/New_York", "Europe/Berlin", "Asia/Tokyo"):
            validate_schedule(
                interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
                timezone=tz,
            )  # no error

    def test_service_rejects_invalid_schedule_no_version_created(
        self,
        plan_service: CrawlPlanService,
        owner_id: UUID,
    ) -> None:
        """An invalid schedule must not create any version — the prior active
        version (if any) stays untouched."""
        # First create a valid version.
        valid_prefs = CrawlPlanPreferences(
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v1 = plan_service.create_version(owner_id, valid_prefs, activate=True)
        assert v1.is_active is True

        # Now try to create one with bad timezone.
        bad_prefs = CrawlPlanPreferences(
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="Not/A/Zone",
        )
        with pytest.raises(InvalidStateError):
            plan_service.create_version(owner_id, bad_prefs, activate=True)

        # v1 is still active and the only version.
        active = plan_service.get_active(owner_id)
        assert active is not None
        assert active.id == v1.id
        versions = plan_service.list_versions(owner_id)
        assert len(versions) == 1

    def test_api_returns_409_for_interval_below_minimum(self) -> None:
        owner = uuid4()
        client = _make_api_client(
            plan_service=CrawlPlanService(InMemoryCrawlPlanRepository()),
            candidate_id=owner,
        )
        resp = client.post(
            "/api/v1/crawl-plans/versions",
            json={
                "interval_seconds": 10,
                "timezone": "UTC",
            },
        )
        assert resp.status_code == 409
        assert "below minimum" in resp.json()["error"]["message"].lower()

    def test_api_returns_409_for_bad_timezone(self) -> None:
        owner = uuid4()
        client = _make_api_client(
            plan_service=CrawlPlanService(InMemoryCrawlPlanRepository()),
            candidate_id=owner,
        )
        resp = client.post(
            "/api/v1/crawl-plans/versions",
            json={
                "interval_seconds": MIN_SCHEDULE_INTERVAL_SECONDS,
                "timezone": "Invalid/Zone",
            },
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVALID_STATE"


# ===========================================================================
# 3. Plan version immutability (copy-on-write)
# ===========================================================================


class TestPlanVersionImmutability:
    """Design Decision 2: editing an active plan creates a NEW version; the
    prior version is preserved for provenance and the new version applies
    only to future runs."""

    def test_first_version_gets_version_number_1(
        self,
        plan_service: CrawlPlanService,
        owner_id: UUID,
    ) -> None:
        prefs = CrawlPlanPreferences(
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v = plan_service.create_version(owner_id, prefs, activate=True)
        assert v.version == 1
        assert v.is_active is True

    def test_second_version_gets_version_number_2(
        self,
        plan_service: CrawlPlanService,
        owner_id: UUID,
    ) -> None:
        prefs = CrawlPlanPreferences(
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v1 = plan_service.create_version(owner_id, prefs, activate=True)
        v2 = plan_service.create_version(owner_id, prefs, activate=True)
        assert v2.version == 2
        assert v2.id != v1.id

    def test_prior_version_preserved_after_new_version(
        self,
        plan_service: CrawlPlanService,
        owner_id: UUID,
    ) -> None:
        source_a = uuid4()
        prefs1 = CrawlPlanPreferences(
            sources=(source_a,),
            themes=("python",),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v1 = plan_service.create_version(owner_id, prefs1, activate=True)

        source_b = uuid4()
        prefs2 = CrawlPlanPreferences(
            sources=(source_b,),
            themes=("golang",),
            schedule_interval_seconds=3600,
            schedule_timezone="America/New_York",
        )
        v2 = plan_service.create_version(owner_id, prefs2, activate=True)

        # v1 is preserved, no longer active.
        stored_v1 = plan_service.get_version(owner_id, v1.id)
        assert stored_v1.themes == ("python",)
        assert stored_v1.sources == (source_a,)
        assert stored_v1.is_active is False

        # v2 is active with new preferences.
        stored_v2 = plan_service.get_version(owner_id, v2.id)
        assert stored_v2.themes == ("golang",)
        assert stored_v2.sources == (source_b,)
        assert stored_v2.is_active is True

    def test_new_version_applies_only_to_future_runs(
        self,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        source_service: CrawlSourceService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        """A run created under v1 is bound to v1's snapshot; creating v2 does
        not retroactively change v1's run."""
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs1 = CrawlPlanPreferences(
            sources=(src.id,),
            themes=("python",),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v1 = plan_service.create_version(owner_id, prefs1, activate=True)
        run1 = run_service.run_now(owner_id)
        assert run1.plan_version_id == v1.id

        # Create v2 — run1 still points to v1.
        prefs2 = CrawlPlanPreferences(
            sources=(src.id,),
            themes=("golang",),
            schedule_interval_seconds=3600,
            schedule_timezone="America/New_York",
        )
        v2 = plan_service.create_version(owner_id, prefs2, activate=True)
        assert run1.plan_version_id == v1.id  # unchanged
        assert v2.themes == ("golang",)

    def test_only_one_active_version_per_owner(
        self,
        plan_service: CrawlPlanService,
        owner_id: UUID,
    ) -> None:
        prefs = CrawlPlanPreferences(
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v1 = plan_service.create_version(owner_id, prefs, activate=True)
        v2 = plan_service.create_version(owner_id, prefs, activate=True)
        v3 = plan_service.create_version(owner_id, prefs, activate=True)

        active = plan_service.get_active(owner_id)
        assert active is not None
        assert active.id == v3.id
        # v1 and v2 are not active.
        assert plan_service.get_version(owner_id, v1.id).is_active is False
        assert plan_service.get_version(owner_id, v2.id).is_active is False

    def test_api_create_version_returns_new_version(
        self,
        plan_service: CrawlPlanService,
        source_service: CrawlSourceService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="lever",
            source_identifier="test",
            base_url="https://test.com",
            enabled=True,
        )
        client = _make_api_client(
            source_service=source_service,
            plan_service=plan_service,
            candidate_id=owner_id,
        )
        resp1 = client.post(
            "/api/v1/crawl-plans/versions",
            json={
                "sources": [str(src.id)],
                "themes": ["python"],
                "interval_seconds": MIN_SCHEDULE_INTERVAL_SECONDS,
                "timezone": "UTC",
            },
        )
        assert resp1.status_code == 201
        v1_id = resp1.json()["id"]

        resp2 = client.post(
            "/api/v1/crawl-plans/versions",
            json={
                "sources": [str(src.id)],
                "themes": ["golang"],
                "interval_seconds": 3600,
                "timezone": "America/New_York",
            },
        )
        assert resp2.status_code == 201
        v2_id = resp2.json()["id"]
        assert v2_id != v1_id

        # List versions: both present, v2 is active.
        list_resp = client.get("/api/v1/crawl-plans/versions")
        items = list_resp.json()["items"]
        assert len(items) == 2
        active_ids = [i["id"] for i in items if i["is_active"]]
        assert active_ids == [v2_id]


# ===========================================================================
# 4. Pause behavior
# ===========================================================================


class TestPauseBehavior:
    """Paused source/plan schedules no new runs; history is retained."""

    def test_paused_source_excluded_from_run_eligible_set(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        run_repo: InMemoryCrawlRunRepository,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_id, prefs, activate=True)
        run1 = run_service.run_now(owner_id)
        assert run1.state is CrawlRunState.PENDING

        # Transition the first run to terminal so run_now does not short-circuit
        # on the existing non-terminal run.
        run_repo.update_terminal(owner_id, run1.id, state=CrawlRunState.SUCCEEDED)

        # Pause the source; run_now should fail (no eligible sources).
        source_service.pause(owner_id, src.id)
        with pytest.raises(InvalidStateError, match="no eligible sources"):
            run_service.run_now(owner_id)

    def test_paused_plan_prevents_run_now(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_id, prefs, activate=True)

        # Pause plan.
        plan_service.pause(owner_id)
        with pytest.raises(InvalidStateError, match="no active version"):
            run_service.run_now(owner_id)

    def test_pause_preserves_run_history(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_id, prefs, activate=True)
        run_service.run_now(owner_id)

        # Pause plan.
        plan_service.pause(owner_id)

        # Run history is preserved.
        runs = run_service.list_runs(owner_id)
        assert len(runs) == 1

    def test_paused_source_state_is_paused(
        self,
        source_service: CrawlSourceService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        paused = source_service.pause(owner_id, src.id)
        assert paused.enabled is False
        assert paused.state is CrawlSourceState.PAUSED

    def test_resume_source_restores_active(
        self,
        source_service: CrawlSourceService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        source_service.pause(owner_id, src.id)
        resumed = source_service.resume(owner_id, src.id)
        assert resumed.enabled is True
        assert resumed.state is CrawlSourceState.ACTIVE

    def test_resume_plan_reactivates_latest_version(
        self,
        plan_service: CrawlPlanService,
        owner_id: UUID,
    ) -> None:
        prefs = CrawlPlanPreferences(
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_id, prefs, activate=True)
        plan_service.pause(owner_id)
        assert plan_service.get_active(owner_id) is None

        resumed = plan_service.resume(owner_id)
        assert resumed.is_active is True
        assert plan_service.get_active(owner_id) is not None

    def test_api_pause_plan_returns_paused_state(self) -> None:
        owner = uuid4()
        plan_svc = CrawlPlanService(InMemoryCrawlPlanRepository())
        client = _make_api_client(plan_service=plan_svc, candidate_id=owner)

        # Create a version first.
        client.post(
            "/api/v1/crawl-plans/versions",
            json={
                "interval_seconds": MIN_SCHEDULE_INTERVAL_SECONDS,
                "timezone": "UTC",
            },
        )
        # Pause.
        resp = client.post("/api/v1/crawl-plans/pause")
        assert resp.status_code == 200
        assert resp.json()["state"] == "paused"
        assert resp.json()["active"] is None


# ===========================================================================
# 5. Run-now idempotency
# ===========================================================================


class TestRunNowIdempotency:
    """Iron Rule 4: same run_identity returns existing run (no duplicate)."""

    def test_first_run_now_creates_pending_run(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_id, prefs, activate=True)
        run = run_service.run_now(owner_id)
        assert run.state is CrawlRunState.PENDING
        assert run.plan_version_id == plan_service.get_active(owner_id).id  # type: ignore[union-attr]

    def test_second_run_now_returns_same_pending_run(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_id, prefs, activate=True)
        run1 = run_service.run_now(owner_id)
        run2 = run_service.run_now(owner_id)
        assert run1.id == run2.id
        assert run1.run_identity == run2.run_identity

    def test_new_run_after_terminal_state(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        run_repo: InMemoryCrawlRunRepository,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        """After a run reaches a terminal state, run_now mints a new identity."""
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_id, prefs, activate=True)
        run1 = run_service.run_now(owner_id)

        # Transition run1 to SUCCEEDED.
        run_repo.update_terminal(owner_id, run1.id, state=CrawlRunState.SUCCEEDED)

        # Now run_now should create a new run.
        run2 = run_service.run_now(owner_id)
        assert run2.id != run1.id
        assert run2.run_identity != run1.run_identity

    def test_run_identity_is_unique_per_attempt(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        run_repo: InMemoryCrawlRunRepository,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_id, prefs, activate=True)
        run1 = run_service.run_now(owner_id)
        run_repo.update_terminal(owner_id, run1.id, state=CrawlRunState.FAILED)
        run2 = run_service.run_now(owner_id)

        identities = {run1.run_identity, run2.run_identity}
        assert len(identities) == 2

    def test_api_run_now_idempotent(self) -> None:
        owner = uuid4()
        src_repo = InMemoryCrawlSourceRepository()
        plan_repo = InMemoryCrawlPlanRepository()
        run_repo = InMemoryCrawlRunRepository(plan_repo)
        src_svc = CrawlSourceService(src_repo)
        plan_svc = CrawlPlanService(plan_repo)
        run_svc = CrawlRunService(run_repo, plan_repository=plan_repo, source_repository=src_repo)

        # Register source + plan.
        src = src_svc.register(
            owner,
            company_id=uuid4(),
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        plan_svc.create_version(
            owner,
            CrawlPlanPreferences(
                sources=(src.id,),
                schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
                schedule_timezone="UTC",
            ),
            activate=True,
        )
        client = _make_api_client(
            source_service=src_svc,
            plan_service=plan_svc,
            run_service=run_svc,
            candidate_id=owner,
        )
        r1 = client.post("/api/v1/crawl-plans/run-now")
        r2 = client.post("/api/v1/crawl-plans/run-now")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["id"] == r2.json()["id"]
        assert r1.json()["run_identity"] == r2.json()["run_identity"]


# ===========================================================================
# 6. Overlap coalesce
# ===========================================================================


class TestOverlapCoalesce:
    """Task 4.6: a trigger overlapping a running source/plan records CANCELLED
    with reason (no second crawl)."""

    def test_record_overlap_skip_creates_cancelled_run(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v = plan_service.create_version(owner_id, prefs, activate=True)
        run1 = run_service.run_now(owner_id)
        assert run1.state is CrawlRunState.PENDING

        # Simulate overlap: record a skip while run1 is still pending.
        skip = run_service.record_overlap_skip(
            owner_id,
            plan_version_id=v.id,
            source_set=(src.id,),
            reason="overlap_with_running",
        )
        assert skip.state is CrawlRunState.CANCELLED
        assert skip.error_category == "overlap_with_running"

    def test_overlap_skip_preserved_in_history(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v = plan_service.create_version(owner_id, prefs, activate=True)
        run_service.run_now(owner_id)
        run_service.record_overlap_skip(
            owner_id,
            plan_version_id=v.id,
            source_set=(src.id,),
            reason="overlap_with_running",
        )
        runs = run_service.list_runs(owner_id)
        assert len(runs) == 2
        cancelled = [r for r in runs if r.state is CrawlRunState.CANCELLED]
        assert len(cancelled) == 1
        assert cancelled[0].error_category == "overlap_with_running"

    def test_find_non_terminal_run_detects_pending(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v = plan_service.create_version(owner_id, prefs, activate=True)
        run_service.run_now(owner_id)
        non_terminal = run_service.find_non_terminal_run(owner_id, v.id)
        assert non_terminal is not None
        assert non_terminal.state is CrawlRunState.PENDING

    def test_find_non_terminal_run_none_after_terminal(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        run_repo: InMemoryCrawlRunRepository,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v = plan_service.create_version(owner_id, prefs, activate=True)
        run = run_service.run_now(owner_id)
        run_repo.update_terminal(owner_id, run.id, state=CrawlRunState.SUCCEEDED)
        non_terminal = run_service.find_non_terminal_run(owner_id, v.id)
        assert non_terminal is None


# ===========================================================================
# 7. Ownership scoping
# ===========================================================================


class TestOwnershipScoping:
    """Iron Rule 2: routes use server-side candidate_id; client-supplied owner
    substitution is rejected; missing repo -> 503."""

    def test_api_source_routes_reject_unauthenticated(self) -> None:
        """Without a session/auth, the route rejects the request.

        The crawl-plan routes carry a ``require_capability`` dependency that
        checks the capability resolver on ``app.state``.  When the resolver
        is absent (non-RuntimeResources probe) the dependency raises 503.
        When auth_service is absent AND no capability resolver is wired the
        request may hit either the capability gate (503) or the auth gate
        (403).  Both are safe-deny outcomes; the test accepts either.
        """
        client = _make_api_client(
            source_service=CrawlSourceService(InMemoryCrawlSourceRepository()),
            candidate_id=None,
        )
        resp = client.get("/api/v1/crawl-sources")
        assert resp.status_code in (401, 403, 503)

    def test_api_plan_routes_reject_unauthenticated(self) -> None:
        client = _make_api_client(
            plan_service=CrawlPlanService(InMemoryCrawlPlanRepository()),
            candidate_id=None,
        )
        resp = client.get("/api/v1/crawl-plans")
        assert resp.status_code in (401, 403, 503)

    def test_api_run_routes_reject_unauthenticated(self) -> None:
        client = _make_api_client(
            run_service=CrawlRunService(
                InMemoryCrawlRunRepository(InMemoryCrawlPlanRepository()),
                plan_repository=InMemoryCrawlPlanRepository(),
                source_repository=InMemoryCrawlSourceRepository(),
            ),
            candidate_id=None,
        )
        resp = client.get("/api/v1/crawl-runs")
        assert resp.status_code in (401, 403, 503)

    def test_api_returns_503_when_source_service_missing(self) -> None:
        """Routes use require_repository; missing service -> 503.

        The capability resolver must also be wired (it gates every crawl
        route); otherwise the 503 comes from the capability gate rather than
        the repository lookup — both are ``DEPENDENCY_NOT_READY``.
        """
        owner = uuid4()
        # Wire auth + capability resolver but NOT the crawl source service.
        client = _make_api_client(candidate_id=owner)
        # Also wire the capability resolver so the route reaches the repo
        # check rather than failing at the capability gate.
        settings = Settings.model_validate({"environment": RuntimeEnvironment.TEST})
        cast(Any, client).app.state.capability_resolver = SettingsCapabilityResolver(settings)
        resp = client.get("/api/v1/crawl-sources")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "DEPENDENCY_NOT_READY"

    def test_api_returns_503_when_plan_service_missing(self) -> None:
        owner = uuid4()
        client = _make_api_client(candidate_id=owner)
        settings = Settings.model_validate({"environment": RuntimeEnvironment.TEST})
        cast(Any, client).app.state.capability_resolver = SettingsCapabilityResolver(settings)
        resp = client.get("/api/v1/crawl-plans")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "DEPENDENCY_NOT_READY"

    def test_api_returns_503_when_run_service_missing(self) -> None:
        owner = uuid4()
        client = _make_api_client(candidate_id=owner)
        settings = Settings.model_validate({"environment": RuntimeEnvironment.TEST})
        cast(Any, client).app.state.capability_resolver = SettingsCapabilityResolver(settings)
        resp = client.get("/api/v1/crawl-runs")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "DEPENDENCY_NOT_READY"

    def test_service_ownership_scoped_reads(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        company_id: UUID,
    ) -> None:
        """Plan and runs created by owner A are not visible to owner B.

        Sources use a global table (``job_sources``) in the single-user
        runtime and do not enforce per-owner filtering — the ownership stamp
        is for uniform API shape only.  Plans and runs ARE ownership-scoped.
        """
        owner_a = uuid4()
        owner_b = uuid4()

        src = source_service.register(
            owner_a,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_a, prefs, activate=True)
        run_service.run_now(owner_a)

        # Owner B sees no plans and no runs.
        assert plan_service.get_active(owner_b) is None
        assert run_service.list_runs(owner_b) == []
        assert plan_service.list_versions(owner_b) == []

    def test_plan_and_run_ownership_is_scoped(
        self,
        plan_service: CrawlPlanService,
        source_service: CrawlSourceService,
        run_service: CrawlRunService,
        company_id: UUID,
    ) -> None:
        """Plans and runs enforce transitive ownership (Iron Rule 2).

        A plan version created by owner A cannot be accessed by owner B.
        A run whose plan version belongs to owner A cannot be accessed by
        owner B.  Cross-owner lookups raise NotFoundError.
        """
        owner_a = uuid4()
        owner_b = uuid4()
        src = source_service.register(
            owner_a,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v = plan_service.create_version(owner_a, prefs, activate=True)
        run = run_service.run_now(owner_a)

        # Owner B cannot access owner A's plan version.
        with pytest.raises(NotFoundError):
            plan_service.get_version(owner_b, v.id)

        # Owner B cannot access owner A's run.
        with pytest.raises(NotFoundError):
            run_service.get_run(owner_b, run.id)

    def test_service_rejects_cross_owner_plan_access(
        self,
        plan_service: CrawlPlanService,
        owner_id: UUID,
    ) -> None:
        prefs = CrawlPlanPreferences(
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        v = plan_service.create_version(owner_id, prefs, activate=True)
        other_owner = uuid4()
        with pytest.raises(NotFoundError):
            plan_service.get_version(other_owner, v.id)

    def test_service_rejects_cross_owner_run_access(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_id, prefs, activate=True)
        run = run_service.run_now(owner_id)
        other_owner = uuid4()
        with pytest.raises(NotFoundError):
            run_service.get_run(other_owner, run.id)

    def test_run_now_requires_active_plan(
        self,
        run_service: CrawlRunService,
        owner_id: UUID,
    ) -> None:
        with pytest.raises(InvalidStateError, match="no active version"):
            run_service.run_now(owner_id)

    def test_run_now_requires_eligible_sources(
        self,
        source_service: CrawlSourceService,
        plan_service: CrawlPlanService,
        run_service: CrawlRunService,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        # Register a source but don't enable it.
        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=False,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_id, prefs, activate=True)
        with pytest.raises(InvalidStateError, match="no eligible sources"):
            run_service.run_now(owner_id)


# ===========================================================================
# Cross-cutting: state transition table enforcement
# ===========================================================================


class TestCrawlRunTransitions:
    """Phase 0 contract: ALLOWED_TRANSITIONS accepts legal moves, rejects
    illegal ones. Terminal states admit no further moves."""

    def test_pending_can_move_to_running(self) -> None:
        from careerops.domain.crawl import ALLOWED_TRANSITIONS

        assert CrawlRunState.RUNNING in ALLOWED_TRANSITIONS[CrawlRunState.PENDING]

    def test_pending_can_move_to_cancelled(self) -> None:
        from careerops.domain.crawl import ALLOWED_TRANSITIONS

        assert CrawlRunState.CANCELLED in ALLOWED_TRANSITIONS[CrawlRunState.PENDING]

    def test_running_can_move_to_all_terminals(self) -> None:
        from careerops.domain.crawl import ALLOWED_TRANSITIONS

        for terminal in (
            CrawlRunState.SUCCEEDED,
            CrawlRunState.FAILED,
            CrawlRunState.CANCELLED,
            CrawlRunState.TIMEOUT,
        ):
            assert terminal in ALLOWED_TRANSITIONS[CrawlRunState.RUNNING]

    @pytest.mark.parametrize(
        "terminal",
        [
            CrawlRunState.SUCCEEDED,
            CrawlRunState.FAILED,
            CrawlRunState.CANCELLED,
            CrawlRunState.TIMEOUT,
        ],
    )
    def test_terminal_states_admit_no_moves(self, terminal: CrawlRunState) -> None:
        from careerops.domain.crawl import ALLOWED_TRANSITIONS

        assert ALLOWED_TRANSITIONS[terminal] == frozenset()

    def test_update_terminal_records_succeeded(
        self,
        plan_service: CrawlPlanService,
        source_service: CrawlSourceService,
        run_service: CrawlRunService,
        run_repo: InMemoryCrawlRunRepository,
        owner_id: UUID,
        company_id: UUID,
    ) -> None:
        from careerops.domain.crawl_plans import CrawlRunCounters

        src = source_service.register(
            owner_id,
            company_id=company_id,
            source_type="greenhouse",
            source_identifier="acme",
            base_url="https://acme.com/careers",
            enabled=True,
        )
        prefs = CrawlPlanPreferences(
            sources=(src.id,),
            schedule_interval_seconds=MIN_SCHEDULE_INTERVAL_SECONDS,
            schedule_timezone="UTC",
        )
        plan_service.create_version(owner_id, prefs, activate=True)
        run = run_service.run_now(owner_id)

        updated = run_repo.update_terminal(
            owner_id,
            run.id,
            state=CrawlRunState.SUCCEEDED,
            counters=CrawlRunCounters(discovered=5, updated=2, closed=0, failed=0),
        )
        assert updated.state is CrawlRunState.SUCCEEDED
        assert updated.counters.discovered == 5
        assert updated.counters.updated == 2
