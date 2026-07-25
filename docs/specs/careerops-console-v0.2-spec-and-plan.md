# CareerOps 控制台修正版 Spec v0.2 与落地计划

- 状态：提案，尚未实施
- 日期：2026-07-25
- 适用范围：Vue 控制台、认证、职位、申请、部署、测试和文档
- 代码基线：`be99d96` 加当前工作树中的未提交前端改动
- 本文目的：把独立审查发现的问题改成可执行、可验收的工程要求

## 0. 先说清楚两个 Bootstrap

- Bootstrap CSS 组件库：移除，不再使用。
- `/bootstrap` 地址：保留，它是首次创建 CareerOps 账号的流程，不能因为移除 CSS 组件库而删除。

正式控制台统一使用 Vue 3、Ant Design Vue 和 AntV G2。旧的 Jinja 页面只能作为迁移期间的兼容实现，不能和 Vue 页面同时作为正式入口。

本文不改变以下安全边界：

- `MODEL_PROVIDER=disabled`；
- Google OAuth 关闭；
- 外部写入、自动发送、自动投递关闭；
- 未知策略动作拒绝；
- Redis 或限流失败时拒绝请求；
- 审计记录只能通过安全函数追加；
- 所有外部镜像和基础镜像继续使用摘要固定；
- `make verify-m1-full` 在真实 D0 数据不足时继续失败，不生成合成数据冒充真实数据。

## 1. 审查结论和修正目标

当前项目的工程骨架较完整，但用户主链路没有接通。已确认的问题包括：

1. 当前运行服务的数据库未就绪，登录无法完成；
2. Vue 模式下 `/bootstrap` 没有真正的初始化页面；
3. 登录后前端读取接口没有带后端要求的防伪令牌；
4. 刷新页面后前端丢失登录状态；
5. 职位详情的申请请求缺少后端要求的候选人 ID；
6. Dockerfile 没有构建或复制 Vue 前端；
7. Compose 没有明确启用 Vue 页面；
8. 前端没有测试，也没有进入 CI；
9. 旧文档对 `/bootstrap`、退出登录和覆盖率的描述已经落后；
10. 后端有图编排和 Temporal 能力，但当前 Vue 页面无法进入审查链路。

修正后的目标是完整走通：

```text
启动项目
  -> 首次创建账号
  -> 登录
  -> 刷新页面仍保持登录
  -> 查看职位列表
  -> 查看职位详情
  -> 加入申请列表
  -> 查看申请状态
  -> 退出并撤销服务端会话
```

任何一步失败，都不能宣称“控制台可用”。

## 2. 正式运行方式

正式运行只允许一种模式：Vue 前端由 Docker 构建并由 API 同域名提供。

```text
Docker / Compose
  ├── Node 构建 frontend/dist
  ├── 把 frontend/dist 复制到 Python 镜像
  ├── CAREEROPS_SERVE_SPA=true
  └── API 同时提供页面、静态资源和 API
```

要求：

- Dockerfile 使用多阶段构建；
- Node 镜像摘要固定；
- 前端构建使用 `npm ci`；
- API 镜像中必须存在 `frontend/dist`；
- Compose 明确设置 `CAREEROPS_SERVE_SPA=true`；
- Vue 页面和 API 使用同一个域名和端口；
- `dist` 缺失时启动失败，不得静默退回旧页面；
- `make verify-compose` 必须检查返回的是 Vue 页面，而不是旧 Jinja 页面。

现有 Dockerfile 未复制前端，必须在实施阶段修正：
`Dockerfile` 的当前复制范围只覆盖 Python 源码和迁移文件。

## 3. 认证和会话 Spec

### 3.1 页面

必须存在：

- `/login`：登录；
- `/bootstrap`：首次创建账号；
- `/dashboard`：登录后的首页；
- `/404`：未知地址。

未知地址不能显示只有菜单、没有正文的空白布局。

### 3.2 登录接口

`POST /api/v1/auth/login`

请求：

```json
{
  "username": "owner",
  "password": "password"
}
```

成功：

```json
{
  "ok": true,
  "authenticated": true,
  "user": {"username": "owner"},
  "csrf_token": "...",
  "expires_at": "2026-07-25T12:00:00Z"
}
```

失败必须区分：

| 情况 | HTTP | 错误码 |
| --- | ---: | --- |
| 账号密码错误 | 401 | `INVALID_CREDENTIALS` |
| 尝试次数过多 | 429 | `RATE_LIMITED` |
| 数据库或 Redis 不可用 | 503 | `DEPENDENCY_NOT_READY` |
| 请求格式错误 | 422 | `VALIDATION_ERROR` |

