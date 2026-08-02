# 爬取配方仓库 + 双轨配置化抓取（v4）

> **方向（用户拍板）：双轨完全配置化。** 所有抓取知识都变成数据，删除全部 per-site Python adapter。
> - **Tier 1（确定性、可声明式）**：ATS/JsonLd/Sitemap/StaticHtml → 标准 JSONPath 配方，`RecipeEngine` 执行。
> - **Tier 2（复杂、需自适应）**：Workday 递归、国内站反爬/登录态 → **LLM skill**（per-site playbook，存配方仓库，`CrawlAgent` 按 skill 执行）。
> - **无 per-site Python adapter 类**：6 个全删。
>
> **v4 相对 v3 的关键变化**：(1) 引入 LLM skill 化解决"复杂站如何配置化"（不再强行声明式化 Workday/StaticHtml 的图灵完备逻辑）；(2) 标准 JSONPath（RFC 9529）+ 步骤命名空间，配完整 DSL 语义文档；(3) 一次性切 execute（无双协议分派）；(4) execute 扩展信号字段；(5) adapter 内嵌护栏显式写进 schema。

## 目标

CareerOps 现有 6 个 per-site Python adapter + Tier 2 `CrawlAgent`（通用 LLM loop）。本 spec 把抓取知识**全部数据化**：

1. **Tier 1 配方化**：5 类确定性抓取（greenhouse/lever/ashby/JsonLd/Sitemap/StaticHtml）翻译成标准 JSONPath 配方，`RecipeEngine` 是 Tier 1 唯一来源。
2. **Tier 2 skill 化**：复杂站（Workday 递归、国内站反爬/登录态）沉淀成 LLM skill（playbook 数据），`CrawlAgent` 按 skill 执行（取代现在的通用盲跑 loop）。
3. **删全部 6 adapter 类**，`_ADAPTER_REGISTRY` 从配方 manifest 动态构建。
4. **加站 = 加一份配方 or skill**，零 Python。

**分轨标准 = 能否用声明式（JSONPath/CSS）可靠表达**（不是"静态 vs 动态"）：
- 简单/中等静态 HTML（规整结构、css+zip 可靠）→ Tier 1 配方。现有 `StaticHtmlAdapter`（两正则按索引配对）属此类，进 css+zip。
- **复杂静态 HTML**（结构不规则、字段需语义理解/关联推断、css 写不出可靠选择器）→ **Tier 2 LLM skill**（LLM 擅长理解复杂 DOM，复用现有 `LLMJobExtractor`）。
- 动态复杂（Workday 递归、国内站反爬/登录态）→ Tier 2 skill。

## 现状（全部 CodeGraph 核实）

- **6 adapter**（`adapters/job_sources.py`）：`GreenhouseAdapter`(:94)/`GreenhouseDetailAdapter`(:145)/`LeverAdapter`(:186)/`AshbyAdapter`(:231)/`AshbyDetailAdapter`(:281)/`JsonLdAdapter`(~:319)/`SitemapAdapter`(~:374)/`StaticHtmlAdapter`(~:436)。
- **`_ADAPTER_REGISTRY`**（`m1_crawl_sink.py:78`）：硬编码 dict，key=greenhouse/lever/ashby/json_ld/sitemap/static_html。
- **`CrawlSourceType` enum 只有 4 值**（`crawl_plans.py:97`）：greenhouse/lever/ashby/official。json_ld/sitemap/static_html 是 registry key（OFFICIAL 的 meta-type 解析产物），**不是 enum 值**。
- **执行链**：`crawl_source`(:260) → `_fetch_for_request`(:246，按 source_type 路由 public_ats 大响应通道) → `adapter.list_jobs`(:266) → CrawledPostingRecord(:291) → ingest_posting(:488，内部 join companies)。
- **production 走 signals**：`crawl_source_with_signals`(:310) 返回 `CrawlSourceResult(postings, status_code, body_prefix, expected_fields_missing)`（:403），status_code/body_prefix 是 CAPTCHA/login-wall 检测信号。
- **Tier 2**：`_crawl_via_agent`(:410) → `CrawlAgent.crawl()`（`application/crawl_agent.py`，通用 LLM loop + BrowserTool），ego source + 结构化 adapter 空结果时 fallback。
- **greenhouse 多步**：`_fetch_greenhouse_details`(:432，batch_size=10 顺序、失败静默降级、detail URL 剥 query)。
- **adapter 内嵌护栏**：SitemapAdapter 的 XXE 防护（`_UNSAFE_XML_RE`）+ 职位 URL filter（`_JOB_PATH_RE`）；JsonLdAdapter 的 `@type=="JobPosting"` 过滤；StaticHtmlAdapter 的两正则按索引配对。
- **`scripts/crawl_jobs.py:96-99`** 直接 import 6 adapter。
- **测试**：`test_greenhouse_bulk_fetch.py` 等已是端到端（校 CrawledPostingRecord，:291-307 映射不变则零改动跑通）。

