# CareerOps Agent MVP 风险关闭决策

> 状态：Risk decisions frozen
> 日期：2026-07-17
> 适用范围：CareerOps Agent v1 单用户自托管 MVP
> 上位计划：[careerops-agent-mvp-plan.md](careerops-agent-mvp-plan.md)

## 1. 关闭口径

这里的“关闭”不是声称外部平台风险消失，而是满足以下三点：

1. 产品不再等待未决选择；默认行为已经确定。
2. 无法安全满足前置条件时，能力会降级为只读、人工复核或直接禁用。
3. 每项决策都有可执行 Gate；不能用文档说明替代测试或发布证据。

若本文件与上位计划的概括性描述冲突，以本文件的更窄权限和更严格 Gate 为准。

## 2. R-LLM：模型供应商与数据隐私

**决定：运行时默认 `MODEL_PROVIDER=disabled`；模型是可资格化的低权限 adapter，不是系统必需的授权主体。**

### 2.1 固定边界

- 确定性 parser、地域 hard gate、Policy、Approval、状态 reducer 和 Side-effect Worker 在模型关闭时仍可运行。
- 未资格化模型的结果只能进入 `review_required`，不能打开 Policy allow、改变 recipient、扩大 scope 或触发 provider write。
- 生产启用某个 provider 前，必须在 ADR 中冻结 provider/model、区域、数据保留、是否用于训练、成本上限、超时、允许的 outbound hostname 和回归数据集版本。
- 只允许发送经 allowlist 选择并脱敏后的最小片段：公开 JD 文本、用户明确选择的 Candidate Evidence 短片段，以及招聘邮件中完成对邮箱地址、电话、签名和无关历史裁剪后的必要片段。
- 禁止发送原始 Gmail MIME、完整线程/邮箱、附件字节、OAuth token、cookie、Secret Store 内容、身份证件、银行信息或未选择的私人材料。
- 默认不保存原始 prompt/response；只保存 provider/model/prompt 版本、脱敏输入 hash、结构化输出、延迟、token/cost 和 trace ID。为调试临时打开内容日志必须有独立开关、短 TTL 和审计。
- provider、model、prompt、redaction 或 schema 任一版本变化，相关 Release Qualification 立即失效并重跑完整评测。

### 2.2 资格 Gate

- 模型关闭时系统可启动，所有外部写能力保持 fail-closed。
- egress capture 测试证明禁止数据类别离开主机的次数为 0。
- provider 的合同/配置必须支持 API 数据不用于通用模型训练，并满足已冻结的 retention；不能证明则保持 disabled。
- Remote、Evidence Match、Email 和 Prompt Injection 的 sealed holdout 达标后，才允许对应模型 capability 从 `review_only` 升级。
- 模型输出直接造成 Policy allow 或 provider effect 的测试计数为 0。

**已拒绝：** 在 M-1 先拍脑袋锁定某个厂商；让模型获得 Gmail/Calendar 工具；为了质量阈值默认上传完整邮件。

**残余影响：** 模型未资格化时召回率可能较低，但结果只会增加人工复核，不会扩大自动化权限。

## 3. R-GOOGLE：OAuth 分发、Gmail 数据面与公网入口

**决定：MVP 仅支持单用户自托管、用户自备 Google Cloud 项目和 OAuth client，并强制使用专用求职 Gmail；不提供 CareerOps 共享 OAuth client。**

### 3.1 部署与授权模式

