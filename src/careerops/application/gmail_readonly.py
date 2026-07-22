from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import cast
from uuid import UUID

from pydantic import JsonValue

_TOKEN = re.compile(r"^[A-Za-z0-9._:@/-]{1,256}$")
_HISTORY_ID = re.compile(r"^[0-9]{1,40}$")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d .()/-]{7,}\d)(?!\d)")
_GREETING_NAME = re.compile(r"\b(hi|hello|dear)\s+[A-Z][a-z]{1,40}\b", re.IGNORECASE)
_WEB_LINK = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'{}\[\]]+")
_SENSITIVE_SERVICE_LINK = re.compile(
    r"(?i)\b(?:calendly\.com|calendar\.google\.com|meet\.google\.com|zoom\.us|"
    r"teams\.microsoft\.com|hackerrank\.com|codesignal\.com|codility\.com)/"
    r"[^\s<>\"'{}\[\]]+"
)
_CURRENCY_AMOUNT = re.compile(
    r"(?ix)(?<!\w)(?:"
    r"[$€£¥₹]\s*\d[\d,.]*(?:\s?[km])?|"
    r"(?:USD|EUR|GBP|CNY|RMB|JPY|CAD|AUD|INR)\s*\d[\d,.]*(?:\s?[km])?|"
    r"\d[\d,.]*(?:\s?[km])?\s*(?:USD|EUR|GBP|CNY|RMB|JPY|CAD|AUD|INR)"
    r")(?:\s*(?:-|[\u2013\u2014]|to)\s*(?:"
    r"[$€£¥₹]\s*)?\d[\d,.]*(?:\s?[km])?)?"
)
_COMPENSATION_AMOUNT = re.compile(
    r"(?ix)\b(?:salary|compensation|base\s+pay|pay|rate|bonus)\b"
    r"\s*(?:is|of|:|=|range\s+is)?\s*"
    r"(?:[$€£¥₹]|USD|EUR|GBP|CNY|RMB|JPY|CAD|AUD|INR)?\s*"
    r"\d[\d,.]*(?:\s?[km])?"
    r"(?:\s*(?:-|[\u2013\u2014]|to)\s*(?:[$€£¥₹]\s*)?"
    r"\d[\d,.]*(?:\s?[km])?)?"
    r"(?:\s*(?:per\s+(?:year|month|hour)|/(?:yr|year|mo|month|hr|hour)))?"
)
_POSTAL_ADDRESS = re.compile(
    r"(?ix)\b\d{1,6}\s+(?:[A-Z0-9][A-Z0-9.'-]*\s+){0,8}"
    r"(?:street|st|road|rd|avenue|ave|boulevard|blvd|lane|ln|drive|dr|way|"
    r"court|ct|parkway|pkwy|place|pl|terrace|circle|highway|hwy)\b"
    r"(?:\s*,\s*[A-Z][A-Za-z .'-]{1,40}){0,2}"
    r"(?:\s*,?\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)?"
)
_PO_BOX = re.compile(r"(?i)\bP\.?\s*O\.?\s+Box\s+\d{1,10}\b")
_UK_POSTCODE = re.compile(r"(?i)\b(?:GIR\s?0AA|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})\b")
_US_POSTCODE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_CJK_POSTAL_ADDRESS = re.compile(
    r"[\u4e00-\u9fff]{1,20}(?:省|市|自治区|区|县)"
    r"[\u4e00-\u9fff\d]{1,40}(?:路|街|道|巷|弄|号)"
    r"[\u4e00-\u9fff\d-]{0,20}"
)
_NUMERIC_DATE = re.compile(
    r"\b(?:(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])|"
    r"(?:0?[1-9]|[12]\d|3[01])[-/](?:0?[1-9]|1[0-2])"
    r"(?:[-/](?:\d{2}|\d{4}))?)\b"
)
_MONTH_DATE = re.compile(
    r"(?i)\b(?:"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
    r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)(?:\s+\d{4})?"
    r")\b"
)
_WEEKDAY = re.compile(r"(?i)\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b")
_TIME_TOKEN = re.compile(
    r"(?ix)\b(?:"
    r"(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*[ap]\.?m\.?)?|"
    r"(?:1[0-2]|0?[1-9])\s*[ap]\.?m\.?)"
    r"(?:\s*(?:UTC|GMT|[ECMP][SD]T|[+-]\d{2}:?\d{2}))?\b"
)
_CONTEXTUAL_NAME = re.compile(
    r"(?i:\b(?:contact|ask\s+for|my\s+name\s+is|this\s+is|regards|sincerely)\s+)"
    r"[A-Z][A-Za-z'\u2019-]{1,30}(?:\s+[A-Z][A-Za-z'\u2019-]{1,30}){0,2}\b"
)
_TITLE_CASE_NAME = re.compile(r"\b[A-Z][a-z'\u2019-]{1,30}(?:\s+[A-Z][a-z'\u2019-]{1,30}){1,2}\b")
_NON_PERSON_TOKENS = frozenset(
    {
        "agent",
        "analyst",
        "architect",
        "assistant",
        "backend",
        "company",
        "corp",
        "corporation",
        "developer",
        "director",
        "engineer",
        "engineering",
        "group",
        "inc",
        "interview",
        "labs",
        "lead",
        "limited",
        "llc",
        "manager",
        "principal",
        "recruiter",
        "senior",
        "software",
        "solutions",
        "staff",
        "systems",
        "technologies",
    }
)
_RAW_BODY_HEADERS = frozenset({"body", "text", "html", "payload.body", "raw"})
_RECRUITING_DOMAINS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workday.com")

