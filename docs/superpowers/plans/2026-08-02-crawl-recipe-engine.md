# 双轨配置化抓取 P1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除全部 6 个 per-site Python adapter，用配方引擎（Tier 1 标准 JSONPath 配方）+ LLM skill 框架（Tier 2）替代，实现"加站只加数据"。

**Architecture:** `RecipeEngine` 实现 `SelfFetchingAdapter.execute` 协议，从 `vendor/crawl-recipes/manifest.json` 加载 YAML 配方动态构建 `_ADAPTER_REGISTRY`。`crawl_source`/`crawl_source_with_signals` 一次性切到 execute（无双协议）。复杂站走 Tier 2 LLM skill（CrawlAgent + playbook）。

**Tech Stack:** Python 3、Pydantic、`pyyaml`、`jg-rp/python-jsonpath`（RFC 9529）、`extruct`、`selectolax`、pytest、Temporal。

## Global Constraints

- spec：`docs/superpowers/specs/2026-08-02-crawl-recipe-repo-design.md`（v4，双轨配置化）。
- 不改 `ingest_posting` / `CrawledPostingRecord` 构造（`m1_crawl_sink.py:291-307`）/ `clean_job_structured_data` 契约。
- 不改 `CrawlSourceType` enum（保 OFFICIAL meta-type）；manifest source_type key 与 DB enum 解耦。
- 配方 `fields` 目标 = 归一化层内部字段名（`title`/`location`/`description`/`apply_url`），不是 schema.org 名。
- 标准 RFC 9529 JSONPath，filter 用 `[?(@.field=='')]` 语法。
- AGPL 项目（Skyvern/Crawl4AI/Firecrawl/nodriver）不看代码；DSL 自研。
- 一次性切 execute，无 execute/list_jobs 双协议共存期。
- `make verify` 全绿才算任务完成。每个任务结束 commit。

## File Structure

**新建：**
- `vendor/crawl-recipes/manifest.json` — 配方索引
- `vendor/crawl-recipes/recipes/{greenhouse,lever,ashby,official_jsonld,official_sitemap,official_static}/recipe.yaml`
- `vendor/crawl-recipes/skills/_template/SKILL.md` — Tier 2 skill 模板
- `src/careerops/recipes/__init__.py`
- `src/careerops/recipes/schema.py` — 配方 Pydantic 模型 + DSL 语义
- `src/careerops/recipes/loader.py` — manifest 加载 + JSONPath 静态校验
- `src/careerops/recipes/evaluator.py` — DSL 求值（steps/when/foreach/merge/extract 模式）
- `src/careerops/recipes/engine.py` — `RecipeEngine`（`SelfFetchingAdapter` 实现）
- `src/careerops/recipes/skill_loader.py` — Tier 2 skill 加载
- `tests/unit/recipes/test_schema.py` / `test_loader.py` / `test_evaluator.py` / `test_engine.py`
- `tests/unit/recipes/test_recipe_parity.py` — 配方 vs 旧 adapter 行为等价

**修改：**
- `pyproject.toml` — 加依赖
- `src/careerops/adapters/job_sources.py` — 扩展 `AdapterFetchResult`（加 status_code/body_prefix），最后删 6 adapter 类
- `src/careerops/infrastructure/temporal/m1_crawl_sink.py` — `_ADAPTER_REGISTRY` 动态化、`crawl_source`/`crawl_source_with_signals` 切 execute、删 `_fetch_greenhouse_details` + greenhouse 分支
- `scripts/crawl_jobs.py` — 接 RecipeEngine 或废弃

---

## Phase A — 引擎

### Task 1: 依赖 + 配方仓库骨架

**Files:**
- Modify: `pyproject.toml`
- Create: `vendor/crawl-recipes/manifest.json`, `vendor/crawl-recipes/recipes/.gitkeep`, `vendor/crawl-recipes/skills/.gitkeep`, `vendor/crawl-recipes/LICENSE`, `vendor/crawl-recipes/CONTRIBUTING.md`

**Interfaces:** Produces: 空仓库骨架 + manifest schema 占位。

- [ ] **Step 1: 加依赖**

`pyproject.toml` `[project] dependencies` 加：
```
"pyyaml>=6.0",
"jg-rp-python-jsonpath>=1.0",  # 包名待确认，见 Step 2
"extruct>=0.16",
"selectolax>=0.3",
```

- [ ] **Step 2: 确认 jsonpath 包名并安装**

Run: `uv add pyyaml extruct selectolax && uv add python-jsonpath` 
（`jg-rp/python-jsonpath` 在 PyPI 注册名是 `python-jsonpath`；若不对，`uv pip install python-jsonpath` 后核对 `import jsonpath` 可用）
验证：`python -c "import jsonpath; print(jsonpath.findall('$.a', {'a':1}))"` 输出 `[1]`。把 pyproject 里的占位改成实际可 import 的包名。

- [ ] **Step 3: 建 manifest.json 骨架**

`vendor/crawl-recipes/manifest.json`：
```json
{ "schema_version": "1", "recipes": [], "skills": [] }
```

- [ ] **Step 4: 建 LICENSE（MIT）+ CONTRIBUTING.md（一段话：配方/skill 格式、PR 流程）**

- [ ] **Step 5: 验证依赖装好**

Run: `make verify`（应仍全绿，仅新增未用依赖）。

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock vendor/crawl-recipes/
git commit -m "feat(recipes): add recipe repo skeleton + DSL deps"
```

---

### Task 2: 配方 Pydantic 模型 + YAML 加载 + JSONPath 静态校验

**Files:**
- Create: `src/careerops/recipes/__init__.py`, `src/careerops/recipes/schema.py`, `src/careerops/recipes/loader.py`
- Test: `tests/unit/recipes/__init__.py`, `tests/unit/recipes/test_schema.py`, `tests/unit/recipes/test_loader.py`

**Interfaces:**
- Produces: `Recipe`（Pydantic 模型）、`load_recipe(path) -> Recipe`、`load_manifest(dir) -> list[Recipe]`。Recipe 字段：`recipe_version:str, source_type:str, executor_mode:str, match:Match, steps:list[Step]`；Step：`id, when:str|None, foreach:str|None, fetch:Fetch, extract:Extract, merge:Merge|None`；Extract：`mode:Literal['json_path','json_ld','sitemap','css'], items_path:str|None, fields:dict[str,Field], filter:Filter|None, url_filter:str|None`；Field：`path:str, fallback:list[str], transform:Literal['html_unescape','none']`。

- [ ] **Step 1: 写失败测试 — 模型 + 加载**

`tests/unit/recipes/test_schema.py`：
```python
import pytest
from careerops.recipes.schema import Recipe
from careerops.recipes.loader import load_recipe

def test_recipe_loads_minimal(tmp_path):
    p = tmp_path/"r.yaml"
    p.write_text("""
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["boards.greenhouse.io/{slug}"]}
steps:
  - id: list
    fetch: {endpoint: "https://x/{slug}", method: GET}
    extract: {mode: json_path, items_path: "$.jobs",
      fields: {title: {path: title}, id: {path: id}}}
""", encoding="utf-8")
    r = load_recipe(p)
    assert r.source_type == "greenhouse"
    assert r.steps[0].id == "list"
    assert r.steps[0].extract.fields["title"].path == "title"

