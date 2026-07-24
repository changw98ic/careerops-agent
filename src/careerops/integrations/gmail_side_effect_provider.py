"""Gmail-backed ``SideEffectProvider`` for the M5A authorization chain.

Adapts ``GmailSender`` (M6) to the ``SideEffectProvider`` protocol so the
kernel can execute email sends through the Gmail API. Reconciliation uses
the Gmail message ID as the stable provider resource identifier.

v1 posture: this provider is only injected when
``auto_send_enabled`` AND ``external_writes_enabled`` are both ``True`` in
``Settings``. Otherwise ``FakeSideEffectProvider`` remains active.
"""

from __future__ import annotations

from datetime import datetime

from careerops.domain.side_effects import (
    ProviderCallResult,
    ProviderFailureClass,
    ProviderResultKind,
    ReceiptFinalState,
    ReconciliationResult,
)
from careerops.integrations.gmail_sender import (
    GmailSender,
    GmailSendError,
    OutgoingEmail,
)


class GmailSideEffectProvider:
    """``SideEffectProvider`` backed by ``GmailSender``.

    ``execute`` sends a single email via the Gmail API. The reconciliation key
    is the kernel-provided key (``idempotency_key:payload_hash``); the provider
    resource ID is the Gmail ``messageId``.

    ``reconcile`` checks an in-memory receipt map populated by prior successful
    ``execute`` calls. This is a v1 simplification; a durable store would query
    the Gmail API for sent messages by ID.

    ``revoke`` is not supported for email (Gmail does not support unsending).
    """

    provider_name = "gmail"

    def __init__(self, sender: GmailSender) -> None:
        self._sender = sender
        self._sent: dict[str, tuple[str, str, datetime]] = {}
        # reconciliation_key -> (message_id, thread_id, sent_at)

    def execute(
        self,
        *,
        reconciliation_key: str,
        request_fingerprint: str,
        target: dict[str, object],
        payload: dict[str, object],
        now: datetime,
    ) -> ProviderCallResult:
        # Idempotent: if we already sent for this key, return SUCCESS.
        existing = self._sent.get(reconciliation_key)
        if existing is not None:
            message_id, thread_id, _ = existing
            return ProviderCallResult(
                kind=ProviderResultKind.SUCCESS,
                provider_resource_id=message_id,
                reconciliation_key=reconciliation_key,
                metadata={"thread_id": thread_id, "duplicate": True},
            )

        to = str(target.get("to", ""))
        subject = str(payload.get("subject", ""))
        body = str(payload.get("body", ""))

        if not to:
            return ProviderCallResult(
                kind=ProviderResultKind.FAILURE,
                error_code="NO_RECIPIENT",
                failure_class=ProviderFailureClass.VALIDATION,
            )

        email = OutgoingEmail(to=to, subject=subject, body=body)
        try:
            result = self._sender.send(email)
        except GmailSendError as exc:
            error_msg = str(exc)
            # Classify: HTTP 4xx -> VALIDATION, network/5xx -> TRANSIENT
            if "HTTP 4" in error_msg:
                failure_class = ProviderFailureClass.VALIDATION
            elif "HTTP 5" in error_msg or "network" in error_msg.lower():
                failure_class = ProviderFailureClass.TRANSIENT
            else:
                failure_class = ProviderFailureClass.UNKNOWN
            return ProviderCallResult(
                kind=ProviderResultKind.FAILURE,
                error_code=f"GMAIL_SEND_ERROR:{error_msg[:120]}",
                failure_class=failure_class,
            )

        self._sent[reconciliation_key] = (result.provider_message_id, result.thread_id, now)
        return ProviderCallResult(
            kind=ProviderResultKind.SUCCESS,
            provider_resource_id=result.provider_message_id,
            reconciliation_key=reconciliation_key,
            metadata={"thread_id": result.thread_id},
        )

    def reconcile(self, *, reconciliation_key: str, now: datetime) -> ReconciliationResult:
        del now
        existing = self._sent.get(reconciliation_key)
        if existing is None:
            return ReconciliationResult(found=False)
        message_id, thread_id, sent_at = existing
        return ReconciliationResult(
            found=True,
            final_state=ReceiptFinalState.SUCCEEDED,
            provider_resource_id=message_id,
            metadata={"thread_id": thread_id, "sent_at": sent_at.isoformat()},
        )

    def revoke(
        self, *, reconciliation_key: str, provider_resource_id: str, now: datetime
    ) -> ReconciliationResult:
        del now
        existing = self._sent.get(reconciliation_key)
        if existing is None or existing[0] != provider_resource_id:
            return ReconciliationResult(found=False)
        # Gmail does not support unsending; report not-found so the kernel
        # records a RECONCILIATION_REQUIRED status.
        return ReconciliationResult(
            found=False,
            metadata={"reason": "gmail_does_not_support_revoke"},
        )