- 用户在自己的 Google Cloud 项目创建 External OAuth app，并在长期运行前将 publishing status 设为 In production 的 personal-use 模式。未验证提示可以接受，但安装文档必须准确披露用途、scope、存储和撤销方式。
- External app 的 Testing 状态只用于短期开发。Google 对包含 Gmail/Calendar scope 的 Testing app 通常签发 7 天刷新令牌，因此 Testing 环境不能作为 Watch、soak 或发布验收证据。
- 仓库、镜像和发行包不携带默认 Google client ID/secret；生产启动要求显式配置 `DEPLOYMENT_MODE=single_user_byo` 和 `GOOGLE_OAUTH_PUBLISHING_STATUS=in_production`。后者是 operator attestation，M7 还需保存 OAuth Console 配置核验记录，不能把配置字符串当平台证明。
- 专用求职 Gmail 是 MVP 硬前置；“主邮箱 + label”移到 v1.1，直到完成单独的隐私威胁模型和数据删除演练。
- M4 只增量申请 `gmail.readonly`；M6 单独申请 `gmail.send`；MVP 从不请求 `gmail.compose`。
- Calendar 先申请 `calendar.freebusy`，事件写能力在 M5B 由用户单独授权；授权存在不代表允许执行写入。
- 若未来提供共享 OAuth client、面向其他用户分发或超出 personal-use 范围，OAuth verification、品牌验证以及适用的 restricted-scope security assessment 都成为该版本的发布阻塞项。

### 3.2 Gmail 通知与网络面

- Gmail Watch 使用 Pub/Sub **pull/StreamingPull subscriber**；subscriber 主动出站拉取并 ACK，不部署公网 push webhook。
- 保留每日 Watch 续期、`history.list` 增量同步和周期 reconciliation；Pub/Sub 只是一条可能重复或丢失的提示，不是业务真源。
- 默认部署只暴露经认证的 API/UI。未来若增加 push webhook，必须另开 ADR，补身份/audience/replay/DoS 威胁模型和网络 Gate。

### 3.3 Token 保护

- refresh token 使用 AES-256-GCM envelope encryption；数据行保存 ciphertext、nonce、key ID、scope inventory 和更新时间，不保存主密钥。
- 主密钥来自宿主 OS keychain 或只读 mounted secret file；不得写入仓库、数据库、镜像、Compose literal、普通环境变量、日志或常规备份。
- OAuth credential 只由独立 integration/side-effect worker 通过 opaque handle 访问；API、UI、模型和普通 workflow 不读取明文 token。
- 实现 key rotation、revoke、scope shrink、导出/删除和恢复演练；恢复后必须重新证明 token 解密权限没有扩大。

### 3.4 资格 Gate

- 非开发配置缺少 BYO/In production attestation、发现发行包内置 client 或账户未被声明为专用 Gmail 时，Gmail capability 拒绝进入 ready。
- 默认部署的公网监听端口中不存在 webhook；Pub/Sub duplicate、drop 和 history expiration 测试通过。
- token、authorization code 和 client secret 在 DB dump、日志、trace、错误响应、备份和模型 egress 中的明文命中数为 0。
- 在 In production personal-use 配置上完成跨 7 天边界的 refresh/Watch 观察记录，才能把长期 Gmail 集成标记为 release-qualified；这段平台等待时间不计作工程完成。

