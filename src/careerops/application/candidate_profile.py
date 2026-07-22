from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath
from typing import cast
from uuid import UUID

from pydantic import JsonValue

PROFILE_SCHEMA_VERSION = "candidate-profile.v1"
PREFERENCES_SCHEMA_VERSION = "candidate-job-preferences.v1"
MATERIAL_BUNDLE_SCHEMA_VERSION = "candidate-material-bundle.v1"
PROFILE_SNAPSHOT_SCHEMA_VERSION = "candidate-profile-snapshot.v1"
CANONICALIZATION_VERSION = "careerops-json-c14n.v1"
MAX_MATERIAL_BYTES = 10 * 1024 * 1024
MAX_MATERIALS = 10

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_OBJECT_KEY = re.compile(r"^sha256/[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_MAX_TEXT = 2_000
_MAX_LIST_ITEMS = 100
_SUPPORTED_MATERIAL_MEDIA_TYPES: Mapping[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".doc": frozenset({"application/msword"}),
    ".docx": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    ),
    ".txt": frozenset({"text/plain"}),
    ".rtf": frozenset({"application/rtf", "text/rtf"}),
}


class CandidateSeniority(StrEnum):
    UNKNOWN = "unknown"
    INTERN = "intern"
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    EXECUTIVE = "executive"


class WorkAuthorizationStatus(StrEnum):
    UNKNOWN = "unknown"
    AUTHORIZED = "authorized"
    RESTRICTED = "restricted"
    REQUIRES_SPONSORSHIP = "requires_sponsorship"


class WorkMode(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"


class CandidateMaterialKind(StrEnum):
    RESUME = "resume"
    COVER_LETTER = "cover_letter"
    PORTFOLIO = "portfolio"
    CERTIFICATE = "certificate"
    OTHER = "other"


class CandidateProfileDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class CandidateProfileDocumentV1:
    skills: tuple[str, ...]
    role_titles: tuple[str, ...]
    seniority: CandidateSeniority = CandidateSeniority.UNKNOWN
    years_experience: int | None = None
    industries: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    work_authorization: WorkAuthorizationStatus = WorkAuthorizationStatus.UNKNOWN
    requires_sponsorship: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "skills",
            _normalize_terms(self.skills, "profile skills", allow_empty=True),
        )
        object.__setattr__(
            self,
            "role_titles",
            _normalize_terms(self.role_titles, "profile role_titles", allow_empty=True),
        )
        object.__setattr__(
            self,
            "industries",
            _normalize_terms(self.industries, "profile industries", allow_empty=True),
        )
        object.__setattr__(
            self,
            "languages",
            _normalize_terms(self.languages, "profile languages", allow_empty=True),
        )
        if not self.skills and not self.role_titles:
            raise ValueError("candidate profile requires at least one skill or role title")
        if self.years_experience is not None and not 0 <= self.years_experience <= 80:
            raise ValueError("profile years_experience must be between 0 and 80")
        if (
            self.work_authorization is WorkAuthorizationStatus.REQUIRES_SPONSORSHIP
            and self.requires_sponsorship is False
        ):
            raise ValueError("profile sponsorship fields contradict each other")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "skills": list(self.skills),
            "role_titles": list(self.role_titles),
            "seniority": self.seniority.value,
            "years_experience": self.years_experience,
            "industries": list(self.industries),
            "languages": list(self.languages),
            "work_authorization": self.work_authorization.value,
            "requires_sponsorship": self.requires_sponsorship,
        }