数据库异常不能被伪装成密码错误。响应必须带 `trace_id`，但不能带密码、token 或数据库详情。

### 3.3 当前会话接口

新增 `GET /api/v1/auth/session`。

未登录返回 200：

```json
{"authenticated": false}
```

已登录返回 200：

```json
{
  "authenticated": true,
  "user": {"username": "owner"},
  "csrf_token": "...",
  "expires_at": "2026-07-25T12:00:00Z"
}
```

前端启动时必须先完成一次会话检查，再决定进入登录页还是业务页面。不能只依赖内存里的 `ref(false)`。

### 3.4 退出接口

`POST /api/v1/auth/logout`

要求：

- 验证当前会话；
- 验证 `X-CSRF-Token`；
- 撤销服务端会话；
- 删除会话 Cookie 和 CSRF Cookie；
- 重复退出安全返回；
- 不能只删除浏览器 Cookie。

### 3.5 首次创建账号

`POST /api/v1/auth/bootstrap`

请求：

```json
{
  "bootstrap_token": "...",
  "username": "owner",
  "password": "password"
}
```

规则：

- 只有数据库中还没有用户时允许；
- token 只能使用一次；
- token 15 分钟过期；
- 失败次数受 Redis 限制；
- 成功后直接建立登录会话；
- 已经存在用户时返回 `409 BOOTSTRAP_CLOSED`；
- bootstrap 过程和账号、候选人档案创建必须在同一个业务事务中完成。

## 4. 防伪令牌规则

认证依赖拆成两种：

### 4.1 读取请求

`GET`、`HEAD` 只检查登录 Cookie。

### 4.2 修改请求

`POST`、`PUT`、`PATCH`、`DELETE` 检查：

- 登录 Cookie；
- `X-CSRF-Token`；
- 当前会话是否有效。

前端客户端必须按请求方法自动附加令牌。这样可以修复当前“前端 GET 不带令牌、后端 GET 强制要求令牌”的冲突。

登录、bootstrap 属于匿名入口，由后端内部建立和轮换预登录会话。

## 5. 候选人和申请 Spec

当前项目是单用户模式，前端不允许猜测或写死候选人 ID。

### 5.1 默认候选人

- bootstrap 成功时创建一个默认候选人档案；
- `console_user` 和默认候选人建立明确关联；
- 后端从当前登录用户解析候选人；
- 前端不再提交 `candidate_id`；
- 后续多候选人能力另行设计，不在本次范围内。

建议增加一个用户到候选人的关联字段或独立关联表，并为其增加 Alembic 迁移、角色权限和回滚测试。

### 5.2 创建申请

`POST /api/v1/applications`

请求：

```json
{
  "canonical_job_id": "...",
  "apply_url": ""
}
```

后端根据当前用户补充候选人。

如果候选人档案不存在或未完成，返回：

```json
{
  "error": {
    "code": "CANDIDATE_PROFILE_REQUIRED",
    "message": "请先完成候选人档案"
  }
}
```

### 5.3 幂等性

- 同一个用户申请同一个职位不能产生两条申请；
- 继续使用 `(candidate_id, canonical_job_id)` 唯一约束；
- 重复点击返回已有申请；
- 重试不得产生重复生命周期事件；
- 已存在申请时返回 200，并明确表示是已有记录。

## 6. 职位和列表接口

统一支持：

```text
GET /api/v1/jobs?q=&state=&limit=&cursor=
GET /api/v1/companies?limit=&cursor=
GET /api/v1/applications?state=&limit=&cursor=
```

统一响应：

```json
{
  "items": [],
  "total": 0,
  "next_cursor": null,
  "has_more": false
}
```

要求：

- 使用 `created_at + id` 稳定排序；
- `total` 表示完整筛选结果数量；
- `next_cursor` 没有下一页时为 `null`；
- 前端不能默认只取 200 条后假装全部加载完；
- 搜索由后端执行；
- OpenAPI、后端模型、前端类型和测试使用同一份契约。

职位详情必须包含：

- canonical job；
- 来源职位；
- 来源 URL；
- 所有版本；
- 当前申请地址；
- 来源状态和职位聚合状态。

## 7. 前端行为 Spec

### 7.1 全局会话状态

会话状态至少包含：

```text
unknown -> checking -> anonymous/authenticated
```

路由守卫必须等待 `checking` 完成，避免刷新时先跳登录页再跳回业务页。

### 7.2 页面要求

登录页：

- 用户名和密码校验；
- 登录中禁用按钮；
- 错误、限流、服务不可用使用不同提示；
- 登录失败保留用户名，不保留密码。

首页：

