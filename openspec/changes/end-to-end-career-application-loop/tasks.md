## 1. Contract freeze, scope, and release switches

- [x] 1.1 Add a superseding ADR that records the new application-centered product scope, including system-managed email delivery after CareerOps confirmation and the continued exclusion of unattended mass apply.
- [x] 1.2 Add a threat-model delta covering crawl-plan control, resume tailoring, system-managed Gmail send, inbound email classification, thread association, and reply drafts.
- [x] 1.3 Add canonical enums and transition tables for `SubmissionChannel`, crawl plan/run states, package states, mail event proposal states, review states, and reconciliation states.
- [x] 1.4 Add capability resolver entries for crawl plans, model tailoring, Gmail read, system-managed send, and auto-send with safe defaults and fail-closed unknown behavior.
- [x] 1.5 Define API error codes and response envelopes for invalid state, stale payload, unavailable dependency, denied policy, unresolved email link, and reconciliation-required outcomes.
- [x] 1.6 Add contract tests proving the default configuration keeps Google OAuth, external writes, and auto-send disabled while preview, manual tracking, and deterministic matching remain usable.
- [x] 1.7 Update README, handoff, and local runbook terminology so “CareerOps confirmation causes system-managed send” is not described as manual Gmail sending.

## 2. Database identity, candidate ownership, and migrations

- [x] 2.1 Add a durable user-to-candidate association with a uniqueness constraint for the single-user runtime and server-side lookup support.
- [x] 2.2 Add profile-version storage for target roles, locations, remote rules, compensation, seniority, authorization, include keywords, exclude keywords, and hard exclusions.
- [x] 2.3 Add validation constraints and service-level validation for contradictory profile preferences and invalid compensation/location values.
- [x] 2.4 Extend resume-version storage with parse status, confirmation status, target type, source reference, content hash, and lifecycle timestamps if not already present in the current schema.
- [x] 2.5 Add evidence-item storage or projection fields for candidate claims, source spans, confirmation status, extractor version, and evidence hashes.
- [x] 2.6 Add `application_cycle` identity and uniqueness constraints so one candidate/job pair has one active cycle while explicit re-application cycles retain prior history.
- [x] 2.7 Add `submission_channel`, package version, payload hash, and provider linkage fields to the application projection without weakening append-only event history.
- [x] 2.8 Add additive migration guards, downgrade behavior, role grants, and runtime capability checks for all new tables and columns.
- [x] 2.9 Run disposable PostgreSQL migration tests for clean upgrade, downgrade where supported, uniqueness, ownership scoping, and runtime-role permissions.
- [x] 2.10 Backfill existing single-user records into the new candidate/profile/cycle/provenance fields, quarantine ambiguous ownership links for user review, and test that no legacy job/application/email history is silently reassigned.
- [x] 2.11 Wire new repositories, application services, capability resolver, and projections through `infrastructure/runtime.py` and `api/app.py`, with explicit dependency-not-ready behavior instead of silently using an unscoped or in-memory fallback.

## 3. Career profile, resume, and evidence services

- [ ] 3.1 Implement profile create/update/read services that create immutable versions and activate only validated versions.
- [ ] 3.2 Add profile API routes scoped to the authenticated user's candidate and reject client-supplied candidate substitution.
- [ ] 3.3 Add resume registration with content-addressed storage, media/size validation, duplicate hash handling, and immutable version creation.
- [ ] 3.4 Connect deterministic resume parsing to parse-status persistence and expose parse errors without persisting unsupported raw artifacts.
- [ ] 3.5 Implement evidence extraction records with bounded source spans, extractor version, and confirmed/unconfirmed state.
- [ ] 3.6 Add evidence confirmation/rejection endpoints with idempotency and append-only audit events.
- [ ] 3.7 Add model-input minimization for optional tailoring so only selected candidate evidence and minimum job text can leave the local boundary.
- [ ] 3.8 Add unit and security tests for unsupported claims, duplicate resumes, expired content, model-disabled operation, and forbidden egress markers.
- [ ] 3.9 Add frontend views for profile preferences, resume versions, parse status, evidence review, and active-version selection with empty/error states.

