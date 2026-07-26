## Context

CareerOps 当前是一个单用户、自托管、默认拒绝的求职运营系统。代码已经包含以下可复用基础：

- `canonical_jobs` / `job_postings` / `job_posting_versions` 的职位规范化、来源和版本模型；
- `CrawlPolicyInput`、SSRF 防护、限流和适配器层；
- `Candidate`、`EvidenceItem`、`ResumeVersion`、`ApplicationPackage`、`ApplicationEvent` 和 `FollowUpReminder` 的领域模型；
- `email_sync.py` 的 Gmail 只读同步、招聘邮件分类、内部回复草稿和附件隔离；
- `gmail_send.py`、`GmailSideEffectProvider`、Side-effect Kernel、Outbox、Provider Receipt 和 reconciliation 的发送骨架；
- Temporal 持久化工作流、LangGraph 状态图和 review gate；
- Vue SPA 的 Dashboard、Jobs、Companies、Applications 页面。

当前缺少的是一条以用户申请为中心的产品闭环：用户目标没有成为爬取计划和匹配的统一输入，爬取计划尚未成为可管理资源，职位收件箱不能自然进入申请工作区，简历定制与投递材料没有稳定绑定，邮件发送和邮件回复跟踪也没有在前端形成连续时间线。

本设计受以下约束：

- 单用户和自托管仍是范围边界；不引入多租户权限模型；
- 未经用户确认的外部写入必须拒绝；“用户确认”发生在 CareerOps UI/API 内，由系统调用邮件提供商完成发送；用户不需要打开 Gmail 手动发送；
- `CAREEROPS_EXTERNAL_WRITES_ENABLED`、`CAREEROPS_AUTO_SEND_ENABLED` 和 `CAREEROPS_GOOGLE_OAUTH_ENABLED` 不得作为本变更的默认启用结果；
- 模型输出只能是建议或草稿，不能改变策略事实、收件人、申请状态或发送权限；
- 真实 D0 数据仍须由真实、脱敏、可审查的 pilot/holdout 提供，不能用合成数据宣称产品质量；
- 所有跨外部系统的写入都必须经过审批/确认、Outbox、隔离 worker、回执、reconciliation 和 append-only audit。

## Goals / Non-Goals

**Goals:**

- 建立从用户画像、采集计划、职位收件箱到申请、邮件投递和邮件跟进的可操作主流程；
- 将 `Application` 作为核心生命周期记录，连接职位、候选人、材料、投递渠道、邮件线程、事件和跟进任务；
- 让用户在 CareerOps 内确认初始申请邮件或回复邮件，系统随后通过受控 Gmail provider 执行发送；
- 以不可变版本、来源证据、payload hash 和事件时间线支持解释、审计、重放和纠错；
- 把长期运行/重试工作交给 Temporal，把结构化建议编排交给 LangGraph，把所有外部副作用授权交给 Side-effect Kernel；
- 让用户能够管理采集源、主题、过滤范围、间隔、时间窗、限额、暂停和运行历史；
- 在模型不可用时仍支持确定性采集、硬过滤、申请记录、人工提交记录和邮件状态维护；
- 提供可观测、可回滚、可逐阶段启用的迁移路径。

**Non-Goals:**

- 不实现无人值守海投、批量申请、自动填写第三方申请表或绕过登录/CAPTCHA/Cloudflare；
- 不默认启用 Gmail OAuth、Gmail Send、Auto-send 或主邮箱全量读取；
- 不允许模型自行决定收件人、申请状态、简历事实、OAuth scope、策略 allow 或工具调用；
- 不把“自动回复”定义为无需用户批准的普遍自动发送；高风险回复永远需要用户确认；
- 不在本变更中引入多用户、团队协作、公共 SaaS 或跨用户数据隔离；
- 不保证招聘方一定回复、不保证录用，也不把模型分数当作求职结果；
- 不把外部申请表的“打开链接”误报成“已投递”。

## Decisions

