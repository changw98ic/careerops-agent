from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, TypeGuard, cast

GREENHOUSE_ACTION_KIND: Final = "submit_application"
GREENHOUSE_ADAPTER_ID: Final = "greenhouse-job-board"
GREENHOUSE_BOARD_API_HOST: Final = "boards-api.greenhouse.io"
GREENHOUSE_BOARD_API_ORIGIN: Final = f"https://{GREENHOUSE_BOARD_API_HOST}"
GREENHOUSE_CHANNEL: Final = "greenhouse:job-board"
GREENHOUSE_PAYLOAD_VERSION: Final = "greenhouse-submit-payload.v1"
GREENHOUSE_SCHEMA_VERSION: Final = "greenhouse-job-schema.v1"
GREENHOUSE_SUBMISSION_IDENTITY_VERSION: Final = "greenhouse-submission-identity.v1"

_BOARD_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_CORE_FIELD_NAME = re.compile(r"^(?:first_name|last_name|email|phone|resume|cover_letter)$")
_PROVIDER_ATTACHMENT_ALTERNATIVES: Final = frozenset({"resume_text", "cover_letter_text"})
_HASH = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_KEY = re.compile(r"^[A-Za-z0-9._:@/-]{1,256}$")
_EMAIL = re.compile(r"^[^@\s]{1,160}@[^@\s]{1,160}\.[^@\s]{2,40}$")
_URL_IN_TEXT = re.compile(r"https?://|www\.", re.IGNORECASE)
_MAX_JOB_ID: Final = 9_999_999_999_999_999
_MAX_FIELDS: Final = 64
_MAX_FIELD_TEXT_CHARS: Final = 50_000
_MAX_TOTAL_FIELD_TEXT_CHARS: Final = 250_000
_MAX_SCHEMA_RESPONSE_BYTES: Final = 4 * 1024 * 1024
_MAX_ATTACHMENTS: Final = 2
_MAX_ATTACHMENT_BYTES: Final = 10 * 1024 * 1024
_MAX_TOTAL_ATTACHMENT_BYTES: Final = 20 * 1024 * 1024
_SUPPORTED_ATTACHMENT_TYPES: Final = MappingProxyType(
    {
        ".pdf": frozenset({"application/pdf"}),
        ".doc": frozenset({"application/msword"}),
        ".docx": frozenset(
            {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
        ),
        ".txt": frozenset({"text/plain"}),
        ".rtf": frozenset({"application/rtf", "text/rtf"}),
    }
)
_STANDARD_REQUIRED_FIELDS: Final = frozenset({"first_name", "last_name", "email"})
_SAFE_FILE_FIELDS: Final = frozenset({"resume", "cover_letter"})
_LOCATION_FIELDS: Final = frozenset(
    {"location", "latitude", "longitude", "country_short_name", "applicant_ip"}
)
_PROHIBITED_ROOT_SECTIONS: Final = MappingProxyType(
    {
        "compliance": "compliance_eeo",
        "data_compliance": "legal_attestation",
        "demographic_questions": "compliance_eeo",
    }
)
_PROHIBITED_MARKERS: Final = MappingProxyType(
    {
        "compliance_eeo": (
            "eeo",
            "eeoc",
            "equal employment",
            "equal opportunity",
            "race",
            "ethnicity",
            "gender",
            "sexual orientation",
            "disability",
            "disabled",
            "veteran",
            "religion",
            "age",
            "pronouns",
        ),
        "legal_attestation": (
            "legally authorized",
            "authorization to work",
            "work authorization",
            "certify",
            "certification",
            "attest",
            "attestation",
            "electronic signature",
            "consent",
            "terms and conditions",
            "privacy policy",
        ),
        "sponsorship": (
            "sponsor",
            "sponsorship",
            "work visa",
            "visa status",
            "immigration status",
        ),
        "assessment": (
            "assessment",
            "take home",
            "take-home",
            "coding challenge",
            "skills test",
            "test task",
            "quiz",
            "examination",
        ),
        "payment": (
            "payment",
            "application fee",
            "credit card",
            "debit card",
            "bank account",
            "routing number",
        ),
        "identity": (
            "social security",
            "national id",
            "government id",
            "passport",
            "driver's license",
            "drivers license",
            "date of birth",
            "birth date",
            "identity document",
            "photo id",
        ),
        "captcha": ("captcha", "recaptcha", "human verification"),
        "mfa": (
            "multi-factor",
            "multi factor",
            "multifactor",
            "two-factor",
            "two factor",
            "2fa",
            "one-time passcode",
            "one time passcode",
            "otp code",
        ),
        "login": (
            "log in",
            "login",
            "sign in",
            "password",
            "create an account",
            "account credentials",
        ),
    }
)


class GreenhouseSchemaError(ValueError):
    """Provider schema could not be bounded or tied to the requested job."""


class GreenhouseFieldKind(StrEnum):
    INPUT_FILE = "input_file"
    INPUT_HIDDEN = "input_hidden"
    INPUT_TEXT = "input_text"
    TEXTAREA = "textarea"
    SINGLE_SELECT = "multi_value_single_select"
    MULTI_SELECT = "multi_value_multi_select"


_EXPECTED_FIELD_KINDS: Final = MappingProxyType(
    {
        "first_name": GreenhouseFieldKind.INPUT_TEXT,
        "last_name": GreenhouseFieldKind.INPUT_TEXT,
        "email": GreenhouseFieldKind.INPUT_TEXT,
        "phone": GreenhouseFieldKind.INPUT_TEXT,
        "resume": GreenhouseFieldKind.INPUT_FILE,
        "cover_letter": GreenhouseFieldKind.INPUT_FILE,
        "resume_text": GreenhouseFieldKind.TEXTAREA,
        "cover_letter_text": GreenhouseFieldKind.TEXTAREA,
    }
)


class GreenhouseHardStopCategory(StrEnum):
    COMPLIANCE_EEO = "compliance_eeo"
    LEGAL_ATTESTATION = "legal_attestation"
    SPONSORSHIP = "sponsorship"
    ASSESSMENT = "assessment"
    PAYMENT = "payment"
    IDENTITY = "identity"
    CAPTCHA = "captcha"
    MFA = "mfa"
    LOGIN = "login"
    LOCATION = "location"
    UNKNOWN_FIELD = "unknown_field"


@dataclass(frozen=True, slots=True)
class GreenhouseTarget:
    board_token: str
    job_id: int
    target_hash: str = field(init=False)

    def __post_init__(self) -> None:
        # ``internal`` is Greenhouse's reserved board token, not password material.
        if _BOARD_TOKEN.fullmatch(self.board_token) is None or self.board_token == "internal":  # nosec B105
            raise ValueError("board_token must be a canonical public Greenhouse board token")
        if isinstance(self.job_id, bool) or not 0 < self.job_id <= _MAX_JOB_ID:
            raise ValueError("job_id must be a bounded positive integer")
        object.__setattr__(self, "target_hash", _canonical_hash(self.canonical()))

    @property
    def endpoint(self) -> str:
        return f"{GREENHOUSE_BOARD_API_ORIGIN}/v1/boards/{self.board_token}/jobs/{self.job_id}"

    @property
    def schema_endpoint(self) -> str:
        return f"{self.endpoint}?questions=true"

    def canonical(self) -> Mapping[str, object]:
        return {
            "host": GREENHOUSE_BOARD_API_HOST,
            "board_token": self.board_token,
            "job_id": self.job_id,
        }


@dataclass(frozen=True, slots=True)
class GreenhouseFieldSpec:
    name: str
    kind: GreenhouseFieldKind
    option_values: tuple[str | int, ...] = ()

    def canonical(self) -> Mapping[str, object]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "option_values": list(self.option_values),
        }


