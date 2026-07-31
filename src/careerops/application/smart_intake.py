"""Candidate-owned, review-only smart form intake.

This module intentionally stops at a short-lived draft patch. It never creates
profile versions, starts agents, confirms evidence, or calls an action service.
The model sees only bounded text supplied for this request; all versioned
context references are resolved and hashed by the server before inference.
"""

# The repositories and JSONB columns are deliberately narrow seams. Dynamic
# provider JSON is validated immediately at the boundary and is never passed
# to an effectful service.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.engine import Connection

from careerops.api.errors import (
    ConflictError,
    IdempotencyKeyReusedError,
    InvalidStateError,
    NotFoundError,
    RateLimitedError,
    SmartPreviewExpiredError,
    SmartPreviewInProgressError,
    SmartPreviewNotFoundError,
    StaleSmartIntakePreviewError,
)
from careerops.application.audit import AuditActorType, AuditEventDraft
from careerops.domain.applications import ConfirmationStatus, ResumeParseStatus
from careerops.infrastructure.database.audit import PostgresAuditWriter
from careerops.infrastructure.database.schema import (
    canonical_jobs,
    evidence_items,
    job_posting_assignments,
    job_posting_versions,
    job_postings,
    profile_versions,
    resume_versions,
    smart_intake_decisions,
    smart_intake_previews,
)
from careerops.infrastructure.rate_limit import RateLimitAction, RateLimiter, hash_subject
from careerops.model_gateway.anthropic_compat import ModelInvocationError
from careerops.model_gateway.base import StructuredModelClient, StructuredModelRequest
from careerops.observability import current_trace_id

__all__ = [
    "ApplyDecisionInput",
    "SmartIntakeRequest",
    "SmartIntakeService",
    "SmartPreviewResult",
]

_SCHEMA_VERSION = "smart-intake-v1"
_POLICY_VERSION = "smart-intake-policy-v1"
_PROMPT_VERSION = "smart-intake-extract-v1"
_CAPABILITY_STATE = "smart_intake_released"
_CLAIM_SECONDS = 30
_PREVIEW_SECONDS = 30 * 60
_MAX_PROFILE_TEXT = 12_000
_MAX_INTERVIEW_TEXT = 2_000
_MAX_EVIDENCE = 50
_MAX_REASON = 240
_MAX_SOURCE_REFS = 3
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_URL_RE = re.compile(
    r"(?:\b[a-z][a-z0-9+.-]{1,31}://[^\s<>]+"
    r"|\b(?:file|data|mailto):[^\s<>]+"
    r"|\bwww\.[^\s<>]+"
    r"|\b(?:[a-z0-9](?:[a-z0-9-]{0,62}\.)+[a-z]{2,})(?::\d+)?(?:/[^\s<>]*)?)",
    flags=re.IGNORECASE,
)
_PROFILE_PATH_RE = re.compile(
    r"^(?:target_roles\[(\d+)\]\.(title|seniority|notes)|"
    r"locations\[(\d+)\]\.(name|radius_km)|"
    r"(include_keywords|exclude_keywords)\[(\d+)\])$"
)


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    input_digest: str = Field(
        validation_alias=AliasChoices("input_digest", "digest"),
        pattern=r"^[0-9a-f]{64}$",
    )
    start_offset: int = Field(
        validation_alias=AliasChoices("start_offset", "start"),
        ge=0,
        le=_MAX_PROFILE_TEXT,
    )
    end_offset: int = Field(
        validation_alias=AliasChoices("end_offset", "end"),
        ge=0,
        le=_MAX_PROFILE_TEXT,
    )


class ModelField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=80)
    value: Any = None
    value_type: Literal["string", "string_list", "integer", "number", "boolean"]
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["proposed", "unknown", "blocked"]
    reason: str = Field(default="", max_length=_MAX_REASON)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=_MAX_SOURCE_REFS)


class ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "abstained"] = "ready"
    fields: list[ModelField] = Field(default_factory=list, max_length=80)


@dataclass(frozen=True, slots=True)
class SmartIntakeRequest:
    target: Literal["profile", "interview_context"]
    text: str
    idempotency_key: str
    profile_version_id: UUID | None = None
    canonical_job_id: UUID | None = None
    job_version_id: UUID | None = None
    resume_version_id: UUID | None = None
    evidence_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class ApplyDecisionInput:
    path: str
    decision: Literal["accept", "edit", "reject", "unknown"]
    value: Any = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SmartPreviewResult:
    id: UUID
    candidate_id: UUID
    target: str
    state: str
    input_digest: str
    context_digest: str
    fields: list[dict[str, Any]]
    expires_at: datetime
    model_id: str
    prompt_version: str
    draft_patch: dict[str, Any] | None = None
    decision_set_hash: str | None = None


class CapabilityResolver(Protocol):
    def decide(self, capability: Any) -> Any: ...