### 1. 以申请生命周期而不是 Agent 图作为产品骨架

用户界面和 API 以 `Application` 为中心；Agent、Temporal workflow 和 adapter 是实现细节。用户看到的是职位收件箱、申请工作区和跟进中心，不需要理解 `crawl → match → review_gate → send` 的内部节点图。

主流程如下：

```text
CareerProfileVersion + CrawlPlanVersion
              │
              ▼
       CrawlRun / SourceObservation
              │
              ▼
 CanonicalJob → hard filters → advisory match
              │
              ▼
       Favorite → Application(FAVORITED)
              │
              ▼
  Prepare → ResumeVersion + ApplicationPackage
              │
              ├── external_form → user completes outside → manual confirmation
              │
              └── email → CareerOps review/confirm → kernel → provider receipt
                                                    │
                                                    ▼
                                             Application(SUBMITTED)
                                                    │
                                      Gmail sync / thread association
                                                    │
                                                    ▼
                              EmailEventProposal → user review → timeline
                                                    │
                                      ReplyDraft / FollowUpReminder
                                                    │
                              CareerOps approval → same email send chain
```

`FAVORITED` 只代表用户选中职位；用户进入申请准备工作区后才转换为 `PREPARING`。只有系统发送获得 provider receipt，或用户明确确认外部表单完成后，才进入 `SUBMITTED`。

### 2. 将采集计划建模为一等资源

新增三个概念：

- `crawl_sources`：一个官方 Careers/ATS/公司来源及其适配器、信任级别、terms/robots 状态、启用状态；
- `crawl_plans`：用户选择的 source 集合、主题、关键词、地点、远程、薪资、职级、排除项、内容范围、频率和限额；
- `crawl_runs`：一次计划执行的不可变快照、状态、计数、错误、安全决策和输出引用。

计划更新采用 copy-on-write：旧版本不变，新版本只影响后续 run。每个 `job_posting_version` 记录 `crawl_run_id` 和 `crawl_plan_version_id`，因此用户可以解释“这个职位为什么进入收件箱”。

计划调度采用 Temporal workflow 或 Temporal schedule 的可恢复触发；计划本身不直接持有 worker lease。单域限流、SSRF、terms/robots 和重试策略仍在 adapter/policy 层执行，计划只能声明范围，不能绕过安全策略。

### 3. 以“职位收件箱”承载确定性筛选和建议性匹配

职位处理顺序固定为：

1. 来源适配器解析和 schema 校验；
2. canonical URL/source ID/title-location 等确定性去重；
3. 关闭/重开和内容版本处理；
4. 用户偏好的硬过滤；
5. 证据约束的 requirement match；
6. 可选 LLM 的解释性排名；
7. inbox projection。

硬过滤和 LLM 匹配不得互换顺序。LLM 只能返回 `review_only` 的建议，且每个匹配点绑定岗位证据和候选人 evidence。未知 remote、未知授权、缺失薪资等情况不得被模型分数隐式放行。

### 4. 候选人资料采用版本化、证据化和岗位包隔离

基础简历永远不可变。岗位定制不修改基础简历，而是创建 `ApplicationPackage`：

```text
CandidateProfileVersion
      ├── confirmed EvidenceItems
      └── ResumeVersion (immutable content hash)
                         │
CanonicalJobVersion ─────┴─────▶ ApplicationPackageVersion
                                  ├── resume presentation/diff
                                  ├── cover letter
                                  ├── answers
                                  ├── email subject/body
                                  ├── claim → evidence refs
                                  └── approval + payload hash
```

Resume Agent 的职责是解析、比较、提出可追溯编辑、生成草稿和校验 claim；不是创造事实。用户编辑后的内容可以包含用户主动添加的事实，但在系统标记为“可信申请材料”前必须通过用户确认和 evidence/claim 校验。

### 5. 将“用户确认”设计为系统发送的授权事件

用户在 CareerOps 中点击最终确认时，API 不直接调用 Gmail。它执行以下事务性步骤：