@dataclass(frozen=True, slots=True)
class GreenhouseQuestionSpec:
    label: str
    required: bool
    fields: tuple[GreenhouseFieldSpec, ...]

    def canonical(self) -> Mapping[str, object]:
        return {
            "label": self.label,
            "required": self.required,
            "fields": [item.canonical() for item in self.fields],
        }


@dataclass(frozen=True, slots=True)
class GreenhouseJobSchemaSnapshot:
    target: GreenhouseTarget
    internal_job_id: int
    title: str
    company_name: str
    updated_at: str
    application_deadline: str | None
    absolute_url: str
    raw_response_sha256: str
    questions: tuple[GreenhouseQuestionSpec, ...]
    hard_stop_categories: tuple[GreenhouseHardStopCategory, ...]
    schema_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.questions:
            raise GreenhouseSchemaError("Greenhouse job schema must contain questions")
        if isinstance(self.internal_job_id, bool) or not 0 < self.internal_job_id <= _MAX_JOB_ID:
            raise GreenhouseSchemaError(
                "Greenhouse prospect or invalid internal job is not eligible"
            )
        _validate_bounded_schema_text(self.title, "title", max_chars=500)
        _validate_bounded_schema_text(self.company_name, "company_name", max_chars=500)
        _validate_bounded_schema_text(self.updated_at, "updated_at", max_chars=80)
        _validate_iso_datetime(self.updated_at, "updated_at")
        if self.application_deadline is not None:
            _validate_bounded_schema_text(
                self.application_deadline,
                "application_deadline",
                max_chars=80,
            )
            _validate_iso_datetime(self.application_deadline, "application_deadline")
        _validate_bounded_schema_text(self.absolute_url, "absolute_url", max_chars=2048)
        _validate_hash(self.raw_response_sha256, "raw_response_sha256")
        object.__setattr__(self, "questions", tuple(self.questions))
        object.__setattr__(self, "hard_stop_categories", tuple(self.hard_stop_categories))
        object.__setattr__(self, "schema_hash", _canonical_hash(self.canonical()))

    @classmethod
    def from_mapping(
        cls,
        *,
        target: GreenhouseTarget,
        payload: Mapping[str, object],
        raw_response_sha256: str,
    ) -> GreenhouseJobSchemaSnapshot:
        _validate_hash(raw_response_sha256, "raw_response_sha256")
        job_id = payload.get("id")
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id != target.job_id:
            raise GreenhouseSchemaError("Greenhouse schema job id does not match target")
        internal_job_id = payload.get("internal_job_id")
        if (
            isinstance(internal_job_id, bool)
            or not isinstance(internal_job_id, int)
            or not 0 < internal_job_id <= _MAX_JOB_ID
        ):
            raise GreenhouseSchemaError(
                "Greenhouse prospect or invalid internal job is not eligible"
            )
        title = _required_schema_str(payload, "title", max_chars=500)
        company_name = _required_schema_str(payload, "company_name", max_chars=500)
        updated_at = _required_schema_str(payload, "updated_at", max_chars=80)
        _validate_iso_datetime(updated_at, "updated_at")
        raw_deadline = payload.get("application_deadline")
        if raw_deadline is not None and not isinstance(raw_deadline, str):
            raise GreenhouseSchemaError("application_deadline must be a string or null")
        application_deadline = raw_deadline.strip() if isinstance(raw_deadline, str) else None
        if application_deadline is not None:
            _validate_bounded_schema_text(
                application_deadline,
                "application_deadline",
                max_chars=80,
            )
            _validate_iso_datetime(application_deadline, "application_deadline")
        absolute_url = _required_schema_str(payload, "absolute_url", max_chars=2048)
        raw_questions = payload.get("questions")
        if not _is_object_sequence(raw_questions):
            raise GreenhouseSchemaError("Greenhouse schema questions must be an array")
        raw_location_questions = payload.get("location_questions", ())
        if not _is_object_sequence(raw_location_questions):
            raise GreenhouseSchemaError("Greenhouse location_questions must be an array")

        questions: list[GreenhouseQuestionSpec] = []
        hard_stops: list[GreenhouseHardStopCategory] = []
        seen_fields: dict[str, GreenhouseFieldSpec] = {}
        for raw_question in (*raw_questions, *raw_location_questions):
            question, question_stops = _parse_question(raw_question)
            questions.append(question)
            hard_stops.extend(question_stops)
            for field_spec in question.fields:
                previous = seen_fields.get(field_spec.name)
                if previous is not None and previous != field_spec:
                    hard_stops.append(GreenhouseHardStopCategory.UNKNOWN_FIELD)
                else:
                    seen_fields[field_spec.name] = field_spec

        if raw_location_questions:
            hard_stops.append(GreenhouseHardStopCategory.LOCATION)

        for section, category in _PROHIBITED_ROOT_SECTIONS.items():
            value = payload.get(section)
            if value not in (None, (), [], {}):
                hard_stops.append(GreenhouseHardStopCategory(category))
        if not _STANDARD_REQUIRED_FIELDS.issubset(seen_fields):
            hard_stops.append(GreenhouseHardStopCategory.UNKNOWN_FIELD)

        return cls(
            target=target,
            internal_job_id=internal_job_id,
            title=title,
            company_name=company_name,
            updated_at=updated_at,
            application_deadline=application_deadline,
            absolute_url=absolute_url,
            raw_response_sha256=raw_response_sha256,
            questions=tuple(questions),
            hard_stop_categories=_unique_hard_stops(hard_stops),
        )

    @classmethod
    def from_json_bytes(
        cls,
        *,
        target: GreenhouseTarget,
        raw_response: bytes,
    ) -> GreenhouseJobSchemaSnapshot:
        if not raw_response or len(raw_response) > _MAX_SCHEMA_RESPONSE_BYTES:
            raise GreenhouseSchemaError("Greenhouse schema response bytes must be bounded")
        try:
            decoded = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GreenhouseSchemaError("Greenhouse schema response must be valid json") from exc
        if not isinstance(decoded, dict):
            raise GreenhouseSchemaError("Greenhouse schema response must be an object")
        return cls.from_mapping(
            target=target,
            payload=cast("Mapping[str, object]", decoded),
            raw_response_sha256=hashlib.sha256(raw_response).hexdigest(),
        )

    @property
    def can_submit(self) -> bool:
        return not self.hard_stop_categories

    @property
    def allowed_field_names(self) -> frozenset[str]:
        return frozenset(
            field.name
            for question in self.questions
            for field in question.fields
            if _CORE_FIELD_NAME.fullmatch(field.name) is not None
        )

    def field_spec(self, field_name: str) -> GreenhouseFieldSpec | None:
        for question in self.questions:
            for field_spec in question.fields:
                if field_spec.name == field_name:
                    return field_spec
        return None

    def canonical(self) -> Mapping[str, object]:
        return {
            "version": GREENHOUSE_SCHEMA_VERSION,
            "target": self.target.canonical(),
            "internal_job_id": self.internal_job_id,
            "title": self.title,
            "company_name": self.company_name,
            "updated_at": self.updated_at,
            "application_deadline": self.application_deadline,
            "absolute_url": self.absolute_url,
            "raw_response_sha256": self.raw_response_sha256,
            "questions": [question.canonical() for question in self.questions],
            "hard_stop_categories": [item.value for item in self.hard_stop_categories],
        }


