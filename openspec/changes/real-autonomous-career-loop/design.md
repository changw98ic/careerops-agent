## Context

The career loop is implemented as a large, well-structured scaffold that does not actually run. Concretely: the model provider ships disabled by default (so every LLM feature — intake, matching, resume review — returns `unavailable`); the send and reply chains terminate at a `FakeSideEffectProvider` in the production runtime while the real `GmailSideEffectProvider` sits unused in `scripts/`; nothing self-drives (no Temporal Schedule is registered, the action queue is an empty stub, `interval_seconds` is a dead field); and the crawler can only parse structured public sources (Greenhouse/Lever/Ashby + JsonLd/Sitemap), with ego reduced to a single read-only, session-less HTML capture wrapped in extra safety machinery (sidecar + placeholder Ed25519 signatures).

On top of that, the founding invariant — every external message requires per-message human approval (`review_gate` interrupt + `SideEffectKernel` approval stage + permanently-denied categories) — is precisely what blocks the autonomous agent the product is meant to be. The `llm-agent-career-loop` change explicitly froze "no external writes, no auto-send, no OAuth, review-only model output." This change reverses that for a single-user, self-hosted tool.

Constraints: single user, self-hosted, loopback. Retain default-deny for unknown actions and append-only audit; remove only the per-message human-approval invariant.

## Goals / Non-Goals

**Goals:**
- Power on the real model provider and wire real Gmail (read + write) into the production runtime.
- Replace the per-message human-approval gate with a bounded autonomous-action policy (auto-reply / auto-send on reversible categories; hard-block irreversible commitments).
- Add a self-driving Temporal Schedule loop so mail-sync, outbox-drain, sweep, reminder-check, and crawl advance on their own.
- Add a real notification outlet (in-app + SSE) so reminders are pushed, not refreshed.
- Deliver a broad two-tier crawl path across structured and discovered job sources, with bounded browser navigation and schema-bound LLM extraction for sources Tier 1 cannot parse.

**Non-Goals:**
- Guaranteeing successful access to every named source, bypassing CAPTCHAs or source restrictions, and building a hand-written adapter for every site.
- Multi-language translation; full JD structuring/cleaning; LLM output quality regression suite; frontend i18n.
- Multi-user tenancy; public network exposure.

## Decisions

**D1 — Power on via configuration and runtime injection, not new code.**
The model gateway, the send/reply chains, mail-sync, and follow-up reminders are already implemented; they are simply off. The highest-leverage move is to configure a real provider, inject the real Gmail provider in `infrastructure/runtime.py` (replacing the fake), and flip the capability flags.
*Alternative considered:* rewrite the mail/agent stack — rejected as wasteful; the pieces exist.
*Trade-off:* this surfaces whatever latent bugs the fake provider hid; acceptable, and exactly what we want to find now.

**D2 — Replace the human review gate with a dual-model OA approval loop (A drafts, B reviews).**

The founding per-message human-approval invariant (`review_gate` `interrupt`) is removed. Sending is governed by an autonomous generate-review-revise loop using **the same MiMo model in two independent roles**, not two different vendors:

- **A (drafter)** composes the email on top of a template skeleton, grounded in trusted business state (job / resume / contact). A is a real generator that adjusts wording, not a pure slot-filler.
- **B (reviewer)** reviews A's draft along an **orthogonal axis** (wording risk, recipient correctness, over-promise) and **independently** — B sees only the draft, never A's self-assessment. No shared context on the first pass.
- **Loop**: B approves → send. B rejects → B's feedback is passed back to A → A revises → resubmit to B.
- **Termination**: at most **5 rounds**; if still unapproved, **escalate to human review** (no auto-send, no confidence-degraded send). Human review demotes from "every message" to a fallback for the rare case A/B cannot converge.

Independent calls (`AnthropicCompatClient` is stateless) give true independent judgement on the first pass; the B→A feedback is an explicit channel, not shared context. Same-model systematic blind spots are accepted because sends are low-stakes (templated; worst case = blacklisted).

*Alternative considered:* two different-vendor models (true dual-model) — rejected; not worth a second credential/cost for low-stakes templated sends. *Superseded earlier idea:* a reversible/irreversible category classifier — replaced by this A/B loop.

**D3 — Temporal Schedules for the trigger loop.**
Temporal is already the durable-execution backbone. Registering Schedules (not cron, not in-process loops) gives resumable, crash-safe auto-advancement and consumes the dead `interval_seconds` field. Crawl completion chains into matching/inbox projection so the loop is contiguous.
*Alternative considered:* external cron hitting API endpoints — rejected; not durable, not resumable, duplicates the Temporal investment.

**D4 — Server-Sent Events for the notification outlet.**
Reminders are unidirectional server→client push; SSE fits FastAPI with no new infrastructure and directly fixes the "manual refresh" complaint. Reminders are persisted (outbox-style) before push so a missed push is not lost.
*Alternative considered:* WebSocket — rejected as overkill for one-way push; polling — rejected (the complaint it would solve is the one the user already has).

**D5 — Two-tier crawl: broad public sweep, reasoned escalation, and permission-gated login.**