# Review proposals persist only these bounded, redacted metadata excerpts. The
# Gmail provider may accept larger metadata values for classification, but none
# of that unbounded source text crosses the review-storage boundary.
GMAIL_REDACTION_VERSION = "gmail-metadata-redaction.v2"
GMAIL_REDACTED_SUBJECT_MAX_CHARS = 256
GMAIL_REDACTED_SENDER_MAX_CHARS = 160
GMAIL_REDACTED_SNIPPET_EXCERPT_MAX_CHARS = 512


class GmailRecruitingClassification(StrEnum):
    APPLICATION_ACKNOWLEDGEMENT = "application_acknowledgement"
    ASSESSMENT = "assessment"
    DOCUMENT_REQUEST = "document_request"
    INTERVIEW_INVITATION = "interview_invitation"
    OFFER = "offer"
    REJECTION = "rejection"
    RECRUITER_QUESTION = "recruiter_question"
    UNKNOWN_RECRUITING = "unknown_recruiting"
    UNRELATED = "unrelated"


class GmailReviewPriority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class GmailMetadataSignal:
    provider_message_id: str
    provider_thread_id: str
    provider_history_id: str
    account_subject: str
    headers: Mapping[str, str]
    label_ids: tuple[str, ...]
    snippet: str
    received_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_token(self.provider_message_id, "provider_message_id")
        _validate_token(self.provider_thread_id, "provider_thread_id")
        _validate_history_id(self.provider_history_id)
        _validate_bounded_text(self.account_subject, "account_subject", max_length=320)
        normalized_headers = _normalize_headers(self.headers)
        object.__setattr__(self, "headers", MappingProxyType(normalized_headers))
        object.__setattr__(self, "label_ids", _normalize_tokens(self.label_ids, "label_ids"))
        _validate_bounded_text(self.snippet, "snippet", max_length=2000, allow_blank=True)
        if self.received_at is not None:
            _validate_aware(self.received_at, "received_at")

    @property
    def subject(self) -> str:
        return self.headers.get("subject", "")

    @property
    def sender(self) -> str:
        return self.headers.get("from", "")


@dataclass(frozen=True, slots=True)
class GmailJobMatchCandidate:
    canonical_job_id: UUID
    company_name: str
    title: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_bounded_text(self.company_name, "company_name", max_length=200)
        _validate_bounded_text(self.title, "title", max_length=200)
        object.__setattr__(self, "aliases", _normalize_texts(self.aliases, "aliases"))


@dataclass(frozen=True, slots=True)
class GmailRedactedEvidence:
    provider_message_id: str
    provider_thread_id: str
    provider_history_id: str
    redacted_subject: str
    redacted_sender: str
    redacted_snippet: str
    label_ids: tuple[str, ...]
    received_at: datetime | None
    redaction_version: str
    content_sha256: str
    span_sha256: str


@dataclass(frozen=True, slots=True)
class GmailRecruitingDecision:
    classification: GmailRecruitingClassification
    confidence: float
    reason_codes: tuple[str, ...]
    review_priority: GmailReviewPriority
    matched_job_id: UUID | None = None
    high_risk_ambiguity: bool = False

    def __post_init__(self) -> None:
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("confidence must be in [0, 1]")
        object.__setattr__(
            self, "reason_codes", _normalize_tokens(self.reason_codes, "reason_codes")
        )

    @property
    def requires_review(self) -> bool:
        return self.classification is not GmailRecruitingClassification.UNRELATED