## 4. Crawl sources and user-managed plans

- [ ] 4.1 Define the `CrawlSource`, `CrawlPlanVersion`, and `CrawlRun` domain contracts and repository protocols.
- [ ] 4.2 Implement source registration for official company Careers, Greenhouse, Lever, and Ashby sources using existing adapters and source identifiers.
- [ ] 4.3 Store source trust, terms/robots status, adapter version, enabled/paused state, last-run metadata, and safe policy status.
- [ ] 4.4 Implement crawl-plan version creation with source selection, themes, keyword rules, location/remote rules, seniority, compensation, content scope, interval, timezone, and per-run limits.
- [ ] 4.5 Implement plan pause/resume, source pause/resume, manual run requests, and idempotent command handling.
- [ ] 4.6 Implement schedule validation and next-run calculation with bounded intervals, timezone validation, and no overlapping unbounded runs.
- [ ] 4.7 Add API routes for source CRUD, plan CRUD/versioning, run-now, pause/resume, and run history.
- [ ] 4.8 Add frontend “采集计划” workspace with source status, effective filters, schedule, last/next run, pause/resume, and run-now actions.
- [ ] 4.9 Add frontend run-history detail with discovered/updated/closed/failed counts, safe error category, backoff time, and inbox links.
- [ ] 4.10 Add contract tests for unsupported sources, invalid schedules, plan version immutability, pause behavior, and authenticated ownership.

## 5. Crawl execution, safety, and provenance

- [ ] 5.1 Bind every crawl activity to an immutable plan-version snapshot and run identity before any source request starts.
- [ ] 5.2 Integrate existing HTTP/browser adapters with source policy evaluation, SSRF checks, redirect/DNS checks, terms/robots decisions, response limits, and per-domain rate limits.
- [ ] 5.3 Ensure unknown or unavailable crawl policy dependencies fail closed or enter the documented safe limited-discovery state.
- [ ] 5.4 Add Temporal workflow/activity wiring for scheduled and manual runs, retries, cancellation, progress, and durable terminal status.
- [ ] 5.5 Make source observation ingestion idempotent by run/source/external identity and content hash.
- [ ] 5.6 Persist crawl run, plan version, source, parser version, capture time, and content hash on each resulting posting/version.
- [ ] 5.7 Add source-specific backoff and stop rules for 403, 429, CAPTCHA/login walls, terms blocks, parser drift, and repeated failures.
- [ ] 5.8 Add integration tests for restart/retry, overlapping schedules, SSRF redirects, private/metadata targets, rate limits, and duplicate observations.
- [ ] 5.9 Add metrics for active runs, duration, source failures, rate-limit denials, postings created/updated/closed, and policy denials with bounded labels.
- [ ] 5.10 Gate the next phase on a read-only crawl slice that creates a plan, runs it, persists provenance, and renders new/updated/closed jobs in the inbox without duplicate records.

## 6. Job inbox, filtering, and matching

- [ ] 6.1 Connect the job projection to the active profile version and crawl-plan provenance without changing canonical job identity semantics.
- [ ] 6.2 Implement deterministic hard filters for role, location, remote, authorization, compensation, source status, and explicit exclusions.
- [ ] 6.3 Persist filter decisions with profile version, rule version, blocking reasons, and evidence references.
- [ ] 6.4 Keep deterministic dedup/merge/split decisions reversible and expose semantic matches only as reviewable proposals.
- [ ] 6.5 Add requirement-level match results with job evidence, candidate evidence, confidence, rules/model versions, and review-only status.
- [ ] 6.6 Ensure model-disabled/unavailable/invalid output falls back to deterministic filtering and marks semantic ranking unavailable.
- [ ] 6.7 Add API support for inbox filters, cursor pagination, search, favorite, ignore, snooze, excluded-reason inspection, and job detail evidence.
- [ ] 6.8 Make favorite and ignore actions idempotent and record user decisions without deleting source history.
- [ ] 6.9 Add frontend inbox cards/table with recommended/excluded tabs, deterministic blocking reasons, evidence links, and next-action affordances.
- [ ] 6.10 Add unit/contract tests for hard-gate precedence, unknown remote, profile-version provenance, prompt injection, reversible merge, and stale job handling.

