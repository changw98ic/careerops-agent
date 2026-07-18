# ADR 0006: Keep models optional, isolated and qualification-bound

- Status: Accepted
- Date: 2026-07-17

## Context

The Spec requires structured extraction and drafting but does not choose a model provider. Picking a vendor before defining privacy and failure behavior would make sensitive email/candidate data an implicit dependency.

## Decision

`MODEL_PROVIDER=disabled` is the production-safe default and must be a tested adapter. Deterministic parsing, hard gates, policy, approval, state reducers and provider writes remain operable without a model.

The `StructuredModelClient` port accepts a named capability, a versioned schema and an already minimized payload. It returns structured data plus provider/model/prompt/schema versions, latency, cost and hashes. It never exposes tools or credentials.

Allowed outbound content is capability-specific:

- sanitized public JD text;
- short Candidate Evidence snippets explicitly selected by the user;
- the minimum recruiting-mail excerpt after removing addresses, phone numbers, signatures, tracking links and irrelevant history.

Raw Gmail MIME, full mailbox/thread, attachment bytes, OAuth tokens, cookies, secrets, identity/bank documents and unselected private materials are always forbidden. Network egress is restricted to the qualified provider hostname.

Raw prompt/response logging is off. A temporary debug mode requires explicit local enablement, audit, redaction and a maximum 24-hour TTL. Normal records keep only structured output, hashes, versions and operational metadata.

A provider/model capability is enabled only when its ADR addendum records region, no-training/data-use terms, retention controls, timeouts, budget, hostname and the exact sealed datasets it passed. Provider, model, prompt, redaction or schema change invalidates that capability's Release Qualification.

Model results can propose `review_required`; they cannot authorize policy, choose recipients, widen OAuth scopes, change application state directly or invoke a provider.

## Consequences

- Vendor selection can happen after contracts exist and does not block the security kernel.
- If quality thresholds are not met, the product degrades to deterministic/review-only behavior rather than uploading more data.
- Model metadata is auditable without retaining sensitive content by default.

## Verification

Egress-capture tests seed forbidden canary values and require zero outbound matches. Injection tests require zero tool effect and zero policy allow. Each enabled capability must name a passing dataset artifact and current version tuple.
