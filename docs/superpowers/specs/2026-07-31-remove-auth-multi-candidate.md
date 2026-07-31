# 移除控制台登录 + 多 candidate 路径参数化

## 目标

CareerOps 是本地、loopback 单进程工具，却背了一套 SaaS 级控制台登录（bootstrap token / 密码 / session / CSRF / Redis 限流 / auth 审计）。这套"管你自己"的护栏对本地工具是纯负担。本变更：

1. **彻底删除控制台登录**（后端 auth 子系统 + 数据表 + 前端登录页）。
2. **`candidate` 升为顶级业务实体**——本地可能适配多个人（多份简历），每个 candidate 一套独立环境（抓取计划 / Gmail / 投递 / 收件箱）。
3. **"当前 candidate"由前端 selector 决定**，通过 **URL 路径参数** `/api/v1/candidates/{candidate_id}/...` 传递；切换 candidate 时整套环境跟随切换。
4. **不鉴权**：本地工具，API 在 loopback 下信任调用方，不再校验 session/CSRF。

## 现状（为什么工作量可控）

- `candidates` 表已存在（`id` + `display_name` + 时间戳），`Candidate` domain model 已有。
- 数据**已经 per-candidate**：evidence / match_results / mail_account / application / inbox / agent_contexts / agent_runs 全按 `candidate_id` 范围。
- crawl 子系统用 `owner_id`，但它**实际就是 candidate_id**（同一个人，命名不同）。`CrawlPlanService` 全部按 `owner_id` 范围。
- 约 **20+ 个路由**通过 `require_candidate_id` 依赖取 candidate_id（目前从 session principal 解析）。
- `require_api_auth` 已有"未配 auth 即放行"分支。

**结论：不动 schema、不动 domain model。** 改的是"当前 candidate 的来源"（session → 路径参数）和登录层的去留。

## 设计

### A. 删除控制台登录（后端）

删：
- `src/careerops/auth/`（service / contracts / rate_limit）
- `src/careerops/infrastructure/auth.py`（`create_console_auth_service` + Argon2 + PostgresAuthRepository + FixedWindowAuthRateLimiter + PostgresAuthAuditSink）
- `src/careerops/web/api_auth.py`（preauth / login / bootstrap / logout / session 路由）
- `src/careerops/cli/auth.py`（`careerops-bootstrap` CLI）
- auth 路由注册（`/api/v1/auth/*`）
- `console_users` / `auth_sessions` 数据表 + 建表迁移（配套删迁移或新增 drop 迁移）
- Redis 用于 auth 限流的装配
- 6 个 `tests/unit/test_api_auth_*.py` + 相关 fixtures

改：
- `create_app`：去掉 `auth_service` / `web_settings` 装配。
- `api/auth_dependency.py`：删 `require_api_auth` / `require_web_auth` / `AuthenticatedPrincipal` 依赖；`require_candidate_id` 改为从**路径参数**解析（见 C）。`reject_candidate_substitution` 删除（本地无鉴权、无恶意替换场景）。

### B. candidate 顶级实体 + CRUD

新增/补全 candidate 管理端点（全局，不带 candidate_id 路径）：
- `GET /api/v1/candidates` — 列出所有 candidate。
- `POST /api/v1/candidates` — 新建（body: `display_name`）。
- `GET /api/v1/candidates/{candidate_id}` — 详情。

复用已有 `candidates` 表 + repository；如缺则补一个 `CandidateService` + `PostgresCandidateRepository`（薄）。

### C. 路径参数化（核心）

**数据分层先分清（决定哪些路由路径化）：**

- **全局共享数据**（无 `candidate_id`，所有人共用）：`companies`、`canonical_jobs`、`job_postings`、`job_posting_versions`、`job_sources`（招聘来源定义）。职位库/公司库是全局知识。
- **per-candidate 数据**（带 `candidate_id` / crawl 的 `owner_id`）：`profiles` / `evidence`（简历、技能）、`crawl_plan_versions`（抓取计划）、`applications`（投递）、mail accounts/threads、inbox（匹配收件箱）、`match_results`、`agent_contexts`/`agent_runs`。

只把 **per-candidate 路由**迁移到 `/api/v1/candidates/{candidate_id}/...`：