def test_invalid_jsonpath_rejected(tmp_path):
    p = tmp_path/"r.yaml"
    p.write_text("""
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["x"]}
steps:
  - id: list
    fetch: {endpoint: "x", method: GET}
    extract: {mode: json_path, items_path: "$.[broken",
      fields: {title: {path: title}}}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="jsonpath"):
        load_recipe(p)
```

- [ ] **Step 2: 跑测试验证失败**

Run: `pytest tests/unit/recipes/test_schema.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 schema.py**

`src/careerops/recipes/schema.py`：
```python
from __future__ import annotations
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field

class Match(BaseModel):
    url_patterns: list[str] = Field(default_factory=list)
    host_suffix: str = ""

class Fetch(BaseModel):
    endpoint: str
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    pagination: dict[str, str] = Field(default_factory=dict)

class Field_(BaseModel):
    model_config = {"populate_by_name": True}
    path: str
    fallback: list[str] = Field(default_factory=list)
    transform: Literal["html_unescape", "none"] = "none"

class Filter(BaseModel):
    jsonpath: str  # e.g. "@.type==JobPosting"

class Extract(BaseModel):
    mode: Literal["json_path", "json_ld", "sitemap", "css"]
    items_path: str | None = None
    fields: dict[str, Field_] = Field(default_factory=dict)
    filter: Filter | None = None
    url_filter: str | None = None  # sitemap only

class Merge(BaseModel):
    strategy: Literal["overwrite_empty", "overwrite_all", "keep_first"] = "overwrite_empty"

class Step(BaseModel):
    id: str
    when: str | None = None
    foreach: str | None = None
    fetch: Fetch
    extract: Extract
    merge: Merge | None = None

class Recipe(BaseModel):
    recipe_version: str
    source_type: str
    executor_mode: Literal["http", "ego"] = "http"
    match: Match
    steps: list[Step]
    rate_limit: dict[str, int] = Field(default_factory=dict)
    fallback: Literal["llm_skill", "none"] = "llm_skill"
```

- [ ] **Step 4: 实现 loader.py（含 JSONPath 静态校验）**

`src/careerops/recipes/loader.py`：
```python
from __future__ import annotations
from pathlib import Path
import json
import jsonpath
import yaml
from careerops.recipes.schema import Recipe

def _validate_jsonpath(expr: str, where: str) -> None:
    try:
        jsonpath.findall(expr, {})  # dry-parse against empty data
    except Exception as e:
        raise ValueError(f"invalid jsonpath in {where}: {expr!r}: {e}") from e

def _validate_recipe_paths(r: Recipe) -> None:
    for step in r.steps:
        if step.extract.items_path:
            _validate_jsonpath(step.extract.items_path, f"step {step.id} items_path")
        if step.when:
            _validate_jsonpath(step.when, f"step {step.id} when")
        if step.foreach:
            _validate_jsonpath(step.foreach, f"step {step.id} foreach")
        if step.extract.filter:
            _validate_jsonpath(f"$[{step.extract.filter.jsonpath}]", f"step {step.id} filter")

def load_recipe(path: Path) -> Recipe:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    r = Recipe.model_validate(data)
    _validate_recipe_paths(r)
    return r

def load_manifest(recipes_dir: Path) -> list[Recipe]:
    mf = json.loads((recipes_dir / "manifest.json").read_text(encoding="utf-8"))
    out = []
    for entry in mf.get("recipes", []):
        out.append(load_recipe(recipes_dir / entry["path"]))
    return out
```
注：`jsonpath.findall` 对非法语法抛异常；对合法但无匹配返回 `[]`。dry-parse 用空数据 `{}` 即可触发语法校验。

- [ ] **Step 5: 跑测试验证通过**

Run: `pytest tests/unit/recipes/test_schema.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/careerops/recipes/__init__.py src/careerops/recipes/schema.py src/careerops/recipes/loader.py tests/unit/recipes/
git commit -m "feat(recipes): recipe schema + loader with jsonpath static validation"
```

---

### Task 3: DSL 单步求值（json_path extract + fields/fallback/transform）

**Files:**
- Create: `src/careerops/recipes/evaluator.py`
- Test: `tests/unit/recipes/test_evaluator.py`

**Interfaces:**
- Consumes: `Recipe`/`Step`/`Extract`（Task 2）
- Produces: `evaluate_extract(extract: Extract, data: object) -> list[dict]`（每 dict 是一条记录的 field→value）；`apply_field(field, item) -> str`（含 fallback + transform）。

- [ ] **Step 1: 写失败测试**

`tests/unit/recipes/test_evaluator.py`：
```python
from careerops.recipes.evaluator import evaluate_extract, apply_field
from careerops.recipes.schema import Extract, Field_

def test_json_path_items_and_fields():
    ex = Extract(mode="json_path", items_path="$.jobs",
        fields={"title": Field_(path="title"),
                "loc": Field_(path="location.name")})
    data = {"jobs": [{"title":"Eng","location":{"name":"SF"}},
                     {"title":"PM","location":{"name":"NYC"}}]}
    rows = evaluate_extract(ex, data)
    assert rows == [{"title":"Eng","loc":"SF"},{"title":"PM","loc":"NYC"}]

def test_field_fallback_chain():
    f = Field_(path="applyUrl", fallback=["hostedUrl","url"])
    assert apply_field(f, {"applyUrl":"a"}) == "a"
    assert apply_field(f, {"hostedUrl":"b"}) == "b"
    assert apply_field(f, {"url":"c"}) == "c"
    assert apply_field(f, {}) == ""

def test_transform_html_unescape():
    f = Field_(path="content", transform="html_unescape")
    assert apply_field(f, {"content":"a &amp; b"}) == "a & b"
```

- [ ] **Step 2: 跑验证失败**

Run: `pytest tests/unit/recipes/test_evaluator.py -v` → FAIL

- [ ] **Step 3: 实现 evaluator.py 的单步部分**

`src/careerops/recipes/evaluator.py`：
```python
from __future__ import annotations
import html
import json
import jsonpath
from careerops.recipes.schema import Extract, Field_

def apply_field(field: Field_, item: object) -> str:
    if not isinstance(item, dict):
        return ""
    candidates = [field.path, *field.fallback]
    raw = ""
    for c in candidates:
        vals = jsonpath.findall(f"$.{c}", item)
        if vals:
            raw = vals[0]
            break
    text = "" if raw is None else str(raw)
    if field.transform == "html_unescape" and text:
        text = html.unescape(text)
    return text

def evaluate_extract(extract: Extract, data: object) -> list[dict]:
    if extract.mode != "json_path":
        raise NotImplementedError(extract.mode)
    items = jsonpath.findall(extract.items_path or "$", data) if isinstance(data, (dict, list)) else []
    rows = []
    for item in items:
        if extract.filter:
            ok = jsonpath.findall(f"$[{extract.filter.jsonpath}]", item)
            if not ok:
                continue
        row = {name: apply_field(f, item) for name, f in extract.fields.items()}
        rows.append(row)
    return rows
```
注：filter `@.type==JobPosting` 拼成 `$[@.type==JobPosting]` 求值——`@` 在 filter 内指当前 item，python-jsonpath 支持。

- [ ] **Step 4: 跑验证通过**

Run: `pytest tests/unit/recipes/test_evaluator.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/careerops/recipes/evaluator.py tests/unit/recipes/test_evaluator.py
git commit -m "feat(recipes): single-step json_path extract with fallback+transform"
```

---

### Task 4: DSL 多步求值（steps/when/foreach/merge）

**Files:**
- Modify: `src/careerops/recipes/evaluator.py`
- Test: `tests/unit/recipes/test_evaluator.py`（追加）

**Interfaces:**
- Produces: `run_steps(steps, fetch_one, base_ns) -> dict`，其中 `fetch_one(endpoint) -> object` 是注入的 fetcher，`base_ns` 含 `{slug}` 等模板变量 + `{list_endpoint}`。返回 `{step_id: [row,...]}` 命名空间。多步语义：when 步骤级求值一次；foreach 顺序遍历、per-item fetch；merge 按 strategy 合并到前一步 rows。

- [ ] **Step 1: 写失败测试 — greenhouse 多步**

追加到 `test_evaluator.py`：
```python
from careerops.recipes.evaluator import run_steps
from careerops.recipes.schema import Recipe, Step
import yaml

GREENHOUSE_YAML = """
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["x"]}
steps:
  - id: list
    fetch: {endpoint: "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", method: GET}
    extract: {mode: json_path, items_path: "$.jobs",
      fields: {id: {path: id}, title: {path: title}, description: {path: content}}}
  - id: detail
    when: "$.steps.list[?(@.description=='')]"
    foreach: "$.steps.list[?(@.description=='')]"
    fetch: {endpoint: "{list_endpoint}/{id}", method: GET}
    extract: {mode: json_path, fields: {description: {path: content}}}
    merge: {strategy: overwrite_empty}
