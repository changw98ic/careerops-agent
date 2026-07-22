# Bounded autopilot acceptance

- Status date: 2026-07-20
- Evidence scope: future implementation and release qualification for ADR 0011.
- Current status: an in-memory policy and append-only database control plane exist, including
  exact autopilot policy-decision binding, grant revocation/expiry guards, deterministic
  evidence-first drafts, a release-qualified `.test` synthetic reservation, generic outbox
  worker handoff, deterministic no-network synthetic provider, durable
  `side_effect_attempt` plus `provider_receipt` or reconciliation outcome, and audit
  reconstruction. The release gate permits only `.test` fixtures in `synthetic_sandbox`;
  real ATS/browser/Gmail/credential/provider execution, real provider credentials, production
  autopilot, and any real application remain disabled.

This document defines the evidence required before autonomous low-risk application submission
can move beyond shadow or review-required operation. It does not authorize production
autopilot by itself.

The current proof status and mandatory runtime blockers are recorded in the
[bounded-autopilot evidence ledger](bounded-autopilot-evidence.md).
G006 adds the release-qualification control-plane contract in
[release qualification control-plane runbook](../runbooks/release-qualification-control-plane.md).
That work is limited to synthetic shadow/review measurement, audit drill-down, rollback gates
and release evidence structure; it does not qualify Gmail, browser automation, Greenhouse, or
any other real provider channel.

The focused tests are sandbox evidence only. `tests/integration/test_migrations.py` covers the
migration-backed transaction path, and `tests/integration/test_submission_outbox_control_plane.py`
covers the synthetic outbox control plane. Both integration files require disposable PostgreSQL
evidence when run outside the in-memory unit path. No row below authorizes a real employer
submission.

## Acceptance matrix

| ID | Requirement | Required evidence | Acceptance status |
| --- | --- | --- | --- |
| BA.1 | Campaign-scoped authorization grants bind actor, campaign, target criteria/exclusions, approved campaign materials, channels, action kinds, caps, expiry, policy versions, release qualification version and required evidence. | Schema, policy, and reservation-trigger checks bind action, target, channel, material hash, policy outcome, and release version. | Synthetic sandbox implementation; production acceptance pending. |
| BA.2 | Grants are immutable after activation; narrowing, extension, renewal or correction creates a new grant version. | Mutation attempts fail; new-version flow preserves history and supersedes only future decisions. | Control-plane implementation; production acceptance pending. |
| BA.3 | Revoked, expired, exhausted or superseded grants cannot authorize new outbox events. | Unit and integration tests cover each terminal grant state and cap/rate-limit boundary. | Synthetic reservation guard verified with focused unit and disposable-PostgreSQL integration evidence. |
| BA.4 | Autonomous submission is allowed only when destination policy permits automated or agentic submission for the exact action. | Classifier fixtures and review cases cover permit, prohibit, unknown and low-confidence outcomes. Unknown and low-confidence fail closed. | Synthetic fixture classifier only; no real-site classification evidence. |
| BA.5 | Hard-stop categories route to human review before the synthetic provider worker can record a no-network attempt. | Tests cover prohibited automation language, legal attestation, sensitive personal data, credentials/payment/identity requests, interactive challenges, out-of-campaign targets, ambiguous duplicates, stale policy and active kill switches. | Synthetic hard-stop and durable global/campaign/provider kill-switch coverage only; real ATS/browser/Gmail/credential/provider execution remains disabled. |
| BA.6 | `allow_autopilot_submission` is distinct from ordinary `allow` and cannot be inferred from user preference, model confidence or prior approval. | Policy decision tests prove all non-autopilot allow paths fail to authorize autonomous submission. | Implemented for the synthetic lane; production acceptance pending. |
| BA.7 | Each authorized intent binds its exact prepared payload hash and approved campaign materials. | Editing prepared-draft bytes, source provenance, form fields, target, channel, grant version or policy version changes the derived submission-envelope hash and requires re-evaluation. | Draft/retry identity and derived synthetic submission-envelope hash are implemented; real-provider acceptance pending. |
| BA.8 | Synthetic provider execution is idempotent for campaign id, target id, action kind, payload hash, grant version and provider reconciliation key. | Concurrent duplicate dispatches converge to one generic outbox worker path and one durable no-network `provider_receipt` or reconciliation result. | Synthetic outbox/provider idempotency only; no real provider-side effect or real application exists. |
| BA.9 | Ambiguous synthetic provider states stop automatic retry until reconciliation proves a safe outcome. | Fault injection covers timeout, connection loss, duplicate-like response and missing synthetic receipt. | Pure synthetic reconciler stops ambiguity; real provider/browser fault-injection remains pending. |
| BA.10 | Audit reconstructs every autonomous decision. | Reviewer drill-down shows grant, policy version, classifier evidence, hard-stop checks, payload hash, generic outbox worker dispatch, `side_effect_attempt`, synthetic `provider_receipt` or reconciliation result and kill-switch states. | Synthetic reservation, worker, attempt/receipt or reconciliation, and audit chain exists; real-provider drill-down remains out of scope. |
| BA.11 | Rollout stages are enforced: shadow, review-required, limited-autopilot and expanded-autopilot. | Stage gates prevent provider execution before allowed stages; stage changes require release evidence and are reversible by kill switch and grant revocation. | Gate allows only `synthetic_sandbox`; shadow/review require review and all real-site stages block. |
| BA.12 | Shadow and review-required stages collect precision, override and false-positive/false-negative evidence without autonomous provider writes. | Metrics and audit records compare would-allow decisions against reviewer outcomes while proving no autonomous submissions occurred. G006 requires at least 50 deterministic synthetic shadow/review fixture cases before claiming measurement readiness. | G006 synthetic measurement control plane verified: 50 deterministic observations, zero provider calls, zero autonomous writes. This cannot qualify a real channel. |
| BA.13 | Limited-autopilot caps are conservative and enforce global, campaign and provider kill switches. | Tests prove cap exhaustion, per-day limits and durable global, campaign and provider kill-switch events stop new execution while preserving audit. | Synthetic cap/kill-switch code exists; limited-autopilot is intentionally blocked. |
| BA.14 | Release qualification prevents unqualified adapters or channels from autonomous submission. | Adapter registry tests deny autonomy for missing reconciliation, missing receipt handling, stale release version, same-actor evidence approval, disabled credentials, and excluded channels. | G006 append-only qualification lifecycle, complete evidence-set binding, evidence freeze, independent review, and read-only audit are verified for synthetic evidence only; real adapter qualification/receipt evidence remains pending. |
| BA.15 | Cap enforcement is transactional with intent/outbox eligibility. | Migration-backed and outbox-control-plane integration tests verify the transactional path on disposable PostgreSQL, including a hostile final-slot concurrency drill. | Synthetic transaction path and hostile final-slot concurrency drill verified on disposable PostgreSQL; real-provider cap evidence remains pending. |

