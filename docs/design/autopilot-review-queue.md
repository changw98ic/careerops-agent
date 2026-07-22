# Autopilot review queue design brief

- Status: Draft
- Date: 2026-07-19
- Scope: Autopilot Grant setup and the simple high-autonomy review queue for the Chinese-first CareerOps console.
- Source of truth: root `DESIGN.md`, ADR 0001, ADR 0003, ADR 0006, ADR 0007, and the MVP execution plan.
- Non-claim: this document does not authorize Auto-send, Gmail Send, Calendar writes, Release Qualification, or provider credentials.

## Product intent

CareerOps should feel like a bounded operations console, not a chatbot. The user grants a narrow Autopilot permission envelope, then spends most daily sessions in a three-tab review queue:

1. `待确认`: normal proposed actions that need a user decision.
2. `异常`: blocked, stale, denied, high-risk, or batch-ineligible items.
3. `恢复`: ambiguous side effects, failed reconciliation, token failures, duplicate receipts, and retry decisions.

The interface should make high autonomy feel safe because the remaining review load is small, sorted, evidenced, and reversible where the backend permits it.

## Autopilot Grant setup

`自动驾驶授权` is the user's explicit local permission envelope. It is not Release Qualification and cannot override policy, side-effect authority, provider scope, the global kill switch, or disabled model/provider settings.

Required grant fields:

| Field | UX control | Notes |
| --- | --- | --- |
| Capability scope | checkbox group | Separate discovery, matching, follow-up reminders, internal email drafts, scheduling proposals, and write-capable actions. Write-capable actions remain unavailable until their milestone gates exist. |
| Risk ceiling | radio/segmented control | `只整理`, `低风险建议`, `可批量低风险`, `需要逐项确认`. Do not offer labels that imply unattended provider writes before M7. |
| Entity scope | multi-select/list | Companies, applications, labels, calendars, or source groups included in the grant. |
| Exclusions | text/list builder | Companies, domains, recipients, action kinds, salary/offer topics, identity documents, and private data exclusions. |
| Batch eligibility | checkbox + explanation | Enables batch review only for policy-approved low-risk classes; the server decides final eligibility. |
| Expiry | date/time input | Default short expiry for early milestones; show Asia/Shanghai and original timezone if external dates are involved. |
| Evidence threshold | select | Minimum evidence completeness before an item can enter `待确认` instead of `异常`. |
| Recovery mode | radio | Manual only, suggest retry, or auto-create recovery tasks. No automatic provider retry without side-effect kernel approval. |
| Revocation | primary-danger action | Revoking the grant stops new autonomous proposals and invalidates queued items whose policy binds to the old grant. |

Setup screen hierarchy:

1. Capability strip: model, Gmail, Calendar, external writes, Release Qualification, global kill switch.
2. Grant editor: scope, limits, exclusions, expiry.
3. Preview: examples of what will be automated, what will still require review, and what is forbidden.
4. Confirmation: mutation form with CSRF, policy version, and visible audit outcome.

## Review queue architecture

The review queue should be one mental model with three tabs, not three unrelated tools.

Shared list row:

- action kind and human-readable Chinese summary;
- target entity: job, application, thread, draft, calendar proposal, or provider attempt;
- risk label and policy decision;
- age, expiry, and last sync/reconciliation timestamp;
- source evidence count and top evidence snippet;
- payload version/hash shortened for display with full value in details;
- primary action, secondary action, and details link.

Shared detail panel:

- `建议`: proposed action and expected provider/user-visible effect;
- `证据`: source snippets, URLs, timestamps, extractor/model versions, and hashes;
- `策略`: policy decision, grant reference, release gate, and deny/approval reason;
- `载荷`: immutable payload preview, diff from prior payload, attachments/material references;
- `历史`: prior decisions, invalidations, provider attempts, receipts, and audit events;
- `操作`: approve, reject, edit into new payload, defer, mark resolved, retry/reconcile if eligible.

## Tab 1: 待确认

Purpose: fast decision-making for normal review items.

Default sort:

1. expiring approvals or recruiter deadlines;
2. user-visible provider writes;
3. application-stage relevance;
4. confidence/evidence completeness;
5. newest proposals.

Expected item types:

