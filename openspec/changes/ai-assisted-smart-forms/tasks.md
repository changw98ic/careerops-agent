## 1. Freeze the MVP contract

- [x] 1.1 Add the closed target matrix for `profile` and `interview_context`, including field paths, array identity/order, `locations.kind=preferred`, nullable `radius_km`, character/cardinality limits, source-span limits, and protected fields.
- [x] 1.2 Add `smart_intake_enabled=false`, `SMART_INTAKE` capability resolution, `AuthAction`/limiter entry, route dependency, app wiring, canonical `SMART_*` API/OpenAPI errors, and default-deny tests for disabled, unsupported, stale, expired, in-progress, and idempotency-key-reuse cases.
- [x] 1.3 Add request/response DTOs and closed schemas with `additionalProperties: false`; accept only normalized bounded text plus exact Profile or Agent context IDs (`canonical_job_id`, bound `job_version_id`, confirmed `resume_version_id`, optional profile/evidence IDs).
- [x] 1.4 Add contract tests proving URLs, files, raw resume bytes, arbitrary prompts/schemas, unsupported targets, and cross-candidate context IDs are rejected before model input assembly.

## 2. Preview storage and lifecycle

- [x] 2.1 Add additive PostgreSQL tables/models for candidate-owned previews and append-only field decisions with non-null candidate foreign keys, input/context digests, expiry, purge metadata, and bounded model/capability metadata.
- [x] 2.2 Add unique candidate-scoped preview idempotency and apply idempotency constraints plus canonical SHA-256 request/decision fingerprints (sorted keys/field paths, normalized NFC text, typed values, bounded reasons).
- [x] 2.3 Implement repository create/reuse/get/apply methods with transaction-scoped ownership checks, a 30-second pending claim lease/recovery path, server-computed context digests, row-lock stale checks, and expired/revoked tombstones.
- [x] 2.4 Add a retention-role cleanup path that purges preview values within 24 hours after the 30-minute expiry, revokes/purges unapplied values on rollback, and retains only bounded tombstone/decision/audit metadata; add migration, SECURITY DEFINER/grant, rollback, and purge tests.

## 3. Model service and HTTP API

- [x] 3.1 Implement target-specific input builders that pass only normalized bounded pasted text as fenced untrusted content, use no tools, and never include raw resume, mailbox, browser, credential, or arbitrary URL data; verify source-span semantics and substring support.
- [x] 3.2 Implement structured model invocation through `StructuredModelClient` with a total 15-second deadline including retry/backoff, 768 output-token budget, one repair attempt that preserves the fenced input, closed-schema validation, egress redaction, and exact unavailable/abstained/invalid mapping.
- [x] 3.3 Implement truthful provider-disabled/unavailable/abstained/invalid responses with zero provider calls in disabled mode and a manual fallback payload.
- [x] 3.4 Add candidate-scoped `POST /api/v1/smart-intake/previews`, `GET /api/v1/smart-intake/previews/{id}`, and `POST /api/v1/smart-intake/previews/{id}/apply`; require session, CSRF, allowlisted Host+Origin, `Cache-Control: no-store` on success/errors/GETs, the 10-per-10-minute candidate-hash limiter, and fail-closed Redis behavior.
- [x] 3.5 Make apply accept only immutable preview keys and explicit decisions, return a non-persisted draft patch, and prove it cannot call Agent-start, crawl/Ego, package, send, OAuth, or application-submit services.
- [x] 3.6 Add same-transaction append-only audit writes through `careerops.append_audit_event` SECURITY DEFINER (never direct API-role INSERTs) and bounded metrics for preview/outcome/decision/latency/token data without raw values, titles, emails, or trace IDs as labels.

## 4. Shared frontend workflow