1. 读取当前 application、approved package、trusted recipient、account 和 attachment state；
2. 重新计算 normalized payload hash 和稳定 idempotency key；
3. 创建或复用 `ActionIntent`、`PayloadVersion`、`PolicyDecision` 和 `ApprovalRequest`；
4. 在同一业务事务中写入 eligible Outbox event；
5. 返回 `pending` 状态，前端显示发送处理中；
6. Side-effect Worker 解析 opaque credential reference 并调用 Gmail provider；
7. 保存 provider attempt/receipt，必要时进入 reconciliation；
8. 只有 receipt 确认后才追加 `SUBMITTED` 或 `REPLY_SENT` 事件。

这里的 `ApprovalRequest` 不是要求用户再去另一个邮箱确认，而是系统对 UI 最终确认的持久化审计表示。任何 payload、recipient、account 或 attachment 变化都会创建新的 payload version 并使旧 approval 失效。

### 6. 初始投递和回复共用同一发送内核，策略分类不同

初始申请邮件和后续回复都使用同一个 `EmailDeliveryPort`/Gmail provider 和同一个 Side-effect Kernel，避免存在绕过安全链路的“快捷发送”路径。两者在 domain 层区分 `intent_kind` 和 `SendCategory`：

- 初始申请：`initial_application`，始终需要用户最终确认；
- 低风险回复：delivery confirmation、recruiter contact ack、confirmed time ack 等，当前仍默认不自动发送，但用户确认后可系统发送；
- 高风险回复：salary、offer、visa、identity/bank、withdrawal、unknown 等，永远不得 auto-send；
- follow-up、resume/link、work authorization、deadline commitment 等，始终进入用户审核。

未来若要支持低风险自动发送，必须同时满足 global kill switch、Release Qualification、账户 opt-in、policy allowlist、可信上下文、D0 证据和 reconciliation；本 change 不打开这些开关。

### 7. Gmail 同步采用专用账户、只读输入和提议事件

邮件同步只处理明确授权的招聘邮箱，优先采用 provider history/watch 增量同步。消息以 provider IDs 去重，线程以稳定 thread ID 保存。非招聘邮件不持久化正文；招聘邮件只保留满足 retention 的最小内容和 bounded evidence。

邮件理解输出 `EmailEventProposal`，至少包括：

- `application_id` 或 unresolved link candidate；
- category/outcome/next step；
- extracted fields；
- confidence；
- message and evidence references；
- classifier/model/rules version；
- risk level and review state。

proposal 不能直接写 `ApplicationState`。用户接受后才通过合法状态转换追加 `ApplicationEvent`。歧义关联、Offer/薪资/签证/身份材料和 prompt injection 一律进入 review queue。

### 8. 统一数据模型，避免在多个表里维护同一状态

目标关系如下：

```text
User ──1:1── Candidate
Candidate ──N── ProfileVersion / ResumeVersion / EvidenceItem
User ──N── CrawlSource ──N── CrawlPlanVersion ──N── CrawlRun
CrawlRun ──N── JobPostingVersion ──N── CanonicalJob
Candidate + CanonicalJob ──1── ApplicationCycle
Application ──N── ApplicationPackageVersion
Application ──N── ApplicationEvent
Application ──N── FollowUpReminder
Application ──N── EmailThread ──N── EmailMessage
EmailMessage ──N── EmailEventProposal / ReplyDraft
ReplyDraft / InitialSubmission ──1── ActionIntent ──N── PayloadVersion
ActionIntent ──N── OutboxEvent / ProviderAttempt / ProviderReceipt / AuditEvent
```

核心约束：

- 单用户的 active candidate 与 user 关联必须由服务端解析，客户端不能提交任意 candidate ID 代替当前用户；
- application 对 candidate + canonical job + cycle 具有幂等约束；
- application event append-only，当前状态是经过合法转换后的 projection；
- email thread 至多绑定一个 application，歧义时进入 unresolved 状态；
- approval/payload/receipt 以 hash 和版本绑定，不允许原地改写历史；
- crawl/source/job 记录删除采用 retention/retirement 语义，不因用户隐藏职位而删除审计证据。

