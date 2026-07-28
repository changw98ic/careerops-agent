## Why

The console currently asks users to translate career goals and interview intent into structured fields before the system can help. This creates avoidable setup friction, while replacing every field with an opaque AI interaction would make high-impact choices harder to verify and would fail when the model is disabled. The first release needs a small, reviewable intake layer that proves the interaction pattern without introducing a second persistence path.

## What Changes

- Add a text-only smart-intake preview for two concrete surfaces: career-profile low-risk preferences and Agent interview-preparation context.
- Return typed, field-level proposals with confidence, server-verifiable source spans, unknown/blocked states, and an explicit capability/provider state.
- Require explicit accept/edit/reject decisions before values are copied into the existing local form state.
- Keep final profile version creation and Agent run creation on their existing APIs, validation, ownership, CSRF, idempotency, and review-only boundaries.
- Preserve manual controls for compensation, authorization, visa, remote policy, hard exclusions, consent, dates, permissions, and every external or durable action.
- Provide truthful disabled/unavailable/invalid/stale/expired states and retain manual workflows when AI is unavailable.
- Persist only a short-lived review artifact and append-only decision metadata; do not log or retain raw prompts/responses by default.
- Add contract, frontend, integration, adversarial, and browser evidence for the MVP. Resume-file AI parsing, arbitrary job URLs, crawl-plan generation, and application/send automation are explicitly deferred to follow-up changes.

## Capabilities

### New Capabilities

- `smart-form-intake`: Reviewable text-to-prefill for the profile and interview-context surfaces, with schema boundaries, provenance, confirmation, fallback, and safe rollout.

### Modified Capabilities

None. Existing profile, resume, crawl-plan, application, and Agent capabilities remain the authoritative persistence and action contracts; this change adds an optional preview layer only for the two MVP surfaces.

## Impact

- Frontend: shared smart-intake panel/composable and adapters in `Profile.vue` and `AgentWorkbench.vue`; no smart confirmation controls in resume upload, crawl run-now, package approval, send, or application submission flows.
- Backend: candidate-scoped preview/apply endpoints, target schemas, capability/readiness checks, short-lived preview persistence, model-gateway invocation, deterministic disabled behavior, stale checks, and append-only decisions.
- Data/API: additive preview and decision records with candidate ownership, canonical request fingerprints, idempotency keys, context digests, 30-minute expiry, bounded metadata, and `Cache-Control: no-store`.
- Safety: model provider remains disabled by default; no raw resume bytes, arbitrary URLs, browser/mailbox content, credentials, external writes, auto-send, OAuth, or Ego actions enter this capability.
- Verification: run `make verify`, frontend tests/build, disposable-PostgreSQL integration tests, fake-provider/adversarial tests, and browser UI evidence before claiming the MVP is usable.
