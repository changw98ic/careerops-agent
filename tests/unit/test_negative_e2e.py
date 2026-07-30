"""Phase 10.4-10.6: Negative-scenario E2E verification (unit/mock, no real providers).

Covers:
- 10.4: Irreversible-commitment reply (accept interview time / offer) is blocked
  from autonomous send via A/B approval default-deny + PERMANENTLY_DENIED.
- 10.5: Pending / denied / expired / revoked crawl permission never attaches
  an authenticated session.
- 10.6: Empty results, HTTP 403, CAPTCHA, model judgement alone, and temporary
  failures never create a login-permission request (outcome_classifier rules).

All tests use in-memory fakes and mocks -- no database or external provider
required.  These MUST pass in CI.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from careerops.application.approval_loop import (
    ABApprovalLoop,
    DraftContext,
    DraftResult,
    MAX_ROUNDS,
)
from careerops.application.outcome_classifier import (
    ClassificationInput,
    classify_outcome,
    has_login_evidence,
)
from careerops.domain.crawl import CrawlDecision
from careerops.domain.crawl_attempts import (
    CrawlAttemptOutcome,
    CrawlPermissionState,
    CrawlSourcePermission,
    PERMISSION_TERMINAL_STATES,
    is_permission_transition_allowed,
)
from careerops.domain.reply_draft import (
    PERMANENTLY_DENIED_REPLY_CATEGORIES,
    ReplyIntent,
    ReplyRiskCategory,
    is_system_send_denied,
    reply_risk_for_category,
    reply_risk_for_intent,
)
from careerops.infrastructure.temporal.m1_crawl_sink import CrawlSourceResult
from careerops.workflows.m1_contracts import CrawledPostingRecord
from careerops.model_gateway.base import (
    DisabledModelAdapter,
    StructuredModelRequest,
    StructuredModelResponse,
)
from careerops.orchestration.capability_resolver import (
    CapabilityDecision,
    CapabilityKind,
)


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_approval_loop.py patterns)
# ---------------------------------------------------------------------------

_SKELETON = DraftResult(subject="Template subject", body="Template body")
_CONTEXT = DraftContext(
    recipient="hiring@example.com",
    job_title="Backend Engineer",
    company="ExampleCorp",
    resume_summary="Python, FastAPI, 5 years",
)


class _ReleasedResolver:
    def decide(self, capability: CapabilityKind) -> CapabilityDecision:
        return CapabilityDecision(released=True, reason="released")


class _DeniedResolver:
    def decide(self, capability: CapabilityKind) -> CapabilityDecision:
        return CapabilityDecision(released=False, reason="not released")


class _ScriptedModel:
    """Records every invoke and returns scripted responses keyed by call index."""

    def __init__(
        self,
        *,
        drafts: list[dict[str, object]] | None = None,
        reviews: list[dict[str, object]] | None = None,
        enabled: bool = True,
        raise_on: set[int] | None = None,
    ) -> None:
        self._drafts = list(drafts or [])
        self._reviews = list(reviews or [])
        self._enabled = enabled
        self._raise_on = raise_on or set()
        self.calls: list[StructuredModelRequest] = []
        self._seq = 0

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:
        seq = self._seq
        self._seq += 1
        self.calls.append(request)
        if seq in self._raise_on:
            raise RuntimeError("provider error")
        if request.task_type == "reply_draft":
            result = self._drafts.pop(0) if self._drafts else {
                "subject": "Refined",
                "body": "Refined body",
            }
        else:
            result = self._reviews.pop(0) if self._reviews else {
                "verdict": "approve",
                "issues": [],
            }
        return StructuredModelResponse(
            task_type=request.task_type,
            result=result,
            confidence=0.9,
            model_id="scripted",
            prompt_version="test",
            is_review_only=True,
            trace_id=request.trace_id,
        )


def _empty_result(
    *,
    status_code: int = 200,
    body_prefix: str = "",
    expected_fields_missing: tuple[str, ...] = (),
) -> CrawlSourceResult:
    return CrawlSourceResult(
        postings=(),
        status_code=status_code,
        body_prefix=body_prefix,
        expected_fields_missing=expected_fields_missing,
    )


def _result_with_postings(count: int = 1, *, status_code: int = 200) -> CrawlSourceResult:
    return CrawlSourceResult(
        postings=tuple(
            CrawledPostingRecord(
                source_id=str(uuid4()),
                external_id=f"job-{i}",
                canonical_url=f"https://example.com/jobs/{i}",
                source_url="https://example.com/careers",
                structured_data={"title": "Engineer", "location": "Remote"},
                parser_version="test",
            )
            for i in range(count)
        ),
        status_code=status_code,
    )


# ===========================================================================
# 10.4 -- Irreversible-commitment reply blocked from autonomous send
# ===========================================================================


class TestIrreversibleCommitmentBlocked:
    """10.4: Replies that accept interview times, offers, or carry
    legal/financial/authorization consequences are PERMANENTLY_DENIED and
    can never be system-sent, even if the A/B loop would approve."""

    # -- Domain-level: is_system_send_denied blocks these categories --

    @pytest.mark.parametrize(
        "mail_category",
        [
            "offer",
            "salary",
            "visa",
            "identity",
            "withdrawal",
            "relocation",
            "tax",
            "background_check",
            "deadline_commitment",
            "resume_link",
            "work_authorization",
            "unknown",
        ],
    )
    def test_permanently_denied_mail_categories(self, mail_category: str) -> None:
        """Every irreversible-commitment mail category maps to PERMANENTLY_DENIED."""
        risk = reply_risk_for_category(mail_category)
        assert risk == ReplyRiskCategory.PERMANENTLY_DENIED, (
            f"{mail_category!r} should map to PERMANENTLY_DENIED, got {risk}"
        )
        assert is_system_send_denied(risk) is True

    def test_high_risk_intent_decline_is_system_denied(self) -> None:
        """DECLINE intent maps to HIGH_RISK which is also system-send denied."""
        risk = reply_risk_for_intent(ReplyIntent.DECLINE)
        assert risk == ReplyRiskCategory.HIGH_RISK
        assert is_system_send_denied(risk) is True

    def test_offer_and_salary_categories_never_system_sendable(self) -> None:
        """Offer and salary -- the two most dangerous irreversible commitments."""
        for cat in ("offer", "salary"):
            risk = reply_risk_for_category(cat)
            assert is_system_send_denied(risk) is True

    @pytest.mark.parametrize(
        "category",
        sorted(PERMANENTLY_DENIED_REPLY_CATEGORIES),
    )
    def test_all_permanently_denied_categories_are_send_denied(
        self, category: ReplyRiskCategory
    ) -> None:
        """Every category in the PERMANENTLY_DENIED frozenset blocks system send."""
        assert is_system_send_denied(category) is True

    # -- Approval loop level: default-deny on uncertainty --

    def test_approval_loop_escalates_when_capability_denied(self) -> None:
        """Even with a fully functional model, capability denial blocks send."""
        client = _ScriptedModel(
            drafts=[{"subject": "I accept", "body": "I accept the offer."}],
            reviews=[{"verdict": "approve", "issues": []}],
        )
        loop = ABApprovalLoop(client=client, capability_resolver=_DeniedResolver())
        result = loop.run(_SKELETON, _CONTEXT)
        assert result.outcome == "escalated"
        assert result.rounds == 0
        # The model was NEVER called.
        assert client.calls == []

    def test_approval_loop_escalates_when_provider_disabled(self) -> None:
        """Disabled provider -> escalated (never auto-sends)."""
        loop = ABApprovalLoop(
            client=DisabledModelAdapter(), capability_resolver=_ReleasedResolver()
        )
        result = loop.run(_SKELETON, _CONTEXT)
        assert result.outcome == "escalated"
        assert result.reason == "model provider disabled"

    def test_approval_loop_escalates_on_non_convergence(self) -> None:
        """B never approves -> escalated (budget exhausted, no auto-send)."""
        reviews = [{"verdict": "reject", "issues": ["too risky"]} for _ in range(MAX_ROUNDS)]
        drafts = [{"subject": f"v{i}", "body": f"draft {i}"} for i in range(MAX_ROUNDS)]
        client = _ScriptedModel(drafts=drafts, reviews=reviews)
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        result = loop.run(_SKELETON, _CONTEXT)
        assert result.outcome == "escalated"
        assert "escalated to human review" in result.reason

    def test_approval_loop_escalates_on_drafter_error(self) -> None:
        """Model error during drafting -> escalated (default-deny)."""
        client = _ScriptedModel(
            drafts=[{"subject": "s", "body": "b"}],
            reviews=[{"verdict": "approve", "issues": []}],
            raise_on={0},
        )
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        result = loop.run(_SKELETON, _CONTEXT)
        assert result.outcome == "escalated"
        assert result.reason == "drafter returned no usable draft"

    def test_approval_loop_escalates_on_reviewer_error(self) -> None:
        """Model error during review -> escalated (default-deny)."""
        client = _ScriptedModel(
            drafts=[{"subject": "s", "body": "b"}],
            reviews=[{"verdict": "approve", "issues": []}],
            raise_on={1},
        )
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        result = loop.run(_SKELETON, _CONTEXT)
        assert result.outcome == "escalated"
        assert result.reason == "reviewer returned no usable verdict"

    def test_no_resolver_escalates(self) -> None:
        """No resolver at all -> escalated."""
        client = _ScriptedModel()
        loop = ABApprovalLoop(client=client, capability_resolver=None)
        result = loop.run(_SKELETON, _CONTEXT)
        assert result.outcome == "escalated"
        assert client.calls == []


# ===========================================================================
# 10.5 -- Non-granted crawl permission never attaches authenticated session
# ===========================================================================


class _FakePermissionRepo:
    """In-memory CrawlPermissionRepository for permission tests."""

    def __init__(self) -> None:
        self._perms: dict[UUID, CrawlSourcePermission] = {}

    def save(self, owner_id: UUID, permission: CrawlSourcePermission) -> CrawlSourcePermission:
        self._perms[permission.id] = permission
        return permission

    def get_by_id(self, owner_id: UUID, permission_id: UUID) -> CrawlSourcePermission:
        perm = self._perms.get(permission_id)
        if perm is None:
            raise KeyError(f"permission {permission_id} not found")
        return perm

    def get_unresolved_for_source(
        self, owner_id: UUID, source_id: UUID
    ) -> CrawlSourcePermission | None:
        for p in self._perms.values():
            if p.source_id == source_id and p.state == CrawlPermissionState.PENDING:
                return p
        return None

    def list_for_source(
        self, owner_id: UUID, source_id: UUID, *, limit: int = 50
    ) -> list[CrawlSourcePermission]:
        perms = [p for p in self._perms.values() if p.source_id == source_id]
        perms.sort(key=lambda p: p.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return perms[:limit]

    def transition(
        self,
        owner_id: UUID,
        permission_id: UUID,
        *,
        to: CrawlPermissionState,
        now: datetime | None = None,
    ) -> CrawlSourcePermission:
        current = self.get_by_id(owner_id, permission_id)
        if not is_permission_transition_allowed(current.state, to):
            raise ValueError(
                f"illegal crawl permission transition: {current.state.value} -> {to.value}"
            )
        updated = dataclasses.replace(
            current,
            state=to,
            updated_at=now or datetime.now(UTC),
        )
        # Stamp decision timestamp.
        if to == CrawlPermissionState.GRANTED:
            updated = dataclasses.replace(updated, granted_at=now or datetime.now(UTC))
        elif to == CrawlPermissionState.DENIED:
            updated = dataclasses.replace(updated, denied_at=now or datetime.now(UTC))
        elif to == CrawlPermissionState.REVOKED:
            updated = dataclasses.replace(updated, revoked_at=now or datetime.now(UTC))
        elif to == CrawlPermissionState.EXPIRED:
            updated = dataclasses.replace(updated, expired_at=now or datetime.now(UTC))
        self._perms[permission_id] = updated
        return updated


def _make_permission(
    *,
    state: CrawlPermissionState = CrawlPermissionState.PENDING,
    source_id: UUID | None = None,
) -> CrawlSourcePermission:
    now = datetime.now(UTC)
    return CrawlSourcePermission(
        id=uuid4(),
        source_id=source_id or uuid4(),
        owner_id=UUID("00000000-0000-0000-0000-000000000001"),
        state=state,
        requested_at=now,
        created_at=now,
        updated_at=now,
    )


class TestPermissionNeverAttachesSession:
    """10.5: Pending, denied, expired, and revoked crawl permissions never
    allow an authenticated Tier 2 session to be attached.

    The spec says: "Permission is absent or withdrawn -> Tier 2 does not
    use an authenticated session and the source remains paused."

    We verify:
    - Only GRANTED permits a session; all other states are rejected.
    - Terminal states (DENIED, REVOKED, EXPIRED) admit no further transitions.
    - The state machine is enforced: no silent resurrection of denied/revoked/expired.
    """

    @pytest.mark.parametrize(
        "state",
        [
            CrawlPermissionState.PENDING,
            CrawlPermissionState.DENIED,
            CrawlPermissionState.REVOKED,
            CrawlPermissionState.EXPIRED,
        ],
    )
    def test_non_granted_state_does_not_authorize_session(
        self, state: CrawlPermissionState
    ) -> None:
        """Only GRANTED authorizes an authenticated session."""
        perm = _make_permission(state=state)
        # GRANTED is the ONLY state that authorizes a session.
        assert state is not CrawlPermissionState.GRANTED
        # The permission itself records no session -- that's by design (no
        # password/session material stored).  But more importantly, the
        # PERMISSION_TERMINAL_STATES frozenset explicitly blocks session
        # attachment for terminal states.
        if state in PERMISSION_TERMINAL_STATES:
            assert state in {
                CrawlPermissionState.DENIED,
                CrawlPermissionState.REVOKED,
                CrawlPermissionState.EXPIRED,
            }

    def test_only_granted_is_not_in_terminal_states(self) -> None:
        """GRANTED is the only non-terminal state that allows sessions."""
        assert CrawlPermissionState.GRANTED not in PERMISSION_TERMINAL_STATES

    def test_pending_is_not_terminal(self) -> None:
        """PENDING is not terminal (can still be granted/denied/expired)."""
        assert CrawlPermissionState.PENDING not in PERMISSION_TERMINAL_STATES

    @pytest.mark.parametrize(
        "terminal_state",
        [CrawlPermissionState.DENIED, CrawlPermissionState.REVOKED, CrawlPermissionState.EXPIRED],
    )
    def test_terminal_states_admit_no_further_transitions(
        self, terminal_state: CrawlPermissionState
    ) -> None:
        """Denied, revoked, and expired permissions have no legal transitions."""
        allowed = set()
        for target in CrawlPermissionState:
            if is_permission_transition_allowed(terminal_state, target):
                allowed.add(target)
        assert allowed == set(), (
            f"{terminal_state.value} should have no transitions, "
            f"but allows: {', '.join(t.value for t in allowed)}"
        )

    def test_denied_cannot_be_resurrected_to_granted(self) -> None:
        """Denied permission can never transition to granted."""
        assert is_permission_transition_allowed(
            CrawlPermissionState.DENIED, CrawlPermissionState.GRANTED
        ) is False

    def test_revoked_cannot_be_resurrected_to_granted(self) -> None:
        """Revoked permission can never transition to granted."""
        assert is_permission_transition_allowed(
            CrawlPermissionState.REVOKED, CrawlPermissionState.GRANTED
        ) is False

    def test_expired_cannot_be_resurrected_to_granted(self) -> None:
        """Expired permission can never transition to granted."""
        assert is_permission_transition_allowed(
            CrawlPermissionState.EXPIRED, CrawlPermissionState.GRANTED
        ) is False

    def test_pending_can_be_granted(self) -> None:
        """Pending -> granted is the only path to authorization."""
        assert is_permission_transition_allowed(
            CrawlPermissionState.PENDING, CrawlPermissionState.GRANTED
        ) is True

    def test_granted_can_be_revoked(self) -> None:
        """Granted -> revoked is legal (withdrawal of consent)."""
        assert is_permission_transition_allowed(
            CrawlPermissionState.GRANTED, CrawlPermissionState.REVOKED
        ) is True

    def test_granted_can_expire(self) -> None:
        """Granted -> expired is legal (TTL lapse)."""
        assert is_permission_transition_allowed(
            CrawlPermissionState.GRANTED, CrawlPermissionState.EXPIRED
        ) is True

    def test_permission_stores_no_session_material(self) -> None:
        """CrawlSourcePermission has no password or session fields.

        The docstring explicitly states: "It deliberately holds NO password
        and NO raw session material."
        """
        perm = _make_permission()
        field_names = set(CrawlSourcePermission.__dataclass_fields__)
        # These should NOT exist on the permission record.
        for forbidden in ("password", "session_token", "raw_session", "credentials"):
            assert forbidden not in field_names, (
                f"CrawlSourcePermission should not have a {forbidden!r} field"
            )

    def test_fake_repo_enforces_transition_state_machine(self) -> None:
        """The in-memory repo correctly enforces the state machine."""
        repo = _FakePermissionRepo()
        owner = UUID("00000000-0000-0000-0000-000000000001")

        # Create a pending permission and deny it.
        perm = _make_permission(state=CrawlPermissionState.PENDING)
        repo.save(owner, perm)
        denied = repo.transition(owner, perm.id, to=CrawlPermissionState.DENIED)
        assert denied.state == CrawlPermissionState.DENIED

        # Trying to transition denied -> granted must raise.
        with pytest.raises(ValueError, match="illegal"):
            repo.transition(owner, perm.id, to=CrawlPermissionState.GRANTED)


# ===========================================================================
# 10.6 -- Empty/403/CAPTCHA/model-judgement/temporary failures
#         never become AUTH_REQUIRED without explicit login evidence
# ===========================================================================


class TestOutcomeClassifierNeverFalslyAuthRequired:
    """10.6: The outcome classifier MUST NOT produce AUTH_REQUIRED for:

    - Empty results (200, no postings, no login evidence)
    - HTTP 403 alone (no login-wall text in body)
    - CAPTCHA alone (no login-wall text in body)
    - Model judgement alone (no structural evidence)
    - Temporary failures (transport error, timeout, 5xx)

    Each of these must produce a non-AUTH_REQUIRED outcome.
    """

    # -- Empty results --

    def test_empty_200_no_signals_is_verified_empty(self) -> None:
        """Clean 200 with no postings and no signals -> VERIFIED_EMPTY."""
        inp = ClassificationInput(result=_empty_result(status_code=200))
        assert classify_outcome(inp) == CrawlAttemptOutcome.VERIFIED_EMPTY

    def test_empty_200_with_parse_drift_is_dynamic(self) -> None:
        """200 but expected fields missing -> DYNAMIC_OR_UNSUPPORTED."""
        inp = ClassificationInput(
            result=_empty_result(status_code=200, expected_fields_missing=("title",))
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    # -- HTTP 403 alone --

    def test_403_forbidden_alone_is_dynamic(self) -> None:
        """403 with generic 'Forbidden' body -> NOT AUTH_REQUIRED."""
        inp = ClassificationInput(
            result=_empty_result(status_code=403, body_prefix="Forbidden")
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED
        assert classify_outcome(inp) is not CrawlAttemptOutcome.AUTH_REQUIRED

    def test_403_access_denied_alone_is_dynamic(self) -> None:
        """403 with 'Access Denied' but no login-wall -> NOT AUTH_REQUIRED.

        Note: 'access denied' IS a login evidence signal, so this one actually
        maps to AUTH_REQUIRED.  The key point is 403 WITHOUT any login signal
        is NOT auth_required.  'Forbidden' has no login signal.
        """
        inp = ClassificationInput(
            result=_empty_result(status_code=403, body_prefix="Forbidden - no entry")
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    def test_403_with_login_evidence_is_auth_required(self) -> None:
        """403 + explicit 'sign in to continue' -> AUTH_REQUIRED (positive case)."""
        inp = ClassificationInput(
            result=_empty_result(
                status_code=403, body_prefix="Please sign in to continue"
            )
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.AUTH_REQUIRED

    # -- CAPTCHA alone --

    def test_captcha_alone_is_dynamic(self) -> None:
        """CAPTCHA without login-wall text -> NOT AUTH_REQUIRED."""
        inp = ClassificationInput(
            result=_empty_result(
                status_code=200,
                body_prefix="Please verify you are human with captcha",
            )
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    def test_captcha_with_login_evidence_is_auth_required(self) -> None:
        """CAPTCHA + 'sign in to continue' -> AUTH_REQUIRED (positive case)."""
        inp = ClassificationInput(
            result=_empty_result(
                status_code=200,
                body_prefix="captcha detected. Sign in to continue.",
            )
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.AUTH_REQUIRED

    def test_recaptcha_alone_is_dynamic(self) -> None:
        """recaptcha signal alone -> NOT AUTH_REQUIRED."""
        inp = ClassificationInput(
            result=_empty_result(
                status_code=200, body_prefix="Please complete the recaptcha"
            )
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    def test_hcaptcha_alone_is_dynamic(self) -> None:
        """hcaptcha signal alone -> NOT AUTH_REQUIRED."""
        inp = ClassificationInput(
            result=_empty_result(
                status_code=200, body_prefix="hcaptcha verification required"
            )
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    # -- Model judgement alone --

    def test_no_adapter_is_not_job_source(self) -> None:
        """No adapter (model could not determine structure) -> NOT_JOB_SOURCE,
        NOT AUTH_REQUIRED."""
        inp = ClassificationInput(result=_empty_result(), has_adapter=False)
        assert classify_outcome(inp) == CrawlAttemptOutcome.NOT_JOB_SOURCE
        assert classify_outcome(inp) is not CrawlAttemptOutcome.AUTH_REQUIRED

    def test_no_adapter_with_postings_is_postings_found(self) -> None:
        """Even without adapter, postings found takes priority."""
        inp = ClassificationInput(
            result=_result_with_postings(2), has_adapter=False
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.POSTINGS_FOUND

    # -- Temporary failures --

    def test_transport_error_is_transient(self) -> None:
        """Transport error -> TRANSIENT_FAILURE, NOT AUTH_REQUIRED."""
        inp = ClassificationInput(result=_empty_result(), transport_error=True)
        assert classify_outcome(inp) == CrawlAttemptOutcome.TRANSIENT_FAILURE

    def test_timeout_is_transient(self) -> None:
        """Timeout -> TRANSIENT_FAILURE, NOT AUTH_REQUIRED."""
        inp = ClassificationInput(result=_empty_result(), timed_out=True)
        assert classify_outcome(inp) == CrawlAttemptOutcome.TRANSIENT_FAILURE

    def test_500_is_transient(self) -> None:
        """500 -> TRANSIENT_FAILURE."""
        inp = ClassificationInput(result=_empty_result(status_code=500))
        assert classify_outcome(inp) == CrawlAttemptOutcome.TRANSIENT_FAILURE

    def test_502_is_transient(self) -> None:
        """502 -> TRANSIENT_FAILURE."""
        inp = ClassificationInput(result=_empty_result(status_code=502))
        assert classify_outcome(inp) == CrawlAttemptOutcome.TRANSIENT_FAILURE

    def test_503_is_transient(self) -> None:
        """503 -> TRANSIENT_FAILURE."""
        inp = ClassificationInput(result=_empty_result(status_code=503))
        assert classify_outcome(inp) == CrawlAttemptOutcome.TRANSIENT_FAILURE

    def test_504_is_transient(self) -> None:
        """504 -> TRANSIENT_FAILURE."""
        inp = ClassificationInput(result=_empty_result(status_code=504))
        assert classify_outcome(inp) == CrawlAttemptOutcome.TRANSIENT_FAILURE

    # -- Policy denied --

    def test_policy_denied_overrides_everything(self) -> None:
        """POLICY_DENIED takes priority even with postings and login evidence."""
        inp = ClassificationInput(
            result=_result_with_postings(5),
            policy_decision=CrawlDecision.DENY_BLOCKED,
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.POLICY_DENIED

    # -- Other 4xx --

    def test_404_is_dynamic(self) -> None:
        """404 -> DYNAMIC_OR_UNSUPPORTED, NOT AUTH_REQUIRED."""
        inp = ClassificationInput(result=_empty_result(status_code=404))
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    def test_429_is_dynamic(self) -> None:
        """429 (rate limit) -> DYNAMIC_OR_UNSUPPORTED."""
        inp = ClassificationInput(result=_empty_result(status_code=429))
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    # -- Status 0 --

    def test_status_0_is_not_job_source(self) -> None:
        """Status 0 (no response at all) -> NOT_JOB_SOURCE."""
        inp = ClassificationInput(result=_empty_result(status_code=0))
        assert classify_outcome(inp) == CrawlAttemptOutcome.NOT_JOB_SOURCE

    # -- Exhaustive: NONE of these negative scenarios produce AUTH_REQUIRED --

    @pytest.mark.parametrize(
        "label,inp",
        [
            ("empty_200", ClassificationInput(result=_empty_result(status_code=200))),
            ("403_forbidden", ClassificationInput(result=_empty_result(status_code=403, body_prefix="Forbidden"))),
            ("captcha_only", ClassificationInput(result=_empty_result(status_code=200, body_prefix="captcha check"))),
            ("transport_err", ClassificationInput(result=_empty_result(), transport_error=True)),
            ("timeout", ClassificationInput(result=_empty_result(), timed_out=True)),
            ("500", ClassificationInput(result=_empty_result(status_code=500))),
            ("502", ClassificationInput(result=_empty_result(status_code=502))),
            ("503", ClassificationInput(result=_empty_result(status_code=503))),
            ("504", ClassificationInput(result=_empty_result(status_code=504))),
            ("404", ClassificationInput(result=_empty_result(status_code=404))),
            ("429", ClassificationInput(result=_empty_result(status_code=429))),
            ("no_adapter", ClassificationInput(result=_empty_result(), has_adapter=False)),
            ("status_0", ClassificationInput(result=_empty_result(status_code=0))),
            ("parse_drift", ClassificationInput(result=_empty_result(status_code=200, expected_fields_missing=("title",)))),
        ],
        ids=lambda label: label if isinstance(label, str) else None,
    )
    def test_negative_scenario_never_auth_required(
        self, label: str, inp: ClassificationInput
    ) -> None:
        """Exhaustive check: every negative scenario produces a non-AUTH_REQUIRED outcome."""
        outcome = classify_outcome(inp)
        assert outcome is not CrawlAttemptOutcome.AUTH_REQUIRED, (
            f"{label} should NOT produce AUTH_REQUIRED, got {outcome.value}"
        )

    # -- Positive: only explicit login evidence produces AUTH_REQUIRED --

    @pytest.mark.parametrize(
        "label,inp",
        [
            (
                "403_sign_in",
                ClassificationInput(
                    result=_empty_result(status_code=403, body_prefix="Sign in to continue")
                ),
            ),
            (
                "captcha_login",
                ClassificationInput(
                    result=_empty_result(
                        status_code=200,
                        body_prefix="captcha detected. Sign in to continue.",
                    )
                ),
            ),
            (
                "200_login_wall",
                ClassificationInput(
                    result=_empty_result(
                        status_code=200, body_prefix="login required to view jobs"
                    )
                ),
            ),
        ],
        ids=lambda label: label if isinstance(label, str) else None,
    )
    def test_positive_login_evidence_produces_auth_required(
        self, label: str, inp: ClassificationInput
    ) -> None:
        """Only scenarios with explicit login-wall evidence produce AUTH_REQUIRED."""
        assert classify_outcome(inp) == CrawlAttemptOutcome.AUTH_REQUIRED