## 设计

### A. 双轨架构

分轨标准 = **能否用声明式（JSONPath/CSS）可靠表达**（非静态/动态）。

```
source(source_type, executor_mode)
  ├─ Tier 1（声明式可表达：ATS JSON API / 简单中等静态HTML / JsonLd / Sitemap）
  │     → RecipeEngine.execute(配方) → AdapterFetchResult(+信号)
  └─ Tier 2（声明式不可靠：复杂静态HTML / Workday递归 / 国内站反爬登录态）
        → CrawlAgent.crawl(skill) → RawJobRecord[]（skill 复用 LLMJobExtractor）
两者产出都经 :291-307 → CrawledPostingRecord → ingest_posting（不变）
```

### B. 配方仓库结构（vendor 起步）

```
vendor/crawl-recipes/
├── manifest.json                # Tier1 配方 + Tier2 skill 索引
├── recipes/                     # Tier 1（标准 JSONPath）
│   ├── greenhouse/recipe.yaml   # 多步 list→detail
│   ├── lever/, ashby/
│   ├── official_jsonld/, official_sitemap/, official_static/   # 三种 extract mode
├── skills/                      # Tier 2（LLM skill）
│   └── workday/SKILL.md         # 复杂站 playbook（+ metadata）
└── LICENSE (MIT)
```

### C. Tier 1 配方 schema（标准 JSONPath + 完整 DSL 语义）

**DSL 语义文档（解 BLOCKER 1）**：
- **表达式语言**：标准 RFC 9529 JSONPath，实现用 `jg-rp/python-jsonpath`（benchmark 优于 jsonpath-ng）。
- **步骤命名空间**：每步产出绑定到 `steps.<id>`。跨步骤引用 `steps.list.jobs[*]`。filter 用标准语法 `[?(@.description=='')]`（v3 缺 `@.` 是错的）。
- **`when`**：步骤级，步骤开始前求值一次（per-batch）。值为标准 JSONPath，非空才执行该步。
- **`foreach`**：遍历一个 JSONPath 选出的数组，per-item 子 fetch；默认顺序执行（非并发），受 `rate_limit` 约束。
- **`merge`**：`{strategy: overwrite_empty | overwrite_all | keep_first}`，默认 `overwrite_empty`（只填空字段，保 greenhouse 语义）。
- **失败语义**：子 fetch 失败 → 该 item 静默降级（保留 list 数据，等价旧 `_fetch_greenhouse_details` 容错）；整步失败 → 产出空，不阻断后续。
- **静态校验**：配方加载时用 JSONPath 库 dry-parse 所有表达式，非法即拒。

**字段定义**：

| 字段 | 说明 |
|---|---|
| `recipe_version` | 写入 parser_version |
| `source_type` | manifest key（greenhouse/official_jsonld/…），与 CrawlSourceType enum 解耦 |
| `executor_mode` | http（Tier 1 只 http） |
| `match` | `{ url_patterns, host_suffix }` |
| `steps` | 有序：`{ id, when, foreach, fetch, extract, merge }` |
| `extract` | `{ mode: json_path\|json_ld\|sitemap\|css, items_path, fields, filter }` |
| `fields` | `{ name: { path, fallback:[...], transform: html_unescape\|none } }` |
| `extract.filter` | **通用过滤原语**（解 JsonLd 护栏）：`{ jsonpath: "@.type==JobPosting" }` |
| `rate_limit` / `pagination` | 同 v3 |

