# Bounded-autopilot evidence ledger

- Status date: 2026-07-20
- Decision: **The reviewed Gmail GoalRun chain is implemented through outbox enqueue and
  reconciliation inspection; G013 Greenhouse is separately qualified as a reviewed adapter; no
  live Gmail or Greenhouse credentialed provider effect is configured or release-authorized.**
- Scope: the bounded-autopilot path defined by ADR 0011 plus the later G009-G015 evidence. This
  ledger distinguishes implemented controls from runtime authority and must not be read as
  permission to submit applications or send email.

## Implemented chain

The current composed path is:

```text
configurable source registry/scheduler
  -> canonical public-ATS ingestion
  -> deterministic match and draft preparation
  -> Chinese GoalRun review
  -> reviewed Gmail outbox enqueue
  -> provider receipt/reconciliation inspection
```

G014 composes this path only for the Gmail send boundary. The workflow role does not call Gmail,
read credentials, submit Greenhouse, or mutate application projections directly. It prepares the
exact reviewed `gmail:send` payload, records the Chinese review decision, verifies grant/release/
cap/kill-switch state, and enqueues the reviewed Gmail outbox item. Provider I/O remains behind
the separate disabled-by-default Gmail send worker, broker, credential, release and smoke gates.

G013 Greenhouse Job Board submit is a separate reviewed adapter. It is controlled, default
disabled, and qualified for a narrow employer-authorized Job Board API channel, but it is not part
of the G014 Gmail GoalRun composition and must not be reported as a live or universal auto-apply
path.

## Requirement-to-evidence map

| Objective capability | Current evidence | Status |
| --- | --- | --- |
| Configurable source registry and scheduler | G009 added durable source manifests, operator enablement, cursors, DB-clock due claiming, fenced leases, budgets, rate limits, retries, robots/terms policy, public-ATS-only ingestion, provenance and deterministic deduplication. | Implemented and verified; no external email or application channel is implied. |
| Durable GoalRun orchestration | G010 added owner-scoped GoalRun state, Temporal replay/restart/continue-as-new, phase checkpoints through reconciliation, review/resume/cancel controls, bounded retries, terminal states, operator API/CLI and restricted API/workflow/readonly roles. | Implemented and verified; dispatch initially remained disabled pending provider stories. |
| Gmail read-only signal intake | G011 added a separately scoped `gmail.readonly` subsystem with opaque credential references, fixed-origin GET-only client, list/get/history sync, provider identity deduplication, signal provenance, review proposals, revocation/reset and least-privilege roles. | Implemented and verified; no send/compose/modify/watch/raw-body/attachment authority and no live credential. |
| Reviewed Gmail send boundary | G015 repaired and requalified the G012 lane: exact `gmail.send` and reconciliation `gmail.readonly` scope evidence, reviewed immutable payloads, active grant and exact approval, caps, kill switches, least-privilege mail-sender role, durable attempts/receipts, Sent reconciliation and ambiguity stop. | Implemented and verified in code and disposable PostgreSQL; default disabled and no live send credential or smoke. |
| G014 human-simple-review Gmail composition | G014 composes reviewed crawler output into canonical jobs, deterministic match from `match_config`, exact Gmail draft payload, Chinese simple review, approved outbox enqueue and receipt/reconciliation inspection. | Implemented for Gmail outbox enqueue only; `provider_io_performed=false` in the workflow role. |
| G013 controlled Greenhouse adapter | G013 added a fixed-origin `boards-api.greenhouse.io` adapter with public schema revalidation, allowlisted identity/material fields, hard stops for legal/EEO/assessment/auth/custom questions, reviewed packet binding, transactional caps, durable outbox journal, receipts and reconciliation-before-retry. | Separately qualified as a reviewed adapter; no employer-owned key, broker smoke, live POST or confirmed submission. |
| Campaign-scoped, revocable authority | G008/G005/G015 bind owner, campaign, grant, policy, payload hash, release evidence, cap reservation, expiry, revocation and kill-switch state. Grant revocation, cap reservation and Gmail prepare share the G015 grant-state lock protocol. | Implemented and verified on disposable PostgreSQL. |
| Exception and human-review boundary | Unknown destination policy, hard stops, ambiguous provider state, stale authority, rejected review, exhausted caps and kill switches stop in review/recovery paths. GoalRun review packets are Chinese-first and exact-payload bound. | Implemented and verified. |
| Idempotent dispatch, receipt and reconciliation | Synthetic dispatch, Gmail send and Greenhouse submit all use transactional outbox identity, durable attempts, provider receipt or reconciliation-required state, and no blind retry after ambiguity. Greenhouse `2xx` is only `accepted_unverified`, never confirmed. | Implemented and verified per channel; external provider effects still require credentials, brokers and release qualification. |
| Audit reconstruction | Audit links source evidence, GoalRun, match, payload, review, grant, release qualification, cap, outbox, attempt, receipt/reconciliation and kill-switch state. | Verified through G005/G006 synthetic evidence and later Gmail/Greenhouse PostgreSQL integration evidence. |

