# ADR 0006: Keep models optional, isolated and qualification-bound

- Status: Accepted (qualification gate relaxed 2026-07-24)
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

### Addendum 2026-07-24: Qualification gate relaxed to user-supplied config

The original decision required a formal ADR addendum with sealed dataset artifacts
(`model_qualification_artifact` + `model_qualification_version`) before any
non-disabled provider could be instantiated.  This gate was removed and replaced
with user-supplied connection parameters:

- `model_base_url` — any Anthropic-compatible endpoint
- `model_api_key` — the user's API key
- `model_name` — the model identifier

The `Settings` validator now requires all three fields to be non-empty when
`model_provider != "disabled"`.  The factory (`create_model_client`) accepts
these as direct parameters and has no `Settings` dependency.

**What was preserved:**
- LLM never binds tools, outputs `review_only=True`, `untrusted_claims={}`
- `DisabledModelAdapter` remains the default and tested safe path
- Network egress is still restricted to the configured provider hostname
- All existing security tests continue to pass

**What changed:**
- Removed `_PROVIDER_ENV` hardcoding (xiaomi/zhipu specific env vars)
- Removed `model_qualification_artifact` / `model_qualification_version` from Settings
- Removed `get_settings()` call from factory (decoupled)
- Added `model_base_url` / `model_api_key` / `model_name` to Settings
- Any Anthropic-compatible endpoint works, not just pre-registered providers

**Rationale:** The qualification gate was designed for a future M2 pilot where
a formal privacy review would precede provider enablement.  In practice, this
blocked single-user self-hosted deployments where the user IS the qualification
authority.  The gate added friction without adding security for the threat model
(single user, local deployment, no multi-tenant data mixing).

## Consequences

- Vendor selection can happen after contracts exist and does not block the security kernel.
- If quality thresholds are not met, the product degrades to deterministic/review-only behavior rather than uploading more data.
- Model metadata is auditable without retaining sensitive content by default.
- Users can enable any Anthropic-compatible LLM by setting three config values, without requiring a formal qualification artifact.

## Verification

Egress-capture tests seed forbidden canary values and require zero outbound matches. Injection tests require zero tool effect and zero policy allow. Each enabled capability must name a passing dataset artifact and current version tuple.
