# Adversarial review ledger

This ledger distinguishes two axes:

- **Document-contract score:** whether the spec/plan is complete, internally consistent, and executable as an implementation contract. Target: at least 95/100 for every lens in the final post-fix review.
- **Runtime-readiness status:** whether the current repository already implements the contract. This design-only change does not claim that the new migration, Agent workflow, Ego sidecar, frontend flow, or CI gates already exist. Current status remains `not implemented / not pilot-ready` until tasks are applied and evidence is produced.

OpenSpec strict validation is structural evidence only and is not a score.

## Group 1 — baseline review and repair

| Reviewer lens | Pre-fix document score | Main finding | Repair applied |
| --- | ---: | --- | --- |
| Security/privacy | 66 | egress/SSRF/authorization/idempotency/fencing/audit/budget gaps | provider/browser/security contracts, deny rules, matrix, budgets, audit and cache requirements |
| Product/Ego/accessibility | 79 | no first-run contract, event refresh, accessible acceptance, API/SLO evidence | route journey, bootstrap/no-data, event-invalidated pull, accessibility and scenario register |
| Architecture/API/DB/Temporal | 70 | state mismatch, duplicate authority, no API/Temporal/migration/plan contract | orthogonal states, authority map, API payloads, Temporal, 0030 migration, implementation plan |

Group 1 result: all findings were incorporated before Group 2.

## Group 2 — independent re-review and repair

| Reviewer lens | Pre-fix document score | Main finding | Repair applied |
| --- | ---: | --- | --- |
| Security/privacy | 80 (spec), 75 (plan) | consent schema, subresource SSRF, real matrix, composite FK, audit transaction | `ProviderPolicy`/`ConsentEnvelope`/`EgressDecision`, forced interceptor/fresh task space, authorization annex, composite FK/dual-write/audit contract |
| Product/Ego/accessibility | 88 (spec), 84 (plan) | exact route/query/context/Ego assertions, outbound dispatch boundary, accessibility evidence | route table, target/action allowlist, preflight endpoint, 97→106 scenario register, route-specific DOM/focus artifacts |
| Architecture/API/DB/Temporal | 77 (spec), 70 (plan) | migration head conflict, worker/sidecar/provider factory/CI mismatch | 0030 after 0029, workflow protocol, sidecar protocol, model allowlist wiring task, release gates, design-only status |

Group 2 result: all findings were incorporated before Group 3.

## Group 3 — final independent document-contract review

The first final review intentionally inspected both the document and the current runtime. It correctly confirmed that the runtime is not pilot-ready, but mixed implementation readiness into the document score (66–82). The post-fix review must use the document-contract rubric above and report runtime gaps separately; it must not count “the task is not implemented yet” as a missing spec clause.

Repair round before the final rescore: moved the new requirements into the
`ADDED Requirements` sections while retaining one compatibility requirement in
each `MODIFIED` section; added the exact crawl-run detail/stage/retry/stop
contract; expanded authorization to every `/api/v1` route family including
audit/metrics/export; froze the outbound method/path/adapter deny matcher;
aligned the four operation enums and provider `verified_tls`/expiry/field-set
constraints; clarified that the ten provider calls/hour quota is a candidate
aggregate across all providers; and added the API, Temporal, sidecar, full
migration, retention/CI, register, and artifact annexes.

| Fresh independent reviewer | SPEC five weighted dimensions | PLAN five weighted dimensions | P0/P1 | Result |
| --- | --- | --- | --- | --- |
| Mendel | 98/97/97/98/96 → **97.2** | 97/96/98/97/96 → **96.8** | none | pass |
| Euclid | 25/19/20/14/20 → **98** | 24/19/20/15/20 → **98** | none | pass |
| Goodall | 97/96/96/96/97 → **96.5** | 96/95/95/96/97 → **95.8** | none | pass |

The five dimensions are independently weighted to 25/20/20/15/20; reviewers
used equivalent labels (security/privacy, product/UX/Ego/A11y,
architecture/API/DB/Temporal, migration/data/rollback, verification/ops).
All three fresh reviewers scored both documents at least 95 and found no P0/P1
document finding. The default verifier passes; `--require-artifacts` correctly
fails closed because runtime evidence has not been produced.

Final completion rule: document-contract review is complete. Runtime readiness
remains a separate release gate and cannot be represented as complete by this
document score.