- [x] 4.1 Add API client methods and typed frontend helpers for preview create/get/apply, including structured mappings for disabled, unavailable, stale, expired, and idempotency errors.
- [x] 4.2 Build a reusable `SmartIntakePanel`/composable with input, loading, provider state, field decisions, confidence/source-span display, blocked reasons, expiry handling, and manual fallback.
- [x] 4.3 Ensure apply returns a patch using closed field paths, compares indexed arrays/fields with the local baseline digest, leaves dirty values unchanged with a visible conflict, and keeps Apply separate from Save/Start.
- [x] 4.4 Add frontend unit tests for accept/edit/unknown, manual-value preservation, disabled/incomplete context, stale apply, and the manual-first interaction; the build gate and Ego evidence cover narrow viewport continuity.

## 5. Surface integrations and explicit exclusions

- [x] 5.1 Integrate profile text intake into `frontend/src/views/Profile.vue` for low-risk roles/locations/include/exclude keywords only; keep remote rules, compensation, authorization, and hard exclusions manual.
- [x] 5.2 Integrate interview-context intake into `frontend/src/views/AgentWorkbench.vue`; apply only to local `user_context`, fence it as untrusted user input for later Agent execution, and require the existing separate start action with `canonical_job_id`, bound job/resume/profile/evidence inputs.
- [x] 5.3 Leave `frontend/src/views/Resumes.vue`, `CrawlPlans.vue`, `ApplicationWorkspace.vue`, send/submit, package approval, OAuth, and external form flows without smart confirmation controls; add regression coverage for these exclusions.
- [x] 5.4 Run the existing Ego browser harness in an isolated `careerops smart intake` task space for the releasable default-deny states (disabled, dismissed, and manual-only) at desktop and narrow mobile widths; record route/state/assertion evidence in `openspec/changes/ai-assisted-smart-forms/reviews/smart-intake-browser.md` and do not retain personal-data screenshots. Ready/invalid/stale provider-enabled states remain separate pilot qualification evidence.

## 6. Verification and release evidence

- [x] 6.1 Add model-boundary tests for valid/allowlisted output, protected fields, source-span validation, prompt-injection handling, size/token/rate limits, disabled mode, and shared gateway failure mapping; real-provider qualification remains outside this change.
- [x] 6.2 Add disposable-PostgreSQL integration coverage for migration, candidate ownership, retention purge, append-only SECURITY DEFINER enforcement, and direct-delete denial; service-level idempotency/stale/apply paths are covered by unit/contract tests, while the broader repository integration suite runs when `CAREEROPS_TEST_DATABASE_URL` is supplied.
- [x] 6.3 Update `Makefile:verify-frontend` and CI so the reproducible command runs `cd frontend && npm ci && npm test -- --run && npm run build`; keep browser and real-provider evidence separate from this build/test gate.
- [x] 6.4 Run `make verify`, frontend verification, security/secret scans, API negative-path checks, and the browser harness with model provider, OAuth, external writes, and auto-send disabled.
- [x] 6.5 Record current evidence and independent review findings; fix every blocking finding before marking the MVP release-ready.

- [x] 6.6 Gate AgentWorkbench integration on a passing Profile vertical slice, including API contract, persistence, disabled-mode, apply/merge, and browser evidence.

## 7. Pilot boundary

- [x] 7.1 Add a runbook distinguishing disabled-mode engineering evidence, fake-provider evidence, browser/UI evidence, and real-provider qualification.
- [x] 7.2 Keep the repository and Compose defaults model-disabled; enable no real provider or external capability as part of this change.
- [x] 7.3 Defer resume-file/job-URL smart intake until a follow-up spec defines a server-resolved evidence projection and authoritative destination API.

## Evidence boundary

The checked items describe the default-deny MVP that is safe to release for
manual-first use. The Ego run covered the currently releasable disabled,
dismissed, and manual-only states at desktop and narrow mobile widths. A
provider-enabled `ready`/`invalid`/`stale` browser qualification is deliberately
not claimed here; fake-provider and backend tests cover those state transitions,
while a real-provider pilot requires a separately approved fixture and review.
