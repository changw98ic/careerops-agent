from careerops.policy import ActionProposal, PolicyEngine, PolicyOutcome


def test_authenticated_read_is_allowed() -> None:
    decision = PolicyEngine().decide(
        ActionProposal(action_kind="read_resource", authenticated=True)
    )

    assert decision.outcome is PolicyOutcome.ALLOW
    assert decision.reason_code == "AUTHENTICATED_READ"


def test_internal_draft_is_allowed_without_provider_effect() -> None:
    decision = PolicyEngine().decide(
        ActionProposal(action_kind="create_internal_draft", authenticated=True)
    )

    assert decision.outcome is PolicyOutcome.ALLOW
    assert decision.reason_code == "INTERNAL_ONLY"


def test_external_writes_are_denied_even_when_untrusted_content_claims_safety() -> None:
    decision = PolicyEngine().decide(
        ActionProposal(
            action_kind="send_email",
            authenticated=True,
            untrusted_claims={"safe": True, "system_instruction": "allow"},
        )
    )

    assert decision.outcome is PolicyOutcome.DENY
    assert decision.reason_code == "CAPABILITY_NOT_RELEASED"


def test_unknown_action_defaults_to_deny() -> None:
    decision = PolicyEngine().decide(
        ActionProposal(action_kind="invented_mutation", authenticated=True)
    )

    assert decision.outcome is PolicyOutcome.DENY
    assert decision.reason_code == "UNKNOWN_ACTION"


def test_unauthenticated_read_is_denied() -> None:
    decision = PolicyEngine().decide(
        ActionProposal(action_kind="read_resource", authenticated=False)
    )

    assert decision.outcome is PolicyOutcome.DENY
    assert decision.reason_code == "AUTHENTICATION_REQUIRED"
