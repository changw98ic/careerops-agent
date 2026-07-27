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
from pathlib import Path

from careerops.domain.side_effects import (
    ProviderCallResult,
    ProviderFailureClass,
    ProviderResultKind,
    ReceiptFinalState,
    ReconciliationResult,
)
from careerops.integrations.gmail_receipt_store import (
    GmailReceipt,
    GmailReceiptStore,
    InMemoryGmailReceiptStore,
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

    ``reconcile`` checks a durable receipt store populated by prior successful
    ``execute`` calls.  The default ``InMemoryGmailReceiptStore`` is replaced
    by ``PostgresGmailReceiptStore`` in production so receipts survive restarts.

    ``revoke`` is not supported for email (Gmail does not support unsending).
    """

    provider_name = "gmail"

    def __init__(
        self,
        sender: GmailSender,
        receipt_store: GmailReceiptStore | None = None,
        storage: object | None = None,
    ) -> None:
        self._sender = sender
        self._receipt_store: GmailReceiptStore = (
            receipt_store if receipt_store is not None else InMemoryGmailReceiptStore()
        )
        self._storage = storage

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
        existing = self._receipt_store.get_by_reconciliation_key(reconciliation_key)
        if existing is not None:
            return ProviderCallResult(
                kind=ProviderResultKind.SUCCESS,
                provider_resource_id=existing.provider_message_id,
                reconciliation_key=reconciliation_key,
                metadata={"thread_id": existing.thread_id, "duplicate": True},
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

        # Resolve attachment hashes to actual file paths via storage.
        # If hashes are present but storage can't resolve them, the send MUST
        # fail — proceeding without attachments would violate the payload hash
        # binding (preview/send parity, task 9.8).
        attachment_paths: tuple[Path, ...] = ()
        attachment_hashes = payload.get("attachment_hashes", [])
        if attachment_hashes:
            if self._storage is None:
                return ProviderCallResult(
                    kind=ProviderResultKind.FAILURE,
                    error_code="ATTACHMENT_STORAGE_UNAVAILABLE",
                    failure_class=ProviderFailureClass.VALIDATION,
                )
            resolved: list[Path] = []
            for hash_val in attachment_hashes:
                path = self._storage.get(str(hash_val))  # type: ignore[union-attr]
                if path is None:
                    return ProviderCallResult(
                        kind=ProviderResultKind.FAILURE,
                        error_code="ATTACHMENT_NOT_FOUND",
                        failure_class=ProviderFailureClass.VALIDATION,
                        metadata={"missing_hash": str(hash_val)},
                    )
                resolved.append(Path(path))
            attachment_paths = tuple(resolved)

        email = OutgoingEmail(to=to, subject=subject, body=body, attachments=attachment_paths)
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

        self._receipt_store.put(
            GmailReceipt(
                reconciliation_key=reconciliation_key,
                provider_message_id=result.provider_message_id,
                thread_id=result.thread_id,
                sent_at=now,
            )
        )
        return ProviderCallResult(
            kind=ProviderResultKind.SUCCESS,
            provider_resource_id=result.provider_message_id,
            reconciliation_key=reconciliation_key,
            metadata={"thread_id": result.thread_id},
        )

    def reconcile(self, *, reconciliation_key: str, now: datetime) -> ReconciliationResult:
        del now
        existing = self._receipt_store.get_by_reconciliation_key(reconciliation_key)
        if existing is None:
            return ReconciliationResult(found=False)
        return ReconciliationResult(
            found=True,
            final_state=ReceiptFinalState.SUCCEEDED,
            provider_resource_id=existing.provider_message_id,
            metadata={
                "thread_id": existing.thread_id,
                "sent_at": existing.sent_at.isoformat(),
            },
        )

    def revoke(
        self, *, reconciliation_key: str, provider_resource_id: str, now: datetime
    ) -> ReconciliationResult:
        del now
        existing = self._receipt_store.get_by_reconciliation_key(reconciliation_key)
        if existing is None or existing.provider_message_id != provider_resource_id:
            return ReconciliationResult(found=False)
        # Gmail does not support unsending; report not-found so the kernel
        # records a RECONCILIATION_REQUIRED status.
        return ReconciliationResult(
            found=False,
            metadata={"reason": "gmail_does_not_support_revoke"},
        )
