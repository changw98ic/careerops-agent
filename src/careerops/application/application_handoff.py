"""Evidence-bound handoff for a human to submit a real application.

This is deliberately not a provider adapter. It creates an immutable, reviewable packet for a
human to open the public job URL and complete the final submission themselves. The packet cannot
contain credentials or cause a browser, mail, or provider side effect.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import JsonValue

from careerops.application.application_prep import (
    ApprovedMaterialRef,
    PreparedApplicationDraft,
    materialize_json_mapping,
    prepared_draft_payload_hash,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")
_MAX_TEXT = 2_000
_HANDOFF_VERSION = "manual-application-handoff.v1"
_ACTION_KIND = "create_manual_application_handoff"
_INSTRUCTIONS_ZH = (
    "打开已核验的公开岗位链接\uff0c并由本人完成最终提交。",
    "登录、验证码或 MFA、法律声明、敏感信息、身份材料、付款和评估必须由本人处理。",
    "仅使用此交接包列出的已审核材料\uff1b提交后记录站点回执或参考号。",
)


@dataclass(frozen=True, slots=True)
class ManualHandoffEvidenceRef:
    """The minimum immutable evidence required to open a real public application URL."""

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
            raise ValueError("handoff evidence must keep a source URL or provider identifier")
        if self.source_url is not None:
            object.__setattr__(
                self,
                "source_url",
                _normalize_public_https_url(self.source_url, "source_url"),
            )
        if self.provider_id is not None:
            _validate_identifier(self.provider_id, "provider_id")
        _validate_text(self.sanitized_span, "sanitized_span")


@dataclass(frozen=True, slots=True)
class ManualApplicationHandoff:
    """Immutable local packet for a user-owned final submission step.

    The target must be the exact public URL present in the prepared draft and it must be backed by
    at least one source-evidence URL on the same HTTPS origin. A caller cannot redirect a reviewed
    draft to an unrelated site or replace its materials after handoff creation.
    """

    prepared_at: datetime
    candidate_id: UUID
    canonical_job_id: UUID
    job_posting_id: UUID
    target_url: str
    source_draft_payload_hash: str
    evidence: tuple[ManualHandoffEvidenceRef, ...]
    approved_materials: tuple[ApprovedMaterialRef, ...]
    payload_hash: str = field(init=False)
    idempotency_key: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_aware(self.prepared_at, "prepared_at")
        target_url = _normalize_public_https_url(self.target_url, "target_url")
        _validate_hash(self.source_draft_payload_hash, "source_draft_payload_hash")
        evidence = tuple(sorted(self.evidence, key=_evidence_sort_key))
        if not evidence:
            raise ValueError("manual handoff requires public source evidence")
        if len({item.evidence_id for item in evidence}) != len(evidence):
            raise ValueError("manual handoff evidence ids must be unique")
        if not any(
            item.source_url is not None and _same_public_origin(item.source_url, target_url)
            for item in evidence
        ):
            raise ValueError("manual handoff target requires same-origin public source evidence")
        materials = tuple(
            sorted(
                self.approved_materials,
                key=lambda material: (
                    str(material.material_id),
                    material.material_kind,
                    material.sha256,
                ),
            )
        )
        if not materials:
            raise ValueError("manual handoff requires at least one approved material")
        if len({item.material_id for item in materials}) != len(materials):
            raise ValueError("manual handoff material ids must be unique")
        if len({item.sha256 for item in materials}) != len(materials):
            raise ValueError("manual handoff material hashes must be unique")
        payload_hash = manual_handoff_payload_hash(
            candidate_id=self.candidate_id,
            canonical_job_id=self.canonical_job_id,
            job_posting_id=self.job_posting_id,
            target_url=target_url,
            source_draft_payload_hash=self.source_draft_payload_hash,
            evidence=evidence,
            approved_materials=materials,
        )
        object.__setattr__(self, "target_url", target_url)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "approved_materials", materials)
        object.__setattr__(self, "payload_hash", payload_hash)
        object.__setattr__(
            self,
            "idempotency_key",
            f"manual-handoff:{self.candidate_id}:{self.job_posting_id}:{payload_hash}",
        )

    @property
    def instructions_zh(self) -> tuple[str, ...]:
        return _INSTRUCTIONS_ZH

    @property
    def target(self) -> MappingProxyType[str, JsonValue]:
        return MappingProxyType(
            {
                "candidate_id": str(self.candidate_id),
                "submission_url": self.target_url,
                "canonical_job_id": str(self.canonical_job_id),
                "job_posting_id": str(self.job_posting_id),
            }
        )

    @property
    def payload(self) -> MappingProxyType[str, JsonValue]:
        return MappingProxyType(
            {
                "version": _HANDOFF_VERSION,
                "mode": "human_final_submission",
                "provider_execution": "disabled",
                "source_draft_payload_hash": self.source_draft_payload_hash,
                "source_evidence": [
                    {
                        "evidence_id": str(item.evidence_id),
                        "content_hash": item.content_hash,
                        "span_hash": item.span_hash,
                        "source_url": item.source_url,
                        "provider_id": item.provider_id,
                        "sanitized_span": item.sanitized_span,
                    }
                    for item in self.evidence
                ],
                "instructions_zh": list(self.instructions_zh),
            }
        )

    @property
    def attachment_refs(self) -> tuple[MappingProxyType[str, JsonValue], ...]:
        return tuple(
            MappingProxyType(
                {
                    "material_id": str(material.material_id),
                    "material_kind": material.material_kind,
                    "sha256": material.sha256,
                    "label_zh": material.label_zh,
                }
            )
            for material in self.approved_materials
        )


@dataclass(frozen=True, slots=True)
class ManualApplicationHandoffRef:
    action_intent_id: UUID
    payload_version_id: UUID
    payload_hash: str
    candidate_id: UUID
    canonical_job_id: UUID
    job_posting_id: UUID

    def __post_init__(self) -> None:
        _validate_hash(self.payload_hash, "payload_hash")


@dataclass(frozen=True, slots=True)
class ManualSubmissionAttestation:
    """User-provided record of a manual submission, never a provider receipt."""

    receipt_reference: str
    submitted_at: datetime

    def __post_init__(self) -> None:
        _validate_text(self.receipt_reference, "receipt_reference")
        _validate_aware(self.submitted_at, "submitted_at")


@dataclass(frozen=True, slots=True)
class ManualSubmissionRecord:
    application_event_id: UUID
    newly_created: bool


class ManualApplicationHandoffBuilder:
    """Convert an immutable internal draft into a human-only real-site handoff."""

    def prepare(
        self,
        *,
        draft: PreparedApplicationDraft,
        candidate_id: UUID,
        now: datetime,
    ) -> ManualApplicationHandoff:
        _validate_aware(now, "now")
        _validate_prepared_draft(draft)
        if draft.action_kind != "create_internal_draft":
            raise ValueError("manual handoff requires an internal application draft")
        if draft.resource_type != "canonical_job":
            raise ValueError("manual handoff requires a canonical job draft")
        target = materialize_json_mapping(draft.target)
        draft_candidate_id = _parse_uuid(target.get("candidate_id"), "candidate_id")
        if draft_candidate_id != candidate_id:
            raise ValueError("manual handoff candidate must match the prepared draft")
        canonical_job_id = _parse_uuid(target.get("canonical_job_id"), "canonical_job_id")
        if canonical_job_id != draft.resource_id:
            raise ValueError("manual handoff canonical job must match draft resource")
        job_posting_id = _parse_uuid(target.get("job_posting_id"), "job_posting_id")
        target_url = _required_text(target.get("canonical_url"), "canonical_url")
        evidence = _extract_evidence(draft.payload)
        materials = _extract_materials(draft.attachment_refs)
        return ManualApplicationHandoff(
            prepared_at=now,
            candidate_id=candidate_id,
            canonical_job_id=canonical_job_id,
            job_posting_id=job_posting_id,
            target_url=target_url,
            source_draft_payload_hash=draft.payload_hash,
            evidence=evidence,
            approved_materials=materials,
        )


def manual_handoff_payload_hash(
    *,
    candidate_id: UUID,
    canonical_job_id: UUID,
    job_posting_id: UUID,
    target_url: str,
    source_draft_payload_hash: str,
    evidence: tuple[ManualHandoffEvidenceRef, ...],
    approved_materials: tuple[ApprovedMaterialRef, ...],
) -> str:
    return _canonical_hash(
        {
            "version": _HANDOFF_VERSION,
            "candidate_id": str(candidate_id),
            "canonical_job_id": str(canonical_job_id),
            "job_posting_id": str(job_posting_id),
            "target_url": target_url,
            "source_draft_payload_hash": source_draft_payload_hash,
            "evidence": [
                {
                    "evidence_id": str(item.evidence_id),
                    "content_hash": item.content_hash,
                    "span_hash": item.span_hash,
                    "source_url": item.source_url,
                    "provider_id": item.provider_id,
                    "sanitized_span": item.sanitized_span,
                }
                for item in evidence
            ],
            "approved_materials": [
                {
                    "material_id": str(item.material_id),
                    "material_kind": item.material_kind,
                    "sha256": item.sha256,
                    "label_zh": item.label_zh,
                }
                for item in approved_materials
            ],
            "instructions_zh": list(_INSTRUCTIONS_ZH),
        }
    )


def _validate_prepared_draft(draft: PreparedApplicationDraft) -> None:
    actual = prepared_draft_payload_hash(
        target=draft.target,
        payload=draft.payload,
        attachment_refs=draft.attachment_refs,
    )
    if actual != draft.payload_hash:
        raise ValueError("prepared draft payload hash does not match immutable content")


def _extract_evidence(payload: Mapping[str, object]) -> tuple[ManualHandoffEvidenceRef, ...]:
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, (list, tuple)):
        raise ValueError("prepared draft must contain evidence")
    raw_items = cast("list[object] | tuple[object, ...]", raw_evidence)
    evidence: list[ManualHandoffEvidenceRef] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise ValueError("prepared draft evidence entries must be objects")
        item = cast("Mapping[str, object]", raw_item)
        evidence.append(
            ManualHandoffEvidenceRef(
                evidence_id=_parse_uuid(item.get("evidence_id"), "evidence_id"),
                content_hash=_required_text(item.get("content_hash"), "content_hash"),
                span_hash=_required_text(item.get("span_hash"), "span_hash"),
                source_url=_optional_text(item.get("source_url"), "source_url"),
                provider_id=_optional_text(item.get("provider_id"), "provider_id"),
                sanitized_span=_required_text(item.get("sanitized_span"), "sanitized_span"),
            )
        )
    return tuple(evidence)


def _extract_materials(
    attachment_refs: tuple[Mapping[str, object], ...],
) -> tuple[ApprovedMaterialRef, ...]:
    materials: list[ApprovedMaterialRef] = []
    for item in attachment_refs:
        materials.append(
            ApprovedMaterialRef(
                material_id=_parse_uuid(item.get("material_id"), "material_id"),
                material_kind=_required_text(item.get("material_kind"), "material_kind"),
                sha256=_required_text(item.get("sha256"), "sha256"),
                label_zh=_required_text(item.get("label_zh"), "label_zh"),
            )
        )
    return tuple(materials)


def _normalize_public_https_url(value: str, label: str) -> str:
    _validate_text(value, label)
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"{label} must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError(f"{label} must not contain credentials or a fragment")
    host = parsed.hostname
    if host is None or not _HOST.fullmatch(host):
        raise ValueError(f"{label} must use a bounded DNS host")
    normalized_host = host.lower()
    if (
        normalized_host.startswith(".")
        or normalized_host.endswith(".")
        or ".." in normalized_host
        or normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
        or normalized_host.endswith(".local")
    ):
        raise ValueError(f"{label} must use a public DNS host")
    try:
        ipaddress.ip_address(normalized_host)
    except ValueError:
        pass
    else:
        raise ValueError(f"{label} must use a public DNS host")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{label} has an invalid port") from error
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{label} has an invalid port")
    authority = normalized_host if port is None else f"{normalized_host}:{port}"
    return urlunsplit(("https", authority, parsed.path or "/", parsed.query, ""))


def _same_public_origin(left: str, right: str) -> bool:
    left_url = urlsplit(_normalize_public_https_url(left, "source_url"))
    right_url = urlsplit(_normalize_public_https_url(right, "target_url"))
    return (left_url.scheme, left_url.netloc) == (right_url.scheme, right_url.netloc)


def _canonical_hash(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("manual handoff identity must be JSON-canonicalizable") from error
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _evidence_sort_key(item: ManualHandoffEvidenceRef) -> tuple[str, str, str, str, str, str]:
    return (
        str(item.evidence_id),
        item.content_hash,
        item.span_hash,
        item.source_url or "",
        item.provider_id or "",
        item.sanitized_span,
    )


def _parse_uuid(value: object, label: str) -> UUID:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UUID string")
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError(f"{label} must be a UUID string") from error


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    _validate_text(value, label)
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label)


def _validate_hash(value: str, label: str) -> None:
    if not _HASH.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase sha256 hex digest")


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")


def _validate_text(value: str, label: str) -> None:
    if not value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"{label} must be non-empty and bounded")


def _validate_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = [
    "ManualApplicationHandoff",
    "ManualApplicationHandoffBuilder",
    "ManualApplicationHandoffRef",
    "ManualHandoffEvidenceRef",
    "ManualSubmissionAttestation",
    "ManualSubmissionRecord",
    "manual_handoff_payload_hash",
]
