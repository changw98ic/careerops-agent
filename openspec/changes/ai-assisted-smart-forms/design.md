## Context

The Vue console already has a structured, versioned career-profile form and an Agent workbench with a free-text interview context. The backend owns candidate-scoped validation, immutable versions, evidence confirmation, CSRF/idempotency handling, and the `StructuredModelClient`; its safe default is `MODEL_PROVIDER=disabled`. Resume registration already has deterministic parsing and explicit confirmation, while crawled jobs are read-only projections. There is no safe destination for an AI-generated resume/job form payload in this change.

The MVP therefore adds a preview layer only to Profile and the Agent interview-context field. It is synchronous and bounded because the current model gateway is synchronous; it does not pretend to be a durable Agent run. The preview is a short-lived review artifact, not a profile, resume, job, crawl run, package, or application resource.

## Goals / Non-Goals

**Goals:**

- Convert bounded career-goal text into reviewable, low-risk Profile fields.
- Convert bounded interview-intent text into a reviewable `user_context` draft for AgentWorkbench.
- Freeze target schemas, field limits, source-reference rules, protected-field rules, and lifecycle semantics before implementation.
- Reuse existing final save/start APIs after the user explicitly applies a preview.
- Make disabled, unavailable, malformed, stale, expired, and rate-limited states truthful and useful.
- Preserve default-deny, no-tools, evidence-first, append-only audit, and manual fallback invariants.

**Non-Goals:**

- AI parsing of uploaded resume files, raw resume bytes, arbitrary job URLs, browser pages, mailbox content, or credentials.
- AI creation or mutation of a canonical job, resume/evidence item, crawl plan, application package, email, or external form.
- Replacing deterministic controls for compensation, authorization/visa, remote policy, hard exclusions, consent, dates, permissions, or external actions.
- Starting an Agent run from preview apply; the user must separately use the existing Agent start action.
- Async queueing, voice input, a general chat surface, arbitrary prompt/schema editing, or model-quality qualification.

## Decisions

### 1. Freeze the MVP target and field matrix

Only two targets are released by this change:

| Target | Input | Accepted preview fields | Always manual/protected |
| --- | --- | --- | --- |
| `profile` | bounded text, max 12,000 Unicode characters | `target_roles[]` (max 5; title 120, seniority 80, notes 240), `locations[]` (max 8; name 120, `kind=preferred` only, `radius_km` nullable 0–500), `include_keywords[]` (max 30 × 80), `exclude_keywords[]` (max 30 × 80) | top-level `seniority`, `remote_rules`, `compensation`, `authorization`, `hard_exclusions`, consent, activation |
| `interview_context` | bounded text, max 2,000 Unicode characters, plus optional selected input IDs for staleness only | `user_context` (max 2,000 characters) | job/profile/resume/evidence facts, answers, achievements, application state, Agent start |

`job_context` and `resume_context` are rejected as unsupported targets in this change. A later change may add them only after it defines an authoritative destination and a server-resolved confirmed-evidence projection. No input accepts a URL, file upload, raw resume content, or client-supplied prompt/schema. `profile` may include `profile_version_id` (or the server-resolved active version, or the sentinel `none`) for staleness. `interview_context` SHALL include `canonical_job_id`, `job_version_id`, and `resume_version_id`; it MAY include `profile_version_id` and up to 50 `evidence_ids`. The canonical job is a shared read-only resource; all candidate-owned references must belong to the authenticated candidate. These references are identity/context only in the MVP and are passed to the later Agent start action, not promoted to model facts by smart intake.

### 2. Use one typed preview API, synchronously

Add:

- `POST /api/v1/smart-intake/previews` to create or reuse a preview.
- `GET /api/v1/smart-intake/previews/{preview_id}` to retrieve an owned preview.
- `POST /api/v1/smart-intake/previews/{preview_id}/apply` to record decisions and return a non-persisted draft patch.

The create request is discriminated by target:

```text
{
  target: "profile" | "interview_context",
  input: { kind: "text", text: string },
  context_refs?: {
    profile_version_id?: UUID
  },
  // Required when target=interview_context:
  interview_refs?: {
    canonical_job_id: UUID,
    job_version_id: UUID,
    resume_version_id: UUID,
    profile_version_id?: UUID,
    evidence_ids?: UUID[]
  },
  idempotency_key: string
}
```

`context_refs`/`interview_refs` are server-verified identity references used for stale detection; the current MVP builders use the submitted text only. The later Agent handoff still requires the existing `canonical_job_id`, `job_version_id`, `resume_version_id`, optional profile version, and confirmed evidence IDs. Smart-intake does not make an unconfirmed resume or evidence item eligible.

The apply request is:

```text
{
  apply_idempotency_key: string,
  context_digest: string,
  decision_set_hash: string,
  decisions: [{
    path: closed-field-path,
    decision: "accept" | "edit" | "reject" | "unknown",
    value?: target-typed-value,
    reason?: bounded-string
  }]
}
```

The server recomputes the context digest and decision-set hash and rejects a mismatch. A client-supplied digest is only a comparison token, never authority. The decision hash is SHA-256 over canonical JSON (sorted keys, sorted field paths, typed values, bounded reasons, no whitespace, UTF-8). Apply returns `{ preview_id, draft_patch, decision_set_hash }`; the target API remains responsible for final persistence or Agent execution.

### 3. Store a short-lived review artifact, not a domain mutation

A preview row is candidate-owned and contains the bounded field proposal, input digest, context digest, source spans, schema/prompt/model versions, capability state, public state, trace ID, timestamps, `expires_at`, and a non-sensitive tombstone status. It expires 30 minutes after creation. Expired/revoked preview values are purged within 24 hours by the retention role; the tombstone remains so retrieval/apply can return `410` without returning values. The append-only decision/audit record retains hashes, keys, actor, outcome, and bounded reasons but not raw prompt/response content. Responses, including GETs and errors, set `Cache-Control: no-store`.

Decision rows intentionally persist only closed metadata (`path`, decision,
bounded reason, submitted value digest, and immutable proposal value digest). The non-persisted `draft_patch` is rebuilt
from the immutable preview fields and the equivalent apply request on retry;
the database never needs to retain an edited field value in the append-only
decision record. An expired or purged tombstone takes precedence over replay so
no old field value is disclosed. Migrations `0028` and `0029` scrub rows
written by the earlier prototype and add the bounded proposal digest before
restoring the append-only trigger; they are irreversible and must be treated
as forward-only after release.

Apply replay returns the same draft only while immutable preview values are
retained. Once a preview is expired, revoked, or purged, its tombstone wins and
returns `410` without rebuilding or disclosing the old draft.

The preview table requires a non-null candidate foreign key and uniqueness on `(candidate_id, target, idempotency_key)`. A canonical request fingerprint is SHA-256 over normalized NFC text, target, context references, and schema/policy versions. A short-lived atomic claim (`claim_state`, `claim_expires_at`) is inserted before model invocation; an in-flight claim returns `409 SMART_PREVIEW_IN_PROGRESS`, and a crashed claim may be reclaimed after its 30-second lease. Reusing a key with a different fingerprint returns `409 IDEMPOTENCY_KEY_REUSED`. Apply has its own idempotency key and decision-set hash; replay returns the same draft and creates no duplicate decisions.

### 4. Enforce ownership, state, and stale checks in one transaction

Every preview, context reference, and apply query includes the authenticated candidate predicate. Cross-candidate preview IDs return indistinguishable `404 SMART_PREVIEW_NOT_FOUND`. Creation verifies all referenced versions in the same transaction. The context digest is SHA-256 over canonical JSON containing target, active/base profile ID and rules version (or `none`), and for interview context the canonical job ID, job-version content hash, resume-version content hash, selected profile rules version, sorted confirmed evidence IDs/hashes, schema version, and policy version. Apply re-reads these values under a row lock, compares the server-computed digest, and returns `409 STALE_SMART_INTAKE_PREVIEW` on mismatch. Expired, revoked, or purged previews return `410 SMART_PREVIEW_EXPIRED` without revealing their contents.

### 5. Keep the model boundary shared and default-deny

Add a `SMART_INTAKE` capability, a `smart_intake_enabled` setting/env flag, an `AuthAction`/rate-limit entry, and canonical API/OpenAPI error codes; all default to denied/false. Capability-denied requests return `403 SMART_INTAKE_DISABLED` before input assembly. When the capability is released but the provider remains disabled, the endpoint returns `200 state=unavailable`, makes zero provider calls, and leaves manual entry available. A provider timeout, 429, 5xx, or unavailable dependency maps to `200 state=unavailable`; explicit model abstention maps to `200 state=abstained`; schema/repair failure maps to `200 state=invalid`. Enabled calls use `StructuredModelClient` with no tools, an untrusted-content envelope, `additionalProperties: false`, a total 15-second deadline including retry/backoff, max output tokens 768, one repair attempt that preserves the fenced input, and egress redaction. Partial output is never trusted.

### 6. Apply is a local patch and never an action trigger

