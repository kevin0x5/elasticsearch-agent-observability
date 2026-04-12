# elasticsearch-agent-observability

> Bootstrap agent observability on the Elastic stack: OpenTelemetry for collection, Elasticsearch for storage, Kibana for the human-facing surface.

## What This Repo Actually Is

This repo is an **observability bootstrap tool** for agents.

It is not a full observability platform.
It does not rewrite your agent runtime.
It does not auto-instrument arbitrary code — but it can generate a ready-to-use instrumentation snippet for Python agents.

What it does well:

- inspect a workspace and discover monitorable modules
- recommend an ingest mode (`collector` / `elastic-agent-fleet` / `apm-otlp-hybrid`)
- render OTel Collector config with **traces + logs + metrics pipelines**, spanmetrics connector, filelog receiver, and probabilistic sampling
- render an Elastic-native starter bundle for Fleet / APM operators
- render Elasticsearch assets using **data streams**, **ECS-compatible mappings**, **component templates**, and **tiered ILM** (hot → warm → cold → frozen → delete)
- render a structured ingest pipeline with ECS field alignment, JSON body parsing, GenAI SemConv preservation, and legacy field migration
- render Kibana saved objects including **Lens visualizations** (event rate, latency, top tools, token usage), **starter dashboard**, **failure search**, and an **error-rate alerting rule**
- optionally apply all of the above to a live cluster
- generate a Python auto-instrumentation bootstrap file with `traced_tool_call` / `traced_model_call` decorators
- generate a smoke report from the same reporting contract

The product path is:

**discover → render → apply → instrument → observe**

## Quick Start

```bash
python scripts/bootstrap_observability.py \
  --workspace /path/to/your-agent \
  --output-dir generated/bootstrap \
  --es-url http://localhost:9200 \
  --apply-es-assets \
  --kibana-url http://localhost:5601 \
  --apply-kibana-assets \
  --generate-instrument-snippet
```

### Ingest Modes

```bash
# Default: Collector-only
--ingest-mode collector

# Elastic Agent + Fleet managed enrollment
--ingest-mode elastic-agent-fleet \
  --fleet-server-url https://fleet.example.com:8220 \
  --fleet-enrollment-token <token>

# Hybrid: Collector for OTLP + Elastic-native for APM/Fleet
--ingest-mode apm-otlp-hybrid \
  --apm-server-url https://apm.example.com:8200
```

## What You Get

```text
generated/bootstrap/
├── discovery.json
├── otel-collector.generated.yaml
├── run-collector.sh
├── agent-otel.env
├── agent_otel_bootstrap.py          ← auto-instrument snippet
├── report.md
├── elastic-native/                   ← only with fleet/hybrid mode
│   ├── elastic-agent-policy.json
│   ├── elastic-agent.env
│   ├── run-elastic-agent.sh
│   └── README.md
├── elasticsearch/
│   ├── component-template-ecs-base.json
│   ├── component-template-settings.json
│   ├── index-template.json           ← data stream backed
│   ├── ingest-pipeline.json           ← ECS + structured parsing
│   ├── ilm-policy.json               ← hot/warm/cold/frozen/delete
│   ├── report-config.json
│   ├── kibana-saved-objects.json
│   ├── kibana-saved-objects.ndjson
│   └── apply-summary.json
└── bootstrap-summary.md
```

## Storage Model

- **Data streams** instead of legacy rollover aliases
- **Component templates**: `{prefix}-ecs-base` (ECS mappings) + `{prefix}-settings` (ILM, pipeline, codec)
- **ECS-compatible field names**: `@timestamp`, `event.outcome`, `service.name`, `trace.id`, `gen_ai.usage.*`, `gen_ai.agent.*`
- **Backward compat**: legacy field names (`agent_id`, `tool_name`, etc.) are automatically renamed by the ingest pipeline
- **GenAI Semantic Conventions**: `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.request.model` etc. are preserved, not deleted

## Kibana Surface

The generated Kibana bundle now includes:

| Object | Type | Description |
|---|---|---|
| Data view | index-pattern | `{prefix}-events*`, time field `@timestamp` |
| Event stream | search | Full event stream in Discover |
| Failure stream | search | `event.outcome:failure` events only |
| Event rate chart | lens (XY) | Event count over time, split by outcome |
| Latency P50/P95 | lens (metric) | `event.duration` percentiles |
| Top tools | lens (pie) | Most-called agent tools |
| Token usage | lens (XY) | Input vs output token trend |
| Overview dashboard | dashboard | All of the above in one view |
| Error rate alert | alert | Fires when error count > 10 in 5 min |

## Current Boundaries

- this repo does **not** rewrite the agent SDK or runtime code
- the auto-instrument snippet requires `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-grpc` to be installed
- the Elastic-native bundle is render-only; it does not call Fleet APIs
- normalization handles JSON body parsing and ECS field mapping, but is not a full schema parser
- the frozen tier config assumes a `found-snapshots` repository; operators should adjust for their cluster
- alerting rules are disabled by default; operators enable and route actions in Kibana

## Security Defaults

- credentials stay in env placeholders unless `--embed-es-credentials` is used
- sensitive GenAI payloads (`gen_ai.prompt`, `gen_ai.completion`, tool arguments/results) are redacted in the ingest pipeline
- generated files stay readable JSON / YAML / Python, not hidden state

## Repo Layout

```text
SKILL.md      Trigger and execution contract
scripts/      Discovery, rendering, apply, instrumentation, reporting
references/   Config and reporting rules
generated/    Default output directory
```
