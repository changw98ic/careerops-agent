# 移除控制台登录 + 多 candidate 路径参数化 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除本地工具不需要的控制台登录，把 `candidate` 升为顶级实体、通过路径参数 `/api/v1/candidates/{cid}/...` 切换"整套环境"，前后端 + 后台 worker 全部适配。

**Architecture:** candidate 已是数据模型里的业务实体（`candidates` 表，owner_id/candidate_id 已统一为同一 FK），数据基本已 per-candidate。本变更只改"当前 candidate 的来源"（session → 路径参数）+ 删除鉴权层 + 后台遍历所有 candidate。不动 schema/domain。agent 护栏（能力开关/A-B/outbox/审计/预算）全留，唯一例外是 review 端点要去掉 session/CSRF。

**Tech Stack:** Python 3.12 / FastAPI / Temporal / SQLAlchemy-Postgres / Vue 3 + vue-router / pytest。

## Global Constraints

- 代码搜索用 CodeGraph（项目规则）；禁 rg/grep/find。
- 不动 schema、不动 domain model（`candidates` 表、owner_id/candidate_id 列原样）。
- agent 护栏保留；`require_capability` 不依赖登录（已核实），不用改。
- 路径里的 `candidate_id` 同时当 crawl 的 `owner_id` 传给 service（service 签名不改）。
- 全局路由不路径化：`/api/v1/jobs`、`/api/v1/companies`、`/api/v1/health/*`、`/api/v1/metrics`、`/api/v1/candidates`（列表/新建）、review 端点。
- 部署时 API 和 worker 同步上线（避免路径参数 vs 旧 owner 解析的版本不一致）。
- 每个任务结束跑相关测试 + commit。

## File Structure

**Create:**
- `src/careerops/application/candidate_service.py` — 候选 CRUD 服务（如无现成）。
- `src/careerops/infrastructure/database/postgres_candidate_repo.py` — 候选 Postgres 仓库（如无现成）。
- `src/careerops/api/routes/candidates.py` — 候选 CRUD 路由。
- `migrations/versions/0039_drop_auth_tables.py` — drop `console_users` / `auth_sessions`。
- `frontend/src/stores/candidate.js` — 当前 candidate selector 状态。
- `frontend/src/components/CandidateSelector.vue` — 顶部切换器。
- `tests/unit/test_candidate_*` — 对应测试。

**Delete:**
- `src/careerops/auth/`（service / contracts / rate_limit / crypto）
- `src/careerops/infrastructure/auth.py`
- `src/careerops/web/api_auth.py`
- `src/careerops/cli/auth.py`
- `tests/unit/test_api_auth*.py`（6 个）
- `frontend/src/views/Login.vue`、`frontend/src/views/Bootstrap.vue`

**Modify:**
- `src/careerops/api/auth_dependency.py` — 删鉴权依赖；`require_candidate_id` 改路径版。
- `src/careerops/api/app.py` — 去 auth 装配；挂 candidate 路由；review 适配。
- `src/careerops/api/routes/review.py`（或 `orchestration/review.py`）— 去 session/CSRF，actor=local-reviewer。
- `src/careerops/application/loop_bootstrap.py` — 遍历所有 candidate。
- 18 个 per-candidate 路由文件 — 路径参数化（见阶段 2 批次）。
- `frontend/src/api/client.js` — 全部 per-candidate URL 带 `{cid}`；删 CSRF。
- `frontend/src/router.js` — 删登录守卫；加 candidate 前缀路由（可选）。
- `frontend/src/stores/session.js` — 删 login/bootstrap，或整个删除。

---

## 阶段 1 — candidate 顶级实体 + 路径依赖

### Task 1: CandidateService + PostgresCandidateRepository

**Files:** Create `src/careerops/application/candidate_service.py`, `src/careerops/infrastructure/database/postgres_candidate_repo.py`; Test `tests/unit/test_candidate_service.py`

**Interfaces:**
- Produces: `CandidateService.list_all() -> list[Candidate]`, `create(display_name) -> Candidate`, `get(candidate_id) -> Candidate | None`。仓库实现 `list_all/create/get` 针对 `candidates` 表。