"""

def test_multistep_detail_fills_empty_description():
    r = Recipe.model_validate(yaml.safe_load(GREENHOUSE_YAML))
    list_json = {"jobs":[{"id":"1","title":"A","description":""},
                         {"id":"2","title":"B","description":"has"}]}
    detail_json = {"content":"<p>desc</p>"}
    def fetch_one(endpoint):
        return detail_json if endpoint.endswith("/1") else list_json
    ns = run_steps(r.steps, fetch_one, {"slug":"acme",
        "list_endpoint":"https://boards-api.greenhouse.io/v1/boards/acme/jobs"})
    # job 1 description filled from detail; job 2 kept
    by_id = {row["id"]: row for row in ns["list"]}
    assert by_id["1"]["description"] == "<p>desc</p>"
    assert by_id["2"]["description"] == "has"
```

- [ ] **Step 2: 跑验证失败** → FAIL（run_steps 未实现）

- [ ] **Step 3: 实现 run_steps**

追加到 `evaluator.py`：
```python
from urllib.parse import urlsplit, urlunsplit
from careerops.recipes.schema import Step

class _SafeDict(dict):
    def __missing__(self, k): return "{"+k+"}"

def _render(template: str, ns: dict) -> str:
    return template.format_map(_SafeDict(ns))

def run_steps(steps, fetch_one, base_ns):
    ns = {"steps": {}}
    ctx = dict(base_ns)
    for step in steps:
        rows = []
        if step.when is not None:
            if not jsonpath.findall(step.when, ns):
                ns["steps"][step.id] = rows
                continue
        if step.foreach is not None:
            targets = jsonpath.findall(step.foreach, ns)
            for item in targets:
                local = dict(ctx)
                local.update({k: str(v) for k, v in item.items()} if isinstance(item, dict) else {})
                # list_endpoint derived from list step's endpoint (strip query)
                ep = _render(step.fetch.endpoint, local)
                data = fetch_one(ep)
                drows = evaluate_extract(step.extract, data)
                rows.extend(drows)
                if step.merge and "steps" in ns and step.merge.strategy == "overwrite_empty":
                    _merge_into(ns, step, drows, item)
        else:
            ep = _render(step.fetch.endpoint, ctx)
            data = fetch_one(ep)
            # capture list_endpoint for detail steps
            parts = urlsplit(ep)
            ctx["list_endpoint"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            rows = evaluate_extract(step.extract, data)
        ns["steps"][step.id] = rows
    return ns["steps"]

def _merge_into(ns, step, drows, parent_item):
    # find target list rows by matching id
    list_rows = None
    for sid, srows in ns.get("steps", {}).items():
        if srows and "id" in srows[0]:
            list_rows = srows
    if list_rows is None:
        return
    pid = parent_item.get("id") if isinstance(parent_item, dict) else None
    for d in drows:
        for row in list_rows:
            if row.get("id") == pid:
                for k, v in d.items():
                    if not row.get(k):
                        row[k] = v
```
注：merge 按 id 关联 detail 到 list row，overwrite_empty 只填空字段——等价 `_fetch_greenhouse_details` 的 detail_cache 语义。`list_endpoint` 从 list 步 endpoint 剥 query 派生（等价 :452-453）。

- [ ] **Step 4: 跑验证通过**

Run: `pytest tests/unit/recipes/test_evaluator.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/careerops/recipes/evaluator.py tests/unit/recipes/test_evaluator.py
git commit -m "feat(recipes): multistep steps/when/foreach/merge evaluation"
```

---

### Task 5: json_ld / sitemap / css extract 模式 + 护栏

**Files:**
- Modify: `src/careerops/recipes/evaluator.py`
- Test: `tests/unit/recipes/test_evaluator.py`（追加）

**Interfaces:** 扩展 `evaluate_extract` 支持 mode=json_ld/sitemap/css。护栏：json_ld 经 `extract.filter`（默认 `@.type==JobPosting`）；sitemap XXE 拒绝 + url_filter；css 多选择器 zip。

- [ ] **Step 1: 写失败测试**

追加：
```python
def test_json_ld_filters_jobposting():
    from careerops.recipes.schema import Extract, Field_, Filter
    html_doc = '''<script type="application/ld+json">{"@type":"Person","name":"x"}</script>
    <script type="application/ld+json">{"@type":"JobPosting","title":"Eng"}</script>'''
    ex = Extract(mode="json_ld", filter=Filter(jsonpath="@.type==JobPosting"),
        fields={"title": Field_(path="title")})
    rows = evaluate_extract(ex, html_doc)
    assert rows == [{"title":"Eng"}]

def test_sitemap_xxe_rejected_and_url_filter():
    from careerops.recipes.schema import Extract
    xml = '''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>https://x.com/about</loc></url>
    <url><loc>https://x.com/jobs/1</loc></url></urlset>'''
    ex = Extract(mode="sitemap", url_filter=r"\b(jobs|careers|positions)\b")
    rows = evaluate_extract(ex, xml)
    assert rows == [{"url":"https://x.com/jobs/1"}]
    evil = '<!DOCTYPE x [<!ENTITY xxe "y">]><urlset></urlset>'
    assert evaluate_extract(ex, evil) == []

def test_css_zip_pairing():
    from careerops.recipes.schema import Extract, Field_
    doc = '<div><h2 class="t">A</h2><span class="l">SF</span><h2 class="t">B</h2><span class="l">NYC</span></div>'
    ex = Extract(mode="css", fields={"title": Field_(path="h2.t"), "location": Field_(path="span.l")})
    rows = evaluate_extract(ex, doc)
    assert rows == [{"title":"A","location":"SF"},{"title":"B","location":"NYC"}]
```

- [ ] **Step 2: 跑验证失败** → FAIL（mode 未实现）

- [ ] **Step 3: 实现三种模式 + 护栏**

追加到 `evaluator.py`：
```python
import re
import hashlib
from xml.etree import ElementTree

_UNSAFE_XML_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)

def _extract_json_ld(extract, html_str):
    import extruct
    blocks = extruct.extract(html_str, syntaxes=["json-ld"]).get("json-ld", [])
    out = []
    for b in blocks:
        if extract.filter:
            if not jsonpath.findall(f"$[{extract.filter.jsonpath}]", b):
                continue
        out.append({n: apply_field(f, b) for n, f in extract.fields.items()})
    return out

def _extract_sitemap(extract, xml_str):
    if _UNSAFE_XML_RE.search(xml_str):
        return []
    try:
        root = ElementTree.fromstring(xml_str)
    except ElementTree.ParseError:
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    url_re = re.compile(extract.url_filter) if extract.url_filter else None
    out = []
    for loc in root.findall(".//sm:url/sm:loc", ns):
        u = (loc.text or "").strip()
        if url_re and not url_re.search(u):
            continue
        out.append({"url": u, "external_id": hashlib.sha256(u.encode()).hexdigest()[:16]})
    return out

def _extract_css(extract, html_str):
    from selectolax.parser import HTMLParser
    tree = HTMLParser(html_str)
    cols = {n: tree.css(f.path) for n, f in extract.fields.items()}  # path 用作 css selector
    maxlen = max((len(v) for v in cols.values()), default=0)
    out = []
    for i in range(maxlen):
        row = {}
        for n, nodes in cols.items():
            row[n] = nodes[i].text(strip=True) if i < len(nodes) else ""
        out.append(row)
    return out
```
并改 `evaluate_extract` 顶部分派：
```python
def evaluate_extract(extract, data):
    if extract.mode == "json_path":
        ...  # Task 3 原逻辑
    if extract.mode == "json_ld":
        return _extract_json_ld(extract, data)
    if extract.mode == "sitemap":
        return _extract_sitemap(extract, data)
    if extract.mode == "css":
        return _extract_css(extract, data)
    raise NotImplementedError(extract.mode)
```
注：css 模式 `Field_.path` 复用作 CSS 选择器（语义清楚，schema 不改）。

- [ ] **Step 4: 跑验证通过**

Run: `pytest tests/unit/recipes/test_evaluator.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/careerops/recipes/evaluator.py tests/unit/recipes/test_evaluator.py
git commit -m "feat(recipes): json_ld/sitemap/css modes with XXE/url_filter/css-zip guards"
```

---

### Task 6: RecipeEngine + SelfFetchingAdapter.execute + 扩展 AdapterFetchResult

**Files:**
- Create: `src/careerops/recipes/engine.py`
- Modify: `src/careerops/adapters/job_sources.py`（`AdapterFetchResult` 加 `status_code:int=0`,`body_prefix:str=""`）
- Test: `tests/unit/recipes/test_engine.py`

**Interfaces:**
- Consumes: `run_steps`/`evaluate_extract`（Task 3-5）、`AdapterFetchResult`/`RawJobRecord`（adapters）、`FetcherFn`（m1_crawl_sink:51）、`CrawlJobSourceInput`
- Produces: `RecipeEngine(recipe: Recipe)`，方法 `source_type`/`parser_version`/`detect(base_url)`/`async execute(request, fetch) -> AdapterFetchResult`。execute 内部：用 `fetch`（绑定 request 的 `_fetch_for_request`）跑 steps，首步 resp 的 status_code/body_prefix 填入 AdapterFetchResult。

- [ ] **Step 1: 扩展 AdapterFetchResult**

`src/careerops/adapters/job_sources.py` `AdapterFetchResult`（:38）加两字段：
```python
@dataclass(frozen=True, slots=True)
class AdapterFetchResult:
    jobs: tuple[RawJobRecord, ...] = ()
    response_hash: str = ""
    fetched_at: datetime | None = None
    source_url: str = ""
    parser_version: str = ""
    status_code: int = 0       # 新增：首步 resp 状态
    body_prefix: str = ""      # 新增：首步 resp 前 4096 字节（CAPTCHA 信号）
```

- [ ] **Step 2: 写失败测试**

`tests/unit/recipes/test_engine.py`：
```python
import asyncio
from careerops.recipes.engine import RecipeEngine
from careerops.recipes.schema import Recipe
from careerops.adapters.job_sources import AdapterFetchResult
import yaml

RECIPE = """
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["x"]}
steps:
  - id: list
    fetch: {endpoint: "https://x/jobs", method: GET}
    extract: {mode: json_path, items_path: "$.jobs",
      fields: {id: {path: id}, title: {path: title}}}