- 显示系统准备状态；
- 职位、申请数量；
- 最近申请；
- 加载、空数据和错误状态；
- 明确下一步操作。

职位列表：

- 后端搜索；
- 状态筛选；
- 翻页；
- 空结果和失败提示；
- 进入详情。

职位详情：

- 来源 URL；
- 职位描述；
- 申请入口；
- 已申请状态；
- 重复点击保护；
- 失败原因。

未知路径：

- 进入 404 页面；
- 不显示空菜单壳。

### 7.3 可视化

第一阶段只做四类有用图表：

1. 职位状态分布；
2. 职位新增趋势；
3. 申请状态漏斗；
4. 最近申请时间线。

每张图必须有加载、空数据、错误和点击跳转状态。AntV G2 只负责绘图，业务统计由前端数据层负责。

包大小预算：

- 初始 JS gzip 不超过 250KB；
- 单个图表异步包 gzip 不超过 200KB；
- 超出预算时 CI 报错或必须有明确批准。

## 8. 错误格式

业务错误统一使用：

```json
{
  "error": {
    "code": "MACHINE_READABLE_CODE",
    "message": "用户可读的中文提示",
    "retryable": false,
    "trace_id": "..."
  }
}
```

至少覆盖：

- `UNAUTHORIZED`；
- `CSRF_REJECTED`；
- `INVALID_CREDENTIALS`；
- `RATE_LIMITED`；
- `BOOTSTRAP_CLOSED`；
- `CANDIDATE_PROFILE_REQUIRED`；
- `NOT_FOUND`；
- `CONFLICT`；
- `DEPENDENCY_NOT_READY`；
- `VALIDATION_ERROR`。

OpenAPI 必须声明实际会返回的 401、403、409、422、429、503，不能只声明 200 和 422。

## 9. 高可用和安全要求

- 数据库未就绪时 readiness 返回 503；
- 数据库异常不能伪装成空列表或密码错误；
- 读取接口依赖异常时返回明确不可用状态；
- 同步数据库调用不能长期阻塞异步接口；
- Redis 限流继续 fail closed；
- 会话继续使用 HttpOnly、SameSite 和过期时间；
- 登出必须撤销服务端会话；
- 外部写入和自动发送继续关闭；
- 审计、策略、镜像摘要和 D0 安全边界不得削弱。

## 10. 测试和验收要求

### 10.1 后端

- 登录成功、失败、限流、数据库异常；
- session 查询；
- logout 撤销会话；
- bootstrap 成功、过期、重复使用、已关闭；
- GET 不要求 CSRF；
- 修改接口必须要求 CSRF；
- 申请重复请求幂等；
- 职位详情完整；
- 列表分页稳定；
- OpenAPI 状态码和模型正确。

### 10.2 前端

- 登录表单；
- bootstrap 表单；
- 会话恢复；
- 路由守卫；
- logout；
- JobDetail 申请；
- 分页和搜索；
- loading、empty、error；
- 404 页面；
- 图表空数据。

### 10.3 集成验收

必须在一次性 PostgreSQL 和 Redis 上完成：

1. 空数据库启动；
2. migration；
3. bootstrap；
4. login；
5. refresh；
6. jobs；
7. job detail；
8. create application；
9. duplicate application；
10. logout；
11. old session rejected。

`make verify-m1-full` 继续按项目规定保持真实数据门槛，不使用合成数据替代。

## 11. 文档要求

必须同步更新：

- 根 README；
- `frontend/README.md`；
- `docs/runbooks/local-compose.md`；
- API 使用文档；
- M0 验收证据；
- 变更记录。

每份上手文档必须包含：

1. 前置条件；
2. 环境变量；
3. 启动命令；
4. 数据库迁移；
5. bootstrap；
6. 登录；
7. 验证命令；
8. 常见错误；
9. 停止和清理；
10. 当前关闭的能力。

每个命令都要写预期结果，不能只写“执行即可”。

# 落地计划

预计单人 12–18 个工程日，不包含外部数据库准备和真实 D0 数据工作。

## 阶段 0：冻结范围和接口清单

预计 0.5 天。

任务：

- 保留当前未提交改动，不回滚；
- 以 `be99d96` 作为代码基线；
- 确认 Vue 是正式前端；
- 确认 `/bootstrap` 是账号初始化页面；
- 建立前端调用—后端接口对照表；
- 记录登录、CSRF、刷新、Apply、Docker 和文档失败点。

完成标准：所有后续任务都能对应到具体审查问题和验收项。

## 阶段 1：修登录、bootstrap 和会话

预计 2–3 天。

后端任务：