- [ ] **Step 1 — 先核实再写测试。** 用 CodeGraph 查 `CandidateService` / `PostgresCandidateRepository` 是否已存在。**若已存在**，跳到 Task 2。**若不存在**，写失败测试：

```python
# tests/unit/test_candidate_service.py
from uuid import uuid4
from careerops.application.candidate_service import CandidateService

class _FakeRepo:
    def __init__(self): self.rows = []; self._by_id = {}
    def list_all(self, *, limit=50): return list(self.rows)[:limit]
    def create(self, candidate): self.rows.append(candidate); self._by_id[candidate.id] = candidate; return candidate
    def get(self, cid): return self._by_id.get(cid)

def test_create_and_list():
    svc = CandidateService(_FakeRepo())
    c = svc.create("Alice")
    assert c.display_name == "Alice"
    assert svc.list_all() == [c]
    assert svc.get(c.id) is c
```

- [ ] **Step 2:** `pytest tests/unit/test_candidate_service.py -q` → FAIL（模块不存在）。
- [ ] **Step 3 — 实现：**

```python
# src/careerops/application/candidate_service.py
from __future__ import annotations
from uuid import uuid4
from datetime import datetime, UTC
from careerops.domain.candidates import Candidate

class CandidateService:
    def __init__(self, repository): self._repo = repository
    def list_all(self, *, limit: int = 50) -> list[Candidate]:
        return self._repo.list_all(limit=limit)
    def create(self, display_name: str) -> Candidate:
        c = Candidate(id=uuid4(), display_name=display_name, created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        return self._repo.create(c)
    def get(self, candidate_id): return self._repo.get(candidate_id)
```

```python
# src/careerops/infrastructure/database/postgres_candidate_repo.py
from __future__ import annotations
from uuid import UUID
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from careerops.domain.candidates import Candidate
from careerops.infrastructure.database.schema import candidates

class PostgresCandidateRepository:
    def __init__(self, engine: Engine): self._engine = engine
    def list_all(self, *, limit: int = 50) -> list[Candidate]:
        with self._engine.begin() as conn:
            rows = conn.execute(sa.select(candidates).order_by(candidates.c.created_at).limit(limit)).mappings().all()
        return [_row(r) for r in rows]
    def create(self, c: Candidate) -> Candidate:
        with self._engine.begin() as conn:
            conn.execute(candidates.insert().values(id=c.id, display_name=c.display_name))
        return self.get(c.id)
    def get(self, cid: UUID) -> Candidate | None:
        with self._engine.begin() as conn:
            r = conn.execute(sa.select(candidates).where(candidates.c.id == cid)).mappings().first()
        return _row(r) if r else None

def _row(r) -> Candidate:
    return Candidate(id=r["id"], display_name=r["display_name"], created_at=r["created_at"], updated_at=r["updated_at"])
```

- [ ] **Step 4:** `pytest tests/unit/test_candidate_service.py -q` → PASS。
- [ ] **Step 5:** commit `feat(auth-rm): candidate service + repository`。

---

### Task 2: candidate CRUD 路由

**Files:** Create `src/careerops/api/routes/candidates.py`; Modify `src/careerops/api/app.py`（挂路由 + 在 RuntimeResources/`probe` 上暴露 candidate repo → service）；Test `tests/unit/test_candidates_routes.py`

**Interfaces:**
- Produces: `GET /api/v1/candidates`（列表）、`POST /api/v1/candidates`（新建，body `{display_name}`）、`GET /api/v1/candidates/{candidate_id}`（详情）。无 candidate 路径前缀（全局）。

- [ ] **Step 1 — 失败测试：**

```python
# tests/unit/test_candidates_routes.py
from fastapi.testclient import TestClient
from careerops.api.app import create_app
from careerops.config import Settings

def _app():
    return create_app(Settings(environment="test"), readiness_probe=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock())

def test_list_create_get_candidate():
    app = _app()
    # 注入一个内存 candidate service（route 通过 app.state.candidate_service 取）
    from careerops.application.candidate_service import CandidateService
    class _Repo:
        def __init__(self): self.r=[]; self.b={}
        def list_all(self,*,limit=50): return list(self.r)
        def create(self,c): self.r.append(c); self.b[c.id]=c; return c
        def get(self,i): return self.b.get(i)
    app.state.candidate_service = CandidateService(_Repo())
    c = TestClient(app)
    r = c.post("/api/v1/candidates", json={"display_name": "Alice"})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert c.get("/api/v1/candidates").json()["items"][0]["id"] == cid
    assert c.get(f"/api/v1/candidates/{cid}").status_code == 200
```

