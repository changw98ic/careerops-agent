# CareerOps 交接文档

**版本**: v1.2 | **日期**: 2026-07-24 | **状态**: 工作树 19 modified + 2 untracked，`make verify` 1227 passed / 32 skipped

---

## 一、系统概述

CareerOps 是一个安全优先的单用户求职运营助理，核心能力：

1. **职位采集** — 从 ATS（Greenhouse/Lever/Ashby）+ 公司官网 + X/Reddit 采集职位
2. **简历分析** — 确定性技能提取 + LLM 匹配（当前 disabled，ADR 0006 门控）
3. **邮件沟通** — 投递草稿 + 面试邀请识别（stub）
4. **LangGraph 编排** — 9 节点状态图 + human-in-the-loop 审核

## 二、架构总览

```
crawl → extract_contacts → resume → filter → dedup → match → draft → review_gate ─→ send → END
                                                                               └→ END (reject)
```

| 层 | 技术 | 状态 |
|---|---|---|
| Web/API | FastAPI + Jinja2 + HTMX | ✅ |
| 工作流编排 | Temporal（持久化后台任务） | ✅ 已通电（M1 workflows + RealCrawlActivitySink） |
| Agent 编排 | LangGraph（状态图 + HITL） | ✅ 9 节点 StateGraph + review_gate interrupt |
| 数据 | PostgreSQL（业务）+ Redis（缓存/限流） | ✅ |
| LLM 网关 | `StructuredModelClient`（ADR 0006） | ✅ 用户配置 endpoint 即可启用（qualification gate 已放宽） |
| 可观测性 | Prometheus metrics | ✅ 运维 + 业务指标 |

## 三、已完成工作（按 commit 顺序）

| Commit | 内容 | 关键文件 |
|---|---|---|
| `5a90f48` | M1-M7 基础 spec | `docs/multi-agent-system-spec.md` |
| `bc7e572` | Stage 0: adapter detail parser, http_fetcher, scripts→src, schema 校验 | `adapters/`, `model_gateway/`, `application/` |
| `a7b9268` | AshbyDetailAdapter | `adapters/job_sources.py` |
| `0e5d4b5` | crawl_jobs 接 adapter | `scripts/crawl_jobs.py` |
| `2542b1b` | Stage 1: LangGraph StateGraph + review_gate HITL | `orchestration/{state,nodes,graph,kernel_adapter,mapping_store}` |
| `dd1d1f4` | Stage 2+3+3.5: conversions, filter, review endpoint, circuit breaker, 429 退避 | `orchestration/conversions.py`, `api/routes/review.py` |
| `f7c1fe3` | Stage 4: kernel Postgres + PostgresSaver + Temporal M1 + GmailProvider | `infrastructure/database/side_effect_postgres.py`, `infrastructure/temporal/m1_crawl_sink.py` |
| `58b6d89` | Stage 4 修: PostgresSaver startup, PRODUCTION guard, Temporal sink, filter reducer | fix files |
| `9347388` | v1.1: kernel 并发, Temporal M1 真实 ingest, gmail durable, Postgres 验证 | kernel atomic APIs, `gmail_receipt_postgres.py`, `migration 0012` |
| `2da0565` | SSRF + OAuth AES-GCM + factory gate + API auth + 集成测试修复 | security fixes |
| `b3d5f1c` | CDE: crawl_full JsonLdAdapter + 语义去重 SimHash + 质量指标 | `crawl_full.py`, `orchestration/dedup.py`, metrics wiring |
| *(working tree)* | v1.2: outbox drain + sweeper 通电, engine SSRF, http_fetcher 接入 crawl_jobs, LLM config 用户配置化, crawl_full SSRF, email_apply 走 kernel, Lever slug 清理, API repo wiring, 198.18 SSRF 放行 | 见下方详情 |

## 四、LangGraph 图结构（当前）

```
crawl → extract_contacts → resume → filter → dedup → match → draft → review_gate ─→ send
  │          │              │        │        │       │        │         │                │
  │        提取联系人    简历技能  过滤JD  SimHash去重  LLM匹配  生成草稿  HITL审批      kernel.execute
  │                                              (disabled)   interrupt()          → provider
  │                                                                │
  │                                                            approve/reject/edit
  │                                                                ↓
  │                                                         Command(goto="send"/"draft"/END)
```

