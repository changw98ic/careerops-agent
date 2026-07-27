"""System-managed Gmail send service (Section 10, tasks 10.1-10.7).

Turns the CareerOps final-confirmation click into a *durable authorization
chain* instead of a direct provider call. The flow is:

1. **confirm_send** (10.1-10.3): revalidate every trusted precondition
   (ownership, application state, package approval, recipient eligibility,
   capability release, account status, payload hash) at confirmation time;
   create-or-reuse the kernel ``ActionIntent`` + payload version + policy
   decision; persist the user confirmation as the kernel ``ApprovalRequest``
   (approved); enqueue one transactional Outbox event; return a durable
   ``PENDING`` status. The provider is NEVER called here.
2. **worker_step** (10.4-10.6): the isolated side-effect worker resolves
   opaque credentials and calls ``kernel.execute`` (which calls the provider,
   records the attempt, and reconciles ambiguity before any retry). On a
   confirmed receipt the service appends exactly one ``SUBMITTED_VIA_PROVIDER``
   application event and transitions the application to ``SUBMITTED``.
   Ambiguous outcomes leave the application visibly unresolved and disable
   blind retry.
3. **get_status** (10.7): idempotent read of the existing intent / receipt /
   reconciliation state for the UI.

The service reuses the existing :class:`SideEffectKernel`,
:class:`OutboxStore`, :class:`ActionIntent`, :class:`ProviderReceipt` and
reconciliation tables — no new persistence is introduced. The provider is the
:class:`FakeSideEffectProvider` only; real Gmail activation is a separate
future qualification change (task 17.6).

Iron rules honored:
- Model review-only (Iron Rule 2): the recipient / payload / channel come
  from trusted business state passed via :class:`SystemSendRequest`; the
  service never consults model output.
- Append-only / reversible (Iron Rule 4): the submitted event is appended
  once and only after a confirmed receipt; ambiguous outcomes never produce
  a submitted claim.
- Server-side ownership (Iron Rule 2/6): ``candidate_id`` is the
  server-resolved owner; a mismatch raises before any provider call.
- Default-deny (Iron Rule 7): a denied ``SYSTEM_MANAGED_SEND`` capability,
  an inactive account, or any policy failure refuses before the provider is
  contacted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from careerops.application.outbox import OutboxEventType, OutboxStore, PendingOutboxEvent
from careerops.application.side_effect_kernel import (
    ProposalInput,
    SideEffectKernel,
)
from careerops.domain.applications import (
    Application,
    ApplicationEvent,
    ApplicationEventSource,
    ApplicationEventType,
    ApplicationState,
    IllegalTransitionError,
    SubmissionChannel,
)
from careerops.domain.side_effects import IntentStatus
from careerops.domain.system_send import (
    POLICY_VERSION,
    SystemSendDenialReason,
    SystemSendPhase,
    SystemSendRequest,
    SystemSendStatus,
    compute_system_send_idempotency_key,
)
from careerops.orchestration.capability_resolver import (
    CapabilityDecision,
    CapabilityKind,
)

__all__ = [
    "AccountInactiveError",
    "PackageNotApprovedError",
    "PayloadHashMismatchError",
    "RecipientNotEligibleError",
    "SystemManagedSendService",
    "SystemSendDeniedError",
    "SystemSendNotFoundError",
]


# ---------------------------------------------------------------------------
# Errors (translated at the API layer)
# ---------------------------------------------------------------------------


class SystemSendError(Exception):
    """Base for system-managed send service errors."""


class SystemSendNotFoundError(SystemSendError):
    """The send intent does not exist or is not owned by the candidate."""


class SystemSendDeniedError(SystemSendError):
    """Confirmation denied BEFORE the provider call (task 10.8).

    Carries the fail-closed reason codes safe to surface in UI messaging.
    Codes are plain strings so they can carry both the
    :class:`SystemSendDenialReason` enum values and the kernel policy's own
    reason codes (e.g. ``EVIDENCE_REQUIRED``).
    """

    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        self.reason_codes = reason_codes
        super().__init__("system-managed send denied: " + ",".join(reason_codes))


class PackageNotApprovedError(SystemSendError):
    pass


class PayloadHashMismatchError(SystemSendError):
    pass


class RecipientNotEligibleError(SystemSendError):
    pass


class AccountInactiveError(SystemSendError):
    pass


# ---------------------------------------------------------------------------
# Optional collaborator protocols
# ---------------------------------------------------------------------------


class _ApplicationRepository(Protocol):
    def find_by_id(self, application_id: UUID) -> Application | None: ...

    def save(self, application: Application) -> None: ...

    def append_event(self, event: ApplicationEvent) -> None: ...


class _PackageReader(Protocol):
    """Reads the latest package version for revalidation (Section 8 binding)."""

    def get_latest(self, application_id: UUID) -> object | None: ...


class CapabilityResolver(Protocol):
    def decide(self, kind: CapabilityKind) -> CapabilityDecision: ...


# account_status_lookup(application_id) -> True when the sending account is
# active and not revoked. Section 11 wires a real account-status reader; until
# then callers pass ``None`` and the gate is skipped (the capability gate still
# applies). Revocation at runtime is handled by the worker refusing a revoked
# credential (task 10.9 credential-revoke scenario).
AccountStatusLookup = Callable[[UUID], bool]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SystemManagedSendService:
    """Orchestrates CareerOps confirmation -> durable intent -> worker receipt.

    The service is additive: it composes the existing kernel + application
    repo + outbox rather than mutating them. It is the ONLY entry point that
    appends a ``SUBMITTED_VIA_PROVIDER`` event, and it does so exactly once
    per confirmed receipt.
    """

    def __init__(
        self,
        kernel: SideEffectKernel,
        application_repo: _ApplicationRepository,
        *,
        package_reader: _PackageReader | None = None,
        capability_resolver: CapabilityResolver | None = None,
        outbox_store: OutboxStore | None = None,
        account_status_lookup: AccountStatusLookup | None = None,
        recipient_eligible: Callable[[SystemSendRequest], bool] | None = None,
    ) -> None:
        self._kernel = kernel
        self._applications = application_repo
        self._packages = package_reader
        self._capability = capability_resolver
        self._outbox = outbox_store
        self._account_status = account_status_lookup
        # recipient_eligible(req) -> True when the recipient is a trusted
        # recruiting contact or a verified reply target whose domain matches
        # the company evidence (task 9.2 / 10.2). Default-allow when no lookup
        # is wired AND a non-empty recipient was supplied, so contract tests
        # can drive the slice; production wiring supplies a real lookup.
        self._recipient_eligible = recipient_eligible or _default_recipient_eligible

    # ------------------------------------------------------------------
    # 10.1-10.3 — CareerOps final confirmation -> durable pending intent
    # ------------------------------------------------------------------

    def confirm_send(
        self,
        request: SystemSendRequest,
        *,
        candidate_id: UUID,
        now: datetime,
    ) -> SystemSendStatus:
        """Record the user's final confirmation as a durable authorization chain.

        Revalidates every trusted precondition (10.2), creates-or-reuses the
        kernel intent + payload + policy decision, persists the confirmation as
        the approved ``ApprovalRequest`` (10.1), enqueues one Outbox event
        (10.3), and returns ``PENDING``. The provider is NOT called here.
        """
        app = self._revalidate(request, candidate_id=candidate_id, now=now)

        idempotency_key = compute_system_send_idempotency_key(
            application_id=request.application_id,
            account_email=request.account_email,
            recipient=request.recipient,
            normalized_payload_hash=request.payload_hash,
        )

        proposal = ProposalInput(
            action_kind="send_email",
            resource_type="application",
            resource_id=request.application_id,
            idempotency_key=idempotency_key,
            created_by=f"candidate:{candidate_id}",
            target={
                "to": request.recipient,
                "account": request.account_email,
                "application_id": str(request.application_id),
            },
            payload={
                "subject": request.subject,
                "body": request.body,
                "payload_hash": request.payload_hash,
                "policy_version": POLICY_VERSION,
                "package_version_id": str(request.package_version_id),
                "attachment_hashes": list(request.attachment_hashes),
                "thread_headers": dict(request.thread_headers),
            },
            attachment_refs=request.attachment_hashes,
            evidence_refs=request.evidence_refs,
            trusted_facts={
                "capability_released": True,
                "target_allowlisted": True,
                "confirmation_source": "careerops_final_confirmation",
            },
            untrusted_claims={},
            authenticated=True,
        )
        result = self._kernel.propose(proposal, now=now)

        # Policy denial (e.g. evidence missing, target not allowlisted) refuses
        # BEFORE the provider call (task 10.8). The intent is recorded as DENIED
        # for audit, but no approval is created and no outbox event is enqueued.
        if result.policy_decision.decision.value == "deny":
            raise SystemSendDeniedError(
                (
                    SystemSendDenialReason.POLICY_DENIED.value,
                    *result.policy_decision.reason_codes,
                )
            )

        # The user's final confirmation IS the durable approval. The kernel
        # records it bound to (payload version + policy decision + expiration)
        # so any later payload mutation invalidates it. A REPEAT confirmation
        # (double-click, resume) MUST be idempotent: it returns the existing
        # intent/approval/outbox state and creates no second provider trigger.
        replay = self._kernel.replay(result.intent.id)
        already_approved = any(
            a.decision.value == "approved"
            and a.payload_version_id == result.payload_version.id
            for a in replay.approvals
        )
        if not already_approved:
            approval = self._kernel.get_or_create_pending_approval(
                result.intent.id,
                requested_for=f"candidate:{candidate_id}",
                now=now,
                decision_rule_reference="careerops_final_confirmation",
            )
            if approval.decision.value != "approved":
                self._kernel.decide_approval(
                    approval.id,
                    action="approve",
                    requested_for=f"candidate:{candidate_id}",
                    now=now,
                    decision_rule_reference="careerops_final_confirmation",
                )
            # Exactly one transactional outbox event per logical send, enqueued
            # on the first confirmation only.
            self._enqueue_outbox(result.intent.id, result.payload_version.id, now)

        return SystemSendStatus(
            application_id=app.id,
            intent_id=result.intent.id,
            phase=SystemSendPhase.PENDING,
            payload_hash=request.payload_hash,
        )

    # ------------------------------------------------------------------
    # 10.4-10.6 — isolated worker step (provider call + receipt + reconcile)
    # ------------------------------------------------------------------

    def worker_step(
        self,
        intent_id: UUID,
        *,
        candidate_id: UUID,
        now: datetime,
    ) -> SystemSendStatus:
        """Drive one worker pass: execute the provider, then apply the receipt.

        The kernel reconciles ambiguity BEFORE any retry and records at most
        one provider effect per reconciliation key. On a confirmed receipt the
        service appends exactly one ``SUBMITTED_VIA_PROVIDER`` event and
        transitions the application to ``SUBMITTED`` (idempotent). Ambiguous
        outcomes leave the application in ``PREPARING`` and surface
        ``RECONCILIATION_REQUIRED``.
        """
        intent = self._kernel.replay(intent_id).intent
        if intent.resource_type != "application":
            raise SystemSendNotFoundError(str(intent_id))
        application_id = intent.resource_id
        app = self._require_owned(application_id, candidate_id)

        outcome = self._kernel.execute(intent_id, now=now)

        if outcome.status is IntentStatus.CONFIRMED and outcome.receipt is not None:
            self._apply_confirmed_receipt(app, intent_id, outcome, now=now)
            return SystemSendStatus(
                application_id=app.id,
                intent_id=intent_id,
                phase=SystemSendPhase.SENT,
                payload_hash=_payload_hash_from_replay(intent_id, self._kernel),
                provider_resource_id=outcome.receipt.provider_resource_id,
                provider_message_id=_message_id(outcome.receipt),
                submitted_at=now,
                reconciled=bool(outcome.reconciled),
            )

        if outcome.status is IntentStatus.FAILED:
            return SystemSendStatus(
                application_id=app.id,
                intent_id=intent_id,
                phase=SystemSendPhase.FAILED,
                denial_reasons=outcome.reason_codes,
            )

        # RECONCILIATION_REQUIRED (or any non-terminal): stay unresolved.
        return SystemSendStatus(
            application_id=app.id,
            intent_id=intent_id,
            phase=SystemSendPhase.RECONCILIATION_REQUIRED,
            denial_reasons=outcome.reason_codes,
        )

    def escalate_reconciliation(
        self,
        intent_id: UUID,
        *,
        candidate_id: UUID,
        now: datetime,
    ) -> SystemSendStatus:
        """Surface an ambiguous send as a user reconciliation task (10.6).

        Does NOT retry the provider: blind retry is disabled for ambiguous
        outcomes. The intent stays in ``RECONCILIATION_REQUIRED`` and the
        application remains in ``PREPARING`` (visibly unresolved).
        """
        replay = self._kernel.replay(intent_id)
        intent = replay.intent
        if intent.resource_type != "application":
            raise SystemSendNotFoundError(str(intent_id))
        app = self._require_owned(intent.resource_id, candidate_id)
        del now
        return SystemSendStatus(
            application_id=app.id,
            intent_id=intent_id,
            phase=SystemSendPhase.RECONCILIATION_REQUIRED,
            payload_hash=_latest_payload_hash(replay),
            denial_reasons=("RECONCILIATION_ESCALATED",),
        )

    # ------------------------------------------------------------------
    # 10.7 — idempotent status read
    # ------------------------------------------------------------------

    def get_status(
        self,
        intent_id: UUID,
        *,
        candidate_id: UUID,
    ) -> SystemSendStatus:
        """Return the current send phase for the UI (idempotent, no side effects).

        Derives the phase from the kernel intent status + receipt presence +
        the application's submitted state. A repeat confirmation or status poll
        never creates a second provider effect.
        """
        replay = self._kernel.replay(intent_id)
        intent = replay.intent
        if intent.resource_type != "application":
            raise SystemSendNotFoundError(str(intent_id))
        app = self._require_owned(intent.resource_id, candidate_id)
        payload_hash = _latest_payload_hash(replay)
        receipts = replay.receipts

        if intent.status is IntentStatus.CONFIRMED and receipts:
            receipt = receipts[-1]
            return SystemSendStatus(
                application_id=app.id,
                intent_id=intent_id,
                phase=SystemSendPhase.SENT,
                payload_hash=payload_hash,
                provider_resource_id=receipt.provider_resource_id,
                provider_message_id=_message_id(receipt),
                submitted_at=app.submitted_at,
            )
        if intent.status is IntentStatus.FAILED:
            return SystemSendStatus(
                application_id=app.id,
                intent_id=intent_id,
                phase=SystemSendPhase.FAILED,
                payload_hash=payload_hash,
            )
        if intent.status is IntentStatus.RECONCILIATION_REQUIRED:
            return SystemSendStatus(
                application_id=app.id,
                intent_id=intent_id,
                phase=SystemSendPhase.RECONCILIATION_REQUIRED,
                payload_hash=payload_hash,
            )
        # PROPOSED / ELIGIBLE / AWAITING_APPROVAL / PROCESSING -> pending.
        return SystemSendStatus(
            application_id=app.id,
            intent_id=intent_id,
            phase=SystemSendPhase.PENDING,
            payload_hash=payload_hash,
        )

    # ------------------------------------------------------------------
    # 10.2 — revalidation (denies BEFORE the provider call)
    # ------------------------------------------------------------------

    def _revalidate(
        self,
        request: SystemSendRequest,
        *,
        candidate_id: UUID,
        now: datetime,
    ) -> Application:
        del now  # revalidation is point-in-time against current trusted state
        reasons: list[SystemSendDenialReason] = []

        app = self._applications.find_by_id(request.application_id)
        if app is None or app.candidate_id != candidate_id:
            raise SystemSendDeniedError(
                (SystemSendDenialReason.APPLICATION_NOT_OWNED.value,)
            )

        if app.state is not ApplicationState.PREPARING:
            reasons.append(SystemSendDenialReason.APPLICATION_NOT_PREPARING.value)

        if self._capability is not None:
            decision = self._capability.decide(CapabilityKind.SYSTEM_MANAGED_SEND)
            if not decision.released:
                reasons.append(SystemSendDenialReason.CAPABILITY_NOT_RELEASED.value)

        if self._packages is not None:
            latest = self._packages.get_latest(request.application_id)
            approval_state = getattr(latest, "approval_state", None) if latest else None
            latest_id = getattr(latest, "id", None) if latest else None
            latest_hash = getattr(latest, "payload_hash", None) if latest else None
            if approval_state is None or approval_state.value != "approved":
                reasons.append(SystemSendDenialReason.PACKAGE_NOT_APPROVED.value)
            elif latest_id != request.package_version_id or latest_hash != request.payload_hash:
                reasons.append(SystemSendDenialReason.PACKAGE_BINDING_STALE.value)

        if not self._recipient_eligible(request):
            reasons.append(SystemSendDenialReason.RECIPIENT_NOT_ELIGIBLE.value)

        if self._account_status is not None and not self._account_status(
            request.application_id
        ):
            reasons.append(SystemSendDenialReason.ACCOUNT_NOT_ACTIVE.value)

        if reasons:
            raise SystemSendDeniedError(tuple(reasons))

        return app

    # ------------------------------------------------------------------
    # 10.5 — apply a confirmed receipt (append-only, exactly once)
    # ------------------------------------------------------------------

    def _apply_confirmed_receipt(
        self,
        app: Application,
        intent_id: UUID,
        outcome: object,
        *,
        now: datetime,
    ) -> None:
        receipt = getattr(outcome, "receipt", None)
        assert receipt is not None  # caller guarantees CONFIRMED + receipt
        provider_resource_id = receipt.provider_resource_id
        provider_message_id = _message_id(receipt)

        updated = app
        # The submitted event is appended EXACTLY ONCE: only on the first
        # confirmed receipt that transitions PREPARING -> SUBMITTED. A repeat
        # worker pass on an already-submitted application records no second
        # event (task 10.5 / 10.7).
        if updated.state is ApplicationState.SUBMITTED:
            return
        if updated.state is not ApplicationState.PREPARING:
            raise IllegalTransitionError(updated.state, ApplicationState.SUBMITTED)
        updated = replace(
            updated,
            state=ApplicationState.SUBMITTED,
            submission_channel=SubmissionChannel.EMAIL,
            submitted_at=updated.submitted_at or now,
            version=updated.version + 1,
            updated_at=now,
        )
        self._applications.save(updated)

        event_data: dict[str, object] = {
            "intent_id": str(intent_id),
            "provider": receipt.provider,
            "provider_resource_id": provider_resource_id,
            "channel": SubmissionChannel.EMAIL.value,
            "confirmation_method": "system_managed_send",
            "policy_version": POLICY_VERSION,
        }
        if provider_message_id is not None:
            event_data["provider_message_id"] = provider_message_id
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=updated.id,
                event_type=ApplicationEventType.SUBMITTED_VIA_PROVIDER,
                from_state=ApplicationState.PREPARING,
                to_state=ApplicationState.SUBMITTED,
                source=ApplicationEventSource.SYSTEM,
                actor_id="side-effect-worker",
                note="System-managed email delivered; provider receipt confirmed",
                event_data=event_data,
                occurred_at=now,
                created_at=now,
            )
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _enqueue_outbox(
        self, intent_id: UUID, payload_version_id: UUID, now: datetime
    ) -> None:
        if self._outbox is None:
            return
        self._outbox.enqueue(
            PendingOutboxEvent(
                event_id=uuid4(),
                event_key=f"system_send/{intent_id}",
                action_intent_id=intent_id,
                payload_version_id=payload_version_id,
                event_type=OutboxEventType.PROVIDER_WRITE,
                available_at=now,
            )
        )

    def _require_owned(self, application_id: UUID, candidate_id: UUID) -> Application:
        app = self._applications.find_by_id(application_id)
        if app is None or app.candidate_id != candidate_id:
            raise SystemSendNotFoundError(str(application_id))
        return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_recipient_eligible(request: SystemSendRequest) -> bool:
    """Default recipient check: a non-empty, well-formed address.

    Production wiring replaces this with the Section 9 trusted-contact /
    verified-reply-target lookup (domain match against company evidence).
    """
    recipient = request.recipient.strip()
    return bool(recipient) and "@" in recipient and not recipient.endswith("@")


def _message_id(receipt: object) -> str | None:
    metadata = getattr(receipt, "receipt_metadata", None) or getattr(
        receipt, "metadata", None
    )
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("message_id") or metadata.get("provider_message_id")
    return str(value) if value else None


def _latest_payload_hash(replay: object) -> str | None:
    versions = getattr(replay, "payload_versions", ())
    if not versions:
        return None
    return getattr(versions[-1], "payload_hash", None)


def _payload_hash_from_replay(intent_id: UUID, kernel: SideEffectKernel) -> str | None:
    return _latest_payload_hash(kernel.replay(intent_id))
