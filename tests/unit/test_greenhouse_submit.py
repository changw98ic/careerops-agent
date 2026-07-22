from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy

import pytest

from careerops.application.greenhouse_submit import (
    GREENHOUSE_BOARD_API_HOST,
    GreenhouseAttachmentRef,
    GreenhouseHardStopCategory,
    GreenhouseJobSchemaSnapshot,
    GreenhouseSchemaError,
    GreenhouseSubmissionPayload,
    GreenhouseTarget,
    qualify_greenhouse_submission,
)

TARGET = GreenhouseTarget(board_token="acme", job_id=12345)
RESUME_BYTES = b"reviewed resume bytes"
RESUME_HASH = hashlib.sha256(RESUME_BYTES).hexdigest()


def schema_document() -> dict[str, object]:
    return {
        "id": TARGET.job_id,
        "internal_job_id": 777,
        "title": "Senior Engineer",
        "company_name": "Acme",
        "updated_at": "2026-07-20T12:00:00Z",
        "application_deadline": "2026-08-20T12:00:00Z",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
        "questions": [
            {
                "label": "First Name",
                "required": True,
                "fields": [{"name": "first_name", "type": "input_text"}],
            },
            {
                "label": "Last Name",
                "required": True,
                "fields": [{"name": "last_name", "type": "input_text"}],
            },
            {
                "label": "Email",
                "required": True,
                "fields": [{"name": "email", "type": "input_text"}],
            },
            {
                "label": "Phone",
                "required": False,
                "fields": [{"name": "phone", "type": "input_text"}],
            },
            {
                "label": "Resume",
                "required": True,
                "fields": [
                    {"name": "resume", "type": "input_file"},
                    {"name": "resume_text", "type": "textarea"},
                ],
            },
        ],
        "location_questions": [],
        "compliance": [],
        "data_compliance": [],
    }


def snapshot(document: dict[str, object] | None = None) -> GreenhouseJobSchemaSnapshot:
    raw = json.dumps(document or schema_document(), sort_keys=True).encode("utf-8")
    return GreenhouseJobSchemaSnapshot.from_json_bytes(target=TARGET, raw_response=raw)


def attachment_ref() -> GreenhouseAttachmentRef:
    return GreenhouseAttachmentRef(
        field_name="resume",
        object_key="approved:resume:v1",
        filename="resume.pdf",
        content_type="application/pdf",
        size_bytes=len(RESUME_BYTES),
        sha256=RESUME_HASH,
    )


def submission_payload(
    schema: GreenhouseJobSchemaSnapshot,
    *,
    fields: Mapping[str, object] | None = None,
    source_hash: str = "a" * 64,
    material_hashes: tuple[str, ...] = (RESUME_HASH, "b" * 64),
) -> GreenhouseSubmissionPayload:
    return GreenhouseSubmissionPayload(
        target=TARGET,
        schema_hash=schema.schema_hash,
        fields=fields
        or {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "phone": "+1 555 0100",
        },
        source_draft_payload_hash=source_hash,
        approved_material_hashes=material_hashes,
        attachment_refs=(attachment_ref(),),
    )


@pytest.mark.parametrize(
    "board_token",
    ["internal", "Acme", "acme/jobs", "acme%2fjobs", "acme%252fjobs", "acme.example", " acme"],
)
def test_target_rejects_internal_dynamic_or_noncanonical_board_tokens(board_token: str) -> None:
    with pytest.raises(ValueError, match="canonical public"):
        GreenhouseTarget(board_token=board_token, job_id=123)