@dataclass(frozen=True, slots=True)
class GreenhouseAttachmentRef:
    field_name: str
    object_key: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if self.field_name not in _SAFE_FILE_FIELDS:
            raise ValueError("only resume and cover_letter attachments are supported")
        if _OBJECT_KEY.fullmatch(self.object_key) is None:
            raise ValueError("attachment object_key must be a bounded opaque reference")
        if not self.filename or len(self.filename) > 180:
            raise ValueError("attachment filename must be bounded")
        if "/" in self.filename or "\\" in self.filename or "\x00" in self.filename:
            raise ValueError("attachment filename must not contain a path")
        extension = _filename_extension(self.filename)
        allowed_content_types = _SUPPORTED_ATTACHMENT_TYPES.get(extension)
        if allowed_content_types is None or self.content_type not in allowed_content_types:
            raise ValueError("attachment type must be an approved Greenhouse document type")
        if isinstance(self.size_bytes, bool) or not 0 < self.size_bytes <= _MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment size exceeds the Greenhouse boundary")
        _validate_hash(self.sha256, "attachment sha256")

    def canonical(self) -> Mapping[str, object]:
        return {
            "field_name": self.field_name,
            "object_key": self.object_key,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class GreenhouseSubmissionPayload:
    target: GreenhouseTarget
    schema_hash: str
    fields: Mapping[str, object]
    source_draft_payload_hash: str
    approved_material_hashes: tuple[str, ...]
    attachment_refs: tuple[GreenhouseAttachmentRef, ...] = ()
    material_hash: str = field(init=False)
    payload_hash: str = field(init=False)
    submission_identity: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_hash(self.schema_hash, "schema_hash")
        _validate_hash(self.source_draft_payload_hash, "source_draft_payload_hash")
        fields = _normalize_payload_fields(self.fields)
        attachment_refs = tuple(sorted(self.attachment_refs, key=lambda item: item.field_name))
        if len(attachment_refs) > _MAX_ATTACHMENTS:
            raise ValueError("too many Greenhouse attachments")
        if len({item.field_name for item in attachment_refs}) != len(attachment_refs):
            raise ValueError("Greenhouse attachment fields must not repeat")
        if sum(item.size_bytes for item in attachment_refs) > _MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("Greenhouse attachments exceed the total size boundary")
        material_hashes = tuple(sorted(set(self.approved_material_hashes)))
        if not material_hashes:
            raise ValueError("approved_material_hashes must not be empty")
        if len(material_hashes) != len(self.approved_material_hashes):
            raise ValueError("approved_material_hashes must not contain duplicates")
        for item in material_hashes:
            _validate_hash(item, "approved material hash")

        material_hash = _canonical_hash(
            {
                "version": "greenhouse-approved-material.v1",
                "source_draft_payload_hash": self.source_draft_payload_hash,
                "approved_material_hashes": list(material_hashes),
                "attachment_sha256": [item.sha256 for item in attachment_refs],
            }
        )
        payload_hash = _canonical_hash(
            {
                "version": GREENHOUSE_PAYLOAD_VERSION,
                "target": self.target.canonical(),
                "schema_hash": self.schema_hash,
                "fields": _canonicalize(fields),
                "attachment_refs": [item.canonical() for item in attachment_refs],
                "material_hash": material_hash,
            }
        )
        submission_identity = _canonical_hash(
            {
                "version": GREENHOUSE_SUBMISSION_IDENTITY_VERSION,
                "target_hash": self.target.target_hash,
                "schema_hash": self.schema_hash,
                "payload_hash": payload_hash,
                "material_hash": material_hash,
            }
        )
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "attachment_refs", attachment_refs)
        object.__setattr__(self, "approved_material_hashes", material_hashes)
        object.__setattr__(self, "material_hash", material_hash)
        object.__setattr__(self, "payload_hash", payload_hash)
        object.__setattr__(self, "submission_identity", submission_identity)


