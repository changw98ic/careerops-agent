from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import cast
from uuid import UUID

from pydantic import JsonValue

from careerops.policy.autopilot import SiteAutomationPolicy

_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_FIELD = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PROHIBITED_TERMS = (
    "no automated",
    "automation prohibited",
    "bots prohibited",
    "third-party submissions prohibited",
)
_ALLOWED_TERMS = (
    "automated submissions are allowed",
    "agentic submissions are allowed",
    "api submissions are allowed",
)


class BrowserIsolationMode(StrEnum):
    PER_INTENT_CONTEXT = "per_intent_context"
    SHARED_PROFILE = "shared_profile"


class AdapterQualificationStatus(StrEnum):
    QUALIFIED_SANDBOX = "qualified_sandbox"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"


class AdapterDryRunOutcome(StrEnum):
    READY_FOR_REVIEW = "ready_for_review"
    STOPPED = "stopped"


class HardStopCategory(StrEnum):
    CAPTCHA = "captcha"
    MFA = "mfa"
    LEGAL_ATTESTATION = "legal_attestation"
    SENSITIVE_EEO = "sensitive_eeo"
    IDENTITY_DOCUMENT = "identity_document"
    PAYMENT = "payment"
    UNKNOWN_FORM_FIELD = "unknown_form_field"
    CROSS_SITE_NAVIGATION = "cross_site_navigation"
    CROSS_INTENT_SESSION = "cross_intent_session"
    SHARED_BROWSER_PROFILE = "shared_browser_profile"
    REAL_CREDENTIAL = "real_credential"


@dataclass(frozen=True, slots=True)
class SiteAutomationClassification:
    policy: SiteAutomationPolicy
    confidence: float
    reason_code: str

    def __post_init__(self) -> None:
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("site automation classification confidence must be in [0, 1]")
        _validate_identifier(self.reason_code, "reason_code")


@dataclass(frozen=True, slots=True)
class SandboxBrowserSession:
    session_id: UUID
    action_intent_id: UUID
    isolation_mode: BrowserIsolationMode
    host: str
    uses_real_credentials: bool = False
    can_submit_real_provider: bool = False

    def __post_init__(self) -> None:
        _validate_host(self.host, "session host")


@dataclass(frozen=True, slots=True)
class FormFieldSpec:
    name: str
    required: bool = True

    def __post_init__(self) -> None:
        _validate_field(self.name, "form field name")


@dataclass(frozen=True, slots=True)
class SyntheticApplicationFixture:
    fixture_id: str
    adapter_id: str
    allowed_host: str
    site_policy_text: str
    allowed_fields: tuple[FormFieldSpec, ...]
    hard_stop_markers: tuple[HardStopCategory, ...] = ()
    expected_confirmation_selector: str = "#synthetic-confirmation"

    def __post_init__(self) -> None:
        _validate_identifier(self.fixture_id, "fixture_id")
        _validate_identifier(self.adapter_id, "adapter_id")
        _validate_host(self.allowed_host, "allowed_host")
        if not self.site_policy_text.strip():
            raise ValueError("site_policy_text must not be blank")
        if not self.allowed_fields:
            raise ValueError("synthetic fixture requires an explicit field allowlist")
        object.__setattr__(self, "allowed_fields", tuple(self.allowed_fields))
        if len(self.allowed_field_names) != len(self.allowed_fields):
            raise ValueError("synthetic fixture field allowlist must not contain duplicates")
        object.__setattr__(self, "hard_stop_markers", tuple(self.hard_stop_markers))
        _validate_identifier(
            self.expected_confirmation_selector.removeprefix("#"),
            "expected_confirmation_selector",
        )

    @property
    def allowed_field_names(self) -> frozenset[str]:
        return frozenset(field.name for field in self.allowed_fields)


@dataclass(frozen=True, slots=True)
class SandboxApplicationPayload:
    """Immutable synthetic submission envelope.

    ``payload_hash`` is derived locally from the exact intent, target, channel, form fields,
    approved material hashes, and reviewed-draft provenance. Callers cannot supply a convenient
    hash for different form values, so a later authority or database row that binds this hash
    also binds the form plan and material scope.
    """

    action_intent_id: UUID
    target_host: str
    channel: str
    fields: MappingProxyType[str, JsonValue]
    source_draft_payload_hash: str
    approved_material_hashes: tuple[str, ...]
    payload_hash: str = field(init=False)

    def __post_init__(self) -> None:
        target_host = self.target_host.lower()
        _validate_host(target_host, "target_host")
        _validate_identifier(self.channel, "channel")
        _validate_hash(self.source_draft_payload_hash, "source_draft_payload_hash")
        material_hashes = tuple(sorted(self.approved_material_hashes))
        if not material_hashes:
            raise ValueError("approved_material_hashes must not be empty")
        if len(set(material_hashes)) != len(material_hashes):
            raise ValueError("approved_material_hashes must not contain duplicates")
        for material_hash in material_hashes:
            _validate_hash(material_hash, "approved_material_hash")
        for field_name in self.fields:
            _validate_field(field_name, "payload field")
        fields = MappingProxyType(
            {field_name: _freeze_json_value(value) for field_name, value in self.fields.items()}
        )
        object.__setattr__(self, "target_host", target_host)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "approved_material_hashes", material_hashes)
        object.__setattr__(
            self,
            "payload_hash",
            _canonical_submission_payload_hash(
                action_intent_id=self.action_intent_id,
                target_host=target_host,
                channel=self.channel,
                fields=fields,
                source_draft_payload_hash=self.source_draft_payload_hash,
                approved_material_hashes=material_hashes,
            ),
        )