## Release exit criteria

Before enabling `limited_autopilot`, release evidence must include:

- passing unit, integration and fault-injection tests for BA.1-BA.15;
- a reviewed sample of shadow/review-required decisions with measured classifier precision and
  documented reviewer overrides;
- reconciliation evidence for every enabled provider adapter;
- audit review evidence showing decisions can be reconstructed without reading mutable model
  output;
- rollback proof for global kill switch, provider kill switch, campaign kill switch and grant
  revocation;
- an explicit list of enabled channels and excluded channels.

For G006 specifically, the release evidence package must also include:

- an append-only release-qualification lifecycle from `draft` through review decision, with
  evidence creator and final reviewer/operator separated;
- a deterministic 50-case synthetic shadow/review evaluation manifest and metrics report;
- fault-injection results for before-call failure, after-call-before-commit ambiguity, timeout
  with possible success, duplicate receipt, missing receipt, kill switches, grant revocation,
  expiry and final-slot concurrency;
- an audit drill-down that reconstructs grant, evidence, policy, hard-stop, payload, rollout
  stage, release qualification, reviewer outcome, side-effect or zero-write proof, and
  kill-switch state;
- rollback proof for revoke/expire qualification, activate kill switch, and leave affected
  intents in review/recovery.

## Non-claims

- This acceptance contract does not permit autonomous submission where destination policy
  prohibits automation or where policy is unknown.
- This contract does not authorize legal attestations, sensitive personal-data disclosure,
  payments, credential entry, identity-document submission, assessments, live interactions or
  any action outside the active campaign grant.
- This contract does not authorize real ATS/browser/Gmail/credential/provider execution, real
  provider credentials, or a real application. The only executable boundary is the deterministic
  no-network `.test` synthetic provider path.
- This contract does not treat configurable crawlers as fixed channels. Crawler-discovered
  postings must still become exact, evidence-bound targets before any downstream preparation,
  review, or future provider action.
- This contract does not weaken ADR 0003; it adds a narrower policy outcome for bounded
  application submission.