@dataclass(frozen=True, slots=True)
class CandidateJobPreferencesV1:
    target_titles: tuple[str, ...]
    required_skills: tuple[str, ...] = ()
    preferred_skills: tuple[str, ...] = ()
    excluded_skills: tuple[str, ...] = ()
    required_keywords: tuple[str, ...] = ()
    preferred_keywords: tuple[str, ...] = ()
    excluded_keywords: tuple[str, ...] = ()
    allowed_locations: tuple[str, ...] = ()
    excluded_locations: tuple[str, ...] = ()
    work_modes: tuple[WorkMode, ...] = ()
    employment_types: tuple[EmploymentType, ...] = ()
    seniority_levels: tuple[CandidateSeniority, ...] = ()
    minimum_salary: int | None = None
    salary_currency: str | None = None
    sponsorship_allowed: bool | None = None
    allowed_companies: tuple[str, ...] = ()
    excluded_companies: tuple[str, ...] = ()
    allowed_industries: tuple[str, ...] = ()
    excluded_industries: tuple[str, ...] = ()
    minimum_match_score: float = 0.5

    def __post_init__(self) -> None:
        for field_name in (
            "target_titles",
            "required_skills",
            "preferred_skills",
            "excluded_skills",
            "required_keywords",
            "preferred_keywords",
            "excluded_keywords",
            "allowed_locations",
            "excluded_locations",
            "allowed_companies",
            "excluded_companies",
            "allowed_industries",
            "excluded_industries",
        ):
            values = cast("tuple[str, ...]", getattr(self, field_name))
            object.__setattr__(
                self,
                field_name,
                _normalize_terms(values, f"preferences {field_name}", allow_empty=True),
            )
        if not self.target_titles:
            raise ValueError("candidate preferences require at least one target title")
        object.__setattr__(self, "work_modes", _normalize_enums(self.work_modes))
        object.__setattr__(
            self,
            "employment_types",
            _normalize_enums(self.employment_types),
        )
        object.__setattr__(
            self,
            "seniority_levels",
            _normalize_enums(self.seniority_levels),
        )
        if self.minimum_salary is not None and not 0 <= self.minimum_salary <= 100_000_000:
            raise ValueError("preferences minimum_salary is out of bounds")
        if (self.minimum_salary is None) != (self.salary_currency is None):
            raise ValueError("preferences salary amount and currency must be provided together")
        if self.salary_currency is not None:
            normalized_currency = self.salary_currency.strip().upper()
            if not _CURRENCY.fullmatch(normalized_currency):
                raise ValueError("preferences salary_currency must be a three-letter code")
            object.__setattr__(self, "salary_currency", normalized_currency)
        if not 0.0 <= self.minimum_match_score <= 1.0:
            raise ValueError("preferences minimum_match_score must be between 0 and 1")
        _require_disjoint(self.required_skills, self.excluded_skills, "skills")
        _require_disjoint(self.preferred_skills, self.excluded_skills, "skills")
        _require_disjoint(self.required_keywords, self.excluded_keywords, "keywords")
        _require_disjoint(self.preferred_keywords, self.excluded_keywords, "keywords")
        _require_disjoint(self.allowed_locations, self.excluded_locations, "locations")
        _require_disjoint(self.allowed_companies, self.excluded_companies, "companies")
        _require_disjoint(self.allowed_industries, self.excluded_industries, "industries")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": PREFERENCES_SCHEMA_VERSION,
            "target_titles": list(self.target_titles),
            "required_skills": list(self.required_skills),
            "preferred_skills": list(self.preferred_skills),
            "excluded_skills": list(self.excluded_skills),
            "required_keywords": list(self.required_keywords),
            "preferred_keywords": list(self.preferred_keywords),
            "excluded_keywords": list(self.excluded_keywords),
            "allowed_locations": list(self.allowed_locations),
            "excluded_locations": list(self.excluded_locations),
            "work_modes": [item.value for item in self.work_modes],
            "employment_types": [item.value for item in self.employment_types],
            "seniority_levels": [item.value for item in self.seniority_levels],
            "minimum_salary": self.minimum_salary,
            "salary_currency": self.salary_currency,
            "sponsorship_allowed": self.sponsorship_allowed,
            "allowed_companies": list(self.allowed_companies),
            "excluded_companies": list(self.excluded_companies),
            "allowed_industries": list(self.allowed_industries),
            "excluded_industries": list(self.excluded_industries),
            "minimum_match_score": self.minimum_match_score,
        }


