from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Protocol, cast
from uuid import UUID

from pydantic import JsonValue

_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")
_MAX_TEXT = 2_000
_PREP_VERSION = "application-prep.v1"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: UUID
    content_hash: str
    span_hash: str
    source_url: str | None
    provider_id: str | None
    sanitized_span: str

    def __post_init__(self) -> None:
        _validate_hash(self.content_hash, "content_hash")
        _validate_hash(self.span_hash, "span_hash")
        if self.source_url is None and self.provider_id is None:
            raise ValueError("evidence must keep at least one source reference")
        if self.source_url is not None:
            _validate_bounded_text(self.source_url, "source_url")
        if self.provider_id is not None:
            _validate_identifier(self.provider_id, "provider_id")
        _validate_bounded_text(self.sanitized_span, "sanitized_span")


@dataclass(frozen=True, slots=True)
class PublicJobDiscovery:
    canonical_job_id: UUID
    job_posting_id: UUID
    company_name: str
    title: str
    canonical_url: str
    source_type: str
    required_keywords: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        _validate_bounded_text(self.company_name, "company_name")
        _validate_bounded_text(self.title, "title")
        _validate_bounded_text(self.canonical_url, "canonical_url")
        _validate_identifier(self.source_type, "source_type")
        if not self.evidence:
            raise ValueError("job discovery must include evidence")
        object.__setattr__(
            self,
            "required_keywords",
            _normalize_keywords(self.required_keywords, "required_keywords"),
        )
        evidence = tuple(self.evidence)
        if len({item.evidence_id for item in evidence}) != len(evidence):
            raise ValueError("job discovery evidence ids must be unique")
        object.__setattr__(self, "evidence", tuple(sorted(evidence, key=_evidence_sort_key)))


@dataclass(frozen=True, slots=True)
class CandidateMatchProfile:
    candidate_id: UUID
    desired_keywords: tuple[str, ...]
    excluded_keywords: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "desired_keywords",
            _normalize_keywords(self.desired_keywords, "desired_keywords"),
        )
        object.__setattr__(
            self,
            "excluded_keywords",
            _normalize_keywords(self.excluded_keywords, "excluded_keywords", allow_empty=True),
        )


@dataclass(frozen=True, slots=True)
class ApprovedMaterialRef:
    material_id: UUID
    material_kind: str
    sha256: str
    label_zh: str

    def __post_init__(self) -> None:
        _validate_identifier(self.material_kind, "material_kind")
        _validate_hash(self.sha256, "sha256")
        _validate_bounded_text(self.label_zh, "label_zh")


@dataclass(frozen=True, slots=True)
class PreparedApplicationDraft:
    prepared_at: datetime
    action_kind: str
    resource_type: str
    resource_id: UUID
    idempotency_key: str
    target: MappingProxyType[str, JsonValue]
    payload: MappingProxyType[str, JsonValue]
    attachment_refs: tuple[MappingProxyType[str, JsonValue], ...]
    payload_hash: str
    match_score: float
    reason_codes: tuple[str, ...]


class ApplicationDraftStore(Protocol):
    def save_prepared_draft(
        self,
        draft: PreparedApplicationDraft,
        *,
        created_by: str,
    ) -> UUID: ...


class EvidenceFirstApplicationPreparer:
    """Build reviewable internal application drafts from untrusted public-job evidence."""

    def prepare(
        self,
        *,
        discovery: PublicJobDiscovery,
        profile: CandidateMatchProfile,
        materials: tuple[ApprovedMaterialRef, ...],
        now: datetime,
    ) -> PreparedApplicationDraft:
        _validate_aware(now, "now")
        if not materials:
            raise ValueError("application draft requires at least one approved material")
        normalized_materials = _normalize_materials(materials)
        score, reason_codes = _score(discovery, profile)
        target: dict[str, JsonValue] = {
            "candidate_id": str(profile.candidate_id),
            "canonical_job_id": str(discovery.canonical_job_id),
            "job_posting_id": str(discovery.job_posting_id),
            "company_name": discovery.company_name,
            "title": discovery.title,
            "canonical_url": discovery.canonical_url,
            "source_type": discovery.source_type,
        }
        payload: dict[str, JsonValue] = {
            "version": _PREP_VERSION,
            "match": {
                "score": score,
                "reason_codes": list(reason_codes),
                "required_keywords": list(discovery.required_keywords),
                "candidate_keywords": list(profile.desired_keywords),
            },
            "evidence": [
                {
                    "evidence_id": str(item.evidence_id),
                    "content_hash": item.content_hash,
                    "span_hash": item.span_hash,
                    "source_url": item.source_url,
                    "provider_id": item.provider_id,
                    "sanitized_span": item.sanitized_span,
                }
                for item in discovery.evidence
            ],
            "draft": {
                "status": "review_required",
                "summary_zh": (
                    f"为 {discovery.company_name} 的 {discovery.title} 生成内部申请草稿; "
                    "提交前必须经过人工审核。"
                ),
            },
        }
        attachment_refs = tuple(
            _freeze_mapping(
                {
                    "material_id": str(material.material_id),
                    "material_kind": material.material_kind,
                    "sha256": material.sha256,
                    "label_zh": material.label_zh,
                }
            )
            for material in normalized_materials
        )
        frozen_target = _freeze_mapping(target)
        frozen_payload = _freeze_mapping(payload)
        payload_hash = prepared_draft_payload_hash(
            target=frozen_target,
            payload=frozen_payload,
            attachment_refs=attachment_refs,
        )
        return PreparedApplicationDraft(
            prepared_at=now,
            action_kind="create_internal_draft",
            resource_type="canonical_job",
            resource_id=discovery.canonical_job_id,
            idempotency_key=(
                f"application-draft:{profile.candidate_id}:"
                f"{discovery.job_posting_id}:{payload_hash}"
            ),
            target=frozen_target,
            payload=frozen_payload,
            attachment_refs=attachment_refs,
            payload_hash=payload_hash,
            match_score=score,
            reason_codes=reason_codes,
        )