**节点实现状态**：

| 节点 | 实现 | 依赖 |
|---|---|---|
| `crawl_node` | `AdapterCrawler`（http_fetcher + JobSourceAdapter + detail） | 真实 ATS API + ego-browser |
| `extract_contacts_node` | `contact_extraction.extract_from_file` | 真实 |
| `resume_node` | `resume_analysis.build_skill_profile` | 真实 |
| `filter_node` | `filter_jobs` 纯函数 | description覆盖 + remote/direction/region/salary |
| `dedup_node` | `dedup_jobs` SimHash | 纯函数 |
| `match_node` | `LLMJobMatcher`（默认 **DisabledModelAdapter**；配置 `model_provider` + `model_base_url/api_key/name` 后可用真 LLM） | 真实，用户配置 |
| `draft_node` | `email_drafting.generate_body` | 真实 |
| `review_gate` | `kernel_adapter.review_gate`（interrupt + atomic approval） | side_effect_kernel |
| `send_node` | `kernel.execute`（**FakeProvider**，v1 不真发） | side_effect_kernel |

**HITL 机制**：`review_gate` 用 `interrupt()` 暂停图，人工通过 `POST /api/v1/review/{approval_id}` 下发 `Command(resume=...)` 恢复。approval 创建幂等（`idempotency_key` + `get_or_create_pending_approval`）。

## 五、Side-Effect Kernel（安全核心）

```
propose(policy) → request_approval(approval) → interrupt() → [人工审核] → approve → execute → provider
```

**关键文件**：
- `application/side_effect_kernel.py` — kernel 核心（propose/approve/execute/replay）
- `infrastructure/database/side_effect_postgres.py` — Postgres 实现（SELECT FOR UPDATE 原子 get_or_create）
- `infrastructure/database/side_effect_memory.py` — InMemory 实现（v1 demo）
- `policy/side_effect_policy.py` — policy engine（capability_released + target_allowlisted + evidence_refs fail-closed）

**安全不变量**（ADR 0006）：
- LLM 不绑 tools、输出 `review_only=True`、`untrusted_claims={}`
- `model_provider` 默认 `disabled`；用户配置 `model_base_url`/`model_api_key`/`model_name` 后可启用（qualification gate 已放宽为用户自行配置，见 ADR 0006 addendum）
- PRODUCTION 禁挂 review endpoint（无 durable 不开）

## 六、验证状态

```
make verify     : 1227 passed, 32 skipped, pyright 0, ruff clean, coverage 82%
make verify-db  : 34 passed, 0 failed, 4 skipped (env-dependent)
```

**跳过的测试**：
- DB 集成测试需 `CAREEROPS_TEST_DATABASE_URL`（Postgres）
- `test_temporal_recovery` 需 Temporal SDK test-server binary

## 七、未完成事项（按优先级）

### P0 — 影响生产可用性

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 1 | ~~**outbox `publish_batch` 无调用点**~~ | `worker.py` | **已完成** — `main()` 现在创建 `OutboxPublisher` + `OutboxDrainActivities`，通过 Temporal activity 定时 drain。 |
| 2 | ~~**approval 过期 sweeper 不存在**~~ | `worker.py` | **已完成** — `main()` 现在创建 `ApprovalSweeperActivities`，通过 Temporal activity 定时扫描过期 PENDING → EXPIRED。 |
| 3 | ~~**`email_apply.py` 直接调 GmailSender**~~ | `scripts/email_apply.py` | **已完成** — `cmd_send` 现在通过 `SideEffectKernel` → `GmailSideEffectProvider` 走完整 policy/approval 链。 |

### P1 — 影响功能完整性

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 4 | ~~**`crawl_jobs.py` 未接入 `http_fetcher`**~~ | `scripts/crawl_jobs.py` | **已完成** — `fetch_json` 改用 `http_fetcher.fetch`，SSRF + circuit breaker + rate limit 生效。同时接入 `RealCrawlActivitySink` 做 DB 直写。 |
| 5 | ~~**`crawl_full.py` ego-browser HTTP 无 SSRF**~~ | `scripts/crawl_full.py` | **已完成** — 新增 `_is_safe_url()` 预检（scheme + private IP + metadata endpoint），`ego_save_page` / `ego_save_raw_html` 调用前校验。 |
| 6 | ~~**`test_temporal_recovery.py` Protocol 匹配**~~ | `tests/integration/test_temporal_recovery.py` | **已完成** — `CountingSink` 现在显式继承 `SmokeActivitySink`，pyright 0 errors。 |

