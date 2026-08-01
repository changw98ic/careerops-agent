---
source_type: example            # 替换为该站 source_type（如 workday）
executor_mode: ego              # ego = 浏览器驱动的 LLM agent
match:
  host_suffix: example.com      # 该站特征主机后缀
---
# 站点抓取 skill（模板）

> 用法：复制此目录、改名为该站 source_type，把下方替换成该站的具体抓取步骤。
> CrawlAgent 在 Tier 2 兜底时按本 playbook 引导 LLM 决策。
> skill 是**数据**（playbook），不是 per-site Python 代码——不要在此写可执行逻辑。

## 步骤
1. （写明入口与列表端点）
2. （写明翻页 / facet 细分 / 上限绕过策略）
3. （写明详情二次抓取）
4. （写明去重与归一化字段：title / location / description / apply_url）
