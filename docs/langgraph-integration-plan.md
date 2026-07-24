# CareerOps LangGraph 编排层集成 — 落地计划

- 版本：v0.4（review_gate 安全闭环 + v1 生命周期收敛；取代 v0.3）
- 日期：2026-07-24
- 状态：待审阅 · 修订后 · 未开始执行
- 关联：[多 Agent 架构 Spec](multi-agent-system-spec.md)、[ADR 0006 模型隐私](adr/0006-model-privacy.md)、[MVP 实施计划](../.omx/plans/careerops-agent-mvp-plan.md)

> **v0.4 依据**：review_gate 的 LangGraph 重跑语义经 **PoC-1（langgraph 1.2.9 实跑）+ 代码核实**确认（见 §7）。本版本同时把 approval 决策、edit、并发幂等和 v1 单进程边界写成可测试契约；`PostgresSaver`/detail parser 仍属于 v1.1 PoC，不作为 v1 已验证能力。

---

## 1. Context

CareerOps 当前是散装 scripts，无 Agent 编排/HITL。三套并行半成品：`adapters/job_sources.py`（6 adapter，`list_jobs` 齐，**无 detail parser、零通电**）/ M1 Temporal（契约齐，死代码，`worker.py:50` 只注册 Smoke）/ scripts（自实现 HTTP，丢 raw_data）。P0 根因是 JD 正文覆盖率为零；总量必须以 Stage 0 生成并 hash 固化的 fixture manifest 为准，暂不在本文硬编码 `7191` 或 `8057`。

**目标（adapter 路线）**：给 adapter 补 detail parser + 通电，adapter 成唯一 ATS 入口；LangGraph 编排 `采集→简历→过滤→匹配→草稿→HITL→发送`；interrupt 接 side-effect kernel。守 ADR 0006（LLM 不绑 tools、review_only、v1 `disabled`）。

---

## 2. 方案

### 2.1 `CareerOpsState`（TypedDict，全库首个）

`src/careerops/orchestration/state.py` 必须先定义完整字段契约。checkpoint state 只存 JSON-safe DTO，不直接存 domain dataclass；domain 对象只在节点边界转换。所有 UUID 转字符串，datetime 转 UTC ISO-8601，枚举转值字符串。

| 字段 | 类型/规则 | 更新语义 |
|---|---|---|
| `raw_job_records` | `tuple[RawJobDTO, ...]` | append |
| `contacts` | `tuple[ContactDTO, ...]` | append |
| `resume_text` | `str` | overwrite |
| `skill_profile` | `SkillProfileDTO \| None` | overwrite |
| `matches` | `tuple[MatchDTO, ...]` | append |
| `drafts` | `tuple[DraftDTO, ...]` | overwrite；edit 后必须产生新 payload hash |
| `requested_for` | 服务端写入的用户 ID 字符串 | immutable |
| `review_revision` | `int`，从 0 开始 | overwrite/increment |
| `pending_approval_id` / `pending_intent_id` | `str \| None` | overwrite |
| `approved_draft_ids` | `tuple[str, ...]` | overwrite |
| `edit_payload` | 受限 `EditedDraftPayload \| None`，不得是任意 dict | overwrite/consume |
| `send_receipts` / `errors` | tuple DTO | append |

`thread_id` 只在 `config["configurable"]`，不在 state；节点只能返回 dict，禁止原地修改。`RawJobDTO`、`DraftDTO` 等 DTO 的字段和长度上限必须在 Stage 1 测试中固定。测试必须使用实际 `MemorySaver` checkpoint round-trip，不能只做 pickle/JSON round-trip。

### 2.2 图结构（8 节点，调 adapter 非 scripts）

`START → crawl → extract_contacts → resume → filter → match → draft → review_gate →(Command goto)→ send → END`。节点契约：crawl 调 adapter（HTTP，确定性带副作用）；match 注入 `DisabledModelAdapter`（v1）；review_gate 负责 interrupt 后的 approval 决策；draft 消费一次性的 `edit_payload` 并生成新 payload/revision；send 调 `kernel.execute`（v1 FakeProvider）。