"""

class FakeResp:
    def __init__(self, body, status=200):
        from datetime import datetime
        self.body = body; self.status_code = status
        self.fetched_at = datetime(2026,1,1); self.final_url = "https://x/jobs"

def test_execute_returns_records_and_signals():
    r = Recipe.model_validate(yaml.safe_load(RECIPE))
    eng = RecipeEngine(r)
    fetch = lambda url: FakeResp('{"jobs":[{"id":"1","title":"Eng"}]}')
    result = asyncio.run(eng.execute(request=None, fetch=fetch))
    assert result.jobs[0].external_id == "1"
    assert result.jobs[0].title == "Eng"
    assert result.status_code == 200
    assert result.parser_version == "recipe:greenhouse:1"
```
注：`request` 在测试里 None（fetch 是 mock，不用 request）；真实路径 fetch 由 sink 传入绑定 request 的 partial。

- [ ] **Step 3: 跑验证失败** → FAIL

- [ ] **Step 4: 实现 engine.py**

`src/careerops/recipes/engine.py`：
```python
from __future__ import annotations
import hashlib
import json
from careerops.adapters.job_sources import AdapterFetchResult, RawJobRecord
from careerops.recipes.evaluator import run_steps
from careerops.recipes.schema import Recipe

_INTERNAL_FIELDS = {"id", "title", "location", "description", "apply_url", "url"}

def _row_to_record(row: dict) -> RawJobRecord:
    external_id = str(row.get("id") or row.get("external_id") or
                      hashlib.sha256(str(row).encode()).hexdigest()[:16])
    url = row.get("apply_url") or row.get("url") or ""
    return RawJobRecord(
        external_id=external_id,
        title=str(row.get("title") or ""),
        location=str(row.get("location") or ""),
        url=url,
        description=str(row.get("description") or ""),
        raw_data={k: v for k, v in row.items() if k not in _INTERNAL_FIELDS},
    )

class RecipeEngine:
    def __init__(self, recipe: Recipe):
        self._recipe = recipe

    @property
    def source_type(self) -> str:
        return self._recipe.source_type

    @property
    def parser_version(self) -> str:
        return f"recipe:{self._recipe.source_type}:{self._recipe.recipe_version}"

    def detect(self, base_url: str) -> bool:
        return any(p.split("{")[0] in base_url for p in self._recipe.match.url_patterns) \
            or (self._recipe.match.host_suffix and base_url.endswith(self._recipe.match.host_suffix))

    async def execute(self, request, fetch) -> AdapterFetchResult:
        # fetch: Callable[[str], FetchedResponse] —— sink 传入绑定 request 的 _fetch_for_request
        first_status, first_body, final_url = 0, "", ""
        def fetch_one(endpoint):
            nonlocal first_status, first_body, final_url
            resp = fetch(endpoint)
            if not first_status:
                first_status = getattr(resp, "status_code", 200) or 200
                first_body = (getattr(resp, "body", "") or "")[:4096]
            final_url = getattr(resp, "final_url", endpoint) or endpoint
            raw = getattr(resp, "body", "")
            stripped = raw.lstrip() if isinstance(raw, str) else ""
            if stripped and stripped[0] in "{[":
                try:
                    return json.loads(raw)
                except Exception:
                    return raw
            return raw
        base_ns = {}
        if request is not None:
            # {slug} from source_identifier if available
            slug = getattr(request, "source_identifier", "") or ""
            base_ns["slug"] = slug
        steps_ns = run_steps(self._recipe.steps, fetch_one, base_ns)
        # flatten: take the first step's rows as primary; merge folded in already
        primary = next(iter(steps_ns.values()), []) if steps_ns else []
        jobs = tuple(_row_to_record(row) for row in primary)
        return AdapterFetchResult(
            jobs=jobs,
            response_hash=hashlib.sha256(str(primary).encode()).hexdigest(),
            source_url=final_url,
            parser_version=self.parser_version,
            status_code=first_status,
            body_prefix=first_body,
        )
```
注：`_row_to_record` 把配方 row 映射到 RawJobRecord（id/title/location/url=apply_url优先/description/raw_data 余字段）——等价旧 adapter 的字段映射 + raw_data 保留。多步的 merge 已在 run_steps 内完成，取首步 list rows。

- [ ] **Step 5: 跑验证通过**

Run: `pytest tests/unit/recipes/test_engine.py -v` → PASS

- [ ] **Step 6: 跑 make verify 确认 AdapterFetchResult 新字段未破坏旧调用**

Run: `make verify` → 应全绿（新字段有默认值）。

- [ ] **Step 7: Commit**

```bash
git add src/careerops/recipes/engine.py src/careerops/adapters/job_sources.py tests/unit/recipes/test_engine.py
git commit -m "feat(recipes): RecipeEngine.execute + signal fields on AdapterFetchResult"
```

---

## Phase B — 配方翻译 + 切换

### Task 7: _ADAPTER_REGISTRY 动态化（manifest → RecipeEngine）

**Files:**
- Modify: `src/careerops/infrastructure/temporal/m1_crawl_sink.py`（`__init__` 接受 recipe-built adapters；新增 `build_recipe_registry()`）
- Test: `tests/unit/test_m1_integration.py`（追加 registry 构建测试）

**Interfaces:**
- Produces: `build_recipe_registry(recipes_dir) -> dict[str, RecipeEngine]`。`RealCrawlActivitySink.__init__` 优先用注入的 `adapters`，否则 `_ADAPTER_REGISTRY` 合并 recipe registry。

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_m1_integration.py`：
```python
def test_recipe_registry_loads_from_manifest(tmp_path):
    from careerops.infrastructure.temporal.m1_crawl_sink import build_recipe_registry
    # 用 vendor/crawl-recipes 真实 manifest（Task 8 后才有配方；此处先测空 manifest 返回 {}）
    reg = build_recipe_registry(tmp_path)  # tmp_path 只有空 manifest
    assert reg == {}
```

- [ ] **Step 2: 跑验证失败** → FAIL（build_recipe_registry 不存在）

- [ ] **Step 3: 实现 build_recipe_registry**

在 `m1_crawl_sink.py` 顶部 import 后加：
```python
from pathlib import Path
from careerops.recipes.loader import load_manifest
from careerops.recipes.engine import RecipeEngine

DEFAULT_RECIPES_DIR = Path(__file__).resolve().parents[3] / "vendor" / "crawl-recipes"

def build_recipe_registry(recipes_dir: Path | None = None) -> dict[str, RecipeEngine]:
    d = recipes_dir or DEFAULT_RECIPES_DIR
    if not (d / "manifest.json").exists():
        return {}
    return {r.source_type: RecipeEngine(r) for r in load_manifest(d)}
```

- [ ] **Step 4: 改 __init__ 合并 registry**

`RealCrawlActivitySink.__init__`（:214-235）末尾 `self._adapters = adapters or dict(_ADAPTER_REGISTRY)` 改为：
```python
if adapters is not None:
    self._adapters = adapters
else:
    self._adapters = dict(_ADAPTER_REGISTRY)
    self._adapters.update(build_recipe_registry())  # recipe 覆盖同名 key
```

- [ ] **Step 5: 跑验证通过 + make verify**

Run: `pytest tests/unit/test_m1_integration.py -v && make verify` → PASS

- [ ] **Step 6: Commit**

```bash
git add src/careerops/infrastructure/temporal/m1_crawl_sink.py tests/unit/test_m1_integration.py
git commit -m "feat(crawl): dynamic recipe registry merged into _ADAPTER_REGISTRY"
```

---

### Task 8: 翻译 greenhouse（多步）/ lever / ashby 配方

**Files:**
- Create: `vendor/crawl-recipes/recipes/{greenhouse,lever,ashby}/recipe.yaml`
- Modify: `vendor/crawl-recipes/manifest.json`（登记 3 条）
- Test: `tests/unit/recipes/test_recipe_parity.py`（配方 vs 旧 adapter 行为等价）

**Interfaces:** Consumes 旧 adapter 字段映射（已核实）。

字段映射来源（CodeGraph 核实，等价翻译）：
- **greenhouse**（`GreenhouseAdapter:103` + `GreenhouseDetailAdapter:158` + `_fetch_greenhouse_details:432`）：list endpoint `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`；fields `id←id, title←title, location←location.name, apply_url←absolute_url, description←content(html_unescape)`；detail step 补空 description（endpoint `{list_endpoint}/{id}`，`description←content(html_unescape)`，merge overwrite_empty）。
- **lever**（`LeverAdapter:186`）：endpoint `https://api.lever.co/v0/postings/{slug}?mode=json`，响应是 list；fields `id←id, title←text, location←categories.location, apply_url←applyUrl|hostedUrl, description←descriptionPlain|description(html_unescape)`。
- **ashby**（`AshbyAdapter:231`）：endpoint `https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`；fields `id←id, title←title, location←location, apply_url←applyUrl|jobUrl|url, description←descriptionPlain|descriptionHtml|description(html_unescape)`。

- [ ] **Step 1: 写 greenhouse 配方**

`vendor/crawl-recipes/recipes/greenhouse/recipe.yaml`：
```yaml
recipe_version: "1"
source_type: greenhouse
executor_mode: http
match: {url_patterns: ["boards.greenhouse.io"], host_suffix: "greenhouse.io"}
steps:
  - id: list
    fetch: {endpoint: "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", method: GET}
    extract:
      mode: json_path
      items_path: "$.jobs"
      fields:
        id: {path: id}
        title: {path: title}
        location: {path: location.name}
        apply_url: {path: absolute_url}
        description: {path: content, transform: html_unescape}
  - id: detail
    when: "$.steps.list[?(@.description=='')]"
    foreach: "$.steps.list[?(@.description=='')]"
    fetch: {endpoint: "{list_endpoint}/{id}", method: GET}
    extract: {mode: json_path, fields: {description: {path: content, transform: html_unescape}}}
    merge: {strategy: overwrite_empty}
rate_limit: {requests_per_minute: 60}
fallback: llm_skill
```

- [ ] **Step 2: 写 lever + ashby 配方**（按上面映射，单步，无 detail）

`lever/recipe.yaml`：
```yaml
recipe_version: "1"
source_type: lever
executor_mode: http
match: {url_patterns: ["lever.co"], host_suffix: "lever.co"}
steps:
  - id: list
    fetch: {endpoint: "https://api.lever.co/v0/postings/{slug}?mode=json", method: GET}
    extract:
      mode: json_path
      items_path: "$"
      fields:
        id: {path: id}
        title: {path: text}
        location: {path: categories.location}
        apply_url: {path: applyUrl, fallback: [hostedUrl]}
        description: {path: descriptionPlain, fallback: [description], transform: html_unescape}
fallback: llm_skill
```
`ashby/recipe.yaml`：
```yaml
recipe_version: "1"
source_type: ashby
executor_mode: http
match: {url_patterns: ["ashbyhq.com"], host_suffix: "ashbyhq.com"}
steps:
  - id: list
    fetch: {endpoint: "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", method: GET}
    extract:
      mode: json_path
      items_path: "$.jobs"
      fields:
        id: {path: id}
        title: {path: title}
        location: {path: location}
        apply_url: {path: applyUrl, fallback: [jobUrl, url]}
        description: {path: descriptionPlain, fallback: [descriptionHtml, description], transform: html_unescape}
fallback: llm_skill
```

- [ ] **Step 3: 登记进 manifest.json**

manifest.json `recipes` 加：
```json
{"id":"greenhouse","path":"recipes/greenhouse/recipe.yaml","source_type":"greenhouse","executor_mode":"http","match_hosts":["boards.greenhouse.io"],"recipe_version":"1"},
{"id":"lever","path":"recipes/lever/recipe.yaml","source_type":"lever","executor_mode":"http","match_hosts":["lever.co"],"recipe_version":"1"},
{"id":"ashby","path":"recipes/ashby/recipe.yaml","source_type":"ashby","executor_mode":"http","match_hosts":["ashbyhq.com"],"recipe_version":"1"}
```

- [ ] **Step 4: 写等价测试（配方引擎 vs 旧 adapter，同一份 mock JSON）**

`tests/unit/recipes/test_recipe_parity.py`：
```python
import json
from careerops.adapters.job_sources import GreenhouseAdapter, LeverAdapter, AshbyAdapter
from careerops.recipes.engine import RecipeEngine
from careerops.recipes.loader import load_recipe
from pathlib import Path

RECIPES = Path("vendor/crawl-recipes/recipes")

def _recipe_rows(adapter, recipe_name, data):
    expected = adapter.list_jobs(data).jobs
    eng = RecipeEngine(load_recipe(RECIPES/f"{recipe_name}/recipe.yaml"))
    rows = []
    for s in eng._recipe.steps:
        if s.id == "list":
            from careerops.recipes.evaluator import evaluate_extract
            rows = evaluate_extract(s.extract, data)
    # map rows to comparable (external_id, title, location, url, description)
    def key(j): return (j.external_id, j.title, j.location, j.url, j.description)
    got = {(r.get("id"),r.get("title"),r.get("location"),r.get("apply_url"),r.get("description")) for r in rows}
    exp = {key(j) for j in expected}
    return got, exp

def test_greenhouse_list_parity():
    data = {"jobs":[{"id":"1","title":"A","location":{"name":"SF"},"absolute_url":"u","content":"c"}]}
    got, exp = _recipe_rows(GreenhouseAdapter(), "greenhouse", data)
    assert got == exp

def test_lever_parity():
    data = [{"id":"1","text":"A","categories":{"location":"SF"},"applyUrl":"u","descriptionPlain":"d"}]
    got, exp = _recipe_rows(LeverAdapter(), "lever", data)
    assert got == exp

def test_ashby_parity():
    data = {"jobs":[{"id":"1","title":"A","location":"SF","applyUrl":"u","descriptionPlain":"d"}]}
    got, exp = _recipe_rows(AshbyAdapter(), "ashby", data)
    assert got == exp
```

- [ ] **Step 5: 跑等价测试**

Run: `pytest tests/unit/recipes/test_recipe_parity.py -v`
若不等价，修配方 field_map 直到 got==exp（这是回归门）。

- [ ] **Step 6: Commit**

```bash
git add vendor/crawl-recipes/recipes/greenhouse vendor/crawl-recipes/recipes/lever vendor/crawl-recipes/recipes/ashby vendor/crawl-recipes/manifest.json tests/unit/recipes/test_recipe_parity.py
git commit -m "feat(recipes): greenhouse/lever/ashby recipes with parity tests vs legacy adapters"
```

---

### Task 9: 翻译 official_jsonld / official_sitemap / official_static 配方

**Files:**
- Create: `vendor/crawl-recipes/recipes/official_{jsonld,sitemap,static}/recipe.yaml`
- Modify: `manifest.json`（登记 3 条）
- Test: `test_recipe_parity.py`（追加 3 条等价测试）

字段映射来源（核实）：
- **json_ld**（`JsonLdAdapter`，:345 过滤 `@type=="JobPosting"`）：fields `title←title, description←description, location←jobLocation.address.addressLocality|location, apply_url←url`；filter `@.type==JobPosting`。
- **sitemap**（`SitemapAdapter:389`）：mode=sitemap，url_filter `\b(jobs|careers|positions)\b`，产 `{url, external_id}`。
- **static**（`StaticHtmlAdapter:436`）：mode=css，fields `title←` 正则 `class=...job_title`（转 css 选择器 `[class*=job_title]` 或保留正则语义——见 Step 1 说明），location 同理；按索引 zip。

- [ ] **Step 1: 写 jsonld 配方**

`official_jsonld/recipe.yaml`：
```yaml
recipe_version: "1"
source_type: official_jsonld
executor_mode: http
match: {url_patterns: []}
steps:
  - id: fetch
    fetch: {endpoint: "{base_url}", method: GET}
    extract:
      mode: json_ld
      filter: {jsonpath: "@.type==JobPosting"}
      fields:
        title: {path: title}
        description: {path: description}
        location: {path: jobLocation.address.addressLocatility, fallback: [location]}
        apply_url: {path: url}
fallback: llm_skill
```
注：`{base_url}` 在 execute 时由 request.base_url 填入（engine base_ns 加 `base_url`，见 Task 10 Step 3 补充）。

- [ ] **Step 2: 写 sitemap 配方**

`official_sitemap/recipe.yaml`：
```yaml
recipe_version: "1"
source_type: official_sitemap
executor_mode: http
match: {url_patterns: ["sitemap"]}
steps:
  - id: fetch
    fetch: {endpoint: "{base_url}", method: GET}
    extract:
      mode: sitemap
      url_filter: "\\b(jobs|careers|positions)\\b"
fallback: llm_skill
```

- [ ] **Step 3: 写 static 配方**

`official_static/recipe.yaml`：
```yaml
recipe_version: "1"
source_type: official_static
executor_mode: http
match: {url_patterns: []}
steps:
  - id: fetch
    fetch: {endpoint: "{base_url}", method: GET}
    extract:
      mode: css
      fields:
        title: {path: "[class*=job_title]"}
        location: {path: "[class*=job_location]"}
fallback: llm_skill
```
注：旧 StaticHtmlAdapter 用正则 `class=["'][^"']*job[-_]?title`；css 近似 `[class*=job_title]`（不区分 `-`/`_` 时需调选择器）。等价测试若不通过，把 css 选择器放宽（如 `[class*=job]`）或在 evaluator css 模式支持正则属性选择——但优先调选择器，避免扩 DSL。

- [ ] **Step 4: 登记进 manifest.json**（3 条 official_*）

- [ ] **Step 5: 写等价测试**

追加到 `test_recipe_parity.py`：
```python
from careerops.adapters.job_sources import JsonLdAdapter, SitemapAdapter, StaticHtmlAdapter

def test_jsonld_parity():
    html = '<script type="application/ld+json">{"@type":"JobPosting","title":"A","description":"d","url":"u"}</script>'
    # ... 同 _recipe_rows 模式，mode=json_ld 时直接 evaluate_extract(s.extract, html)
def test_sitemap_parity():
    xml = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://x/jobs/1</loc></url></urlset>'
def test_static_parity():
    doc = '<div><h2 class="job_title">A</h2><span class="job_location">SF</span></div>'
```
（按 Task 8 的 `_recipe_rows` 模式，分别对比 JsonLdAdapter/SitemapAdapter/StaticHtmlAdapter 的 list_jobs 输出。）

- [ ] **Step 6: 跑等价测试，调选择器直到通过**

Run: `pytest tests/unit/recipes/test_recipe_parity.py -v` → PASS

- [ ] **Step 7: Commit**

```bash
git add vendor/crawl-recipes/recipes/official_jsonld vendor/crawl-recipes/recipes/official_sitemap vendor/crawl-recipes/recipes/official_static vendor/crawl-recipes/manifest.json tests/unit/recipes/test_recipe_parity.py
git commit -m "feat(recipes): official jsonld/sitemap/static recipes with parity tests"
```

---

### Task 10: crawl_source + crawl_source_with_signals 切 execute + 删 greenhouse 分支

**Files:**
- Modify: `src/careerops/infrastructure/temporal/m1_crawl_sink.py`
- Test: `tests/unit/test_greenhouse_bulk_fetch.py`（应零改动通过——回归门）

**Interfaces:** Consumes RecipeEngine.execute（Task 6）+ registry（Task 7）。

- [ ] **Step 1: 改 crawl_source 调 execute**

`crawl_source`（:260-308）替换为：
```python
async def crawl_source(self, request):
    adapter = self._adapters.get(request.source_type)
    if adapter is None:
        return []
    if hasattr(adapter, "execute"):
        result = await adapter.execute(request, lambda url: self._fetch_for_request(request, url))
        jobs = result.jobs
        resp_meta = (result.status_code, result.source_url, result.fetched_at)
    else:
        # 遗留无 execute 的 adapter（过渡期不应存在；P1 后全删）
        resp = self._fetch_for_request(request, request.base_url)
        result = adapter.list_jobs(_parse_body(resp.body))
        jobs = result.jobs
        resp_meta = (resp.status_code, resp.final_url, resp.fetched_at)
    fetched_at = (resp_meta[2].isoformat() if hasattr(resp_meta[2],'isoformat') else str(resp_meta[2]))
    postings = []
    for record in jobs:
        raw = {k: (v if isinstance(v,str) else json.dumps(v, ensure_ascii=False)) for k,v in record.raw_data.items()}
        postings.append(CrawledPostingRecord(
            source_id=request.source_id, external_id=record.external_id,
            canonical_url=record.url or request.base_url,
            source_url=resp_meta[1] or request.base_url,
            structured_data={"title":record.title,"location":record.location,
                "description":record.description,"apply_url":record.url or "", **raw},
            parser_version=adapter.parser_version, fetched_at=fetched_at))
    return postings
```
**删除** greenhouse 特殊分支（:275-280 detail_cache）——detail 已在 greenhouse 配方多步里。

- [ ] **Step 2: 改 crawl_source_with_signals 调 execute**

`crawl_source_with_signals`（:310-408）同样：adapter 有 execute 走 execute（用 result.status_code/body_prefix 填 CrawlSourceResult），否则旧路径。**删除** greenhouse 分支（:352-357）。Tier2 fallback（`_crawl_via_agent` :388-389）保留。

关键：signals 路径的 `CrawlSourceResult(status_code=result.status_code, body_prefix=result.body_prefix, ...)` —— 用 execute 返回的首步信号（Task 6）。

- [ ] **Step 3: 删 _fetch_greenhouse_details**

删除 :432-486 整个方法（greenhouse 多步已在配方）。

- [ ] **Step 4: 跑 greenhouse 端到端回归测试（零改动通过）**

Run: `pytest tests/unit/test_greenhouse_bulk_fetch.py -v`
若失败，多半是 detail 多步的 merge 语义或 list_endpoint 派生——回到 Task 4/8 调 evaluator，直到旧测试通过。这是大爆炸的关键回归门。

- [ ] **Step 5: make verify**

Run: `make verify` → 全绿。

- [ ] **Step 6: Commit**

```bash
git add src/careerops/infrastructure/temporal/m1_crawl_sink.py
git commit -m "refactor(crawl): switch crawl_source/signals to RecipeEngine.execute; drop greenhouse special-case"
```

---

## Phase C — 清理 + skill + enum

### Task 11: 删 6 adapter 类 + 处置 scripts/crawl_jobs.py

**Files:**
- Modify: `src/careerops/adapters/job_sources.py`（删 GreenhouseAdapter/GreenhouseDetailAdapter/LeverAdapter/AshbyAdapter/AshbyDetailAdapter/JsonLdAdapter/SitemapAdapter/StaticHtmlAdapter + `_ADAPTER_REGISTRY` 硬编码 dict；保留 RawJobRecord/AdapterFetchResult/JobSourceAdapter 协议）
- Modify: `src/careerops/infrastructure/temporal/m1_crawl_sink.py`（删 `_ADAPTER_REGISTRY` import/定义，registry 完全由 build_recipe_registry 提供）
- Modify: `scripts/crawl_jobs.py`（接 RecipeEngine 或废弃）
- Test: 删 `tests/unit/test_adapters_fetch_job.py`/`test_adapter_manifest.py` 中针对已删 adapter 的用例（保留 RawJobRecord/AdapterFetchResult 的）

- [ ] **Step 1: 确认 registry 已完全由配方提供**

`m1_crawl_sink.py` `__init__` 改为 `self._adapters = adapters if adapters is not None else build_recipe_registry()`（移除 `dict(_ADAPTER_REGISTRY)` fallback）。删 `_ADAPTER_REGISTRY` 定义 + 6 adapter 的 import。

- [ ] **Step 2: 删 6 adapter 类**

`adapters/job_sources.py` 删除 GreenhouseAdapter/GreenhouseDetailAdapter/LeverAdapter/AshbyAdapter/AshbyDetailAdapter/JsonLdAdapter/SitemapAdapter/StaticHtmlAdapter 及其正则常量。保留：`RawJobRecord`/`AdapterFetchResult`/`JobSourceAdapter`/`DetailJobSourceAdapter` 协议/`_hash_response`。

- [ ] **Step 3: 处置 scripts/crawl_jobs.py**

改为用 RecipeEngine 跑配方（或若已无运行需要，标记 deprecated 并简化为调 sink）。最低：删除对 6 adapter 的直接 import，改 `from careerops.recipes.loader import load_manifest`。

- [ ] **Step 4: 修/删受影响测试**

删 `test_adapters_fetch_job.py`、`test_adapter_manifest.py` 里测已删 adapter 的用例。`test_greenhouse_bulk_fetch.py` 保留（端到端，已通过 RecipeEngine）。

- [ ] **Step 5: make verify**

Run: `make verify` → 全绿。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(crawl): remove 6 legacy adapters; registry fully recipe-driven"
```

---

### Task 12: Tier 2 skill 框架 + 1 示范 skill

**Files:**
- Create: `src/careerops/recipes/skill_loader.py`, `vendor/crawl-recipes/skills/workday/SKILL.md`, `vendor/crawl-recipes/skills/_template/SKILL.md`
- Modify: `manifest.json`（skills 段）
- Test: `tests/unit/recipes/test_skill_loader.py`

**Interfaces:**
- Produces: `Skill`（frontmatter: source_type/executor_mode/match + body playbook）、`load_skills(skills_dir) -> dict[str,Skill]`。本任务只建加载框架 + 1 示范 skill（Workday），不改 CrawlAgent 执行逻辑（CrawlAgent 现已兜底；skill 引导集成留后续 spec）。

- [ ] **Step 1: 写示范 skill**

`vendor/crawl-recipes/skills/workday/SKILL.md`：
```markdown
---
source_type: workday
executor_mode: ego
match: {host_suffix: "myworkdayjobs.com"}
---
# Workday 抓取 skill
Workday 用 POST + 递归 facet 细分绕 2000 条上限，声明式配方无法表达，走 LLM agent：
1. POST tenant 的 jobSearch 端点，body 含 facet（location/jobType）
2. 若结果数 == 2000（命中上限），按当前 facet 值再细分（最多 4 层）
3. 每岗二次 GET 详情
4. 多标签 facet 去重（同 job 多 facet 命中）
产出归一化字段：title/location/description/apply_url。
```

`_template/SKILL.md`：留 frontmatter + 占位说明（"# 用法：写明该站抓取步骤，CrawlAgent 按此引导"）。

- [ ] **Step 2: 写 skill_loader + 测试**

`skill_loader.py`：
```python
from __future__ import annotations
from pathlib import Path
import re
import yaml

class Skill:
    def __init__(self, source_type, executor_mode, match, body, path):
        self.source_type = source_type
        self.executor_mode = executor_mode
        self.match = match
        self.body = body
        self.path = path

_FRONT = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

def load_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    m = _FRONT.match(text)
    if not m:
        raise ValueError(f"skill {path} missing frontmatter")
    meta = yaml.safe_load(m.group(1))
    return Skill(meta["source_type"], meta.get("executor_mode","ego"), meta.get("match",{}), m.group(2).strip(), path)

def load_skills(skills_dir: Path) -> dict[str, Skill]:
    out = {}
    for sk in skills_dir.glob("*/SKILL.md"):
        s = load_skill(sk)
        out[s.source_type] = s
    return out
```

`test_skill_loader.py`：测 load_skill 解析 frontmatter + body。

- [ ] **Step 3: manifest.json skills 段登记 workday**

- [ ] **Step 4: 跑测试 + make verify**

Run: `pytest tests/unit/recipes/test_skill_loader.py -v && make verify` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/careerops/recipes/skill_loader.py vendor/crawl-recipes/skills/ tests/unit/recipes/test_skill_loader.py vendor/crawl-recipes/manifest.json
git commit -m "feat(recipes): Tier 2 LLM skill loader + workday example skill"
```

---

### Task 13: enum 解耦确认 + 端到端验证

**Files:**
- 无代码改动（验证 enum 不动 + 端到端）。
- Modify: spec/plan 注记（如需）。

- [ ] **Step 1: 确认 CrawlSourceType 未改**

Run: `git diff main -- src/careerops/domain/crawl_plans.py | grep -i CrawlSourceType`
Expected: 无 enum 改动（OFFICIAL 保留 meta-type）。manifest 的 official_jsonld 等 key 不进 enum。

- [ ] **Step 2: 端到端 smoke — greenhouse 真实抓取**

Run: `python scripts/verify_real_crawl_to_inbox.py`（若该脚本接 sink；否则用 `scripts/crawl_jobs.py` 经 Task 11 改造后的路径跑一个 greenhouse board）
Expected: 产出 postings 入库，parser_version 形如 `recipe:greenhouse:1`，行为与旧 adapter 一致（岗位数、字段）。

- [ ] **Step 3: 全量回归**

Run: `make verify` → 全绿（含旧 greenhouse 端到端测试）。

- [ ] **Step 4: 确认无 per-site Python adapter 残留**

Run: `grep -rn "class GreenhouseAdapter\|class LeverAdapter\|class AshbyAdapter\|class JsonLdAdapter\|class SitemapAdapter\|class StaticHtmlAdapter" src/`
Expected: 无命中（6 类全删，抓取知识全在 vendor/crawl-recipes/）。

- [ ] **Step 5: Commit（如有文档注记）+ 收尾**

```bash
git add -A
git commit -m "chore(crawl): P1 complete — fully recipe/skill driven, no per-site adapters"
```

---

## Self-Review

**Spec 覆盖：**
- 双轨架构（Tier1 配方/Tier2 skill）→ Task 1-11（Tier1）+ Task 12（Tier2 框架）。✅
- 标准 JSONPath + DSL 语义文档 → Task 2（schema+静态校验）+ Task 3-4（求值）+ Task 5（模式）。✅
- RecipeEngine + execute + 扩展信号 → Task 6。✅
- 一次性切 execute（无双协议）→ Task 10。✅
- 删 6 adapter + registry 动态化 → Task 7 + Task 11。✅
- enum 解耦（OFFICIAL meta-type 保留）→ Task 13 Step 1。✅
- adapter 内嵌护栏（XXE/url_filter/@type filter/css zip/html_unescape）→ Task 5。✅
- fetch 绑 request（保 public_ats 通道）→ Task 6 execute + Task 10 lambda 闭包。✅
- 测试等价（CrawledPostingRecord 层）→ Task 8/9 parity + Task 10 旧端到端零改动。✅
- scripts/crawl_jobs.py 处置 → Task 11 Step 3。✅
- Tier 2 skill 完整集留后续 → Task 12 只建框架 + workday 示范。✅（spec 明确）

**Placeholder 扫描：** 无 TBD/TODO；配方字段映射全部来自 CodeGraph 核实的 adapter 源码；代码块完整。

**类型一致性：** `RecipeEngine.execute(request, fetch) -> AdapterFetchResult`（Task 6 定义）↔ Task 10 调用一致；`AdapterFetchResult.status_code/body_prefix`（Task 6 加）↔ Task 10 signals 用一致；`build_recipe_registry() -> dict[str,RecipeEngine]`（Task 7）↔ Task 11 用一致；`run_steps`/`evaluate_extract`（Task 3-5）签名在 Task 6/8 复用一致。

**风险提示（非阻塞）：**
- Task 1 Step 2 的 jsonpath 包名需落地确认（`python-jsonpath` vs `jg-rp`）。
- Task 4 merge 的 id 关联假设 list row 有 `id` 字段（greenhouse/ashby 有，lever 有）——lever 单步无 detail，不受影响。
- Task 9 css 选择器近似旧正则，可能需调；等价测试是回归门。
- Task 10 是大爆炸关键点：旧 `test_greenhouse_bulk_fetch.py` 必须零改动通过，否则 detail 多步语义有偏差，回 Task 4 调。
