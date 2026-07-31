# CareerOps 需求 + 当前进度

> 最后更新：2026-07-30
> 这份文档反映实际代码状态和用户需求，不是规划文档。

## 核心目标

一个自主职业运营助手：agent 自己爬全网岗位、匹配、投递、回复、提醒。
用户只做最终确认。安全护栏不在流程打通前做。

---

## 一、数据采集（爬虫）

### 架构
- **ego**（真浏览器，复用登录态）→ 国内站 + 需要登录的站
- **Playwright**（CDP 网络捕获，headless）→ 国外站 + 公开站
- **LLM agent loop**（MiMo 自主决策）→ 模型看页面、自己决定点/等/滚/抽，适配任意站
- **接口抓包**（hook + replay）→ 有 JSON API 的站直接批量调，快且准

### 已建代码
| 模块 | 文件 | 功能 |
|------|------|------|
| `EgoBrowserTool` | `infrastructure/ego_tool.py` | 程序化控制 ego（navigate/capture_network/fetch_in_page/capture_html/run_page_script） |
| `PlaywrightTool` | `infrastructure/playwright_tool.py` | Playwright CDP 控制（捕获所有网络流量含初始加载） |
| `CrawlAgent` | `application/crawl_agent.py` | 自主爬取策略：API 捕获→翻页 / LLM agent loop / LLM 抽取 |
| `LLMJobExtractor` | `application/llm_job_extraction.py` | MiMo 从 HTML 抽取结构化岗位（5MB 上限） |
| `career_page_finder` | `application/career_page_finder.py` | 从公司域名自动发现招聘页 URL |
| `seed_companies` | `application/seed_companies.py` | 246 个种子域名（~100 国内 + ~100 国外） |
| `crawl_sources` | `application/crawl_sources.py` | 通用 trigger + 源注册表 + CrawlSource 数据类 |

### 已验证
- ✅ 阿里云：100 岗位（API 抓包 → 翻页回放），20 岗位（LLM agent loop）
- ✅ Google：20 岗位（LLM agent loop，模型自主决策）
- ✅ 腾讯：1 岗位
- ✅ career_page_finder：7/10 成功率（google/microsoft/apple/nvidia/bytedance/tencent/netflix）
- ✅ GitHub 域名：998 个（API 搜索上限 1000）
- ✅ ego timeout 120s

### 待做
- [ ] 域名扩展到 ≥ 10,000 家（当前 1,244）
- [ ] 雪球发现（从爬过的页面提取新公司域名）
- [ ] 爬到的岗位接 `ingest_posting` 入库
- [ ] 百度/字节/美团等国内站调通（ego + LLM agent loop）
- [ ] Boss直聘/智联（需 ego 登录态）
- [ ] per-page detail URL（目前部分站回退到列表页 URL）

---

## 二、数据清洗

### 待做（全部未开始）
- [ ] 多语言翻译（JD 中英互译）
- [ ] JD 结构化（职责/要求/薪资/技能拆字段）
- [ ] JD 格式化（统一排版、去噪）

---

## 三、用户数据 + 匹配

### 已有
| 功能 | 状态 |
|------|------|
| SmartIntake（自然语言→profile） | ✅ 后端通了，前端需 rebuild |
| 简历上传/解析（PDF/txt/md） | ✅ 代码在 |
| 简历评审（ResumeReviewService） | ✅ 代码在，模型已通 |
| LLM 匹配（LLMJobMatcher） | ✅ 代码在，模型已通 |
| MiMo 模型接入 | ✅ `.env` 配好 |

### 待做
- [ ] 简历文件 → profile 自动填充的桥
- [ ] 匹配结果 → 收件箱推荐列表的连接（当前断开）
- [ ] SmartIntake 前端 rebuild（`VITE_SMART_INTAKE_ENABLED=true`）

---

## 四、Gmail + 发送

### 已有
| 功能 | 状态 |
|------|------|
| Gmail 真 provider 接 runtime | ✅ `_build_gmail_provider`（token→refresh→GmailSender→GmailSideEffectProvider） |
| 附件解析 | ✅ `path_for_digest` + `_AttachmentPathResolver` |
| 审批 Gate 2 拆除 | ✅ `auto_send_approved` → policy ALLOW |
| Gmail token（gmail.send scope） | ✅ `secrets/gmail_send_token.json`（refresh OK） |
| 能力闸打开 | ✅ config 启动拒绝 + resolver AUTO_SEND 硬拒都拆了 |
| 真实邮件发送 | ✅ 已发到 changw98@126.com（msg ID 19fac3a891f16ef2） |

### 待做
- [ ] 大模型自动回复接入邮件触发（来邮件→模型生成→auto-send）
- [ ] 自动提醒：通知出口（SSE）+ 定时检查循环
- [ ] 自动归档

---

## 五、主动性 loop

### 待做（核心缺口）
- [ ] 行动队列填实（`ActionProjectionBuilder` 当前是空壳）
- [ ] 自驱触发循环（Temporal Schedule）
- [ ] 爬取→匹配链式触发
- [ ] 反思/学习（CareerLoopTrace 指标回喂）

---

## 六、基础设施

### 已有
| 功能 | 状态 |
|------|------|
| MiMo 模型（xiaomi-mimo） | ✅ enabled |
| 能力闸 | ✅ 全打开 |
| tests/conftest.py hermeticity | ✅ 测试不读 .env |
| make verify | ✅ 2149 passed（仅 1 个 Docker 测试 error） |

### 待做
- [ ] prompt 版本管理（当前硬编码）
- [ ] LLM 输出质量评估

---

## 七、明确拒绝的

- ❌ 回滚机制（"流程都没通 管这个干什么"）
- ❌ 兼容/双路径/shim
- ❌ kill switch
- ❌ 安全护栏在流程没通之前做

---

## 八、技术分工

| 场景 | 工具 | 原因 |
|------|------|------|
| 国外公开站 | Playwright (headless) | CDP 捕获一切，无反爬 |
| 国内站（百度/腾讯等） | ego | 真浏览器，不被反爬 |
| 需要登录的站 | ego | 复用用户登录态 |
| LLM 决策（看页面→决定操作） | MiMo via model_gateway | 自主适配任意站 |
| 扩大规模 | 让 LLM 自己处理 | 不人工调试每个站 |
