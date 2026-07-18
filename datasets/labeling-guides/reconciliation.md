# Reconciliation labeling guide v1

## Unit

One row is one immutable action plus a provider fault/observation trace. Group variants of the same intent and provider fingerprint together.

## Outcomes

- `succeeded`: an exact provider receipt/effect is proven.
- `failed`: a non-retryable validation/policy/provider failure proves no effect.
- `cancelled`: authority was revoked before execution and no effect exists.
- `reconciliation_required`: available evidence cannot prove exact success or absence.

## Retry rule

Before-call crash with no provider call may retry under the bounded policy. Timeout/connection loss after a possible call must reconcile first. Exact found result succeeds without a second call. Ambiguous/unavailable lookup stops automatic retry and enters manual queue.

Confirmed provider effect count is never greater than one inside the supported fault matrix. Duplicate receipts are deduplicated by provider identity and reconciliation key, not discarded without audit.

## Quality

Every fault outcome is safety-critical and double-labeled. Annotators use the stated provider observations rather than assuming that a timeout means failure.
