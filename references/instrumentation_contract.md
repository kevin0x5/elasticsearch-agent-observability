# Instrumentation Contract

The full field dictionary lives in [`telemetry_schema.md`](telemetry_schema.md).
This file answers a different question: **which fields power which part of the generated pipeline, and in what order should an agent emit them.**

## The three tiers

```
┌─ Tier 1 (baseline) ────────── bootstrap gives you this for free
├─ Tier 2 (semantic)  ────────── agent wires these when it touches tool/model code
└─ Tier 3 (operational) ────────── nice-to-have, unblocks richer alerts
```

The generated Kibana dashboard and the `alert_and_diagnose.py` rules are layered so each tier unlocks strictly more surface.
**If Tier 2 is empty, half the dashboard stays empty.** That is the intended signal — don't paper over it with fake data.

## Tier 1 — baseline (free after bootstrap)

Provided by the OTel Collector config, HTTP auto-instrumentation, or the LLM proxy.

| Field | Source | Powers |
|---|---|---|
| `@timestamp` | ingest pipeline | every query's time filter |
| `event.duration` | HTTP / gRPC instrumentation | p50/p95 latency charts, `latency_degradation` alert |
| `event.outcome` | HTTP status derived | error-rate chart, `error_rate_spike` alert |
| `service.name` | `OTEL_SERVICE_NAME` | service filter in every dashboard |
| `gen_ai.request.model` | LLM proxy or HTTP headers | per-model latency chart |
| `gen_ai.usage.input_tokens` / `output_tokens` | LLM proxy or SDK response | token consumption chart, `token_consumption_anomaly` alert |

**Minimum signals on the dashboard after Tier 1**: request rate, latency distribution, error rate, total tokens.

## Tier 2 — semantic (agent adds these at tool/model call sites)

The agent has to wrap its own call sites. The Python or Node instrumentation bundle ships `tracedToolCall` / `tracedModelCall` wrappers; using them emits Tier 2 fields automatically.

| Field | Where to set | Powers |
|---|---|---|
| `gen_ai.tool.name` | every tool-call span | tool-level latency/error panels, `error_rate_spike` root cause |
| `gen_ai.conversation.id` | span context propagated through a user session | `session_failure_hotspot` alert, session drill-down |
| `gen_ai.agent_ext.turn_id` | span context per conversation turn | `long_turn_hotspot` alert, turn-level diffing |
| `gen_ai.agent_ext.component_type` | one of `tool` / `llm` / `mcp` / `memory` / `knowledge` / `guardrail` / `runtime` | per-component dashboards; filters in every alert |
| `gen_ai.operation.name` | `tool_call` / `chat` / `retrieval` / `guardrail_check` | logical type faceting (separate from component tier) |

**Minimum signals on the dashboard after Tier 2**: everything above _plus_ tool mix, model mix, per-session view, per-turn view, retry-storm detection.

## Tier 3 — operational (unlocks sharper alerts)

| Field | Powers |
|---|---|
| `gen_ai.agent_ext.retry_count` | `retry_storm` alert |
| `error.type` | RCA phrasing ("timeout" vs "application-level"); tightens `error_rate_spike` recommendations |
| `gen_ai.agent_ext.latency_ms` | explicit turn latency for `long_turn_hotspot` |
| `gen_ai.agent_ext.cost` | cost panels in the dashboard |
| `gen_ai.guardrail.*` | safety dashboards (see `telemetry_schema.md`) |
| `gen_ai.evaluation.*` | regression dashboards (see `telemetry_schema.md`) |

## Rules

1. **Don't invent field names.** If a field isn't listed here or in `telemetry_schema.md`, the dashboard doesn't consume it, and adding it silently doesn't help. Propose it via a PR that also updates a panel.
2. **Don't fake Tier 2.** Writing `gen_ai.tool.name = "unknown"` for every span defeats the dashboard. Leave the field unset if you don't know it.
3. **Internal events are tagged with `event.dataset`.** `internal.sanity_check` and `internal.alert_check` are filtered out of every baseline query so they never skew rates. If you emit your own internal events, follow the same convention.

## Reading order for an agent doing self-extension

1. This file (what fields count).
2. `post_bootstrap_playbook.md` (what to do next, in order).
3. `telemetry_schema.md` (full field dictionary).
4. `config_guide.md` (operational contract).
