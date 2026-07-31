"""Phase-0 contract freeze tests (OpenSpec change
``end-to-end-career-application-loop`` task 1.6).

These tests pin the capability contract after the single-user autonomous-loop
teardown (real-autonomous-career-loop): the external-effect flags
(google_oauth / external_writes / auto_send) are now honored at startup and
default OFF; the capability resolver denies GMAIL_READ / SYSTEM_MANAGED_SEND /
AUTO_SEND when their flag is off and releases them when it is on; it still
fails-closed on unknown capabilities. Business error codes map to their
intended HTTP statuses; application / crawl-run transition tables accept legal
moves and reject illegal ones. Preview, manual tracking, and deterministic
matching remain usable because crawl-plan management stays released by default.

These are pure contract tests — no database, no provider, no FastAPI app
instance. They import only the typed enum / dataclass / exception contracts.
"""

from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace
from typing import cast

import pytest

from careerops.api.contracts import ErrorCode
from careerops.api.errors import (
    DeniedPolicyError,
    InvalidStateError,
    ReconciliationRequiredError,
    StalePayloadError,
    UnavailableDependencyError,
    UnresolvedEmailLinkError,
)
from careerops.config import Settings
from careerops.domain.applications import (
    ALLOWED_TRANSITIONS as APPLICATION_TRANSITIONS,
)
from careerops.domain.applications import (
    ApplicationState,
)
from careerops.domain.applications import (
    is_transition_legal as is_application_transition_legal,
)
from careerops.domain.crawl import ALLOWED_TRANSITIONS as CRAWL_RUN_TRANSITIONS
from careerops.domain.crawl import CrawlRunState
from careerops.orchestration.capability_resolver import (
    CapabilityDecision,
    CapabilityKind,
    SettingsCapabilityResolver,
)

# ---------------------------------------------------------------------------
# Settings fixture — hermetic against CAREEROPS_* env-var leakage
# ---------------------------------------------------------------------------


@pytest.fixture
def default_settings() -> Settings:
    """Default-deny ``Settings`` constructed explicitly so a stray
    ``CAREEROPS_*`` environment variable in the test process cannot flip a
    capability flag and silently widen the contract."""
    return Settings.model_validate(
        {
            "google_oauth_enabled": False,
            "external_writes_enabled": False,
            "auto_send_enabled": False,
            "crawl_plan_management_enabled": True,
            "model_tailoring_enabled": False,
        }
    )


# ---------------------------------------------------------------------------
# Iron Rule 1 — prohibited external-effect flags stay default-OFF and the
# Settings validator rejects any attempt to enable them in Phase 0.
# ---------------------------------------------------------------------------


def test_default_settings_keep_prohibited_capabilities_off(
    default_settings: Settings,
) -> None:
    """The default configuration MUST keep Google OAuth, external writes, and
    auto-send disabled (design Decision 11; Iron Rule 1)."""
    assert default_settings.google_oauth_enabled is False
    assert default_settings.external_writes_enabled is False
    assert default_settings.auto_send_enabled is False


def test_default_settings_keep_preview_and_manual_paths_usable(
    default_settings: Settings,
) -> None:
    """Deterministic matching, preview, and manual tracking remain usable
    because the crawl-plan management gate stays released by default."""
    assert default_settings.crawl_plan_management_enabled is True
    assert default_settings.model_tailoring_enabled is False


@pytest.mark.parametrize(
    "flag",
    [
        "google_oauth_enabled",
        "external_writes_enabled",
        "auto_send_enabled",
    ],
)
def test_settings_validator_accepts_capability_flag_enabled(flag: str) -> None:
    """The M4/M5A/M7 startup rejection is torn down for the single-user
    autonomous loop: enabling any capability flag at construction is now
    accepted (the flag flows to the resolver / kernel). Defaults stay off
    (see test_default_settings_keep_prohibited_capabilities_off)."""
    base = {
        "google_oauth_enabled": False,
        "external_writes_enabled": False,
        "auto_send_enabled": False,
    }
    base[flag] = True
    settings = Settings.model_validate(base)
    assert getattr(settings, flag) is True