@dataclass(frozen=True, slots=True)
class CandidateMaterialRefV1:
    material_id: UUID
    content_object_id: UUID
    kind: CandidateMaterialKind
    label: str
    filename: str
    media_type: str
    sha256: str
    object_key: str
    byte_size: int

    def __post_init__(self) -> None:
        _validate_text(self.label, "material label", max_length=160)
        _validate_safe_filename(self.filename)
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("material sha256 must be a lowercase sha256 digest")
        if not _OBJECT_KEY.fullmatch(self.object_key) or not self.object_key.endswith(self.sha256):
            raise ValueError("material object_key must be the CAS key for sha256")
        suffix = PurePath(self.filename).suffix.lower()
        if self.media_type not in _SUPPORTED_MATERIAL_MEDIA_TYPES.get(suffix, frozenset()):
            raise ValueError("material filename and media_type are not a supported pair")
        if not 1 <= self.byte_size <= MAX_MATERIAL_BYTES:
            raise ValueError("material byte_size is out of bounds")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "material_id": str(self.material_id),
            "content_object_id": str(self.content_object_id),
            "kind": self.kind.value,
            "label": self.label.strip(),
            "filename": self.filename,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "object_key": self.object_key,
            "byte_size": self.byte_size,
        }

    def to_semantic_json(self) -> dict[str, JsonValue]:
        """Return the replay-stable identity; generated catalog UUIDs are excluded."""

        return {
            "kind": self.kind.value,
            "label": self.label.strip(),
            "filename": self.filename,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
        }


@dataclass(frozen=True, slots=True)
class CandidateMaterialBundleDraft:
    owner_user_id: UUID
    candidate_id: UUID
    bundle_id: UUID
    materials: tuple[CandidateMaterialRefV1, ...]

    def __post_init__(self) -> None:
        materials = tuple(
            sorted(
                self.materials,
                key=lambda item: (
                    item.kind.value,
                    item.sha256,
                    item.filename,
                    item.label,
                    item.media_type,
                    item.byte_size,
                ),
            )
        )
        if not 1 <= len(materials) <= MAX_MATERIALS:
            raise ValueError(f"candidate material bundle must contain 1 to {MAX_MATERIALS} items")
        if sum(item.kind is CandidateMaterialKind.RESUME for item in materials) != 1:
            raise ValueError("candidate material bundle requires exactly one resume")
        if len({item.material_id for item in materials}) != len(materials):
            raise ValueError("candidate material ids must be unique")
        if len({item.content_object_id for item in materials}) != len(materials):
            raise ValueError("candidate content object ids must be unique")
        object.__setattr__(self, "materials", materials)

    @property
    def manifest_json(self) -> list[JsonValue]:
        return [cast(JsonValue, item.to_json()) for item in self.materials]

    @property
    def semantic_manifest_json(self) -> list[JsonValue]:
        return [cast(JsonValue, item.to_semantic_json()) for item in self.materials]

    @property
    def canonical_payload(self) -> dict[str, JsonValue]:
        return {
            "schema_version": MATERIAL_BUNDLE_SCHEMA_VERSION,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "owner_user_id": str(self.owner_user_id),
            "candidate_id": str(self.candidate_id),
            "materials": self.semantic_manifest_json,
        }

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.canonical_payload)

    @property
    def bundle_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateProfileSnapshotDraft:
    owner_user_id: UUID
    candidate_id: UUID
    profile: CandidateProfileDocumentV1
    preferences: CandidateJobPreferencesV1
    material_bundle: CandidateMaterialBundleDraft

    def __post_init__(self) -> None:
        if self.material_bundle.owner_user_id != self.owner_user_id:
            raise ValueError("candidate material bundle owner does not match profile owner")
        if self.material_bundle.candidate_id != self.candidate_id:
            raise ValueError("candidate material bundle candidate does not match profile candidate")

    @property
    def canonical_payload(self) -> dict[str, JsonValue]:
        return {
            "schema_version": PROFILE_SNAPSHOT_SCHEMA_VERSION,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "owner_user_id": str(self.owner_user_id),
            "candidate_id": str(self.candidate_id),
            "profile": cast(JsonValue, self.profile.to_json()),
            "preferences": cast(JsonValue, self.preferences.to_json()),
            "material_bundle_sha256": self.material_bundle.bundle_sha256,
        }

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.canonical_payload)

    @property
    def snapshot_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateProfileImportCommand:
    snapshot: CandidateProfileSnapshotDraft
    profile_version_id: UUID
    display_name: str
    idempotency_key: str
    trace_id: str

    def __post_init__(self) -> None:
        _validate_text(self.display_name, "candidate display_name", max_length=160)
        _validate_identifier(self.idempotency_key, "candidate profile idempotency_key")
        _validate_identifier(self.trace_id, "candidate profile trace_id")