@dataclass(frozen=True, slots=True)
class GmailSignalReviewProposal:
    created_at: datetime
    action_kind: str
    resource_type: str
    resource_id: UUID
    idempotency_key: str
    target: MappingProxyType[str, JsonValue]
    payload: MappingProxyType[str, JsonValue]
    payload_hash: str
    review_priority: GmailReviewPriority


class GmailEvidenceRedactor:
    def redact(self, signal: GmailMetadataSignal) -> GmailRedactedEvidence:
        subject = _truncate_redacted(
            _redact_sensitive(signal.subject), GMAIL_REDACTED_SUBJECT_MAX_CHARS
        )
        sender = _truncate_redacted(
            _redact_sensitive(signal.sender), GMAIL_REDACTED_SENDER_MAX_CHARS
        )
        snippet = _truncate_redacted(
            _redact_sensitive(signal.snippet), GMAIL_REDACTED_SNIPPET_EXCERPT_MAX_CHARS
        )
        canonical = {
            "redaction_version": GMAIL_REDACTION_VERSION,
            "provider_message_id": signal.provider_message_id,
            "provider_thread_id": signal.provider_thread_id,
            "provider_history_id": signal.provider_history_id,
            "subject": subject,
            "sender": sender,
            "snippet": snippet,
            "label_ids": list(signal.label_ids),
            "received_at": signal.received_at.isoformat() if signal.received_at else None,
        }
        return GmailRedactedEvidence(
            provider_message_id=signal.provider_message_id,
            provider_thread_id=signal.provider_thread_id,
            provider_history_id=signal.provider_history_id,
            redacted_subject=subject,
            redacted_sender=sender,
            redacted_snippet=snippet,
            label_ids=signal.label_ids,
            received_at=signal.received_at,
            redaction_version=GMAIL_REDACTION_VERSION,
            content_sha256=_canonical_hash(canonical),
            span_sha256=_canonical_hash(
                {
                    "redaction_version": GMAIL_REDACTION_VERSION,
                    "subject": subject,
                    "snippet": snippet,
                }
            ),
        )


class GmailRecruitingClassifier:
    def classify(
        self,
        signal: GmailMetadataSignal,
        *,
        candidate_jobs: Sequence[GmailJobMatchCandidate],
    ) -> GmailRecruitingDecision:
        text = f"{signal.subject} {signal.snippet}".lower()
        match_text = _redact_pii(f"{signal.subject} {signal.snippet}").lower()
        sender = signal.sender.lower()
        classification, confidence, priority, reasons = _classify_text(text, sender)
        matched_job_id, match_reasons, ambiguous = _match_job(match_text, candidate_jobs)
        reasons.extend(match_reasons)
        if ambiguous:
            reasons.append("AMBIGUOUS_JOB_MATCH")
        if classification is GmailRecruitingClassification.UNRELATED:
            return GmailRecruitingDecision(
                classification=classification,
                confidence=confidence,
                reason_codes=tuple(reasons),
                review_priority=GmailReviewPriority.NORMAL,
            )
        if matched_job_id is None:
            ambiguous = True
            if "AMBIGUOUS_JOB_MATCH" not in reasons:
                reasons.append("AMBIGUOUS_JOB_MATCH")
        return GmailRecruitingDecision(
            classification=classification,
            confidence=confidence,
            reason_codes=tuple(reasons),
            review_priority=GmailReviewPriority.HIGH if ambiguous else priority,
            matched_job_id=matched_job_id,
            high_risk_ambiguity=ambiguous,
        )


