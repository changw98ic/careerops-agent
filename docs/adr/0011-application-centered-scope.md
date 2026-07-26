# ADR 0011: Widen scope to an application-centered workspace with system-managed delivery

- Status: Accepted
- Date: 2026-07-26
- Supersedes: [ADR 0001](0001-mvp-scope.md) (MVP scope and release boundaries)
- Refines: [ADR 0003](0003-side-effect-authority.md) (side-effect authority covers initial application and reply sends), [ADR 0004](0004-google-oauth-scopes.md) (system-managed Gmail send on a dedicated recruiting account)
- Owners: repository owner; scope and capability-default decisions require independent review before any provider capability is release-qualified

## Context

ADR 0001 froze CareerOps as a discovery-first, manually-applied, Gmail-read-only MVP. That boundary was correct for the first executable slice: it let canonical jobs, crawl adapters, evidence-constrained matching, side-effect authority, retention and console auth land without smuggling in provider writes the safety model was not ready to authorize. The repository now has the reusable foundations ADR 0001 anticipated — side-effect kernel, outbox, provider receipts, reconciliation, immutable resume/evidence models, LangGraph review gate, Temporal workflows and the Vue SPA — but users still cannot walk one end-to-end application loop. Discovery, manual application, Gmail sync and drafting exist as separate surfaces with no shared `Application` spine.

The product question is no longer "what is safe to build first?" but "what is the controlled shape of one reliable application, once the side-effect kernel exists?" Two constraints shape the answer:

1. The user should not have to leave CareerOps to complete the final delivery step on a trusted recruiting email thread. Treating "preview in CareerOps, then go open Gmail and send it yourself" as the terminal state keeps the audit chain torn between two systems and makes receipt/state reconciliation impossible.
2. Removing the human from the final decision is still prohibited. Unattended mass apply, automatic recipient selection, automatic resume facts and default-on provider OAuth remain off the table, because every prior threat model in ADR 0001, ADR 0003 and ADR 0004 was written against the assumption that an external write only ever follows an explicit, in-product, hash-bound approval.

This ADR records the scope decision only. It does not assert that any new capability is shipped, qualified or default-on. The implementing change (`end-to-end-career-application-loop`) is in Phase 0 — contract freeze. Capability release remains gated by ADR 0003 authority, ADR 0004 BYO/dedicated-account rules, and the Release Qualification gate referenced by ADR 0001.

## Decision

CareerOps widens from a discovery-and-manual-apply surface to a **controlled application workspace** organized around the `Application` lifecycle, not around an agent graph. The user-visible spine is: career profile and resume → managed crawl plan → job inbox → application workspace (package, channel, confirmation) → inbound thread intelligence → reply and follow-up. Agents, Temporal workflows, LangGraph proposals and provider adapters are implementation details behind that spine.

The authorization model for delivery is fixed as follows.

**CareerOps-internal final confirmation is the authorization event.** When the user confirms an initial application email or an approved reply inside CareerOps, the API does not hand the user a "now go to Gmail and send" instruction. The confirmation is recorded as a durable `ApprovalRequest` / `ActionIntent` bound to the current payload hash, recipient, account, attachment hashes and policy version. The Side-effect Worker then resolves the opaque credential reference and calls the provider on the user's behalf. The user's confirmation inside CareerOps is the entire authorization; there is no second out-of-band email confirmation step.

**Initial application sends and reply sends share one delivery chain.** Both go through the same `EmailDeliveryPort`, the same Side-effect Kernel, the same outbox, reconciliation and audit path defined in ADR 0003. There is no "quick send" bypass. The two differ only in domain-level `intent_kind` / `SendCategory`:

- `initial_application`: always requires the user's final in-product confirmation;
- low-risk replies (delivery confirmation, recruiter contact ack, confirmed time ack): default not auto-sent, but eligible for system-managed send once the user confirms inside CareerOps;
- high-risk replies (salary, offer, visa, identity/bank, withdrawal, unknown): never auto-sent, always user-confirmed, never bypass policy.

**What remains excluded.** The scope widening does not authorize any of:

- unattended mass apply, batch submission, automatic third-party application-form filling, or any automation that bypasses login/CAPTCHA/Cloudflare terms;
- default-on Gmail OAuth, default-on external writes, or default-on auto-send. The capability flags `CAREEROPS_EXTERNAL_WRITES_ENABLED`, `CAREEROPS_AUTO_SEND_ENABLED` and `CAREEROPS_GOOGLE_OAUTH_ENABLED` stay default OFF; the capability resolver fails closed on unknown or unqualified capabilities;
- the model deciding recipients, application state, resume facts, OAuth scope, policy allow-lists or tool calls. Model output is untrusted proposal data under ADR 0003 and ADR 0006;
- low-risk auto-send becoming a no-confirmation default. Any future auto-send path requires a separate change that simultaneously satisfies the global kill switch, Release Qualification, account opt-in, policy allow-list, trusted context, D0 evidence and reconciliation;
- multi-user, team collaboration, public SaaS or cross-user data isolation;
- treating "external application form link opened" as "submitted". `SUBMITTED` is entered only on a confirmed provider receipt or the user's explicit manual completion of the external form, with the minimal evidence recorded.

**Capability release defaults.** Capability defaults follow the table in the implementing change's design: crawl plan / read discovery stays on under source policy; model matching and tailoring stay disabled or review-only; Gmail OAuth read, system-managed Gmail send and auto-send stay off by default; external form automation is not implemented. Any system-config, model, prompt, schema, migration, policy, dataset or qualification change forces the relevant provider capability back to unqualified until re-qualified.

**Relationship to prior ADRs.**

- **ADR 0001** is **superseded** as the active scope statement. Its release-order milestones and gate discipline remain useful history; its "no automated application submission, no system-managed send" boundary is the part being widened, strictly along the authorization model above. ADR 0001's exclusions on login/CAPTCHA bypass, take-home completion, guessed employee email, main-mailbox label mode, MCP mutation tools, salary negotiation, offer acceptance and identity/bank transmission still stand.
- **ADR 0003** is **refined**, not weakened. The same authority chain now authorizes initial application sends and reply sends in addition to the original Gmail reply path. Every constraint — immutable `PayloadVersion`, `PolicyDecision`, transactional outbox, isolated worker, reconciliation, append-only audit, role separation, fail-closed unknown policy, idempotency under ambiguity — applies unchanged. No new path is added that lets the API, UI or model call a provider directly.
- **ADR 0004** is **refined**. System-managed send uses the same BYO Google Cloud project, dedicated recruiting-account, staged-scope, envelope-encrypted-credential, opaque-reference model. `gmail.send` is requested only at its existing stage, on a dedicated account, behind the same revocation behavior. Possessing a scope still does not authorize an action.

## Consequences

- The product surface is described as "CareerOps confirmation causes system-managed send," not as "manual Gmail sending." Documentation, runbooks and UI copy must use that framing; this ADR is the authority for that terminology change.
- `Application` becomes the central lifecycle record. Job, candidate, package, channel, thread, event and reminder records hang off it; the projection of current state is a legal-transition view over an append-only event log, not an updatable column.
- Every external write — initial application email, reply, future calendar write — continues to require durable approval, payload hash, transactional outbox, isolated worker execution, provider receipt, reconciliation and audit. Repeated confirmation is idempotent and returns the existing intent/receipt/reconciliation state; ambiguous outcomes stop in review, never in blind retry.
- Default-off capability flags mean that with the shipped configuration, the system-managed send path is exercised only under a fake or offline provider harness. Live Gmail OAuth, live provider activation, external-write enablement and auto-send are not delivered by the implementing change; each requires a separate proposal, threat-model delta, qualification and approval.
- The Phase 0 deliverable for the implementing change is the contract shell: this ADR, a threat-model delta, canonical enums and transition tables, capability-resolver entries, API error codes, and contract tests. No new business behavior, migration, repo, route or frontend workspace is asserted as shipped by accepting this ADR.
- Accepted design is not release qualification. The Phase 0 contract shell is not a claim that any new provider capability is safe to enable; that determination remains with the Release Qualification gate and the independent review ADR 0001 reserves for security decisions.

## Verification

Phase 0 verification is scope-and-contract only:

- The default configuration keeps `CAREEROPS_EXTERNAL_WRITES_ENABLED`, `CAREEROPS_AUTO_SEND_ENABLED` and `CAREEROPS_GOOGLE_OAUTH_ENABLED` off; the capability resolver fails closed on unknown capabilities; preview, manual tracking and deterministic matching remain usable with all provider capabilities disabled.
- Contract tests prove the new enums, transition tables, error codes and resolver entries exist and reject illegal states, stale payloads, unresolved email links and reconciliation-required outcomes without touching a real provider.
- This ADR exists and is linked from the ADR index; ADR 0001 is marked superseded-by and ADR 0003 / ADR 0004 are marked refined-by in the index.

Later gates — fake-provider send slice, mail-intelligence slice, reply slice and the real D0 pilot — produce their own recorded pass/fail evidence under the implementing change and under ADR 0003 / ADR 0004 qualification. None of that evidence is asserted by accepting this scope ADR.