| 现在 | 之后 |
|---|---|
| `GET /api/v1/crawl/plans` | `GET /api/v1/candidates/{cid}/crawl/plans` |
| `POST /api/v1/crawl/runs` | `POST /api/v1/candidates/{cid}/crawl/runs` |
| `GET /api/v1/applications` | `GET /api/v1/candidates/{cid}/applications` |
| `GET /api/v1/inbox` | `GET /api/v1/candidates/{cid}/inbox` |
| `GET /api/v1/mail/...` | `GET /api/v1/candidates/{cid}/mail/...` |
| `GET /api/v1/matching/...` | `GET /api/v1/candidates/{cid}/matching/...` |
| ……（per-candidate 路由，约 18 个） | |

身份解析：把 `require_candidate_id` 改写为从路径取 `candidate_id`（FastAPI 路径参数），不再读 principal。crawl 子系统仍用 `owner_id` 命名——**API 层把路径取到的 `candidate_id` 当 `owner_id` 传给 crawl service，不改 service 签名**（`owner_id` 重命名留作后续清理）。

**全局路由不动（不路径化）**：`/api/v1/jobs`、`/api/v1/companies`、`/api/v1/health/*`、`/api/v1/metrics`、`/api/v1/candidates`（列表/新建）、`/api/v1/openapi.json`、review 端点。jobs/companies 是全局职位库/公司库（已核实：`jobs.py` 路由不依赖 `require_candidate_id`、不过滤 candidate）。

### D. 前端

删：`views/Login.vue`、`views/Bootstrap.vue`、router 登录守卫、`stores/session.js` 里的 login/bootstrap/preauth/logout 逻辑、`api/client.js` 的 CSRF token 处理、`tests/session.test.js` 相关。

加：
- **candidate selector**（顶部下拉，列出所有 candidate + 切换 + "新建候选人"入口）。
- 切换 candidate 后，所有后续 API 请求 URL 带上选中的 `candidate_id`（前端 store 持有当前 candidate，API client 拼 URL）。
- 启动时：若只有 0 个 candidate → 引导新建；若有 → 默认选第一个。

### E. 不在范围

- **schema / domain model 不动**（`candidates` 表、owner_id/candidate_id 列原样保留）。
- **agent 护栏全留**：能力开关、A/B 双模型审批、outbox、审计哈希链、Tier 2 预算、crawl 权限。
- **crawl 的 `owner_id` 不重命名**（API 层适配；统一命名留后续）。
- `/api/v1/auth/session` 端点删除（前端不再需要；当前 candidate 由 `GET /api/v1/candidates` + 前端 selector 决定）。

### F. 后台 worker 的多 candidate 化（关键设计章节）

后台活动（Temporal worker）没有 HTTP 请求、没有路径参数——candidate 上下文必须由后台循环自身遍历提供。多 candidate 下，所有 per-candidate 的后台活动都要从"单一隐含用户"改成"遍历每个 candidate"：

- **mail-sync（per-candidate）**：每个 candidate 有独立 Gmail 账号。`loop_bootstrap` 要为**每个** connected mail account 各注册一个 schedule（`schedule_id = mail-sync:{candidate_id}`，arg 带各自的 account_id/candidate_id）。Phase 2 现在只注册一个固定 account——改成枚举所有 candidate 的 mail account。
- **crawl（计划 per-candidate，结果全局共享）**：`loop_bootstrap` 遍历所有 candidate，逐个对其 crawl plan 跑 `CrawlActivationService.activate_crawl_schedules`（每个 candidate 一套 schedule，`schedule_id = crawl:{candidate_id}:{source_id}`）。**注意：抓取结果（postings）经 `ingest_posting` 去重进全局 `canonical_jobs`，是共享的**——各 candidate 的 plan 只决定"她关注哪些来源/关键词"，抓到的职位进同一职位库，不重复抓同一批公司/职位。Phase 2 现在的 `_resolve_owner_id` 取"唯一 owner"——改成枚举所有 candidate。
- **outbox-drain / approval-sweep（保持全局）**：不 per-candidate，全局一个 schedule 即可。但 drain 触发的发信 side-effect 必须能用**对应 candidate 的** Gmail 账号/token——依赖 `action_intent` 携带 candidate 上下文（迁移时核实，必要时在 side-effect provider 侧按 candidate 解析账号）。
- **matching / inbox 投影**：crawl 完成链里按该次 run 的 candidate_id（=owner_id）投影，本来就 per-candidate（`CrawlDownstreamService` 按 owner 范围），无需改。
- **Tier 2 预算（多 candidate 共享，v1 先到先得）**：`PostgresTier2Budget` 是全局的（并发槽 / 每日浏览器动作预算，跨所有 worker 共享），多 candidate 消费同一份。v1 采用 **全局共享、先到先得**，不按 candidate 配额——含义：一个 candidate 的高频抓取理论上可能挤占其他 candidate 的预算。对单用户、少量 candidate（预期使用场景）够用；若未来要公平，再引入 per-candidate 配额（明确留作后续，不阻塞本次）。

