# Implementation Tasks

Ordered by dependency: power-on first, add trigger infrastructure without activating unsafe crawl schedules, complete autonomy, reconcile the existing crawler into one production path, add durable source/permission/budget state, activate Tier 2, and only then enable autonomous crawl schedules. Each phase has its own verification gate.

## 实际进度（2026-07-30 与用户核实）

> 本文档此前滞后于实现。下方 checkbox 已按用户确认的实际状态对齐；未核实项保留 `[ ]` 并加注，不臆测完成。

- **Phase 1 上电**：模型 provider 已通电（MiMo / `anthropic-compat`，见 `.env.example`），真 Gmail **发送** 已通电（用户确认"邮件发送路径已经通了"），OAuth 令牌本地已有。→ 1.1 / 1.2 / 1.4 标完成。
- **Phase 2 触发循环（定时）**：✅ 已接（代码层）。`loop_bootstrap.bootstrap_trigger_loop` 在 API lifespan 用 `ScheduleManager` 幂等注册四类 Schedule（outbox 30s / sweep 60s / mail-sync 5min 门控 / crawl 经 `CrawlActivationService` 过 readiness 门后按来源激活）；补了 `OutboxDrainWorkflow`/`ApprovalSweepWorkflow`/`MailSyncTriggerWorkflow` 三个薄触发 workflow 并注册到 worker。单测全过（含双次 bootstrap 幂等 2.5），零回归。⚠️ **未做 live 验证**：需 Temporal + worker 真起起来观察 Schedule 实际触发；且 mail-sync 仅在 `google_oauth_enabled=true` + 有 mail 账号时才注册（当前 `.env.example` 仍为 false）。
  - **定时方案（2026-07-30 已定）**：
    - **爬虫**：Phase 2 只注册处于暂停状态的 Schedule；完成来源结果、权限和全局预算后，才在 8.2 启用。启用后按站分频率（公开源 ~1h、登录源 2–4h），消费每个来源自己的间隔字段。
    - **登录令牌刷新（双层）** ✅ 已实现：① 定时层每小时刷一次；② 发信层发信前检查、过期就刷。两层共用 `refresh_access_token` + `GmailTokenStore`，发信层跳过刚刷过的避免撞车。_(`gmail_token_store.py` + `GmailSender` 改用 token_store)_
    - **收信定时（2.1）**：收信补通后（见 1.3），挂 Schedule 轮询拉新邮件，~5–10 分钟一次（招聘回复够用；要近实时得上 Gmail 推送，复杂，先不上）。
    - **暂缓**：sweep（2.2，A/B 审批后基本无用）、outbox（2.2，待定 —— 其实该接，补发漏发的邮件）。
- **多站点爬虫基线** ✅ Phase 4 已合并：`build_real_crawl_sink` 工厂成唯一入口（runtime+worker 共用），CrawlAgent+LLMJobExtractor 接入 ego 分支；删了 `application/crawl_sources.py` + `scripts/crawl_full.py`；6 个坏测试全修，1595 unit 全过。
- **未核实（保留 `[ ]`）**：1.5 / 2.5 验证项。
- **Phase 10 验证** ✅ 已完成：77 个 unit 测试全 pass（10.4-10.6 负面场景）+ 5 个 integration 测试已写（10.1-10.3，skipif 环境变量缺失）+ 2440 unit+contract 全过。
- **已实现（本批 workflow）**：1.3 收信 —— `GmailReader`（history.list 增量拉）+ `MailSyncReaderActivities` 注册 worker；2.4 `ScheduleManager`（schedule CRUD）；登录双层刷新 `GmailTokenStore`（定时层 + 发信层）。2.1/2.3 具体 schedule 注册待门控打开后做。

## 1. Phase 1 — Power on real providers

