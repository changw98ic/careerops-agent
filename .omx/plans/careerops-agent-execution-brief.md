# CareerOps Agent durable execution brief

## Current checkpoint — 2026-07-18

- M-1 contract materialization and engineering verifier: complete and independently reviewed.
- M0 local engineering/Compose acceptance: complete; see `docs/acceptance/m0-evidence.md`.
- D0 real pilot: blocked by genuine external evidence, currently `0/286`; the full M-1 gate
  intentionally fails closed. No synthetic rows, placeholder actors, editable booleans, or
  repository-local hashes may substitute for real lawful/de-identified samples, authenticated
  independent review/adjudication, legal/privacy judgment, or holdout custody.
- Release boundary: M-1 scope is `d0_pilot_engineering_consistency` and
  `release_qualification_allowed` must remain strictly `false`; only the separate M7 release
  process can authorize release, and Auto-send remains hard-disabled.
- Next executable critical path: collect and review the frozen 10% D0 pilot, then open the M1
  safe-HTTP/adapters/data-model/UI lanes in parallel. Engineering may improve D0 intake tooling
  while evidence collection proceeds, but must not implement around the pilot gate.

Authoritative requirements are the user-provided Spec, [careerops-agent-mvp-plan.md](careerops-agent-mvp-plan.md), [careerops-agent-risk-closure.md](careerops-agent-risk-closure.md), and [labeling-plan.md](../../docs/evaluation/labeling-plan.md). Every story inherits their hard boundaries, milestone gates, metrics, D0 data obligations, verification requirements, and stop rules. A story is complete only when its documented exit Gate has current file/test/runtime evidence; later stories must not weaken earlier decisions.

1. M-1 — Materialize the execution contract: write ADR 0001–0008, the security threat model, metric contracts, dataset schemas/labeling guides/manifests, D0 ownership/pilot protocol, and a machine-checkable M-1 gate; do not add business behavior in this story.
2. M0 — Build the default-deny engineering and security kernel: Python/uv project, configuration and secret references, API health shell, canonical database schema/migrations, append-only audit constraints, policy engine, Temporal/outbox disabled skeleton, single-user auth shell, Compose topology, CI, and the complete M0 verification gate.
3. M1 — Deliver the read-only job discovery vertical: safe HTTP/crawl policy, official Careers and Greenhouse/Lever/Ashby/JSON-LD/Sitemap/static adapters, canonical job/posting version model, precision-first dedup with rollback, thin Jobs UI/API, frozen fixtures, D0 Discovery/Parser/Dedup evidence, and all M1 metrics/security tests.
4. M2 — Deliver Remote eligibility and evidence-constrained matching: reviewed evidence import, deterministic hard gates, disabled-by-default structured model port, compensation normalization, explainable scoring/UI, D0 Remote/Evidence datasets, injection tests, and all M2 gates.
5. M3 — Deliver contacts and Application operations without submission: evidence-backed public recruiting contacts, append-only Application lifecycle, existing resume/package registry, deterministic follow-up reminders, thin UI/API, D0 Contact evidence, and all M3 gates; no submit adapter exists.
6. M4 — Deliver dedicated-Gmail read/classify/internal-draft/approval: BYO personal-use OAuth, encrypted tokens, Pub/Sub pull plus history reconciliation, MIME/attachment isolation, untrusted-content classification, internal drafts and review UI, retention, D0 Email/Policy evidence, and all M4 gates; no send/compose scope or public webhook exists.
7. M5A — Prove the external side-effect kernel with fake providers: immutable intent/payload, policy, approval, transactional outbox, isolated worker, receipts/reconciliation/audit, crash and ambiguous-outcome matrix, D0 Reconciliation traces, and all M5A effectively-once gates before any real write provider.
8. M5B — Deliver human-triggered Calendar scheduling: FreeBusy, timezone/buffer/notice/limit rules, immutable proposals, dedicated CareerOps Interviews calendar, fresh preflight, no attendees, post-write reconciliation and conflict review, D0 Calendar scenarios, and all M5B gates; no unattended event creation.
9. M6 — Deliver Gmail Send behind the global kill switch: incremental send scope, immutable RFC reply payload, sandbox-proven reconciliation, approval UI, deny-by-default categories, fault matrix, and all M6 gates; production Auto-send remains off.
10. M7 — Qualify and release the single-user self-hosted v1: complete D0 independent review and sealed holdouts, security/E2E/chaos/restore/OAuth drills, observability/runbooks/retention, hardened Compose, immutable Release Qualification, opt-in low-risk Auto-send, final documentation, and every final DoD/metric with reproducible evidence.