# ---------------------------------------------------------------------------
# Capability resolver — default-deny + fail-closed contracts
# ---------------------------------------------------------------------------


def test_resolver_denies_gmail_read_under_default_settings(
    default_settings: Settings,
) -> None:
    decision = SettingsCapabilityResolver(default_settings).decide(CapabilityKind.GMAIL_READ)
    assert isinstance(decision, CapabilityDecision)
    assert decision.released is False
    assert decision.reason  # non-empty, safe to surface


def test_resolver_denies_system_managed_send_under_default_settings(
    default_settings: Settings,
) -> None:
    decision = SettingsCapabilityResolver(default_settings).decide(
        CapabilityKind.SYSTEM_MANAGED_SEND
    )
    assert isinstance(decision, CapabilityDecision)
    assert decision.released is False
    assert decision.reason


def test_resolver_denies_auto_send_under_default_settings(
    default_settings: Settings,
) -> None:
    """Auto-send is denied when its operator flag is off (default). The
    permanent hard-deny was torn down for the single-user autonomous loop;
    AUTO_SEND now follows the flag like the other capabilities."""
    decision = SettingsCapabilityResolver(default_settings).decide(CapabilityKind.AUTO_SEND)
    assert isinstance(decision, CapabilityDecision)
    assert decision.released is False
    assert decision.reason


def test_resolver_releases_auto_send_when_flag_on(
    default_settings: Settings,
) -> None:
    """AUTO_SEND follows the operator flag: released when on, denied when off.
    The permanent hard-deny was torn down for the single-user autonomous loop
    (real-autonomous-career-loop); autonomy is bounded downstream by the
    autonomous-action policy, not by a permanent capability denial."""
    forced = default_settings.model_copy(update={"auto_send_enabled": True})
    assert forced.auto_send_enabled is True  # sanity: the flag really is on
    decision = SettingsCapabilityResolver(forced).decide(CapabilityKind.AUTO_SEND)
    assert isinstance(decision, CapabilityDecision)
    assert decision.released is True
    assert decision.reason


def test_resolver_releases_crawl_plan_management_by_default(
    default_settings: Settings,
) -> None:
    """Crawl-plan management stays released so preview / manual tracking /
    deterministic matching remain usable under the default config."""
    decision = SettingsCapabilityResolver(default_settings).decide(
        CapabilityKind.CRAWL_PLAN_MANAGEMENT
    )
    assert isinstance(decision, CapabilityDecision)
    assert decision.released is True
    assert decision.reason


def test_resolver_keeps_model_tailoring_disabled_and_review_only_by_default(
    default_settings: Settings,
) -> None:
    """Model tailoring is disabled by default (design Decision 11:
    ``disabled 或 review-only``). Iron Rule 4: model output never mutates
    policy facts, application state, or recipients."""
    assert default_settings.model_tailoring_enabled is False
    decision = SettingsCapabilityResolver(default_settings).decide(CapabilityKind.MODEL_TAILORING)
    assert isinstance(decision, CapabilityDecision)
    assert decision.released is False
    # The reason MUST state the review-only invariant even when disabled, so
    # operators cannot read the denial as "model is authoritative when on".
    assert "review-only" in decision.reason


def test_model_tailoring_decision_is_review_only_even_when_released() -> None:
    """Iron Rule 4: even when the tailoring flag is released, model output is
    review-only and never authoritative — the reason MUST say so.

    All prohibited external-effect flags are pinned False so a stray
    ``CAREEROPS_GOOGLE_OAUTH_ENABLED``-style env var cannot widen the test."""
    settings = Settings.model_validate(
        {
            "google_oauth_enabled": False,
            "external_writes_enabled": False,
            "auto_send_enabled": False,
            "model_tailoring_enabled": True,
        }
    )
    decision = SettingsCapabilityResolver(settings).decide(CapabilityKind.MODEL_TAILORING)
    assert decision.released is True
    assert "review-only" in decision.reason


