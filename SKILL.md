---
name: elasticsearch-agent-observability
description: "Use this skill when a user wants to set up Elasticsearch as the backend for AI agent observability. One bootstrap gives ES storage, Kibana dashboards, RCA alerting, and evaluation — all aligned to OTel GenAI Semantic Conventions."
---

# Elasticsearch Agent Observability

给 AI agent 接 Elasticsearch 可观测。一条命令出全套：索引模板、ILM、ingest pipeline、Kibana 仪表板、告警 + 根因分析。

不是完整可观测平台。只管 ES 后端的 data layer。

## Trigger Conditions

- "给这个 agent 建可观测能力"
- "用 Elasticsearch 给当前 agent 接观测"
- "add observability to this agent"
- "set up OTel + Elasticsearch + Kibana for this workspace"
- "帮我看看 agent 为什么慢 / 为什么报错"
- "run evaluation / check agent quality"

## Operating Path

### First-time setup

```
1. quickstart.py --agent-dir <path> --apply
   （自动检测框架、生成配置、apply 到 ES/Kibana）

2. 或者分步走：
   bootstrap_observability.py --workspace <path> --output-dir <dir> \
     --es-url <url> --apply-es-assets --kibana-url <url> --apply-kibana-assets
```

### After bootstrap

```
3. doctor.py --es-url <url>
   （确认管线健康，看 instrumentation coverage 缺什么字段）

4. 按 doctor 输出的 fix 补埋点（或让 AI agent 自动补）

5. alert_and_diagnose.py --es-url <url> --time-range now-15m
   （告警 + 根因分析）
```

### Day-2 operations

| 我想… | 跑 |
|-------|-----|
| 检查管线健康 | `doctor.py` |
| 看告警 + 根因 | `alert_and_diagnose.py` |
| 看成本分布 | `model_pricing.py summary` |
| 跑回归评估 | `evaluate.py run` |
| 回放一个 session | `replay.py --session-id <id>` |
| 看部署了什么 | `status.py` |
| 检测配置漂移 | `validate_state.py` |
| 卸载 | `uninstall.py --confirm` |

统一入口：`cli.py <command>`（init / quickstart / doctor / alert / cost / eval / replay / status / validate / uninstall / scenarios）

## Boundary

**是：** ES 后端的 agent 可观测 data layer — 采集、存储、查询、告警、评估。

**不是：** 完整可观测平台、agent runtime、prompt management、UI 产品。

不要声称：
- 自动重连 agent SDK
- 完整的 Kibana 可观测套件
- 深度语义解析任意遥测

## Key Rules

**Pipeline honesty** — 不要拿 `/healthz` 200 当"管线正常"。用 `doctor.py`，只有 `healthy` 才算。

**Credential safety** — 默认 env placeholder，不落盘。只有用户明确要求才 inline。

**Reasoning trace PII** — `rationale` 截断 500 字符、`input_summary` 截断 300 字符。ingest pipeline 强制执行，防止 PII 无限落库。

**Instrumentation coverage** — doctor 会告诉你缺什么字段。缺 Tier 2 字段不代表管线坏了，代表面板是空的。按 fix 补。

## Commands

核心 5 个：

| 脚本 | 用途 |
|------|------|
| `bootstrap_observability.py` | 一键生成全套资产 + 可选 apply |
| `doctor.py` | 5 项诊断 + 埋点覆盖度检查 |
| `alert_and_diagnose.py` | 6 种告警 + 根因分析 + 因果链 |
| `evaluate.py` | 7 个回归评估器（含 LLM-as-Judge） |
| `replay.py` | session 回放（嵌套 span 树） |

辅助：

| 脚本 | 用途 |
|------|------|
| `quickstart.py` | 引导式一键设置（自动检测框架） |
| `model_pricing.py` | 成本查询 / 回填 |
| `status.py` | 集群资产状态 |
| `validate_state.py` | 配置漂移检测 |
| `uninstall.py` | 安全卸载（默认 dry-run） |
| `instrument_frameworks.py` | 框架自动插桩（AutoGen/CrewAI/LangGraph/OpenAI Agents） |

渲染（通常由 bootstrap 内部调用）：

`render_es_assets.py` / `render_collector_config.py` / `render_otlp_http_bridge.py` / `render_elastic_agent_assets.py` / `render_instrument_snippet.py` / `render_llm_proxy_starter.py`

## Self-Extension

1. `doctor.py` 是 source of truth。verdict 不是 `healthy` → 先修管线。
2. 首次装机走 bridge（`:14319`）。稳了再升级到 Collector。
3. 按 `references/post_bootstrap_playbook.md` 的顺序补字段，不要跳。
4. 只 emit `references/telemetry_schema.md` 里列的字段。
5. 新字段要同时改 4 个文件：`instrumentation_contract.md`、`telemetry_schema.md`、`render_es_assets.py`、`alert_and_diagnose.py`。

## References

- `references/instrumentation_contract.md` — 字段分层（Tier 1/2/3）
- `references/telemetry_schema.md` — 完整字段字典
- `references/post_bootstrap_playbook.md` — bootstrap 后自检清单
- `references/config_guide.md` — 操作契约
- `references/credentials_playbook.md` — 凭证安全