class SmartIntakeService:
    """Orchestrates the preview/apply lifecycle with no persistent form writes."""

    def __init__(
        self,
        engine: sa.Engine,
        *,
        profile_repository: Any,
        job_repository: Any,
        resume_repository: Any,
        evidence_repository: Any,
        model_client: StructuredModelClient | None,
        rate_limiter: RateLimiter,
        metrics: Any | None = None,
        capability_state: str = _CAPABILITY_STATE,
    ) -> None:
        self._engine = engine
        self._profiles = profile_repository
        self._jobs = job_repository
        self._resumes = resume_repository
        self._evidence = evidence_repository
        self._model = model_client
        self._rate_limiter = rate_limiter
        self._metrics = metrics
        self._capability_state = _bounded(capability_state, 64)

    @property
    def provider_enabled(self) -> bool:
        """Expose only the bounded provider state needed by the UI capability read."""
        return bool(self._model is not None and self._model.is_enabled)

    def create_preview(
        self,
        candidate_id: UUID,
        request: SmartIntakeRequest,
        *,
        actor_id: str,
        trace_id: str | None = None,
    ) -> SmartPreviewResult:
        started_at = perf_counter()
        normalized = _normalize_text(request.text, request.target)
        if not normalized:
            raise InvalidStateError("smart intake text must not be empty")
        if not _IDEMPOTENCY_RE.fullmatch(request.idempotency_key):
            raise InvalidStateError("invalid smart intake idempotency key")
        if len(request.evidence_ids) > _MAX_EVIDENCE:
            raise InvalidStateError("too many evidence references")
        model_version = self._model_version()
        input_digest = _digest(normalized)
        context_refs, context_digest = self._resolve_context(
            candidate_id, request, model_version=model_version
        )
        fingerprint = _request_fingerprint(request, normalized, context_refs)
        existing = self._find_preview_by_key(candidate_id, request, fingerprint)
        if existing is not None:
            if _is_expired(existing):
                raise SmartPreviewExpiredError()
            if (
                existing["claim_state"] == "pending"
                and existing["claim_expires_at"]
                and _as_utc(existing["claim_expires_at"]) > datetime.now(UTC)
            ):
                raise SmartPreviewInProgressError()
            if existing["claim_state"] == "finalized":
                return _preview_result(existing)
        try:
            allowed = self._rate_limiter.check(
                RateLimitAction.SMART_INTAKE,
                hash_subject(str(candidate_id)),
                now=datetime.now(UTC),
            )
        except Exception:
            allowed = False
        if not allowed:
            raise RateLimitedError()
        trace = trace_id or current_trace_id()
        # Re-read the context and claim the preview on the same transaction
        # connection. The first read above is only an idempotency fast path;
        # it is never used as the authoritative claim context.
        with self._engine.begin() as conn:
            context_refs, context_digest = self._resolve_context(
                candidate_id,
                request,
                model_version=model_version,
                conn=conn,
            )
            fingerprint = _request_fingerprint(request, normalized, context_refs)
            reservation = self._reserve(
                candidate_id,
                request,
                normalized,
                input_digest,
                context_digest,
                context_refs,
                fingerprint,
                trace,
                conn=conn,
            )
        if isinstance(reservation, SmartPreviewResult):
            return reservation
        preview_id, claim_token = reservation

        state = "unavailable"
        model_id = "disabled"
        prompt_version = "none"
        fields: list[dict[str, Any]] = []
        try:
            if self._model is None or not self._model.is_enabled:
                state = "unavailable"
            else:
                response = self._model.invoke(
                    StructuredModelRequest(
                        task_type=f"smart_intake_{request.target}",
                        system_prompt=_system_prompt(request.target),
                        user_prompt=_user_prompt(request.target),
                        untrusted_content=normalized,
                        schema_name="smart-intake-field-proposals",
                        schema=_model_schema(request.target),
                        timeout_seconds=15.0,
                        max_tokens=768,
                        trace_id=trace,
                        metadata={"prompt_version": _PROMPT_VERSION},
                    )
                )
                model_id = _bounded(response.model_id, 128)
                prompt_version = _bounded(response.prompt_version or _PROMPT_VERSION, 64)
                self._record_model_usage(response)
                parsed = ModelOutput.model_validate(response.result)
                if parsed.status == "abstained":
                    state = "abstained"
                else:
                    state = "ready"
                    fields = _trusted_fields(
                        request.target,
                        parsed.fields,
                        normalized,
                        input_digest,
                    )
        except ModelInvocationError as exc:
            message = str(exc).lower()
            state = (
                "unavailable"
                if any(
                    marker in message
                    for marker in (
                        "network",
                        "deadline",
                        "http 401",
                        "http 403",
                        "http 404",
                        "http 429",
                        "http 5",
                        "unauthorized",
                        "forbidden",
                        "not found",
                    )
                )
                else "invalid"
            )
        except ValidationError:
            state = "invalid"
        except (TimeoutError, ConnectionError, OSError):
            state = "unavailable"
        except Exception:
            # Providers and transport adapters are intentionally opaque to the
            # API. A provider failure is a review-only unavailable outcome, and
            # no exception text or content enters logs/audit.
            state = "unavailable"

        return self._finalize(
            candidate_id,
            actor_id,
            preview_id,
            claim_token,
            request,
            fingerprint,
            normalized,
            context_refs,
            input_digest,
            context_digest,
            state,
            fields,
            model_id,
            prompt_version,
            trace,
            duration_seconds=perf_counter() - started_at,
        )

    def _record_model_usage(self, response: Any) -> None:
        recorder = getattr(self._metrics, "record_llm_tokens", None)
        if recorder is None:
            return
        try:
            recorder(
                input_tokens=max(0, int(getattr(response, "input_tokens", 0))),
                output_tokens=max(0, int(getattr(response, "output_tokens", 0))),
                component="smart_intake",
            )
        except Exception:
            # Metrics are advisory and must never change the review-only result.
            return

    def _record_preview_metrics(self, target: str, state: str, duration_seconds: float) -> None:
        record_preview = getattr(self._metrics, "record_smart_intake_preview", None)
        observe_latency = getattr(self._metrics, "observe_smart_intake_latency", None)
        if record_preview is None or observe_latency is None:
            return
        try:
            record_preview(target=target, state=state)
            observe_latency(
                target=target,
                operation="preview",
                duration_seconds=max(0.0, duration_seconds),
            )
        except Exception:
            # Metrics are advisory and must never change the review-only result.
            return

    def _record_decision_metric(self, target: str, decision: str) -> None:
        recorder = getattr(self._metrics, "record_smart_intake_decision", None)
        if recorder is None:
            return
        try:
            recorder(target=target, decision=decision)
        except Exception:
            return

    def _record_apply_latency(self, target: str, duration_seconds: float) -> None:
        recorder = getattr(self._metrics, "observe_smart_intake_latency", None)
        if recorder is None:
            return
        try:
            recorder(
                target=target,
                operation="apply",
                duration_seconds=max(0.0, duration_seconds),
            )
        except Exception:
            return

    def _find_preview_by_key(
        self,
        candidate_id: UUID,
        request: SmartIntakeRequest,
        fingerprint: str,
        *,
        conn: Connection | None = None,
    ) -> Any | None:
        def find(connection: Connection) -> Any | None:
            return (
                connection.execute(
                    sa.select(smart_intake_previews).where(
                        smart_intake_previews.c.candidate_id == candidate_id,
                        smart_intake_previews.c.target == request.target,
                        smart_intake_previews.c.idempotency_key == request.idempotency_key,
                    )
                )
                .mappings()
                .first()
            )

        if conn is None:
            with self._engine.begin() as connection:
                row = find(connection)
        else:
            row = find(conn)
        if row is not None and row["request_fingerprint"] != fingerprint:
            raise IdempotencyKeyReusedError()
        return row

    def get_preview(self, candidate_id: UUID, preview_id: UUID) -> SmartPreviewResult:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(smart_intake_previews).where(
                        smart_intake_previews.c.id == preview_id,
                        smart_intake_previews.c.candidate_id == candidate_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise SmartPreviewNotFoundError()
        if _is_expired(row):
            raise SmartPreviewExpiredError()
        return _preview_result(row)

    def apply_preview(
        self,
        candidate_id: UUID,
        preview_id: UUID,
        *,
        apply_idempotency_key: str,
        context_digest: str,
        decision_set_hash: str,
        decisions: tuple[ApplyDecisionInput, ...],
        actor_id: str,
        trace_id: str | None = None,
    ) -> SmartPreviewResult:
        started_at = perf_counter()
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(smart_intake_previews)
                    .where(
                        smart_intake_previews.c.id == preview_id,
                        smart_intake_previews.c.candidate_id == candidate_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                raise SmartPreviewNotFoundError()
            # Ownership is resolved before inspecting client-controlled hashes
            # or values so a cross-candidate ID is always indistinguishable.
            if not _IDEMPOTENCY_RE.fullmatch(apply_idempotency_key):
                raise InvalidStateError("invalid smart intake apply idempotency key")
            if len(context_digest) != 64 or any(
                c not in "0123456789abcdef" for c in context_digest
            ):
                raise InvalidStateError("invalid context_digest")
            if len(decision_set_hash) != 64 or any(
                c not in "0123456789abcdef" for c in decision_set_hash
            ):
                raise InvalidStateError("invalid decision_set_hash")
            trace = trace_id or current_trace_id()
            # The expiry tombstone wins over idempotent replay so an expired or
            # purged preview can never disclose its former field values. Replay
            # is guaranteed only while the immutable preview values are retained.
            if _is_expired(row):
                raise SmartPreviewExpiredError()

            existing = (
                conn.execute(
                    sa.select(smart_intake_decisions).where(
                        smart_intake_decisions.c.preview_id == preview_id,
                        smart_intake_decisions.c.candidate_id == candidate_id,
                        smart_intake_decisions.c.apply_idempotency_key == apply_idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            canonical_decisions = _canonical_decisions(decisions)
            computed_hash = _digest(_canonical_json(canonical_decisions))
            if computed_hash != decision_set_hash:
                raise IdempotencyKeyReusedError(
                    "decision_set_hash does not match the submitted decisions"
                )
            if existing is not None:
                if existing["decision_set_hash"] != decision_set_hash:
                    raise IdempotencyKeyReusedError()
                # Idempotent replay is resolved before stale-context checks. The
                # same request body is revalidated against the immutable preview
                # and reconstructs the local draft without storing raw values in
                # the append-only decision row.
                draft_patch = _build_patch(row["target"], row["fields"] or [], decisions)
                self._record_apply_latency(row["target"], perf_counter() - started_at)
                return _preview_result(
                    row,
                    draft_patch=draft_patch,
                    decision_set_hash=existing["decision_set_hash"],
                )
            if row["state"] != "ready":
                raise ConflictError("only a ready smart intake preview can be applied")

            _, current_digest = self._resolve_context(
                candidate_id,
                _request_from_row(row),
                model_version=self._model_version(),
                conn=conn,
            )
            if context_digest != row["context_digest"] or current_digest != row["context_digest"]:
                # Commit the non-sensitive lifecycle transition before
                # returning the conflict. The preview values remain available
                # for the user to inspect, but a second apply is now blocked.
                conn.execute(
                    sa.update(smart_intake_previews)
                    .where(
                        smart_intake_previews.c.id == preview_id,
                        smart_intake_previews.c.candidate_id == candidate_id,
                    )
                    .values(state="stale")
                )
                conn.commit()
                raise StaleSmartIntakePreviewError()

            draft_patch = _build_patch(row["target"], row["fields"] or [], decisions)
            decision_metadata = _decision_metadata(canonical_decisions, row["fields"] or [])
            conn.execute(
                sa.insert(smart_intake_decisions).values(
                    id=uuid4(),
                    preview_id=preview_id,
                    candidate_id=candidate_id,
                    apply_idempotency_key=apply_idempotency_key,
                    decision_set_hash=decision_set_hash,
                    decisions=decision_metadata,
                    draft_patch={},
                    actor_id=_bounded(actor_id, 128),
                    trace_id=_bounded(trace, 128),
                )
            )
            for item in canonical_decisions:
                self._record_decision_metric(row["target"], str(item["decision"]))
            self._record_apply_latency(row["target"], perf_counter() - started_at)
            PostgresAuditWriter(conn).append(
                AuditEventDraft(
                    event_type="smart_intake_decision_recorded",
                    actor_type=AuditActorType.USER,
                    actor_id=_bounded(actor_id, 128),
                    resource_type="smart_intake_preview",
                    resource_id=preview_id,
                    trace_id=_bounded(trace, 128),
                    event_data={
                        "candidate_id": str(candidate_id),
                        "target": row["target"],
                        "input_digest": row["input_digest"],
                        "context_digest": row["context_digest"],
                        "decision_set_hash": decision_set_hash,
                        "accepted_count": sum(
                            1
                            for item in canonical_decisions
                            if item["decision"] in {"accept", "edit"}
                        ),
                        "schema_version": row["schema_version"],
                        "prompt_version": row["prompt_version"],
                        "model_id": row["model_id"],
                        "capability_state": row.get("capability_state", self._capability_state),
                    },
                )
            )
            for item, metadata in zip(canonical_decisions, decision_metadata, strict=True):
                PostgresAuditWriter(conn).append(
                    AuditEventDraft(
                        event_type="smart_intake_field_decision_recorded",
                        actor_type=AuditActorType.USER,
                        actor_id=_bounded(actor_id, 128),
                        resource_type="smart_intake_preview",
                        resource_id=preview_id,
                        trace_id=_bounded(trace, 128),
                        event_data={
                            "candidate_id": str(candidate_id),
                            "target": row["target"],
                            "input_digest": row["input_digest"],
                            "context_digest": row["context_digest"],
                            "path": _bounded(str(item["path"]), 80),
                            "decision": item["decision"],
                            "reason": _bounded(str(item["reason"]), _MAX_REASON),
                            "value_digest": metadata["value_digest"],
                            "proposal_value_digest": metadata["proposal_value_digest"],
                            "schema_version": row["schema_version"],
                            "prompt_version": row["prompt_version"],
                            "model_id": row["model_id"],
                            "capability_state": row.get("capability_state", self._capability_state),
                        },
                    )
                )
            return _preview_result(
                row,
                draft_patch=draft_patch,
                decision_set_hash=decision_set_hash,
            )

    def _reserve(
        self,
        candidate_id: UUID,
        request: SmartIntakeRequest,
        normalized: str,
        input_digest: str,
        context_digest: str,
        context_refs: dict[str, Any],
        fingerprint: str,
        trace_id: str,
        *,
        conn: Connection | None = None,
    ) -> tuple[UUID, str] | SmartPreviewResult:
        now = datetime.now(UTC)
        claim_token = _digest(f"{uuid4()}:{trace_id}")

        def reserve(connection: Connection) -> tuple[UUID, str] | SmartPreviewResult:
            # Serialize all preview claims for one candidate. This closes the
            # check-then-insert race for different idempotency keys as well as
            # the unique-key race for the same key.
            lock_key = _advisory_lock_key(candidate_id)
            if connection.dialect.name == "postgresql":
                connection.execute(
                    sa.text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
                )
            row = (
                connection.execute(
                    sa.select(smart_intake_previews)
                    .where(
                        smart_intake_previews.c.candidate_id == candidate_id,
                        smart_intake_previews.c.target == request.target,
                        smart_intake_previews.c.idempotency_key == request.idempotency_key,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is not None:
                if row["request_fingerprint"] != fingerprint:
                    raise IdempotencyKeyReusedError()
                if _is_expired(row):
                    raise SmartPreviewExpiredError()
                if (
                    row["claim_state"] == "pending"
                    and row["claim_expires_at"]
                    and _as_utc(row["claim_expires_at"]) > now
                ):
                    raise SmartPreviewInProgressError()
                if row["claim_state"] == "finalized":
                    return _preview_result(row)
                connection.execute(
                    sa.update(smart_intake_previews)
                    .where(
                        smart_intake_previews.c.id == row["id"],
                        smart_intake_previews.c.candidate_id == candidate_id,
                    )
                    .values(
                        claim_state="pending",
                        claim_token=claim_token,
                        claim_expires_at=now + timedelta(seconds=_CLAIM_SECONDS),
                        input_text=normalized,
                        state="unavailable",
                        fields=[],
                        purged_at=None,
                        revoked_at=None,
                        capability_state=self._capability_state,
                    )
                )
                return row["id"], claim_token

            inflight = connection.execute(
                sa.select(smart_intake_previews.c.id).where(
                    smart_intake_previews.c.candidate_id == candidate_id,
                    smart_intake_previews.c.claim_state == "pending",
                    smart_intake_previews.c.claim_expires_at > now,
                )
            ).first()
            if inflight is not None:
                raise SmartPreviewInProgressError()

            preview_id = uuid4()
            connection.execute(
                sa.insert(smart_intake_previews).values(
                    id=preview_id,
                    candidate_id=candidate_id,
                    target=request.target,
                    idempotency_key=request.idempotency_key,
                    request_fingerprint=fingerprint,
                    input_digest=input_digest,
                    context_digest=context_digest,
                    input_text=normalized,
                    context_refs=context_refs,
                    fields=[],
                    state="unavailable",
                    claim_state="pending",
                    claim_token=claim_token,
                    claim_expires_at=now + timedelta(seconds=_CLAIM_SECONDS),
                    schema_version=_SCHEMA_VERSION,
                    prompt_version="",
                    model_id="disabled",
                    trace_id=_bounded(trace_id, 128),
                    capability_state=self._capability_state,
                    expires_at=now + timedelta(seconds=_PREVIEW_SECONDS),
                )
            )
            return preview_id, claim_token

        if conn is not None:
            return reserve(conn)
        with self._engine.begin() as connection:
            return reserve(connection)

    def _finalize(
        self,
        candidate_id: UUID,
        actor_id: str,
        preview_id: UUID,
        claim_token: str,
        request: SmartIntakeRequest,
        fingerprint: str,
        normalized: str,
        context_refs: dict[str, Any],
        input_digest: str,
        context_digest: str,
        state: str,
        fields: list[dict[str, Any]],
        model_id: str,
        prompt_version: str,
        trace_id: str,
        duration_seconds: float,
    ) -> SmartPreviewResult:
        with self._engine.begin() as conn:
            rowcount = conn.execute(
                sa.update(smart_intake_previews)
                .where(
                    smart_intake_previews.c.id == preview_id,
                    smart_intake_previews.c.candidate_id == candidate_id,
                    smart_intake_previews.c.request_fingerprint == fingerprint,
                    smart_intake_previews.c.claim_state == "pending",
                    smart_intake_previews.c.claim_token == claim_token,
                    smart_intake_previews.c.claim_expires_at > datetime.now(UTC),
                )
                .values(
                    state=state,
                    fields=fields,
                    claim_state="finalized",
                    claim_token="",  # nosec B106 - clears an opaque lease token, not a password
                    claim_expires_at=None,
                    prompt_version=prompt_version,
                    model_id=model_id,
                )
            ).rowcount
            if rowcount != 1:
                raise SmartPreviewInProgressError()
            row = (
                conn.execute(
                    sa.select(smart_intake_previews).where(
                        smart_intake_previews.c.id == preview_id,
                        smart_intake_previews.c.candidate_id == candidate_id,
                    )
                )
                .mappings()
                .one()
            )
            PostgresAuditWriter(conn).append(
                AuditEventDraft(
                    event_type="smart_intake_preview_created",
                    actor_type=AuditActorType.USER,
                    actor_id=_bounded(actor_id, 128),
                    resource_type="smart_intake_preview",
                    resource_id=preview_id,
                    trace_id=_bounded(trace_id, 128),
                    event_data={
                        "candidate_id": str(candidate_id),
                        "target": request.target,
                        "state": state,
                        "input_digest": input_digest,
                        "context_digest": context_digest,
                        "field_count": len(fields),
                        "schema_version": row["schema_version"],
                        "prompt_version": prompt_version,
                        "model_id": model_id,
                        "capability_state": row.get("capability_state", self._capability_state),
                    },
                )
            )
            result = _preview_result(row)
            self._record_preview_metrics(request.target, state, duration_seconds)
            return result

    def _resolve_context(
        self,
        candidate_id: UUID,
        request: SmartIntakeRequest,
        *,
        model_version: str,
        conn: Connection | None = None,
    ) -> tuple[dict[str, Any], str]:
        if conn is not None:
            refs = self._resolve_context_in_connection(conn, candidate_id, request)
        elif request.target == "profile":
            profile = (
                self._profiles.get_by_version_id(candidate_id, request.profile_version_id)
                if request.profile_version_id is not None
                else self._profiles.get_active_for(candidate_id)
            )
            refs: dict[str, Any] = {
                "profile_version_id": str(profile.id) if profile is not None else None,
                "profile_rules_version": profile.rules_version if profile is not None else "none",
            }
        else:
            if not all(
                value is not None
                for value in (
                    request.canonical_job_id,
                    request.job_version_id,
                    request.resume_version_id,
                )
            ):
                raise InvalidStateError("interview context references are incomplete")
            canonical_id = cast(UUID, request.canonical_job_id)
            version_id = cast(UUID, request.job_version_id)
            resume_id = cast(UUID, request.resume_version_id)
            job = self._jobs.find_canonical_by_id(canonical_id)
            version = self._jobs.find_version_for_canonical(canonical_id, version_id)
            resume = self._resumes.find_resume_by_id(candidate_id, resume_id)
            if job is None or version is None:
                raise NotFoundError("job context is not owned or bound to the requested job")
            if resume is None:
                raise NotFoundError("resume version not found for candidate")
            if resume.parse_status is not ResumeParseStatus.PARSED:
                raise InvalidStateError("resume must be parsed before smart interview intake")
            if resume.confirmation_status is not ConfirmationStatus.CONFIRMED:
                raise InvalidStateError("resume must be confirmed before smart interview intake")
            evidence_hashes: list[dict[str, str]] = []
            for evidence_id in sorted(request.evidence_ids, key=str):
                item = self._evidence.get_by_id(candidate_id, evidence_id)
                if item.confirmation_status is not ConfirmationStatus.CONFIRMED:
                    raise InvalidStateError("smart interview intake requires confirmed evidence")
                if item.resume_version_id != resume.id:
                    raise InvalidStateError("evidence is not bound to the requested resume")
                evidence_hashes.append(
                    {"id": str(item.id), "hash": item.evidence_hash or item.content_hash}
                )
            profile = (
                self._profiles.get_by_version_id(candidate_id, request.profile_version_id)
                if request.profile_version_id is not None
                else self._profiles.get_active_for(candidate_id)
            )
            refs = {
                "canonical_job_id": str(job.id),
                "job_version_id": str(version.id),
                "job_content_hash": version.content_hash,
                "resume_version_id": str(resume.id),
                "resume_content_hash": resume.content_hash,
                "profile_version_id": str(profile.id) if profile is not None else None,
                "profile_rules_version": profile.rules_version if profile is not None else "none",
                "evidence": evidence_hashes,
            }
        digest_payload = {
            "target": request.target,
            "schema_version": _SCHEMA_VERSION,
            "policy_version": _POLICY_VERSION,
            "prompt_version": _PROMPT_VERSION,
            "model_version": model_version,
            "capability_state": self._capability_state,
            "refs": refs,
        }
        stored_refs = {**refs, "_model_version": model_version}
        digest_payload["refs"] = stored_refs
        return stored_refs, _digest(_canonical_json(digest_payload))

    def _resolve_context_in_connection(
        self,
        conn: Connection,
        candidate_id: UUID,
        request: SmartIntakeRequest,
    ) -> dict[str, Any]:
        """Read the stale-check projection on the apply transaction connection."""
        profile_id = request.profile_version_id
        profile_stmt = sa.select(
            profile_versions.c.id,
            profile_versions.c.rules_version,
        ).where(profile_versions.c.candidate_id == candidate_id)
        if profile_id is not None:
            profile_stmt = profile_stmt.where(profile_versions.c.id == profile_id)
        else:
            profile_stmt = profile_stmt.where(profile_versions.c.is_active.is_(True))
        profile_stmt = profile_stmt.with_for_update()
        profile_row = conn.execute(profile_stmt).mappings().first()
        if profile_id is not None and profile_row is None:
            raise NotFoundError("profile version not found for candidate")
        profile_ref = {
            "profile_version_id": str(profile_row["id"]) if profile_row is not None else None,
            "profile_rules_version": (
                str(profile_row["rules_version"]) if profile_row is not None else "none"
            ),
        }
        if request.target == "profile":
            return profile_ref
        if not all(
            value is not None
            for value in (
                request.canonical_job_id,
                request.job_version_id,
                request.resume_version_id,
            )
        ):
            raise InvalidStateError("interview context references are incomplete")
        canonical_id = cast(UUID, request.canonical_job_id)
        version_id = cast(UUID, request.job_version_id)
        resume_id = cast(UUID, request.resume_version_id)
        job_row = (
            conn.execute(
                sa.select(canonical_jobs.c.id)
                .where(canonical_jobs.c.id == canonical_id)
                .with_for_update()
            )
            .mappings()
            .first()
        )
        version_row = (
            conn.execute(
                sa.select(job_posting_versions.c.id, job_posting_versions.c.content_hash)
                .join(job_postings, job_postings.c.id == job_posting_versions.c.job_posting_id)
                .join(
                    job_posting_assignments,
                    job_posting_assignments.c.job_posting_id == job_postings.c.id,
                )
                .where(
                    job_posting_versions.c.id == version_id,
                    job_posting_assignments.c.canonical_job_id == canonical_id,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        resume_row = (
            conn.execute(
                sa.select(
                    resume_versions.c.id,
                    resume_versions.c.content_hash,
                    resume_versions.c.parse_status,
                    resume_versions.c.confirmation_status,
                )
                .where(
                    resume_versions.c.id == resume_id,
                    resume_versions.c.candidate_id == candidate_id,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if job_row is None or version_row is None:
            raise NotFoundError("job context is not owned or bound to the requested job")
        if resume_row is None:
            raise NotFoundError("resume version not found for candidate")
        if resume_row["parse_status"] != ResumeParseStatus.PARSED.value:
            raise InvalidStateError("resume must be parsed before smart interview intake")
        if resume_row["confirmation_status"] != ConfirmationStatus.CONFIRMED.value:
            raise InvalidStateError("resume must be confirmed before smart interview intake")
        evidence_hashes: list[dict[str, str]] = []
        for evidence_id in sorted(request.evidence_ids, key=str):
            evidence_row = (
                conn.execute(
                    sa.select(
                        evidence_items.c.id,
                        evidence_items.c.resume_version_id,
                        evidence_items.c.confirmation_status,
                        evidence_items.c.evidence_hash,
                        evidence_items.c.content_hash,
                    )
                    .where(
                        evidence_items.c.id == evidence_id,
                        evidence_items.c.candidate_id == candidate_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if evidence_row is None:
                raise NotFoundError("evidence item not found for candidate")
            if evidence_row["confirmation_status"] != ConfirmationStatus.CONFIRMED.value:
                raise InvalidStateError("smart interview intake requires confirmed evidence")
            if evidence_row["resume_version_id"] != resume_id:
                raise InvalidStateError("evidence is not bound to the requested resume")
            evidence_hashes.append(
                {
                    "id": str(evidence_row["id"]),
                    "hash": str(evidence_row["evidence_hash"] or evidence_row["content_hash"]),
                }
            )
        return {
            "canonical_job_id": str(job_row["id"]),
            "job_version_id": str(version_row["id"]),
            "job_content_hash": str(version_row["content_hash"]),
            "resume_version_id": str(resume_row["id"]),
            "resume_content_hash": str(resume_row["content_hash"]),
            **profile_ref,
            "evidence": evidence_hashes,
        }

    def _model_version(self) -> str:
        if self._model is None or not self._model.is_enabled:
            return "disabled"
        return _bounded(str(getattr(self._model, "model_id", "enabled")), 128)

    def revoke_unapplied(self, *, actor_id: str = "smart-intake-rollback") -> int:
        """Revoke and purge all unapplied previews during capability rollback."""
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    sa.select(
                        smart_intake_previews.c.id,
                        smart_intake_previews.c.candidate_id,
                        smart_intake_previews.c.target,
                    )
                    .where(
                        smart_intake_previews.c.revoked_at.is_(None),
                        smart_intake_previews.c.purged_at.is_(None),
                        ~sa.exists(
                            sa.select(smart_intake_decisions.c.id).where(
                                smart_intake_decisions.c.preview_id == smart_intake_previews.c.id,
                                smart_intake_decisions.c.candidate_id
                                == smart_intake_previews.c.candidate_id,
                            )
                        ),
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            for row in rows:
                conn.execute(
                    sa.update(smart_intake_previews)
                    .where(
                        smart_intake_previews.c.id == row["id"],
                        smart_intake_previews.c.candidate_id == row["candidate_id"],
                    )
                    .values(
                        input_text="",
                        context_refs={},
                        fields=[],
                        state="revoked",
                        claim_state="finalized",
                        claim_token="",  # nosec B106 - clears an opaque lease token, not a password
                        claim_expires_at=None,
                        revoked_at=now,
                        purged_at=now,
                    )
                )
                PostgresAuditWriter(conn).append(
                    AuditEventDraft(
                        event_type="smart_intake_preview_revoked",
                        actor_type=AuditActorType.SYSTEM,
                        actor_id=_bounded(actor_id, 128),
                        resource_type="smart_intake_preview",
                        resource_id=row["id"],
                        trace_id="smart-intake-rollback",
                        event_data={
                            "candidate_id": str(row.get("candidate_id", "")),
                            "target": row["target"],
                            "reason": "capability_disabled",
                        },
                    )
                )
            return len(rows)


def _normalize_text(text: str, target: str) -> str:
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    if _URL_RE.search(normalized):
        raise InvalidStateError("smart intake accepts text only; URLs are not supported")
    maximum = _MAX_PROFILE_TEXT if target == "profile" else _MAX_INTERVIEW_TEXT
    if len(normalized) > maximum:
        raise InvalidStateError(f"smart intake text exceeds {maximum} characters")
    return normalized


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _advisory_lock_key(candidate_id: UUID) -> int:
    """Return a stable signed PostgreSQL advisory-lock key for one candidate."""
    return int.from_bytes(
        hashlib.sha256(str(candidate_id).encode("ascii")).digest()[:8], "big", signed=True
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_fingerprint(
    request: SmartIntakeRequest,
    normalized: str,
    context_refs: dict[str, Any],
) -> str:
    return _digest(
        _canonical_json(
            {
                "target": request.target,
                "text": normalized,
                "schema_version": _SCHEMA_VERSION,
                "policy_version": _POLICY_VERSION,
                "prompt_version": _PROMPT_VERSION,
                "context_refs": context_refs,
            }
        )
    )


def _bounded(value: str, maximum: int) -> str:
    return str(value or "")[:maximum]


def _system_prompt(target: str) -> str:
    if target == "profile":
        return (
            "Extract only candidate-owned job-search preferences from the untrusted text. "
            "Never infer compensation, remote rules, authorization, visa status, identity, "
            "contact details, hard exclusions, permissions, or consent. Every proposal is "
            "review-only and must have a source span."
        )
    return (
        "Extract a concise user-authored interview context from the untrusted text. "
        "Do not turn it into evidence, instructions, permissions, identity, or job facts. "
        "The result remains untrusted review-only draft text."
    )


def _user_prompt(target: str) -> str:
    if target == "profile":
        return "Return proposals only for the explicitly allowed profile field paths."
    return "Return at most one proposal at path user_context, preserving the author's meaning."


def _model_schema(target: str) -> dict[str, object]:
    path_description = (
        "target_roles[i].title, target_roles[i].seniority, target_roles[i].notes, "
        "locations[i].name, locations[i].radius_km, include_keywords[i], exclude_keywords[i]"
        if target == "profile"
        else "user_context"
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "fields"],
        "properties": {
            "status": {"type": "string", "enum": ["ready", "abstained"]},
            "fields": {
                "type": "array",
                "maxItems": 80,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "path",
                        "value",
                        "value_type",
                        "confidence",
                        "status",
                        "reason",
                        "source_refs",
                    ],
                    "properties": {
                        "path": {
                            "type": "string",
                            "maxLength": 80,
                            "description": path_description,
                        },
                        "value": {},
                        "value_type": {
                            "type": "string",
                            "enum": ["string", "string_list", "integer", "number", "boolean"],
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "status": {"type": "string", "enum": ["proposed", "unknown", "blocked"]},
                        "reason": {"type": "string", "maxLength": _MAX_REASON},
                        "source_refs": {
                            "type": "array",
                            "maxItems": _MAX_SOURCE_REFS,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["input_digest", "start_offset", "end_offset"],
                                "properties": {
                                    "input_digest": {
                                        "type": "string",
                                        "pattern": "^[0-9a-f]{64}$",
                                    },
                                    "start_offset": {"type": "integer", "minimum": 0},
                                    "end_offset": {"type": "integer", "minimum": 0},
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def _trusted_fields(
    target: str,
    fields: list[ModelField],
    normalized: str,
    input_digest: str,
) -> list[dict[str, Any]]:
    if target == "profile":
        fields = _normalize_profile_field_paths(fields)
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_values: set[tuple[str, str]] = set()
    for field in fields:
        path = field.path
        if path in seen:
            continue
        seen.add(path)
        reason = field.reason
        status = field.status
        value = field.value
        if target == "profile":
            match = _PROFILE_PATH_RE.fullmatch(path)
            if match is None or not _value_allowed(path, value, field.value_type):
                status = "blocked"
                value = None
                reason = "field path or value is outside the smart intake allowlist"
        elif (
            path != "user_context"
            or field.value_type != "string"
            or not isinstance(value, str)
            or len(value) > _MAX_INTERVIEW_TEXT
        ):
            status = "blocked"
            value = None
            reason = "only bounded user_context text is allowed for interview context"
        refs = [ref.model_dump(mode="json") for ref in field.source_refs]
        refs_valid = True
        for ref in field.source_refs:
            if (
                ref.input_digest != input_digest
                or ref.start_offset >= ref.end_offset
                or ref.end_offset > len(normalized)
            ):
                refs_valid = False
                break
            span = normalized[ref.start_offset : ref.end_offset].casefold()
            if target == "profile" and isinstance(value, str) and value.casefold() not in span:
                refs_valid = False
                break
            if (
                target == "profile"
                and isinstance(value, list)
                and any(not isinstance(item, str) or item.casefold() not in span for item in value)
            ):
                refs_valid = False
                break
        if not refs_valid:
            status = "blocked"
            value = None
            reason = "source span does not support the proposed value"
            refs = []
        if target == "profile" and status == "proposed" and not refs:
            status = "blocked"
            value = None
            reason = "a proposed profile value requires a source span"
        if target == "interview_context" and status == "proposed":
            # User context is deliberately untrusted, but its source is the
            # complete user input rather than candidate evidence.
            refs = [
                {"input_digest": input_digest, "start_offset": 0, "end_offset": len(normalized)}
            ]
        if target == "profile" and status == "proposed" and isinstance(value, str):
            collection = path.split("[", 1)[0]
            if "." in path:
                collection = f"{collection}.{path.rsplit('.', 1)[-1]}"
            value_key = (collection, value.casefold())
            if value_key in seen_values:
                continue
            seen_values.add(value_key)
        output.append(
            {
                "path": path,
                "value": value,
                "value_type": field.value_type,
                "confidence": field.confidence,
                "status": status,
                "reason": _bounded(reason, _MAX_REASON),
                "source_refs": refs[:_MAX_SOURCE_REFS],
            }
        )
    return output


def _normalize_profile_field_paths(fields: list[ModelField]) -> list[ModelField]:
    """Reindex bounded model array proposals into the server's order."""
    limits = {
        "target_roles": 5,
        "locations": 8,
        "include_keywords": 30,
        "exclude_keywords": 30,
    }
    indexes: dict[str, set[int]] = {name: set() for name in limits}
    for field in fields:
        match = _PROFILE_PATH_RE.fullmatch(field.path)
        if match is None:
            continue
        collection = (
            "target_roles"
            if match.group(1) is not None
            else "locations"
            if match.group(3) is not None
            else str(match.group(5))
        )
        index = int(match.group(1) or match.group(3) or match.group(6))
        if index < limits[collection]:
            indexes[collection].add(index)
    mappings = {
        collection: {old: new for new, old in enumerate(sorted(values))}
        for collection, values in indexes.items()
    }
    normalized: list[ModelField] = []
    for field in fields:
        match = _PROFILE_PATH_RE.fullmatch(field.path)
        if match is None:
            normalized.append(field)
            continue
        collection = (
            "target_roles"
            if match.group(1) is not None
            else "locations"
            if match.group(3) is not None
            else str(match.group(5))
        )
        original_index = int(match.group(1) or match.group(3) or match.group(6))
        new_index = mappings[collection].get(original_index)
        if new_index is None:
            normalized.append(field)
            continue
        suffix = (
            f".{match.group(2)}"
            if match.group(1) is not None
            else f".{match.group(4)}"
            if match.group(3) is not None
            else ""
        )
        normalized.append(field.model_copy(update={"path": f"{collection}[{new_index}]{suffix}"}))
    return normalized


def _value_allowed(path: str, value: Any, value_type: str) -> bool:
    match = _PROFILE_PATH_RE.fullmatch(path)
    if match is None:
        return False
    role_index = int(match.group(1)) if match.group(1) is not None else None
    location_index = int(match.group(3)) if match.group(3) is not None else None
    keyword_index = int(match.group(6)) if match.group(6) is not None else None
    if role_index is not None and role_index >= 5:
        return False
    if location_index is not None and location_index >= 8:
        return False
    if keyword_index is not None and keyword_index >= 30:
        return False
    field_name = match.group(2) or match.group(4) or "keyword"
    if field_name == "radius_km":
        return (value_type == "integer" and value is None) or (
            value_type == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= 500
        )
    max_length = {
        "title": 120,
        "seniority": 80,
        "notes": 240,
        "name": 120,
        "keyword": 80,
    }[field_name]
    return value_type == "string" and isinstance(value, str) and 0 < len(value) <= max_length


def _canonical_decisions(decisions: tuple[ApplyDecisionInput, ...]) -> list[dict[str, Any]]:
    values = []
    seen: set[str] = set()
    for item in decisions:
        if not item.path or len(item.path) > 80 or len(item.reason) > _MAX_REASON:
            raise InvalidStateError("smart intake decision is out of bounds")
        if item.decision not in {"accept", "edit", "reject", "unknown"}:
            raise InvalidStateError("smart intake decision is unsupported")
        if item.path in seen:
            raise InvalidStateError("smart intake decisions must contain one decision per path")
        seen.add(item.path)
        values.append(
            {
                "path": item.path,
                "decision": item.decision,
                # Rejected/unknown values are never part of the durable
                # decision JSON, even when an untrusted client supplied one.
                "value": item.value if item.decision in {"accept", "edit"} else None,
                "reason": item.reason,
            }
        )
    values.sort(key=_decision_sort_key)
    return values


def _decision_sort_key(value: dict[str, Any]) -> tuple[str, str, str]:
    return (str(value["path"]), str(value["decision"]), _canonical_json(value["value"]))


def _decision_metadata(
    decisions: list[dict[str, Any]], raw_fields: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Persist digests that distinguish a proposal from a user correction."""
    proposal_values = {str(item.get("path")): item.get("value") for item in raw_fields}
    return [
        {
            "path": item["path"],
            "decision": item["decision"],
            "reason": item["reason"],
            "value_digest": _digest(_canonical_json(item["value"])),
            "proposal_value_digest": _digest(
                _canonical_json(proposal_values.get(str(item["path"])))
            ),
        }
        for item in decisions
    ]


def _build_patch(
    target: str,
    raw_fields: list[dict[str, Any]],
    decisions: tuple[ApplyDecisionInput, ...],
) -> dict[str, Any]:
    fields = {str(item.get("path")): item for item in raw_fields}
    patch: dict[str, Any] = {}
    for decision in decisions:
        field = fields.get(decision.path)
        if field is None:
            raise InvalidStateError("only proposed smart intake fields can be applied")
        if field.get("status") != "proposed":
            if decision.decision in {"reject", "unknown"}:
                continue
            raise InvalidStateError("only proposed smart intake fields can be accepted")
        if decision.decision in {"reject", "unknown"}:
            continue
        if decision.decision == "accept":
            if decision.value is not None and _canonical_json(decision.value) != _canonical_json(
                field.get("value")
            ):
                raise InvalidStateError("accepted smart intake value must match the preview")
            value = field.get("value")
        else:
            value = decision.value
        value_type = str(field.get("value_type", ""))
        if target == "profile" and not _value_allowed(decision.path, value, value_type):
            raise InvalidStateError("edited smart intake value is outside the allowlist")
        if target == "interview_context" and (
            decision.path != "user_context"
            or not isinstance(value, str)
            or len(value) > _MAX_INTERVIEW_TEXT
        ):
            raise InvalidStateError("edited interview context is outside the allowlist")
        patch[decision.path] = value
    return {"fields": dict(sorted(patch.items()))}


def _is_expired(row: Any) -> bool:
    expires_at = row.get("expires_at")
    return bool(
        row.get("purged_at")
        or row.get("revoked_at")
        or row.get("state") in {"expired", "revoked"}
        or (expires_at and _as_utc(expires_at) <= datetime.now(UTC))
    )


def _as_utc(value: datetime) -> datetime:
    """Normalize database timestamps for adapters that omit timezone metadata."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _preview_result(
    row: Any,
    *,
    draft_patch: dict[str, Any] | None = None,
    decision_set_hash: str | None = None,
) -> SmartPreviewResult:
    return SmartPreviewResult(
        id=row["id"],
        candidate_id=row["candidate_id"],
        target=row["target"],
        state=row["state"],
        input_digest=row["input_digest"],
        context_digest=row["context_digest"],
        fields=cast(list[dict[str, Any]], row.get("fields") or []),
        expires_at=row["expires_at"],
        model_id=row.get("model_id", ""),
        prompt_version=row.get("prompt_version", ""),
        draft_patch=draft_patch,
        decision_set_hash=decision_set_hash,
    )


def _request_from_row(row: Any) -> SmartIntakeRequest:
    refs = cast(dict[str, Any], row.get("context_refs") or {})
    target = cast(Literal["profile", "interview_context"], row["target"])
    if target == "profile":
        return SmartIntakeRequest(
            target=target,
            text=str(row.get("input_text", "")),
            idempotency_key=str(row["idempotency_key"]),
            profile_version_id=UUID(refs["profile_version_id"])
            if refs.get("profile_version_id")
            else None,
        )
    return SmartIntakeRequest(
        target=target,
        text=str(row.get("input_text", "")),
        idempotency_key=str(row["idempotency_key"]),
        canonical_job_id=UUID(refs["canonical_job_id"]),
        job_version_id=UUID(refs["job_version_id"]),
        resume_version_id=UUID(refs["resume_version_id"]),
        profile_version_id=UUID(refs["profile_version_id"])
        if refs.get("profile_version_id")
        else None,
        evidence_ids=tuple(UUID(item["id"]) for item in refs.get("evidence", [])),
    )