## Current verification record

Latest integrated evidence reported for the G014/G007 synthesis:

- `make verify`: Ruff clean, Pyright `0 errors`, `1383 passed`, `156 skipped`, coverage at or
  above `80%`;
- real disposable PostgreSQL verification: `172/172` passed, including migration, role,
  composition, outbox, receipt/reconciliation, revocation, cap and kill-switch paths;
- `make security`: Bandit passed and the bounded secret scan covered `3069` files;
- Compose readiness: the image rebuilt and the migration, PostgreSQL, Redis, Temporal, API,
  crawler publisher and workflow worker all became healthy on custom loopback ports; readiness
  reported model provider, Google OAuth and external writes as disabled, and cleanup removed the
  disposable containers, volumes and network;
- independent G014 reviews: code review `APPROVE`, security review `APPROVE` and architecture
  `CLEAR`; the aggregate G007 checkpoint remains gated on this reconciled ledger and the final
  post-cleaner review.

This verification proves the implemented control plane, review composition, disabled runtime
readiness and database invariants. It does not prove live Gmail send, live Greenhouse submission,
OAuth broker/vault operation, employer credential availability or real-world application success.

## Historical G005/G006 evidence

G005 and G006 remain useful historical evidence, but they are narrower than the current
G009-G015 chain:

- G005 proved the synthetic-only qualified dispatch chain:
  `release evidence -> cap reservation -> generic outbox -> deterministic no-network provider ->
  durable attempt/confirmed receipt or reconciliation -> audit`.
- G006 proved synthetic staged release controls: immutable evidence-set binding, runner/reviewer/
  operator separation, deterministic 50-observation evaluator, fail-open metric rejection,
  kill-switch audit reconstruction and readonly non-authority reporting.
- The refreshed G005/G006 gate previously reported `make verify` with `915 passed`, `42` skips,
  `80.77%` coverage, disposable PostgreSQL `47/47`, and a bounded secret scan over `2959` files.

G005/G006 did not qualify Gmail, Greenhouse, browser automation, live credentials, real provider
execution or employer submission. Later G009-G015 evidence supersedes the stale G005/G006-only
framing for this ledger, but it does not remove those external-effect boundaries.

## Mandatory external-effect blockers

The current package remains default-disabled. External effects require all of the following before
any live send or submit:

1. Explicit user authorization for the exact campaign, destination, account and release stage.
2. Channel-specific credentials and auth: Gmail BYO OAuth/broker/vault material or an employer-
   authorized Greenhouse Job Board API key profile, exposed only through the documented broker.
3. Current release qualification bound to the exact commit, migration, config, adapter, account,
   credential profile, broker version, cap settings, kill-switch state and smoke evidence.
4. Live controlled smoke evidence for the exact channel: Gmail send plus Sent/read-only
   reconciliation, or Greenhouse POST plus employer-authoritative reconciliation. A Job Board
   `2xx` response alone is not confirmation.
5. No missing hard-stop review. The system must not bypass login, MFA, CAPTCHA, robots/terms,
   legal/EEO/compliance/assessment questions, identity documents, payment, signatures or employer
   restrictions.

Generic approval, model output, match score, a synthetic qualification, previous review, disabled
Compose health or a reviewed draft cannot be promoted into live provider authority.

## Explicitly unconfigured or not claimed

The current evidence package does not include:

- live Gmail OAuth credential, broker server, refresh-token vault or controlled send smoke;
- live Greenhouse employer-owned Job Board API key, broker smoke or employer-authoritative
  confirmation path;
- live employer submission smoke for Gmail, Greenhouse, browser, Lever, Ashby, Workday, LinkedIn
  or custom employer ATS;
- browser automation against real application pages;
- credentials, cookies, identity-document upload, payment, signatures, legal attestations,
  EEO/compliance answers, assessments, CAPTCHA or MFA handling;
- automatic retry after ambiguous Gmail or Greenhouse provider state;
- any claim that G014 composes Greenhouse dispatch.

## Manual real-world handoff

Manual Real-World Application Handoff is tracked separately in
[manual application handoff acceptance](manual-application-handoff.md). It creates an
evidence-bound packet for a human to complete the final real-site submission. The handoff payload
uses `mode: human_final_submission`; any saved submission record is a user attestation with
`provider_receipt_verified: False`, not a provider receipt.

This capability is not real provider execution, browser automation, Gmail send, Greenhouse submit,
generic outbox worker execution or autopilot release evidence. It cannot qualify
`limited_autopilot` or `expanded_autopilot`.

## Safe operator handoff

Use the [durable GoalRun runbook](../runbooks/durable-goal-runs.md),
[reviewed Gmail send runbook](../runbooks/reviewed-gmail-send.md),
[reviewed Greenhouse submit runbook](../runbooks/reviewed-greenhouse-submit.md) and
[release qualification control-plane runbook](../runbooks/release-qualification-control-plane.md)
for the current boundaries. If an outcome is ambiguous or a required gate above is missing, leave
it in review/recovery; do not retry against a real provider or relax the release stage.
