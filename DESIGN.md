# Design

## Source of truth
- Status: Draft
- Last refreshed: 2026-07-19
- Primary product surfaces: single-user Chinese-first web console; local bootstrap and login; dashboard; future Jobs, Applications, Gmail draft approval, Calendar scheduling, Autopilot Grant, and review queue surfaces.
- Evidence reviewed:
  - `README.md`: safety-first single-user product status, setup, M0 evidence, D0/M1 gates, and known limits.
  - `.omx/plans/careerops-agent-mvp-plan.md`: Chinese-first console, FastAPI/Jinja2/HTMX decision, milestone gates, and side-effect boundaries.
  - `.omx/plans/careerops-agent-execution-brief.md`: M0 complete, D0 blocked, next UI lanes, and Release Qualification boundary.
  - `docs/adr/0001-mvp-scope.md`: MVP UI stack, included/excluded capabilities, and thin server-rendered console.
  - `docs/adr/0003-side-effect-authority.md`: policy, approval, outbox, worker, reconciliation, and audit chain.
  - `docs/adr/0006-model-privacy.md`: model-disabled default and review-only model authority.
  - `docs/adr/0007-console-auth.md`: single-user bootstrap, session, CSRF, origin, and approval endpoint security.
  - `docs/design/m1-preimplementation-readiness.md`: M1 UI/API boundaries and forbidden claims.
  - `src/careerops/web/templates/*.html`: current Chinese-first base, bootstrap, login, dashboard, and error templates.
  - `src/careerops/web/routes.py`: current FastAPI/Jinja2 route structure, CSRF, no-store responses, and protected dashboard.

## Brand
- Personality: calm operator console for a capable but bounded career agent; decisive, precise, and local-first.
- Trust signals: explicit permission state, visible evidence provenance, immutable payload/version references, expiry, audit status, and recovery affordances before every provider write.
- Avoid: marketing-style pages, chatbot-first flows, cheerful automation promises, vague AI confidence, hidden background work, dark-pattern opt-ins, and any copy that implies Auto-send is available before Release Qualification.

## Product goals
- Goals:
  - Let the user run a high-autonomy career workflow while reviewing only meaningful decisions, exceptions, and recovery cases.
  - Make every autonomous action explainable through evidence, policy decision, payload hash, expiry, and audit trail.
  - Keep the console fast enough for repeated daily use on a laptop without a separate frontend build system.
  - Preserve Chinese as the default operator language while showing English job/email source material accurately.
- Non-goals:
  - No unauthenticated remote administration, multi-user role management, public sharing, generic CRM dashboards, or consumer onboarding funnel.
  - No automatic application submission, Calendar write, Gmail send, or Auto-send without the milestone-specific approval and release gates.
  - No raw Gmail/thread/attachment display unless it has passed the relevant isolation and minimization boundary.
- Success signals:
  - The user can understand the system state from one console screen and handle the daily review queue in minutes.
  - Batch actions reduce repetitive approvals without hiding exceptions.
  - Rejected, expired, ambiguous, and reconciled actions remain auditable and recoverable.
  - Keyboard-only operation covers review, approve, reject, edit, batch select, and recovery paths.

## Personas and jobs
- Primary personas:
  - Single technical/professional job seeker operating a self-hosted CareerOps instance.
  - Same user acting as release owner for capability grants and provider permissions.
- User jobs:
  - Set up local access and bounded Autopilot permissions.
  - Review proposed job, application, email draft, and scheduling actions.
  - Batch-approve low-risk repeat decisions while escalating unclear, stale, or high-risk items.
  - Recover from provider ambiguity, token expiry, stale policy, duplicate intent, or failed reconciliation.
- Key contexts of use:
  - Daily laptop review session in Asia/Shanghai time.
  - Focused interruption when a recruiter email or schedule conflict needs action.
  - Local development or self-hosted operations where network, provider, and model capabilities may be disabled.

## Information architecture
- Primary navigation:
  - `首页` for system status and pending counts.
  - `机会` for companies, job postings, canonical jobs, source evidence, and merge/split review.
  - `申请` for application state, material versions, follow-up reminders, and manual submission records.
  - `邮箱` for dedicated Gmail sync, classified threads, internal drafts, and approval.
  - `日程` for FreeBusy proposals, buffers, conflicts, and human-triggered event creation.
  - `审阅` for the three-tab review queue: `待确认`, `异常`, `恢复`.
  - `授权` for Autopilot Grant setup, capability status, expiry, revocation, and release gates.