## 7. Application workspace and lifecycle

- [ ] 7.1 Refine the application domain state machine so `FAVORITED`, `PREPARING`, `SUBMITTED`, `INTERVIEWING`, `OFFER`, `REJECTED`, `WITHDRAWN`, and `ON_HOLD` transitions match the new contract.
- [ ] 7.2 Implement application create/get-or-create using candidate/job/cycle idempotency and authenticated candidate ownership.
- [ ] 7.3 Implement favorite-to-preparing and preparing-to-submission validation services without allowing model output to transition state.
- [ ] 7.4 Define the application channel-eligibility interface for verified email contact, official external form, and manual handling; defer concrete contact resolution to group 9.
- [ ] 7.5 Define the application/package binding interface for job version, resume version, evidence, and application package version; consume the package implementation from group 8.
- [ ] 7.6 Implement append-only application timeline projection for user decisions, package events, submissions, mail proposals, reviews, reminders, and provider receipts.
- [ ] 7.7 Implement external-form flow with trusted URL display, manual checklist, explicit completion confirmation, and no automatic submitted claim.
- [ ] 7.8 Add API routes for application detail, state actions, channel selection, package selection, timeline, and manual submission confirmation.
- [ ] 7.9 Add frontend application workspace with job evidence, channel choice, package status, exact payload preview, timeline, and pending actions.
- [ ] 7.10 Add contract tests for duplicate application creation, illegal transitions, stale package approval, manual-form abandonment, and submitted evidence.
- [ ] 7.11 Complete a vertical application-workspace slice using deterministic fixtures: inbox favorite → preparation → package binding → external-form/manual confirmation → timeline projection.

## 8. Job-specific application packages

- [ ] 8.1 Define application-package version contracts for resume presentation, cover letter, answers, email subject/body, attachments, claim references, and approval state.
- [ ] 8.2 Implement package draft creation from exact job version, profile version, confirmed resume, and selected evidence.
- [ ] 8.3 Implement deterministic requirement/evidence comparison and explainable gap output.
- [ ] 8.4 Add optional LangGraph tailoring workflow that returns structured suggestions and never writes trusted claims or external payloads directly.
- [ ] 8.5 Implement diff generation against immutable resume content and preserve user edits as new package versions.
- [ ] 8.6 Reject package approval when any positive claim lacks evidence, any required resume confirmation is missing, or any source content is stale beyond policy.
- [ ] 8.7 Implement package approval with actor, timestamp, input identities, payload hash, attachment hashes, and invalidation on mutation.
- [ ] 8.8 Add frontend package editor/review with side-by-side diff, evidence references, unresolved warnings, attachment status, and approval action.
- [ ] 8.9 Add tests for unsupported model claims, evidence binding, diff/version behavior, stale inputs, attachment mutation, and model-disabled package drafting.
- [ ] 8.10 Gate email work on a package slice that creates a job-specific diff, rejects unsupported claims, approves an exact payload, and proves any input mutation invalidates approval.

## 9. Trusted contacts and initial email payloads

- [ ] 9.1 Define trusted recruiting-contact resolution using source evidence, company domain, contact type, confidence, and retention metadata.
- [ ] 9.2 Reject guessed employee addresses, unverified domains, missing source evidence, and model-only recipients before payload creation.
- [ ] 9.3 Build initial application email payloads from the approved package, selected account, recipient, subject, body, attachment set, and application binding.
- [ ] 9.4 Add normalized payload hash, attachment hashes, thread headers when applicable, evidence references, and stable idempotency/reconciliation keys.
- [ ] 9.5 Implement MIME/size/hash/quarantine/retention validation for resume and other permitted attachments.
- [ ] 9.6 Add a submission preview endpoint that returns the exact sendable representation and validation errors without provider side effects.
- [ ] 9.7 Add frontend confirmation screen that makes recipient, subject, body, attachments, source evidence, and “system will send after confirmation” explicit.
- [ ] 9.8 Add tests for domain mismatch, guessed addresses, mutated package payloads, unsafe attachments, preview-without-send, and exact preview/send parity.