def _score(
    discovery: PublicJobDiscovery,
    profile: CandidateMatchProfile,
) -> tuple[float, tuple[str, ...]]:
    required = set(discovery.required_keywords)
    desired = set(profile.desired_keywords)
    excluded = set(profile.excluded_keywords)
    if required & excluded:
        return 0.0, ("EXCLUDED_KEYWORD_MATCH",)
    if not required:
        return 0.5, ("NO_REQUIRED_KEYWORDS",)
    matched = required & desired
    score = round(len(matched) / len(required), 4)
    if score >= 0.8:
        return score, ("STRONG_KEYWORD_MATCH",)
    if score >= 0.4:
        return score, ("PARTIAL_KEYWORD_MATCH",)
    return score, ("LOW_KEYWORD_MATCH",)


def prepared_draft_payload_hash(
    *,
    target: Mapping[str, object],
    payload: Mapping[str, object],
    attachment_refs: tuple[Mapping[str, object], ...],
) -> str:
    return _canonical_hash(
        {
            "target": target,
            "payload": payload,
            "attachment_refs": attachment_refs,
        }
    )


def materialize_json_mapping(value: Mapping[str, object]) -> dict[str, JsonValue]:
    materialized = _materialize_json_value(value)
    if not isinstance(materialized, dict):
        raise ValueError("expected a JSON object")
    return materialized


def _canonical_hash(value: object) -> str:
    try:
        encoded = json.dumps(
            _materialize_json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("draft identity must be JSON-canonicalizable") from error
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _freeze_mapping(value: Mapping[str, JsonValue]) -> MappingProxyType[str, JsonValue]:
    return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})


def _freeze_json_value(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return cast(JsonValue, _freeze_mapping(value))
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
    raise ValueError("value is not JSON-canonicalizable")


def _evidence_sort_key(item: EvidenceRef) -> tuple[str, str, str, str, str, str]:
    return (
        str(item.evidence_id),
        item.content_hash,
        item.span_hash,
        item.source_url or "",
        item.provider_id or "",
        item.sanitized_span,
    )


def _normalize_materials(
    materials: tuple[ApprovedMaterialRef, ...],
) -> tuple[ApprovedMaterialRef, ...]:
    if len({material.material_id for material in materials}) != len(materials):
        raise ValueError("application draft material ids must be unique")
    return tuple(
        sorted(
            materials,
            key=lambda material: (
                str(material.material_id),
                material.material_kind,
                material.sha256,
                material.label_zh,
            ),
        )
    )


def _normalize_keywords(
    values: tuple[str, ...],
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip().lower() for value in values if value.strip()}))
    if not normalized and not allow_empty:
        raise ValueError(f"{label} must not be empty")
    for value in normalized:
        _validate_bounded_text(value, label)
    return normalized


def _validate_hash(value: str, label: str) -> None:
    if not _HASH.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase sha256 hex digest")


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")


def _validate_bounded_text(value: str, label: str) -> None:
    if not value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"{label} must be non-empty and bounded")


def _validate_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = [
    "ApplicationDraftStore",
    "ApprovedMaterialRef",
    "CandidateMatchProfile",
    "EvidenceFirstApplicationPreparer",
    "EvidenceRef",
    "PreparedApplicationDraft",
    "PublicJobDiscovery",
    "materialize_json_mapping",
    "prepared_draft_payload_hash",
]
