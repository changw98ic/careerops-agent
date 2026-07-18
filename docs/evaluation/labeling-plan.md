# CareerOps Agent MVP Evaluation Data Workstream（D0）

> 状态：Engineering contracts/tooling in progress; real pilot evidence pending
> 日期：2026-07-18
> 预算：20–35 人日，独立于工程估算

当前 `contracts-only` Gate 只证明静态合同存在；full Gate 仍为 `0/286`
真实 pilot rows。九类真实样本、独立 reviewer/adjudicator、人工双标与裁决、
来源/法律背书和 sealed-holdout custody 均未完成，禁止把工程工具完成写成 pilot
或 Release Qualification 完成。M-1 verifier 的唯一输出 scope 是
`d0_pilot_engineering_consistency`，`release_qualification_allowed` 必须保持
strict `false`，不能授权 release 或 Auto-send。

## 1. 目的与原则

D0 为每个产品指标提供可审计的数据依据。目标流程是在对应能力接受数据资格验收前完成 pilot，并在里程碑验收前冻结 sealed holdout；它不是测试 Fixture 收集，也不能在功能写完后临时拼凑。当前仓库只具备部分工程合同与校验工具，不具备真实数据资格。

- 数据单位、分母、abstain、阈值和错误 taxonomy 先写合同，再标注。
- 按 company/domain/source、email thread/recruiter 或 candidate evidence bundle 分组切分。工程 Gate 拒绝相同 `group_id` 和冻结 provenance key 的精确值跨 split；语义近似、paraphrase 和错误分组仍需 curator 与独立 reviewer 人工检查。
- development 60%、validation 20%、sealed holdout 20% 是目标 split policy；规模不足或类别极不平衡时，在 metric contract 中预先记录例外。除非 Gate 已验证比例和例外，不得把该比例描述为工程强制事实。
- sealed holdout 目标态只通过版本、manifest、hash 和评测结果访问，日常调试不得查看样本内容。当前 repo-local pilot hash 不提供访问隔离、托管、签名或封存证明。
- 发现真值错误时先登记 issue，由裁决者决定；修订必须产生新 dataset version，不能原地改标签保住阈值。

## 2. 最低数据规模

| 数据集 | 最低规模与单位 | 关键覆盖 | 最晚冻结 |
| --- | --- | --- | --- |
| Discovery / Parser | 350 个页面快照 | Greenhouse、Lever、Ashby 各 75；JSON-LD/Sitemap/静态 HTML 75；关闭/变更页 50 | M1 开发前 pilot，M1 Gate 前 holdout |
| Dedup | 600 对 posting pairs | 同岗跨来源、相似标题不同岗位、remote/location hard negatives；hard negatives ≥ 40% | M1 Gate |
| Remote | 400 个 JD/location cases | China eligible/ineligible/unknown、APAC、timezone、雇佣限制、中英双语 | M2 Gate |
| Evidence Match | 250 个 requirement-evidence judgments，覆盖至少 80 个岗位 | strong/partial/transferable/unsupported/hard-fail、无证据断言 | M2 Gate |
| Contact | 250 个页面或 thread contact cases | 官方公开招聘联系、普通员工、猜测地址、域名不匹配、过期证据 | M3 Gate |
| Email | 400 个脱敏招聘 threads | 中英双语；screen/interview/reject/offer/document/salary/visa/unknown；高风险至少 150 | M4 Gate |
| Policy / Injection | 250 个 adversarial cases | 改收件人、越权发送、泄露 secret、网页/邮件/附件指令、Unicode/HTML 混淆 | M4/M6 Gate |
| Calendar | 240 个 scheduling scenarios | DST、半小时/45 分钟时区、CST 歧义、buffer、notice、limit、并发插入 | M5B Gate |
| Reconciliation | 120 个 fault traces | timeout-with-success、before/after-call crash、duplicate receipt、revocation、ambiguous lookup | M5A/M6 Gate |

这些是 MVP 的最低可验收规模，不是永久统计充分性声明。若某关键类别在 holdout 中少于 30 个正例，报告必须单独标记低样本置信，不得只展示汇总分数。

## 3. 来源、隐私与合规

- Discovery、Parser、Remote、Contact 目标上只使用公开可访问且符合 Crawl Policy 的冻结快照；manifest 记录 URL、抓取时间、许可/robots 判断和内容 hash。Verifier 只检查引用与 hash，不判断公开性、robots 结论或来源是否合法。
- Email 只允许使用用户拥有且有权处理的真实招聘线程，先在隔离环境去除姓名、私人地址、电话、会议链接 token、签名和无关历史，再进入标注工具。该权利与去标识化充分性必须由人类复核，不能由技术扫描代替。
- Candidate Evidence 只使用用户明确选择的材料；holdout 不包含 repository secret、客户数据或第三方私有代码。
- 仓库只保存脱敏样本或受控下载 manifest；原始邮件和 PII 不提交 Git。
- synthetic/paraphrase 可补 development 数据与攻击/fault matrix，但 Email、Remote、Contact 的最终质量分数必须来自真实脱敏 holdout。
- 原始数据、标注副本和导出结果分别配置 owner、TTL、删除证明和访问日志。

source/consent/retention 引用及其 SHA-256 是人工 attestation 的完整性载体：
hash 只能证明被引用文件相对 manifest 未变化，不能证明文件内容真实、consent
充分、来源合法或声明者身份可信。v2 技术扫描会在 artifact rows 上重算，整体 Gate
另行要求这些 rows 通过对应 dataset schema；扫描要求
零个未抑制 technical-pattern finding；其 opaque、content/artifact-bound finding
和 suppression evidence 仍只属于技术检查，不是 PII-free、隐私合规或法律许可
证明。