## 10. System-managed Gmail sending and reconciliation

- [ ] 10.1 Implement the CareerOps final-confirmation command as a durable approval/intent request rather than a direct provider call.
- [ ] 10.2 Revalidate candidate ownership, application state, package approval, recipient eligibility, capability release, policy version, account status, and payload hash at confirmation time.
- [ ] 10.3 Enqueue one transactional Outbox event linked to the action intent and return a durable pending status to the UI.
- [ ] 10.4 Wire the isolated Side-effect Worker to resolve opaque credentials, call the Gmail provider, and persist provider attempts without exposing tokens to API/model paths.
- [ ] 10.5 Implement provider receipt persistence and application `SUBMITTED` event creation only after confirmed provider success.
- [ ] 10.6 Implement timeout/connection-loss/worker-crash reconciliation by stable key and disable blind retry for ambiguous outcomes.
- [ ] 10.7 Make repeated confirmation idempotent and return existing intent/receipt/reconciliation state.
- [ ] 10.8 Ensure disabled external-write, disabled Google, invalid qualification, revoked account, Redis failure, or policy failure all deny before provider call.
- [ ] 10.9 Add fake-provider crash matrix tests: before-call crash, after-call-before-receipt crash, timeout-with-success, duplicate confirmation, credential revoke, and reconciliation escalation.
- [ ] 10.10 Add controlled integration tests with a fake or offline provider harness and verify no duplicate provider effects without enabling live OAuth, external-write, or auto-send flags.
- [ ] 10.11 Add UI send-progress, sent, failed, and reconciliation-required states; never display queued as sent.
- [ ] 10.12 Gate inbound/reply work on a system-managed fake-provider slice: CareerOps confirmation creates one intent, the worker sends without manual Gmail action, receipt updates the application, and ambiguous outcomes stop in reconciliation.

## 11. Gmail read synchronization and thread association

- [ ] 11.1 Implement explicit dedicated-account connection state with allowed read scope validation and protected credential reference storage.
- [ ] 11.2 Implement account revoke/error handling that immediately stops future sync and provider use and invalidates pending provider actions.
- [ ] 11.3 Implement incremental history/watch cursor storage, retry, backfill, and durable sync-run status.
- [ ] 11.4 Enforce provider message/thread ID uniqueness and duplicate Pub/Sub/polling delivery handling.
- [ ] 11.5 Persist only minimum metadata for non-recruitment mail and apply attachment quarantine/retention for recruitment content.
- [ ] 11.6 Implement thread association using provider IDs, sent-message linkage, trusted domains, subject/source evidence, and unresolved multi-candidate state.
- [ ] 11.7 Add API routes for account status, sync-now, sync history, threads, messages, unresolved links, and user link confirmation.
- [ ] 11.8 Add frontend follow-up inbox with stale sync status, thread summaries, unresolved association queue, and evidence links.
- [ ] 11.9 Add integration tests for duplicate messages, partial sync restart, revoked account, non-recruitment body discard, ambiguous thread link, and confirmed link reuse.
- [ ] 11.10 Add bounded cursor pagination, `Cache-Control: no-store`, and response-size limits for sync runs, threads, messages, unresolved links, and evidence-bearing collections.

## 12. Mail intelligence and application event proposals

- [ ] 12.1 Define controlled mail taxonomy for acknowledgement, screening, interview, assessment, request-more-info, rejection, offer, salary, visa, identity, withdrawal, and unknown.
- [ ] 12.2 Implement deterministic extraction of provider metadata, sender, dates, timezones, deadlines, and obvious outcomes before optional model enrichment.
- [ ] 12.3 Implement structured model extraction with minimized input, schema validation, confidence, evidence spans, version metadata, and review-only output.
- [ ] 12.4 Detect high-risk categories and force mandatory user review without provider/action side effects.
- [ ] 12.5 Create durable `EmailEventProposal` records with idempotency and link to message/thread/application.
- [ ] 12.6 Implement accept/reject proposal endpoints with ownership, CSRF, rate limit, legal transition validation, and repeat-decision handling.
- [ ] 12.7 Append accepted proposals to application timeline and create interview/deadline/follow-up tasks where policy permits.
- [ ] 12.8 Add prompt-injection fixtures covering recipient changes, secret requests, policy bypass, HTML/Unicode confusion, attachments, and signatures.
- [ ] 12.9 Add tests for rejection, interview, offer, unknown, low-confidence, stale message, invalid schema, and model-unavailable paths.
- [ ] 12.10 Gate reply work on a mail-intelligence slice that ingests a fixture thread, links it or leaves it unresolved, produces a reviewable event proposal, and updates application state only after acceptance.

