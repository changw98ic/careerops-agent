from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from careerops.policy import ActionProposal, PolicyEngine, PolicyOutcome
from careerops.policy.autopilot import (
    AUTOPILOT_RULE_VERSION,
    AutopilotGrant,
    AutopilotGrantStatus,
    AutopilotOutcome,
    AutopilotPolicyEngine,
    AutopilotSubmissionFacts,
    SiteAutomationPolicy,
)

NOW = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)
OWNER_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_OWNER_ID = UUID("00000000-0000-0000-0000-000000000002")
CAMPAIGN_ID = UUID("00000000-0000-0000-0000-000000000010")
OTHER_CAMPAIGN_ID = UUID("00000000-0000-0000-0000-000000000011")
GRANT_ID = UUID("00000000-0000-0000-0000-000000000100")
PAYLOAD_HASH = "a" * 64
MATERIAL_HASH = "b" * 64
UNAPPROVED_MATERIAL_HASH = "c" * 64


def active_grant() -> AutopilotGrant:
    return AutopilotGrant(
        id=GRANT_ID,
        actor_id=OWNER_ID,
        campaign_id=CAMPAIGN_ID,
        version=1,
        action_kinds=frozenset({"submit_application"}),
        channels=frozenset({"qualified_browser"}),
        target_hosts=frozenset({"careers.example.test"}),
        approved_material_hashes=frozenset({MATERIAL_HASH}),
        max_submissions=3,
        max_submissions_per_day=2,
        expires_at=NOW + timedelta(days=1),
        policy_version="submission-policy.v1",
        release_qualification_version="adapter-release.v1",
    )


def trusted_submission() -> AutopilotSubmissionFacts:
    return AutopilotSubmissionFacts(
        authenticated=True,
        actor_id=OWNER_ID,
        campaign_id=CAMPAIGN_ID,
        action_kind="submit_application",
        channel="qualified_browser",
        target_host="careers.example.test",
        payload_hash=PAYLOAD_HASH,
        policy_payload_hash=PAYLOAD_HASH,
        material_hashes=frozenset({MATERIAL_HASH}),
        target_matches_campaign=True,
        evidence_complete=True,
        site_policy=SiteAutomationPolicy.ALLOWED,
        adapter_release_qualified=True,
        credentials_active=True,
        policy_version="submission-policy.v1",
        release_qualification_version="adapter-release.v1",
        confirmed_submission_count=0,
        confirmed_submission_count_today=0,
    )


def test_matching_trusted_facts_allow_only_the_distinct_autopilot_outcome() -> None:
    decision = AutopilotPolicyEngine().decide(active_grant(), trusted_submission(), now=NOW)

    assert decision.outcome is AutopilotOutcome.ALLOW_AUTOPILOT_SUBMISSION
    assert decision.reason_code == "AUTOPILOT_GRANT_MATCHED"
    assert decision.rule_version == AUTOPILOT_RULE_VERSION


def test_default_policy_engine_still_denies_application_submission() -> None:
    decision = PolicyEngine().decide(
        ActionProposal(action_kind="submit_application", authenticated=True)
    )

    assert decision.outcome is PolicyOutcome.DENY
    assert decision.reason_code == "UNKNOWN_ACTION"


@pytest.mark.parametrize(
    ("grant", "facts", "reason_code"),
    [
        (
            active_grant(),
            replace(trusted_submission(), actor_id=OTHER_OWNER_ID),
            "GRANT_ACTOR_MISMATCH",
        ),
        (
            active_grant(),
            replace(trusted_submission(), campaign_id=OTHER_CAMPAIGN_ID),
            "GRANT_CAMPAIGN_MISMATCH",
        ),
        (
            replace(active_grant(), action_kinds=frozenset({"other_action"})),
            trusted_submission(),
            "ACTION_NOT_GRANTED",
        ),
        (
            active_grant(),
            replace(trusted_submission(), channel="other_channel"),
            "CHANNEL_NOT_GRANTED",
        ),
        (
            active_grant(),
            replace(trusted_submission(), target_host="other.example.test"),
            "TARGET_HOST_NOT_GRANTED",
        ),
    ],
)
def test_scope_mismatches_deny_autopilot(
    grant: AutopilotGrant,
    facts: AutopilotSubmissionFacts,
    reason_code: str,
) -> None:
    decision = AutopilotPolicyEngine().decide(grant, facts, now=NOW)

    assert decision.outcome is AutopilotOutcome.DENY
    assert decision.reason_code == reason_code