@dataclass(frozen=True, slots=True)
class GreenhouseSubmissionQualification:
    can_submit: bool
    reason_codes: tuple[str, ...]


def qualify_greenhouse_submission(
    *,
    schema: GreenhouseJobSchemaSnapshot,
    payload: GreenhouseSubmissionPayload,
) -> GreenhouseSubmissionQualification:
    reasons: list[str] = []
    if payload.target != schema.target:
        reasons.append("GREENHOUSE_TARGET_MISMATCH")
    if payload.schema_hash != schema.schema_hash:
        reasons.append("GREENHOUSE_SCHEMA_HASH_MISMATCH")
    reasons.extend(
        f"GREENHOUSE_HARD_STOP_{item.value.upper()}" for item in schema.hard_stop_categories
    )

    allowed = schema.allowed_field_names
    supplied_fields = set(payload.fields)
    attachment_fields = {item.field_name for item in payload.attachment_refs}
    if supplied_fields - allowed or attachment_fields - allowed:
        reasons.append("GREENHOUSE_UNKNOWN_FIELD")

    supplied_nonempty = {
        name for name, value in payload.fields.items() if _payload_value_is_nonempty(value)
    } | attachment_fields
    for question in schema.questions:
        if question.required and not any(
            field_spec.name in supplied_nonempty for field_spec in question.fields
        ):
            reasons.append("GREENHOUSE_REQUIRED_FIELD_MISSING")
            break
    if not _STANDARD_REQUIRED_FIELDS.issubset(supplied_nonempty):
        reasons.append("GREENHOUSE_STANDARD_REQUIRED_FIELD_MISSING")

    for field_name, value in payload.fields.items():
        field_spec = schema.field_spec(field_name)
        if field_spec is None:
            continue
        if not _value_matches_field_spec(value, field_spec):
            reasons.append("GREENHOUSE_FIELD_VALUE_SCHEMA_MISMATCH")
            break
        if field_spec.kind is GreenhouseFieldKind.INPUT_FILE:
            reasons.append("GREENHOUSE_FILE_FIELD_MUST_USE_APPROVED_ATTACHMENT")
            break
    for attachment in payload.attachment_refs:
        field_spec = schema.field_spec(attachment.field_name)
        if field_spec is not None and field_spec.kind is not GreenhouseFieldKind.INPUT_FILE:
            reasons.append("GREENHOUSE_ATTACHMENT_FIELD_SCHEMA_MISMATCH")
            break

    _validate_standard_provider_fields(payload.fields, reasons)
    reason_codes = tuple(dict.fromkeys(reasons))
    if not reason_codes:
        reason_codes = ("GREENHOUSE_SUBMISSION_QUALIFIED",)
    return GreenhouseSubmissionQualification(
        can_submit=reason_codes == ("GREENHOUSE_SUBMISSION_QUALIFIED",),
        reason_codes=reason_codes,
    )