### P1.5 — 本轮新增

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 11 | **`memory_repos.py` 无测试覆盖** | `infrastructure/memory_repos.py` | 新增的 InMemory repo 实现（matching/jobs/contacts/applications）覆盖率 36%，需补单元测试。 |
| 12 | **`internal_event_sink.py` 无测试覆盖** | `infrastructure/temporal/internal_event_sink.py` | 新增的 LoggingInternalEventSink 覆盖率 24%，需补单元测试。 |
| 13 | **`api/app.py` Protocol adapter 缺乏泛型** | `api/app.py` | `_ResumeRepoAdapter` 等 wrapper 用具体类型硬编码，如果 InMemoryApplicationRepository 接口变化需同步改。 |
| 14 | **`198.18.0.0/15` SSRF 放行** | `adapters/http_fetcher.py` | RFC 2544 benchmarking 段不再阻断（本地 DNS proxy / VPN 常见），但扩大了攻击面。 |

### P2 — 技术债

| # | 问题 | 说明 |
|---|---|---|
| 7 | **`anthropic_compat.py` pyright suppress** | 文件头 `# pyright: reportUnknownMemberType=false...`（JSON 边界）。evaluated at runtime, not compile time. |
| 8 | **`adapters/job_sources.py` pyright suppress** | 同上，`# pyright: reportUnknownVariableType=false...` |
| 9 | **0/8057 vs spec 数字不一致** | spec 仍写 0/8057，fixture manifest 用 `adapter_manifest.json` 固化基线。 |
| 10 | ~~**Lever seed board slug 失效**~~ | **已完成** — `airbnb`/`shopify` lever 板块已从 `SEED_SOURCES` 移除（404 as of 2026-07）。 |

## 八、关键文件索引

| 模块 | 核心文件 | 行数 |
|---|---|---|
| 状态定义 | `src/careerops/orchestration/state.py` | ~225 |
| 图编排 | `src/careerops/orchestration/graph.py` | ~110 |
| 节点实现 | `src/careerops/orchestration/nodes.py` | ~460 |
| kernel adapter | `src/careerops/orchestration/kernel_adapter.py` | ~410 |
| 映射 store | `src/careerops/orchestration/mapping_store.py` | ~145 |
| 语义去重 | `src/careerops/orchestration/dedup.py` | ~220 |
| 转换层 | `src/careerops/orchestration/conversions.py` | ~200 |
| filter 节点 | `src/careerops/orchestration/filter_node.py` | ~140 |
| side-effect kernel | `src/careerops/application/side_effect_kernel.py` | ~600 |
| policy engine | `src/careerops/policy/side_effect_policy.py` | ~100 |
| Postgres store | `src/careerops/infrastructure/database/side_effect_postgres.py` | ~560 |
| adapter 层 | `src/careerops/adapters/job_sources.py` | ~370 |
| http_fetcher | `src/careerops/adapters/http_fetcher.py` | ~160 |
| LLM 匹配 | `src/careerops/application/llm_matching.py` | ~210 |
| OAuth AES-GCM | `src/careerops/auth/google_oauth.py` | ~180 |
| Gmail provider | `src/careerops/integrations/gmail_side_effect_provider.py` | ~140 |
| Temporal M1 sink | `src/careerops/infrastructure/temporal/m1_crawl_sink.py` | ~110 |
| 运维指标 | `src/careerops/observability/metrics.py` | ~180 |
| API 入口 | `src/careerops/api/app.py` | ~130 |
| review endpoint | `src/careerops/api/routes/review.py` | ~130 |
| Spec | `docs/multi-agent-system-spec.md` | ~340 |
| 计划 v0.4 | `docs/langgraph-integration-plan.md` | ~250 |

## 九、环境与构建

- **Python**: 3.12（`requires-python = ">=3.12"`）
- **构建**: hatchling + uv（`uv run`, `uv sync`, `make verify`）
- **数据库**: PostgreSQL + Alembic migrations（`make migrate`）
- **测试**: pytest（`make verify` / `make verify-db`）
- **Lint**: ruff（format + check）、pyright（strict on src/）
- **覆盖率**: 75% 门槛（`fail_under=75`，当前 83%）
- **Temporal**: `temporalio>=1.30,<2`（worker 独立进程，`careerops-worker`）

