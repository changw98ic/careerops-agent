from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, cast
from urllib.parse import urlparse
from uuid import UUID

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_DOMAIN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_SPACE = re.compile(r"\s+")
_NON_KEY = re.compile(r"[^a-z0-9]+")
_PUBLIC_ATS_ROW_SCHEMA_V1 = 1
_PUBLIC_ATS_ROW_SCHEMA_V2 = 2
_PUBLIC_ATS_ROW_SCHEMAS = frozenset({_PUBLIC_ATS_ROW_SCHEMA_V1, _PUBLIC_ATS_ROW_SCHEMA_V2})
_MAX_DESCRIPTION_LENGTH = 48_000


class CanonicalJobIngestionError(RuntimeError):
    pass


class CanonicalJobDedupePolicy(StrEnum):
    HASH = "hash"
    URL = "url"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class CrawlerRunProvenance:
    """Binds an ingested record back to a leased crawler source/run artifact."""

    crawler_source_row_id: UUID
    crawler_run_id: UUID
    run_event_id: UUID
    registry_id: UUID
    source_id: str
    adapter: str
    output_artifact_sha256: str

    def __post_init__(self) -> None:
        _validate_token(self.source_id, "source_id")
        _validate_token(self.adapter, "adapter")
        _validate_sha256(self.output_artifact_sha256, "output_artifact_sha256")