@dataclass(frozen=True, slots=True)
class CandidateProfileDecisionCommand:
    actor_id: UUID
    candidate_id: UUID
    profile_version_id: UUID
    snapshot_sha256: str
    decision: CandidateProfileDecision
    reason: str
    idempotency_key: str
    trace_id: str

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.snapshot_sha256):
            raise ValueError("candidate profile decision snapshot_sha256 is invalid")
        _validate_text(self.reason, "candidate profile decision reason", max_length=4_000)
        _validate_identifier(self.idempotency_key, "candidate profile decision idempotency_key")
        _validate_identifier(self.trace_id, "candidate profile decision trace_id")


@dataclass(frozen=True, slots=True)
class ApprovedCandidateProfileQuery:
    actor_id: UUID
    candidate_id: UUID


@dataclass(frozen=True, slots=True)
class CandidateProfileGetQuery:
    actor_id: UUID
    candidate_id: UUID
    profile_version_id: UUID


@dataclass(frozen=True, slots=True)
class CandidateProfileListQuery:
    actor_id: UUID
    candidate_id: UUID
    limit: int = 20

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 100:
            raise ValueError("candidate profile list limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class CandidateProfileSnapshotRecord:
    owner_user_id: UUID
    candidate_id: UUID
    profile_version_id: UUID
    profile_version: int
    material_bundle_id: UUID
    material_bundle_version: int
    profile: Mapping[str, JsonValue]
    preferences: Mapping[str, JsonValue]
    materials: tuple[Mapping[str, JsonValue], ...]
    material_bundle_sha256: str
    snapshot_sha256: str
    decision: CandidateProfileDecision | None
    newly_created: bool


@dataclass(frozen=True, slots=True)
class CandidateProfileListPage:
    items: tuple[CandidateProfileSnapshotRecord, ...]
    count: int


class CandidateProfileRepositoryError(RuntimeError):
    def __init__(self, reason_code: str, *, sqlstate: str | None = None) -> None:
        _validate_identifier(reason_code, "candidate profile repository reason_code")
        self.reason_code = reason_code
        self.sqlstate = sqlstate
        super().__init__(reason_code if sqlstate is None else f"{reason_code}:{sqlstate}")


def candidate_profile_record_from_mapping(
    value: Mapping[str, object],
) -> CandidateProfileSnapshotRecord:
    try:
        material_values = _as_sequence(value["materials"], "materials")
        materials = tuple(_as_json_mapping(item, "material") for item in material_values)
        decision_value = value.get("decision")
        return CandidateProfileSnapshotRecord(
            owner_user_id=UUID(str(value["owner_user_id"])),
            candidate_id=UUID(str(value["candidate_id"])),
            profile_version_id=UUID(str(value["profile_version_id"])),
            profile_version=int(cast("int | str", value["profile_version"])),
            material_bundle_id=UUID(str(value["material_bundle_id"])),
            material_bundle_version=int(cast("int | str", value["material_bundle_version"])),
            profile=_as_json_mapping(value["profile"], "profile"),
            preferences=_as_json_mapping(value["preferences"], "preferences"),
            materials=materials,
            material_bundle_sha256=_as_sha256(
                value["material_bundle_sha256"], "material_bundle_sha256"
            ),
            snapshot_sha256=_as_sha256(value["snapshot_sha256"], "snapshot_sha256"),
            decision=(
                None
                if decision_value is None
                else CandidateProfileDecision(str(decision_value))
            ),
            newly_created=bool(value.get("newly_created", False)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CandidateProfileRepositoryError(
            "CANDIDATE_PROFILE_DATABASE_RESULT_INVALID"
        ) from error


def candidate_profile_list_page_from_mapping(
    value: Mapping[str, object],
) -> CandidateProfileListPage:
    try:
        raw_items = _as_sequence(value["items"], "items")
        items = tuple(
            candidate_profile_record_from_mapping(_as_json_mapping(item, "candidate profile"))
            for item in raw_items
        )
        count = int(cast("int | str", value["count"]))
        if count != len(items):
            raise ValueError("candidate profile list count does not match items")
        return CandidateProfileListPage(items=items, count=count)
    except (KeyError, TypeError, ValueError) as error:
        raise CandidateProfileRepositoryError(
            "CANDIDATE_PROFILE_DATABASE_RESULT_INVALID"
        ) from error


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            _materialize_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("candidate profile value must be JSON-canonicalizable") from error


def _materialize_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in cast("Mapping[object, object]", value).items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            result[key] = _materialize_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_materialize_json(item) for item in cast("Sequence[object]", value)]
    if value is None or isinstance(value, str | int | float | bool):
        return cast(JsonValue, value)
    raise ValueError("value is not JSON-canonicalizable")


def _normalize_terms(
    values: Sequence[str],
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip().lower() for value in values if value.strip()}))
    if len(normalized) > _MAX_LIST_ITEMS:
        raise ValueError(f"{label} contains too many items")
    if not normalized and not allow_empty:
        raise ValueError(f"{label} must not be empty")
    for value in normalized:
        _validate_text(value, label, max_length=160)
    return normalized


