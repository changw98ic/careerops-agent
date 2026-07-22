from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

GMAIL_SEND_ACTION_KIND = "send_email"
GMAIL_SEND_CHANNEL = "gmail:send"
GMAIL_SEND_EXACT_APPROVAL_KIND = "gmail_send_exact_payload"
GMAIL_SEND_TARGET_HOST = "gmail.googleapis.com"
GMAIL_SEND_ADAPTER_ID = "gmail"
GMAIL_SEND_FIXTURE_ID = "gmail-send.v1"
GMAIL_SEND_PAYLOAD_VERSION = "gmail-send-payload.v1"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:@/-]{1,256}$")
_EMAIL = re.compile(r"^[^@\s]{1,160}@[^@\s]{1,160}\.[^@\s]{2,40}$")
_CONTENT_TYPE = re.compile(r"^[A-Za-z0-9.+-]{1,80}/[A-Za-z0-9.+-]{1,80}$")
_MESSAGE_ID = re.compile(r"^<[^<>\s@]{1,160}@[^<>\s@]{1,160}>$")
_MAX_SUBJECT_CHARS = 320
_MAX_BODY_CHARS = 50_000
_MAX_ATTACHMENTS = 5
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024


class GmailSendDecisionState(StrEnum):
    RESERVE_GMAIL_SEND = "reserve_gmail_send"
    STOPPED = "stopped"