### 9. API 与前端按工作区拆分

建议的 API 资源边界：

| 工作区 | 资源 | 关键动作 |
|---|---|---|
| Profile | `/profile`, `/preferences`, `/resumes`, `/evidence` | 版本创建、确认、选择 |
| Sources | `/crawl-sources`, `/crawl-plans`, `/crawl-runs` | CRUD、pause/resume、run-now、历史 |
| Inbox | `/jobs`, `/matches`, `/applications` | filter、favorite、ignore、prepare |
| Application | `/applications/{id}`, `/packages`, `/submission-preview` | 选材料、diff、确认、外部表单回填 |
| Email | `/email-accounts`, `/threads`, `/messages`, `/email-events` | 连接、同步状态、关联、审查提议 |
| Review | `/review/{approval_id}` | approve/reject/edit，复用现有受保护入口 |
| Follow-up | `/follow-ups` | snooze、complete、reschedule、cancel |

前端主导航应为：职位收件箱、采集计划、申请工作区、招聘跟进、简历与偏好。Dashboard 只做摘要和待办入口，不承担状态写入。所有页面必须有 loading、empty、stale/unavailable、error 和 next action 状态。

### 10. 编排和职责分工

- **Adapters**：抓取和 provider 交互，不拥有业务决策；
- **Application services**：执行 profile、crawl plan、matching、package、email sync、follow-up 的业务规则；
- **LangGraph**：仅编排提取/匹配/草稿/审查建议，所有模型输出标记 untrusted/review-only；
- **Temporal**：承载 crawl run、mail sync、outbox drain、sweeper、reconciliation 等长任务和可恢复活动；
- **Side-effect Kernel**：唯一的外部写入授权入口；
- **PostgreSQL**：业务真源、状态机、事件、审计关联和幂等身份；
- **Redis**：限流、短租约、缓存；Redis 不作为授权真源，故障时 fail closed；
- **Vue SPA**：展示投影和收集用户确认，不在前端实现授权判断。

### 11. Feature flags 和发布门

能力采用单向安全开关，默认值如下：

| 能力 | 默认 | 未通过门时的可用替代 |
|---|---|---|
| crawl plan/read discovery | 开启（遵循 source policy） | 计划暂停/单次手动 run |
| model matching/tailoring | disabled 或 review-only | 确定性 profile/filter/package 校验 |
| Gmail OAuth read | 关闭 | 手动导入/手动状态更新 |
| system-managed Gmail send | 关闭 | 预览、保存包、手动 external-form 确认 |
| auto-send | 永久默认关闭 | CareerOps 内人工确认后发送 |
| external form automation | 不实现 | 官方链接 + 用户手动完成 |

系统配置变化、model/provider/prompt/schema 变化、migration 变化、policy 变化、dataset/qualification 变化都必须使相关 provider capability 重新进入未发布状态。

### 12. 可观测性和验收指标

工程安全不变量：

- 未经确认的 provider writes = 0；
- 目标/正文/附件与审批 payload 不一致的发送 = 0；
- 确认重复导致的重复 provider effect = 0；
- ambiguous send 自动重试 = 0；
- 未经证据支持的 claim 进入 approved package = 0；
- unresolved email thread 被自动错误绑定 = 0（必须进入 review）；
- prompt injection 导致 tool/policy/send effect = 0。

产品信号：

- 从 crawl run 到 trusted shortlist 的时间；
- shortlist 被用户保留/忽略/纠正的比例；
- 从 favorite 到 approved package、从 approved package 到 confirmed submission 的转化；
- 用户每周管理职位和跟进所花时间；
- 邮件事件正确关联率和需要人工纠正率；
- follow-up 完成率和过期任务率。

这些指标必须区分工程测试、真实 pilot 和生产用户反馈，不能用单元测试通过代替用户价值证据。