- Core routes/screens:
  - Existing: `/bootstrap`, `/login`, `/`, `/logout`.
  - Planned: `/review`, `/review?tab=pending`, `/review?tab=exceptions`, `/review?tab=recovery`, `/grants/autopilot`, `/jobs`, `/applications`, `/mail`, `/calendar`.
- Content hierarchy:
  - Start with capability state and blocked gates.
  - Then show action workload by risk and deadline.
  - Then provide dense item rows with evidence, proposed payload, policy result, expiry, and recovery state.
  - Details open in-page with HTMX fragments or full-page fallback, not modal-only flows.

## Design principles
- Principle 1: autonomy must be visible. Every automated step shows what authority it used, what it did not have, and what still needs the user.
- Principle 2: simple review beats chat. The main interaction is triage, compare, approve, reject, edit, recover, and audit; conversational explanation is optional support, not the primary UI.
- Principle 3: default-deny is part of the interface. Disabled capabilities, missing evidence, expired approval, and failed reconciliation should look like normal states, not system crashes.
- Tradeoffs:
  - Prefer dense tables and side-by-side evidence over large cards when users compare many actions.
  - Prefer server-rendered reliability and form fallbacks over complex client state.
  - Prefer fewer, clearer batch controls over granular automation toggles that create hidden authority.

## Visual language
- Color: neutral paper/background, ink-forward text, strong status accents. Suggested tokens: `--color-bg: #f6f3ec`, `--color-surface: #fffaf0`, `--color-ink: #1f2421`, `--color-muted: #69716b`, `--color-line: #d9d2c3`, `--color-accent: #0f766e`, `--color-warn: #b45309`, `--color-danger: #b42318`, `--color-ok: #287d3c`, `--color-agent: #315c85`.
- Typography: Chinese-first UI should use a high-legibility CJK stack such as `"Noto Serif SC"` for page titles and `"Noto Sans SC"` for controls/body, with English source snippets falling back to `"IBM Plex Mono"` only for hashes, IDs, URLs, and diffs. Avoid Arial, Inter, Roboto, and browser default system-only styling.
- Spacing/layout rhythm: compact operations-console rhythm; 8 px base spacing; dense row lists; sticky table/action headers; full-width bands instead of nested cards.
- Shape/radius/elevation: 4-6 px radius; thin borders; elevation only for popovers, menus, and focused side panels.
- Motion: restrained HTMX transitions for row insertion, status changes, and detail expansion; no decorative animation; honor `prefers-reduced-motion`.
- Imagery/iconography: no stock imagery. Use simple semantic icons only when a local icon path or vetted inline sprite exists; text labels must remain present for critical actions.

## Components
- Existing components to reuse:
  - Jinja2 `base.html` structure, form POST + CSRF pattern, no-store protected pages, Chinese labels, and semantic `section`/`dl` dashboard structure.
- New/changed components:
  - App shell with primary navigation and capability strip.
  - Review queue tab set with counters.
  - Action row for proposed side effects and review-only model outputs.
  - Evidence drawer with source snippet, hash, timestamp, policy version, payload version, and audit link.
  - Batch action toolbar with selected count, risk summary, expiry warning, and confirmation step.
  - Autopilot Grant setup form with scope, limits, exclusions, expiry, model/provider capability, and kill-switch state.
  - Recovery workbench for ambiguous provider attempts, duplicate receipts, stale approvals, and token failures.
- Variants and states:
  - Risk: low, medium, high, blocked.
  - Decision: proposed, denied, awaiting approval, approved, expired, invalidated, eligible, executing, succeeded, failed, reconciliation required.
  - Capability: disabled, review-only, setup required, granted, expiring soon, revoked, release-blocked.
  - Queue item: unread, selected, stale, edited, batch-ineligible, recovered.
- Token/component ownership:
  - Define tokens in a local CSS file loaded by `base.html`; avoid adding a frontend framework or design-system package before product need exists.
  - Jinja macros may own repeated queue rows/forms once duplication appears in at least two templates.

## Accessibility
- Target standard: WCAG 2.2 AA for the console, with keyboard-first operation for all review and recovery workflows.
- Keyboard/focus behavior:
  - Tabs use real links or proper tab semantics with full-page fallback.
  - Batch selection, approve, reject, edit, and recover controls are reachable in source order.
  - Sticky toolbars must not trap focus or cover focused rows.
- Contrast/readability:
  - Status cannot rely on color alone; include labels and symbols/text.
  - Chinese body text should not drop below 16 px in dense interactive regions.