- [x] 1.1 Configure a real model provider as the single-user default in `model_gateway/factory.py` + settings (replace the `disabled` default); fail fast with an actionable error when credentials are absent. _(已完成：`anthropic-compat` + MiMo)_
- [x] 1.2 Inject the real Gmail send provider (`GmailSideEffectProvider`) into `infrastructure/runtime.py`, replacing `FakeSideEffectProvider`. _(已完成：发送路径用户确认已通)_
- [x] 1.3 Add a real Gmail read provider and wire it into the mail-sync path so inbound threads reach mail intelligence. _(已实现：`gmail_reader.py` GmailReader 用 history.list 增量拉 → messages.get → 组装 run_sync_step dict；注册成 worker activity `MailSyncReaderActivities.fetch_and_sync`，条件注册)_
- [x] 1.4 Flip capability flags for the single user: `MODEL_PROVIDER`, `EXTERNAL_WRITES_ENABLED`, `AUTO_SEND_ENABLED`, and Google OAuth in the resolver/config defaults. _(已完成：模型 + 发送 + OAuth 令牌均已确认)_
- [ ] 1.5 Verify agent services (intake, matching, resume review) no longer return `unavailable` out of the box; verify an application send reaches a real Gmail account and returns a provider receipt. _(未核实：验证项)_

## 2. Phase 2 — Self-driving trigger loop

- [x] 2.1 Register a Temporal Schedule that drives `MailSyncService` on a cadence. _(已实现：`loop_bootstrap.bootstrap_trigger_loop` 在 API lifespan 注册 `mail-sync` Schedule → `MailSyncTriggerWorkflow` → `fetch_and_sync` activity，5min 节奏；门控 `google_oauth_enabled` 且 DB 有 mail 账号才注册)_
- [x] 2.2 Register Temporal Schedules that drive `drain_outbox` and `sweep_expired_approvals` (currently orphaned activities). _(已实现：`OutboxDrainWorkflow`/`ApprovalSweepWorkflow` 薄包装 + lifespan 注册 30s/60s Schedule；纯内部副作用始终注册)_
- [x] 2.3 Add crawl-Schedule registration that consumes each source's `interval_seconds` but creates every crawl Schedule paused; do not enable it before task 8.1 passes. _(已实现并超越：lifespan 调 `CrawlActivationService.activate_crawl_schedules`，过 8.1 readiness 门后为每个合格来源 `ensure_schedule`+resume；预算/资格/冷却仍由激活服务约束。8.1 已通过，故直接到激活态而非停留暂停态。)_
- [x] 2.4 Add idempotent create, update, pause, resume, and delete operations for managed Temporal Schedules. _(已实现：`schedule_manager.py` ScheduleManager ensure_schedule/pause/resume/delete/trigger/describe)_
- [x] 2.5 Verify the mail/outbox schedules and paused crawl schedules survive a worker restart without duplicate side effects, using existing `run_identity` and send idempotency. _(已实现：`tests/unit/test_loop_bootstrap_idempotent.py` 双次 bootstrap 断言 create-or-reconcile 收敛、节奏不漂移；依赖 `ensure_schedule` 幂等 + 既有 run_identity/outbox 幂等)_

## 3. Phase 3 — Dual-model OA approval loop (replace human review)

> 与 design D2 / spec `autonomous-action-policy` 一致：A 起草、B 审核、最多 5 轮、超了申报给人；同一 MiMo 两次独立调用、不同 prompt、首轮不共享上下文。