- **Tier 1 — broad sweep**: crawl structured and discovered sources at scale using ATS / JsonLd / Sitemap extraction and public probes. Every attempt records a reasoned outcome. An empty result is not an escalation signal: a source with no current openings, unrelated content, or a temporary network failure stays out of Tier 2.
- **Tier 2 — bounded browser agent**: confirmed job sources that require public browser rendering may enter Tier 2 without login. When Tier 1 confirms that job content requires authentication, the source pauses and the product requests source-specific permission from the user. Only a grant allows reuse of that source's authenticated session; CAPTCHA and generic 403 responses do not count as login evidence.
- **Execution limits**: Tier 2 has explicit per-source action, duration, duplicate-stop, scheduling, and cooldown limits plus global concurrency and daily browser-action budgets. Model latency is not a rate limiter. Source policy and user revocation stop access.
- **Extraction**: rendered job content passes through a schema-bound LLM adapter with one repair attempt and fail-closed validation, then through `ingest_posting` for deduplication, provenance, and inbox projection.

*Superseded earlier idea:* immediate escalation on every empty extraction result and automatic login-session reuse. *Alternative considered:* per-site hand-written parsers — rejected; does not scale.

**D6 — Keep default-deny for unknown actions and append-only audit.**
The reversal targets per-message human approval, not all safety. Unknown-action deny and the tamper-evident audit hash chain are cheap, useful even for an autonomous agent (every agent-initiated send is reconstructable), and retained.

## Risks / Trade-offs

- **Agent sends a wrong or embarrassing reply autonomously** → bounded policy blocks irreversible commitments; reversible-category replies are low-stakes; every agent-initiated send is recorded in the audit hash chain for after-the-fact review.
- **Real Gmail wiring sends real email / handles credentials** → single-user self-hosted only; credentials via env, not code; provider wired only when configured.
- **ego session reuse violates target-site terms or risks the user's account** → public crawling runs first; confirmed login requirements create a source-specific permission request; authenticated crawling starts only after approval and stops on revocation, CAPTCHA, account-risk challenge, or source-policy denial.
- **Temporal Schedules double-fire or drift** → existing idempotency (`run_identity`, send idempotency key, reconciliation key) makes schedules idempotent; Schedules are designed for at-least-once with idempotent workers.
- **LLM extraction hallucinates postings** → bounded schema validation + one repair retry + fail-closed; extracted postings are tagged with `llm-extraction` provenance so they are distinguishable.
- **Broad discovery does not guarantee universal coverage** → every source records an explicit outcome; unsupported, denied, or permission-pending sources remain visible instead of being reported as successfully crawled.

## Migration Plan

Phased; each phase is independently shippable and independently rollback-able.

- **Phase 1 — Power on (lowest risk, highest leverage):** configure real model provider; inject real Gmail provider in `runtime.py`; flip capability flags (`MODEL_PROVIDER`, `EXTERNAL_WRITES_ENABLED`, `AUTO_SEND_ENABLED`, Google OAuth). *Rollback:* flip flags back to disabled.
- **Phase 2 — Trigger loop:** register Temporal Schedules; chain crawl → matching. No change to existing flow logic, only auto-triggering. *Rollback:* remove the Schedules.
- **Phase 3 — Autonomy (highest impact, after power-on so there is something to act on):** swap the approval gate for the autonomous-action policy; enable auto-reply. *Rollback:* re-enable `review_gate`.
- **Phase 4 — Canonical crawler path:** reconcile the existing `CrawlExecutionService`, `RealCrawlActivitySink`, `CrawlAgent`, and `LLMJobExtractor` into one production path; remove the duplicate source model and repair the existing crawler tests before adding behavior. *Rollback:* retain the current structured Tier 1 path while the reconciled path remains disabled.
- **Phase 5 — Durable discovery and outcomes:** add persisted source-attempt outcomes, canonical source discovery/deduplication, bounded retry, and cooldown. *Rollback:* stop discovery intake and keep existing registered sources.
- **Phase 6 — Login permission:** add source-specific permission state, API/UI decisions, audit, and source-scoped session references. Authenticated crawling remains disabled until this phase passes. *Rollback:* revoke or pause all crawl permissions.
- **Phase 7 — Bounded Tier 2:** activate public and permission-gated authenticated navigation behind durable shared concurrency and daily-action budgets; reuse and harden the existing LLM extractor and canonical ingest path. *Rollback:* disable Tier 2 while retaining structured Tier 1 ingestion.
- **Phase 8 — Autonomous crawl activation:** enable previously paused Temporal crawl Schedules only after migrations, tests, permission APIs, and finite budgets pass a readiness gate; then chain successful crawl completion into matching and inbox projection. *Rollback:* pause the managed crawl Schedules.
- **Phase 9 — Notification outlet:** persist and push reminders, crawl-permission actions, and long-run progress through in-app state and SSE. *Rollback:* retain persisted notifications while disabling live SSE delivery.

## Open Questions

- Which initial seed catalogs and discovery channels provide enough breadth without introducing low-value unrelated domains?
- Does auto-reply need a per-thread opt-out, or is it purely policy-driven?
- Where are reminder notifications persisted before push (an outbox-style table) so a missed SSE push is recoverable?
- Does the autonomous-action policy reuse the existing reply risk-category classifier, or does it need a dedicated action-category classifier?
