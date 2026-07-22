from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue

from careerops.application.gmail_send import (
    GMAIL_SEND_CHANNEL,
    GmailSendAttachmentRef,
    GmailSendPayload,
)

GOAL_RUN_COMPOSITION_VERSION = "goal-run-composition.v1"
GOAL_RUN_REVIEW_KIND = "goal_run_application_review.v1"
_SUPPORTED_CHANNELS = frozenset({GMAIL_SEND_CHANNEL})
_EMAIL = re.compile(r"^[^@\s]{1,160}@[^@\s]{1,160}\.[^@\s]{2,40}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_MAX_TEXT = 8_000


@dataclass(frozen=True, slots=True)
class GoalRunCompositionConfig:
    candidate_id: UUID
    sender_email: str
    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    min_score: float
    selected_application_id: str
    applications: tuple[GoalRunApplicationSpec, ...]

    def __post_init__(self) -> None:
        _validate_email(self.sender_email, "sender_email")
        object.__setattr__(
            self,
            "include_keywords",
            _normalize_keywords(self.include_keywords, "include_keywords"),
        )
        object.__setattr__(
            self,
            "exclude_keywords",
            _normalize_keywords(self.exclude_keywords, "exclude_keywords", allow_empty=True),
        )
        if not 0 <= self.min_score <= 1:
            raise ValueError("min_score must be between 0 and 1")
        _validate_identifier(self.selected_application_id, "selected_application_id")
        if not self.applications:
            raise ValueError("applications must include at least one application")
        application_ids = [application.application_id for application in self.applications]
        if len(set(application_ids)) != len(application_ids):
            raise ValueError("application_id values must be unique")
        selected = [
            application
            for application in self.applications
            if application.application_id == self.selected_application_id
        ]
        if len(selected) != 1:
            raise ValueError("selected_application_id must identify exactly one application")


@dataclass(frozen=True, slots=True)
class DiscoveredGoalRunJob:
    source_id: str
    discovered_job_id: str
    company_name: str
    title: str
    canonical_url: str
    description: str
    keywords: tuple[str, ...]
    evidence_sha256: str

    def __post_init__(self) -> None:
        _validate_identifier(self.source_id, "job source_id")
        _validate_identifier(self.discovered_job_id, "discovered_job_id")
        _validate_bounded_text(self.company_name, "company_name")
        _validate_bounded_text(self.title, "title")
        _validate_bounded_text(self.canonical_url, "canonical_url")
        if not self.canonical_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("canonical_url must be HTTPS or local test URL")
        _validate_bounded_text(self.description, "description")
        object.__setattr__(
            self,
            "keywords",
            _normalize_keywords(self.keywords, "job keywords", allow_empty=True),
        )
        _validate_hash(self.evidence_sha256, "evidence_sha256")

    def haystack(self) -> str:
        return " ".join(
            (
                self.company_name,
                self.title,
                self.canonical_url,
                self.description,
                " ".join(self.keywords),
            )
        ).lower()

    def review_safe(self) -> Mapping[str, JsonValue]:
        return _freeze_mapping(
            {
                "source_id": self.source_id,
                "discovered_job_id": self.discovered_job_id,
                "company_name": self.company_name,
                "title": self.title,
                "canonical_url": self.canonical_url,
                "keywords": list(self.keywords),
                "evidence_sha256": self.evidence_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class GoalRunApplicationSpec:
    application_id: str
    channel: str
    job: DiscoveredGoalRunJob
    recipient_email: str
    subject: str
    text_body: str
    attachment_refs: tuple[GmailSendAttachmentRef, ...]
    thread_id: str | None = None
    in_reply_to_message_id: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.application_id, "application_id")
        if self.channel not in _SUPPORTED_CHANNELS:
            raise ValueError("unsupported application channel")
        _validate_email(self.recipient_email, "recipient_email")
        _validate_bounded_text(self.subject, "subject")
        _validate_bounded_text(self.text_body, "text_body")
        if not self.attachment_refs:
            raise ValueError("application requires at least one reviewed attachment")
        if (self.thread_id is None) is not (self.in_reply_to_message_id is None):
            raise ValueError("thread_id and in_reply_to_message_id must be supplied together")


@dataclass(frozen=True, slots=True)
class GoalRunMatchResult:
    application_id: str
    score: float
    matched_keywords: tuple[str, ...]
    excluded_keywords: tuple[str, ...]
    reason_codes: tuple[str, ...]

    @property
    def selectable(self) -> bool:
        return not self.excluded_keywords

    def canonical(self) -> Mapping[str, JsonValue]:
        return _freeze_mapping(
            {
                "application_id": self.application_id,
                "score": self.score,
                "matched_keywords": list(self.matched_keywords),
                "excluded_keywords": list(self.excluded_keywords),
                "reason_codes": list(self.reason_codes),
            }
        )


@dataclass(frozen=True, slots=True)
class GoalRunCompositionIdentity:
    review_item_id: UUID
    authorization_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    policy_decision_id: UUID
    review_snapshot_sha256: str
    match_snapshot_sha256: str
    reservation_key: str
    reconciliation_key: str


@dataclass(frozen=True, slots=True)
class ComposedGoalRunApplication:
    config: GoalRunCompositionConfig
    ranked_matches: tuple[GoalRunMatchResult, ...]
    selected_application: GoalRunApplicationSpec
    selected_match: GoalRunMatchResult
    gmail_payload: GmailSendPayload
    match_snapshot: MappingProxyType[str, JsonValue]
    review_payload: MappingProxyType[str, JsonValue]
    identity: GoalRunCompositionIdentity


class GoalRunApplicationComposer:
    """Pure domain composer for the G014 human-reviewed execution chain.

    The composer accepts already-reviewed local GoalRun context and returns immutable
    payloads, snapshots, and deterministic identities. It performs no network, database,
    browser, credential, or outbox IO.
    """

    def compose(self, context: Mapping[str, object]) -> ComposedGoalRunApplication:
        return self.compose_config(parse_goal_run_composition_context(context))

    def compose_config(
        self,
        config: GoalRunCompositionConfig,
    ) -> ComposedGoalRunApplication:
        """Compose an already-validated configuration assembled from durable discovery rows."""

        ranked = rank_goal_run_applications(config)
        selected = _selected_application(config)
        selected_match = _selected_match(ranked, config.selected_application_id)
        if selected_match.excluded_keywords:
            raise ValueError("selected application is blocked by exclude_keywords")
        if selected_match.score < config.min_score:
            raise ValueError("selected application does not meet min_score")
        gmail_payload = GmailSendPayload(
            sender=config.sender_email,
            recipient=selected.recipient_email,
            subject=selected.subject,
            text_body=selected.text_body,
            attachment_refs=selected.attachment_refs,
            thread_id=selected.thread_id,
            in_reply_to_message_id=selected.in_reply_to_message_id,
        )
        match_snapshot = build_match_snapshot(config, ranked)
        review_payload = build_review_payload(
            config=config,
            selected=selected,
            selected_match=selected_match,
            ranked=ranked,
            gmail_payload=gmail_payload,
            match_snapshot=match_snapshot,
        )
        review_snapshot_sha256 = canonical_json_hash(review_payload)
        match_snapshot_sha256 = canonical_json_hash(match_snapshot)
        identity = GoalRunCompositionIdentity(
            review_item_id=stable_uuid("goal-run-review-item", review_snapshot_sha256),
            authorization_id=stable_uuid("goal-run-authorization", review_snapshot_sha256),
            action_intent_id=stable_uuid("goal-run-action-intent", gmail_payload.payload_hash),
            payload_version_id=stable_uuid("goal-run-payload-version", gmail_payload.payload_hash),
            policy_decision_id=stable_uuid("goal-run-policy-decision", review_snapshot_sha256),
            review_snapshot_sha256=review_snapshot_sha256,
            match_snapshot_sha256=match_snapshot_sha256,
            reservation_key=stable_key(
                "goal-run-channel-reservation",
                config.candidate_id,
                selected.application_id,
                gmail_payload.payload_hash,
                gmail_payload.target_hash,
            ),
            reconciliation_key=stable_key(
                "goal-run-channel-reconciliation",
                config.candidate_id,
                selected.application_id,
                gmail_payload.payload_hash,
                gmail_payload.per_recipient_cap_key,
            ),
        )
        return ComposedGoalRunApplication(
            config=config,
            ranked_matches=ranked,
            selected_application=selected,
            selected_match=selected_match,
            gmail_payload=gmail_payload,
            match_snapshot=match_snapshot,
            review_payload=review_payload,
            identity=identity,
        )


def parse_goal_run_composition_context(context: Mapping[str, object]) -> GoalRunCompositionConfig:
    _reject_extra_keys(context, {"composition"}, "goal run context")
    raw_composition = context.get("composition")
    if not isinstance(raw_composition, Mapping):
        raise ValueError("goal run context requires composition object")
    composition = cast("Mapping[str, object]", raw_composition)
    _reject_extra_keys(
        composition,
        {
            "version",
            "candidate_id",
            "sender_email",
            "include_keywords",
            "exclude_keywords",
            "min_score",
            "selected_application_id",
            "applications",
        },
        "composition",
    )
    if composition.get("version") != GOAL_RUN_COMPOSITION_VERSION:
        raise ValueError("unsupported composition version")
    raw_applications_value = composition.get("applications")
    if not isinstance(raw_applications_value, Sequence) or isinstance(
        raw_applications_value, str | bytes
    ):
        raise ValueError("applications must be a list")
    raw_applications = cast("Sequence[object]", raw_applications_value)
    applications = tuple(_parse_application(item) for item in raw_applications)
    return GoalRunCompositionConfig(
        candidate_id=_uuid(composition.get("candidate_id"), "candidate_id"),
        sender_email=_required_str(composition, "sender_email"),
        include_keywords=_string_tuple(composition.get("include_keywords"), "include_keywords"),
        exclude_keywords=_string_tuple(
            composition.get("exclude_keywords", ()),
            "exclude_keywords",
            allow_empty=True,
        ),
        min_score=_float(composition.get("min_score"), "min_score"),
        selected_application_id=_required_str(composition, "selected_application_id"),
        applications=applications,
    )


def rank_goal_run_applications(
    config: GoalRunCompositionConfig,
) -> tuple[GoalRunMatchResult, ...]:
    results = tuple(_score_application(config, application) for application in config.applications)
    return tuple(
        sorted(
            results,
            key=lambda item: (
                item.excluded_keywords != (),
                -item.score,
                item.application_id,
            ),
        )
    )


def build_match_snapshot(
    config: GoalRunCompositionConfig,
    ranked: Sequence[GoalRunMatchResult],
) -> MappingProxyType[str, JsonValue]:
    return _freeze_mapping(
        {
            "version": "goal-run-match-snapshot.v1",
            "candidate_id": str(config.candidate_id),
            "include_keywords": list(config.include_keywords),
            "exclude_keywords": list(config.exclude_keywords),
            "min_score": config.min_score,
            "selected_application_id": config.selected_application_id,
            "ranked_matches": [match.canonical() for match in ranked],
        }
    )


def build_review_payload(
    *,
    config: GoalRunCompositionConfig,
    selected: GoalRunApplicationSpec,
    selected_match: GoalRunMatchResult,
    ranked: Sequence[GoalRunMatchResult],
    gmail_payload: GmailSendPayload,
    match_snapshot: Mapping[str, JsonValue],
) -> MappingProxyType[str, JsonValue]:
    return _freeze_mapping(
        {
            "version": GOAL_RUN_REVIEW_KIND,
            "locale": "zh-CN",
            "summary_zh": (
                f"请审核是否向 {selected.job.company_name} 的 {selected.job.title} "
                "发送这份求职邮件。通过后系统只会执行此精确 payload。"
            ),
            "required_human_action_zh": "只需确认: 岗位、收件人、正文和附件是否正确。",
            "selected_application_id": selected.application_id,
            "channel": selected.channel,
            "job": selected.job.review_safe(),
            "match": selected_match.canonical(),
            "ranked_matches": [match.canonical() for match in ranked],
            "email": {
                "sender": _redact_email_domain(config.sender_email),
                "recipient": selected.recipient_email,
                "subject": gmail_payload.subject,
                "text_body": gmail_payload.text_body,
                "message_id_header": gmail_payload.message_id_header,
            },
            "attachments": [
                {
                    "filename": ref.filename,
                    "content_type": ref.content_type,
                    "size_bytes": ref.size_bytes,
                    "sha256": ref.sha256,
                }
                for ref in gmail_payload.attachment_refs
            ],
            "exact_payload": {
                "payload_hash": gmail_payload.payload_hash,
                "target_hash": gmail_payload.target_hash,
                "grant_material_hash": gmail_payload.grant_material_hash,
                "subject_sha256": _sha256_text(gmail_payload.subject),
                "body_sha256": gmail_payload.body_sha256,
                "recipient_sha256": gmail_payload.recipient_sha256,
                "attachment_manifest_sha256": canonical_json_hash(
                    [ref.canonical() for ref in gmail_payload.attachment_refs]
                ),
            },
            "snapshots": {
                "match_snapshot_sha256": canonical_json_hash(match_snapshot),
            },
            "safety": {
                "contains_credentials": False,
                "external_io_performed": False,
                "human_review_required": True,
                "model_output_is_not_execution_authority": True,
            },
        }
    )


def stable_uuid(label: str, *parts: object) -> UUID:
    return uuid5(NAMESPACE_URL, stable_key(label, *parts))


def stable_key(label: str, *parts: object) -> str:
    return canonical_json_hash({"label": label, "parts": [str(part) for part in parts]})


def canonical_json_hash(value: object) -> str:
    try:
        encoded = json.dumps(
            _materialize_json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("value must be JSON-canonicalizable") from error
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_application(value: object) -> GoalRunApplicationSpec:
    if not isinstance(value, Mapping):
        raise ValueError("application must be an object")
    application = cast("Mapping[str, object]", value)
    _reject_extra_keys(
        application,
        {
            "application_id",
            "channel",
            "job",
            "recipient_email",
            "subject",
            "text_body",
            "attachment_refs",
            "thread_id",
            "in_reply_to_message_id",
        },
        "application",
    )
    raw_attachments_value = application.get("attachment_refs")
    if not isinstance(raw_attachments_value, Sequence) or isinstance(
        raw_attachments_value, str | bytes
    ):
        raise ValueError("attachment_refs must be a list")
    raw_attachments = cast("Sequence[object]", raw_attachments_value)
    return GoalRunApplicationSpec(
        application_id=_required_str(application, "application_id"),
        channel=_required_str(application, "channel"),
        job=_parse_job(application.get("job")),
        recipient_email=_required_str(application, "recipient_email"),
        subject=_required_str(application, "subject"),
        text_body=_required_str(application, "text_body"),
        attachment_refs=tuple(_parse_attachment(item) for item in raw_attachments),
        thread_id=_optional_str(application.get("thread_id"), "thread_id"),
        in_reply_to_message_id=_optional_str(
            application.get("in_reply_to_message_id"),
            "in_reply_to_message_id",
        ),
    )


def _parse_job(value: object) -> DiscoveredGoalRunJob:
    if not isinstance(value, Mapping):
        raise ValueError("job must be an object")
    job = cast("Mapping[str, object]", value)
    _reject_extra_keys(
        job,
        {
            "source_id",
            "discovered_job_id",
            "company_name",
            "title",
            "canonical_url",
            "description",
            "keywords",
            "evidence_sha256",
        },
        "job",
    )
    return DiscoveredGoalRunJob(
        source_id=_required_str(job, "source_id"),
        discovered_job_id=_required_str(job, "discovered_job_id"),
        company_name=_required_str(job, "company_name"),
        title=_required_str(job, "title"),
        canonical_url=_required_str(job, "canonical_url"),
        description=_required_str(job, "description"),
        keywords=_string_tuple(job.get("keywords", ()), "job keywords", allow_empty=True),
        evidence_sha256=_required_str(job, "evidence_sha256"),
    )


def _parse_attachment(value: object) -> GmailSendAttachmentRef:
    if not isinstance(value, Mapping):
        raise ValueError("attachment must be an object")
    attachment = cast("Mapping[str, object]", value)
    _reject_extra_keys(
        attachment,
        {"object_key", "filename", "content_type", "size_bytes", "sha256"},
        "attachment",
    )
    return GmailSendAttachmentRef(
        object_key=_required_str(attachment, "object_key"),
        filename=_required_str(attachment, "filename"),
        content_type=_required_str(attachment, "content_type"),
        size_bytes=_int(attachment.get("size_bytes"), "size_bytes"),
        sha256=_required_str(attachment, "sha256"),
    )


def _score_application(
    config: GoalRunCompositionConfig,
    application: GoalRunApplicationSpec,
) -> GoalRunMatchResult:
    haystack = application.job.haystack()
    matched = tuple(keyword for keyword in config.include_keywords if keyword in haystack)
    excluded = tuple(keyword for keyword in config.exclude_keywords if keyword in haystack)
    score = 0.0 if excluded else round(len(matched) / len(config.include_keywords), 4)
    reason_codes: list[str] = []
    if excluded:
        reason_codes.append("EXCLUDE_KEYWORD_MATCH")
    if score >= config.min_score and not excluded:
        reason_codes.append("MEETS_MIN_SCORE")
    else:
        reason_codes.append("BELOW_MIN_SCORE")
    if score == 1:
        reason_codes.append("ALL_INCLUDE_KEYWORDS_MATCHED")
    elif matched:
        reason_codes.append("PARTIAL_INCLUDE_KEYWORD_MATCH")
    else:
        reason_codes.append("NO_INCLUDE_KEYWORD_MATCH")
    return GoalRunMatchResult(
        application_id=application.application_id,
        score=score,
        matched_keywords=matched,
        excluded_keywords=excluded,
        reason_codes=tuple(reason_codes),
    )


def _selected_application(config: GoalRunCompositionConfig) -> GoalRunApplicationSpec:
    for application in config.applications:
        if application.application_id == config.selected_application_id:
            return application
    raise ValueError("selected_application_id must identify exactly one application")


def _selected_match(
    ranked: Sequence[GoalRunMatchResult],
    application_id: str,
) -> GoalRunMatchResult:
    for match in ranked:
        if match.application_id == application_id:
            return match
    raise ValueError("selected application is missing from match results")


def _reject_extra_keys(value: Mapping[str, object], allowed: set[str], label: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise ValueError(f"{label} contains unsupported keys: {', '.join(extra)}")


def _required_str(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string")
    _validate_bounded_text(item, key)
    return item


def _optional_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    _validate_bounded_text(value, label)
    return value


def _string_tuple(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError(f"{label} must be a list of strings")
    values: list[str] = []
    for item in cast("Sequence[object]", value):
        if not isinstance(item, str):
            raise ValueError(f"{label} must contain only strings")
        values.append(item)
    return _normalize_keywords(tuple(values), label, allow_empty=allow_empty)


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


def _uuid(value: object, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise ValueError(f"{label} must be a UUID")


def _float(value: object, label: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"{label} must be finite")
    return result


def _int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return value


def _validate_email(value: str, label: str) -> None:
    if _EMAIL.fullmatch(value.strip()) is None:
        raise ValueError(f"{label} must be a bounded email address")


def _validate_hash(value: str, label: str) -> None:
    if _HASH.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase sha256 hex digest")


def _validate_identifier(value: str, label: str) -> None:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded machine identifier")


def _validate_bounded_text(value: str, label: str) -> None:
    if not value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"{label} must be non-empty and bounded")
    if any(ord(character) < 32 and character not in ("\n", "\t") for character in value):
        raise ValueError(f"{label} must not contain control characters")


def _redact_email_domain(email: str) -> str:
    local, domain = email.strip().lower().split("@", 1)
    visible = local[:2] if len(local) >= 2 else local[:1]
    return f"{visible}***@{domain}"


def _freeze_mapping(value: Mapping[str, object]) -> MappingProxyType[str, JsonValue]:
    return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})


def _freeze_json_value(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return cast(JsonValue, _freeze_mapping(cast("Mapping[str, object]", value)))
    if isinstance(value, list | tuple):
        sequence = cast("Sequence[object]", value)
        return cast(JsonValue, tuple(_freeze_json_value(item) for item in sequence))
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return cast(JsonValue, value)
    raise ValueError("value is not JSON-freezable")


def _materialize_json_value(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        materialized: dict[str, JsonValue] = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            materialized[key] = _materialize_json_value(item)
        return materialized
    if isinstance(value, list | tuple):
        sequence = cast("Sequence[object]", value)
        return [_materialize_json_value(item) for item in sequence]
    if value is None or isinstance(value, str | int | float | bool):
        return cast(JsonValue, value)
    if isinstance(value, UUID):
        return str(value)
    raise ValueError("value is not JSON-canonicalizable")


__all__ = [
    "GOAL_RUN_COMPOSITION_VERSION",
    "GOAL_RUN_REVIEW_KIND",
    "ComposedGoalRunApplication",
    "DiscoveredGoalRunJob",
    "GoalRunApplicationComposer",
    "GoalRunApplicationSpec",
    "GoalRunCompositionConfig",
    "GoalRunCompositionIdentity",
    "GoalRunMatchResult",
    "build_match_snapshot",
    "build_review_payload",
    "canonical_json_hash",
    "parse_goal_run_composition_context",
    "rank_goal_run_applications",
    "stable_key",
    "stable_uuid",
]
