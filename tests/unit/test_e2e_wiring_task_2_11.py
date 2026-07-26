"""Wiring tests for end-to-end-career-application-loop task 2.11.

Asserts the contract the runtime/app wiring promises to Section-3+ routes:

1. ``RuntimeResources`` constructs the new Section-2 repos
   (profile/evidence/application_cycle) from the DB engine and exposes a
   shared ``capability_resolver`` — NO in-memory fallback.
2. ``require_candidate_id`` resolves the candidate SERVER-SIDE from the
   authenticated principal and rejects absent/None candidate links with
   ``CandidateProfileRequiredError`` (403); ``reject_candidate_substitution``
   blocks client-supplied substitution attempts.
3. ``require_capability`` routes external-effect projections through the
   Phase-0 resolver with Iron-Rule-3 precedence: dependency-not-ready (503)
   wins over capability denial (403), and a missing resolver is 503 (never a
   silent allow or a silent deny).
4. ``require_repository`` surfaces a missing repo as ``DependencyNotReadyError``
   (503) rather than letting the route degrade silently.
5. ``create_app`` exposes the new repos + resolver on ``app.state`` when the
   probe is ``RuntimeResources``, and the existing review/auth surface is not
   disturbed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

import careerops.infrastructure.runtime as runtime
from careerops.api.app import create_app
from careerops.api.auth_dependency import (
    reject_candidate_substitution,
    require_candidate_id,
)
from careerops.api.capability_dependency import (
    require_capability,
    require_repository,
)
from careerops.api.errors import (
    CandidateProfileRequiredError,
    DeniedPolicyError,
    DependencyNotReadyError,
)
from careerops.auth.contracts import AuthenticatedPrincipal
from careerops.config import RuntimeEnvironment, Settings
from careerops.infrastructure.database.postgres_application_cycle_repo import (
    PostgresApplicationCycleRepository,
)
from careerops.infrastructure.database.postgres_evidence_repo import (
    PostgresEvidenceRepository,
)
from careerops.infrastructure.database.postgres_profile_repo import (
    PostgresProfileRepository,
)
from careerops.infrastructure.runtime import RuntimeResources
from careerops.orchestration.capability_resolver import (
    CapabilityKind,
    SettingsCapabilityResolver,
)

CANDIDATE_A = UUID("11111111-1111-1111-1111-111111111111")
CANDIDATE_B = UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


def _principal(*, candidate_id: UUID | None = CANDIDATE_A) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=uuid4(),
        username="owner",
        session_id=uuid4(),
        csrf_token_hash="hash",
        absolute_expires_at=__import__("datetime").datetime.now(
            tz=__import__("datetime").timezone.utc
        ),
        candidate_id=candidate_id,
    )


def _request_with_state(state: SimpleNamespace) -> Any:
    """Build a request-like object whose ``app.state`` is the given namespace.

    Returned as ``Any`` so the dependency functions (typed to accept a
    Starlette ``Request``) accept it without per-call site casts; the
    dependencies only touch ``request.app.state``.
    """
    return cast("Any", SimpleNamespace(app=SimpleNamespace(state=state)))


# ---------------------------------------------------------------------------
# 1. RuntimeResources constructs the new repos; no in-memory fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_resources_constructs_section2_repos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RuntimeResources builds the Section-2 repos from the engine and shares
    one capability resolver with the review stack."""

    class FakeEngine:
        def dispose(self) -> None:
            return None

    class FakeAsyncRedis:
        async def aclose(self, close_connection_pool: bool | None = None) -> None:
            return None

    class FakeSyncRedis:
        def close(self) -> None:
            return None

    fake_engine = FakeEngine()
    monkeypatch.setattr(runtime, "create_database_engine", lambda _settings, **_kw: fake_engine)
    monkeypatch.setattr(runtime.AsyncRedis, "from_url", lambda *_a, **_k: FakeAsyncRedis())
    monkeypatch.setattr(runtime.SyncRedis, "from_url", lambda *_a, **_k: FakeSyncRedis())

    resources = RuntimeResources(
        Settings.model_validate(
            {
                "environment": RuntimeEnvironment.TEST,
                "storage_root": tmp_path / "objects",
                "readiness_timeout_seconds": 0.1,
            }
        )
    )

    # The new repos are Postgres-backed and built from the same engine.
    assert isinstance(resources.profile_repo, PostgresProfileRepository)
    assert isinstance(resources.evidence_repo, PostgresEvidenceRepository)
    assert isinstance(resources.application_cycle_repo, PostgresApplicationCycleRepository)
    # Shared capability resolver (Phase 0) — one source of truth.
    assert isinstance(resources.capability_resolver, SettingsCapabilityResolver)
    # The resolver stays fail-closed under default settings.
    assert resources.capability_resolver.decide(CapabilityKind.AUTO_SEND).released is False
    assert (
        resources.capability_resolver.decide(CapabilityKind.SYSTEM_MANAGED_SEND).released is False
    )

    await resources.close()


# ---------------------------------------------------------------------------
# 2. Server-side candidate resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_candidate_id_returns_server_resolved_id() -> None:
    """The dependency returns the candidate linked to the session, ignoring
    anything a client might try to supply alongside it."""

    resolved = await require_candidate_id(principal=_principal(candidate_id=CANDIDATE_A))
    assert resolved == CANDIDATE_A


@pytest.mark.asyncio
async def test_require_candidate_id_rejects_missing_principal() -> None:
    with pytest.raises(CandidateProfileRequiredError):
        await require_candidate_id(principal=None)


