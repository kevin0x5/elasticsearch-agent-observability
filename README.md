# elasticsearch-agent-observability

> 指一个 agent 目录，自动发现该监测什么，一键生成 OTel Collector 配置 + Elasticsearch 9.x 资产 + 报表定义。

## 解决什么问题

你有一个 agent（或者一个 skill），你想给它建可观测能力：traces、logs、报表。

传统做法是手写 Collector 配置、手建 index template、手配 ILM、手写聚合查询 —— 每换一个 agent 就重来一遍。

这个 skill 的做法不同：**先扫描 agent 代码结构，自动发现应该监测哪些模块，然后据此生成所有配置和资产。**

```
agent 目录 → 架构发现 → Collector 配置 + ES 资产 + ILM + 报表定义
```

不需要你知道 OTel Collector 怎么配，也不需要你手写 mapping。

## 30 秒体验

```bash
# 一条命令完成：发现 + 生成 Collector 配置 + ES 资产 + 总结
python scripts/bootstrap_observability.py \
  --workspace /path/to/your-agent \
  --output-dir generated/bootstrap \
  --es-url http://localhost:9200

# 看看它发现了什么
cat generated/bootstrap/discovery.json | python -m json.tool | head -30

# 看看生成了什么
ls generated/bootstrap/
# → discovery.json
# → otel-collector.generated.yaml
# → elasticsearch/index-template.json
# → elasticsearch/ingest-pipeline.json
# → elasticsearch/ilm-policy.json
# → elasticsearch/report-config.json
# → bootstrap-summary.md
```

也可以分步执行：

```bash
# 单独发现
python scripts/discover_agent_architecture.py \
  --workspace /path/to/your-agent \
  --output generated/discovery.json

# 单独生成 Collector 配置
python scripts/render_collector_config.py \
  --discovery generated/discovery.json \
  --output generated/collector.yaml

# 单独生成 ES 资产
python scripts/render_es_assets.py \
  --discovery generated/discovery.json \
  --output-dir generated/es-assets

# 从 ES 生成报表
python scripts/generate_report.py \
  --config generated/bootstrap/elasticsearch/report-config.json \
  --es-url http://localhost:9200 \
  --output generated/report.md
```

## 它能发现什么

discovery 会自动识别这些模块类型：

| 模块类型 | 示例信号 | 触发条件 |
|----------|---------|---------|
| agent manifest | runs, turns, errors | 发现 SKILL.md / agents.md |
| runtime entrypoint | latency, errors | 发现 main.py / app.py / `__main__` |
| command surface | command calls, latency | 发现 argparse subcommands / click |
| tool registry | tool calls, tool errors | 发现 tool / function_call 相关代码 |
| model adapter | token usage, cost | 发现 openai / anthropic / completion |
| memory store | cache hits, sync events | 发现 memory / cache / retrieval |
| MCP surface | mcp calls, session events | 发现 mcp / jsonrpc / tools/call |
| workflow orchestrator | task latency, failures | 发现 workflow / pipeline / planner |

发现结果决定了 Collector 配置里标记哪些模块、ES 资产里预设哪些字段、报表里统计哪些指标。

## 生成的报表长什么样

```markdown
# Agent Observability Report

- time_range: `now-24h`
- documents: `1842`
- success_rate: `0.9631`
- p50_latency_ms: `120`
- p95_latency_ms: `480`
- token_input_total: `125000`
- cost_total: `3.47`

## Top tools
- web_search: 412
- read_file: 389

## Error types
- timeout: 23
- rate_limit: 15
```

## 目标环境

| 环境 | 状态 |
|------|------|
| 自建 Elasticsearch 9.x | ✅ 首发支持 |
| 腾讯云 Elasticsearch Service 9.x | ✅ 首发支持 |
| Elastic Cloud / 托管 OTLP | 🔜 后续扩展 |

## 跟手写配置有什么不同

| | 手写 | 这个 skill |
|---|---|---|
| 了解 agent 结构 | 你自己分析 | 自动发现 |
| Collector 配置 | 手写 YAML | 自动生成 |
| Index template | 手写 JSON | 自动生成，含 ILM 引用 |
| Ingest pipeline | 手写 | 自动生成，含默认脱敏 |
| ILM / 生命周期 | 手配 | 自动生成，含 rollover |
| 报表 | 手写聚合 | 一条命令出 Markdown |
| 换一个 agent | 全部重来 | 重新 bootstrap 就行 |

## 仓库结构

```
SKILL.md              # agent 行为协议
scripts/              # 发现、渲染、引导、报表
references/           # 架构、配置、字段、报表、运行时说明
assets/               # 默认模板（Collector / ES / 报表）
generated/            # 产出物（gitignore）
```