def _parse_question(
    raw: Mapping[str, object],
) -> tuple[GreenhouseQuestionSpec, tuple[GreenhouseHardStopCategory, ...]]:
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > 500:
        raise GreenhouseSchemaError("Greenhouse question label must be bounded")
    required = raw.get("required")
    if not isinstance(required, bool):
        raise GreenhouseSchemaError("Greenhouse question required must be boolean")
    raw_fields = raw.get("fields")
    if not _is_object_sequence(raw_fields) or not raw_fields:
        raise GreenhouseSchemaError("Greenhouse question fields must be a non-empty array")

    hard_stops = list(_classify_prohibited_text(label))
    fields: list[GreenhouseFieldSpec] = []
    for raw_field in raw_fields:
        field_name = raw_field.get("name")
        raw_kind = raw_field.get("type")
        if not isinstance(field_name, str) or len(field_name) > 128:
            raise GreenhouseSchemaError("Greenhouse field name must be bounded")
        if not isinstance(raw_kind, str):
            raise GreenhouseSchemaError("Greenhouse field type must be a string")
        try:
            kind = GreenhouseFieldKind(raw_kind)
        except ValueError:
            kind = GreenhouseFieldKind.INPUT_TEXT
            hard_stops.append(GreenhouseHardStopCategory.UNKNOWN_FIELD)
        is_core_field = _CORE_FIELD_NAME.fullmatch(field_name) is not None
        is_known_attachment_alternative = field_name in _PROVIDER_ATTACHMENT_ALTERNATIVES
        if not is_core_field and not is_known_attachment_alternative:
            hard_stops.append(GreenhouseHardStopCategory.UNKNOWN_FIELD)
        if _EXPECTED_FIELD_KINDS.get(field_name) is not kind:
            hard_stops.append(GreenhouseHardStopCategory.UNKNOWN_FIELD)
        if field_name in _LOCATION_FIELDS:
            hard_stops.append(GreenhouseHardStopCategory.LOCATION)
        if kind is GreenhouseFieldKind.INPUT_FILE and field_name not in _SAFE_FILE_FIELDS:
            hard_stops.append(GreenhouseHardStopCategory.UNKNOWN_FIELD)
        if kind is GreenhouseFieldKind.INPUT_HIDDEN:
            hard_stops.append(GreenhouseHardStopCategory.UNKNOWN_FIELD)
        hard_stops.extend(_classify_prohibited_text(field_name))
        option_values = _parse_option_values(raw_field.get("values", ()), kind)
        fields.append(GreenhouseFieldSpec(name=field_name, kind=kind, option_values=option_values))
    if len({field.name for field in fields}) != len(fields):
        hard_stops.append(GreenhouseHardStopCategory.UNKNOWN_FIELD)
    return (
        GreenhouseQuestionSpec(label=label.strip(), required=required, fields=tuple(fields)),
        _unique_hard_stops(hard_stops),
    )