> 已验证：crawl 路由把 `require_candidate_id` 返回的 `candidate_id` 直接当 `owner_id` 传给 `CrawlPlanService`/`CrawlRunService`（`crawl_plans.py:339+`），故 owner_id 与 candidate_id 是同一值，切换 candidate 时 crawl 数据正确跟随。

### G. review 端点与 capability 的适配（第三轮核实结论）

- **`require_capability` 不依赖登录**（已核实 `capability_dependency.py:65`）：只读 `app.state.capability_resolver`（settings flags），不读 principal / session / authenticated。删登录后照常工作，**无需适配**。能力开关、A/B 审批、outbox 等 agent 护栏全部不受影响。
- **`capability_resolver.for_send_batch` 的 `authenticated=True` 是硬编码**，不从 principal 读——删登录后行为不变（始终信任），仅语义弱化（本地 loopback 工具可接受，不视为风险）。
- **review 端点依赖登录，必须适配**（已核实 `api/routes/review.py` / `orchestration/review.py`）：`ReviewAuthService` 用 `authenticate(session_token)` + `validate_csrf` 确定审批者。删登录后这条链断——适配方案：去掉 review 端点的 session/CSRF 校验，审批者 actor 改为一个本地固定值（如 `local-reviewer`）；审批动作仍写审计哈希链，`actor_type=USER` 的 actor 字段用该固定值（与"审计 actor"风险点一并处理）。review 是 A/B 自动审批的人审兜底，本地无鉴权下信任 loopback 调用。
- **现有数据归属无需迁移**（已核实 `schema.py:2612`）：`crawl_plan_versions.owner_id` 是 `ForeignKey→candidates.id`（ondelete CASCADE），注释确认"semantically identical to candidate_id"。现有 crawl plan 天然归属已存在的 candidate，删 `console_users` 不影响（candidate 独立，无反向 FK）。迁移后 selector 列出已有 candidate、选中即看到其已有数据。

## 实施分阶段（粗略，细节交 writing-plans）

1. **后端 candidate 顶级 + 路径依赖**：candidate CRUD 端点；把 `require_candidate_id` 改为路径参数解析；保留旧路由暂时并行（deprecation）。
2. **逐 router 路径迁移**：按 router 批次把 per-candidate 路由迁到 `/candidates/{cid}/...`，每批跑测试。
3. **删登录层**：删 auth 模块 / 路由 / 表 / CLI / 测试 / 前端登录页。
4. **前端切换器**：candidate selector + API client 全改 URL + 启动引导。
5. **清理**：删 `reject_candidate_substitution`、过渡并行路由、owner_id 别名注释。

## 风险

- **20+ 路由路径迁移 + 前端 API client 联动**：是迄今最大改动，须分批 + 每批测试。
- **测试面广（被低估过）**：`require_candidate_id` 的 27 个调用者对应的全部契约/安全测试（section3/4/10/12/13……）URL 都要改，体量可能超过路由迁移本身。
- **outbox 投递的 per-candidate 账号解析**：全局 drain 触发的发信必须用对应 candidate 的 Gmail 账号——需核实 `action_intent` 是否携带足够 candidate 上下文（见 F 节），不足则要补。
- **审计 actor 无登录后来源**：人工操作（审批/手动改）的 `actor_type=USER` 原来来自登录用户；删登录后需定一个本地 actor 来源（如"当前 candidate"或固定 `local`），否则审计哈希链的 actor 字段空缺。
- **路由并行过渡 vs 一次迁移**：阶段 1 的"旧路由并行"会让 OpenAPI/前端/测试在两套 URL 间混乱；倾向于不分并行、按 router 批次一次性迁。
- ~~crawl owner_id 等价假设~~ **已验证**（见 F 节末）：owner_id = candidate_id，切换安全。
- ~~后台触发循环~~ **已升级为设计章节 F**（遍历所有 candidate）。
