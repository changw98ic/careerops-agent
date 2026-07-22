from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GreenhouseSubmitAcceptanceCase:
    case_id: str
    category: str
    expected_state: str
    retry_allowed: bool
    reconciliation_required: bool
    required_reasons: tuple[str, ...]


ACCEPTANCE_MATRIX: tuple[GreenhouseSubmitAcceptanceCase, ...] = (
    GreenhouseSubmitAcceptanceCase(
        case_id="destination-host-board-job-bound",
        category="destination_binding",
        expected_state="ready",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("DESTINATION_BOUND_TO_REVIEWED_BOARD_AND_JOB",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="destination-host-mismatch-stops",
        category="destination_binding",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("DESTINATION_HOST_MISMATCH",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="destination-board-mismatch-stops",
        category="destination_binding",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("DESTINATION_BOARD_TOKEN_MISMATCH",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="destination-job-mismatch-stops",
        category="destination_binding",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("DESTINATION_JOB_ID_MISMATCH",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="fixed-origin-no-redirect-or-proxy",
        category="transport_origin",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("GREENHOUSE_REDIRECT_OR_PROXY_BLOCKED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="schema-hash-toctou-stops",
        category="schema_hash_toctou",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("FORM_SCHEMA_HASH_CHANGED_AFTER_REVIEW",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="raw-full-schema-hash-binds-internal-job-and-updated-at",
        category="schema_hash_toctou",
        expected_state="ready",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("RAW_SCHEMA_IDENTITY_BOUND",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="allowlisted-required-fields-ready",
        category="allowlisted_fields",
        expected_state="ready",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("ALL_REQUIRED_ALLOWLISTED_FIELDS_PRESENT",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="question-field-default-stops",
        category="hard_stops",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("QUESTION_FIELD_NOT_ALLOWLISTED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="location-field-default-stops",
        category="hard_stops",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("LOCATION_FIELD_REQUESTED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="website-field-default-stops",
        category="hard_stops",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("UNKNOWN_FORM_FIELD",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="missing-required-field-stops",
        category="allowlisted_fields",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("REQUIRED_FIELD_MISSING",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="sensitive-field-stops",
        category="hard_stops",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("SENSITIVE_FIELD_REQUESTED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="legal-attestation-stops",
        category="hard_stops",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("LEGAL_ATTESTATION_REQUIRED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="eeo-demographic-field-stops",
        category="hard_stops",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("EEO_DEMOGRAPHIC_FIELD_REQUESTED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="assessment-field-stops",
        category="hard_stops",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("ASSESSMENT_OR_SCREENING_FIELD_REQUESTED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="captcha-stops",
        category="hard_stops",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("CAPTCHA_REQUIRED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="mfa-stops",
        category="hard_stops",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("MFA_REQUIRED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="login-stops",
        category="hard_stops",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("LOGIN_REQUIRED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="opaque-broker-binding-required",
        category="credential_binding",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("OPAQUE_BROKER_BINDING_REQUIRED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="credential-scope-mismatch-stops",
        category="credential_binding",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("CREDENTIAL_SCOPE_MISMATCH",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="credential-owner-employer-board-profile-mismatch-stops",
        category="credential_binding",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("CREDENTIAL_BINDING_MISMATCH",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="credential-expired-or-inactive-stops",
        category="credential_binding",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("CREDENTIAL_PROFILE_UNUSABLE",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="release-qualification-required",
        category="release_qualification",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("RELEASE_QUALIFICATION_REQUIRED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="release-qualification-expired-stops",
        category="release_qualification",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("RELEASE_QUALIFICATION_EXPIRED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="review-exact-payload-binding-required",
        category="review_grant_binding",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("REVIEW_PAYLOAD_HASH_MISMATCH",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="each-material-sha-exact-authorization-required",
        category="review_grant_binding",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("GRANT_MATERIAL_HASH_MISMATCH",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="attachment-rehash-mismatch-stops",
        category="review_grant_binding",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("ATTACHMENT_SHA256_MISMATCH",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="cap-slot-exhausted-stops",
        category="caps_kill_revocation_locking",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("AUTOPILOT_CAP_EXHAUSTED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="global-kill-switch-stops-before-reservation",
        category="caps_kill_revocation_locking",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("GLOBAL_KILL_SWITCH_ACTIVE",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="provider-kill-switch-stops-before-reservation",
        category="caps_kill_revocation_locking",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("PROVIDER_KILL_SWITCH_ACTIVE",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="revoked-grant-stops-before-reservation",
        category="caps_kill_revocation_locking",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("GRANT_REVOKED",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="idempotent-replay-returns-original-reservation",
        category="idempotency",
        expected_state="replayed",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("IDEMPOTENT_REPLAY",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="same-key-different-payload-stops",
        category="idempotency",
        expected_state="stopped",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("IDEMPOTENCY_PAYLOAD_MISMATCH",),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="job-board-post-2xx-is-accepted-unverified",
        category="provider_result_semantics",
        expected_state="accepted_unverified",
        retry_allowed=False,
        reconciliation_required=True,
        required_reasons=("JOB_BOARD_POST_ACCEPTED_UNVERIFIED", "RECONCILIATION_REQUIRED"),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="timeout-reset-3xx-5xx-unparseable-or-oversized-2xx-is-ambiguous",
        category="provider_result_semantics",
        expected_state="ambiguous",
        retry_allowed=False,
        reconciliation_required=True,
        required_reasons=("PROVIDER_RESULT_AMBIGUOUS", "BLIND_RETRY_BLOCKED"),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="receipt-persistence-failure-is-ambiguous",
        category="provider_result_semantics",
        expected_state="ambiguous",
        retry_allowed=False,
        reconciliation_required=True,
        required_reasons=("RECEIPT_PERSISTENCE_AMBIGUOUS", "BLIND_RETRY_BLOCKED"),
    ),
    GreenhouseSubmitAcceptanceCase(
        case_id="manual-reviewed-employer-evidence-confirms",
        category="receipt_reconciliation",
        expected_state="confirmed",
        retry_allowed=False,
        reconciliation_required=False,
        required_reasons=("MANUAL_REVIEWED_EMPLOYER_EVIDENCE_CONFIRMED",),
    ),
)


REQUIRED_CATEGORIES = frozenset(
    {
        "destination_binding",
        "transport_origin",
        "schema_hash_toctou",
        "allowlisted_fields",
        "hard_stops",
        "credential_binding",
        "release_qualification",
        "review_grant_binding",
        "caps_kill_revocation_locking",
        "idempotency",
        "provider_result_semantics",
        "receipt_reconciliation",
    }
)


def test_acceptance_matrix_covers_every_greenhouse_submit_risk_category() -> None:
    categories = {case.category for case in ACCEPTANCE_MATRIX}

    assert categories == REQUIRED_CATEGORIES


def test_acceptance_matrix_case_ids_are_unique_and_descriptive() -> None:
    case_ids = [case.case_id for case in ACCEPTANCE_MATRIX]

    assert len(case_ids) == len(set(case_ids))
    assert all(case.case_id == case.case_id.lower() for case in ACCEPTANCE_MATRIX)
    assert all("_" not in case.case_id for case in ACCEPTANCE_MATRIX)


def test_acceptance_matrix_never_treats_provider_post_as_confirmed() -> None:
    provider_cases = [
        case for case in ACCEPTANCE_MATRIX if case.category == "provider_result_semantics"
    ]

    assert provider_cases
    assert all(case.expected_state != "confirmed" for case in provider_cases)
    assert all(case.retry_allowed is False for case in provider_cases)
    assert all(case.reconciliation_required for case in provider_cases)


def test_acceptance_matrix_confirms_only_from_manual_reviewed_employer_evidence() -> None:
    confirmed_cases = [case for case in ACCEPTANCE_MATRIX if case.expected_state == "confirmed"]

    assert confirmed_cases == [
        GreenhouseSubmitAcceptanceCase(
            case_id="manual-reviewed-employer-evidence-confirms",
            category="receipt_reconciliation",
            expected_state="confirmed",
            retry_allowed=False,
            reconciliation_required=False,
            required_reasons=("MANUAL_REVIEWED_EMPLOYER_EVIDENCE_CONFIRMED",),
        )
    ]


def test_acceptance_matrix_has_positive_ready_and_fail_closed_cases() -> None:
    states = {case.expected_state for case in ACCEPTANCE_MATRIX}
    stopped_cases = [case for case in ACCEPTANCE_MATRIX if case.expected_state == "stopped"]

    assert "ready" in states
    assert "accepted_unverified" in states
    assert "ambiguous" in states
    assert stopped_cases
    assert all(case.required_reasons for case in stopped_cases)