@dataclass(frozen=True, slots=True)
class PublicAtsJobRow:
    """Normalized subset accepted from a reviewed public ATS crawler artifact.

    This model intentionally carries only structured fields. It does not carry an
    executable script, arbitrary URL fetch instruction, browser state, credential,
    or provider callback.
    """

    company_name: str
    company_domain: str
    source_type: str
    source_identifier: str
    base_url: str
    external_id: str
    canonical_url: str
    title: str
    location: str | None
    department: str | None
    captured_at: datetime
    parser_version: str
    source_url: str
    structured_data: MappingProxyType[str, object]
    content_hash: str | None = None

    def __post_init__(self) -> None:
        _validate_text(self.company_name, "company_name", max_length=240)
        _validate_domain(self.company_domain)
        _validate_token(self.source_type, "source_type")
        _validate_text(self.source_identifier, "source_identifier", max_length=500)
        _validate_text(self.external_id, "external_id", max_length=500)
        _validate_url(self.base_url, "base_url")
        _validate_url(self.canonical_url, "canonical_url")
        _validate_url(self.source_url, "source_url")
        _validate_text(self.title, "title", max_length=500)
        if self.location is not None:
            _validate_text(self.location, "location", max_length=500)
        if self.department is not None:
            _validate_text(self.department, "department", max_length=500)
        _validate_aware(self.captured_at, "captured_at")
        _validate_text(self.parser_version, "parser_version", max_length=160)
        if self.content_hash is not None:
            _validate_sha256(self.content_hash, "content_hash")
        structured_data = dict(self.structured_data)
        if not all(1 <= len(key) <= 160 for key in structured_data):
            raise ValueError("structured_data keys must be bounded strings")
        try:
            encoded = json.dumps(
                structured_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("structured_data must contain only JSON values") from exc
        if len(encoded) > 65_536:
            raise ValueError("structured_data exceeds the 65536-byte limit")
        object.__setattr__(self, "structured_data", MappingProxyType(structured_data))

    @property
    def normalized_company_name(self) -> str:
        # A public crawl has not verified a legal/display name yet.  Bind the provisional
        # company identity to the registrable domain so same-named organizations cannot merge.
        return f"crawler-domain:{self.company_domain.casefold()}"

    @property
    def normalized_title(self) -> str:
        return normalize_key(self.title)

    @property
    def normalized_location(self) -> str:
        return normalize_key(self.location or "")

    @property
    def effective_content_hash(self) -> str:
        if self.content_hash is not None:
            return self.content_hash
        return sha256_json(
            {
                "canonical_url": normalize_url(self.canonical_url),
                "company_domain": self.company_domain,
                "department": self.department,
                "external_id": self.external_id,
                "location": self.location,
                "source_identifier": self.source_identifier,
                "source_type": self.source_type,
                "structured_data": dict(self.structured_data),
                "title": self.title,
            }
        )


@dataclass(frozen=True, slots=True)
class CanonicalJobEvidence:
    crawler_source_row_id: UUID
    crawler_run_id: UUID
    run_event_id: UUID
    source_url: str
    captured_at: datetime
    content_hash: str
    parser_version: str
    output_artifact_sha256: str
    provenance_policy: str
    dedupe_policy: CanonicalJobDedupePolicy
    dedupe_key: str
    rule: str

    def __post_init__(self) -> None:
        _validate_url(self.source_url, "source_url")
        _validate_aware(self.captured_at, "captured_at")
        _validate_sha256(self.content_hash, "content_hash")
        _validate_sha256(self.output_artifact_sha256, "output_artifact_sha256")
        _validate_text(self.parser_version, "parser_version", max_length=160)
        _validate_token(self.provenance_policy, "provenance_policy")
        _validate_sha256(self.dedupe_key, "dedupe_key")
        _validate_text(self.rule, "rule", max_length=240)


@dataclass(frozen=True, slots=True)
class CanonicalJobIngestionRecord:
    provenance: CrawlerRunProvenance
    row: PublicAtsJobRow
    dedupe_policy: CanonicalJobDedupePolicy
    provenance_policy: str = "preserve"

    def __post_init__(self) -> None:
        if self.provenance_policy not in {"preserve", "compact"}:
            raise ValueError("provenance_policy has an unsupported value")

    @property
    def dedupe_rule(self) -> str:
        return f"deterministic:{self.dedupe_policy.value}:v1"

    @property
    def dedupe_key(self) -> str:
        match self.dedupe_policy:
            case CanonicalJobDedupePolicy.HASH:
                payload = {
                    "source_type": self.row.source_type,
                    "source_identifier": self.row.source_identifier,
                    "external_id": self.row.external_id,
                }
            case CanonicalJobDedupePolicy.URL:
                payload = {"canonical_url": normalize_url(self.row.canonical_url)}
            case CanonicalJobDedupePolicy.HYBRID:
                payload = {
                    "company": self.row.company_domain,
                    "external_id": self.row.external_id,
                    "policy": "exact-source-identity-first",
                    "source_identifier": self.row.source_identifier,
                    "source_type": self.row.source_type,
                }
        return sha256_json(payload)

    @property
    def structured_payload(self) -> MappingProxyType[str, object]:
        payload = dict(self.row.structured_data)
        payload.update(
            {
                "company_domain": self.row.company_domain,
                "department": self.row.department,
                "location": self.row.location,
                "normalized_company_name": self.row.normalized_company_name,
                "normalized_title": self.row.normalized_title,
                "source_adapter": self.provenance.adapter,
                "source_registry_id": str(self.provenance.registry_id),
                "source_registry_source_id": self.provenance.source_id,
            }
        )
        return MappingProxyType(payload)

    @property
    def evidence(self) -> CanonicalJobEvidence:
        return CanonicalJobEvidence(
            crawler_source_row_id=self.provenance.crawler_source_row_id,
            crawler_run_id=self.provenance.crawler_run_id,
            run_event_id=self.provenance.run_event_id,
            source_url=self.row.source_url,
            captured_at=self.row.captured_at,
            content_hash=self.row.effective_content_hash,
            parser_version=self.row.parser_version,
            output_artifact_sha256=self.provenance.output_artifact_sha256,
            provenance_policy=self.provenance_policy,
            dedupe_policy=self.dedupe_policy,
            dedupe_key=self.dedupe_key,
            rule=self.dedupe_rule,
        )


@dataclass(frozen=True, slots=True)
class CanonicalJobIngestionResult:
    company_id: UUID
    source_id: UUID
    posting_id: UUID
    version_id: UUID
    canonical_job_id: UUID
    merge_decision_id: UUID | None
    evidence_id: UUID
    dedupe_key: str
    inserted_version: bool

    def __post_init__(self) -> None:
        _validate_sha256(self.dedupe_key, "dedupe_key")


class CanonicalJobIngestionStore(Protocol):
    def ingest_public_ats_job(
        self,
        record: CanonicalJobIngestionRecord,
    ) -> CanonicalJobIngestionResult: ...


class CanonicalJobIngestionService:
    def __init__(self, store: CanonicalJobIngestionStore) -> None:
        self._store = store

    def ingest_public_ats_job(
        self,
        row: PublicAtsJobRow,
        *,
        provenance: CrawlerRunProvenance,
        dedupe_policy: CanonicalJobDedupePolicy,
        provenance_policy: str = "preserve",
    ) -> CanonicalJobIngestionResult:
        record = CanonicalJobIngestionRecord(
            provenance=provenance,
            row=row,
            dedupe_policy=dedupe_policy,
            provenance_policy=provenance_policy,
        )
        return self._store.ingest_public_ats_job(record)


def public_ats_row_from_mapping(
    value: dict[str, object],
    *,
    captured_at: datetime | None = None,
    parser_version: str | None = None,
) -> PublicAtsJobRow:
    """Map existing public ATS JSONL rows into the canonical ingestion boundary."""

    schema_version = _public_ats_schema_version(value.get("schema_version"))
    resolved_parser_version = parser_version or f"public-ats-jsonl:v{schema_version}"
    discovered_at = _coerce_datetime(value.get("discovered_at"), fallback=captured_at)
    company_domain = _string(value.get("registrable_domain"), "registrable_domain")
    title = _string(value.get("source_job_title"), "source_job_title")
    canonical_url = _string(value.get("canonical_url"), "canonical_url")
    external_id = str(value.get("source_job_id") or value.get("source_record_id") or canonical_url)
    source_feed_ats = _optional_string(value.get("source_feed_ats"))
    source_feed_token = _optional_string(value.get("source_feed_token"))
    canonical_host = (urlparse(canonical_url).hostname or company_domain).casefold()
    source_identity_sha256 = sha256_json(
        {"ats": source_feed_ats, "host": canonical_host, "token": source_feed_token}
    )
    source_identifier = f"{(source_feed_ats or 'unknown').casefold()}:{source_identity_sha256}"
    base_url = _base_url(canonical_url)
    structured = {
        key: item
        for key, item in value.items()
        if key
        in {
            "source_assessment",
            "source_feed_ats",
            "source_feed_token",
            "source_index",
            "source_job_department",
            "source_job_location",
            "source_record_id",
        }
    }
    # Preserve the exact legacy v1 structured payload so re-ingesting an existing
    # crawler artifact cannot manufacture a new immutable job-posting version.
    # Versioned v2 artifacts declare their expanded contract explicitly.
    if schema_version == _PUBLIC_ATS_ROW_SCHEMA_V2:
        structured["artifact_schema_version"] = schema_version
    _copy_optional_text(
        structured,
        "description",
        value.get("source_job_description"),
        max_length=_MAX_DESCRIPTION_LENGTH,
    )
    _copy_optional_url(structured, "apply_url", value.get("source_job_apply_url"))
    keywords = value.get("source_job_keywords")
    if keywords is not None or schema_version == _PUBLIC_ATS_ROW_SCHEMA_V2:
        structured["keywords"] = list(
            _optional_string_sequence(
                keywords,
                "source_job_keywords",
                max_items=100,
                max_item_length=160,
            )
        )
    locations = value.get("source_job_locations")
    if locations is not None or schema_version == _PUBLIC_ATS_ROW_SCHEMA_V2:
        structured["locations"] = list(
            _optional_string_sequence(
                locations,
                "source_job_locations",
                max_items=100,
                max_item_length=500,
            )
        )
    if schema_version == _PUBLIC_ATS_ROW_SCHEMA_V2:
        industries = value.get("source_job_industries")
        structured["industries"] = list(
            _optional_string_sequence(
                industries,
                "source_job_industries",
                max_items=100,
                max_item_length=160,
            )
        )
        _copy_optional_text(
            structured,
            "industry",
            value.get("source_job_industry"),
            max_length=160,
        )
    for source_key, target_key in (
        ("source_job_employment_type", "employment_type"),
        ("source_job_work_mode", "work_mode"),
        ("source_job_seniority", "seniority"),
    ):
        _copy_optional_text(structured, target_key, value.get(source_key), max_length=500)
    remote = value.get("source_job_remote")
    if remote is not None:
        if not isinstance(remote, bool | str):
            raise ValueError("source_job_remote must be a boolean or string")
        if isinstance(remote, str):
            remote = _string(remote, "source_job_remote")
        structured["remote"] = remote
    for source_key, target_key in (
        ("source_job_salary", "salary"),
        ("source_job_authorization", "authorization"),
    ):
        fragment = _optional_json_object(value.get(source_key), source_key)
        if fragment is not None:
            structured[target_key] = fragment
    return PublicAtsJobRow(
        company_name=company_domain,
        company_domain=company_domain,
        source_type="public_ats",
        source_identifier=source_identifier,
        base_url=base_url,
        external_id=external_id,
        canonical_url=canonical_url,
        title=title,
        location=_optional_string(value.get("source_job_location")),
        department=_optional_string(value.get("source_job_department")),
        captured_at=discovered_at,
        parser_version=resolved_parser_version,
        source_url=canonical_url,
        structured_data=MappingProxyType(structured),
    )


def normalize_key(value: str) -> str:
    return _NON_KEY.sub(" ", value.casefold()).strip()


def normalize_url(value: str) -> str:
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        raise ValueError("url must include scheme and host")
    port = f":{parsed.port}" if parsed.port is not None else ""
    path = parsed.path.rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{host}{port}{path}{query}"


def sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _base_url(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("canonical_url must include scheme and host")
    return f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}"


def _public_ats_schema_version(value: object) -> int:
    if value is None:
        return _PUBLIC_ATS_ROW_SCHEMA_V1
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value not in _PUBLIC_ATS_ROW_SCHEMAS
    ):
        raise ValueError("schema_version is unsupported")
    return value