def _parse_option_values(
    raw_values: object,
    kind: GreenhouseFieldKind,
) -> tuple[str | int, ...]:
    if kind not in {GreenhouseFieldKind.SINGLE_SELECT, GreenhouseFieldKind.MULTI_SELECT}:
        return ()
    if not _is_object_sequence(raw_values) or not raw_values:
        raise GreenhouseSchemaError("Greenhouse select field requires bounded options")
    options: list[str | int] = []
    for raw_option in raw_values:
        value = raw_option.get("value")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise GreenhouseSchemaError("Greenhouse option value must be a string or integer")
        if isinstance(value, str) and (not value.strip() or len(value) > 256):
            raise GreenhouseSchemaError("Greenhouse option string must be bounded")
        options.append(value)
    if len(set(options)) != len(options):
        raise GreenhouseSchemaError("Greenhouse option values must be unique")
    return tuple(options)


def _normalize_payload_fields(fields: Mapping[str, object]) -> MappingProxyType[str, object]:
    if not fields or len(fields) > _MAX_FIELDS:
        raise ValueError("Greenhouse payload fields must be non-empty and bounded")
    normalized: dict[str, object] = {}
    text_chars = 0
    for name, value in sorted(fields.items()):
        if not name or len(name) > 128:
            raise ValueError("Greenhouse payload field names must be bounded")
        frozen = _normalize_payload_value(value)
        text_chars += _payload_text_chars(frozen)
        normalized[name] = frozen
    if text_chars > _MAX_TOTAL_FIELD_TEXT_CHARS:
        raise ValueError("Greenhouse payload field text exceeds the total boundary")
    return MappingProxyType(normalized)