## 十、v1.2 Working Tree 变更清单

| 文件 | 变更摘要 |
|---|---|
| `scripts/crawl_jobs.py` | `fetch_json` 改用 `http_fetcher.fetch`（SSRF + breaker + rate limit）；接入 `RealCrawlActivitySink` DB 直写替代 JSONL 文件输出；移除失效 Lever slug（airbnb/shopify） |
| `scripts/crawl_full.py` | 新增 `_is_safe_url()` SSRF 预检（scheme + private IP + metadata endpoint），`ego_save_page`/`ego_save_raw_html` 调用前校验 |
| `scripts/email_apply.py` | `cmd_send` 改走 `SideEffectKernel` → `GmailSideEffectProvider`，不再直接调 `GmailSender` |
| `src/careerops/config.py` | `model_qualification_artifact/version` 替换为 `model_base_url`/`model_api_key`/`model_name`；validator 改为检查三个字段非空 |
| `src/careerops/model_gateway/factory.py` | 移除 `_PROVIDER_ENV` 硬编码和 `get_settings()` 依赖；改为接收 `base_url`/`api_key`/`model` 参数 |
| `src/careerops/infrastructure/runtime.py` | 传 `model_base_url/api_key/name` 给 `create_model_client`；移除 Gmail placeholder，改为 `raise RuntimeError`；新增 InMemory repo wiring |
| `src/careerops/infrastructure/temporal/worker.py` | `main()` 新增 OutboxDrainActivities + ApprovalSweeperActivities 通电；task_queue 改为 `careerops-m1` |
| `src/careerops/infrastructure/temporal/__init__.py` | 导出 `LoggingInternalEventSink` |
| `src/careerops/infrastructure/temporal/internal_event_sink.py` | **新增** — Logging-only InternalEventSink for outbox audit |
| `src/careerops/infrastructure/memory_repos.py` | **新增** — InMemory{Matching,Job,Contact,Application}Repository for API routes |
| `src/careerops/api/app.py` | 新增 Protocol adapter（_ResumeRepoAdapter 等）；RuntimeResources wiring for API routes；matching_ui_router 加 auth |
| `src/careerops/adapters/http_fetcher.py` | `198.18.0.0/15` SSRF 放行（本地 DNS proxy 常见） |
| `src/careerops/integrations/gmail_sender.py` | 小改 |
| `src/careerops/model_gateway/anthropic_compat.py` | 加 `nosec B310` 注释 |
| `src/careerops/observability/metrics.py` | 小改 |
| `src/careerops/workflows/m1_contracts.py` | 移除未用 import |
| `tests/unit/test_model_gateway.py` | 适配新 factory 参数化 API |
| `tests/unit/test_config.py` | 适配新 validator 错误信息 |
| `tests/unit/test_http_fetcher.py` | 198.18.0.0/15 改为 allow 测试 |
| `tests/security/test_security_suite.py` | 适配新 validator 错误信息 |
| `tests/integration/test_temporal_recovery.py` | `CountingSink` 显式继承 `SmokeActivitySink` |

## 十一、交接建议

1. **outbox + sweeper 已通电**——Temporal worker `main()` 现在创建 drain 和 sweeper activity，需确认 Temporal 部署后 schedule 生效。
2. **http_fetcher 已接入 crawl_jobs**——SSRF 防护生效，但 `198.18.0.0/15` 放行扩大了攻击面，需评估。
3. **LLM qualification gate 已放宽**——不再要求 ADR 0006 addendum qualification artifact，改为用户自行配置 `model_base_url`/`model_api_key`/`model_name`。安全不变量（不绑 tools、review_only、untrusted_claims）仍保留。
4. **pyright suppress 是权宜之计**——JSON 边界（`json.loads` 返回 `Any`）需长期方案（Pydantic v2 或 TypedDict schema）。
5. **新文件缺测试**——`memory_repos.py`（36%）和 `internal_event_sink.py`（24%）覆盖率低，需补单元测试。
6. **Temporal M1 已通电**——RealCrawlActivitySink 已写入 job_postings 表，Lever airbnb/shopify slug 已移除。