class GmailSendProviderState(StrEnum):
    NOT_STARTED = "not_started"
    SENT_CONFIRMED = "sent_confirmed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class GmailSendAttachmentRef:
    object_key: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_identifier(self.object_key, "attachment object_key")
        _validate_header_text(self.filename, "attachment filename", max_length=180)
        if "/" in self.filename or "\\" in self.filename:
            raise ValueError("attachment filename must not contain path separators")
        if _CONTENT_TYPE.fullmatch(self.content_type) is None:
            raise ValueError("attachment content_type must be a bounded MIME type")
        if self.size_bytes < 0 or self.size_bytes > _MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment size_bytes exceeds the Gmail send boundary")
        _validate_hash(self.sha256, "attachment sha256")

    def canonical(self) -> Mapping[str, object]:
        return {
            "object_key": self.object_key,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class GmailSendPayload:
    sender: str
    recipient: str
    subject: str
    text_body: str
    attachment_refs: tuple[GmailSendAttachmentRef, ...] = ()
    thread_id: str | None = None
    in_reply_to_message_id: str | None = None
    payload_hash: str = field(init=False)
    target_hash: str = field(init=False)
    recipient_sha256: str = field(init=False)
    body_sha256: str = field(init=False)
    grant_material_hash: str = field(init=False)
    per_recipient_cap_key: str = field(init=False)
    message_id_header: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_email(self.sender, "sender")
        _validate_email(self.recipient, "recipient")
        if self.sender.strip().lower() == self.recipient.strip().lower():
            raise ValueError("sender and recipient must be different")
        _validate_header_text(self.subject, "subject", max_length=_MAX_SUBJECT_CHARS)
        _validate_bounded_text(self.text_body, "text_body", max_length=_MAX_BODY_CHARS)
        refs = _normalize_attachment_refs(self.attachment_refs)
        object.__setattr__(self, "attachment_refs", refs)
        if self.thread_id is not None:
            _validate_identifier(self.thread_id, "thread_id")
        if self.in_reply_to_message_id is not None:
            _validate_message_id(self.in_reply_to_message_id, "in_reply_to_message_id")
        if (self.thread_id is None) is not (self.in_reply_to_message_id is None):
            raise ValueError("thread_id and in_reply_to_message_id must be supplied together")
        recipient_sha256 = _sha256_text(self.recipient.strip().lower())
        body_sha256 = _sha256_text(self.text_body)
        payload_hash = _canonical_hash(self.canonical_without_hash())
        grant_material_hash = _canonical_hash(
            {
                "version": "gmail-send-grant-material.v1",
                "payload_hash": payload_hash,
                "body_sha256": body_sha256,
                "attachment_sha256": [ref.sha256 for ref in refs],
            }
        )
        target_hash = _canonical_hash(
            _canonical_target(
                recipient_sha256=recipient_sha256,
                body_sha256=body_sha256,
                attachment_sha256=tuple(ref.sha256 for ref in refs),
                grant_material_hash=grant_material_hash,
            )
        )
        object.__setattr__(self, "recipient_sha256", recipient_sha256)
        object.__setattr__(self, "body_sha256", body_sha256)
        object.__setattr__(self, "payload_hash", payload_hash)
        object.__setattr__(self, "target_hash", target_hash)
        object.__setattr__(self, "grant_material_hash", grant_material_hash)
        object.__setattr__(self, "per_recipient_cap_key", recipient_sha256)
        object.__setattr__(
            self,
            "message_id_header",
            f"<careerops.{payload_hash[:48]}@gmail-send.careerops.local>",
        )

    def canonical_without_hash(self) -> Mapping[str, object]:
        return {
            "version": GMAIL_SEND_PAYLOAD_VERSION,
            "sender": self.sender.strip().lower(),
            "recipient": self.recipient.strip().lower(),
            "recipient_sha256": _sha256_text(self.recipient.strip().lower()),
            "subject": self.subject,
            "text_body": self.text_body,
            "body_sha256": _sha256_text(self.text_body),
            "attachment_refs": [ref.canonical() for ref in self.attachment_refs],
            "thread_id": self.thread_id,
            "in_reply_to_message_id": self.in_reply_to_message_id,
        }

    def canonical_target(self) -> Mapping[str, object]:
        return _canonical_target(
            recipient_sha256=self.recipient_sha256,
            body_sha256=self.body_sha256,
            attachment_sha256=tuple(ref.sha256 for ref in self.attachment_refs),
            grant_material_hash=self.grant_material_hash,
        )


@dataclass(frozen=True, slots=True)
class GmailSendAuthority:
    campaign_id: UUID
    grant_version_id: UUID
    authorization_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    policy_decision_id: UUID
    action_kind: str
    channel: str
    target_host: str
    approval_kind: str
    approved_payload_hash: str
    approved_target_hash: str
    approved_grant_material_hash: str
    active_grant: bool
    authorized_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _validate_identifier(self.action_kind, "action_kind")
        _validate_identifier(self.channel, "channel")
        _validate_identifier(self.target_host, "target_host")
        _validate_identifier(self.approval_kind, "approval_kind")
        _validate_hash(self.approved_payload_hash, "approved_payload_hash")
        _validate_hash(self.approved_target_hash, "approved_target_hash")
        _validate_hash(self.approved_grant_material_hash, "approved_grant_material_hash")
        _validate_aware(self.authorized_at, "authorized_at")
        _validate_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.authorized_at:
            raise ValueError("Gmail send authority must expire after authorization")


@dataclass(frozen=True, slots=True)
class QualifiedGmailSendRequest:
    authority: GmailSendAuthority
    payload: GmailSendPayload
    now: datetime
    global_kill_switch_active: bool = False
    campaign_kill_switch_active: bool = False
    provider_kill_switch_active: bool = False
    generic_approval_present: bool = False
    model_output_approval_present: bool = False
    provider_state: GmailSendProviderState = GmailSendProviderState.NOT_STARTED

    def __post_init__(self) -> None:
        _validate_aware(self.now, "now")


@dataclass(frozen=True, slots=True)
class GmailSendDecision:
    state: GmailSendDecisionState
    reason_codes: tuple[str, ...]
    reservation_key: str | None = None
    outbox_event_key: str | None = None
    reconciliation_key: str | None = None
    message_id_header: str | None = None

    @property
    def can_reserve(self) -> bool:
        return self.state is GmailSendDecisionState.RESERVE_GMAIL_SEND


class GmailSendPlanner:
    """Fail-closed planner for a reviewed Gmail send reservation.

    This domain layer does not resolve credentials or call Gmail. It only proves
    that an immutable payload hash is bound to exact human authority before a
    later outbox worker may reserve and execute a send.
    """

    def plan(self, request: QualifiedGmailSendRequest) -> GmailSendDecision:
        authority = request.authority
        payload = request.payload
        reasons: list[str] = []
        if authority.action_kind != GMAIL_SEND_ACTION_KIND:
            reasons.append("ACTION_KIND_MUST_BE_SEND_EMAIL")
        if authority.channel != GMAIL_SEND_CHANNEL:
            reasons.append("CHANNEL_MUST_BE_GMAIL_SEND")
        if authority.target_host != GMAIL_SEND_TARGET_HOST:
            reasons.append("TARGET_HOST_MUST_BE_GMAIL_API")
        if authority.approval_kind != GMAIL_SEND_EXACT_APPROVAL_KIND:
            reasons.append("EXACT_GMAIL_SEND_APPROVAL_REQUIRED")
        if authority.approved_payload_hash != payload.payload_hash:
            reasons.append("APPROVED_PAYLOAD_HASH_MISMATCH")
        if authority.approved_target_hash != payload.target_hash:
            reasons.append("APPROVED_TARGET_HASH_MISMATCH")
        if authority.approved_grant_material_hash != payload.grant_material_hash:
            reasons.append("APPROVED_GRANT_MATERIAL_HASH_MISMATCH")
        if not authority.active_grant:
            reasons.append("ACTIVE_GRANT_REQUIRED")
        if request.global_kill_switch_active:
            reasons.append("GLOBAL_KILL_SWITCH_ACTIVE")
        if request.campaign_kill_switch_active:
            reasons.append("CAMPAIGN_KILL_SWITCH_ACTIVE")
        if request.provider_kill_switch_active:
            reasons.append("PROVIDER_KILL_SWITCH_ACTIVE")
        if request.generic_approval_present:
            reasons.append("GENERIC_APPROVAL_NOT_EXECUTION_AUTHORITY")
        if request.model_output_approval_present:
            reasons.append("MODEL_OUTPUT_NOT_EXECUTION_AUTHORITY")
        if request.now >= authority.expires_at:
            reasons.append("GMAIL_SEND_AUTHORITY_EXPIRED")
        if request.now < authority.authorized_at:
            reasons.append("GMAIL_SEND_PRECEDES_AUTHORIZATION")
        if request.provider_state is GmailSendProviderState.AMBIGUOUS:
            reasons.append("AMBIGUOUS_PROVIDER_STATE")
        if request.provider_state is GmailSendProviderState.SENT_CONFIRMED:
            reasons.append("ALREADY_CONFIRMED_PROVIDER_STATE")

        if reasons:
            return GmailSendDecision(
                GmailSendDecisionState.STOPPED,
                tuple(dict.fromkeys(reasons)),
            )

        reservation_key = _stable_key(
            "gmail-send-reservation",
            authority.campaign_id,
            authority.grant_version_id,
            authority.authorization_id,
            authority.action_intent_id,
            authority.payload_version_id,
            authority.policy_decision_id,
            authority.approved_payload_hash,
            authority.approved_target_hash,
            authority.approved_grant_material_hash,
            payload.per_recipient_cap_key,
            payload.message_id_header,
        )
        reconciliation_key = _stable_key(
            "gmail-send-reconciliation",
            authority.campaign_id,
            authority.grant_version_id,
            authority.action_intent_id,
            authority.approved_payload_hash,
            payload.per_recipient_cap_key,
            payload.message_id_header,
        )
        return GmailSendDecision(
            GmailSendDecisionState.RESERVE_GMAIL_SEND,
            ("GMAIL_SEND_QUALIFIED_FOR_RESERVATION",),
            reservation_key=reservation_key,
            outbox_event_key=f"gmail-send:{reservation_key}",
            reconciliation_key=reconciliation_key,
            message_id_header=payload.message_id_header,
        )


def _normalize_attachment_refs(
    refs: Sequence[GmailSendAttachmentRef],
) -> tuple[GmailSendAttachmentRef, ...]:
    if len(refs) > _MAX_ATTACHMENTS:
        raise ValueError("Gmail send payload supports at most five attachments")
    if sum(ref.size_bytes for ref in refs) > _MAX_TOTAL_ATTACHMENT_BYTES:
        raise ValueError("Gmail send payload exceeds total attachment byte boundary")
    seen: set[str] = set()
    normalized: list[GmailSendAttachmentRef] = []
    for ref in refs:
        if ref.object_key in seen:
            raise ValueError("attachment object_key must be unique")
        seen.add(ref.object_key)
        normalized.append(ref)
    return tuple(normalized)


def _stable_key(label: str, *parts: object) -> str:
    return _canonical_hash({"label": label, "parts": [str(part) for part in parts]})


def _canonical_target(
    *,
    recipient_sha256: str,
    body_sha256: str,
    attachment_sha256: Sequence[str],
    grant_material_hash: str,
) -> Mapping[str, object]:
    return {
        "version": "gmail-send-target.v1",
        "target_host": GMAIL_SEND_TARGET_HOST,
        "channel": GMAIL_SEND_CHANNEL,
        "adapter_id": GMAIL_SEND_ADAPTER_ID,
        "fixture_id": GMAIL_SEND_FIXTURE_ID,
        "recipient_sha256": recipient_sha256,
        "body_sha256": body_sha256,
        "attachment_sha256": list(attachment_sha256),
        "grant_material_hash": grant_material_hash,
    }


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        _freeze_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {
            str(key): _freeze_json(item)
            for key, item in sorted(mapping.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        sequence = cast("Sequence[object]", value)
        return [_freeze_json(item) for item in sequence]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
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


def _validate_message_id(value: str, label: str) -> None:
    if _MESSAGE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded RFC Message-ID")


def _validate_bounded_text(value: str, label: str, *, max_length: int) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be blank")
    if len(value) > max_length:
        raise ValueError(f"{label} is too long")


def _validate_header_text(value: str, label: str, *, max_length: int) -> None:
    _validate_bounded_text(value, label, max_length=max_length)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must not contain control characters")


def _validate_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = [
    "GMAIL_SEND_ACTION_KIND",
    "GMAIL_SEND_ADAPTER_ID",
    "GMAIL_SEND_CHANNEL",
    "GMAIL_SEND_EXACT_APPROVAL_KIND",
    "GMAIL_SEND_FIXTURE_ID",
    "GMAIL_SEND_PAYLOAD_VERSION",
    "GMAIL_SEND_TARGET_HOST",
    "GmailSendAttachmentRef",
    "GmailSendAuthority",
    "GmailSendDecision",
    "GmailSendDecisionState",
    "GmailSendPayload",
    "GmailSendPlanner",
    "GmailSendProviderState",
    "QualifiedGmailSendRequest",
]