**Greenhouse 多步（标准 JSONPath 重写）**：
```yaml
source_type: greenhouse
steps:
  - id: list
    fetch: { endpoint: "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", method: GET }
    extract: { mode: json_path, items_path: "$.jobs",
      fields: { id:{path:id}, title:{path:title}, location:{path:"location.name"},
                url:{path:absolute_url}, description:{path:content, transform:html_unescape} } }
  - id: detail
    when: "$.steps.list.jobs[?(@.description=='')]"         # 仅当有空描述项
    foreach: "$.steps.list.jobs[?(@.description=='')]"      # 标准 filter 语法
    fetch: { endpoint: "{list_endpoint}/{external_id}", method: GET }   # list_endpoint=base_url 剥 query
    extract: { mode: json_path, fields: { description:{path:content, transform:html_unescape} } }
    merge: { strategy: overwrite_empty }
rate_limit: { requests_per_minute: 60 }
fallback: llm_skill
```

**护栏原语（解 adapter 内嵌安全/正确性）**：
- json_ld 模式：`extract.filter: {jsonpath: "@.type==JobPosting"}`（extruct 不替你过滤）。
- sitemap 模式：引擎内置 **XXE 防护**（拒 DOCTYPE/ENTITY）+ `extract.url_filter: "\\b(jobs|careers|positions)\\b"`（只挑职位 URL）。
- css 模式：支持 **多选择器 zip**（`fields: {title:{css:"h2"}, location:{css:".loc"}}` 按索引配对，等价 StaticHtmlAdapter）。
- detail URL 构造：`{list_endpoint}` = base_url 剥 query string（引擎内置，保 scheme/netloc/path）。
- `transform: html_unescape` 显式声明。

### D. RecipeEngine + execute 协议（解 BLOCKLER 2）

```python
class SelfFetchingAdapter(Protocol):
    @property
    def source_type(self) -> str: ...
    @property
    def parser_version(self) -> str: ...     # recipe:{id}:{version}
    def detect(self, base_url: str) -> bool: ...
    async def execute(self, request: CrawlJobSourceInput, fetch: FetcherFn) -> AdapterFetchResult: ...
```

**`AdapterFetchResult` 扩展**（解 BLOCKER 2）：加 `status_code: int` + `body_prefix: str`。多步配方取**首步（list）**的 resp 作信号（detail 失败走静默降级，不掩盖 list 信号，保 CAPTCHA 检测语义）。

**`fetch` 参数 = `_fetch_for_request` 绑定 request 的 partial**（解 fetch 上下文丢失）：保 source_type 路由的 `public_ats` 大响应通道（greenhouse 响应几十 MB 不被 1MB cap 截断）。

### E. 执行链（一次性切 execute，解 BLOCKER 3）

**无双协议分派**（用户选大爆炸）：`crawl_source` 和 `crawl_source_with_signals` **都**改为调 `adapter.execute(request, fetch)`。一次迁移全部 6 adapter → 配方，不存在 execute/list_jobs 共存期。

- `crawl_source`：`result = await adapter.execute(...)` → 现有 :291-307 构造 CrawledPostingRecord（不变）。
- `crawl_source_with_signals`：`result = await adapter.execute(...)` → `CrawlSourceResult(postings, status_code=result.status_code, body_prefix=result.body_prefix, ...)`。
- **删除**：`_fetch_greenhouse_details` + greenhouse 特殊分支（多步移进配方）；6 adapter 类；`_ADAPTER_REGISTRY` 硬编码 dict（改 manifest 动态构建）。
- **Tier 2 fallback 不变**：adapter=None 或配方产出空 → `_crawl_via_agent`（:410）。

### F. Tier 2 LLM skill 子系统（框架）

**skill 形态**：每个复杂站一个 `skills/<site>/SKILL.md`（自然语言 playbook + frontmatter metadata）。内容示例（Boss 直聘）：
```markdown
---
source_type: cn_zhipin
executor_mode: ego
match: { host_suffix: "zhipin.com" }
---
# Boss 直聘抓取 skill
1. 登录态：复用 ego 隔离 profile（已登录）
2. 在已登录页内 fetch /wapi/zpgeek/search/joblist.json（拿明文 salaryDesc，绕字体反爬）
3. 列表→详情必带 securityId/lid
4. 翻页 12-22s 随机延迟，单次 ≤10 页防封号
```
**执行**：`CrawlAgent` 加载 skill，按 playbook 自主执行（现有 LLM loop + skill 引导，取代盲跑）。skill 是**数据**（非 per-site Python），满足完全配置化。

**框架范围（本 spec）**：确立 skill 格式 + `skills/` 目录 + manifest 索引 + CrawlAgent 加载 skill 的接口。**完整 skill 集（国内站等）留后续 spec**（CrawlAgent 现已兜底，skill 化是增量优化）。本 spec 只建框架 + 1 个示范 skill（Workday 或现有 official+ego 站之一）。