- job merge/split proposals that require human confirmation;
- evidence-constrained match recommendations;
- application status proposals;
- internal Gmail reply drafts;
- follow-up reminder proposals;
- Calendar candidate time proposals before write capability exists;
- Gmail Send approvals only after M6 and still behind the global kill switch.

Primary controls:

- approve selected;
- reject selected;
- open details;
- edit payload, which creates a new payload version and invalidates prior approval;
- defer until date/time.

Batch UX:

- Batch selection is opt-in per row and defaults to none selected.
- The batch toolbar shows selected count, action kinds, highest risk, earliest expiry, and ineligible count.
- Mixed write-capable and non-write-capable actions require separate batches.
- Batch confirmation lists the exact payload hashes included.
- Server response returns per-item result: approved, rejected, skipped, expired, invalidated, moved to exception.

## Tab 2: 异常

Purpose: expose work that cannot safely proceed through normal review.

Exception classes:

- missing or stale evidence;
- denied policy decision;
- grant expired, revoked, or too narrow;
- model/provider capability disabled;
- release qualification false;
- payload changed after approval;
- source terms or crawl policy blocked;
- recipient/calendar/application target mismatch;
- batch-ineligible high-risk action;
- CSRF/session/auth mutation failure shown after redirect-safe handling.

UX requirements:

- Group by fix path, not backend subsystem: `补证据`, `更新授权`, `修改载荷`, `等待能力`, `人工处理`.
- Each item states one next action and one reason.
- If the fix is outside the UI, show a runbook/doc reference rather than a dead button.
- Do not let exception rows be batch-approved. Batch operations are limited to reject, defer, or mark manually handled when policy allows.

## Tab 3: 恢复

Purpose: resolve ambiguous side effects and interrupted workflows without pretending provider state is known.

Recovery classes:

- provider timeout after request submission;
- connection lost with unknown provider result;
- duplicate provider receipt;
- reconciliation key mismatch;
- token revoked or expired during workflow;
- outbox event leased but not completed;
- worker crash before/after provider call;
- Calendar conflict found after fresh preflight;
- Gmail thread changed after draft approval.

UX requirements:

- Always show `已知`, `未知`, and `下一步` separately.
- Use provider receipt/reconciliation status as the source of truth, not optimistic local state.
- Retry actions must name whether they will reconcile first, retry without provider call, or create a new payload/version.
- If proof is impossible, the terminal action is manual resolution with an audit note.
- Recovery rows cannot be hidden by success toasts; they leave a visible audit/reference state after completion.

## Accessibility contract

- The tab set must work as links when HTMX or JavaScript is unavailable.
- Every row action is a real button or link with an accessible name.
- Bulk selection uses checkboxes with row-specific labels, not click-only rows.
- Status labels include text, not color alone.
- The details panel has headings and landmarks so screen-reader users can jump between proposal, evidence, policy, payload, history, and actions.
- Focus returns to the changed row or next actionable row after an HTMX update.
- Destructive or provider-facing actions require a confirmation step that is keyboard reachable.

## HTMX/server-rendering contract

- Full-page route: `GET /review?tab=pending|exceptions|recovery`.
- Partial route option: same endpoint can return list/detail fragments when `HX-Request` is present.
- Mutation forms include CSRF and return either an updated row, a moved-row response, or a full redirect fallback.
- The server remains authoritative for selection eligibility, policy freshness, payload validity, and queue membership.
- No client-side cache may be used as approval evidence.

## Assumptions

- The first implementation will use FastAPI, Jinja2 templates, local CSS, and optional HTMX fragments.
- The product remains single-user and loopback-first for this design pass.
- Autopilot Grant is a first-class domain object or projection whose policy version can be referenced from review items.
- Batch approval is limited to classes explicitly permitted by policy and server-side eligibility checks.
- Chinese UI labels are the default; source material may remain English when fidelity matters.

## Open questions

- [ ] What is the durable database shape for Autopilot Grant, and does it live beside policy decisions or as a user settings aggregate?
- [ ] Which action kinds are eligible for batch approval in the first shipped review queue?
- [ ] Should `异常` include D0/M1 data-gate work items, or only runtime product actions?
- [ ] What audit note is required when a user manually resolves an unreconciled provider ambiguity?
- [ ] Should recovery items expire into a hard manual-only state after a configured age?
- [ ] Can preferred Chinese/monospace fonts be vendored locally under the current licensing/security policy?