### 2.3 adapter 路线 + `fetch_job` 边界

- **现状**：adapter 是**纯解析器**（`list_jobs(response_data)`，response 已获取；HTTP 在外层 `scripts/crawl_jobs.py:fetch_json`）。
- **锁定的边界**：adapter 不持有 HTTP client。新增 `DetailJobSourceAdapter.fetch_job(detail_response, *, source_url, fetched_at) -> RawJobRecord`，只负责 detail response 解析；`JobSourceAdapter` 仍只负责 list，JsonLd/Sitemap 等没有 detail parser 的 adapter 不被强行实现该方法。`http_fetcher.py` 返回带 `status_code/final_url/fetched_at/response_hash/body` 的 `FetchedResponse`，并在每次 redirect 后重新执行 host allowlist、私网 IP/协议/大小/timeout 检查。
- **Greenhouse**：list 无 content，detail parser 解析 `/boards/{board}/jobs/{id}` 的 `content`。**Lever/Ashby**：是否能从 list 直接取得 `descriptionPlain` 必须以冻结 response fixture 证明，否则走 detail URL。**JsonLdAdapter**：已含 `description`（`:230`），直接映射为 `RawJobRecord.description`。
- `RawJobRecord` 增加显式 `description: str`；`raw_data` 保留完整原始 response。detail parser 的 provenance 由 `FetchedResponse` 统一写入，不能依赖 parser 隐式补字段。
- **通电 v1**：crawl 节点直接调 `http_fetcher + adapter`；**v1.1**：M1 `CrawlActivitySink` 真实实现 + `worker.py:50` 注册 M1 + `RawJobRecord.raw_data → CrawledPostingRecord.structured_data` 映射。activity sink 具体命名 Stage 4 定（不预设）。
- **scripts 降级**：`crawl_jobs.py`/`crawl_full.py` 改调 adapter 薄壳或废弃。

### 2.4 Checkpointer + v1 生命周期定位

- **v1 固定为单进程 demo**：`MemorySaver + InMemorySideEffectStore + InMemoryReviewMappingStore`，不支持生产部署或进程重启恢复。`RuntimeEnvironment.PRODUCTION` 下禁止挂载 review endpoint，启动时 fail closed；不能用“当前只有一个 worker”作为安全前提。
- v1 mapping 不创建 Alembic migration；mapping 与 checkpoint 同属进程生命周期，使用 `put_if_absent/claim_resume/complete_resume` 保证同一 approval 的重复请求返回同一结果。
- **v1.1 durable**：先用最小独立 PoC 锁定 `langgraph==1.2.9` 与 `langgraph-checkpoint-postgres` 的精确兼容版本、`postgresql+psycopg://` 到 psycopg DSN 的转换、schema/role/grant 和 `.setup()` 行为，再加入 `0010` migration。若 checkpointer 无法安全指定 `langgraph` schema，使用独立 database/role，不能假设 `search_path` 会生效。

### 2.5 review_gate（PoC-1 + 代码确认的正确模式）

**PoC-1 关键发现（§7）**：LangGraph resume **从头重跑整个节点**（`review_gate` 进入 2 次）→ **interrupt 前的副作用必须幂等**。approval 的“查找/创建”和“决策”也必须是原子、可重试操作。