def test_resolver_fails_closed_on_unknown_capability(
    default_settings: Settings,
) -> None:
    """A capability the resolver has not been explicitly taught MUST fall
    through to a denied decision (Iron Rule 1: fail-closed on unknown
    capabilities).

    Simulates a future ``CapabilityKind`` member added to the enum without
    updating the resolver dispatch. The resolver's identity-based dispatch
    must not match the foreign object, and the default branch must deny.
    """
    resolver = SettingsCapabilityResolver(default_settings)
    future_capability = cast(
        CapabilityKind,
        SimpleNamespace(value="future_unreleased_capability"),
    )
    decision = resolver.decide(future_capability)
    assert isinstance(decision, CapabilityDecision)
    assert decision.released is False
    assert "fail-closed" in decision.reason


def test_capability_kind_contract_is_the_phase0_set() -> None:
    """The capability enum ships exactly the explicitly released contract set;
    smart intake is separately default-disabled by its operator flag.
    Decision 11 table calls for. Additions are deliberate (Iron Rule 2:
    additive only) and require updating the resolver dispatch."""
    expected = {
        "crawl_plan_management",
        "model_tailoring",
        "smart_intake",
        "gmail_read",
        "system_managed_send",
        "auto_send",
    }
    assert {member.value for member in CapabilityKind} == expected


# ---------------------------------------------------------------------------
# Business error code -> HTTP status mapping (task 1.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls, expected_code, expected_status, expected_retryable",
    [
        (
            InvalidStateError,
            ErrorCode.INVALID_STATE,
            HTTPStatus.CONFLICT,
            False,
        ),
        (
            StalePayloadError,
            ErrorCode.STALE_PAYLOAD,
            HTTPStatus.CONFLICT,
            False,
        ),
        (
            UnavailableDependencyError,
            ErrorCode.UNAVAILABLE_DEPENDENCY,
            HTTPStatus.SERVICE_UNAVAILABLE,
            True,
        ),
        (
            DeniedPolicyError,
            ErrorCode.DENIED_POLICY,
            HTTPStatus.FORBIDDEN,
            False,
        ),
        (
            UnresolvedEmailLinkError,
            ErrorCode.UNRESOLVED_EMAIL_LINK,
            HTTPStatus.CONFLICT,
            False,
        ),
        (
            ReconciliationRequiredError,
            ErrorCode.RECONCILIATION_REQUIRED,
            HTTPStatus.CONFLICT,
            False,
        ),
    ],
)
def test_business_error_code_maps_to_intended_http_status(
    exc_cls: type,
    expected_code: ErrorCode,
    expected_status: HTTPStatus,
    expected_retryable: bool,
) -> None:
    """Each Phase-0 business error code MUST map to its intended HTTP status
    and retryability via the typed ``CareerOpsHTTPException`` helper. The
    generic HTTPException fallback mapping cannot distinguish these business
    codes, so callers must raise the typed subclass to get the precise code."""
    exc = exc_cls()
    assert exc.error_code == expected_code
    assert exc.status_code == expected_status.value
    assert exc.retryable is expected_retryable