- [ ] **Step 2:** FAIL。
- [ ] **Step 3 — 实现：**

```python
# src/careerops/api/routes/candidates.py
from __future__ import annotations
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/candidates", tags=["candidates"])

class CreateCandidateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)

class CandidateResponse(BaseModel):
    id: str
    display_name: str

class CandidateListResponse(BaseModel):
    items: list[CandidateResponse]

def _service(request: Request):
    svc = getattr(request.app.state, "candidate_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="candidate service not ready")
    return svc

@router.get("", response_model=CandidateListResponse)
async def list_candidates(service=Depends(_service)):
    return CandidateListResponse(items=[CandidateResponse(id=str(c.id), display_name=c.display_name) for c in service.list_all()])

@router.post("", response_model=CandidateResponse, status_code=201)
async def create_candidate(body: CreateCandidateRequest, service=Depends(_service)):
    c = service.create(body.display_name)
    return CandidateResponse(id=str(c.id), display_name=c.display_name)

@router.get("/{candidate_id}", response_model=CandidateResponse)
async def get_candidate(candidate_id: UUID, service=Depends(_service)):
    c = service.get(candidate_id)
    if c is None: raise HTTPException(status_code=404, detail="candidate not found")
    return CandidateResponse(id=str(c.id), display_name=c.display_name)
```

在 `create_app`（`isinstance(probe, RuntimeResources)` 块内）挂：`from careerops.application.candidate_service import CandidateService`；`from careerops.infrastructure.database.postgres_candidate_repo import PostgresCandidateRepository`；`app.state.candidate_service = CandidateService(PostgresCandidateRepository(probe.database))`；并在路由注册处 `app.include_router(candidates.router)`。

- [ ] **Step 4:** PASS。
- [ ] **Step 5:** commit `feat(auth-rm): candidate CRUD routes`。

---

### Task 3: 路径参数依赖 `path_candidate_id`

**Files:** Modify `src/careerops/api/auth_dependency.py`; Test `tests/unit/test_path_candidate_id.py`

**Interfaces:**
- Produces: `async def path_candidate_id(candidate_id: UUID) -> UUID` —— 从 FastAPI 路径参数取 `candidate_id`，校验存在性（404 若不存在），返回。这是 `require_candidate_id` 的替代（路径版）。

- [ ] **Step 1 — 失败测试：**

```python
# tests/unit/test_path_candidate_id.py
import asyncio, uuid
from unittest.mock import MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from careerops.api.auth_dependency import path_candidate_id

def test_path_candidate_id_returns_id_when_exists():
    cid = uuid.uuid4()
    svc = MagicMock(); svc.get.return_value = MagicMock(id=cid)
    app = FastAPI()
    app.state.candidate_service = svc
    @app.get("/api/v1/candidates/{candidate_id}/probe")
    async def probe(candidate_id=...): 
        from fastapi import Depends
        async def _dep(): return await path_candidate_id(candidate_id, request=None)
        return {"ok": True}
    # 简化：直接测函数行为
    assert asyncio.run(path_candidate_id(cid, candidate_service=svc)) == cid
```

> 注：测试需校验"存在→返回 id；不存在→404"。`path_candidate_id` 需访问 `app.state.candidate_service` 校验存在性。实现时通过 `request: Request` 取 service。

- [ ] **Step 2:** FAIL。
- [ ] **Step 3 — 实现（在 auth_dependency.py 追加，保留旧 require_candidate_id 暂时并存，阶段 3 删）：**