@dataclass(frozen=True, slots=True)
class AdapterQualification:
    status: AdapterQualificationStatus
    site_classification: SiteAutomationClassification
    hard_stop_categories: tuple[HardStopCategory, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdapterDryRunResult:
    outcome: AdapterDryRunOutcome
    would_submit_real_provider: bool
    field_plan: MappingProxyType[str, JsonValue]
    hard_stop_categories: tuple[HardStopCategory, ...]
    reason_codes: tuple[str, ...]
    synthetic_confirmation_selector: str | None = None


class ApplicationAdapterContract:
    def qualify_fixture(self, fixture: SyntheticApplicationFixture) -> AdapterQualification:
        raise NotImplementedError

    def dry_run(
        self,
        *,
        fixture: SyntheticApplicationFixture,
        session: SandboxBrowserSession,
        payload: SandboxApplicationPayload,
    ) -> AdapterDryRunResult:
        raise NotImplementedError


class SyntheticApplicationAdapter(ApplicationAdapterContract):
    """Constrained sandbox adapter used to qualify future real-site adapters."""

    def qualify_fixture(self, fixture: SyntheticApplicationFixture) -> AdapterQualification:
        site_classification = classify_site_automation_policy(fixture.site_policy_text)
        hard_stops = tuple(fixture.hard_stop_markers)
        if site_classification.policy is SiteAutomationPolicy.PROHIBITED:
            return _qualification(
                AdapterQualificationStatus.REJECTED,
                site_classification,
                hard_stops,
                ("SITE_AUTOMATION_PROHIBITED",),
            )
        if site_classification.policy is not SiteAutomationPolicy.ALLOWED:
            return _qualification(
                AdapterQualificationStatus.REVIEW_REQUIRED,
                site_classification,
                hard_stops,
                ("SITE_AUTOMATION_UNKNOWN",),
            )
        if hard_stops:
            return _qualification(
                AdapterQualificationStatus.REVIEW_REQUIRED,
                site_classification,
                hard_stops,
                ("HARD_STOP_PRESENT",),
            )
        return _qualification(
            AdapterQualificationStatus.QUALIFIED_SANDBOX,
            site_classification,
            (),
            ("SYNTHETIC_ADAPTER_QUALIFIED",),
        )

    def dry_run(
        self,
        *,
        fixture: SyntheticApplicationFixture,
        session: SandboxBrowserSession,
        payload: SandboxApplicationPayload,
    ) -> AdapterDryRunResult:
        hard_stops = list(fixture.hard_stop_markers)
        if session.isolation_mode is not BrowserIsolationMode.PER_INTENT_CONTEXT:
            hard_stops.append(HardStopCategory.SHARED_BROWSER_PROFILE)
        if session.action_intent_id != payload.action_intent_id:
            hard_stops.append(HardStopCategory.CROSS_INTENT_SESSION)
        if session.uses_real_credentials or session.can_submit_real_provider:
            hard_stops.append(HardStopCategory.REAL_CREDENTIAL)
        if session.host != fixture.allowed_host or payload.target_host != fixture.allowed_host:
            hard_stops.append(HardStopCategory.CROSS_SITE_NAVIGATION)
        unknown_fields = sorted(set(payload.fields) - fixture.allowed_field_names)
        if unknown_fields:
            hard_stops.append(HardStopCategory.UNKNOWN_FORM_FIELD)
        required_missing = sorted(
            field.name
            for field in fixture.allowed_fields
            if field.required and field.name not in payload.fields
        )
        classification = classify_site_automation_policy(fixture.site_policy_text)
        if classification.policy is not SiteAutomationPolicy.ALLOWED:
            return _stopped_result(
                hard_stops=tuple(dict.fromkeys(hard_stops)),
                reason_codes=(classification.reason_code,),
            )
        if required_missing:
            return _stopped_result(
                hard_stops=tuple(dict.fromkeys(hard_stops)),
                reason_codes=("REQUIRED_FIELDS_MISSING",),
            )
        if hard_stops:
            return _stopped_result(
                hard_stops=tuple(dict.fromkeys(hard_stops)),
                reason_codes=("HARD_STOP_DETECTED",),
            )
        return AdapterDryRunResult(
            outcome=AdapterDryRunOutcome.READY_FOR_REVIEW,
            would_submit_real_provider=False,
            field_plan=MappingProxyType(dict(payload.fields)),
            hard_stop_categories=(),
            reason_codes=("SANDBOX_DRY_RUN_READY_FOR_REVIEW",),
            synthetic_confirmation_selector=fixture.expected_confirmation_selector,
        )


def classify_site_automation_policy(text: str) -> SiteAutomationClassification:
    normalized = " ".join(text.lower().split())
    if any(marker in normalized for marker in _PROHIBITED_TERMS):
        return SiteAutomationClassification(
            policy=SiteAutomationPolicy.PROHIBITED,
            confidence=0.95,
            reason_code="SITE_AUTOMATION_PROHIBITED",
        )
    if any(marker in normalized for marker in _ALLOWED_TERMS):
        return SiteAutomationClassification(
            policy=SiteAutomationPolicy.ALLOWED,
            confidence=0.9,
            reason_code="SITE_AUTOMATION_ALLOWED",
        )
    return SiteAutomationClassification(
        policy=SiteAutomationPolicy.UNKNOWN,
        confidence=0.2,
        reason_code="SITE_AUTOMATION_UNKNOWN",
    )


def _qualification(
    status: AdapterQualificationStatus,
    site_classification: SiteAutomationClassification,
    hard_stops: tuple[HardStopCategory, ...],
    reason_codes: tuple[str, ...],
) -> AdapterQualification:
    return AdapterQualification(
        status=status,
        site_classification=site_classification,
        hard_stop_categories=hard_stops,
        reason_codes=reason_codes,
    )


def _stopped_result(
    *,
    hard_stops: tuple[HardStopCategory, ...],
    reason_codes: tuple[str, ...],
) -> AdapterDryRunResult:
    return AdapterDryRunResult(
        outcome=AdapterDryRunOutcome.STOPPED,
        would_submit_real_provider=False,
        field_plan=MappingProxyType({}),
        hard_stop_categories=hard_stops,
        reason_codes=reason_codes,
    )


def _validate_host(value: str, label: str) -> None:
    if not _HOST.fullmatch(value) or value.startswith(".") or value.endswith("."):
        raise ValueError(f"{label} must be a bounded hostname")


def _canonical_submission_payload_hash(
    *,
    action_intent_id: UUID,
    target_host: str,
    channel: str,
    fields: MappingProxyType[str, JsonValue],
    source_draft_payload_hash: str,
    approved_material_hashes: tuple[str, ...],
) -> str:
    try:
        encoded = json.dumps(
            {
                "version": "synthetic-submission-payload.v1",
                "action_intent_id": str(action_intent_id),
                "target_host": target_host,
                "channel": channel,
                "fields": _materialize_json_value(fields),
                "source_draft_payload_hash": source_draft_payload_hash,
                "approved_material_hashes": list(approved_material_hashes),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("sandbox payload fields must be JSON-canonicalizable") from error
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_hash(value: str, label: str) -> None:
    if not _HASH.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase sha256 hex digest")


def _freeze_json_value(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return cast(
            JsonValue,
            MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()}),
        )
    if isinstance(value, list):
        return cast(JsonValue, tuple(_freeze_json_value(item) for item in value))
    return value


def _materialize_json_value(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        materialized: dict[str, JsonValue] = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            materialized[key] = _materialize_json_value(item)
        return materialized
    if isinstance(value, (list, tuple)):
        sequence = cast("list[object] | tuple[object, ...]", value)
        return [_materialize_json_value(item) for item in sequence]
    if value is None or isinstance(value, str | int | float | bool):
        return cast(JsonValue, value)
    raise ValueError("sandbox payload fields must be JSON-canonicalizable")


def _validate_field(value: str, label: str) -> None:
    if not _FIELD.fullmatch(value):
        raise ValueError(f"{label} must be a bounded form field identifier")


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")


__all__ = [
    "AdapterDryRunOutcome",
    "AdapterDryRunResult",
    "AdapterQualification",
    "AdapterQualificationStatus",
    "ApplicationAdapterContract",
    "BrowserIsolationMode",
    "FormFieldSpec",
    "HardStopCategory",
    "SandboxApplicationPayload",
    "SandboxBrowserSession",
    "SiteAutomationClassification",
    "SyntheticApplicationAdapter",
    "SyntheticApplicationFixture",
    "classify_site_automation_policy",
]