官方约束依据：[OAuth token expiration](https://developers.google.com/identity/protocols/oauth2)、[Personal-use app verification](https://support.google.com/cloud/answer/13464323?hl=en)、[Gmail Watch](https://developers.google.com/workspace/gmail/api/guides/push)、[Pub/Sub pull](https://docs.cloud.google.com/pubsub/docs/pull)、[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)。

**已拒绝：** 把 Testing 刷新令牌当长期方案；MVP 读取主邮箱再靠 label 补救；为自托管单用户部署公网 webhook；由项目方托管所有用户的 OAuth token。

**残余影响：** 首次授权可能显示未验证提示；若产品转为多人分发，需要重新做合规和部署设计，不能沿用此豁免。

## 4. R-DATA：Golden Dataset 标注量与质量

**决定：新增独立 D0 Evaluation Data Workstream，预算 20–35 人日；它是发布依赖，不再藏在工程估算里。**

- 数据规模、抽样、分组切分、双标、裁决、隐私和里程碑见 [labeling-plan.md](../../docs/evaluation/labeling-plan.md)。
- 工程工作仍为 112–156 人日；含 D0 后总项目工作量为 **132–191 人日**。
- 单人顺序执行的纯工作量为约 27–39 周；加入集成等待与返工后的现实日历预算为 **30–44 周**。
- 若有独立标注/复核者与 M1–M4 并行，工程关键路径仍可维持 24–36 周，但总人力投入不变。
- 每个能力在开始实现前先完成 10% pilot；进入里程碑 Gate 前必须冻结对应 sealed holdout manifest。
- 安全关键标签 100% 双标并裁决；其他集合至少 20% 双标。没有第二名复核者时，安全关键 holdout 不能签字放行。
- synthetic 数据可用于开发和 fault matrix，但不能替代 Email、Remote、Contact 等真实脱敏 holdout。

**已拒绝：** 功能开发完成后再找样本；把 synthetic 数据当最终准确率证据；修改 holdout 或 mock 让阈值通过。

**残余影响：** 真实招聘邮件和边界样本的收集速度不可保证；缺数据时对应 capability 保持 `review_only`，里程碑不虚假通过。

## 5. R-CALENDAR：FreeBusy 与事件写入的 TOCTOU

**决定：MVP 永久保留人工创建动作；Calendar write 只能由用户点击“重新验证并创建”触发，并写入专用 `CareerOps Interviews` 日历。**

### 5.1 执行协议

1. Schedule Proposal 绑定 slot、所有被检查的 calendar IDs、规则版本、payload hash 和过期时间。
2. 用户明确点击后，系统获取本地 per-calendar lease，并对所有配置日历重新调用 FreeBusy。
3. 系统按执行时刻重算 timezone、DST、notice、buffer、每日/每周上限和 proposal version；任一变化都使旧 approval 失效。
4. FreeBusy 结果在 provider insert 前超过 10 秒就重新查询；无法在 freshness budget 内完成则 fail closed。
5. 事件只写到专用日历，不带外部 attendees；adapter 明确禁止发送更新，并通过 Google sandbox contract test 固定实际行为。
6. 写后立即 reconcile，并执行延迟复核。若发现外部并发事件，转入 `schedule_conflict_review`、停止自动确认邮件且提示人工处置；系统不自动删除或改动现有事件。
7. M6 的面试确认邮件必须绑定 post-write green receipt；Calendar 状态 unknown、stale 或 conflict 时不能发送。

### 5.2 正确验收口径

- 对执行前已经可见的冲突、过期 proposal、超限和缺 buffer，事件创建数必须为 0。
- 对故意插入在“最后一次 FreeBusy 返回之后、insert 之前”的不可消除外部竞态，要求 detection/reconciliation = 100%、自动确认邮件 = 0、进入人工队列 = 100%；不再伪称冲突事件永远为 0。
- 重复点击、timeout-with-success、after-call-before-commit crash 的重复 Event 数为 0。
- MVP 的无人值守 Calendar Event 创建数为 0；每次 write 都有当前用户动作、Policy、Intent、Receipt 和 Audit。

Google Calendar 提供独立的 [FreeBusy query](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query) 与 [Events insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert)，没有跨两次请求的原子空闲占位。本协议关闭的是自动化误承诺和通知风险，而不是声称消除了外部并发。

**已拒绝：** recruiter 邮件一确认就后台自动建事件；写入主日历；用本地锁声称锁住 Google Calendar；把 `sendUpdates` 当成唯一邀请防线。

**残余影响：** 极窄窗口仍可能出现外部并发事件；产品以检测、停止后续发送和人工处置控制后果。

## 6. 风险关闭 Gate 总表

| ID | 默认状态 | 解锁条件 | 失败行为 |
| --- | --- | --- | --- |
| R-LLM | provider disabled | 隐私 ADR + egress test + 对应 holdout | review-only |
| R-GOOGLE | BYO / dedicated account / pull | In production personal-use + token/Watch/retention tests | integration not-ready |
| R-DATA | capability unqualified | pilot、双标、sealed manifest、指标达标 | 不通过里程碑 |
| R-CALENDAR | human-trigger only | fresh preflight + current approval + write scope | refuse/review queue |

四项 Gate 都必须进入 Release Qualification；配置或版本变化使相关资格失效，不能靠 UI 开关绕过。