@pytest.mark.parametrize(
    ("grant", "facts", "reason_code"),
    [
        (
            replace(active_grant(), expires_at=NOW),
            trusted_submission(),
            "GRANT_EXPIRED",
        ),
        (
            replace(active_grant(), status=AutopilotGrantStatus.REVOKED),
            trusted_submission(),
            "GRANT_NOT_ACTIVE",
        ),
        (
            replace(active_grant(), status=AutopilotGrantStatus.SUPERSEDED),
            trusted_submission(),
            "GRANT_NOT_ACTIVE",
        ),
        (
            active_grant(),
            replace(trusted_submission(), confirmed_submission_count=3),
            "GRANT_CAP_EXHAUSTED",
        ),
        (
            active_grant(),
            replace(trusted_submission(), confirmed_submission_count_today=2),
            "DAILY_CAP_EXHAUSTED",
        ),
    ],
)
def test_inactive_or_exhausted_grants_deny_at_the_boundary(
    grant: AutopilotGrant,
    facts: AutopilotSubmissionFacts,
    reason_code: str,
) -> None:
    decision = AutopilotPolicyEngine().decide(grant, facts, now=NOW)

    assert decision.outcome is AutopilotOutcome.DENY
    assert decision.reason_code == reason_code


@pytest.mark.parametrize(
    ("facts", "reason_code"),
    [
        (replace(trusted_submission(), policy_payload_hash="d" * 64), "PAYLOAD_HASH_MISMATCH"),
        (replace(trusted_submission(), payload_hash="not-a-hash"), "INVALID_PAYLOAD_HASH"),
        (
            replace(trusted_submission(), policy_version="submission-policy.v2"),
            "STALE_POLICY_VERSION",
        ),
        (
            replace(trusted_submission(), release_qualification_version="adapter-release.v2"),
            "STALE_RELEASE_QUALIFICATION",
        ),
    ],
)
def test_payload_and_version_binding_fail_closed(
    facts: AutopilotSubmissionFacts,
    reason_code: str,
) -> None:
    decision = AutopilotPolicyEngine().decide(active_grant(), facts, now=NOW)

    assert decision.outcome is AutopilotOutcome.DENY
    assert decision.reason_code == reason_code


@pytest.mark.parametrize(
    ("facts", "reason_code"),
    [
        (
            replace(trusted_submission(), global_kill_switch_active=True),
            "GLOBAL_KILL_SWITCH_ACTIVE",
        ),
        (
            replace(trusted_submission(), campaign_kill_switch_active=True),
            "CAMPAIGN_KILL_SWITCH_ACTIVE",
        ),
        (
            replace(trusted_submission(), provider_kill_switch_active=True),
            "PROVIDER_KILL_SWITCH_ACTIVE",
        ),
        (
            replace(trusted_submission(), adapter_release_qualified=False),
            "ADAPTER_NOT_RELEASE_QUALIFIED",
        ),
        (replace(trusted_submission(), credentials_active=False), "CREDENTIALS_UNAVAILABLE"),
    ],
)
def test_disabled_execution_preconditions_deny(
    facts: AutopilotSubmissionFacts,
    reason_code: str,
) -> None:
    decision = AutopilotPolicyEngine().decide(active_grant(), facts, now=NOW)

    assert decision.outcome is AutopilotOutcome.DENY
    assert decision.reason_code == reason_code


@pytest.mark.parametrize(
    ("facts", "reason_code"),
    [
        (
            replace(trusted_submission(), site_policy=SiteAutomationPolicy.PROHIBITED),
            "SITE_AUTOMATION_PROHIBITED",
        ),
        (
            replace(trusted_submission(), duplicate_or_ambiguous=True),
            "DUPLICATE_OR_AMBIGUOUS_SUBMISSION",
        ),
        (
            replace(trusted_submission(), hard_stop_categories=frozenset({"legal_attestation"})),
            "HARD_STOP_REQUIRES_REVIEW",
        ),
        (
            replace(trusted_submission(), site_policy=SiteAutomationPolicy.UNKNOWN),
            "SITE_POLICY_UNKNOWN",
        ),
        (replace(trusted_submission(), target_matches_campaign=False), "TARGET_OUTSIDE_CAMPAIGN"),
        (replace(trusted_submission(), evidence_complete=False), "EVIDENCE_INCOMPLETE"),
        (
            replace(
                trusted_submission(),
                material_hashes=frozenset({UNAPPROVED_MATERIAL_HASH}),
            ),
            "UNAPPROVED_CAMPAIGN_MATERIAL",
        ),
        (
            replace(trusted_submission(), material_hashes=frozenset()),
            "MATERIAL_EVIDENCE_MISSING",
        ),
    ],
)
def test_hard_stops_and_uncertainty_require_review_not_autopilot(
    facts: AutopilotSubmissionFacts,
    reason_code: str,
) -> None:
    decision = AutopilotPolicyEngine().decide(active_grant(), facts, now=NOW)

    assert decision.outcome is AutopilotOutcome.REQUIRE_APPROVAL
    assert decision.reason_code == reason_code


def test_grants_are_immutable_value_objects() -> None:
    grant = active_grant()

    with pytest.raises(AttributeError):
        grant.max_submissions = 99  # type: ignore[misc]


def test_grant_copies_mutable_scope_inputs_before_evaluation() -> None:
    action_kinds = {"submit_application"}
    grant = replace(active_grant(), action_kinds=action_kinds)  # type: ignore[arg-type]
    action_kinds.add("other_action")

    assert grant.action_kinds == frozenset({"submit_application"})