@pytest.mark.parametrize("job_id", [0, -1, True, 10_000_000_000_000_000])
def test_target_rejects_nonpositive_or_unbounded_job_ids(job_id: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        GreenhouseTarget(board_token="acme", job_id=job_id)


def test_target_is_exactly_bound_to_fixed_greenhouse_origin() -> None:
    assert TARGET.canonical() == {
        "host": GREENHOUSE_BOARD_API_HOST,
        "board_token": "acme",
        "job_id": 12345,
    }
    assert TARGET.schema_endpoint == (
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/12345?questions=true"
    )


def test_schema_snapshot_binds_raw_response_and_complete_job_identity() -> None:
    document = schema_document()
    compact = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    pretty = json.dumps(document, sort_keys=True, indent=2).encode("utf-8")

    first = GreenhouseJobSchemaSnapshot.from_json_bytes(target=TARGET, raw_response=compact)
    second = GreenhouseJobSchemaSnapshot.from_json_bytes(target=TARGET, raw_response=pretty)

    assert first.raw_response_sha256 == hashlib.sha256(compact).hexdigest()
    assert first.schema_hash != second.schema_hash
    assert first.internal_job_id == 777
    assert first.updated_at == "2026-07-20T12:00:00Z"
    assert first.application_deadline == "2026-08-20T12:00:00Z"
    assert first.title == "Senior Engineer"
    assert first.company_name == "Acme"

    prospect = deepcopy(document)
    prospect["internal_job_id"] = None
    with pytest.raises(GreenhouseSchemaError, match="prospect"):
        snapshot(prospect)


def test_safe_core_schema_and_reviewed_file_payload_qualify() -> None:
    item_schema = snapshot()
    payload = submission_payload(item_schema)

    qualification = qualify_greenhouse_submission(schema=item_schema, payload=payload)

    assert item_schema.can_submit is True
    assert "resume_text" not in item_schema.allowed_field_names
    assert qualification.can_submit is True
    assert qualification.reason_codes == ("GREENHOUSE_SUBMISSION_QUALIFIED",)


def test_required_question_group_must_be_satisfied_by_submittable_reviewed_field() -> None:
    item_schema = snapshot()
    payload = GreenhouseSubmissionPayload(
        target=TARGET,
        schema_hash=item_schema.schema_hash,
        fields={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
        },
        source_draft_payload_hash="a" * 64,
        approved_material_hashes=("b" * 64,),
    )

    qualification = qualify_greenhouse_submission(schema=item_schema, payload=payload)

    assert qualification.can_submit is False
    assert "GREENHOUSE_REQUIRED_FIELD_MISSING" in qualification.reason_codes


@pytest.mark.parametrize(
    ("field_name", "label", "category"),
    [
        ("question_1", "What is your favorite editor?", GreenhouseHardStopCategory.UNKNOWN_FIELD),
        ("location", "Location", GreenhouseHardStopCategory.LOCATION),
        ("website", "Website", GreenhouseHardStopCategory.UNKNOWN_FIELD),
        ("resume_url", "Resume URL", GreenhouseHardStopCategory.UNKNOWN_FIELD),
        ("question_1_url", "Upload URL", GreenhouseHardStopCategory.UNKNOWN_FIELD),
        ("question_2", "Are you legally authorized?", GreenhouseHardStopCategory.LEGAL_ATTESTATION),
        ("question_3", "Will you require sponsorship?", GreenhouseHardStopCategory.SPONSORSHIP),
        ("question_4", "Complete this assessment", GreenhouseHardStopCategory.ASSESSMENT),
        ("question_5", "Provide a credit card", GreenhouseHardStopCategory.PAYMENT),
        ("question_6", "Passport number", GreenhouseHardStopCategory.IDENTITY),
        ("question_7", "Complete CAPTCHA", GreenhouseHardStopCategory.CAPTCHA),
        ("question_8", "Enter your two factor code", GreenhouseHardStopCategory.MFA),
        ("question_9", "Sign in to continue", GreenhouseHardStopCategory.LOGIN),
    ],
)
def test_custom_location_url_and_sensitive_schema_fields_are_hard_stops(
    field_name: str,
    label: str,
    category: GreenhouseHardStopCategory,
) -> None:
    document = schema_document()
    questions = document["questions"]
    assert isinstance(questions, list)
    questions.append(
        {
            "label": label,
            "required": False,
            "fields": [{"name": field_name, "type": "input_text"}],
        }
    )

    item_schema = snapshot(document)

    assert item_schema.can_submit is False
    assert category in item_schema.hard_stop_categories


@pytest.mark.parametrize(
    ("root_field", "value", "category"),
    [
        ("compliance", [{"label": "Veteran"}], GreenhouseHardStopCategory.COMPLIANCE_EEO),
        (
            "demographic_questions",
            {"questions": [{"label": "Race"}]},
            GreenhouseHardStopCategory.COMPLIANCE_EEO,
        ),
        (
            "data_compliance",
            [{"requires_consent": True}],
            GreenhouseHardStopCategory.LEGAL_ATTESTATION,
        ),
    ],
)
def test_complete_compliance_structures_are_bound_and_hard_stopped(
    root_field: str,
    value: object,
    category: GreenhouseHardStopCategory,
) -> None:
    document = schema_document()
    document[root_field] = value

    item_schema = snapshot(document)

    assert category in item_schema.hard_stop_categories
    assert item_schema.can_submit is False


def test_payload_rejects_unreviewed_field_and_text_attachment_alternative() -> None:
    item_schema = snapshot()
    fields = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "resume_text": "unreviewed alternate material",
    }
    payload = submission_payload(item_schema, fields=fields)

    qualification = qualify_greenhouse_submission(schema=item_schema, payload=payload)

    assert qualification.can_submit is False
    assert "GREENHOUSE_UNKNOWN_FIELD" in qualification.reason_codes


def test_payload_schema_material_and_submission_hashes_are_canonical_and_independent() -> None:
    item_schema = snapshot()
    first = submission_payload(item_schema)
    same = submission_payload(item_schema)
    changed_material = submission_payload(item_schema, material_hashes=(RESUME_HASH, "c" * 64))
    changed_fields = submission_payload(
        item_schema,
        fields={
            "first_name": "Grace",
            "last_name": "Lovelace",
            "email": "ada@example.com",
        },
    )

    assert first.payload_hash == same.payload_hash
    assert first.material_hash == same.material_hash
    assert first.submission_identity == same.submission_identity
    assert first.material_hash != changed_material.material_hash
    assert first.submission_identity != changed_material.submission_identity
    assert first.payload_hash != changed_fields.payload_hash
    assert first.submission_identity != changed_fields.submission_identity


def test_attachment_ref_is_local_bounded_and_content_addressed() -> None:
    ref = attachment_ref()

    assert ref.object_key == "approved:resume:v1"
    assert ref.sha256 == RESUME_HASH

    with pytest.raises(ValueError, match="resume and cover_letter"):
        GreenhouseAttachmentRef(
            field_name="resume_url",
            object_key="https://example.com/resume.pdf",
            filename="resume.pdf",
            content_type="application/pdf",
            size_bytes=1,
            sha256="a" * 64,
        )

    with pytest.raises(ValueError, match="path"):
        GreenhouseAttachmentRef(
            field_name="resume",
            object_key="approved:resume:v1",
            filename="../resume.pdf",
            content_type="application/pdf",
            size_bytes=1,
            sha256="a" * 64,
        )