## Risks / Trade-offs

- **[Risk] 变更范围过大，多个子系统同时演进导致无法交付。** → 采用分阶段 feature flags 和下方任务顺序；先完成 read/preparation/draft，再开启 system-managed send，最后接入 inbound/reply。
- **[Risk] 用户偏好过于复杂，配置成本高于收益。** → 提供安全默认模板和渐进式配置；硬约束与软偏好分栏，先只要求目标角色、地点和远程规则。
- **[Risk] ATS/公司网站结构变化造成职位质量下降。** → adapter manifest、schema contract、crawl run 指标、source-specific pause 和人工查看原始证据。
- **[Risk] 误去重让用户错过岗位或错误合并历史。** → precision-first deterministic identity，语义合并只做 proposal，所有 merge 可回滚。
- **[Risk] 简历 Agent 生成夸大或不真实内容。** → immutable resume、claim→evidence、diff、approved package gate、模型 review-only 和无证据 claim 阻断。
- **[Risk] 邮件投递重复、错发或超时不确定。** → recipient provenance、immutable payload、idempotency key、provider reconciliation、no blind retry、receipt-gated state transition。
- **[Risk] 收件箱内容 prompt injection。** → untrusted content envelope、无 tools、最小化模型输入、策略只读 trusted facts、审查队列。
- **[Risk] 邮件线程错误关联到错误申请。** → provider IDs 优先，多候选时 unresolved，不自动猜测，用户确认后才固定关联。
- **[Risk] Gmail 权限和个人隐私扩大。** → dedicated recruiting account、read-only scope、非招聘正文不持久化、账户撤销即停、短留存和审计。
- **[Risk] 系统管理发送的用户体验不透明。** → CareerOps 内显示完整 payload、发送中状态、provider receipt、失败原因和 reconciliation task；不把“已排队”显示成“已发送”。
- **[Risk] Temporal、LangGraph、PostgreSQL 和 Vue 的联调复杂。** → 先用 application service/contract tests 固定行为，再接 worker/UI；每个阶段保留可运行的降级路径。
- **[Risk] 当前 ADR 与新范围冲突。** → 本 change 实施前新增 superseding ADR 和 threat-model delta；未完成迁移前不在 README 宣称旧 M1 范围已通过。

## Migration Plan

### Phase 0 — Contract freeze and safe shell

1. 以本 change 的 specs 为唯一新产品行为合同，补充 superseding ADR、威胁模型增量、API 错误码和状态枚举表。
2. 创建 feature flags 和 capability resolver；默认保持 Google OAuth、external writes、auto-send 关闭。
3. 增加跨 capability 的 ID/状态/approval contract tests，先不连接真实 provider。

### Phase 1 — Profile, resume, and crawl management

1. 建立用户到 candidate 的服务端关联和 profile/resume/evidence API。
2. 建立 `crawl_sources`、`crawl_plan_versions`、`crawl_runs` 迁移、仓储和 Temporal run 入口。
3. 将现有脚本/adapter 的 source observation 接入 crawl run provenance。
4. 实现采集计划 UI、运行历史和安全失败状态。

### Phase 2 — Job inbox and application workspace

1. 把现有 canonical job/matching projection 接到 active profile 和 plan version。
2. 完善 favorite/ignore/prepare/application 幂等和状态转换。
3. 实现 resume selection、evidence review、package draft、diff 和 package approval。
4. 实现 external-form 的 URL 展示和用户手动确认，不实现表单自动填充。

### Phase 3 — System-managed initial email delivery