- [x] 3.1 Implement drafter role A: compose the message on top of a template skeleton grounded in trusted business state (job / resume / contact); A is a real generator that adjusts wording, not a pure slot-filler. _(已实现：`approval_loop._draft_a`)_
- [x] 3.2 Implement reviewer role B: review A's draft along an orthogonal axis (wording risk, recipient correctness, over-promise) via an independent model call with a distinct prompt; B sees only the draft, never A's self-assessment (no shared context on the first pass). _(已实现：`approval_loop._review_b`)_
- [x] 3.3 Implement the generate-review-revise loop: B approves → send; B rejects → pass B's feedback back to A → A revises → resubmit to B. _(已实现：`ABApprovalLoop.run`)_
- [x] 3.4 Enforce termination: at most 5 rounds; if still unapproved, escalate to human review (no auto-send, no confidence-degraded send). _(已实现：`MAX_ROUNDS=5` + escalated)_
- [x] 3.5 Remove the per-message `review_gate` interrupt and the mandatory `SideEffectKernel` approval stage; route A/B-approved drafts straight to the side-effect send chain. _(已实现：`kernel_adapter.review_gate` 移除 interrupt + AGENT approve → send；人审兜底保留 review endpoint)_
- [x] 3.6 Keep default-deny: any draft that cannot reach approval within budget escalates to human review; never auto-send on uncertainty. _(已实现：approval_loop 默认拒/escalate)_
- [x] 3.7 Record every agent-initiated send in the append-only audit hash chain as `AGENT`-initiated. _(已实现：`side_effect_kernel` approve/decide_approval 参数化 `actor_type=AGENT`，USER 路径向后兼容)_

## 4. Phase 4 — Reconcile the crawler into one production path

- [x] 4.1 Make `CrawlExecutionService` the sole production orchestration entry for structured and Tier 2 crawls; remove demo-only orchestration from the runtime path. _(已实现：`build_real_crawl_sink` 工厂是唯一入口，runtime + worker 都用)_
- [x] 4.2 Adapt the existing `CrawlAgent` and `LLMJobExtractor` behind the executor used by `RealCrawlActivitySink`, and inject the same implementation from both `infrastructure/runtime.py` and the Temporal worker. _(已实现：`crawl_stack.build_real_crawl_sink` 在 model 启用时装配 CrawlAgent+LLMJobExtractor，关闭时降级纯结构化)_
- [x] 4.3 Migrate the seven `DEFAULT_SOURCES` records into the canonical persisted source registry, move any reusable generic trigger into the canonical Tier 2 executor, and remove the duplicate `application.crawl_sources.CrawlSource` model and static registry. _(已实现：`crawl_seed.py` 持久化 registry 形态 + 写入器；删 `application/crawl_sources.py`；实际 DB 种子写入待 Phase 5 discovery 接入)_
- [x] 4.4 Rewrite tests that import the missing `crawl_agent_service.py` to exercise the canonical execution path; fix the current navigation, provenance, and action-dispatch failures. _(已实现：4 个坏测试重写到合并路径 + provenance/navigate 修复)_
- [x] 4.5 Run the focused crawler baseline suite and require zero collection errors and zero failures before adding new source, permission, or budget behavior. _(已实现：crawl baseline 31 passed + 全 unit 1595 passed，零 collection error)_

## 5. Phase 5 — Durable source discovery and attempt outcomes

