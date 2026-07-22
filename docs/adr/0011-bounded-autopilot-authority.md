# ADR 0011: Bound autopilot application-submission authority to campaign-scoped authorization

- Status: Accepted
- Date: 2026-07-19
- Supersedes: ADR 0001 only for the v1 exclusion of automated application submission when this ADR's campaign-scoped controls are satisfied. ADR 0001 remains authoritative for all other MVP scope exclusions and milestone gates.
- Supersedes: ADR 0003 only for the per-intent approval requirement for low-risk autonomous application submission authorized by an active campaign grant. ADR 0003 remains authoritative for the general external side-effect chain, provider-write isolation, idempotency, reconciliation and audit.

## Context

ADR 0001 freezes the v1 MVP scope and explicitly excludes automated application submission,
unattended mass apply, take-home completion and guessed employee email. That exclusion was
the correct default before a narrower authority model existed. This ADR creates the narrow
scope expansion required for bounded autopilot application submission and leaves the rest of
ADR 0001 intact.

ADR 0003 requires external writes to pass through immutable payloads, policy decisions,
approval when required, a transactional outbox, side-effect workers, reconciliation,
receipts and append-only audit. That chain prevents model output or retries from directly
performing provider writes, but it does not define the product boundary for a bounded
autopilot that may submit low-risk applications without one approval per target.

The product direction is to allow campaign-level autonomy only after a user grants explicit,
limited authority for a campaign. This is not global auto-send. It is a narrower delegated
authority model for low-risk application submissions where site policy, campaign policy and
system evidence all permit autonomous execution.

## Decision

### Authorization scope

Autopilot authority is represented as a campaign-scoped authorization grant. A grant binds:

- user actor and account identity;
- campaign id, target role criteria and excluded targets;
- permitted application channels and action kinds;
- policy version, release qualification version and grant version;
- maximum submission count, optional per-day rate limit and expiry;
- required evidence shape, including job snapshot, approved campaign-material hashes,
  qualification result and submission policy classification; each intent separately records
  its immutable payload hash at intent creation;
- revocation state and reason.

The grant is immutable after activation. Narrowing, extending, renewing or correcting any
bound field creates a new grant version. A revoked, expired, exhausted or superseded grant
cannot authorize a new outbox event.

### Permitted autonomous submissions

Autonomous application submission is allowed only when all of the following are true:

- the active campaign grant permits the campaign criteria, approved materials, channel and
  action kind, while the exact per-intent policy decision permits the specific target and
  payload version;
- the submission policy classifier records that the destination permits automated or agentic
  application submission for the intended action;
- the role and employer pass campaign constraints, exclusion rules and duplicate detection;
- the payload was generated from approved campaign materials, has a current immutable
  per-intent payload hash and meets any target-specific payload constraints;
- no hard-stop exception category is present;
- the provider adapter is release-qualified for idempotent execution and reconciliation;
- global, campaign and provider kill switches are inactive;
- credentials are current and authorized for the target channel;
- the policy engine returns `allow_autopilot_submission` for the exact grant and intent.

Every other external write continues to use ADR 0003 outcomes: `deny`, `require_approval` or
the existing narrowly defined `allow`. `allow_autopilot_submission` is a separate outcome and
must not be inferred from ordinary approval, convenience settings or model confidence.

This ADR does not authorize unattended mass apply. A campaign grant must be bounded by target
criteria, exclusions, caps, expiry and evidence. It also does not authorize take-home
completion, guessed employee email, login bypass, CAPTCHA/Cloudflare evasion, proxy-pool
crawling, identity/bank document transmission, salary negotiation, offer decisions, process
withdrawal, external Calendar attendees or unattended Calendar writes.

### Hard stops and exceptions

The system must stop before provider execution and route to human review when any strict
exception category applies:

- destination terms, robots policy, application instructions or detected workflow text
  prohibit automation, bots, agents, scraping or third-party submission;