1. 完成可信 recruiting contact → channel eligibility → email payload 的绑定。
2. 将 CareerOps 最终确认映射为 durable ApprovalRequest/ActionIntent/Outbox event。
3. 在 fake provider 上完成重复点击、crash、timeout-with-success、reconciliation、receipt 和 application transition 测试。
4. 完成 Gmail provider 的专用账户、credential reference、attachment quarantine 和发送回执适配。
5. 本 change 只实现并验证 fake provider/离线受控 harness 的 system-managed send 路径；不得启用 `CAREEROPS_EXTERNAL_WRITES_ENABLED`、`CAREEROPS_AUTO_SEND_ENABLED` 或 `CAREEROPS_GOOGLE_OAUTH_ENABLED`，不得要求真实 OAuth/token。真实能力激活必须由独立的未来 release/qualification change 处理。

### Phase 4 — Inbound Gmail intelligence

1. 实现专用账户 OAuth/read sync、cursor/watch、message/thread dedup 和撤销停用。
2. 将现有 `email_sync.py` 接入 durable repositories 和 application/thread linkage。
3. 实现 classifier/extractor proposal、evidence span、risk category 和 review queue。
4. 将接受的 proposal 通过合法 application transition 写入 timeline。

### Phase 5 — Reply drafts and follow-up

1. 实现 reminder rules、snooze/reschedule/cancel/complete。
2. 实现 reply draft context minimization、thread header binding、diff 和 risk review。
3. 复用 Phase 3 的系统管理发送链路发送用户确认后的回复；不实现默认 auto-send。
4. 建立统一申请时间线和 Dashboard 待办入口；本 change 只使用 fixture/fake provider 验证 system-managed send，真实 Gmail OAuth/send 激活不属于本 change。

### Phase 6 — Pilot and qualification

1. 用真实、合法、脱敏的 discovery/parser/dedup 数据验证采集和职位收件箱。
2. 按 D0 计划完成 evidence match、contact、email、policy/injection、reconciliation 等 pilot/holdout。
3. 在 fake provider 和不含真实凭据的隔离 harness 执行发送/恢复测试；不得启用仓库禁止的 OAuth、external-write 或 auto-send 开关。真实 provider qualification 与激活必须由独立变更完成，不能由本地测试宣称完成。
4. 独立复核安全不变量、隐私/留存、角色权限、备份恢复和用户体验指标。

### Rollback strategy

- 所有 schema 变更优先采用 additive migration；旧表和旧投影在新路径稳定前保留。
- 任何阶段出现安全或数据一致性问题，先关闭对应 capability flag；读取、预览、手动记录和历史查询保持可用。
- 发送链路问题通过停止新 Outbox claim、撤销 provider capability、进入 reconciliation/manual review 回滚；不删除已产生的审计和 provider receipt。
- 邮件关联错误通过解除 thread/application projection 关联并追加更正事件修复，不直接修改历史消息或申请事件。
- 计划/职位 projection 错误通过重跑固定的 crawl plan version、parser version 和 run identity 修复，不覆盖原始 evidence。
- 只有完成数据迁移、回滚演练和 `make verify`/`make verify-db`/安全测试后，才允许进入下一阶段。

## Open Questions

- 第一阶段是否只支持 Gmail 作为 system-managed send provider，还是同时抽象出未来的 SMTP/其他 provider port？本 change 建议只实现 Gmail adapter，保留 port。
- 初始邮件申请是否要求用户配置专用招聘 Gmail，还是允许已有专用账号？设计按“明确用户授权、非主邮箱、只保存 opaque credential reference”处理。
- 外部申请表完成确认需要哪些最小证据（截图、确认号、用户备注、URL/时间）？建议先要求 URL、时间和用户确认，截图作为可选证据。
- 简历岗位定制是否允许模型直接输出整份新简历，还是只输出结构化 diff？建议第一版只允许结构化 diff + 用户确认后的渲染，降低不可追溯风险。
- 邮件事件接受是否需要每一种低风险状态都人工确认，还是允许确定性 provider receipt/明确送达确认自动写入？建议第一版所有状态改变都产生 reviewable proposal，只有无业务状态影响的同步元数据可自动写入。
- 低风险 auto-send 是否属于后续独立 change？是。本 change 只支持“用户在 CareerOps 确认，系统发送”，不打开无人值守 auto-send。