def _normalize_payload_value(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if not value.strip() or len(value) > _MAX_FIELD_TEXT_CHARS or "\x00" in value:
            raise ValueError("Greenhouse text field value must be non-empty and bounded")
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast("Sequence[object]", value)
        if not sequence or len(sequence) > 50:
            raise ValueError("Greenhouse multi-select value must be non-empty and bounded")
        normalized = tuple(_normalize_select_scalar(item) for item in sequence)
        if len(set(normalized)) != len(normalized):
            raise ValueError("Greenhouse multi-select value must not contain duplicates")
        return normalized
    raise ValueError("Greenhouse field values must be scalar or a bounded scalar sequence")


def _normalize_select_scalar(value: object) -> str | int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Greenhouse select values must be strings or integers")
    if isinstance(value, str) and (not value.strip() or len(value) > 256):
        raise ValueError("Greenhouse select string must be bounded")
    return value


def _value_matches_field_spec(value: object, field_spec: GreenhouseFieldSpec) -> bool:
    if field_spec.kind in {
        GreenhouseFieldKind.INPUT_TEXT,
        GreenhouseFieldKind.INPUT_HIDDEN,
        GreenhouseFieldKind.TEXTAREA,
    }:
        return isinstance(value, str) and bool(value.strip())
    if field_spec.kind is GreenhouseFieldKind.INPUT_FILE:
        return False
    if field_spec.kind is GreenhouseFieldKind.SINGLE_SELECT:
        return (
            not isinstance(value, bool)
            and isinstance(value, (str, int))
            and value in field_spec.option_values
        )
    if field_spec.kind is GreenhouseFieldKind.MULTI_SELECT:
        values = cast("tuple[object, ...]", value) if isinstance(value, tuple) else ()
        return (
            isinstance(value, tuple)
            and bool(values)
            and all(item in field_spec.option_values for item in values)
        )
    return False


def _validate_standard_provider_fields(fields: Mapping[str, object], reasons: list[str]) -> None:
    for name in ("first_name", "last_name"):
        value = fields.get(name)
        if not isinstance(value, str) or len(value) > 255 or _URL_IN_TEXT.search(value):
            reasons.append("GREENHOUSE_STANDARD_FIELD_INVALID")
            break
    email = fields.get("email")
    if not isinstance(email, str) or len(email) > 255 or _EMAIL.fullmatch(email) is None:
        reasons.append("GREENHOUSE_EMAIL_INVALID")
    phone = fields.get("phone")
    if phone is not None and (
        not isinstance(phone, str) or len(phone) > 255 or _URL_IN_TEXT.search(phone)
    ):
        reasons.append("GREENHOUSE_STANDARD_FIELD_INVALID")


def _classify_prohibited_text(text: str) -> tuple[GreenhouseHardStopCategory, ...]:
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())
    padded = f" {normalized} "
    categories: list[GreenhouseHardStopCategory] = []
    for raw_category, markers in _PROHIBITED_MARKERS.items():
        normalized_markers = (
            " ".join(re.sub(r"[^a-z0-9]+", " ", marker.casefold()).split()) for marker in markers
        )
        if any(f" {marker} " in padded for marker in normalized_markers):
            categories.append(GreenhouseHardStopCategory(raw_category))
    return tuple(categories)


