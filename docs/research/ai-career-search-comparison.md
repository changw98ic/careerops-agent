# `MadsLorentzen/ai-job-search` 与 CareerOps 对比及采纳边界

- 调研日期：2026-07-20
- 上游快照：[`MadsLorentzen/ai-job-search` master @ `faa479973aeaa7b8a1463112d088fdefff202961`](https://github.com/MadsLorentzen/ai-job-search/tree/faa479973aeaa7b8a1463112d088fdefff202961)
- 提交时间：2026-07-19T20:38:17+02:00
- 名称说明：用户所说的 `ai-career-search` 在本调研中指向上述 `ai-job-search`。

## 上游源码的实际能力

`ai-job-search` 是本地、Claude Code/CLI 优先的求职申请框架。其
[README](https://github.com/MadsLorentzen/ai-job-search/blob/faa479973aeaa7b8a1463112d088fdefff202961/README.md)
将主链路定义为 `/setup -> /scrape -> /rank -> /apply`：

- [`/setup`](https://github.com/MadsLorentzen/ai-job-search/blob/faa479973aeaa7b8a1463112d088fdefff202961/.claude/commands/setup.md)
  从 `documents/`、CV 或问答生成候选人 profile 和搜索配置。
- [`/scrape`](https://github.com/MadsLorentzen/ai-job-search/blob/faa479973aeaa7b8a1463112d088fdefff202961/.claude/skills/job-scraper/SKILL.md)
  调用 `.agents/skills/*` 下的 portal CLI，必要时用 WebSearch 降级，并按 `seen_jobs.json` 和 tracker 去重。
- [`/rank`](https://github.com/MadsLorentzen/ai-job-search/blob/faa479973aeaa7b8a1463112d088fdefff202961/.claude/commands/rank.md)
  只做批量 triage 评分，更新 `job_scraper/seen_jobs.json`，源码明确不申请岗位。
- [`/apply`](https://github.com/MadsLorentzen/ai-job-search/blob/faa479973aeaa7b8a1463112d088fdefff202961/.claude/commands/apply.md)
  评估匹配，生成定制 CV/求职信，经独立 reviewer 批评与修订，强制编译、可视检查 PDF 并验证 ATS 文本层。
- [`/gmail-sync`](https://github.com/MadsLorentzen/ai-job-search/blob/faa479973aeaa7b8a1463112d088fdefff202961/.claude/commands/gmail-sync.md)
  通过 Gmail MCP/Connector 读取邮件，先给出批量状态变更 proposal，用户批准后才更新 tracker 和 `outcome.md`。
- [`/outcome`](https://github.com/MadsLorentzen/ai-job-search/blob/faa479973aeaa7b8a1463112d088fdefff202961/.claude/commands/outcome.md)
  根据用户描述更新 `job_search_tracker.csv` 和每份申请的归档。

源码中没有找到外发邮件或浏览器 final Submit 的执行流程。`/apply` 在编译、检查和交付材料
后停止；`/gmail-sync` 是收件信号和审批后的本地状态更新，不是 Gmail send。

## 与本项目的差异

| 维度 | `ai-job-search` | 当前 CareerOps / Full Workflow 目标 | 结论 |
| --- | --- | --- | --- |
| 产品形态 | 本地文件 + AI CLI + Bun/Python/LaTeX | Python/FastAPI、中文优先控制台、PostgreSQL/Temporal | 借鉴工作流，不直接移植文件状态机。 |
| 职位发现 | Portal skill/CLI、WebSearch 降级、JSON 去重 | 当前已有受控 crawler 基础；目标是 SourceAdapterRelease + SourceInstance + 调度 | 采纳可替换模式，补足配置、网络、证据和资格合同。 |
| 匹配 | 以候选人上下文进行结构化评价 | 当前有确定性匹配、证据优先草稿和物料哈希绑定 | 应增加可解释的语义评分，但不能让模型直接产生外部写入权限。 |
| 材料 | 简历 PDF、求职信、外联邮件草稿 | 已绑定审核物料引用；实际 PDF/求职信生产仍是后续能力 | 借鉴材料生成体验，保留“事实只能来自候选人资料”的约束。 |
| 表单/发件 | 没有找到 final Submit 或 Gmail send 实现 | 当前真实 Provider 写入未发布；目标在 RQ 后提供 Gmail/Greenhouse 真实执行 | 不能把材料交付误称为投递。 |
| 授权与审计 | 主要依赖项目规则、文件追踪和人工审核 | Campaign Grant、策略结果、版本化载荷、幂等键、审计、回滚/发布门槛 | 当前项目在可证明授权和事故恢复方面更强。 |
| 自动投递 | 未找到 provider execution 和 receipt/reconcile | 当前仅 `.test` synthetic；未来仅经 Grant/RQ 允许低风险动作 | 自主性需由数据库授权和效果证据约束。 |

## 推荐的“更狂野但有界”路线

将代理的自主性放在高收益步骤，并对真实外部效果使用更强的数据库与 Provider 边界：

1. 自动发现、去重、岗位有效性复核、匹配、公司研究、材料草稿和批量排序。
2. 将每个候选岗位编译成不可变的证据包，绑定候选人、目标 URL、来源、材料哈希和策略版本。
3. 人只审核异常、事实/法律声明、敏感字段和 Release Qualification 摘要；普通高置信任务可按
   Campaign Grant 自动推进。
4. 只有已资格化 Gmail/Browser Provider 才能做真实 Send/Submit；每个效果必须有 intent、outbox、
   attempt、receipt/reconciliation 和 audit。

当前实现中的 Manual Application Handoff 仍是所有未知/未资格化站点的安全降级：它不使用浏览器驱动、
Cookie、凭据、Outbox 或 Provider SDK，且不构成 `limited_autopilot` 发布资格。

## 后续采纳优先级

1. **P0：ATS 只读连接器与岗位有效性复核。** 从上游扫描器的接口分层中提取思想，但每个连接器要保存
   来源证据、速率限制、站点条款状态和失败原因。
2. **P0：批量评估队列。** 复用本项目的幂等键、审计与 Campaign Grant，而不是仅以文件名作为并发协调。
3. **P1：事实约束的材料生成。** 将候选人事实、已验证成果和禁止虚构规则作为强制输入契约。
4. **P1：先 Greenhouse、再 Lever/Ashby 的真实投递适配器。** 只有目标站点 policy 明确允许、独立适配器完成
   Release Qualification、隔离凭据、去重/回执对账/故障注入均具备证据后，才进入 review-required 和
   limited-autopilot；CAPTCHA、MFA、法律声明、身份、付款和测评仍强制转人工。

这条路线保留上游的高质量准备体验，并用可撤销授权与可证明效果完成它没有做的最后一公里。
