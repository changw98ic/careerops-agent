# CareerOps Agent v1 MVP 具体实施计划

> 状态：Risk-closed execution plan
> 日期：2026-07-17
> 仓库：CareerOps Agent repository
> 基线：greenfield；当前只有 `.git/`，没有源码、依赖、测试、配置或远端
> 需求来源：CareerOps Agent Spec v0.1（用户提供）
> 风险决策：[careerops-agent-risk-closure.md](careerops-agent-risk-closure.md)

## 1. 结果摘要

本计划把原 Spec 的 M0–M7 改写为一个可以按依赖顺序执行、逐阶段验收的交付路径：

1. 先增加一个 **M-1 范围与验收冻结**，解决范围冲突、指标公式、数据模型和隐私决策。
2. M0 先建设安全内核，而不是只做工程脚手架。
3. M1–M3 交付无外部写副作用的职位运营 Beta。
4. M4 只接入 Gmail 读取、分类、内部草稿和审批，不申请发送能力。
5. M5A 先证明通用副作用内核，再由 M5B 创建 Calendar Event。
6. M6 实现 Gmail Send，但全局开关继续关闭。
7. M7 完成安全、评测、恢复和发布门后，才允许用户显式开启低风险 Auto-send。

各里程碑工程任务合计约 **112–156 工程日，即 23–32 个全职工程周**。D0 Evaluation Data Workstream 另计 **20–35 人日**，因此总项目工作量为 **132–191 人日**。单人顺序执行约 27–39 个纯工作周；考虑外部集成等待和返工缓冲，现实日历预算为 **30–44 周**。如果有独立标注/复核者从 M1 起并行，工程关键路径仍可按 **24–36 周**管理。其中：

- R0「职位运营 Beta」（M-1–M3）：约 10–14 个工程周。
- R1「人工监督求职助理」（加 M4–M5B）：累计约 17–24 个工程周。
- R2「受控自动化 v1」（加 M6–M7）：累计约 23–32 个工程周。

估算不包含跨 7 天 token/Watch 观察的日历等待，也不包含未来扩大分发范围后可能触发的 Google OAuth 审核等待。D0 的样本量、双标与人日已经固化在 [labeling-plan.md](../../docs/evaluation/labeling-plan.md)，不再作为隐含工作。

## 2. 需求摘要

### 2.1 MVP 必须交付

- 公司、官方 Careers 入口、Greenhouse、Lever、Ashby、JSON-LD、Sitemap 和静态 HTML 职位发现与增量抓取，来源边界见 Spec §4.1。
- 职位标准化、跨来源去重、版本、关闭/重开、Remote 资格、技能要求、证据约束匹配和公开招聘联系方式。
- Application 生命周期、已有材料版本登记、手工投递确认和跟进提醒。
- **专用求职 Gmail** 的增量同步、招聘邮件分类、Application 关联、内部回复草稿和审批队列；主邮箱标签模式后置。
- Calendar FreeBusy、候选时间、时区/缓冲/提前量/每日上限规则，以及招聘方确认后由用户点击“重新验证并创建”写入专用求职日历。
- Policy、Approval、幂等、Outbox、审计、Prompt Injection 隔离、OAuth Token 加密、SSRF 防护、备份恢复和可观测性。
- 中文优先 Web Console；英文/中文 JD 与邮件；所有时间同时显示原时区和 `Asia/Shanghai`。

### 2.2 永久硬边界

以下内容不因工期、配置或模型输出而进入 MVP：

- 不绕过登录、验证码、Cloudflare 或 robots；不使用代理池规避限制。
- 不猜测员工邮箱、不探测邮箱存在性、不抓普通员工私人联系方式。
- 不无人值守海投，不自动提交申请表，不自动完成 Take-home。
- 不自动谈薪、接受/拒绝 Offer、退出流程或发送身份证件、银行信息。
- 不读取非专用范围的私人邮件正文，不删除邮件，不修改非求职日历事件。
- LLM 永远不获得 Gmail、Calendar、浏览器或 Secret Store 凭证。

完整边界来自 Spec §5 和工具权限矩阵。

## 3. 本计划冻结的范围裁决

| 冲突或缺口 | MVP 裁决 | 后续阶段 |
| --- | --- | --- |
| Playwright 在第二阶段，但架构、测试和 Compose 又包含 Browser Worker | MVP 不安装、不运行 Browser Worker；只实现 HTTP egress guard、Browser port 和默认 deny 配置。普通官网限定 JSON-LD、Sitemap、静态 HTML | 第二阶段单独通过 Browser Worker 威胁模型和安全门后启用 |
| M2 需要 Evidence Index，但自动 GitHub 索引在第二阶段 | MVP 支持人工审核的 YAML/JSON/本地仓库冻结清单导入，记录 commit SHA、path、symbol、hash | 自动 GitHub 同步与增量索引后置 |
| M3 需要 Resume Version，但定制生成在第二阶段 | MVP 只导入、哈希、选择和版本化已有简历；Application Package 只保存人工确认内容和证据绑定 | 自动改写简历、求职信生成后置 |
| M5/E2E 要 Prep Pack，但 Prep Pack/STAR 在第二阶段，最终 DoD 又未要求 | 从 MVP Gate 移除生成式 Prep Pack；M5 仅提供确定性的公司、岗位、要求、证据摘要 | 完整 Prep Pack、问题生成、STAR 库后置 |
| UI 有页面需求但没有前端技术 | MVP 使用 FastAPI + Jinja2 + HTMX 的服务端渲染薄 UI；少量交互使用受 CSP 约束的本地 JS | 若产品扩展为多用户，再评估独立 SPA |
| Gmail “创建草稿”和“发送”希望分阶段授权 | M4 草稿保存在 CareerOps 内部，不请求 `gmail.compose`；M6 才请求 `gmail.send`。原因是 `gmail.compose` 本身同时允许管理草稿和发送 | 若必须同步 Gmail Draft，只能在 M6 后作为有发送权限的能力处理 |
| Object Store 未选型 | 单机 MVP 使用 content-addressed 本地持久卷，PostgreSQL 只存引用、hash 和元数据；定义 Storage port | S3/MinIO adapter 后置 |
| MCP 没有里程碑且会扩大授权面 | MVP 不交付 MCP；先稳定 REST/UI 和用户身份边界 | v1.1 增加默认只读 MCP；不直接暴露 send/create-event |
| LLM provider/model 未指定 | 默认 `MODEL_PROVIDER=disabled`；M0 定义 `StructuredModelClient` port，生产 adapter 必须通过隐私、egress 与对应 holdout 资格。确定性规则和 fail-closed 流程不依赖模型 | 增加或升级 provider/model/prompt 时使 Release Qualification 失效并跑完整评测 |
| Google OAuth 分发与长期 refresh token 未定义 | MVP 仅支持用户自备 Google Cloud 项目/OAuth client、External In production personal-use、专用求职 Gmail；Testing 只用于短期开发 | 共享 OAuth client、多人分发或主邮箱标签模式必须单独做合规/安全版本 |
| Gmail Watch 默认要求公网 webhook | MVP 使用 Pub/Sub pull/StreamingPull；默认部署不暴露 webhook | push webhook 后置，并需新 ADR 与身份、重放、DoS Gate |
| Golden Dataset 工作量未进入估算 | 新增 D0，最低规模、双标与封存规则见 labeling plan；预算 20–35 人日，总项目 132–191 人日 | 扩大产品范围时同步扩大数据预算，不能降低 Gate |
| Calendar FreeBusy 与 insert 不原子 | MVP 只允许用户点击后 fresh preflight 并写入专用日历；不带外部 attendee，写后 reconcile，竞态时停止确认邮件并进入人工队列 | 真正自动排期保持后置 |
| 跨币种薪资评分没有汇率来源 | MVP 解析原币种、金额和周期；只使用用户提供并带生效日期的静态 FX snapshot 做比较，缺少可比数据时 salary score 为 unknown/neutral | 实时 FX provider 后置 |
| MVP 允许部分附件，但缺少执行边界 | 只允许限额后的 `text/calendar`、普通文本和 PDF 元数据/纯文本；先 quarantine、类型嗅探、杀毒和无脚本提取，其他类型 deny | Office/压缩包等扩展类型不进入 MVP |
| Auto-send 示例配置包含 true，但默认策略与 M7 发布门要求关闭 | 初始配置、开发、CI、迁移默认全部 false；global kill switch、有效 Release Qualification、账户级显式开关、Policy allow 四者同时满足才可自动发送 | 无 |