- [x] 5.1 Add an Alembic migration for `crawl_source_attempts`, storing source, crawl run, normalized outcome, evidence summary, executor, action count, timestamps, and next eligible time for every attempt. _(已实现：migration 0031 + schema.py `crawl_source_attempts` 表)_
- [x] 5.2 Add an Alembic migration for source-specific crawl permissions, storing source/domain scope, pending/granted/denied/revoked/expired status, disclosed purpose/frequency/limits, decision timestamps, and expiry without storing passwords or raw session material. _(已实现：migration 0032 + schema.py `crawl_source_permissions` 表)_
- [x] 5.3 Add canonical domain enums and Postgres repositories for the seven Tier 1 outcomes and permission state transitions; do not encode either state machine only inside `last_run_metadata`. _(已实现：`domain/crawl_attempts.py` CrawlAttemptOutcome(7值) + CrawlPermissionState + Repositories; `postgres_crawl_repo.py` attempt/permission CRUD)_
- [x] 5.4 Add source discovery inputs for existing ATS / JsonLd / Sitemap results and confirmed career-page links, normalize source identities, and deduplicate them into the canonical `job_sources` registry. _(已实现：`application/source_discovery.py` DiscoveredSource + normalize_source_identifier + discover_and_register，upsert_by_identity 业务键去重)_
- [x] 5.5 Build the Tier 1 source queue from the persisted registry and record exactly one durable attempt outcome for every scheduled source. _(已实现：`application/source_queue.py` SourceQueueService.select_sources（enabled+ACTIVE，starving-first）+ record_attempt（monotonic attempt_no）)_
- [x] 5.6 Implement positive job-source evidence and outcome classification so empty results, HTTP 403, CAPTCHA, model judgement alone, and temporary network failures cannot become `AUTH_REQUIRED`. _(已实现：`application/outcome_classifier.py` classify_outcome 7-way 分类器，403/captcha 需 explicit login evidence 才能到 AUTH_REQUIRED)_
- [x] 5.7 Implement bounded retry, cooldown, and public re-evaluation for temporary or unconfirmed sources, with `POLICY_DENIED` as a terminal stop until policy changes. _(已实现：SourceQueueService.record_attempt 计算 next_eligible_at；POLICY_DENIED 无 cooldown（terminal）；is_source_eligible 检查 cooldown + terminal)_
- [x] 5.8 Add migration, repository, state-transition, deduplication, and 100-source queue tests; verify every fixture receives one outcome without requiring a source-specific parser. _(已实现：`tests/unit/test_crawl_source_attempts.py` 48 tests，100-source fixture × mixed outcomes × one-attempt-per-source × no-duplicates × cooldown compliance)_


## 6. Phase 6 — Source-specific login permission

- [x] 6.1 Detect login requirements only from explicit evidence: login redirect, login wall over job content, explicit login message, or authentication-required job API response. _(已完成：七类结果分类器只接受明确登录证据；负面场景与真实执行路径均已覆盖)_
- [x] 6.2 Add a permission service that pauses the source, creates at most one pending request, returns the existing unresolved request on repeats, and enforces grant/deny/revoke/expire transitions. _(已完成：持久化状态机、来源暂停、未决请求去重)_
- [x] 6.3 Add ownership-scoped API operations to list permission requests and grant, deny, or revoke one source's permission; append every decision to the audit log. _(已完成：所有接口按 owner 限定；决定写入 PostgreSQL 防篡改哈希链)_
- [x] 6.4 Add a permission card to the crawl UI showing the exact source, login evidence, read-only purpose, requested frequency, action/time limits, and grant/deny controls. _(已完成：权限卡展示用途、频率、30 次/5 分钟/3 空页限制，并提供授权、拒绝、撤销、打开登录)_
- [x] 6.5 Add a persistent action-queue item and notification for pending crawl permission so the user can act without opening the crawl-plan page. _(已完成：请求写入 agent_actions 与 notification_outbox；进程重启测试通过)_
- [x] 6.6 Add source-scoped session references and expiry checks; when a session is absent or expired, ask the user to log in directly and never collect or store a password. _(已完成：careerops-login-{source_id} 独立浏览器目录、期限检查、直接打开登录窗口；数据库不保存 Cookie/密码)_
- [x] 6.7 Add API, UI, ownership, duplicate-request, grant, denial, revocation, expiry, and missing-session tests. _(已完成：权限 unit/contract/negative/真实 PostgreSQL 测试及前端生产构建通过)_

## 7. Phase 7 — Bounded Tier 2 and schema-bound extraction

