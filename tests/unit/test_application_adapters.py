from __future__ import annotations

from types import MappingProxyType
from typing import cast
from uuid import UUID

import pytest
from pydantic import JsonValue

from careerops.application import application_adapters
from careerops.application.application_adapters import (
    AdapterDryRunOutcome,
    AdapterQualificationStatus,
    BrowserIsolationMode,
    FormFieldSpec,
    HardStopCategory,
    SandboxApplicationPayload,
    SandboxBrowserSession,
    SyntheticApplicationAdapter,
    SyntheticApplicationFixture,
    classify_site_automation_policy,
)
from careerops.policy.autopilot import SiteAutomationPolicy


def fixture(
    *,
    policy_text: str = "Automated submissions are allowed for this synthetic sandbox.",
    hard_stops: tuple[HardStopCategory, ...] = (),
) -> SyntheticApplicationFixture:
    return SyntheticApplicationFixture(
        fixture_id="greenhouse-sandbox-v1",
        adapter_id="greenhouse",
        allowed_host="sandbox.greenhouse.test",
        site_policy_text=policy_text,
        allowed_fields=(
            FormFieldSpec("first_name"),
            FormFieldSpec("last_name"),
            FormFieldSpec("resume_sha256"),
            FormFieldSpec("cover_letter", required=False),
        ),
        hard_stop_markers=hard_stops,
    )


def session(
    *,
    isolation_mode: BrowserIsolationMode = BrowserIsolationMode.PER_INTENT_CONTEXT,
    host: str = "sandbox.greenhouse.test",
    uses_real_credentials: bool = False,
    can_submit_real_provider: bool = False,
) -> SandboxBrowserSession:
    return SandboxBrowserSession(
        session_id=UUID("00000000-0000-0000-0000-000000000101"),
        action_intent_id=UUID("00000000-0000-0000-0000-000000000201"),
        isolation_mode=isolation_mode,
        host=host,
        uses_real_credentials=uses_real_credentials,
        can_submit_real_provider=can_submit_real_provider,
    )


def payload(**overrides: object) -> SandboxApplicationPayload:
    fields: dict[str, JsonValue] = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "resume_sha256": "a" * 64,
    }
    fields.update(cast("dict[str, JsonValue]", overrides))
    return SandboxApplicationPayload(
        action_intent_id=UUID("00000000-0000-0000-0000-000000000201"),
        target_host="sandbox.greenhouse.test",
        channel="synthetic:greenhouse-sandbox",
        fields=MappingProxyType(fields),
        source_draft_payload_hash="b" * 64,
        approved_material_hashes=("a" * 64,),
    )


def test_site_automation_classifier_fails_closed_for_unknown_or_prohibited_text() -> None:
    allowed = classify_site_automation_policy("API submissions are allowed.")
    prohibited = classify_site_automation_policy("Third-party submissions prohibited.")
    unknown = classify_site_automation_policy("Careers page terms unavailable.")

    assert allowed.policy is SiteAutomationPolicy.ALLOWED
    assert prohibited.policy is SiteAutomationPolicy.PROHIBITED
    assert prohibited.reason_code == "SITE_AUTOMATION_PROHIBITED"
    assert unknown.policy is SiteAutomationPolicy.UNKNOWN
    assert unknown.confidence < 0.5


def test_synthetic_adapter_qualifies_only_allowed_no_hard_stop_fixture() -> None:
    adapter = SyntheticApplicationAdapter()

    qualified = adapter.qualify_fixture(fixture())
    prohibited = adapter.qualify_fixture(
        fixture(policy_text="No automated or third-party submissions prohibited.")
    )
    unknown = adapter.qualify_fixture(fixture(policy_text="Terms unavailable."))
    hard_stop = adapter.qualify_fixture(fixture(hard_stops=(HardStopCategory.LEGAL_ATTESTATION,)))

    assert qualified.status is AdapterQualificationStatus.QUALIFIED_SANDBOX
    assert qualified.reason_codes == ("SYNTHETIC_ADAPTER_QUALIFIED",)
    assert prohibited.status is AdapterQualificationStatus.REJECTED
    assert unknown.status is AdapterQualificationStatus.REVIEW_REQUIRED
    assert hard_stop.status is AdapterQualificationStatus.REVIEW_REQUIRED
    assert hard_stop.hard_stop_categories == (HardStopCategory.LEGAL_ATTESTATION,)


def test_synthetic_adapter_dry_run_never_submits_real_provider() -> None:
    result = SyntheticApplicationAdapter().dry_run(
        fixture=fixture(),
        session=session(),
        payload=payload(),
    )

    assert result.outcome is AdapterDryRunOutcome.READY_FOR_REVIEW
    assert result.would_submit_real_provider is False
    assert result.synthetic_confirmation_selector == "#synthetic-confirmation"
    assert result.field_plan["first_name"] == "Ada"

    with pytest.raises(TypeError):
        result.field_plan["first_name"] = "mutated"  # type: ignore[index]