裁决依据包括 Spec 的第二阶段范围、申请材料边界、配置示例与最终 DoD。

## 4. 已校验的外部集成事实

- Gmail Watch 支持标签过滤，通知只携带 mailbox history ID；客户端仍需用 `history.list` 获取变化。Watch 至少每 7 天续期一次，官方建议每天续期；通知可能延迟或丢失，所以必须周期性对账：[Gmail Push 官方文档](https://developers.google.com/workspace/gmail/api/guides/push)。
- `gmail.readonly`、`gmail.metadata` 和 `gmail.compose` 属于 Restricted scopes；`gmail.send` 是更窄的发送 scope，而 `gmail.compose` 同时包含草稿管理和发送能力：[Gmail scopes 官方文档](https://developers.google.com/workspace/gmail/api/auth/scopes)。
- Gmail 回复线程需要匹配 Subject，并正确设置 `References` 与 `In-Reply-To`：[Gmail Sending 官方文档](https://developers.google.com/workspace/gmail/api/guides/sending)。
- Calendar FreeBusy 可以使用最窄的 `calendar.freebusy` scope；事件写入再增量申请 `calendar.events.owned` 或经 M-1 ADR 选择的更窄可行 scope：[Calendar scopes](https://developers.google.com/workspace/calendar/api/auth)、[FreeBusy](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query)。
- Google Web Server OAuth 支持 offline access 和 incremental authorization；Refresh Token 必须按服务端 Secret 处理：[OAuth Web Server 官方文档](https://developers.google.com/identity/protocols/oauth2/web-server)。
- External OAuth app 处于 Testing 且请求 Gmail/Calendar 等额外 scope 时，refresh token 通常 7 天失效；长期 Watch 验收必须使用 In production 配置：[OAuth token expiration](https://developers.google.com/identity/protocols/oauth2)。
- personal-use app 在不超过 100 个用户等条件下可以不完成验证，但仍会显示未验证提示且仍受 User Data Policy 约束：[Personal-use app verification](https://support.google.com/cloud/answer/13464323?hl=en)、[User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)。
- Pub/Sub Pull/StreamingPull 由 subscriber 主动出站读取并 ACK，适合单机自托管而无需公网 push endpoint：[Pub/Sub pull](https://docs.cloud.google.com/pubsub/docs/pull)。
- Calendar FreeBusy query 与 Events insert 是两个独立请求，没有跨请求原子空闲占位；验收必须区分可预检冲突与不可消除的外部竞态：[FreeBusy](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query)、[Events insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert)。

重要含义：**指定 Gmail 标签是应用层数据最小化，不是 OAuth scope 级隔离**。因此 MVP 强制专用求职 Gmail；同时 Testing OAuth 不能作为长期运行方案，Pub/Sub push webhook 也不是默认部署组件。

## 5. 架构不变量

以下规则从 M0 起写成数据库约束、权限、代码边界和测试，不留到 M7“安全加固”时再补：

1. PostgreSQL 是业务状态唯一真源；Redis 只做缓存、限流、租约和短期锁，符合 Spec §6.1。
2. `canonical_jobs` 表示用户看到的逻辑职位；`job_postings` 表示每个来源的 occurrence。来源关闭不等于 canonical job 一定关闭。
3. 所有外部动作统一走：`ActionIntent → Immutable Payload Version → Policy Decision → Approval → Outbox → Side-effect Worker → Provider Reconciliation → Audit Event`。
4. Agent 只输出有 Schema 的事实、建议或 Action Proposal；Agent 不能直接写 provider，也不能获得 OAuth Token。
5. 外部网页、JD、邮件、签名、附件、日历描述全部是 `UNTRUSTED_CONTENT`；其中出现的工具指令没有权限含义。
6. 明确证据优先于模型推断；无证据、歧义或 Schema 失败时输出 `unknown` / `review_required`，不做乐观默认。
7. Temporal Workflow 只负责编排确定性状态；网络、数据库、模型和时间相关 I/O 全部位于 Activity。Workflow 变更使用版本标记并做 replay test。
8. Approval 绑定不可变 payload hash；任何编辑、目标、附件或时间变化都会使旧决策失效。
9. 自动发送存在独立的系统级 kill switch；普通配置、数据库 UI 和模型输出都不能打开它。
10. HTML 在保存原文与 UI 展示之间分层：原始内容隔离存储，展示内容经过 allowlist sanitizer，并启用 CSP。
11. LLM 分类和抽取只能提出 Application 状态变更；状态 reducer 只接受确定性 provider 事件或经 Policy/人工确认的 proposal。
12. `MODEL_PROVIDER=disabled` 是可运行且经过测试的默认状态；未资格化的模型 capability 永远是 `review_only`。
13. MVP 只接受用户自备 OAuth client 与专用求职 Gmail；OAuth token 的 envelope key 不与数据库、镜像或常规备份同存。
14. Calendar Event 永远由当前用户动作触发；本地 lease 不能被描述成对 Google Calendar 的分布式锁。

## 6. 数据模型修正

原 Spec 的 `NormalizedJob(source_id, external_id)` 与单来源唯一约束不足以表达跨来源去重和来源独立开关状态，见 Spec 的职位模型与唯一约束。M0 冻结以下模型：

### 6.1 职位聚合

- `canonical_jobs`：company、canonical title、聚合 active 状态、主展示 posting。
- `job_postings`：source、external ID、canonical URL、来源状态、抓取时间。
- `job_posting_versions`：raw snapshot ref、content hash、changed fields、captured_at。
- `job_merge_decisions`：候选 posting、canonical job、规则/人工理由、score、actor、可回滚状态。
- `job_aliases`：标准化标题、地点和 URL alias。

唯一约束至少覆盖：

- `job_sources(company_id, source_type, source_identifier)`。
- `job_postings(source_id, external_id)`。
- `job_posting_versions(job_posting_id, content_hash)`。
- `job_merge_decisions(job_posting_id, active)` 的单活跃关联。

### 6.2 外部副作用

- `action_payload_versions`：不可变目标、正文/时间/附件引用、payload hash。
- `action_intents`：动作种类、资源、当前 payload version、idempotency key、状态。
- `policy_decisions`：规则版本、decision、理由、payload hash、过期时间。
- `approval_requests`：审批人、决策、过期、一次性/永久规则引用。
- `outbox_events`：业务事务内写入，单独 publisher 领取。
- `side_effect_attempts`：每次 provider 调用、lease、request fingerprint、响应/错误。
- `provider_receipts`：provider message/event ID、reconciliation key、最终状态。
- `audit_events`：append-only，引用上述不可变记录，而不只保存一个无法回放的 hash。

### 6.3 保留策略

原始网页 30 天后可删除，但必须长期保留以下最小证据，避免 24 个月的 JobVersion 指向不存在的快照：

- 来源 URL、抓取时间、内容 hash。
- 决策使用的脱敏原文片段与片段 hash。
- 结构化字段及提取器/规则/模型版本。
- Approval 实际看到的不可变 payload。
- Provider request fingerprint、receipt 和审计链。

保留期冲突来自 Spec 的 JobVersion 和数据保留章节。

## 7. 目标仓库结构

在 Spec 目标结构基础上补齐 greenfield 项目真正需要的边界：

```text
.
├── pyproject.toml
├── uv.lock
├── README.md
├── docker-compose.yml
├── alembic.ini
├── .env.example
├── .github/workflows/ci.yml
├── docs/
│   ├── adr/
│   ├── architecture/
│   ├── runbooks/
│   ├── security/
│   └── evaluation/
├── src/careerops/
│   ├── api/
│   ├── web/
│   ├── domain/
│   ├── adapters/
│   ├── application/
│   ├── agents/
│   ├── model_gateway/
│   ├── policy/
│   ├── workflows/
│   ├── integrations/
│   ├── infrastructure/
│   ├── evaluation/
│   └── observability/
├── migrations/
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── e2e/
│   ├── security/
│   ├── chaos/
│   └── fixtures/
├── datasets/
│   ├── schemas/
│   ├── labeling-guides/
│   └── manifests/
├── deploy/
└── scripts/
```

Golden Dataset 的冻结测试样本不直接混在普通测试 Fixture 中；仓库只存脱敏样本或受控下载 manifest，避免把真实邮件和 PII 提交到 Git。

## 8. 依赖顺序

```text
M-1 范围/验收冻结
  └─ M0 工程与安全内核
      └─ M1 职位采集
          └─ M2 Remote/证据匹配
              └─ M3 联系方式/Application
                  └─ M4 Gmail 只读/内部草稿
                      └─ M5A 通用副作用证明
                          ├─ M5B Calendar/排期 ─┐
                          └─ M6 Gmail Send ─────┴─ M7 发布门

D0 Evaluation Data：M-1 启动，按 M1–M7 的前置 Gate 横向交付
```

评测数据、威胁模型、Web UI 和可观测性不是最后补的横向任务；它们从对应能力进入开发前一里程碑开始并行维护。D0 需要独立复核者参与，安全关键 holdout 未签字时不得以“工程已完成”替代发布验收。

## 9. 里程碑实施计划

### M-1：范围、架构与验收冻结

**目标：** 用 4–6 工程日把“能讨论的 Spec”变成“能失败的合同”。

**任务：**

- `M-1.1` 在 `docs/adr/0001-mvp-scope.md` 记录第 3 节全部范围裁决。
- `M-1.2` 在 `docs/adr/0002-canonical-job-model.md` 冻结 canonical job/posting/merge/rollback 模型。
- `M-1.3` 在 `docs/adr/0003-side-effect-authority.md` 冻结 Intent、Policy、Approval、Outbox、Worker、Reconcile 的授权链和 DB role。
- `M-1.4` 在 `docs/adr/0004-google-oauth-scopes.md` 冻结 BYO Google Cloud/OAuth client、External In production personal-use、专用 Gmail、Pub/Sub pull、scope 阶段、token envelope encryption、撤销和 Restricted scope 数据处理。
- `M-1.5` 在 `docs/adr/0005-storage-and-retention.md` 冻结本地对象存储、备份、长期 evidence snippet、删除策略，以及 Temporal 与业务数据库的独立 schema/credential/恢复边界。
- `M-1.6` 在 `docs/adr/0006-model-privacy.md` 冻结 `MODEL_PROVIDER=disabled` 默认态、provider 资格流程、允许发送的数据类别、脱敏、留存、egress allowlist、超时、成本上限和升级回归要求；不要求先锁定厂商。
- `M-1.7` 在 `docs/adr/0007-console-auth.md` 冻结单用户 bootstrap credential、session、CSRF 和反向代理信任；默认拓扑不设置 webhook 例外路由。
- `M-1.8` 在 `docs/adr/0008-compensation-normalization.md` 冻结币种、period、静态 FX snapshot、unknown/neutral 评分和可回放规则。
- `M-1.9` 在 `docs/security/threat-model.md` 完成数据流、信任边界、滥用场景、OAuth/SSRF/XSS/Prompt Injection/重复副作用威胁模型。
- `M-1.10` 在 `docs/evaluation/metric-contracts.md` 定义每项指标的样本单位、分母、abstain 计分、阈值、holdout 规则和命令接口。
- `M-1.11` 按 [labeling-plan.md](../../docs/evaluation/labeling-plan.md) 建立 `datasets/schemas/`、`datasets/labeling-guides/` 和 manifest 合同，定义 discovery/parser、dedup、remote、evidence-match、contact、email、policy-injection、calendar、reconciliation 九类数据；MVP discovery holdout 明确排除动态页面并单独报告 unsupported。
- `M-1.12` 指定 D0 data curator 与独立 reviewer，完成各集合 10% pilot、agreement 检查、PII 流程和分组切分演练；将 20–35 人日写入项目排期。

**出口 Gate：**

- Spec 每个成功指标都能映射到数据集、公式、阈值、评测命令和责任里程碑。
- 每个外部副作用都能画出完整授权链。
- 没有 Playwright、自动材料生成、GitHub 自动同步、生成式 Prep Pack 或 MCP 偷渡进 MVP。
- 模型关闭态、Google BYO/personal-use 模式、D0 owner、Web Console 认证和对象存储不再是隐含依赖。
- 任何非开发配置使用 OAuth Testing、共享 client、主邮箱或公网 webhook 都会 fail closed。

### M0：工程基础与安全内核

**目标：** 8–12 工程日交付可启动、可迁移、可恢复、默认拒绝副作用的最小系统。

**任务：**

- `M0.1` 创建 `pyproject.toml`、`uv.lock`、src layout、Python 3.12 约束和依赖组；固定 Ruff、Pyright、Bandit、pytest、coverage 配置。
- `M0.2` 创建 `src/careerops/config.py`；配置从环境或 Secret reference 加载，启动时验证，不允许把 secret 输出到 repr、异常、日志或 metrics。
- `M0.3` 创建 `src/careerops/api/app.py`、`api/routes/health.py`、请求 ID/trace middleware、`/health/live`、`/health/ready`、`/metrics`。
- `M0.4` 创建 `src/careerops/infrastructure/database/`、Alembic 和初始迁移；包含 candidate、company/source、canonical job/posting、evidence、application event、action/policy/approval/outbox/audit、OAuth credential reference 的最小表。
- `M0.5` 用数据库权限和 trigger 让 `audit_events`、payload versions、application events 追加写；生产应用角色不能 update/delete。
- `M0.6` 创建 `src/careerops/policy/engine.py` 和 `rules.py`；未知 action 默认 deny，读操作 allow，所有 provider write 默认 require approval 或 deny。
- `M0.7` 创建 `src/careerops/workflows/` 和 `infrastructure/temporal/`；实现可被 kill/restart 后继续的 smoke workflow，并加 replay test。
- `M0.8` 创建 Outbox publisher 与禁用状态的 Side-effect Worker 骨架；没有 provider adapter 和有效 release capability 时无法执行写动作。
- `M0.9` 创建单用户 Web Console auth：一次性 CLI bootstrap、Argon2id password hash、session rotation/revoke；默认只绑定 loopback；cookie 为 Secure/HttpOnly/SameSite，所有 mutation 有 CSRF；公网部署要求 TLS 反向代理。
- `M0.10` 创建 `src/careerops/web/` 的 Dashboard shell、错误页、基础导航和 CSP；此阶段只显示系统健康、集成状态和待处理计数。
- `M0.11` 创建 Docker Compose：api、workflow-worker、postgres、redis、temporal、temporal-ui；不含 browser-worker。
- `M0.12` 创建 `.github/workflows/ci.yml`，执行 lint、format check、typecheck、unit、migration smoke、security scan 和 secret scan。

**主要验证：**

- `docker compose up` 后 API、Worker、PostgreSQL、Redis、Temporal 全部 ready。
- 从空数据库 upgrade 到 head、downgrade 一个 revision、再次 upgrade 后 schema 一致。
- Workflow 在等待点 kill Worker，重启后只完成一次。
- 直接写 `audit_events` 的 update/delete 被 DB 拒绝。
- 未知 Action Proposal 不能产生 outbox provider write。
- 仓库和构建日志不存在明文 secret。

### M1：安全的职位发现与采集

**目标：** 15–20 工程日交付官方来源优先、可增量、可追溯的职位 Inbox。

**前置：** discovery/parser/dedup 标注合同在 M-1 冻结；HTTP 请求前 SSRF/Crawl Policy 已可用。

**任务：**

- `M1.1` 实现 `domain/companies.py`、`domain/jobs.py`、`domain/crawl.py` 与对应 repository。
- `M1.2` 实现 `infrastructure/http/safe_client.py`：scheme allowlist、DNS 解析、私网/loopback/link-local/metadata deny、每次 redirect 重验、响应大小/时间限制。
- `M1.3` 实现 robots cache、per-domain rate limit、并发上限 2、403/429/Retry-After、登录墙/CAPTCHA 停止规则。
- `M1.4` 对 `terms_status=unknown` 只允许一次低频发现请求，禁止周期性 crawl，直到用户审阅；explicit blocked 永远不抓。
- `M1.5` 实现 Careers link discovery、官方域名验证、Sitemap、Greenhouse、Lever、Ashby、JSON-LD、静态 HTML adapters。
- `M1.6` 每个 adapter 实现 `detect/list_jobs/fetch_job` 合同，保存 raw response hash、fetched_at、source URL 和 parser version。
- `M1.7` 实现 normalization、canonical URL、posting 版本、closed/reopened/expired 规则。
- `M1.8` 实现分层 dedup：source ID → canonical URL → company/title/location → fingerprint → semantic candidate；语义相似只能创建 merge proposal，不能单独自动合并。
- `M1.9` 实现人工 merge/split/rollback，避免一次错误去重永久污染历史。
- `M1.10` 实现 `CompanyDiscoveryWorkflow` 和 `CrawlJobSourceWorkflow`，所有 I/O 在 Activity。
- `M1.11` 实现 Companies/Jobs REST endpoints 与 Company、Jobs Inbox、Job Detail 的薄 UI。
- `M1.12` 建立 frozen adapter fixtures、cross-source dedup dataset、close/reopen dataset 和 evaluator。
- `M1.13` 实现 raw document TTL/purge workflow；删除完整正文前固化长期 evidence snippet、URL、时间和 hash，并验证没有悬空 version reference。

**出口 Gate：**

- 三类 ATS 与 JSON-LD/Sitemap/static HTML contract tests 全绿。
- 同一 posting 重抓不重复写入，内容变化只新增一个 version。
- 一个来源关闭、另一个来源仍 active 时 canonical job 仍 active。
- 所有职位有来源 URL、时间和内容 hash。
- Careers 入口 recall ≥ 0.90；ATS required-field micro accuracy ≥ 0.95。
- Dedup pairwise F1 ≥ 0.98、precision ≥ 0.99；错误合并可人工拆分并回放。
- SSRF、redirect-to-private、DNS rebinding、429 和响应炸弹测试通过。

### M2：Remote Eligibility 与证据约束匹配

**目标：** 12–16 工程日，让系统能解释“为什么适合/不适合成都或中国 Remote”。

**前置：** 400 条 Remote Dataset、Evidence Match Dataset、模型隐私 ADR 和 untrusted-content contract 已冻结。

**任务：**

- `M2.1` 实现 `domain/candidates.py` 和人工审核 Evidence Import；重复导入按 repository/commit/path/symbol/hash 幂等。
- `M2.2` 实现 `model_gateway/base.py`、disabled adapter、Pydantic schema validation、一次结构修复上限、超时/预算/trace/redaction；首个外部 provider adapter 只有通过 R-LLM 资格 Gate 才启用，未通过时相关结果保持 `review_only`。
- `M2.3` 实现统一 untrusted prompt envelope；模型无 tool binding，网页内容中的指令只能作为数据。
- `M2.4` 先实现确定性 JD/结构化字段提取和地域 hard gate，再把 LLM 限制为低优先级结构化提取器。
- `M2.5` 实现 RemoteEligibility：结构化字段优先、evidence span、confidence、unknown/review_required、CST 等歧义。
- `M2.6` 实现 requirement extraction 与 Candidate Evidence mapping，区分 strong/partial/transferable/unsupported/hard_fail。
- `M2.7` 实现确定性评分器和 tier；地域 hard gate 失败永远不能 `apply_now`。
- `M2.8` 实现 Candidate、Remote Evidence、Match Report API/UI。
- `M2.9` 保存规则版本、prompt version、model ID、input hash、output hash，使结果可回放和重新评测。
- `M2.10` 实现 compensation normalization；没有带生效日期的可用 FX snapshot 时，跨币种 salary dimension 必须为 unknown/neutral，不按猜测汇率加减分。

**出口 Gate：**

- Remote Dataset 的 macro F1 与 China Eligible precision/recall 均 ≥ 0.95。
- 明确地域限制的 hard false allow = 0；模糊 APAC/Remote 进入 review，不假装 China eligible。
- 无 Candidate Evidence 的能力不得产生 strong match。
- frozen inputs 在同一规则/模型版本下得到结构稳定、排序稳定的结果。
- Prompt Injection 样本不能改变 Schema、Policy 或触发工具。

### M3：招聘联系方式与 Application

**目标：** 10–14 工程日交付可用的职位到投递状态运营闭环，但不自动投递。

**任务：**

- `M3.1` 实现 `domain/contacts.py`，只接受职位页、Careers、ATS、官方招聘页和已建立线程中的公开邮箱。
- `M3.2` 每个 contact 保存 source URL、source text、publicly listed、domain match、confidence、allowed actions 和验证时间。
- `M3.3` 实现 Application reducer、合法状态转移、append-only events、人工标记 submitted、来源和 actor。
- `M3.4` 实现 Resume Version Registry：文件引用、hash、目标类型、人工确认；不生成或自动改写简历。
- `M3.5` 实现 Application Package：已有 resume、人工输入 cover letter/note/answers、evidence-bound claims、approval state。
- `M3.6` 实现收藏、忽略、准备投递、已投递、面试阶段、拒信、Offer、on-hold，以及 `follow_up_due_at`、business-day 计算、snooze/cancel/reschedule 规则。
- `M3.7` 实现 Contacts、Applications API 与 Job Detail、Application Pipeline UI。
- `M3.8` 实现 Contact、Application state machine、claim evidence 和并发状态更新测试。
- `M3.9` 实现 `FollowUpWorkflow`：Application submitted 后等待配置的 business days；新邮件/状态变化以 Signal 重排或取消；M3 只在 Dashboard/Applications UI 生成幂等提醒，不发送通知邮件。

**出口 Gate：**

- Contact evidence coverage = 100%，猜测邮箱 = 0，普通员工误识别 = 0。
- Contact precision ≥ 0.98；低置信或域名不一致只能 display/review。
- 非法 Application transition 被拒绝，合法 transition 的事件历史完整。
- Application Package 中所有正向能力声明可追到 Candidate Evidence。
- 系统只能打开官方申请页并记录人工投递，不存在 submit adapter。
- 同一 Application/规则版本只产生一条 active follow-up reminder；收到新回复、职位关闭、rejected/withdrawn/offer 后提醒取消；snooze 与 business-day/timezone 边界测试通过。

### M4：Gmail 只读同步、分类、内部草稿与审批

**目标：** 18–24 工程日交付“自动读、自动分析、自动写内部草稿、人工审批”，发送能力仍不存在。

**前置：** 用户自备 Google Cloud project/OAuth client、专用测试 Gmail、Pub/Sub pull subscription、OAuth/Restricted scope ADR、脱敏 Email Dataset。Testing 可用于开发，但长期 Watch/release 证据必须来自 In production personal-use 配置。

**任务：**

- `M4.1` 实现 `integrations/google_oauth.py`：state/PKCE、offline access、incremental auth、AES-256-GCM envelope-encrypted refresh token、scope inventory、key rotation、revoke；发行包不内置 OAuth client，非开发环境要求 BYO/In production operator attestation，并在 M7 核验 OAuth Console 证据。
- `M4.2` 首先只请求 `gmail.readonly`；MVP 强制专用求职邮箱，label mode 不实现。
- `M4.3` 实现 Pub/Sub pull/StreamingPull subscriber、receipt 持久化、ACK deadline 处理、防重放和 idempotency；不创建公网 webhook route。
- `M4.4` 实现 `gmail.watch`、每日续期、`history.list` 增量同步、周期 reconciliation、history expiration 后受控 full resync。
- `M4.5` 实现 MIME parser、招聘相关性过滤、非招聘邮件正文不落库、HTML sanitizer、附件元数据隔离。
- `M4.6` 实现 email account/thread/message/extraction 模型和 provider ID 唯一约束。
- `M4.7` 实现 Sender Trust、邮件分类、结构化字段/时区/截止日期提取和 Application linkage；模型只能产生状态 proposal，不能直接推进 Application reducer。
- `M4.8` 只在 CareerOps DB 创建 Reply Draft；保持 thread metadata，但不调用 Gmail Draft API。
- `M4.9` 运行 Policy，生成 approval request；UNKNOWN、OFFER、DOCUMENT_REQUEST、低信任、无 Application 关联全部禁止自动发送。
- `M4.10` 实现 Inbox Review、Draft diff、Approval Queue；此阶段“批准”只冻结 payload，不会发送。
- `M4.11` 实现重复/丢失 Pub/Sub delivery、Watch 过期、history expiration、OAuth revoke、malformed MIME、HTML XSS 和 Prompt Injection 测试。
- `M4.12` 实现 email retention/redaction/purge workflow；非招聘正文不得进入 DB、对象存储、trace 或模型日志。
- `M4.13` 实现附件 quarantine：声明 MIME 与 magic bytes 双校验、大小上限、杀毒、无脚本文本提取；只允许 `text/calendar`、普通文本和 PDF 元数据/纯文本，未知/宏/可执行/压缩/密码保护内容 deny。
- `M4.14` 将 `FollowUpWorkflow` 接入 Gmail/Application：到期时先检查新邮件和职位 active 状态；仍需跟进时只生成内部草稿并进入 Approval Queue，永不自动发送跟进。

**出口 Gate：**

- 只持有已批准的读取 scope；数据库和日志中没有明文 token。
- 只允许专用求职 Gmail；非招聘邮件正文不持久化、不送模型。
- 重复 Pub/Sub delivery 不重复创建 Message；丢通知可由 reconciliation 补齐；默认部署没有公网 webhook。
- Email classification Macro-F1 ≥ 0.92；高风险识别 recall = 1.00。
- 时间与时区 exact-normalized accuracy ≥ 0.98；歧义时 abstain/review。
- 系统没有 `gmail.send`/`gmail.compose` scope，也没有可调用发送 adapter。
- In production personal-use 配置完成跨 7 天 refresh/Watch 观察前，Gmail 长期集成不能进入 Release Qualification。

### M5A：通用外部副作用内核证明

**目标：** 8–12 工程日先用 fake provider 证明外部动作的有效一次语义，再接 Calendar/Gmail。

**任务：**

- `M5A.1` 完成 immutable payload、intent、policy、approval、outbox、attempt、receipt、audit schema 和 repository。
- `M5A.2` Policy 输入只读取可信业务状态与 evidence refs；不接受模型的“这是安全的”自述。
- `M5A.3` Approval 绑定 target + payload hash + policy version + expiration；编辑后旧 approval 失效。
- `M5A.4` Outbox publisher 使用 lease/heartbeat，有界重试；Policy deny、4xx validation 不重试。
- `M5A.5` Side-effect Worker 使用独立 DB role、独立网络权限和 provider credential handle。
- `M5A.6` 对未知结果先 reconcile，禁止盲目重试；API timeout 不等于 provider 未执行。
- `M5A.7` fake provider 支持 before-call crash、after-call-before-commit crash、timeout-with-success、duplicate receipt、revoke。
- `M5A.8` 每个已执行动作都可从 Approval 页面回放提案、差异、Policy、attempt、provider receipt 和 audit。

**出口 Gate：**

- 同一 idempotency key 并发提交 100 次只产生一个 provider effect。
- Worker 在 provider 成功后、数据库提交前崩溃，恢复时先 reconcile 且不重复执行。
- payload 任一字节、目标、时间或附件变化都会使 approval 失效。
- 没有 Policy Decision 和 Audit Event 的 provider effect = 0。

### M5B：Calendar 与面试排期

**目标：** 10–14 工程日交付 FreeBusy、候选时间，以及招聘方确认后由用户显式触发的安全事件创建；MVP 不存在无人值守 Calendar write。

**任务：**

- `M5B.1` 增量申请 `calendar.freebusy`，实现限定时间窗的 FreeBusy adapter。
- `M5B.2` 实现 IANA timezone 解析、DST、半小时/45 分钟时区、跨日和歧义缩写拒绝规则。
- `M5B.3` 实现 buffer、minimum notice、每日/每周上限、available windows 和 3 个候选 slot。
- `M5B.4` 实现 Schedule Proposal 与内部邮件草稿；proposal 绑定 slot、全部被检查 calendar IDs、规则版本、payload hash 和过期时间，多个候选时间必须审批。
- `M5B.5` 用户在 Integration Settings 中单独增量授权事件写 scope；scope 已存在也不代表可写，只有招聘方明确确认、M5A 授权链通过且用户点击“重新验证并创建”后才能创建 Calendar Event。
- `M5B.6` 首版事件只创建到专用 `CareerOps Interviews` 日历，不带外部 attendee；adapter 明确禁止发送更新并用 Google sandbox contract test 锁定行为。邀请能力不进入 MVP。
- `M5B.7` 使用稳定 reconciliation key、DB unique constraint 和 provider lookup 防重复事件。
- `M5B.8` 实现 Interviews UI、双时区展示、状态、会议链接和确定性岗位/证据摘要。
- `M5B.9` 测试 DST、CST 歧义、并发抢占、buffer、超上限、provider timeout、重复点击、reschedule 和外部竞态。
- `M5B.10` 用户点击后获取本地 per-calendar lease，对所有配置日历重新查询 FreeBusy，并按执行时刻重算 notice、buffer、每日/每周上限和 proposal version；任何变化使旧 payload/approval 失效。FreeBusy 结果在 insert 前超过 10 秒必须重查或 fail closed。
- `M5B.11` 写入后立即并延迟 reconcile；外部 Calendar 无原子 compare-and-insert。若并发外部写造成冲突，进入 `schedule_conflict_review`、告警并阻止任何面试确认邮件；系统不自动删除或修改事件。

**出口 Gate：**

- 时区/slot 解析准确率 ≥ 0.98；歧义请求不自动排期。
- 执行前可见的冲突、低于提前量、超过上限或缺 buffer 的事件创建 = 0。
- 招聘方确认前正式事件创建 = 0。
- 重复 Calendar Event = 0；provider timeout 后能 reconcile。
- 无人值守 Calendar Event 创建 = 0；每个 write 都有当前用户动作、Policy、Approval、Intent、Receipt 和 Audit。
- proposal 后、最后一次 FreeBusy 返回前插入的冲突会阻止写入；故意插在 FreeBusy 返回后、insert 前的不可消除竞态必须 100% 被 reconcile/告警、100% 进入人工队列且确认邮件发送 = 0。生产口径不宣称 Google Calendar 提供原子空闲占位。

### M6：Gmail Send 与受控 Auto-send，默认关闭

**目标：** 12–18 工程日实现可证明的发送能力；完成后仍不能默认自动发送。

**任务：**

- `M6.1` 增量申请 `gmail.send`；credential handle 只提供给专用 Side-effect Worker。
- `M6.2` 构造 RFC 2822 MIME 回复，验证 To/Reply-To/Subject/References/In-Reply-To，附件默认禁用。
- `M6.3` idempotency key 绑定 account + inbound message + intent + normalized body hash；DB unique constraint。
- `M6.4` 保存 provider message ID、thread ID、request fingerprint；先在 Gmail sandbox 验证稳定 RFC `Message-ID`/thread/body fingerprint 的可查询性，未知结果必须按已验证的 reconciliation strategy 查询 Sent/thread 后再决定是否重试。
- `M6.5` 实现 global kill switch、release qualification、account opt-in、policy allowlist 四层门。
- `M6.6` 可配置候选仅限投递确认、招聘联系收件确认、测评收件确认、已明确确认时间、无问题感谢；默认全部 off。
- `M6.7` scheduling options、follow-up、resume/link、work authorization、deadline commitment 必须人工审批。
- `M6.8` salary、offer、visa、relocation、tax、background check、identity/bank、withdrawal、unknown 永远不能 Auto-send；永久规则也不能覆盖。
- `M6.9` Approval UI 在发送前显示目标、完整正文、线程、附件、证据、Policy、diff 和过期时间。
- `M6.10` 执行 send 前/后 crash、重复任务、429/5xx、OAuth revoke、provider timeout、thread mismatch、payload tampering 测试。
- `M6.11` 定义 reconciliation window（默认 15 分钟）与状态机：窗口内按有界退避查询；仍不能证明 sent/not-sent 时转 `reconciliation_required`、停止自动重试并提示人工核对。人工操作也不能复用原 idempotency key 直接重发。

**出口 Gate：**

- 高风险未经审批发送 = 0；Auto-send false allow = 0。
- 在明确定义的故障矩阵中，confirmed duplicate send = 0：并发 intent、调用前崩溃、成功响应后崩溃、响应丢失但 reconciliation 可命中。
- reconciliation 无法定论的 intent 100% 进入 `reconciliation_required` 且不自动重试；生产报告分别展示 confirmed duplicate、prevented duplicate 和 ambiguous counts，不宣称跨 Gmail/数据库的 exactly-once 事务。
- Prompt Injection 无法改变 recipient、payload、Policy 或调用工具。
- 无来源 contact、低信任 sender、无 Application 关联不能 Auto-send。
- 代码能力完成，但生产 global kill switch 仍为 off。

### M7：安全、评测、恢复与发布门

**目标：** 15–20 工程日证明系统可以作为单用户自托管 v1 使用。

**任务：**

- `M7.1` 由独立 reviewer 核验 D0 manifest、双标/裁决/PII 证据并运行完整 held-out Golden Dataset；不得在看过结果后修改标签、mock 或 fixture。
- `M7.2` 完成 SSRF、DNS rebinding、redirect、XSS/CSP、CSRF、session、OAuth state/audience/revoke、prompt injection、附件和 secret scanning 安全套件。
- `M7.3` 完成 Gmail/Calendar sandbox E2E：职位 → 匹配 → Application → 模拟邮件 → 草稿/审批 → 收到时间确认 → 用户重新验证并创建 Calendar → post-write green receipt → 发送确认。
- `M7.4` 完成 Temporal、Worker、PostgreSQL、Redis、对象存储、网络和 provider fault injection。
- `M7.5` 实现并验证 RPO ≤ 5 分钟、单节点 RTO ≤ 30 分钟；执行数据库、对象存储、配置和 credential reference 恢复演练。
- `M7.6` 配置 crawl、match、email、calendar、outbox、workflow、OAuth、watch、reconciliation metrics 和 alerts。
- `M7.7` 编写 `docs/runbooks/`：安装、升级、OAuth、备份恢复、Watch 恢复、stuck intent、duplicate suspicion、kill switch、撤销集成。
- `M7.8` 完成 Docker Compose 单机拓扑、网络隔离、只暴露经认证的 API/UI、不暴露 webhook、资源限制和升级演练。
- `M7.9` 生成不可变 Release Qualification 记录，包含 commit SHA、migration head、dataset versions、测试结果和有效期。
- `M7.10` 只有 qualification 有效时，用户才能在 UI 对单个账户显式开启允许的低风险 Auto-send；开启/关闭均写审计。
- `M7.11` 对 raw documents、邮件、模型 I/O、临时附件和 audit 运行 retention/purge 演练；验证删除符合策略、长期证据仍可回放、备份中也没有已过期明文数据。

**出口 Gate：**

- 第 10 节全部指标达标。
- D0 九类数据的 manifest、agreement、独立 reviewer 和 sealed holdout 证据齐全。
- 完整 E2E、security、chaos、backup/restore、OAuth revoke 演练通过。
- known critical/high security findings = 0。
- 所有 provider writes 的 Policy/Audit coverage = 100%。
- 第 10 节定义的安全事故计数在其故障矩阵/受控并发口径内全部为 0；所有 ambiguous effect 100% 停止自动重试并进入人工队列。
- Release Qualification 失效、部署版本变化或 migration 变化会自动关闭 Auto-send。

## 10. 指标与验收合同

M-1 必须将以下公式写入 `docs/evaluation/metric-contracts.md`，实现到 `src/careerops/evaluation/`，结果写入带 commit/dataset/version 的 JSON artifact。

| 指标 | 公式/口径 | 门槛 | Gate |
| --- | --- | --- | --- |
| Careers 入口识别 | 在有官方入口的 seed companies 上 recall | ≥ 0.90 | M1/M7 |
| ATS 字段解析 | required fields 的 field-level micro accuracy；缺失真值不计分 | ≥ 0.95 | M1/M7 |
| 跨来源去重 | 人工标注 posting pairs 上 pairwise precision/recall/F1 | F1 ≥ 0.98，precision ≥ 0.99 | M1/M7 |
| Remote 分类 | 类别 macro F1 + China Eligible precision/recall | 均 ≥ 0.95 | M2/M7 |
| 地域硬门误放 | 明确不可用样本被标记 eligible/apply_now | 0 | M2/M7 |
| Contact | precision、evidence coverage、employee false positive、guessed count | precision ≥ 0.98；其余分别 1.00、0、0 | M3/M7 |
| Match 证据 | strong match 中无 evidence 的 requirement | 0 | M2/M7 |
| Email 分类 | frozen bilingual email set 上 Macro-F1 | ≥ 0.92 | M4/M7 |
| 高风险识别 | OFFER/document/salary/visa/suspicious 等 recall | 1.00 | M4/M7 |
| 时间/时区 | 归一到 UTC + source timezone 后 exact match；正确 abstain 计正确 | ≥ 0.98 | M4/M5B/M7 |
| 副作用覆盖 | provider writes 中有 valid Policy Decision + Audit + Receipt 的比例 | 1.00 | M5A–M7 |
| 安全事故计数 | 未批高风险发送、故障矩阵内 confirmed duplicate、执行前已知冲突仍写日程、无人值守日程、无证据自动发送、prompt tool effect | 全部 0 | M5B–M7 |
| Calendar 外部竞态处置 | 故意在最后 FreeBusy 返回后、insert 前注入冲突时的检测率、人工队列率、确认邮件数 | detection = 1.00；queue = 1.00；confirmation send = 0 | M5B/M7 |
| 未决副作用 | reconciliation window 后仍 ambiguous 的动作 | 100% 停止自动重试并进入人工队列；单独计数告警 | M5A–M7 |

建议评测命令接口：

```bash
uv run python -m careerops.evaluation.discovery --dataset <version>
uv run python -m careerops.evaluation.dedup --dataset <version>
uv run python -m careerops.evaluation.remote --dataset <version>
uv run python -m careerops.evaluation.matching --dataset <version>
uv run python -m careerops.evaluation.contacts --dataset <version>
uv run python -m careerops.evaluation.email --dataset <version>
uv run python -m careerops.evaluation.policy --dataset <version>
uv run python -m careerops.evaluation.calendar --dataset <version>
```

每套数据按实体分组切分为 development、validation 与 sealed holdout。修复实现时允许更新错误的代码、prompt 或规则；不得为了让测试通过而静默改 holdout 标签、mock response 或 fixture。

## 11. 测试与验证策略

### 11.1 每个 PR 的基础 Gate

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run bandit -r src
uv run pytest tests/unit
uv run pytest tests/contract
```

### 11.2 合并到里程碑前

- Alembic upgrade/downgrade/re-upgrade。
- PostgreSQL、Redis、Temporal Testcontainers 集成测试。
- Adapter frozen response contract tests。
- Workflow replay 与 Worker restart tests。
- API auth、CSRF、OpenAPI schema 与 error contract tests。
- UI HTML sanitizer、CSP 和关键审批路径浏览器测试。

### 11.3 连接真实 provider 前

- 先用 purpose-built fake provider 验证状态机、重试和 crash semantics。
- 再用专用 Gmail/Calendar sandbox 运行最小 smoke。
- 不使用私人主邮箱作为开发测试账户。
- 外部发送只发往受控测试收件箱；Calendar 只写测试日历。

### 11.4 M7

- Held-out evaluation。
- 全链路 E2E。
- Security/adversarial suite。
- Chaos/reconciliation suite。
- Backup/restore drill。
- Manual approval UX walkthrough。
- 24–72 小时 soak：Watch renewal、history reconciliation、outbox、workflow stuck alerts。

## 12. REST 与 UI 交付规则

- API 使用 `/api/v1`，错误统一为 machine code、message、details、trace_id；不得把 provider error/secret 原样透传。
- 列表从第一版就使用 cursor pagination；过滤条件显式 allowlist，避免后续破坏兼容性。
- Mutation 要求 idempotency key 或资源 version；审批和状态更新使用 optimistic concurrency。
- OpenAPI 是合同，不从 ORM 自动泄露内部字段、credential reference 或 raw untrusted payload。
- 每个领域能力同里程碑交付最小 UI，不把完整 Web Console 堆到最后：
  - M0：Dashboard shell、health、integrations status。
  - M1：Companies、Jobs Inbox、Job Detail。
  - M2：Remote evidence、Match Report。
  - M3：Contacts、Application Pipeline。
  - M4：Inbox Review、Draft diff、Approvals。
  - M5B：Interviews、双时区 slot。
  - M6/M7：Send audit、release qualification、kill switch。

## 13. 风险与缓解

| 风险 | 触发信号 | 缓解 |
| --- | --- | --- |
| Google OAuth 模式偏离已关闭边界 | OAuth Testing 被用于长期运行、共享 client、主邮箱、公开分发 | 非开发环境 fail closed；仅 BYO In production personal-use + 专用 Gmail + pull；扩大分发即重新进入 verification/security assessment Gate |
| “Exactly once” 被错误宣传 | provider timeout 后无法判断是否执行 | 对外承诺 effectively-once：唯一 intent + provider fingerprint + reconcile；未知状态不盲重试 |
| 跨来源错误合并 | 相似标题/地点被自动合并 | canonical/posting 两层模型；高 precision 门；人工 split/rollback；语义相似只提议 |
| 数据集晚建或标注质量不足 | 功能完成后才找样本、无第二复核者、agreement 不达标 | D0 预算 20–35 人日；10% pilot；安全关键 100% 双标；sealed holdout 未签字则里程碑不通过 |
| 模型隐私或升级漂移 | provider 未资格化、敏感数据进入 egress、provider/model/prompt 版本变化 | 默认 disabled；最小脱敏 allowlist；egress test；模型不能决定 Policy；版本变化使 qualification 失效 |
| 邮件/网页 Prompt Injection | 外部文本含工具/系统指令 | 无 tool binding、untrusted envelope、Schema validation、Policy 使用可信状态、对抗集 |
| 私人主邮箱被误接入 | 配置接受非专用账户或 label mode | MVP 不实现 label mode；安装与运行时要求专用账户；非招聘正文过滤、审计和删除作为纵深防御 |
| Calendar API 隐式发送邀请 | 事件带 attendees 或 adapter 默认通知 | 只写专用日历、不带 attendee、明确禁止更新通知、sandbox contract test；邀请能力不进入 MVP |
| Calendar FreeBusy 与 insert 之间 TOCTOU | proposal 后用户新增日程 | 永久 human-trigger；fresh preflight + 本地 lease + 规则重算；写后 reconcile；竞态时停确认邮件并入人工队列，不宣称原子占位 |
| Temporal 代码升级无法 replay | workflow 中加入非确定逻辑 | I/O in Activity、version marker、replay CI、部署 runbook |
| UI 成为第二个大项目 | SPA 状态、构建和组件系统膨胀 | MVP server-rendered thin UI；无独立前端状态层；复杂编辑后置 |
| 本地对象存储丢失或备份不一致 | DB 恢复后 snapshot ref 失效 | content-addressed volume、同一备份清单、hash verify、M7 restore drill |
| 低风险 Auto-send 误配置 | config true 或旧 qualification | 默认 off、独立 release capability、账户 opt-in、Policy allow 四重门；任何版本变化自动关 |

## 14. Pre-mortem

### 失败场景 1：Worker 在 Gmail 已发送后崩溃，恢复时重复发送

- 根因：把 API timeout/DB rollback 等同于“没有发送”。
- 预防：immutable intent、唯一 idempotency key、稳定 RFC message fingerprint、provider receipt、Sent/thread reconciliation。
- 证明：M5A fake provider 与 M6 Gmail sandbox 都执行 after-call-before-commit crash；最终 provider effect 必须为 1。

### 失败场景 2：邮件中的 Prompt Injection 诱导系统改收件人或绕过审批

- 根因：模型同时拥有不可信内容和工具权限，Policy 又相信模型自述。
- 预防：分类/草稿模型无工具、输出 Schema；target 由可信 thread metadata 构造；Policy 只读可信状态；Side-effect Worker 校验 payload hash。
- 证明：对抗邮件集覆盖“忽略系统指令、改发到其他地址、上传 secret、点击未知站点”等样本，tool effect 和 false allow 必须为 0。

### 失败场景 3：部署者把私人主邮箱接入 MVP，导致读取面超过预期

- 根因：误把 label filter 当成 OAuth scope 隔离；`gmail.readonly` 实际可看到整个邮箱。
- 预防：MVP 强制专用求职 Gmail，不实现 label mode；非招聘正文过滤、数据导出/删除/审计仍作为纵深防御。
- 证明：非开发配置尝试启用 label mode 或主邮箱声明时 integration not-ready；混合测试消息中的非招聘正文在 DB、object store、trace、model log 中均不存在。

## 15. 建议的首批四个 PR

### PR 1：冻结执行合同

- 只添加 M-1 的 ADR、risk-closure decisions、metric contracts、threat model、dataset schemas、D0 labeling guides/owner/pilot manifest。
- 不添加业务代码。
- Exit：所有范围裁决和指标公式可审阅。

### PR 2：可启动工程骨架

- `pyproject.toml`、锁文件、FastAPI health、配置、Compose、CI、Postgres/Redis/Temporal 连接。
- Exit：全新环境一条命令启动，CI 全绿。

### PR 3：安全内核与第一版迁移

- canonical job/posting、immutable payload、intent/policy/approval/outbox/audit、DB append-only 权限、Temporal recovery smoke。
- Exit：未知副作用被拒绝；kill/restart 不重复完成。

### PR 4：第一条只读纵向切片

- 手工添加 Company → Greenhouse adapter → normalize → posting/version → Jobs API/UI。
- 同时加入 fixture contract、来源/hash 展示和重复抓取测试。
- Exit：从真实公开 Greenhouse board 读取并在 UI 展示职位；无浏览器、无邮件、无外部写动作。

每个 PR 的 commit message 遵循仓库 Lore Commit Protocol，至少记录 intent、constraints、confidence、scope risk、tested 和 not-tested。

## 16. 完成定义

CareerOps Agent v1 只有同时满足以下条件才完成：

- M-1–M7 Gate 全部通过且有 commit/dataset/version 证据。
- Spec 最终 15 条 DoD 全部可通过测试或演练证明；更广 Spec 中的生成式 Prep Pack 与 MCP 按第 3 节明确后置。
- 所有外部副作用均可回放 Action Intent、Payload、Policy、Approval、Attempt、Receipt、Audit。
- 第 10 节限定口径内的安全计数器为 0，critical/high finding 为 0；所有未决发送/日程状态均停止自动重试并进入人工处置。
- D0 的 20–35 人日、独立 reviewer 与九类 sealed holdout 已真实完成，不能用 synthetic-only 或工程 Fixture 替代。
- 新机器恢复演练成功，RPO/RTO 达标，OAuth revoke 后 Worker 停止。
- Auto-send 默认关闭；只有有效 Release Qualification 和用户显式 opt-in 才可打开。
- README、架构、威胁模型、运维 runbook、备份恢复和评测报告足以让另一个工程师复现。

## 17. 停止与升级规则

- 任一里程碑 Gate 未通过，不进入依赖它的下一阶段。
- MVP 永不连接私人主 Gmail；M4 只连接专用求职 Gmail；M5A 前不调用任何真实 write provider；M7 前不打开 Auto-send。
- OAuth Testing、共享 client、主邮箱标签模式、公网 webhook、未资格化模型或无人值守 Calendar write 任一出现，立即停止对应 integration/release。
- 遇到 provider 行为不确定时，系统停在 `unknown/reconciliation_required`，不通过重试“碰碰运气”。
- 任何扩大 OAuth scope、允许新附件、增加自动发送类别、启用 Browser Worker 或开放 MCP mutation 的变更，都必须新增 ADR、威胁模型差异和对应安全 Gate。
- 本计划完成后应进入实现工作流；规划任务本身不自动修改业务源码。