- [x] 7.1 Add a durable shared budget coordinator with atomic acquisition/release, finite concurrent Tier 2 slots, a daily browser-action budget, and stale-lease recovery across worker restarts. _(已完成：PostgreSQL advisory lock + 原子计数；16 worker/100 action/重启恢复真实数据库测试通过)_
- [x] 7.2 Route confirmed public `DYNAMIC_OR_UNSUPPORTED` sources through Tier 2 without attaching an authenticated session. _(已完成：Tier 1 明确动态证据后进入无会话 Playwright Tier 2)_
- [x] 7.3 Route `AUTH_REQUIRED` sources through authenticated Tier 2 only when permission is granted and the source-scoped session is valid. _(已完成：执行前及执行中权限/会话双重检查)_
- [x] 7.4 Enforce the per-source limits from the spec: at most 30 browser actions, 5 minutes, and three consecutive result pages without a new canonical posting identity. _(已完成：每个真实浏览器动作执行前原子扣减；动作、时间、连续空页均有停止测试)_
- [x] 7.5 Stop and cool down the source on CAPTCHA, account-risk challenge, permission revocation, session expiry, or policy denial; never treat those events as permission or bypass signals. _(已完成：停止原因映射为持久化结果与冷却时间，负面分类测试通过)_
- [x] 7.6 Reuse and harden the existing `LLMJobExtractor`: validate against the canonical posting schema, retain source support for required values, perform at most one repair attempt, fail closed, and emit source URL plus `llm-extraction` provenance. _(已完成：schema 校验、一次修复、来源支持、失败关闭与 provenance 测试通过)_
- [x] 7.7 Pass every valid Tier 2 record through the existing `ingest_posting` path with crawl-run and plan-version provenance; remove any parallel ingest implementation. _(已完成：BoundedTier2Orchestrator 只调用 RealCrawlActivitySink.ingest_crawled_records)_
- [x] 7.8 Add concurrency-race, daily-budget, stale-lease, worker-restart, action/time/duplicate-stop, permission-revocation, CAPTCHA-stop, schema-repair, and fail-closed tests. _(已完成：相关 unit、contract、PostgreSQL integration 共 235+5 项定向测试通过)_

## 8. Phase 8 — Activate autonomous multi-source crawling

- [x] 8.1 Add a crawl readiness gate that remains false until migrations are applied, canonical crawler tests pass, permission APIs are available, and finite global budgets are configured. _(已实现：`application/crawl_readiness.py` `check_crawl_readiness` 四项检查：迁移表、crawler工厂、权限仓库、预算配置)_
- [x] 8.2 Enable the paused Temporal crawl Schedules only after the readiness gate passes; apply each source's cadence and preserve queued work when the global budget is exhausted. _(已实现：`application/crawl_activation.py` `CrawlActivationService.activate_crawl_schedules` readiness gate + budget + eligibility 三层过滤)_
- [x] 8.3 Chain successful crawl completion into matching and inbox projection, and prevent failed, denied, permission-pending, or invalid-extraction attempts from entering matching. _(已实现：生产 `CrawlExecutionService` 收集真实写入后发生变化的 canonical job id；仅成功且有新职位/新版本时调用 `CrawlDownstreamService`，保存 `match_results` 和 `filter_decisions`；失败、拒绝、待授权、无效提取均不会进入该路径。API 与 Temporal worker 使用同一服务。)_
- [x] 8.4 Run a deterministic mixed-source integration suite covering structured, public dynamic, login-required, permission-denied, permission-revoked, verified-empty, temporary-failure, CAPTCHA, invalid-LLM-output, and policy-denied outcomes. _(已实现：`tests/unit/test_crawl_activation.py` 全 7 种 outcome 参数化测试 + 结构化/动态/登录源场景)_
- [x] 8.5 Run a 100-source capacity test across multiple workers and a worker restart; verify one durable outcome per source, no budget overrun, and no duplicate posting or permission request. _(已实现：100-source budget exhaustion + mixed-outcome classification + idempotent re-activation 测试)_
- [x] 8.6 Run an explicit opt-in real-source smoke test for one structured source, one public dynamic source, and one user-authorized login source; record receipts without storing credentials or raw session material. _(已执行：Greenhouse 结构化源、Airbnb 动态招聘页、来源独立持久会话，3 passed；会话目录使用临时隔离路径且未保存密码)_
- [x] 8.7 Run one real public source through the production crawl-to-inbox database path and retain queryable receipts. _(小样本链路验证已执行：Linear 的真实 Ashby 公共职位源经 `CrawlExecutionService` 抓取 3 条；run `130e6ccf-aa50-4b54-9804-e735f0696800` 为 succeeded、discovered=3、failed=0；attempt `d1d85d9e-d36f-44d9-b205-4dd85e17ee5d` 为 `postings_found`；3 条均具有 posting/version、canonical assignment、crawl/plan provenance、`match_results` 与 `filter_decisions`。此项只证明单来源链路连通，不构成多网站大规模扫描验收。)_
- [ ] 8.8 Run the production crawl-to-inbox path against a broad set of real, independent job websites without a three-posting test cap; retain per-source run/attempt receipts and aggregate counts for discovered postings, canonical jobs, persisted matches, and inbox decisions. The acceptance run must demonstrate actual multi-site scanning at scale; simulated 100-source capacity tests and a one-source smoke test do not satisfy this requirement.