class GmailSignalReviewPlanner:
    def __init__(self, redactor: GmailEvidenceRedactor | None = None) -> None:
        self._redactor = redactor or GmailEvidenceRedactor()

    def build_review_proposal(
        self,
        *,
        signal: GmailMetadataSignal,
        decision: GmailRecruitingDecision,
        actor_id: UUID,
        signal_id: UUID,
        created_at: datetime,
    ) -> GmailSignalReviewProposal | None:
        _validate_aware(created_at, "created_at")
        if not decision.requires_review:
            return None
        evidence = self._redactor.redact(signal)
        target = _freeze_mapping(
            {
                "actor_id": str(actor_id),
                "provider_message_id": evidence.provider_message_id,
                "provider_thread_id": evidence.provider_thread_id,
                "provider_history_id": evidence.provider_history_id,
                "matched_job_id": str(decision.matched_job_id) if decision.matched_job_id else None,
            }
        )
        payload = _freeze_mapping(
            {
                "version": "gmail-readonly-review.v1",
                "classification": decision.classification.value,
                "confidence": decision.confidence,
                "reason_codes": list(decision.reason_codes),
                "review_priority": decision.review_priority.value,
                "high_risk_ambiguity": decision.high_risk_ambiguity,
                "evidence": {
                    "provider_message_id": evidence.provider_message_id,
                    "provider_thread_id": evidence.provider_thread_id,
                    "provider_history_id": evidence.provider_history_id,
                    "redacted_subject": evidence.redacted_subject,
                    "redacted_sender": evidence.redacted_sender,
                    "redacted_snippet": evidence.redacted_snippet,
                    "label_ids": list(evidence.label_ids),
                    "received_at": evidence.received_at.isoformat()
                    if evidence.received_at
                    else None,
                    "redaction_version": evidence.redaction_version,
                    "content_sha256": evidence.content_sha256,
                    "span_sha256": evidence.span_sha256,
                },
            }
        )
        payload_hash = _canonical_hash({"target": target, "payload": payload})
        return GmailSignalReviewProposal(
            created_at=created_at,
            action_kind="gmail_readonly_signal_review",
            resource_type="gmail_metadata_signal",
            resource_id=signal_id,
            idempotency_key=f"gmail-readonly:{actor_id}:{signal.provider_message_id}:{payload_hash}",
            target=target,
            payload=payload,
            payload_hash=payload_hash,
            review_priority=decision.review_priority,
        )


def _classify_text(
    text: str,
    sender: str,
) -> tuple[GmailRecruitingClassification, float, GmailReviewPriority, list[str]]:
    if _contains_any(text, ("offer", "compensation", "salary", "employment agreement")):
        return (
            GmailRecruitingClassification.OFFER,
            0.97,
            GmailReviewPriority.HIGH,
            ["OFFER_KEYWORDS"],
        )
    if _contains_any(text, ("not moving forward", "regret", "unable to proceed", "rejection")):
        return (
            GmailRecruitingClassification.REJECTION,
            0.96,
            GmailReviewPriority.NORMAL,
            ["REJECTION_KEYWORDS"],
        )
    if _contains_any(text, ("interview", "schedule a call", "availability", "calendar invite")):
        return (
            GmailRecruitingClassification.INTERVIEW_INVITATION,
            0.95,
            GmailReviewPriority.HIGH,
            ["INTERVIEW_KEYWORDS"],
        )
    if _contains_any(text, ("assessment", "take-home", "take home", "coding test", "exercise")):
        return (
            GmailRecruitingClassification.ASSESSMENT,
            0.94,
            GmailReviewPriority.HIGH,
            ["ASSESSMENT_KEYWORDS"],
        )
    if _contains_any(text, ("document", "passport", "right to work", "id verification")):
        return (
            GmailRecruitingClassification.DOCUMENT_REQUEST,
            0.9,
            GmailReviewPriority.HIGH,
            ["DOCUMENT_REQUEST_KEYWORDS"],
        )
    if _contains_any(
        text, ("application received", "thanks for applying", "received your application")
    ):
        return (
            GmailRecruitingClassification.APPLICATION_ACKNOWLEDGEMENT,
            0.9,
            GmailReviewPriority.NORMAL,
            ["APPLICATION_ACKNOWLEDGEMENT_KEYWORDS"],
        )
    if _contains_any(text, ("recruiter", "salary expectations", "when are you available")):
        return (
            GmailRecruitingClassification.RECRUITER_QUESTION,
            0.84,
            GmailReviewPriority.NORMAL,
            ["RECRUITER_QUESTION_KEYWORDS"],
        )
    if _contains_any(text, ("job", "role", "candidate", "application")) or any(
        domain in sender for domain in _RECRUITING_DOMAINS
    ):
        return (
            GmailRecruitingClassification.UNKNOWN_RECRUITING,
            0.45,
            GmailReviewPriority.HIGH,
            ["RECRUITING_CONTEXT"],
        )
    return (
        GmailRecruitingClassification.UNRELATED,
        0.95,
        GmailReviewPriority.NORMAL,
        ["NO_RECRUITING_SIGNAL"],
    )