```python
from fastapi import HTTPException
from careerops.api.errors import NotFoundError

async def path_candidate_id(candidate_id: UUID, request: Request) -> UUID:
    """从路径参数解析 candidate_id 并校验存在性（本地无鉴权下的身份来源）。"""
    svc = getattr(request.app.state, "candidate_service", None)
    if svc is None:
        raise DependencyNotReadyError("candidate service not ready")
    if svc.get(candidate_id) is None:
        raise NotFoundError(f"candidate {candidate_id} not found")
    return candidate_id
```

- [ ] **Step 4:** PASS。
- [ ] **Step 5:** commit `feat(auth-rm): path_candidate_id dependency`。

---

## 阶段 2 — 逐 router 路径迁移（按批次）

每个批次 task 用**同一模式**：把 router 的 `prefix` 改成 `/api/v1/candidates/{candidate_id}/...`，handler 的 `candidate_id: Annotated[UUID, Depends(require_candidate_id)]` 替换为路径参数 `candidate_id: UUID`（来自 `path_candidate_id`）。**代表性完整代码见 Task 4；其余批次同模式应用**（不是占位符——模式确定、每批只换 prefix + handler 签名 + 测试 URL）。

### Task 4: 迁移批次 A — profile + evidence（带完整示例）

**Files:** Modify `src/careerops/api/routes/profile.py`, `src/careerops/api/routes/evidence.py`; Tests `tests/unit/test_section3_routes.py`, `test_section3_security.py`

**迁移模式（所有批次通用）：**

```python
# 之前
router = APIRouter(prefix="/api/v1", tags=["profile"])

@router.get("/profile")
async def get_profile(candidate_id: Annotated[UUID, Depends(require_candidate_id)], ...):
    ...

# 之后
router = APIRouter(prefix="/api/v1/candidates/{candidate_id}", tags=["profile"])

@router.get("/profile")
async def get_profile(candidate_id: UUID, ...):   # 来自路径；如需存在性校验加 Depends(path_candidate_id)
    ...
```

- [ ] **Step 1 — 测试改 URL：** 把对应测试里的 `/api/v1/profile`、`/api/v1/evidence` 改成 `/api/v1/candidates/{cid}/profile` 等（candidate 用测试 fixture 已有的 cid）。
- [ ] **Step 2:** `pytest tests/unit/test_section3_routes.py tests/unit/test_section3_security.py -q` → FAIL（旧 URL 404）。
- [ ] **Step 3 — 改 router prefix + handler 签名**（profile.py、evidence.py）。
- [ ] **Step 4:** PASS。
- [ ] **Step 5:** commit `refactor(auth-rm): path-param profile+evidence routes`。

### Task 5: 迁移批次 B — crawl_plans + crawl_runs + crawl_sources + crawl_permissions
- [ ] 同模式。prefix `/api/v1/candidates/{candidate_id}/crawl/...`；`candidate_id` 当 `owner_id` 传 service。改 `tests/contract/test_section4_crawl_plans.py`、`test_section5_crawl_slice.py` 的 URL。commit。

### Task 6: 迁移批次 C — applications + application_workspace + email_payloads + reply_drafts + system_send
- [ ] 同模式。改 `test_section7/8/9/10/13` 的 URL。commit。

### Task 7: 迁移批次 D — mail_sync + mail_intelligence + inbox + matching + notifications
- [ ] 同模式。注意 notifications 的 SSE 端点也带 `{candidate_id}`。改对应测试 URL。commit。

### Task 8: 迁移批次 E — agent_console + agent_runs + smart_intake
- [ ] 同模式。commit。

> 每批后跑 `pytest tests/unit tests/contract -q` 确保没有遗漏的旧 URL 测试。

---

## 阶段 3 — 删登录层

### Task 9: 删鉴权依赖 + require_candidate_id 收尾

**Files:** Modify `src/careerops/api/auth_dependency.py`；所有还在用 `require_api_auth` / `require_web_auth` / `require_candidate_id` 的路由（阶段 2 后应已切到 `path_candidate_id`）