- the submission requires legal attestation, EEO/OFCCP-sensitive answers, disability,
  veteran status, background-check consent, immigration/work-authorization nuance,
  sponsorship nuance, relocation commitment, compensation commitment or binding availability
  promises not already approved for the campaign;
- the form requests credentials, payment, identity documents, references, government ids,
  security-clearance details or sensitive personal data outside the campaign grant;
- the destination requires an interactive challenge, CAPTCHA, live chat, phone call, video,
  personality/skills assessment or non-standard multi-step workflow;
- the posting, employer, location, title, seniority, employment type, compensation or
  application channel falls outside campaign constraints;
- duplicate detection, reconciliation or provider state is ambiguous;
- classifier confidence is below the release threshold, required evidence is missing, or any
  policy, grant, credential, adapter or release version is stale;
- any reviewer, user or system kill switch is active.

Hard-stop classification is fail-closed. Unknown action kinds, unknown site policy, missing
policy evidence and unavailable reconciliation all require review.

### Audit, idempotency and reconciliation

Autopilot uses the ADR 0003 side-effect chain without bypass. Each autonomous submission has
an immutable `ActionIntent`, immutable payload version, policy decision, grant version,
transactional outbox event, provider attempt, provider receipt when available and append-only
audit event.

The idempotency key binds campaign id, target id, action kind, payload hash, grant version
and provider reconciliation key. Reusing the same key must converge to one confirmed provider
effect. Changing any bound value creates a new intent and requires policy re-evaluation.

Grant-cap consumption and the corresponding intent reservation must occur in the same
transaction that creates the eligible outbox event. A policy evaluation alone never reserves a
cap or authorizes a provider call; concurrent evaluations must converge on the configured cap
before dispatch.

Timeout, connection loss, unexpected redirect, partial form state, ambiguous confirmation,
duplicate-like provider response and missing receipt are ambiguous states. Automatic retry
stops until reconciliation proves the previous effect did not happen or safely converges on
the same provider-side submission.

The audit trail must support a reviewer reconstructing why autonomy was allowed: grant,
policy version, classifier evidence, hard-stop checks, payload hash, provider attempt,
receipt or reconciliation result and every kill-switch state used in the decision.

### Rollout boundary

Autopilot rollout is staged:

1. `shadow`: classify and prepare intents, but do not submit.
2. `review_required`: require human approval while recording whether autopilot would have
   allowed submission.
3. `limited_autopilot`: allow only release-qualified low-risk channels for explicitly
   granted campaigns with conservative caps.
4. `expanded_autopilot`: raise caps or channels only after acceptance evidence demonstrates
   policy precision, reconciliation quality, reviewer override handling and clean audit.

No stage may skip the hard-stop categories. Stage changes require release evidence and must
be reversible through kill switches and grant revocation.

## Consequences

- Campaign authority becomes a first-class product and policy object, not a UI preference.
- A user can delegate a bounded class of submissions without approving every target, but only
  inside a narrow, auditable and revocable grant.
- Site policy classification and reconciliation become release blockers for autonomous
  submission adapters.
- Human review remains the default for unknown, sensitive, ambiguous or prohibited cases.
- ADR 0001's automated-application-submission exclusion is overridden only for submissions
  that satisfy this ADR. All other ADR 0001 exclusions and milestone gates remain in force.
- Existing ADR 0003 controls remain required; this ADR narrows only when the policy engine
  may choose autonomous application submission instead of per-intent human approval.

## Verification

Acceptance evidence is defined in
[bounded-autopilot](../acceptance/bounded-autopilot.md). Release qualification must prove
grant immutability, revocation, expiry, cap enforcement, hard-stop fail-closed behavior,
idempotent provider execution, reconciliation after ambiguous outcomes, audit
reconstructability and staged rollout gates before enabling `limited_autopilot`.

Current foundation evidence may prove policy and database authorization guards, but it does not
prove provider-write outbox eligibility, cap reservation, provider execution, receipt capture,
or reconciliation.