def _match_job(
    text: str,
    candidate_jobs: Sequence[GmailJobMatchCandidate],
) -> tuple[UUID | None, list[str], bool]:
    scored: list[tuple[int, GmailJobMatchCandidate]] = []
    for job in candidate_jobs:
        score = 0
        names = (job.company_name, *job.aliases)
        if any(name.lower() in text for name in names):
            score += 2
        title_tokens = {
            token for token in re.split(r"[^a-z0-9]+", job.title.lower()) if len(token) >= 4
        }
        score += min(2, sum(1 for token in title_tokens if token in text))
        if score:
            scored.append((score, job))
    if not scored:
        return None, [], bool(candidate_jobs)
    scored.sort(key=lambda item: (-item[0], item[1].company_name.lower(), item[1].title.lower()))
    best_score, best_job = scored[0]
    if len(scored) > 1 and scored[1][0] == best_score:
        return None, [], True
    reasons = ["JOB_MATCH_COMPANY_AND_TITLE"] if best_score >= 3 else ["JOB_MATCH_PARTIAL"]
    return best_job.canonical_job_id, reasons, best_score < 3


def _redact_pii(value: str) -> str:
    value = _EMAIL.sub("[email]", value)
    value = _PHONE.sub("[phone]", value)
    return _GREETING_NAME.sub(lambda match: f"{match.group(1)} [name]", value)


def _redact_sensitive(value: str) -> str:
    value = _WEB_LINK.sub("[link]", value)
    value = _SENSITIVE_SERVICE_LINK.sub("[link]", value)
    value = _COMPENSATION_AMOUNT.sub("[compensation]", value)
    value = _CURRENCY_AMOUNT.sub("[compensation]", value)
    value = _POSTAL_ADDRESS.sub("[address]", value)
    value = _PO_BOX.sub("[address]", value)
    value = _UK_POSTCODE.sub("[address]", value)
    value = _US_POSTCODE.sub("[address]", value)
    value = _CJK_POSTAL_ADDRESS.sub("[address]", value)
    value = _NUMERIC_DATE.sub("[date]", value)
    value = _MONTH_DATE.sub("[date]", value)
    value = _WEEKDAY.sub("[date]", value)
    value = _TIME_TOKEN.sub("[time]", value)
    value = _redact_pii(value)
    value = _CONTEXTUAL_NAME.sub("[name]", value)
    return _TITLE_CASE_NAME.sub(_replace_likely_person_name, value)


def _replace_likely_person_name(match: re.Match[str]) -> str:
    tokens = {token.lower() for token in re.findall(r"[A-Za-z]+", match.group(0))}
    if tokens & _NON_PERSON_TOKENS:
        return match.group(0)
    return "[name]"


def _truncate_redacted(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[: max_chars - 1]}…"


def _normalize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        key = str(name).strip().lower()
        if key in _RAW_BODY_HEADERS:
            raise ValueError("raw body headers are forbidden in Gmail metadata signals")
        _validate_bounded_text(key, "header_name", max_length=80)
        _validate_bounded_text(value, "header_value", max_length=2000, allow_blank=True)
        normalized[key] = value.strip()
    return dict(sorted(normalized.items()))


def _normalize_tokens(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip() for value in values if value and value.strip()}))
    for value in normalized:
        _validate_token(value, field_name)
    return normalized


def _normalize_texts(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip() for value in values if value and value.strip()}))
    for value in normalized:
        _validate_bounded_text(value, field_name, max_length=200)
    return normalized


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        _freeze_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> MappingProxyType[str, JsonValue]:
    frozen = _freeze_json(value)
    if not isinstance(frozen, dict):
        raise ValueError("expected JSON object")
    return MappingProxyType(cast("dict[str, JsonValue]", frozen))


def _validate_token(value: str, field_name: str) -> None:
    if _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")


def _validate_history_id(value: str) -> None:
    if _HISTORY_ID.fullmatch(value) is None:
        raise ValueError("provider_history_id must be a Gmail history id")


def _validate_bounded_text(
    value: str,
    field_name: str,
    *,
    max_length: int,
    allow_blank: bool = False,
) -> None:
    if not allow_blank and not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if len(value) > max_length:
        raise ValueError(f"{field_name} is too long")


def _validate_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _contains_any(text: str, phrases: Sequence[str]) -> bool:
    return any(phrase in text for phrase in phrases)


__all__ = [
    "GMAIL_REDACTED_SENDER_MAX_CHARS",
    "GMAIL_REDACTED_SNIPPET_EXCERPT_MAX_CHARS",
    "GMAIL_REDACTED_SUBJECT_MAX_CHARS",
    "GMAIL_REDACTION_VERSION",
    "GmailEvidenceRedactor",
    "GmailJobMatchCandidate",
    "GmailMetadataSignal",
    "GmailRecruitingClassification",
    "GmailRecruitingClassifier",
    "GmailRecruitingDecision",
    "GmailRedactedEvidence",
    "GmailReviewPriority",
    "GmailSignalReviewPlanner",
    "GmailSignalReviewProposal",
]