def _copy_optional_text(
    target: dict[str, object],
    key: str,
    value: object,
    *,
    max_length: int,
) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a nonempty string when present")
    normalized = _SPACE.sub(" ", value.strip())
    _validate_text(normalized, key, max_length=max_length)
    target[key] = normalized


def _copy_optional_url(target: dict[str, object], key: str, value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a nonempty URL when present")
    _validate_url(value.strip(), key)
    normalized = normalize_url(value.strip())
    target[key] = normalized


def _optional_string_sequence(
    value: object,
    field: str,
    *,
    max_items: int,
    max_item_length: int,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValueError(f"{field} must be an array")
    items = cast("Sequence[object]", value)
    if len(items) > max_items:
        raise ValueError(f"{field} exceeds its item limit")
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} items must be nonempty strings")
        normalized = _SPACE.sub(" ", item.strip())
        _validate_text(normalized, field, max_length=max_item_length)
        identity = normalized.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        result.append(normalized)
    return tuple(result)


def _optional_json_object(value: object, field: str) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    raw = cast("dict[object, object]", value)
    if any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{field} keys must be strings")
    result = {cast("str", key): item for key, item in raw.items()}
    try:
        json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain only JSON values") from exc
    return result


def _coerce_datetime(value: object, *, fallback: datetime | None) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif fallback is not None:
        result = fallback
    else:
        raise ValueError("captured_at or discovered_at is required")
    _validate_aware(result, "captured_at")
    return result.astimezone(UTC)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return _SPACE.sub(" ", value.strip())


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return _SPACE.sub(" ", value.strip())


def _validate_text(value: str, field: str, *, max_length: int) -> None:
    if not 1 <= len(value.strip()) <= max_length:
        raise ValueError(f"{field} must be nonempty and bounded")


def _validate_token(value: str, field: str) -> None:
    if _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded token")


def _validate_domain(value: str) -> None:
    if _DOMAIN.fullmatch(value.lower()) is None:
        raise ValueError("company_domain must be a registrable domain")


def _validate_url(value: str, field: str) -> None:
    try:
        normalized = normalize_url(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an http(s) URL") from exc
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{field} must be an http(s) URL")
    raw = urlparse(value)
    if raw.username is not None or raw.password is not None:
        raise ValueError(f"{field} must not contain credentials")


def _validate_sha256(value: str, field: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase sha256 digest")


def _validate_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


__all__ = [
    "CanonicalJobDedupePolicy",
    "CanonicalJobEvidence",
    "CanonicalJobIngestionError",
    "CanonicalJobIngestionRecord",
    "CanonicalJobIngestionResult",
    "CanonicalJobIngestionService",
    "CanonicalJobIngestionStore",
    "CrawlerRunProvenance",
    "PublicAtsJobRow",
    "normalize_key",
    "normalize_url",
    "public_ats_row_from_mapping",
    "sha256_json",
]