- Screen-reader semantics:
  - Queue counts should be announced as text.
  - Error and success messages use `role="alert"` or status regions as appropriate.
  - Evidence diffs and payload summaries need headings and list/table semantics, not visual-only grouping.
- Reduced motion and sensory considerations:
  - Disable non-essential row animations under `prefers-reduced-motion`.
  - Avoid flashing timers; use static expiry text with optional refresh.

## Responsive behavior
- Supported breakpoints/devices:
  - Desktop-first operations layout from 1024 px and up.
  - Tablet/laptop narrow layout around 768 px.
  - Mobile fallback from 360 px for emergency review, not full high-volume operations.
- Layout adaptations:
  - Desktop: two-pane queue with sticky filters and evidence/details panel.
  - Tablet: stacked queue and detail panel with persistent action bar.
  - Mobile: single-column item cards, tab links, and one primary action group per item.
- Touch/hover differences:
  - No hover-only evidence or controls.
  - Touch targets at least 44 px high for primary actions and queue tabs.

## Interaction states
- Loading:
  - HTMX row/detail requests show inline skeleton rows or `正在加载...`; forms disable only the submitted controls.
- Empty:
  - Empty states explain the gate or capability state, for example `暂无待确认动作` or `Gmail 读取能力未授权`.
- Error:
  - Errors distinguish user-fixable issues, release-blocked capability, provider ambiguity, and internal failure.
- Success:
  - Successful approval/rejection/recovery returns the user to the same queue context and shows audit/reference ID.
- Disabled:
  - Disabled actions state the missing precondition: grant, evidence, fresh policy, release qualification, CSRF/session, provider token, or kill switch.
- Offline/slow network, if applicable:
  - Server-rendered full-page form submissions remain valid without HTMX.
  - Provider sync status and last reconciliation time must be visible before the user approves writes.

## Content voice
- Tone: terse, operational, Chinese-first, with enough English source fidelity for job titles, recruiter messages, URLs, and provider IDs.
- Terminology:
  - Autopilot Grant: `自动驾驶授权`.
  - Review queue: `审阅队列`.
  - Pending review: `待确认`.
  - Exception: `异常`.
  - Recovery: `恢复`.
  - Evidence: `证据`.
  - Policy decision: `策略判定`.
  - Payload version: `载荷版本`.
  - Reconciliation required: `需要对账`.
- Microcopy rules:
  - Say exactly why an action is blocked.
  - Never say the agent "sent" or "scheduled" until a provider receipt exists.
  - Use `建议`, `草稿`, `待确认`, and `已执行` carefully to separate proposals from side effects.
  - Dates and deadlines should show original timezone plus `Asia/Shanghai` when relevant.

## Implementation constraints
- Framework/styling system: FastAPI + Jinja2 + HTMX with local CSP-compatible JavaScript; no React/Vue/Svelte/Angular/Solid package detected.
- Design-token constraints: start with CSS variables and Jinja macros; no new npm pipeline or UI dependency without a separate architecture decision.
- Performance constraints:
  - Queue pages should be usable with hundreds of rows through server pagination, filters, and partial updates.
  - Do not render full raw email/JD bodies in list rows.
  - Avoid client-side state as the source of truth for approvals.
- Compatibility constraints:
  - Loopback-first single-user console.
  - CSRF on every mutation and no-store responses for sensitive pages.
  - Model/provider/write capability may be disabled and must not break the UI.
- Test/screenshot expectations:
  - Template tests for Chinese labels, CSRF fields, no-store behavior, tab counts, blocked states, and form fallbacks.
  - Accessibility checks for headings, labels, focus order, alert/status regions, and color contrast.
  - Responsive screenshots for dashboard, review queue, Autopilot Grant setup, and recovery detail at 1440, 1024, 768, and 390 px when UI implementation begins.

## Open questions
- [ ] Define the exact first Autopilot Grant schema fields, owner: product/architecture, impact: blocks implementation of the grant setup form.
- [ ] Decide whether the three review tabs are backed by one endpoint with query params or separate routes, owner: web/API, impact: affects templates and tests.
- [ ] Confirm whether `Noto Serif SC`, `Noto Sans SC`, and `IBM Plex Mono` can be locally vendored or must use fallback-only stacks, owner: frontend/security, impact: typography fidelity and CSP/offline behavior.
- [ ] Define batch approval eligibility rules by action kind, owner: policy, impact: prevents unsafe batch UX.
- [ ] Define mobile emergency-review acceptance criteria, owner: product, impact: determines how much mobile UI work is required for M4-M7.