def _normalize_enums[T: StrEnum](values: Sequence[T]) -> tuple[T, ...]:
    return tuple(sorted(set(values), key=lambda item: item.value))


def _require_disjoint(left: Sequence[str], right: Sequence[str], label: str) -> None:
    if set(left) & set(right):
        raise ValueError(f"candidate preferences {label} allow/require and exclude sets overlap")


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")


def _validate_text(value: str, label: str, *, max_length: int = _MAX_TEXT) -> None:
    if not value.strip() or len(value) > max_length:
        raise ValueError(f"{label} must be non-empty and bounded")


def _validate_safe_filename(value: str) -> None:
    _validate_text(value, "material filename", max_length=255)
    if PurePath(value).name != value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("material filename must not contain a path")


def _as_json_mapping(value: object, label: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    materialized = _materialize_json(cast("Mapping[object, object]", value))
    if not isinstance(materialized, dict):
        raise ValueError(f"{label} must be a JSON object")
    return materialized


def _as_sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ValueError(f"{label} must be a JSON array")
    return cast("Sequence[object]", value)


def _as_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase sha256 digest")
    return value


__all__ = [
    "CANONICALIZATION_VERSION",
    "MATERIAL_BUNDLE_SCHEMA_VERSION",
    "MAX_MATERIALS",
    "MAX_MATERIAL_BYTES",
    "PREFERENCES_SCHEMA_VERSION",
    "PROFILE_SCHEMA_VERSION",
    "PROFILE_SNAPSHOT_SCHEMA_VERSION",
    "ApprovedCandidateProfileQuery",
    "CandidateJobPreferencesV1",
    "CandidateMaterialBundleDraft",
    "CandidateMaterialKind",
    "CandidateMaterialRefV1",
    "CandidateProfileDecision",
    "CandidateProfileDecisionCommand",
    "CandidateProfileDocumentV1",
    "CandidateProfileGetQuery",
    "CandidateProfileImportCommand",
    "CandidateProfileListPage",
    "CandidateProfileListQuery",
    "CandidateProfileRepositoryError",
    "CandidateProfileSnapshotDraft",
    "CandidateProfileSnapshotRecord",
    "CandidateSeniority",
    "EmploymentType",
    "WorkAuthorizationStatus",
    "WorkMode",
    "candidate_profile_list_page_from_mapping",
    "candidate_profile_record_from_mapping",
    "canonical_json",
]
