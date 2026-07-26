## Why

CareerOps 当前已经具备职位采集、匹配、申请状态、Gmail 同步、邮件草稿和副作用安全内核的分散基础，但用户还不能沿着一条清晰的主流程完成“发现职位 → 准备材料 → 投递 → 跟踪回复 → 继续行动”。本变更把这些能力收敛成一个以申请为中心、证据驱动、人工确认的端到端求职工作流。

现在需要明确这条产品主线，是因为继续扩展 Agent、采集源或发送能力而没有统一的用户旅程，会让后端能力不断增加，却无法证明用户真正完成了一次可靠的申请闭环。

## What Changes

- **BREAKING（产品范围）**：将当前仅偏向发现、手动申请和 Gmail 只读同步的范围扩展为受控的申请工作区；允许通过可信招聘邮箱发起人工确认后的邮件投递，但仍禁止无人值守海投和默认自动发送。
- 建立用户求职画像、筛选偏好和简历版本管理，作为采集、匹配和申请材料的共同输入。
- 建立可由用户管理的爬取计划：数据源、主题/关键词、地点与远程条件、频率、时间窗、运行范围、限额、暂停和运行历史。
- 将职位采集、去重、硬过滤、证据匹配和推荐结果汇聚为用户可操作的职位收件箱。
- 建立申请工作区：收藏职位、选择申请渠道、选择简历版本、生成岗位定制申请包、展示修改差异并人工确认。
- 支持两类投递路径：可信招聘邮箱的审核后邮件投递，以及官方外部申请页的用户手动完成并回填。
- 建立招聘邮件同步、线程关联、场景理解和申请状态建议；拒绝、面试、测评、补充材料、Offer 等事件写入申请时间线。
- 建立回复草稿、跟进提醒和人工审批队列；高风险回复永远不得自动发送，低风险自动发送也必须保持默认关闭并受现有资格、策略和 kill switch 约束。
- 在前端增加围绕“职位收件箱、申请工作区、跟进中心、采集计划、简历与偏好”的用户界面，而不是暴露 Agent 图作为主导航。
- 为所有重要状态变更保留来源、证据引用、版本、决策和审计记录；保持默认拒绝、幂等、可恢复和敏感数据最小化原则。

## Capabilities

### New Capabilities

- `career-profile-and-resume`: 管理求职目标、筛选偏好、候选人证据、简历版本和岗位定制材料。
- `crawl-plan-management`: 管理职位数据源、采集主题、范围、频率、限额、运行历史和安全策略。
- `job-discovery-inbox`: 将采集职位规范化、去重、筛选、匹配并呈现为可操作的职位收件箱。
- `application-workspace`: 以申请为中心管理收藏、材料、渠道、人工确认、申请状态和事件时间线。
- `email-application-delivery`: 通过可信招聘邮箱发送初始申请或后续邮件，并通过副作用内核执行审批、幂等和回执处理。
- `recruiting-email-intelligence`: 同步招聘邮箱，理解邮件线程，关联申请并提出结构化状态和下一步建议。
- `reply-draft-and-follow-up`: 生成回复草稿、跟进提醒和审批任务，支持用户确认后发送。

### Modified Capabilities

<!-- No existing OpenSpec capability specs exist in this repository. Existing ADR and code contracts are addressed in the design and impact sections. -->

## Impact

- **Domain and application**：`domain/jobs.py`、`domain/applications.py`、`domain/email.py`、`domain/candidates.py`、`application/crawl_policy.py`、`application/matching.py`、`application/resume_analysis.py`、`application/email_sync.py`、`application/gmail_send.py`、`application/side_effect_kernel.py`。
- **Persistence**：新增或扩展用户偏好、爬取计划/运行、申请包、投递尝试、邮件事件、回复草稿和跟进任务的 PostgreSQL 表、约束、迁移与仓储。
- **API and UI**：新增采集计划、候选人/简历、职位操作、申请工作区、邮件线程、状态建议、回复审批和跟进接口；扩展 Vue SPA 路由和页面。
- **Integrations**：官方 ATS/公司招聘源、Gmail 只读同步和受控 Gmail 发送；所有外部写入继续经过策略、审批、Outbox、Side-effect Worker、回执和审计链。
- **Security and policy**：需要更新 `docs/adr/0001-mvp-scope.md` 的范围记录，并以新的 ADR/威胁模型增量明确岗位定制材料与邮件投递边界；不得启用 `CAREEROPS_EXTERNAL_WRITES_ENABLED`、`CAREEROPS_AUTO_SEND_ENABLED` 或 `CAREEROPS_GOOGLE_OAUTH_ENABLED` 作为本变更的默认结果。
- **Verification and data**：新增端到端契约、状态机、来源证据、Prompt Injection、重复投递、超时回执、邮件误关联、隐私留存和 UI 状态测试；真实 D0 pilot 仍不能用合成数据替代。
