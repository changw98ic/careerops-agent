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