def _unique_hard_stops(
    values: Sequence[GreenhouseHardStopCategory],
) -> tuple[GreenhouseHardStopCategory, ...]:
    return tuple(dict.fromkeys(values))


def _is_object_sequence(value: object) -> TypeGuard[Sequence[Mapping[str, object]]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return False
    sequence = cast("Sequence[object]", value)
    return all(isinstance(item, Mapping) for item in sequence)


def _payload_value_is_nonempty(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, tuple):
        return bool(cast("tuple[object, ...]", value))
    return isinstance(value, (int, bool))


def _payload_text_chars(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, tuple):
        values = cast("tuple[object, ...]", value)
        return sum(len(item) for item in values if isinstance(item, str))
    return 0


def _filename_extension(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].casefold() if dot >= 0 else ""


def _required_schema_str(
    payload: Mapping[str, object],
    field_name: str,
    *,
    max_chars: int,
) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise GreenhouseSchemaError(f"Greenhouse schema {field_name} must be a string")
    normalized = value.strip()
    _validate_bounded_schema_text(normalized, field_name, max_chars=max_chars)
    return normalized


def _validate_bounded_schema_text(value: str, field_name: str, *, max_chars: int) -> None:
    if not value or len(value) > max_chars or "\x00" in value:
        raise GreenhouseSchemaError(f"Greenhouse schema {field_name} must be bounded")


def _validate_iso_datetime(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GreenhouseSchemaError(f"Greenhouse schema {field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GreenhouseSchemaError(f"Greenhouse schema {field_name} must be timezone-aware")


def _validate_hash(value: str, field_name: str) -> None:
    if _HASH.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase sha256")


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonicalize(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        normalized: dict[str, object] = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise TypeError("canonical mapping keys must be strings")
            normalized[key] = _canonicalize(item)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (tuple, list)):
        sequence = cast("Sequence[object]", value)
        return [_canonicalize(item) for item in sequence]
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


__all__ = [
    "GREENHOUSE_ACTION_KIND",
    "GREENHOUSE_ADAPTER_ID",
    "GREENHOUSE_BOARD_API_HOST",
    "GREENHOUSE_BOARD_API_ORIGIN",
    "GREENHOUSE_CHANNEL",
    "GREENHOUSE_PAYLOAD_VERSION",
    "GREENHOUSE_SCHEMA_VERSION",
    "GREENHOUSE_SUBMISSION_IDENTITY_VERSION",
    "GreenhouseAttachmentRef",
    "GreenhouseFieldKind",
    "GreenhouseFieldSpec",
    "GreenhouseHardStopCategory",
    "GreenhouseJobSchemaSnapshot",
    "GreenhouseQuestionSpec",
    "GreenhouseSchemaError",
    "GreenhouseSubmissionPayload",
    "GreenhouseSubmissionQualification",
    "GreenhouseTarget",
    "qualify_greenhouse_submission",
]