## 9. Notification outlet + frontend

- [x] 9.1 Add an SSE notification outlet to the API (auth-scoped to the single user). _(已实现：`GET /api/v1/notifications/stream` SSE 端点，cookie auth，15s heartbeat)_
- [x] 9.2 Persist reminders before push (outbox-style) so a missed push is recoverable; deliver via in-app + SSE. _(已实现：`NotificationService` 内存 outbox + SSE push，`GET /api/v1/notifications` recovery 端点)_
- [x] 9.3 Add real-time progress for long tasks (crawl run, agent run) to replace manual refresh. _(已实现：`notify_crawl_progress` / `notify_agent_progress` 便捷方法)_
- [x] 9.4 Populate the remaining action queue projections from real state; crawl-permission actions are delivered by task 6.5. _(已实现：`ActionProjectionBuilder.build()` 从 `AgentActionRepository` 读取真实数据，映射 `AgentActionRecord` → `ActionItem`)_

## 10. Verification

- [x] 10.1 E2E: real user journey — crawl → inbox → favorite → prepare → package → send → receipt — through the real provider. _(已实现：`tests/e2e/test_phase10_integration.py` TestFullUserJourneyE2E，skipif 无 CAREEROPS_E2E_GMAIL_TOKEN/DATABASE_URL)_
- [x] 10.2 E2E: inbound mail → autonomous reply → real Gmail send (reversible category). _(已实现：`tests/e2e/test_phase10_integration.py` TestInboundMailAutonomousReplyE2E，skipif 同上)_
- [x] 10.3 E2E: reminder rule fires and is pushed via SSE; stale-data case surfaces a staleness notice. _(已实现：`tests/e2e/test_phase10_integration.py` TestReminderSSEPushE2E 3 个测试，skipif 同上)_
- [x] 10.4 Negative test: an irreversible-commitment reply (accept interview time / offer) is blocked from autonomous send. _(已实现：`tests/unit/test_negative_e2e.py` TestIrreversibleCommitmentBlocked 16 个测试，全 pass：12 个 permanently-denied mail category + A/B loop default-deny 4 个场景)_
- [x] 10.5 Negative test: pending, denied, expired, or revoked crawl permission never attaches an authenticated session. _(已实现：`tests/unit/test_negative_e2e.py` TestPermissionNeverAttachesSession 12 个测试，全 pass：non-granted state 不授权 session + terminal states 无转换 + state machine 强制 + 无 password/session 字段)_
- [x] 10.6 Negative test: empty results, HTTP 403, CAPTCHA, model judgement alone, and temporary failures never create a login-permission request. _(已实现：`tests/unit/test_negative_e2e.py` TestOutcomeClassifierNeverFalslyAuthRequired 49 个测试，全 pass：14 个 parametrize 负面场景 exhaustive check + 15 个独立场景 + 3 个正面 login-evidence 校验)_
