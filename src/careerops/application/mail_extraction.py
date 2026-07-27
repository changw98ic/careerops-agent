"""Mail extraction: deterministic first, optional model enrichment (Section 12).

Tasks 12.2 + 12.3:

- :class:`DeterministicMailExtractor` runs FIRST and ALWAYS. It parses provider
  metadata, sender identity, date/time/timezone/deadline expressions, requested
  materials, compensation figures, and obvious outcomes using rules + regex.
  It NEVER consults a model and NEVER transitions state. It also scans for
  prompt-injection markers (instructions to reveal secrets, change recipients,
  bypass review, HTML/Unicode confusion, signature/attachment smuggling) and
  flags them — they are treated as untrusted content and never affect tools,
  policy, state, or send targets (task 12.8).

- :class:`MailModelExtractor` is a Protocol for OPTIONAL structured model
  enrichment. It receives a *minimized* input (sender, subject, bounded
  snippet — never the full raw body or attachments), returns a validated
  :class:`MailExtraction` with confidence + evidence spans + version metadata,
  and is review-only by construction. Gated by the ``MODEL_TAILORING``
  capability (default-disabled); when disabled, unavailable, or invalid the
  deterministic extraction stands.

Iron rules honored:
- Model review-only (Iron Rule 2): model output is DATA; the deterministic
  extraction is authoritative for any field the model did not populate.
- Default-deny (Iron Rule 7): model enrichment is opt-in.
- No external writes: this module computes records only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from careerops.domain.mail_intelligence import (
    LOW_CONFIDENCE_THRESHOLD,
    MAIL_MODEL_VERSION,
    MAIL_RULES_VERSION,
    EmailEvidenceSpan,
    MailCategory,
    MailExtraction,
    MailExtractionSource,
    is_high_risk_category,
    outcome_for_category,
)

__all__ = [
    "DeterministicMailExtractor",
    "MailMessageInput",
    "MailModelExtractor",
    "ModelExtractionInvalidError",
    "ModelExtractionUnavailableError",
    "compute_extraction_hash",
]


# ---------------------------------------------------------------------------
# Minimized message input
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MailMessageInput:
    """The minimized, sanitized input both extractors consume.

    The raw HTML / full body / attachment bytes MUST be reduced to a bounded
    plain-text ``body_text`` + ``snippet`` BEFORE this object is built. Only
    what the extractors need to reason about may cross into this layer; secrets,
    credentials, and unrelated mailbox content stay out (task 12.3 minimized
    input).
    """

    message_id: UUID
    thread_id: UUID | None
    account_id: UUID | None
    sender_email: str
    sender_name: str = ""
    subject: str = ""
    snippet: str = ""
    body_text: str = ""
    received_at: datetime | None = None


# ---------------------------------------------------------------------------
# Prompt-injection markers (task 12.8)
# ---------------------------------------------------------------------------

# These patterns detect injection-shaped content. They are DELIBERATELY broad:
# a match does NOT prove an attack, it only flags the message for mandatory
# review (``prompt_injection_detected = True`` forces ``review_required``). The
# patterns never trigger an action themselves — Iron Rule 2.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # Instructions to reveal secrets / credentials / tokens.
        r"\b(?:reveal|disclose|send|share|reply\s+with)\b.{0,40}\b"
        r"(?:secret|password|api\s*key|token|credential|otp|2fa)\b",
        # Instructions to change the recipient / send target.
        r"\b(?:send\s+to|reply\s+to|forward\s+to|change\s+(?:the\s+)?recipient|update\s+(?:the\s+)?recipient)\b",
        # Instructions to bypass review / policy / approval.
        r"\b(?:bypass|ignore|skip|override)\b.{0,40}\b(?:review|approval|policy|confirmation|gate)\b",
        # Instructions targeted at the model / assistant itself.
        r"\b(?:ignore (?:all |any )?(?:previous|prior) instructions|you are now|"
        r"new instructions|system prompt|disregard the above)\b",
    )
)

# Unicode-confusable zero-width / homoglyph runs that often hide injection.
_UNICODE_CONFUSABLE = re.compile(
    "[​-‏‪-‮⁠﻿]"
)


def _scan_injection(text: str) -> bool:
    """Return True if any injection marker matches the cleaned text."""
    if not text:
        return False
    if bool(_UNICODE_CONFUSABLE.search(text)):
        return True
    return any(p.search(text) for p in _INJECTION_PATTERNS)


# ---------------------------------------------------------------------------
# Deterministic category / outcome / field extraction
# ---------------------------------------------------------------------------

# Keyword -> category. Order matters: more specific buckets first so "offer
# letter" wins over a generic "interview". The matcher is title+subject+body
# aware; matches are case-insensitive whole-word-ish.
_CATEGORY_KEYWORDS: tuple[tuple[MailCategory, tuple[str, ...]], ...] = (
    (
        MailCategory.OFFER,
        ("offer letter", "we are pleased to offer", "job offer", "extend an offer"),
    ),
    (
        MailCategory.REJECTION,
        (
            "regret to inform", "not moving forward", "unfortunately",
            "position has been filled", "not selected",
        ),
    ),
    (
        MailCategory.INTERVIEW,
        (
            "interview", "invite you to interview", "schedule an interview",
            "technical screen", "onsite", "video call",
        ),
    ),
    (
        MailCategory.ASSESSMENT,
        ("assessment", "coding test", "take-home", "hackerrank", "codility", "complete the test"),
    ),
    (MailCategory.SCREENING, ("screening", "phone screen", "initial call", "recruiter call")),
    (
        MailCategory.REQUEST_MORE_INFO,
        (
            "please provide", "we need", "could you share",
            "additional information", "requested documents",
        ),
    ),
    (
        MailCategory.SALARY,
        (
            "salary", "compensation", "base salary",
            "expected salary", "compensation expectation",
        ),
    ),
    (MailCategory.VISA, ("visa", "work authorization", "sponsorship", "right to work")),
    (
        MailCategory.IDENTITY,
        (
            "identity", "passport", "id verification",
            "background check", "employment verification",
        ),
    ),
    (
        MailCategory.WITHDRAWAL,
        ("withdraw", "withdrawing your application", "cancel your application"),
    ),
    (
        MailCategory.ACKNOWLEDGEMENT,
        ("thank you for applying", "we have received your application", "acknowledge receipt"),
    ),
)

# Date + time extraction. Captures a date (YYYY-MM-DD) and an optional
# HH:MM time, plus a trailing timezone name.
_DATE_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:[ T](?P<time>\d{2}:\d{2}))?"
    r"(?:\s*\[(?P<tz>[A-Za-z_/_]+)\])?"
)
_DEADLINE_RE = re.compile(
    r"(?:deadline|respond by|please reply by|expires? on)[^0-9]{0,20}"
    r"(?P<date>\d{4}-\d{2}-\d{2})(?:[ T](?P<time>\d{2}:\d{2}))?",
    re.IGNORECASE,
)
_COMPENSATION_RE = re.compile(
    r"(?P<amount>[$¥€]\s?\d[\d,]*(?:\.\d+)?(?:\s?[KkMB])?|\d[\d,]*\s?(?:USD|EUR|GBP|CAD|AUD|K|RMB))",
)
_REQUESTED_RE = re.compile(
    r"(?:please provide|we need|could you share|please share|attach)\s+(?P<item>[^.;\n]{3,80})",
    re.IGNORECASE,
)


def _sender_domain(sender_email: str) -> str:
    if "@" not in sender_email:
        return ""
    return sender_email.split("@", 1)[1].strip().lower()


def _try_parse_dt(date: str, time: str | None, tz: str | None) -> datetime | None:
    """Parse a date[ time[ tz]] triple into a timezone-aware datetime."""
    iso = date
    iso = f"{date}T{time}" if time else f"{date}T09:00:00"
    try:
        naive = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if tz:
        try:
            aware = naive.replace(tzinfo=ZoneInfo(tz))
        except ZoneInfoNotFoundError:
            aware = naive.replace(tzinfo=None)
    else:
        aware = naive.replace(tzinfo=None)
    return aware


def _evidence(label: str, text: str, needle: str) -> EmailEvidenceSpan | None:
    """Build a span for the first occurrence of ``needle`` in ``text``."""
    if not needle or not text:
        return None
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return None
    end = min(idx + len(needle), len(text))
    return EmailEvidenceSpan(label=label, text=text[idx:end], start=idx, end=end)


def _proposed_state_for_hash(category: MailCategory) -> str:
    """Mirror of category_to_proposed_state for hashing (avoids import cycle)."""
    from careerops.domain.mail_intelligence import category_to_proposed_state

    return category_to_proposed_state(category) or ""


def compute_extraction_hash(extraction: MailExtraction) -> str:
    """Stable content hash for an extraction (idempotency key ingredient).

    Hashes the load-bearing fields that define "same extraction" so a re-run of
    deterministic + model extraction for the same message returns the existing
    proposal. Uses sha256 (matches the payload-hash style elsewhere).
    """
    import hashlib

    payload = "|".join(
        (
            extraction.category.value,
            extraction.outcome.value,
            extraction.sender_email.lower(),
            extraction.sender_domain.lower(),
            extraction.interview_at.isoformat() if extraction.interview_at else "",
            extraction.timezone,
            extraction.deadline.isoformat() if extraction.deadline else "",
            _proposed_state_for_hash(extraction.category),
            str(round(extraction.confidence, 4)),
            extraction.summary.strip(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModelExtractionUnavailableError(Exception):
    """Raised when an optional model extractor cannot produce an extraction."""


class ModelExtractionInvalidError(Exception):
    """Raised when model output fails schema validation (task 12.9).

    Carries the deterministic fallback so the service can proceed rules-only
    rather than failing the whole proposal path.
    """


# ---------------------------------------------------------------------------
# DeterministicMailExtractor
# ---------------------------------------------------------------------------


class DeterministicMailExtractor:
    """Rule-based extractor: always runs, never consults a model (task 12.2).

    Returns a :class:`MailExtraction` with ``source = RULES``. Confidence is
    derived from how many independent signals agreed on the category
    (keyword match + obvious outcome + provider metadata) so a single noisy
    match stays below the review threshold.
    """

    def extract(self, message: MailMessageInput) -> MailExtraction:
        subject = (message.subject or "").strip()
        body = (message.body_text or message.snippet or "").strip()
        haystack = "\n".join(part for part in (subject, body) if part)

        injection = _scan_injection(haystack) or _scan_injection(message.snippet)

        category = self._classify(subject, body)
        outcome = outcome_for_category(category)
        confidence = self._confidence(category, subject, body, message.sender_email)

        interview_at, tz = self._extract_when(body, prefer_first=True)
        deadline, _ = self._extract_deadline(body)
        compensation = self._extract_compensation(body)
        requested = self._extract_requested(body)
        sender_domain = _sender_domain(message.sender_email)

        spans = self._spans(category, subject, body, message.sender_email)

        summary = self._summary(category, message.sender_name or message.sender_email)

        return MailExtraction(
            category=category,
            outcome=outcome,
            sender_email=message.sender_email.lower(),
            sender_name=message.sender_name,
            sender_domain=sender_domain,
            interview_at=interview_at,
            timezone=tz,
            deadline=deadline,
            requested_materials=requested,
            compensation=compensation,
            summary=summary,
            confidence=confidence,
            evidence_spans=spans,
            source=MailExtractionSource.RULES,
            rules_version=MAIL_RULES_VERSION,
            model_version="",
            review_required=self._needs_review(category, confidence, injection),
            prompt_injection_detected=injection,
        )

    # ------------------------------------------------------------------ helpers

    def _classify(self, subject: str, body: str) -> MailCategory:
        text = f"{subject}\n{body}".lower()
        for category, keywords in _CATEGORY_KEYWORDS:
            for kw in keywords:
                if kw in text:
                    return category
        return MailCategory.UNKNOWN

    def _confidence(
        self,
        category: MailCategory,
        subject: str,
        body: str,
        sender_email: str,
    ) -> float:
        if category is MailCategory.UNKNOWN:
            return 0.3
        f"{subject}\n{body}".lower()
        signals = 0
        # subject match is a stronger signal than body match.
        for cat, keywords in _CATEGORY_KEYWORDS:
            if cat is not category:
                continue
            if any(kw in subject.lower() for kw in keywords):
                signals += 2
            if any(kw in body.lower() for kw in keywords):
                signals += 1
        if sender_email and "@" in sender_email:
            signals += 1
        # Map 0..4+ signals onto [0.55, 0.95]; a single body hit stays under the
        # 0.6 review threshold so a one-word match never auto-applies.
        return min(0.95, 0.55 + 0.1 * max(0, signals - 1))

    def _needs_review(
        self, category: MailCategory, confidence: float, injection: bool
    ) -> bool:
        if is_high_risk_category(category):
            return True
        if category is MailCategory.UNKNOWN:
            return True
        if confidence < LOW_CONFIDENCE_THRESHOLD:
            return True
        return bool(injection)

    def _extract_when(
        self, body: str, *, prefer_first: bool
    ) -> tuple[datetime | None, str]:
        matches = list(_DATE_RE.finditer(body or ""))
        if not matches:
            return None, ""
        m = matches[0] if prefer_first else matches[-1]
        dt = _try_parse_dt(m.group("date"), m.group("time"), m.group("tz"))
        return dt, m.group("tz") or ""

    def _extract_deadline(self, body: str) -> tuple[datetime | None, str]:
        m = _DEADLINE_RE.search(body or "")
        if not m:
            return None, ""
        dt = _try_parse_dt(m.group("date"), m.group("time"), None)
        return dt, ""

    def _extract_compensation(self, body: str) -> str:
        m = _COMPENSATION_RE.search(body or "")
        return m.group("amount").strip() if m else ""

    def _extract_requested(self, body: str) -> tuple[str, ...]:
        items: list[str] = []
        for m in _REQUESTED_RE.finditer(body or ""):
            item = m.group("item").strip().rstrip(".,;")
            if item and item not in items:
                items.append(item)
        return tuple(items[:5])  # bounded

    def _spans(
        self,
        category: MailCategory,
        subject: str,
        body: str,
        sender_email: str,
    ) -> tuple[EmailEvidenceSpan, ...]:
        spans: list[EmailEvidenceSpan] = []
        if sender_email:
            spans.append(
                EmailEvidenceSpan(
                    label="sender",
                    text=sender_email,
                    start=0,
                    end=len(sender_email),
                )
            )
        # Find the keyword that triggered the category.
        for cat, keywords in _CATEGORY_KEYWORDS:
            if cat is not category:
                continue
            for kw in keywords:
                span = _evidence("category", body, kw) or _evidence(
                    "category", subject, kw
                )
                if span:
                    spans.append(span)
                    break
            break
        # Date / deadline spans.
        when, _ = self._extract_when(body, prefer_first=True)
        if when is not None:
            m = _DATE_RE.search(body or "")
            if m:
                spans.append(
                    EmailEvidenceSpan(
                        label="when",
                        text=m.group(0),
                        start=m.start(),
                        end=m.end(),
                    )
                )
        return tuple(spans[:6])  # bounded

    def _summary(self, category: MailCategory, who: str) -> str:
        label = category.value.replace("_", " ")
        who = who.strip()
        if who:
            return f"{label.capitalize()} from {who}"
        return label.capitalize()


# ---------------------------------------------------------------------------
# Optional model extractor (task 12.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ModelOutput:
    """Raw model output DTO before validation. Review-only DATA."""

    category: str
    confidence: float
    summary: str = ""
    interview_at: datetime | None = None
    timezone: str = ""
    deadline: datetime | None = None
    requested_materials: tuple[str, ...] = ()
    compensation: str = ""
    evidence_spans: tuple[EmailEvidenceSpan, ...] = field(default_factory=tuple)


class MailModelExtractor(Protocol):
    """Protocol for optional model enrichment (task 12.3).

    Implementations MUST:

    - accept the *minimized* :class:`MailMessageInput` (no raw HTML / full body
      / attachments beyond what the caller already sanitized);
    - return a structured, schema-validated result with confidence and evidence
      spans;
    - be review-only: the caller merges the result into the authoritative
      :class:`MailExtraction` but the model never selects a recipient, transitions
      state, or writes a trusted claim.
    """

    def extract(self, message: MailMessageInput) -> _ModelOutput: ...


def merge_model_output(
    deterministic: MailExtraction, model: object
) -> MailExtraction:
    """Merge a model result into the deterministic extraction (review-only).

    The deterministic category always wins when the model disagrees on a
    high-risk call (the model may refine fields but never downgrade a
    high-risk/low-confidence proposal into an auto-apply one). ``source`` stays
    ``MODEL`` so the UI can show that a model contributed; the version metadata
    is stamped.
    """
    category = deterministic.category
    try:
        model_category = MailCategory(str(getattr(model, "category", "")).strip())
    except ValueError:
        # Unknown model category string → keep deterministic, flag review.
        raise ModelExtractionInvalidError(
            "model returned an unknown category"
        ) from None

    # Model may sharpen the category ONLY when deterministic was UNKNOWN and the
    # model's category is in the controlled taxonomy. It may NEVER override a
    # high-risk deterministic classification.
    if (
        deterministic.category is MailCategory.UNKNOWN
        and model_category is not MailCategory.UNKNOWN
    ):
        category = model_category

    confidence = float(getattr(model, "confidence", deterministic.confidence))
    if not 0.0 <= confidence <= 1.0:
        raise ModelExtractionInvalidError("model confidence out of [0,1]")

    from careerops.domain.mail_intelligence import is_high_risk_category

    # If either source flags high-risk, the merged result stays high-risk.
    high_risk = deterministic.high_risk or is_high_risk_category(category)
    injection = deterministic.prompt_injection_detected

    return MailExtraction(
        category=category,
        outcome=outcome_for_category(category),
        sender_email=deterministic.sender_email,
        sender_name=deterministic.sender_name,
        sender_domain=deterministic.sender_domain,
        interview_at=getattr(model, "interview_at", None) or deterministic.interview_at,
        timezone=getattr(model, "timezone", "") or deterministic.timezone,
        deadline=getattr(model, "deadline", None) or deterministic.deadline,
        requested_materials=getattr(model, "requested_materials", ())
        or deterministic.requested_materials,
        compensation=getattr(model, "compensation", "") or deterministic.compensation,
        summary=(getattr(model, "summary", "") or deterministic.summary),
        confidence=confidence,
        evidence_spans=tuple(getattr(model, "evidence_spans", ()))
        or deterministic.evidence_spans,
        source=MailExtractionSource.MODEL,
        rules_version=MAIL_RULES_VERSION,
        model_version=MAIL_MODEL_VERSION,
        high_risk=high_risk,
        review_required=True if (high_risk or injection) else deterministic.review_required,
        prompt_injection_detected=injection,
    )