```python
def review_gate(state, *, kernel, review_mapping, capability_resolver, config):
    now = utc_now()
    thread_id = config["configurable"]["thread_id"]
    # capability_resolver 只能返回已验证的 trusted facts；禁止在节点内硬编码 True。
    capability = capability_resolver.for_send_batch(state["drafts"])
    # ① propose 幂等：key 必须包含 review_revision + 当前 drafts 的 payload hash。
    proposal = ProposalInput(
        action_kind="send_email", resource_type="email_thread",
        resource_id=capability.resource_id,
        idempotency_key=batch_key(state["drafts"], state["review_revision"]),
        created_by="langgraph.review_gate",
        target=capability.target,
        payload={"drafts": [draft_to_payload(d) for d in state["drafts"]]},
        trusted_facts=capability.trusted_facts,
        evidence_refs=capability.evidence_refs,
        authenticated=capability.authenticated,
    )
    result = kernel.propose(proposal, now=now)
    # ② 一个原子操作：同一 intent 只能有一个 PENDING approval。
    approval = kernel.get_or_create_pending_approval(
        result.intent.id, requested_for=state["requested_for"], now=now
    )
    review_mapping.put_if_absent(
        thread_id=thread_id, approval_id=approval.id, intent_id=result.intent.id,
        requested_for=state["requested_for"],
    )
    # ③ payload 必须 JSON 可序列化；resume token 只接受当前 approval。
    decision = interrupt({"approval_id": str(approval.id), "intent_id": str(result.intent.id)})
    decision = parse_review_decision(decision)
    if decision.approval_id != approval.id:
        raise ReviewDecisionError("approval_id does not match the interrupted approval")
    # ④ decide_approval 是幂等的，并重新执行 payload/policy/expiry/owner 校验。
    if decision.action == "approve":
        kernel.decide_approval(
            approval.id, action="approve", requested_for=state["requested_for"], now=now
        )
        return Command(goto="send", update={
            "approved_draft_ids": tuple(d["id"] for d in state["drafts"]),
        })
    if decision.action == "edit":
        edited = validate_edited_drafts(decision.edited_drafts)
        kernel.decide_approval(
            approval.id, action="reject", requested_for=state["requested_for"], now=now
        )
        return Command(goto="draft", update={
            "edit_payload": edited,
            "review_revision": state["review_revision"] + 1,
        })
    kernel.decide_approval(
        approval.id, action="reject", requested_for=state["requested_for"], now=now
    )
    return Command(goto=END)
```

- transport `ReviewRequest/ResumePayload` 只使用 JSON-safe 字段（`approval_id: str`、`action: Literal["approve", "reject", "edit"]`、`edited_drafts`）；`parse_review_decision` 再转换为内部 `ReviewDecision(approval_id: UUID, ...)`。approve/reject 不允许携带 edit 内容，edit 只允许 draft ID、subject、body，recipient/target 变更必须重新走新的受控 proposal。
- **resume token = `approval.id`**；`/review/{approval_id}` 端点只接受服务端校验过的 `ReviewRequest(action, edited_drafts)`，查 mapping（approval_id↔thread_id↔intent_id↔requested_for）反查 thread，再由 per-thread claim 保护 `graph.invoke(Command(resume=...), {"configurable": {"thread_id": ...}})`。相同 decision 重试返回已缓存结果，冲突 decision 返回 409。
- **多 draft → 一个批量 intent/approval**（一个 `send_email_batch` intent 包多 draft），非逐条。
- **`SideEffectStore`/kernel 需加原子 `get_or_create_pending_approval` 和幂等 `decide_approval`**；单独的 `find_pending_approval` + `request_approval` 不足以防并发重复。

### 2.6 kernel policy（代码核实，`policy/side_effect_policy.py`）

- **`propose(proposal, *, now)` 无 `policy=` 参数**（`kernel:256`）；policy 由注入的 `SideEffectPolicyDecider` 决定。
- **外部写 fail-closed**（`policy:79-92`）：`trusted_facts` 必须由 capability/allowlist 服务产生，含 `capability_released=True` + `target_allowlisted=True`，且 `evidence_refs` 非空，才 `REQUIRE_APPROVAL`；否则 `DENY`（`CAPABILITY_NOT_RELEASED`/`TARGET_NOT_ALLOWLISTED`/`EVIDENCE_REQUIRED`）。节点不得直接填入字面量 `True` 或伪造 `evidence_refs`。
- **edit 流程**（修正 v0.2 错误描述）：`reject(旧 approval)` → **重新 `propose`（新 intent/新 payload）→ 新 approval 映射**。不是"payload hash 自动变自废"——payload hash 变是因为新 propose 生成新 payload_version，旧 approval 仍绑旧 version，需显式 reject。
- **`ProposalInput` 字段**（`kernel:109`）：`action_kind/resource_type/resource_id(UUID)/idempotency_key/created_by/target/payload/attachment_refs/evidence_refs/trusted_facts/untrusted_claims/authenticated`；`authenticated` 必须显式来自可信运行上下文，不依赖 dataclass 默认值。