### G. 选型、契约、enum

- **依赖**：`pyyaml` + `jg-rp/python-jsonpath`（标准 RFC 9529）+ `extruct`（json_ld）+ css 解析器（selectolax）。
- **数据契约**：配方 fields 目标 = 归一化层内部字段名（title/location/description/apply_url）。不重命名、不建兼容层。
- **enum 处理（解 HIGH）**：**保留 `OFFICIAL` 为 enum meta-type**（不拆三、不改 enum、不做数据迁移）。配方 manifest 的 source_type key（official_jsonld 等）与 `CrawlSource.source_type`（存 OFFICIAL）解耦——引擎按 manifest 路由，DB enum 不动。避开 23 caller + 13 测试 + DB migration 的 blast radius。
- **测试等价**：定在 CrawledPostingRecord 层（旧测试零改动跑通），新引擎额外加单元测试（配方 fixture + DSL 校验 + 护栏）。

## license 合规

MIT/BSD（extruct/skills-ml/kalil/eatmoreduck）可引代码。**AGPL（Skyvern/Crawl4AI/Firecrawl/nodriver）连代码都不看**，只读公开概念；DSL 设计自研，commit message 标参考来源 + code review 隔离。kalil CSV 数据合规未核实，不导入。

## 落地（一次性大爆炸 P1，用户选）

P1 范围（原 v3 的 P1+P2+P3 合并，无中间降风险台阶——用户接受）：
1. 引依赖（pyyaml/jg-rp/python-jsonpath/extruct/selectolax）。
2. **DSL 语义实现**：步骤命名空间 + when/foreach/merge + 静态校验 + 护栏原语（XXE/url_filter/json_ld filter/css zip）。
3. `RecipeEngine` + `SelfFetchingAdapter.execute`（扩展 AdapterFetchResult 信号字段，fetch 绑 request）。
4. 翻译 6 站配方：greenhouse（多步）/lever/ashby/official_jsonld/official_sitemap/official_static。
5. `crawl_source` + `crawl_source_with_signals` 一次性切 execute；删 `_fetch_greenhouse_details` + greenhouse 分支。
6. 删 6 adapter 类 + `_ADAPTER_REGISTRY` 动态化 + 处置 `scripts/crawl_jobs.py`（接 RecipeEngine 或废弃）。
7. Tier 2 skill 框架 + 1 个示范 skill。
8. 测试：旧端到端测试零改动跑通（回归门）+ 新引擎单元测试。
9. enum 解耦（OFFICIAL 保留 meta-type，manifest key 独立）。

**后续 spec**：完整 Tier 2 skill 集（国内站）、URL→配方/skill 分类器、Common Crawl 发现、拆独立 repo + submodule。

## 明确不做

- ❌ 不改 `ingest_posting`/`CrawledPostingRecord`/`clean_job_structured_data` 契约（:291-307 映射不变）。
- ❌ 不改 `CrawlSourceType` enum、不做 official 数据迁移（保 meta-type）。
- ❌ 不做完整国内站 skill 集（留后续，CrawlAgent 现已兜底）。
- ❌ 不做 URL 分类器 / Common Crawl / 拆独立 repo（后续）。
- ❌ 不导入 kalil CSV。
- ❌ 不做 execute/list_jobs 双协议分派（一次性切）。

## 已知风险与代价

- **巨型 P1**：大爆炸 + 双轨 = 一次翻译 6 配方 + DSL 引擎 + skill 框架 + 切两条执行路径 + 迁测试。无中间台阶，DSL 设计若返工代价大。建议 P1 内部仍按"DSL→1 配方跑通→批量翻译"小步推进。
- **DSL 是私房语言**：虽基于标准 JSONPath，但步骤命名空间 + when/foreach/merge 是自研扩展，需严格语义文档 + 静态校验保证可移植。
- **LLM skill 子系统是新增设计面**：本 spec 只定框架，CrawlAgent 按 skill 执行的集成细节（skill 如何约束 LLM、确定性程度）留实施时定 + 后续 spec。
- **StaticHtml 分流**：按结构复杂度——简单/中等进 Tier 1（css+zip），复杂（css 不可靠）进 Tier 2 skill。判定标准"css 能否可靠写出选择器"由配方作者把握，spec 不硬编码阈值。
- **回归风险**：6 adapter 生产可用，配方必须逐行为等价（用旧端到端测试做基准）。
