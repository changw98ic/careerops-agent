# ADR 0003: Centralize external side-effect authority

- Status: Accepted
- Date: 2026-07-17

## Context

Gmail and Calendar do not share a transaction with PostgreSQL. Model output and workflow retries therefore cannot be allowed to call providers directly or infer that a timeout means an action did not happen.

## Decision

Every external write follows one chain:

```text
ActionIntent
  → immutable PayloadVersion
  → PolicyDecision
  → Approval (when required)
  → transactional OutboxEvent
  → isolated Side-effect Worker
  → ProviderAttempt / reconciliation
  → ProviderReceipt
  → append-only AuditEvent
```

Policy outcomes are `deny`, `require_approval` or narrowly defined `allow`. Unknown action kinds, missing evidence, stale policy versions, unavailable credentials and invalid Release Qualification all deny. Model output is untrusted proposal data and cannot supply policy facts.

Approval binds actor, target, action kind, payload hash, policy version and expiry. Editing any byte or target creates a new payload version and invalidates the old approval. Calendar writes always require a current user action even if policy otherwise allows them.

Database roles are separated:

- API/UI may create proposals, decisions and approvals but cannot read provider secrets or mark an attempt successful.
- Outbox publisher may lease committed eligible events but cannot execute provider calls.
- Side-effect Worker may resolve opaque credential handles, execute eligible intents and append attempts/receipts/audit; it cannot edit payloads or approvals.
- Model and read-only adapters have no provider-write or credential privileges.

Idempotency uses a unique intent key plus stable request fingerprint and provider reconciliation key. `timeout` and `connection_lost` are ambiguous states; the worker reconciles before retrying. When proof is impossible, the intent becomes `reconciliation_required` and automatic retry stops.

The product promises effectively-once behavior within a documented fault model, not a cross-provider exactly-once transaction.

## Consequences

- All provider adapters implement `execute` and `reconcile`; adapters without a proven reconciliation strategy cannot be release-qualified.
- Outbox and audit records are part of the business transaction, not optional observability.
- A global kill switch is an independent release capability that ordinary configuration, UI or model output cannot enable.

## Verification

M5A must inject before-call crash, after-call-before-commit crash, timeout-with-success, duplicate receipt and credential revoke. Concurrent submission of one idempotency key must yield one confirmed provider effect, and every ambiguous outcome must stop in the review queue.