## 13. Reply drafting and follow-up management

- [ ] 13.1 Define follow-up rule versions and default waiting periods for submitted, awaiting-response, interview, assessment, and user-specified states.
- [ ] 13.2 Implement reminder creation, deduplication, snooze, reschedule, complete, cancel, and terminal-state cleanup.
- [ ] 13.3 Implement reply-draft context assembly from linked thread, bounded excerpt, confirmed application facts, selected evidence, and user intent.
- [ ] 13.4 Implement reply drafting with recipient/thread-header immutability and unsupported-claim validation.
- [ ] 13.5 Implement draft edit/version/payload-hash handling; recipient or target edits must create a new proposal and re-enter policy.
- [ ] 13.6 Reuse initial-email delivery services for user-confirmed reply sends; keep auto-send disabled and high-risk categories permanently denied.
- [ ] 13.7 Add API routes for draft list/detail, create, edit, approve, reject, and reminder actions.
- [ ] 13.8 Add frontend review queue with draft diff, thread context, risk category, recipient, evidence, confirmation action, and send outcome.
- [ ] 13.9 Add tests for scheduling reply, follow-up, resume/link, work authorization, salary/offer/visa denial, target mutation, duplicate approval, and ambiguous send.
- [ ] 13.10 Add bounded pagination, no-store headers, ownership checks, and idempotency contracts for drafts, review items, and reminders.

## 14. Unified frontend workspaces and UX quality

- [ ] 14.1 Add authenticated routes for profile, resumes, crawl plans, crawl runs, job inbox, application workspace, mail follow-up, and review queue.
- [ ] 14.2 Replace hard-coded candidate/job identifiers with current-session API resolution and meaningful dependency-not-ready states.
- [ ] 14.3 Add a job-detail action path from recommendation to favorite, prepare, package, channel, and application timeline.
- [ ] 14.4 Add visible “why recommended” evidence and deterministic exclusion reasons.
- [ ] 14.5 Add application timeline rendering that distinguishes confirmed, proposed, rejected, pending, failed, and reconciliation-required states.
- [ ] 14.6 Add all required loading, empty, stale, unavailable, validation-error, and next-action states for each new view.
- [ ] 14.7 Add responsive layouts for desktop and mobile widths without hiding recipient, attachment, or approval information.
- [ ] 14.8 Add frontend unit tests for route guards, session failure, source/plan CRUD states, package diff, confirmation behavior, thread review, and reminder actions.
- [ ] 14.9 Run `make verify-frontend` and enforce the existing bundle-size budget.

## 15. Cross-cutting verification, observability, and security

