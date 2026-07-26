# ADR 0001: Freeze the single-user MVP scope

- Status: Accepted (scope widened by [ADR 0011](0011-application-centered-scope.md); the MVP exclusions below remain authoritative and are not reopened by 0011)
- Date: 2026-07-17
- Owners: repository owner; security decisions require independent review before M7

## Context

The source Spec mixes MVP behavior, second-stage ideas and examples that imply broader authority than the safety model permits. Greenfield implementation needs one executable boundary before dependencies or adapters are added.

## Decision

CareerOps v1 is a single-user, self-hosted application delivered in the M-1 → M0 → M1 → M2 → M3 → M4 → M5A → M5B → M6 → M7 order. A failed milestone Gate blocks dependent work.

The MVP includes:

- read-only discovery from official Careers pages, Greenhouse, Lever, Ashby, JSON-LD, Sitemap and static HTML;
- canonical jobs, source postings, versions, closure/reopen, precision-first dedup and reversible merge decisions;
- deterministic Remote hard gates, evidence-constrained matching and user-reviewed candidate evidence import;
- public recruiting contacts with source evidence, manual application confirmation and deterministic follow-up reminders;
- dedicated Gmail read/classify/internal-draft/approval followed later by narrowly scoped Gmail Send;
- FreeBusy and human-triggered event creation on a dedicated CareerOps Interviews calendar;
- policy, approval, outbox, reconciliation, audit, retention, recovery and a Chinese-first thin server-rendered console.

The following are not in v1:

- Playwright/Browser Worker, login bypass, CAPTCHA/Cloudflare evasion or proxy-pool crawling;
- automated application submission, unattended mass apply, take-home completion or guessed employee email;
- automated resume/cover-letter rewriting, automatic GitHub evidence sync or generative Prep Pack;
- main-mailbox label mode, Gmail Draft API, public Gmail webhook, external Calendar attendees or unattended Calendar writes;
- MCP, especially mutation tools; direct provider tools exposed to an agent;
- salary negotiation, offer acceptance/rejection, process withdrawal or identity/bank document transmission.

MVP UI is FastAPI + Jinja2 + HTMX with local CSP-compatible JavaScript. Object content uses a local content-addressed volume behind a storage port. `MODEL_PROVIDER=disabled` is the safe runtime default.

## Consequences

- Capability may remain `review_only` when its dataset or provider is unqualified; this is a valid safe degradation, not permission to skip a Gate.
- No provider write exists before M5A proves the side-effect kernel with a purpose-built fake.
- Gmail Send remains globally disabled until M7 Release Qualification and explicit account opt-in.
- Any scope expansion requires a new ADR, threat-model delta and tests before implementation.

## Verification

`scripts/verify_m1.py --contracts-only` verifies that all scope-linked ADRs and contracts exist. Code review must additionally confirm that M0 dependencies and ports do not smuggle deferred capabilities into the runtime.