- [ ] **Step 1:** CodeGraph 搜剩余 `require_api_auth` / `require_web_auth` / 旧 `require_candidate_id` 调用点。
- [ ] **Step 2:** 删 `require_api_auth`、`require_web_auth`、`AuthenticatedPrincipal` 引用、`reject_candidate_substitution`、旧 `require_candidate_id`（session 版）。`path_candidate_id` 成为唯一身份依赖。
- [ ] **Step 3:** `create_app` 删 `auth_service` / `web_settings` 装配。
- [ ] **Step 4:** `pytest tests/unit -q` → 修掉因删依赖导致的 import 错误。
- [ ] **Step 5:** commit `refactor(auth-rm): drop session/csrf auth dependencies`。

### Task 10: review 端点适配（先去 auth 依赖，再删模块）

> **pre-flight 顺序修正**：review 端点依赖 `careerops.auth.contracts.AuthenticatedPrincipal` 与 `ReviewAuthService.authenticate`。必须**先**去掉这些依赖（本 task），**再**删 auth 模块（Task 11），否则 import 断裂。所以本 task 排在删模块之前。

- [ ] 改 `api/routes/review.py` / `orchestration/review.py`：去掉 `ReviewAuthService.authenticate` + `validate_csrf` 调用 + 对 `AuthenticatedPrincipal` 的 import；审批者 actor 用固定 `local-reviewer`；审计写入时 `actor_type=USER`、actor=`local-reviewer`。保留 A/B 兜底的人审动作。
- [ ] 改对应 review 测试（去 session/CSRF fixture）。
- [ ] `pytest tests/unit -q` 通过。
- [ ] commit `refactor(auth-rm): review endpoint trusts loopback, local-reviewer actor`。

### Task 11: 删 auth 模块 + 路由 + auth 测试（合并）

> review 已不再依赖 auth（Task 10），现在可以删整个 auth 子系统。模块和它的测试必须**同时**删——模块删了，6 个 auth 测试必然 import 失败。

- [ ] 删 `src/careerops/auth/`、`src/careerops/infrastructure/auth.py`、`src/careerops/web/api_auth.py`、`src/careerops/cli/auth.py`；删 `app.py` 里 `/api/v1/auth/*` 路由注册 + `RedisAuthRateLimiter`（auth 用途）。
- [ ] 同时删 auth 测试：`tests/unit/test_api_auth.py`、`test_api_auth_bootstrap.py`、`test_api_auth_csrf.py`、`test_api_auth_login.py`、`test_api_auth_logout.py`、`test_api_auth_session.py`。
- [ ] `pytest tests/unit -q` 全绿（无残留 import）。
- [ ] commit `refactor(auth-rm): delete auth subsystem + tests`。

### Task 12: drop auth 数据表迁移

**Files:** Create `migrations/versions/0039_drop_auth_tables.py`

- [ ] **Step 1 — 先核实外键：** CodeGraph 查 `console_users` / `auth_sessions` 是否被其他表 FK 引用（尤其 candidate 是否有 FK 到 console_users）。
- [ ] **Step 2 — 若无 FK 阻塞，写迁移：**

```python
# migrations/versions/0039_drop_auth_tables.py
"""drop console_users and auth_sessions tables

Revision ID: 0039
Revises: 0038
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa

revision = "0039"; down_revision = "0038"; branch_labels = None; depends_on = None

def upgrade() -> None:
    op.drop_table("auth_sessions", schema="careerops")
    op.drop_table("console_users", schema="careerops")

def downgrade() -> None:
    # 不恢复（旧 auth 模型已删）。保留空 downgrade。
    pass
```

- [ ] **Step 3:** 在 compose 跑迁移验证（`docker compose run --rm migration`）。
- [ ] commit `feat(auth-rm): drop auth tables (0039)`。

---

## 阶段 4 — 后台 worker 多 candidate 化

### Task 13: loop_bootstrap 遍历所有 candidate

**Files:** Modify `src/careerops/application/loop_bootstrap.py`; Test `tests/unit/test_loop_bootstrap.py`

**Interfaces:** `_resolve_owner_id` 删除；新增 `_list_all_candidates(engine) -> list[UUID]`；mail-sync / crawl 对每个 candidate 各注册/激活。

