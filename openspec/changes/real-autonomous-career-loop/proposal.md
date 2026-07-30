## Why

The career loop exists only as a safety-locked scaffold: the model provider ships disabled by default, the send and reply chains terminate at a fake provider, nothing self-drives (no Temporal schedule, empty action queue, dead `interval_seconds` field), and the crawler cannot touch any real source the user actually cares about (Boss直聘 / 智联 / social / company sites). The result is that nothing in the product runs for real today — every "built" LLM feature returns `unavailable` out of the box, and every external action is gated behind per-message human approval that the product's own design baked in as a founding invariant.

That invariant is now the blocker, not the asset: this product is meant to be an agent that acts on the single user's behalf, not a dashboard the user clicks through step by step. This change activates the loop for real.

## What Changes

- **BREAKING** — Remove the per-message human-approval gate: the `review_gate` LangGraph interrupt, the mandatory approval stage in `SideEffectKernel`, and the `PERMANENTLY_DENIED_REPLY_CATEGORIES` block list. Replace them with a bounded **autonomous-action policy** that auto-acts on reversible categories (acknowledgements, follow-ups, info requests, scheduling deferrals) and still hard-blocks only irreversible commitments (accepting interview times, accepting/declining offers).
- **BREAKING** — Power on the real model provider for the single user by default and wire the real Gmail provider (read **and** write) into the production runtime, replacing the `FakeSideEffectProvider`. Flip `GMAIL_READ` / send / external-write capabilities on. This reverses the `llm-agent-career-loop` constraint that forbade external writes, auto-send, OAuth, and model tools.
- Add **auto-reply**: when inbound mail arrives and matches a reversible category, the model drafts and the system sends the reply autonomously — no review queue, no per-message click.
- Add a **self-driving trigger loop**: Temporal Schedules that periodically run mail-sync, outbox-drain, expired-approval sweep, reminder-check, and crawl, so the system advances on its own instead of waiting for a manual click at every stage.
- Add **push reminders**: a real notification outlet (in-app + server-sent events) and the rules that actually fire follow-up / interview / stale-application reminders to the user.
- Add a **broad two-tier crawl path**: Tier 1 sweeps structured and discovered job sources at scale; Tier 2 renders sources that require browser navigation and requests source-specific user permission before reusing any authenticated login session. An LLM extraction adapter turns rendered job content into validated structured postings.

## Capabilities

### New Capabilities

- `real-provider-integration`: real model provider powered on by default for the single user, real Gmail (read + write) wired into the production runtime replacing the fake provider, and the read/send/external-write capabilities enabled.
- `autonomous-action-policy`: a bounded policy that lets the agent act without per-message human approval — auto-reply and auto-send on reversible categories, while hard-blocking irreversible commitments. Replaces the review-gate / kernel-approval invariant.
- `proactive-trigger-loop`: Temporal Schedules that self-drive mail-sync, outbox-drain, expired-approval sweep, reminder-check, and crawl on their own cadence.
- `push-reminders`: a notification outlet (in-app + SSE) and the rules that fire follow-up / interview / stale-application reminders to the user.
- `llm-job-extraction`: broad multi-source discovery and structured extraction in Tier 1, bounded browser navigation in Tier 2, source-specific permission before login-session reuse, and schema-bound LLM extraction for rendered job content.

### Modified Capabilities

None at the canonical level (`openspec/specs/` is empty — prior changes were never archived). This change deliberately supersedes the constraints frozen in `llm-agent-career-loop` and `end-to-end-career-application-loop` (no auto-send, no external writes, no OAuth, review-only model output).

## Impact

- **Backend — provider power-on**: `model_gateway/factory.py`, `infrastructure/runtime.py` (inject real Gmail provider + enable model), `integrations/gmail_side_effect_provider.py` moved into the production runtime, capability flags flipped (`CAREEROPS_MODEL_PROVIDER`, `CAREEROPS_EXTERNAL_WRITES_ENABLED`, `CAREEROPS_AUTO_SEND_ENABLED`, Google OAuth).
- **Backend — gate removal**: `orchestration/kernel_adapter.py` (review_gate), `application/side_effect_kernel.py` (approval stage), `application/system_managed_send.py`, `application/reply_draft_service.py`, `domain/reply_draft.py` (permanently-denied categories).
- **Backend — new**: Temporal Schedules registration in `infrastructure/temporal/worker.py`; a notification outlet module; an LLM extraction adapter under `adapters/`; ego session-reuse navigation in `infrastructure/temporal/ego_browser_executor.py`.
- **Frontend**: real-time progress (SSE), reminder/notification surface, removal of the review-queue-as-bottleneck UX, action queue populated by real state.
- **Safety posture**: this is an intentional, documented reversal of the project's founding safety invariants for a single-user, self-hosted tool. Default-deny on unknown actions and append-only audit are retained; the human-in-the-loop-per-message invariant is removed.
- **Deferred (follow-up changes, not this spec)**: per-site hand-written adapters, multi-language translation, full JD structuring/cleaning, a large labelled LLM output-quality regression corpus, and i18n.