# ---------------------------------------------------------------------------
# State transition tables (task 1.3) — legal moves accepted, illegal moves
# rejected, terminal states admit nothing, every state has an entry.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        (ApplicationState.FAVORITED, ApplicationState.PREPARING),
        (ApplicationState.PREPARING, ApplicationState.SUBMITTED),
        (ApplicationState.SUBMITTED, ApplicationState.INTERVIEWING),
        (ApplicationState.INTERVIEWING, ApplicationState.OFFER),
        (ApplicationState.ON_HOLD, ApplicationState.PREPARING),
        (ApplicationState.IGNORED, ApplicationState.FAVORITED),
    ],
)
def test_application_state_legal_transitions_accepted(
    from_state: ApplicationState, to_state: ApplicationState
) -> None:
    assert is_application_transition_legal(from_state, to_state) is True
    # The helper and the table agree.
    assert to_state in APPLICATION_TRANSITIONS[from_state]


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        # Skip the preparing step entirely.
        (ApplicationState.FAVORITED, ApplicationState.SUBMITTED),
        # Resurrect a terminal state.
        (ApplicationState.REJECTED, ApplicationState.PREPARING),
        (ApplicationState.WITHDRAWN, ApplicationState.PREPARING),
        # OFFER can only move to WITHDRAWN.
        (ApplicationState.OFFER, ApplicationState.INTERVIEWING),
        # Re-enter the favorite entrypoint from a post-favorite state.
        (ApplicationState.SUBMITTED, ApplicationState.FAVORITED),
    ],
)
def test_application_state_illegal_transitions_rejected(
    from_state: ApplicationState, to_state: ApplicationState
) -> None:
    assert is_application_transition_legal(from_state, to_state) is False
    assert to_state not in APPLICATION_TRANSITIONS[from_state]


def test_application_terminal_states_admit_no_transitions() -> None:
    """REJECTED and WITHDRAWN are terminal — no further state changes."""
    for terminal in (ApplicationState.REJECTED, ApplicationState.WITHDRAWN):
        assert APPLICATION_TRANSITIONS[terminal] == frozenset()


def test_application_transition_table_covers_every_state() -> None:
    """Every ``ApplicationState`` MUST have an entry so a future state added
    without updating the table cannot silently behave like a terminal state."""
    assert set(APPLICATION_TRANSITIONS) == set(ApplicationState)


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        (CrawlRunState.PENDING, CrawlRunState.RUNNING),
        (CrawlRunState.PENDING, CrawlRunState.CANCELLED),
        (CrawlRunState.RUNNING, CrawlRunState.SUCCEEDED),
        (CrawlRunState.RUNNING, CrawlRunState.FAILED),
        (CrawlRunState.RUNNING, CrawlRunState.CANCELLED),
        (CrawlRunState.RUNNING, CrawlRunState.TIMEOUT),
    ],
)
def test_crawl_run_legal_transitions_accepted(
    from_state: CrawlRunState, to_state: CrawlRunState
) -> None:
    assert to_state in CRAWL_RUN_TRANSITIONS[from_state]


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        # Cannot skip PENDING -> RUNNING.
        (CrawlRunState.PENDING, CrawlRunState.SUCCEEDED),
        # Terminal resurrection blocked across all four terminal states.
        (CrawlRunState.SUCCEEDED, CrawlRunState.RUNNING),
        (CrawlRunState.FAILED, CrawlRunState.RUNNING),
        (CrawlRunState.CANCELLED, CrawlRunState.RUNNING),
        (CrawlRunState.TIMEOUT, CrawlRunState.RUNNING),
    ],
)
def test_crawl_run_illegal_transitions_rejected(
    from_state: CrawlRunState, to_state: CrawlRunState
) -> None:
    assert to_state not in CRAWL_RUN_TRANSITIONS[from_state]


def test_crawl_run_terminal_states_admit_no_transitions() -> None:
    """SUCCEEDED / FAILED / CANCELLED / TIMEOUT are terminal — a finished run
    is never silently resurrected."""
    for terminal in (
        CrawlRunState.SUCCEEDED,
        CrawlRunState.FAILED,
        CrawlRunState.CANCELLED,
        CrawlRunState.TIMEOUT,
    ):
        assert CRAWL_RUN_TRANSITIONS[terminal] == frozenset()


def test_crawl_run_transition_table_covers_every_state() -> None:
    """Every ``CrawlRunState`` MUST have an entry so a future state added
    without updating the table cannot silently behave like a terminal state."""
    assert set(CRAWL_RUN_TRANSITIONS) == set(CrawlRunState)