- [ ] **Step 1 — 改测试：** 现有 `test_loop_bootstrap.py` 的 owner 解析断言改成"遍历多 candidate"。新增 `test_crawl_activated_for_each_candidate`、`test_mail_sync_schedule_per_candidate`（fake manager 记录 `crawl:{cid}:*` / `mail-sync:{cid}` 各一份）。
- [ ] **Step 2:** FAIL。
- [ ] **Step 3 — 实现：** mail-sync 遍历所有 connected mail account（`SELECT candidate_id, id FROM email_accounts`）各注册 `mail-sync:{candidate_id}`；crawl 遍历所有 candidate（`SELECT id FROM candidates`）逐个 `select_sources(cid)` + `activate_crawl_schedules`。outbox/sweep 保持全局。
- [ ] **Step 4:** PASS。
- [ ] **Step 5:** commit `feat(auth-rm): bootstrap iterates all candidates`。

---

## 阶段 5 — 前端

### Task 14: 删登录前端

**Files:** Delete `frontend/src/views/Login.vue`、`Bootstrap.vue`；Modify `frontend/src/router.js`（删登录守卫 + /login、/bootstrap 路由）、`frontend/src/api/client.js`（删 CSRF token 逻辑）、`frontend/src/stores/session.js`（删 login/bootstrap/preauth/logout，或整个删）；`frontend/tests/session.test.js`

- [ ] 删文件 + 改 router/client。session store 改为只暴露"当前 candidate"（见 Task 15）或删除。
- [ ] `npm -C frontend run test` + `npm -C frontend run build` 通过。
- [ ] commit `refactor(auth-rm): drop login UI + CSRF`.

### Task 15: candidate selector + API client 路径化

**Files:** Create `frontend/src/stores/candidate.js`、`frontend/src/components/CandidateSelector.vue`; Modify `frontend/src/api/client.js`（每个 per-candidate 方法拼 `candidates/${cid}/...`）、`frontend/src/App.vue`（挂 selector）

**Interfaces:** `stores/candidate.js` 暴露 `currentCandidateId`（localStorage 持久化）、`candidates` 列表、`load()`、`switch(cid)`、`create(name)`。`client.js` 所有 per-candidate 方法接受/注入当前 cid。

- [ ] **Step 1 — 测试：** `frontend/tests/candidate.test.js` —— mock `/api/v1/candidates`，断言 switch(cid) 后 `api.listApplications()` 调 `/candidates/${cid}/applications`。
- [ ] **Step 2 — 实现 candidate store + selector：** 启动 `load()`；0 个→引导新建；有→默认第一个 + localStorage。
- [ ] **Step 3 — 改 client.js：** per-candidate 方法（listApplications/listInbox/listCrawlPlans/…）URL 前缀 `candidates/${currentCandidateId.value}`。
- [ ] **Step 4:** `npm -C frontend run test && npm -C frontend run build`。
- [ ] commit `feat(auth-rm): candidate selector + path-param API client`。

---

## Self-Review（已做）

- **Spec 覆盖：** spec 的 A–G 全部有对应 task（A 删 auth → T9-13；B candidate CRUD → T1-2；C 路径化 → T3-8；D 前端 → T15-16；E 不在范围 → 约束；F 后台 → T14；G review/capability → T9/T12）。
- **占位符扫描：** 路由迁移批次（T5-8）用"同模式应用 + 具体 router 清单 + 测试 URL 改动"，不是 TODO；每批有 commit + 测试验证。
- **类型一致：** `path_candidate_id(candidate_id: UUID, request) -> UUID`、`CandidateService.list_all/create/get` 签名贯穿 T1/T2/T3。
- **未核实项已标"先核实"：** T1（candidate service 是否已存在）、T11（auth 表 FK）、各 router prefix（阶段 2 每批用 CodeGraph 确认当前 prefix 再改）。

## 风险（plan 执行时盯）

- 18 路由 + 契约测试 URL 改动量大（T4-8）——每批后跑全 unit+contract。
- 部署：API + worker 同步上线（Global Constraints）。
- outbox 发信 per-candidate 账号解析、审计 target candidate 维度、Gmail token store per-candidate 隔离 —— 在 T14（后台）和 T16（前端 OAuth 连接）落实，遇到再细化。