Profile applies merge only accepted/edited low-risk fields into the local form fields that the user has not changed since preview creation. The closed field paths are `target_roles[i].title|seniority|notes`, `locations[i].name|radius_km`, `include_keywords[i]`, and `exclude_keywords[i]`; array order is the normalized preview order, `radius_km=null` means no radius preference, and `kind` is always `preferred`. The frontend stores the local baseline snapshot/digest at preview creation. If an indexed item or array differs from that baseline, the panel leaves it unchanged and shows a conflict requiring manual editing; it never silently replaces or reorders dirty items. Interview applies replace only the editable `user_context` draft; `Apply` and `Start interview preparation` remain separate buttons. When the user later starts an Agent run, `user_context` is fenced as untrusted user input and cannot become evidence or policy authority. No smart-intake route imports or calls crawl, Ego, Agent-start, package, send, OAuth, or application-submit services.

### 7. Use existing request protections and fail closed

Create/apply require the existing authenticated session, CSRF mechanism, and allowlisted Host+Origin validation; production cannot fall back to auth-less access. The `smart_intake_preview` limiter applies 10 preview creations per candidate-hash per 10 minutes and one in-flight claim; Redis/limiter failure denies the request. Logs, traces, analytics, local storage, audit JSON, and metric labels exclude raw values, secrets, email addresses, job titles, and trace IDs as high-cardinality labels. Audit/decision writes use the shared `careerops.append_audit_event` SECURITY DEFINER path in the same transaction; the API role never inserts directly. The model receives untrusted data only and has no tool binding.

### 8. Use one frontend review component with thin adapters

Create `SmartIntakePanel` and a small composable for create/get/apply state. It renders input, loading, capability/provider state, field-level decision controls, confidence/source-span badges, unknown/blocked reasons, expiry/stale errors, and a manual fallback. Profile and AgentWorkbench integrate it; Resumes, CrawlPlans, ApplicationWorkspace, and send/submit views do not. The component must remain keyboard accessible and usable at narrow mobile widths.

## API and persistence details

Field `source_refs` are server-verifiable references of the form `{ input_digest, start_offset, end_offset }`, with offsets within the submitted text and at most three references per field. They are never accepted as trusted facts merely because the model emitted them. Values are bounded by the target matrix, validated with closed schemas, and rejected when `additionalProperties` is present.

The preview states are exactly `ready`, `unavailable`, `abstained`, `invalid`, `stale`, `expired`, and `revoked`. Creation returns a single synchronous `200` response for a ready or safe non-ready result; it does not claim asynchronous completion. Retrieval and apply are candidate-scoped and non-cacheable.

## Risks / Trade-offs

- **[Risk] Users interpret confidence as truth.** → Label every value as a proposal, show source spans, keep fields editable, and require explicit decisions.
- **[Risk] Prompt injection in pasted text changes model behavior.** → Treat text as untrusted content, provide no tools, use closed schemas, and test tool/policy/secret-instruction canaries.
- **[Risk] A profile save replaces a full snapshot.** → Apply only a patch to untouched local fields and let the existing server validator create the new immutable version.
- **[Risk] Preview values contain personal data.** → Use a 30-minute TTL, purge within 24 hours, `no-store`, no raw prompt/response persistence, bounded logs, and append-only hashes.
- **[Risk] Duplicate/replayed requests spend provider budget.** → Claim idempotency before invocation, fingerprint requests, and use a second idempotency key/hash for apply.
- **[Risk] The model is unavailable.** → Return explicit unavailable/abstained states and keep manual and deterministic workflows fully operable.

## Migration Plan

1. Add the target field matrix, DTOs, capability decision, stable errors, and fake-provider contract tests without enabling the capability or provider.
2. Add additive preview/decision tables, candidate-scoped repository methods, grants, expiry/purge metadata, and migration/rollback tests.
3. Add the synchronous model service, disabled/failure fallback, request protection, bounded metrics, and preview/apply routes.
4. Add the shared frontend component and integrate Profile first; integrate AgentWorkbench interview context only after Profile contract tests pass. Do not change Resumes or action flows.
5. Run `make verify`, `make verify-frontend` (frontend tests plus build), disposable-PostgreSQL, security/adversarial checks, and the existing Ego browser harness with model provider and external writes disabled. Record browser route/state/assertion evidence in `openspec/changes/ai-assisted-smart-forms/reviews/smart-intake-browser.md`; do not store screenshots containing personal data.
6. Release the two MVP surfaces independently in a read-only pilot. A real-provider qualification requires separate independent review and current evidence; it is not implied by this change.

Rollback revokes and purges unapplied preview values, retains non-sensitive tombstones/decision metadata, disables preview creation, and hides the new entry points. Existing Profile, Resumes, Agent, crawl, application, and send workflows remain readable and usable; preview/decision history is retained or purged according to the stated retention policy.

## Open Questions

- A follow-up change must decide whether resume/job smart intake can use a server-resolved confirmed-evidence projection without creating a second domain write path.
- A separate qualification decision must set real-provider quality/latency thresholds; it is not a blocker for the disabled-mode MVP.