### 2.7 review endpoint 安全 + 依赖注入

- **新 router**（当前 `app.py:69` 无 review router）：`POST /api/v1/review/{approval_id}`；request schema 只允许 `action: approve|reject|edit`，edit 只允许已定义字段和长度，不接受任意 dict。
- **安全**：复用 console auth（`ConsoleAuthService`）、CSRF（同源 cookie）、**approval 所属用户校验**（`requested_for` == 当前用户）、限流（`RedisAuthRateLimiter`）。`requested_for` 不得硬编码为 `"reviewer"`，也不得由客户端 body 覆盖。
- **graph 依赖注入**：`kernel`/`SideEffectStore`/`adapter`/`provider` 在 `RuntimeResources` 构造，编译图时注入节点（functools.partial 或 closure），挂 `app.state.career_graph`；`lifespan` 关闭时 dispose。
- **运行边界**：v1 demo 只在 development/test 挂载此 router；production 启动若没有 durable checkpointer + durable kernel/mapping，必须拒绝启动或返回固定 503，不得静默降级到内存状态。

### 2.8 scripts 上提 src（修正：scripts 不在安装包）

`pyproject.toml:22-26` 的 entry points 全是 `careerops.*`——**`scripts/` 不在安装包**，graph 节点 `import scripts.*` 生产失败。需上提：
- `resume_review.py:60-94` 纯函数 → `src/careerops/application/resume_analysis.py`（删 `llm_matching.py:101` sys.path）。
- `email_apply.generate_body` → `src/careerops/application/email_drafting.py`。
- `extract_contacts.extract_from_file` → `src/careerops/application/contact_extraction.py`。
节点只 import `src/careerops.*`。

### 2.9 v1 保持 `disabled` + schema 校验

- **不加门控放宽字段**。match 注入 `DisabledModelAdapter`（`base.py:74`）→ `JobMatchResult(error="disabled")`，图流转到 review_gate 人工兜底。真 LLM 走完整 ADR 0006 qualification（pilot）。
- **schema 校验**（与编排并行）：`StructuredModelRequest` 增 `schema: dict|None`；`anthropic_compat.invoke` jsonschema 校验 + 一次修复 + 失败抛 `ModelInvocationError`。Stage 0b 必须同时加入并锁定 `jsonschema` runtime dependency；v1 `disabled` 不发起真实模型调用。

---

## 3. 文件清单 + 分阶段顺序

**Stage 0 — detail parser + http_fetcher + scripts 上提（串行，硬阻塞）**
- 改 `adapters/job_sources.py`（新增 `DetailJobSourceAdapter`、`RawJobRecord.description`、Greenhouse detail parser；不要让所有 list adapter 被迫实现 detail 方法）
- 新 `adapters/http_fetcher.py`（HTTP + 每跳 redirect 校验 + 限流 + SSRF + response provenance；使用已锁定的 runtime HTTP 实现）
- 新 `application/{resume_analysis,email_drafting,contact_extraction}.py`（scripts 上提）；改 scripts 为薄壳
- 新 `tests/fixtures/adapter_manifest.json` 与冻结 response fixtures；新 `tests/unit/test_adapters_fetch_job.py`、`test_resume_analysis.py`、`test_http_fetcher.py`

**Stage 0b — schema 校验（并行）**：改 `model_gateway/base.py`+`anthropic_compat.py`；在 `pyproject.toml` 加锁 `jsonschema` runtime dependency；新 `schemas/`、`test_model_gateway_schema.py`

**Stage 1 — State + 图骨架（串行）**
- 新 `orchestration/{__init__,state,nodes,graph,kernel_adapter,mapping_store}.py`
- 改 `application/side_effect_kernel.py`：增加原子 `get_or_create_pending_approval`、幂等 `decide_approval`，保持现有 policy 规则不变
- 改 `pyproject.toml`：先精确锁 **`langgraph==1.2.9`**（PoC 验证版本）；Postgres checkpointer 不进入 v1 runtime dependency；`uv lock`
- 新 `tests/unit/test_orchestration_graph.py`（复用 PoC-1 模式）、`test_state_serialization.py`、`test_review_idempotency.py`