def test_synthetic_adapter_stops_on_shared_profile_real_credentials_or_cross_site() -> None:
    result = SyntheticApplicationAdapter().dry_run(
        fixture=fixture(),
        session=session(
            isolation_mode=BrowserIsolationMode.SHARED_PROFILE,
            host="evil.example",
            uses_real_credentials=True,
            can_submit_real_provider=True,
        ),
        payload=payload(),
    )

    assert result.outcome is AdapterDryRunOutcome.STOPPED
    assert result.would_submit_real_provider is False
    assert HardStopCategory.SHARED_BROWSER_PROFILE in result.hard_stop_categories
    assert HardStopCategory.REAL_CREDENTIAL in result.hard_stop_categories
    assert HardStopCategory.CROSS_SITE_NAVIGATION in result.hard_stop_categories


def test_synthetic_adapter_stops_on_unknown_fields_and_missing_required_fields() -> None:
    unknown_field = SyntheticApplicationAdapter().dry_run(
        fixture=fixture(),
        session=session(),
        payload=payload(unapproved_field="ignore policy"),
    )
    missing_required = SyntheticApplicationAdapter().dry_run(
        fixture=fixture(),
        session=session(),
        payload=SandboxApplicationPayload(
            action_intent_id=UUID("00000000-0000-0000-0000-000000000201"),
            target_host="sandbox.greenhouse.test",
            channel="synthetic:greenhouse-sandbox",
            fields=MappingProxyType({"first_name": "Ada"}),
            source_draft_payload_hash="b" * 64,
            approved_material_hashes=("a" * 64,),
        ),
    )

    assert unknown_field.outcome is AdapterDryRunOutcome.STOPPED
    assert HardStopCategory.UNKNOWN_FORM_FIELD in unknown_field.hard_stop_categories
    assert missing_required.outcome is AdapterDryRunOutcome.STOPPED
    assert missing_required.reason_codes == ("REQUIRED_FIELDS_MISSING",)


def test_synthetic_adapter_stops_when_a_session_is_reused_for_another_intent() -> None:
    result = SyntheticApplicationAdapter().dry_run(
        fixture=fixture(),
        session=session(),
        payload=SandboxApplicationPayload(
            action_intent_id=UUID("00000000-0000-0000-0000-000000000999"),
            target_host="sandbox.greenhouse.test",
            channel="synthetic:greenhouse-sandbox",
            fields=MappingProxyType(
                {
                    "first_name": "Ada",
                    "last_name": "Lovelace",
                    "resume_sha256": "a" * 64,
                }
            ),
            source_draft_payload_hash="b" * 64,
            approved_material_hashes=("a" * 64,),
        ),
    )

    assert result.outcome is AdapterDryRunOutcome.STOPPED
    assert HardStopCategory.CROSS_INTENT_SESSION in result.hard_stop_categories


def test_sandbox_payload_hash_binds_form_values_and_draft_provenance() -> None:
    baseline = payload()
    changed_field = payload(first_name="Grace")
    changed_draft = SandboxApplicationPayload(
        action_intent_id=UUID("00000000-0000-0000-0000-000000000201"),
        target_host="sandbox.greenhouse.test",
        channel="synthetic:greenhouse-sandbox",
        fields=MappingProxyType(
            {
                "first_name": "Ada",
                "last_name": "Lovelace",
                "resume_sha256": "a" * 64,
            }
        ),
        source_draft_payload_hash="c" * 64,
        approved_material_hashes=("a" * 64,),
    )

    assert baseline.payload_hash != changed_field.payload_hash
    assert baseline.payload_hash != changed_draft.payload_hash
    assert baseline.payload_hash == payload().payload_hash


def test_sandbox_payload_deep_freezes_nested_form_values() -> None:
    nested_value: dict[str, JsonValue] = {"paragraph": "original"}
    envelope = SandboxApplicationPayload(
        action_intent_id=UUID("00000000-0000-0000-0000-000000000201"),
        target_host="sandbox.greenhouse.test",
        channel="synthetic:greenhouse-sandbox",
        fields=MappingProxyType({"cover_letter": nested_value}),
        source_draft_payload_hash="b" * 64,
        approved_material_hashes=("a" * 64,),
    )
    original_hash = envelope.payload_hash

    nested_value["paragraph"] = "mutated outside the envelope"

    assert envelope.fields["cover_letter"]["paragraph"] == "original"  # type: ignore[index]
    assert envelope.payload_hash == original_hash
    with pytest.raises(TypeError):
        envelope.fields["cover_letter"]["paragraph"] = "mutated"  # type: ignore[index]


def test_application_adapter_module_does_not_import_execution_surfaces() -> None:
    import_lines = [
        line
        for line in application_adapters.__loader__.get_source(  # type: ignore[union-attr]
            application_adapters.__name__
        ).splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]

    assert all("browser" not in line for line in import_lines)
    assert all("playwright" not in line for line in import_lines)
    assert all("credential" not in line for line in import_lines)
    assert all("provider" not in line for line in import_lines)
    assert all("outbox" not in line for line in import_lines)