## 4. 标注流程

1. 为九类数据写 schema、labeling guide、正反例、`unknown` 和 `abstain` 规则。
2. 每类先抽取最低最终规模的 10% pilot；至少两人共同复核分歧，修订 guide 后再扩量。当前真实 pilot 为 `0/286`，不得用 synthetic 或测试 fixture 填充。
3. 当前 Gate 以 dataset 粒度执行安全双标：Remote、Email、Policy/Injection、Calendar、Reconciliation 的 pilot rows 100% 双标。若未来只对 hard-false/high-risk/conflict slice 双标，必须先增加 row-level 安全合同和校验。
4. Discovery/Parser、Dedup、Evidence Match、Contact 至少 20% 随机双标；其余单标后由裁决者抽查。随机性与抽查质量属于人工流程证据。
5. 类别标签要求 Cohen's kappa ≥ 0.80；结构化字段/时间标签要求 exact agreement ≥ 0.95。未达标时停止扩量，修订 guide 并重做 pilot。
6. 裁决者只看匿名 sample ID 和证据，不看模型预测。工程 Gate 已要求 actor-bound primary/secondary label 与 final adjudication（final label/value、reason、evidence reference/hash），并重算引用文件 hash；这些只证明工程 actor 结构和引用一致性，不认证真实人类身份或证明裁决质量。
7. 完成 group-aware split，生成包含 schema/guide version、source groups、counts、class distribution、技术扫描和 hashes 的 manifest。当前工程工具已经校验精确 group/provenance collision、derived counts 和 synthetic-only-development；class balance、semantic leakage 与 source truth 仍由人类复核。
8. sealed holdout 交由非日常实现者保管；评测命令只返回聚合指标和受控 opaque error IDs。当前尚无 custody、访问日志、签名或 cryptographic seal 实现，因此该步骤仍未完成。

如果只有一名参与者，安全关键 holdout 不能用“隔几天自己再看一次”替代独立复核；对应 Release Qualification 必须等待第二名复核者完成可认证的签字或等价 attestation。Manifest 中的非空身份字符串本身不构成认证。

## 5. 人日预算

| 工作 | 人日 |
| --- | ---: |
| schema、guide 与 10% pilot | 3–5 |
| 数据收集、授权记录、去重和脱敏 | 5–8 |
| 主标注 | 6–10 |
| 双标、分歧裁决与返工 | 4–8 |
| manifest、PII scan、质量报告与封存 | 2–4 |
| **合计** | **20–35** |

预算包含第二复核者的人日，不包含等待真实招聘邮件自然产生的日历时间。样本不足不是降低 Gate 或扩大 synthetic 占比的理由。

## 6. 与工程里程碑的排期

| 阶段 | D0 交付 | 阻塞对象 |
| --- | --- | --- |
| M-1 | 九类 schema/guide、owner declaration、pilot plan 与 evidence tooling；输出 scope 仅为 `d0_pilot_engineering_consistency`，真实 pilot 仍待执行，`release_qualification_allowed` 保持 strict `false` | 程序级 full D0/M-1 数据资格；不能授权 release、Release Qualification 或 Auto-send；不替代或否定 M0.1–M0.12 的本地工程证据 |
| M0–M1 | Discovery/Parser/Dedup 完整集和 sealed manifest | M1 Gate |
| M1–M2 | Remote/Evidence Match 完整集 | M2 Gate |
| M2–M3 | Contact 完整集 | M3 Gate |
| M3–M4 | Email + Policy/Injection 完整集 | M4 Gate |
| M4–M5A | Reconciliation fault traces | M5A Gate |
| M4–M5B | Calendar scenarios | M5B Gate |
| M6–M7 | 全量重跑、漂移检查、Release Qualification 签字 | M7 release |

数据 curator 可以与工程并行。M0 本地工程验收可单独记录，但任何需要数据资格或发布资格的里程碑，不能在对应真实 pilot/holdout、人工复核和 custody 证据未完成时宣称通过。

## 7. 验收证据

每个 dataset version 至少包含以下三类证据，三者不能互相替代：

工程可派生证据：

- schema、labeling guide 和变更记录；
- artifact/manifest hashes、derived counts、group-aware split 和精确 provenance leakage 结果；
- actor-bound label-review artifact、derived agreement 和 final-adjudication binding；
- 可复现的 v2 technical-pattern scan、零未抑制 findings 及逐条 suppression evidence；
- 评测命令、commit SHA、环境和内容寻址 JSON 结果。Hash 不是签名或不可变存储证明。

人工与运营证据：

- primary/secondary annotator、independent reviewer、adjudicator 的真实身份与角色独立性；
- class balance、semantic/paraphrase leakage、标注正确性和 suppression justification 的复核；
- sealed-holdout custody、访问控制/日志、签字或等价 attestation、删除证明。

来源与法律/隐私证据：

- source/consent/retention manifest 及负责人的真实性背书；
- 数据使用权、Crawl Policy/robots 判断、去标识化充分性和适用法律评估；
- 已知缺口、低样本类别与不得外推的范围。

缺少任一必需项时，数据集只能用于 development，不能用于数据资格、里程碑或发布验收。当前九类数据均为该状态。工程 full Gate 在 `0/286` 时必须保持失败；即使未来 repository evidence 使工程 Gate 通过，M7 仍需要独立可信的人类身份/角色、法律、隐私、custody、安全和产品发布证据；repo-local boolean、path 或 hash 不能替代这些证据。