**Stage 2 — 节点 + 转换 + review_gate（Stage 1 后并行）**
- 新 `orchestration/conversions.py`（RawJobRecord→匹配 DTO、dict→RecruitingContact、(sub,body)→ReplyDraft；补齐 UUID/source_url/company_id 等必填 provenance）
- 新 `orchestration/filter_node.py`
- 新 `integrations/gmail_side_effect_provider.py`（仅 v1.1；把现有 `GmailSender` 适配为 `SideEffectProvider`，不放在 conversions）
- 新 `tests/unit/test_kernel_adapter.py`（resume 不重复 approval；并发 get-or-create；approve/reject 幂等；edit 新 intent；policy fail-closed 三要件 deny）、`test_conversions.py`

**Stage 3 — 挂载 + review endpoint（串行）**
- 改 `infrastructure/runtime.py`（`:40` 加 `MemorySaver`、InMemory mapping、注入依赖和 production fail-closed guard）、`api/app.py`（`:69` 仅 demo/test 挂 `career_graph`）
- 新 `api/routes/review.py`（request schema、认证 + CSRF + 所属校验 + 限流 + duplicate/conflict response）
- 新 `tests/unit/test_review_endpoint.py`（未认证/CSRF/owner/限流/重复决策/冲突决策）

**Stage 3.5 — 错误加固 + 业务可观测**：429 退避；circuit breaker（`failure_threshold=5/recovery_timeout=30s/half_open_max_requests=1`）；**复用 `OutboxPublisher.max_attempts=10`**；LLM token 聚合 + apply→interview 埋点。

**Stage 4 — v1.1**：Temporal M1 通电（worker 注册 + CrawlActivitySink 真实实现 + RawJobRecord→CrawledPostingRecord）、独立 PoC 锁定 `PostgresSaver`/checkpoint package/DSN/schema/role 后，再加入 `0010_langgraph_review_mapping`（`careerops` schema、grant、唯一约束）、`GmailSideEffectProvider`、kernel Postgres 化（解锁 durable 恢复 + outbox 完整驱动 + approval sweeper）。

---

## 4. 关键决策

1. **adapter 路线**：补 `fetch_job`（纯解析）+ `http_fetcher`（HTTP 外层）；scripts 降级。消除三套并行。
2. **v1 节点直接调 adapter**；v1.1 进 Temporal。复用 `JsonLdAdapter`/`SitemapAdapter`。
3. **v1 `disabled`**：不放宽门控；真 LLM 走 qualification pilot。
4. **v1 = 单进程 demo**（MemorySaver + InMemoryStore）；production 不挂载 review endpoint，durable 固定在 v1.1。
5. **review_gate 幂等**：propose 使用包含 revision/payload hash 的 idempotency key；原子 `get_or_create_pending_approval`；幂等 `decide_approval`；payload JSON-safe；`Command(goto=节点名)`；resume token = approval.id。
6. **kernel policy fail-closed**：三要件（capability_released/target_allowlisted/evidence_refs）缺一 deny。
7. **scripts 上提 src**：节点只 import `src/careerops.*`。
8. **kernel 变更边界**：不改变 policy 规则或 default-deny；只增加 approval 生命周期的原子/幂等 API 和对应存储约束。

---

## 5. 验证

**命令**：`make verify`（`:67`：uv lock --check + ruff format/check + pyright + pytest --cov）+ `make verify-db`（`:38`，integration）。**无 `make test`/`make lint`**。

**数据验收（可复现）**：Stage 0 先生成 `tests/fixtures/adapter_manifest.json`，记录 source、样本总数、description 覆盖数、fixture SHA-256 和关联 commit；不依赖 `data/`。source-specific coverage（Greenhouse detail、JsonLdAdapter、Lever/Ashby description）。在 manifest 产生前，不在本文或 Spec 宣称 `0/7191`/`0/8057`；manifest 产生后两份文档必须同一提交同步更新。真 LLM（非空 reasoning）标“需 qualification pilot”，v1 disabled 不要求。