- [ ] 15.1 Add repository contract tests for all new API schemas, status codes, idempotency responses, and authenticated ownership rules.
- [ ] 15.2 Add state-machine property tests for application, package, email proposal, reminder, outbox, and reconciliation transitions.
- [ ] 15.3 Add PostgreSQL integration tests for new migrations, append-only events, role grants, ownership filters, unique keys, and rollback behavior.
- [ ] 15.4 Add Temporal tests for crawl/sync restart, deterministic replay boundaries, schedule overlap, cancellation, and worker identity.
- [ ] 15.5 Add side-effect security tests proving no API/model/parser path can read provider secrets or call provider writes directly.
- [ ] 15.6 Add secret/PII scans for logs, payloads, model egress, attachments, database dumps, and frontend error responses.
- [ ] 15.7 Add metrics for crawl plans/runs, inbox decisions, package approvals, send intents, provider receipts, reconciliation, mail proposals, review latency, and follow-up completion with bounded labels.
- [ ] 15.8 Add audit assertions for profile confirmation, package approval, email confirmation, provider receipt, mail-event review, reply approval, and reminder changes.
- [ ] 15.9 Run `make verify`, `make verify-db`, `make verify-temporal`, `make verify-frontend`, `make security`, and relevant dependency/image audits in disposable environments.
- [ ] 15.10 Add compatibility tests proving legacy internal drafts, old application events, and historical email receipts are projected accurately and never reported as newly sent provider messages.
- [ ] 15.11 Add staged verification commands/evidence for the crawl slice, application/package slice, fake-provider send slice, and mail-intelligence slice so each boundary has an independently recorded pass/fail result.
- [ ] 15.12 Add operator runbooks and bounded alerts for stuck crawl runs, repeated source denials, stale mail cursors, pending approvals, reconciliation backlog, revoked accounts, and failed retention/purge work.
- [ ] 15.13 Add an authority audit over legacy scripts and adapters proving no direct Gmail/provider write or unguarded external fetch bypasses the current policy/kernel path.
- [ ] 15.14 Add worker batch-size, API response-size, crawl backpressure, mailbox sync backfill, and rate-limit tests so large sources/messages cannot create unbounded memory or UI payloads.

## 16. Pilot, staged enablement, and handoff

- [ ] 16.1 Prepare real, legal, de-identified discovery/parser/dedup pilot artifacts and manifests without modifying the repository's synthetic fixtures.
- [ ] 16.2 Prepare evidence-match, contact, email, policy/injection, and reconciliation pilot slices according to the existing D0 ownership and labeling plan.
- [ ] 16.3 Conduct independent review of source/consent/retention, de-identification, model provider terms, Gmail scope, and dedicated-account setup.
- [ ] 16.4 Run a controlled end-to-end dry run with system-managed send disabled: crawl plan → inbox → package → preview → manual/external-form confirmation → mail sync simulation → reply draft.
- [ ] 16.5 Run fake-provider/offline harness tests that verify CareerOps confirmation triggers system-managed execution without a manual Gmail step; do not enable live OAuth, external-write, or auto-send flags in this change.
- [ ] 16.6 Verify rollback by disabling send/read capabilities while preserving application history, drafts, receipts, audit events, and manual tracking.
- [ ] 16.7 Record product metrics for trusted shortlist time, user corrections, package approval, confirmed submission, mail linkage, review latency, and follow-up completion.
- [ ] 16.8 Update handoff, runbooks, API documentation, frontend documentation, and release qualification evidence with actual results rather than planned claims.
- [ ] 16.9 Do not claim the full D0 or release gate is complete until real pilot, independent review, privacy/legal, custody, and qualification evidence exist.

## 17. Milestone gates and scope control

- [ ] 17.1 Gate A — Deliver a read-only discovery slice: user profile → managed crawl plan → provenance-backed job inbox → favorite/ignore, with no external writes.
- [ ] 17.2 Gate B — Deliver an application-preparation slice: favorite → preparing → confirmed resume/evidence → job-specific package diff → package approval → external-form/manual tracking.
- [ ] 17.3 Gate C — Deliver the fake-provider system-managed send slice: CareerOps confirmation → durable intent/outbox → worker → fake provider receipt/reconciliation → submitted timeline, with no manual Gmail step and all prohibited runtime flags still disabled.
- [ ] 17.4 Gate D — Deliver inbound mail intelligence: fixture/dedicated-input sync → thread association or unresolved review → event proposal → user acceptance → legal application transition.
- [ ] 17.5 Gate E — Deliver reply drafts and follow-up: reminder → constrained draft → CareerOps approval → fake-provider send path → receipt/timeline, with auto-send disabled.
- [ ] 17.6 Keep live Gmail OAuth, live provider activation, external-write flag enablement, auto-send, third-party form automation, social-source expansion, calendar automation, and multi-user support in separate future changes with their own proposal, threat-model delta, qualification, and approval.
- [ ] 17.7 Do not start the next capability expansion until the prior gate has a passing contract/integration result and at least one real-user workflow review with recorded corrections.