- 增加 `GET /api/v1/auth/session`；
- 修正登录异常分类；
- 实现服务端 logout；
- 增加 Vue 使用的 bootstrap API；
- 拆分读取认证和修改认证；
- 补充认证 OpenAPI 响应；
- 保持 HttpOnly Cookie、轮换会话和 Redis 限流。

前端任务：

- 新增统一 session store；
- 应用启动时检查 session；
- 路由守卫等待检查完成；
- 新增 `/bootstrap` 页面；
- 新增 404 页面；
- 修正退出流程。

验收：空数据库可以 bootstrap；登录成功；刷新不掉登录；logout 后旧会话失效。

## 阶段 2：修 API 契约和申请链路

预计 2–3 天。

任务：

- 增加用户到默认候选人的关联；
- bootstrap 同事务创建用户和候选人；
- 申请接口不再要求前端提交 `candidate_id`；
- 增加申请幂等保护；
- 补齐职位详情的 postings、来源 URL 和版本；
- 列表接口增加稳定分页和后端搜索；
- 统一错误格式；
- 更新 OpenAPI、契约测试和数据库迁移。

验收：点击申请成功；重复点击不重复；没有候选人时有明确提示；分页不重复、不丢失。

## 阶段 3：修 Docker、Compose 和 CI

预计 1–2 天。

任务：

- Dockerfile 增加 Node 构建阶段；
- Node 镜像摘要固定；
- 执行 `npm ci` 和 `npm run build`；
- 复制 `frontend/dist` 到 Python 镜像；
- Compose 设置 `CAREEROPS_SERVE_SPA=true`；
- dist 缺失时启动失败；
- Compose 验证 Vue 页面、bootstrap、404 和静态资源；
- CI 增加前端安装、测试、构建和包大小检查。

验收：标准 Compose 启动后访问到的是 Vue 页面，不是旧 Jinja 页面。

## 阶段 4：修前端业务页面

预计 3–4 天。

任务：

- Jobs 使用后端搜索和分页；
- Companies 和 Applications 使用分页；
- JobDetail 展示来源 URL 和真实描述；
- Apply 使用当前用户候选人；
- 所有页面补 loading、empty、error；
- 统一错误提示；
- 修正移动端菜单；
- 统一中文文案；
- 移除旧 Vite 示例文件；
- G2 按需加载，控制包大小。

验收：登录后的四个主页面均可进入；空数据和失败状态不白屏；手机宽度不挤压主内容。

## 阶段 5：补前端、后端和集成测试

预计 2–3 天。

后端：

- auth API contract tests；
- session 和 logout 测试；
- GET/修改接口 CSRF 测试；
- application 幂等测试；
- pagination 测试；
- OpenAPI 状态码测试。

前端：

- 登录、bootstrap、路由守卫、session 恢复；
- logout；
- JobDetail 申请；
- 分页、搜索和错误状态；
- 404 和图表空数据。

集成：

- 一次性 PostgreSQL；
- Redis；
- Compose；
- 空数据库到完整申请链路。

## 阶段 6：重写文档

预计 1–2 天。

任务：

- README 改成 Vue 正式入口；
- frontend README 写清 bootstrap 和 session；
- runbook 写清数据库未就绪的判断和处理；
- 更新当前测试数量和覆盖率；
- 删除已经不适用的旧 UI 描述；
- 增加 API 请求、响应、错误和重试示例。

完成标准：新开发者能从空数据库按文档完成启动、bootstrap、登录和退出。

## 阶段 7：最终验收和发布判断

执行顺序：

1. 清空一次性数据库；
2. 启动 Compose；
3. 等待 readiness；
4. 生成 bootstrap token；
5. 打开 `/bootstrap`；
6. 创建账号；
7. 登录；
8. 刷新页面；
9. 查看职位列表；
10. 查看职位详情；
11. 创建申请；
12. 重复点击申请；
13. 查看申请列表；
14. 退出；
15. 使用旧会话访问接口；
16. 停止数据库并验证 503 提示；
17. 恢复数据库并验证服务恢复；
18. 执行所有质量门。

最终命令：

```bash
make verify
make security
make verify-db
make verify-compose
cd frontend
npm ci
npm run test:unit
npm run build
```

## 发布前不可妥协的条件

- `/bootstrap` 可用；
- 正确账号能登录；
- 刷新页面不掉登录；
- 登录后职位接口返回 200；
- Apply 不再返回 422；
- 重复申请不会生成重复记录；
- logout 撤销服务端会话；
- Compose 真正运行 Vue 页面；
- 前端测试和构建进入 CI；
- 文档能从零跑通；
- 所有外部写入和自动发送仍然关闭。

在以上条件完成前，Ant Design 和 AntV 只能算视觉改造，不能算产品完成。