@pytest.mark.asyncio
async def test_require_candidate_id_rejects_unlinked_user() -> None:
    """A session whose user has no candidate link is 403, not a silent None."""

    with pytest.raises(CandidateProfileRequiredError):
        await require_candidate_id(principal=_principal(candidate_id=None))


def test_reject_candidate_substitution_allows_absent_client_value() -> None:
    # No client value supplied -> not a substitution attempt.
    reject_candidate_substitution(provided=None, resolved=CANDIDATE_A)
    reject_candidate_substitution(provided="", resolved=CANDIDATE_A)


def test_reject_candidate_substitution_allows_matching_value() -> None:
    reject_candidate_substitution(provided=str(CANDIDATE_A), resolved=CANDIDATE_A)
    reject_candidate_substitution(provided=CANDIDATE_A, resolved=CANDIDATE_A)


def test_reject_candidate_substitution_blocks_mismatch() -> None:
    with pytest.raises(CandidateProfileRequiredError):
        reject_candidate_substitution(provided=CANDIDATE_B, resolved=CANDIDATE_A)


def test_reject_candidate_substitution_blocks_invalid_uuid() -> None:
    with pytest.raises(CandidateProfileRequiredError):
        reject_candidate_substitution(provided="not-a-uuid", resolved=CANDIDATE_A)


# ---------------------------------------------------------------------------
# 3. Capability gate (Iron Rule 3: 503 beats 403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_capability_denies_send_by_default() -> None:
    """system-managed-send stays denied under default flags (Iron Rule 5)."""

    state = SimpleNamespace(capability_resolver=SettingsCapabilityResolver(_default_settings()))
    dep = require_capability(CapabilityKind.SYSTEM_MANAGED_SEND)
    with pytest.raises(DeniedPolicyError):
        await dep(_request_with_state(state))


@pytest.mark.asyncio
async def test_require_capability_releases_crawl_read_by_default() -> None:
    """profile/crawl-plan read paths stay usable by default (Iron Rule 5)."""

    state = SimpleNamespace(capability_resolver=SettingsCapabilityResolver(_default_settings()))
    dep = require_capability(CapabilityKind.CRAWL_PLAN_MANAGEMENT)
    decision = await dep(_request_with_state(state))
    assert decision.released is True


@pytest.mark.asyncio
async def test_require_capability_dependency_not_ready_beats_denied() -> None:
    """A denied capability whose backing dependency is also down surfaces as
    503, never as a silent 403 that hides the missing dependency (Iron Rule 3)."""

    state = SimpleNamespace(capability_resolver=SettingsCapabilityResolver(_default_settings()))
    dep = require_capability(CapabilityKind.SYSTEM_MANAGED_SEND, dependency_available=False)
    with pytest.raises(DependencyNotReadyError):
        await dep(_request_with_state(state))


@pytest.mark.asyncio
async def test_require_capability_dependency_not_ready_beats_released() -> None:
    """A released capability whose dependency is down also surfaces as 503."""

    state = SimpleNamespace(capability_resolver=SettingsCapabilityResolver(_default_settings()))
    dep = require_capability(CapabilityKind.CRAWL_PLAN_MANAGEMENT, dependency_available=False)
    with pytest.raises(DependencyNotReadyError):
        await dep(_request_with_state(state))


@pytest.mark.asyncio
async def test_require_capability_503_when_resolver_unwired() -> None:
    """No resolver on app.state -> 503 (wiring bug), not a guessed allow/deny."""

    state = SimpleNamespace()  # no capability_resolver attribute
    dep = require_capability(CapabilityKind.SYSTEM_MANAGED_SEND)
    with pytest.raises(DependencyNotReadyError):
        await dep(_request_with_state(state))


# ---------------------------------------------------------------------------
# 4. require_repository: missing repo -> 503, never silent
# ---------------------------------------------------------------------------


def test_require_repository_returns_repo_when_wired() -> None:
    repo = object()
    state = SimpleNamespace(profile_repository=repo)
    assert require_repository(_request_with_state(state), "profile_repository") is repo


def test_require_repository_503_when_absent() -> None:
    """A route that reaches for an unwired repo fails loudly instead of
    silently returning empty results (Iron Rule 3)."""

    state = SimpleNamespace()
    with pytest.raises(DependencyNotReadyError):
        require_repository(_request_with_state(state), "profile_repository")


# ---------------------------------------------------------------------------
# 5. create_app exposes the new repos on app.state
# ---------------------------------------------------------------------------


def test_create_app_exposes_section2_repos_and_resolver() -> None:
    """When RuntimeResources is the probe, the new repos + shared resolver
    land on app.state for Section-3+ routes to consume."""

    # create_app() builds a real RuntimeResources; the repos are constructed
    # from the engine (lazy — no connection at construction). We only assert
    # the wiring surface, so we don't need a live DB.
    app = create_app(console_auth_service=MagicMock())
    assert hasattr(app.state, "profile_repository")
    assert hasattr(app.state, "evidence_repository")
    assert hasattr(app.state, "application_cycle_repository")
    assert hasattr(app.state, "capability_resolver")
    assert isinstance(app.state.capability_resolver, SettingsCapabilityResolver)
    # Existing review/demo wiring is preserved.
    assert app.state.application_repository is not None
    assert app.state.application_service is not None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _default_settings() -> Settings:
    return Settings.model_validate({"environment": RuntimeEnvironment.TEST})
