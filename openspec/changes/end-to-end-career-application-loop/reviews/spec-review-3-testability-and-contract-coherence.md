# Spec Review 3 — Testability and Contract Coherence

## Review method

This pass reviewed the specs as if they were acceptance-test contracts. It checked every requirement for at least one exact `#### Scenario`, checked the scenario heading format required by OpenSpec, traced identifiers across capabilities, and looked for contradictory state, approval, or failure semantics.

## Automated checks

- 7 capability spec files were found, matching the proposal.
- 39 requirements were found.
- 118 scenarios were found.
- Every scenario uses the required four-hash heading form.
- `openspec validate --changes --strict --json` passed with no issues.

## Findings

### 1. Cross-capability identifiers are coherent

The same conceptual links recur consistently: profile version, plan version, crawl run, canonical job, source posting, candidate evidence, resume version, application, package, email thread, payload hash, provider receipt, and application event.

### 2. Human decisions and system suggestions are distinguishable

Specs consistently separate model/classifier proposals from user-confirmed evidence, application state, package approval, and send confirmation. This is required for both UI clarity and audit correctness.

### 3. Send semantics are testable

The critical acceptance test is now unambiguous: a user confirms within CareerOps; CareerOps calls the controlled backend send path; the user does not manually send from Gmail. Preview/edit without confirmation produces no provider write. Payload mutation invalidates confirmation.

### 4. Failure and ambiguity states are represented

The specs cover denied crawl, rate limit, unavailable model, ambiguous thread link, invalid package, revoked account, failed/ambiguous delivery, rejected review, and stale synchronization. They do not collapse uncertainty into success.

### 5. Design-level follow-ups remain, but they are not spec defects

The design must centralize controlled vocabularies and state transitions, define idempotency-key scope, map API boundaries, define migration order, and specify feature-flag behavior. These are implementation decisions rather than missing user requirements.

## Review decision

PASS. The specs are sufficiently complete and testable to proceed to the technical design. No unresolved requirement contradiction remains after the `FAVORITED`/`PREPARING` correction and the explicit system-managed send clarification.