**图编排**（MemorySaver 进程内，复用 PoC-1 模式）：
- `test_orchestration_graph.py`：invoke 到 review_gate → `get_state().next == ("review_gate",)` → `Command(resume={"approval_id": str(...), "action":"approve"})` → `decide_approval` 产生 APPROVED → send 执行 + `kernel.replay(intent_id).approvals[0].decision == APPROVED`（**replay 传 intent_id**）；reject → END。
- `test_kernel_adapter.py`：**resume 重跑 review_gate 不重复创建 approval**；并发 resume 只产生一个 pending approval；重复 approve/reject 返回相同最终状态，冲突决策失败；edit → 旧 approval REJECTED + 新 payload hash + 新 intent/approval；policy 三要件缺一 → DENY（fail-closed）。
- `test_review_endpoint.py`：owner/CSRF/auth/limiter 全部通过；相同 decision 重试返回缓存结果，冲突 decision 为 409，production/demo guard 正确拒绝。
- 端到端：种子源 → adapter 采集 → 匹配（disabled，不能被当作正匹配）→ 草稿 → review_gate 挂起 → /review 批准 → send（FakeProvider）。

**质量门**：`make verify` 全绿（ruff line 100、pyright src strict、coverage ≥ 75）。

**ADR 0006 自检**：match `is_review_only=True`；`untrusted_claims={}`；无 LLM tool binding；kernel policy 规则零改（仅增加 approval 原子/幂等 API）；v1 `disabled`；token 聚合不记内容。

---

## 6. 范围

- v1：Stage 0–3.5（detail parser + http_fetcher + scripts 上提 + schema + 图 + 节点 + review_gate 幂等 + **仅 demo/test review endpoint** + MemorySaver + FakeProvider + 加固 + 业务可观测）。
- v1.1：Stage 4（Temporal M1 通电、PostgresSaver、GmailSideEffectProvider、kernel Postgres 化 → durable 恢复）。

**依赖卡点**：apply→interview（数据可得性，v1 disabled 更晚）；outbox 完整驱动 + sweeper（依赖 kernel Postgres 化）；真 LLM（qualification pilot）。

**明确不做**：语义去重（v1 不含，v1.1 视需求）；官网 ego-browser 详情（JsonLdAdapter 覆盖外站点，v1.1）。

---

## 7. PoC-1 验证记录（langgraph 1.2.9 实跑）

`scripts/poc_langgraph.py`（PoC，Stage 0 后清理）以 `uv run --no-project --with langgraph==1.2.9 python scripts/poc_langgraph.py` 实跑；PoC 必须使用断言，不能只打印结果。当前实跑观察到 `review_gate` 进入次数为 2、approve 路由到 `send`、最终 `next=()`。

| 验证点 | 结果 |
|---|---|
| langgraph 版本 | **1.2.9**（PoC 验证版本；Stage 1 精确锁定 `==1.2.9`） |
| resume 行为 | **从头重跑整个节点**（review_gate 进入 2 次：interrupt 暂停 + resume 重跑）→ interrupt 前副作用必须幂等 |
| `Command(goto=...)` | 路由到**节点名**（`send`/`END`），不是 `interrupt()` |
| `interrupt(payload)` | 返回 resume 值；payload 存 `Interrupt(value=...)`，**必须 JSON 可序列化**（UUID→str） |
| `thread_id` | 在 `config["configurable"]`，不在 state |
| 节点返回 | dict 更新 state（非原地改） |
| `get_state()` | `.next`（待执行节点）/`.values`/`.tasks`（含 `Interrupt`） |

**对 review_gate 的直接结论**：propose 幂等只能解决 intent 重复；approval 必须用原子 `get_or_create_pending_approval`，决策必须用幂等 `decide_approval`；approve 必须写入 kernel 的 `APPROVED` 状态后才允许 send；payload 用 `str`；`Command(goto=节点名)`。
