# elasticsearch-agent-observability

Bootstrap observability for AI agents on Elasticsearch, OpenTelemetry, and Kibana.
This skill inspects a workspace and generates the collection, storage, dashboard, and diagnostic assets needed to make an agent observable without hand-building the whole stack.

## Overview

Most agent projects can run before they can explain themselves.
When latency climbs, token cost drifts, or failures appear, teams usually still lack a usable baseline for answering simple questions:

- which model calls are slow
- which tools fail most often
- where token usage is going
- whether the live cluster still matches the generated configuration

This skill builds that baseline.
It turns a workspace into a ready observability starter for Elasticsearch and Kibana: OpenTelemetry Collector configuration, Elasticsearch assets, Kibana dashboards, Elastic-native APM / RUM / profiling starter assets, alert diagnosis, and drift validation.

## Advantages

- **Less setup drag**: the agent does not need to hand-wire OpenTelemetry Collector configuration, Elasticsearch assets, and Kibana assets one by one.
- **One operational loop**: bootstrap, diagnose, validate, and archive RCA all live in one skill instead of scattered scripts.
- **Built for agent systems**: the discovery flow understands model calls, tool calls, MCP surfaces, token usage, and agent failure paths better than a generic Elasticsearch starter.
- **Closed knowledge loop**: diagnosis output can go straight into `elasticsearch-insight-store`, so incident knowledge does not disappear after the fix.
- **Safe defaults**: credentials stay in environment variables by default, and the ingest pipeline redacts sensitive generative AI fields.

## What the skill generates

- **Workspace discovery**: detect runtime modules, model adapters, tool registries, MCP surfaces, web services, browser frontends, knowledge bases, and guardrails
- **Collection layer**: OpenTelemetry Collector configuration, environment files, and launch scripts
- **Elasticsearch assets**: data streams, component templates (with component-type / guardrail / evaluation / memory fields), index templates, ingest pipelines, and lifecycle policies
- **Kibana assets**: data views, saved searches, Lens visualizations, and an overview dashboard
- **Elastic-native starter bundle**: APM env + entrypoint guide, surface manifest, trace-analysis playbook, browser RUM config/snippet, UX playbook, and profiling rollout checklist
- **Diagnosis flow**: alert checks for error-rate spikes, latency regressions, and token anomalies with RCA output
- **Drift validation**: compare the live Elasticsearch cluster with locally generated assets
- **Knowledge archival**: write RCA results into `elasticsearch-insight-store`
- **Multiple ingest modes**: `collector`, `elastic-agent-fleet`, and `apm-otlp-hybrid`

## Installation

This repository is designed to be used as a skill repository.
Clone it into your agent's skill directory:

```bash
git clone https://github.com/kevin-codelab/elasticsearch-agent-observability.git <skill-dir>/elasticsearch-agent-observability
```

Any agent runtime that resolves `SKILL.md` can load this repository as a skill.

## Compatibility

- **CodeBuddy**
- **Claude Code**
- **OpenClaw** with a thin wrapper that points to the same scripts

## When to use it

Use this skill for requests like:

- "add observability to this agent"
- "set up OpenTelemetry, Elasticsearch, and Kibana for this workspace"
- "generate the Collector, Elasticsearch, and Kibana assets"
- "check whether the observability setup drifted from the cluster"
- "diagnose recent agent failures and store the conclusion"

## Skill contract

Treat this skill as an Elasticsearch observability bootstrapper.

- **`bootstrap`**: inspect the workspace and run the discovery → render → dry-run/apply flow
- **`diagnose`**: run `alert_and_diagnose.py` and return RCA output
- **`validate`**: run `validate_state.py` and compare generated assets with the live Elasticsearch cluster

## Common commands

### Bootstrap the observability stack

```bash
python scripts/bootstrap_observability.py \
  --workspace <workspace> \
  --es-url <elasticsearch-url> \
  --apply-es-assets \
  --apply-kibana-assets
```

### Diagnose recent issues

```bash
python scripts/alert_and_diagnose.py \
  --es-url <elasticsearch-url> \
  --index-prefix <index-prefix>
```

### Store RCA results in the insight store

```bash
python scripts/alert_and_diagnose.py \
  --es-url <elasticsearch-url> \
  --index-prefix <index-prefix> \
  --store-to-insight <path-to-store.py>
```

### Validate cluster drift

```bash
python scripts/validate_state.py \
  --es-url <elasticsearch-url> \
  --assets-dir <assets-dir>
```

## Generated output

```text
generated/<output-dir>/                # default: generated/bootstrap
├── discovery.json
├── otel-collector.generated.yaml
├── run-collector.sh
├── agent-otel.env
├── otlphttpbridge.py
├── run-otlphttpbridge.sh
├── agent-otel-bridge.env
├── agent_otel_bootstrap.py
├── elastic-native/
│   ├── elastic-agent-policy.json
│   ├── elastic-agent.env
│   ├── run-elastic-agent.sh
│   ├── apm-agent.env
│   ├── apm-entrypoints.md
│   ├── surface-manifest.json
│   ├── trace-analysis-playbook.md
│   ├── rum-config.json
│   ├── rum-agent-snippet.js
│   ├── ux-observability-playbook.md
│   └── profiling-starter.md
├── elasticsearch/
│   ├── component-template-*.json
│   ├── index-template.json
│   ├── ingest-pipeline.json
│   ├── ilm-policy.json
│   ├── kibana-saved-objects.json
│   ├── kibana-saved-objects.ndjson
│   └── apply-summary.json
└── bootstrap-summary.md
```

## Running the Collector

The generated launcher expects a working `otelcol-contrib` binary.
Use one of these paths:

- **Preferred**: use an already installed `otelcol-contrib`
- **Safe local fallback**: download a pinned official `otelcol-contrib` release into a workspace-local tools directory and point `OTELCOL_BIN` at it
- **Parallel debug Collector**: if you need a second collector for OTLP-vs-exporter isolation, rerender with a different `--telemetry-metrics-port`; Collector self-telemetry binds `127.0.0.1:8888` by default
- **Do not claim the Collector is running** if neither of those is true; switch to another ingest mode instead of pretending the collection layer is live

A safe workspace-local launch looks like this:

```bash
mkdir -p tools/otelcol/<version> generated/bootstrap/runtime
curl -L -o tools/otelcol/<version>/otelcol-contrib.tar.gz <official-release-url>
tar -xzf tools/otelcol/<version>/otelcol-contrib.tar.gz -C tools/otelcol/<version>
OTELCOL_BIN="$PWD/tools/otelcol/<version>/otelcol-contrib" \
  nohup generated/bootstrap/run-collector.sh \
  > generated/bootstrap/runtime/collector.log 2>&1 &
echo $! > generated/bootstrap/runtime/collector.pid
```

Operational minimums:

- **Pin the version**: do not pull an unversioned "latest" binary
- **Record provenance**: keep the release URL and checksum with the rollout notes
- **Keep logs and PID**: background launch without a log file and PID file is not an operable setup
- **Stop cleanly**: `kill "$(cat generated/bootstrap/runtime/collector.pid)"`
- **Rollback cleanly**: remove the local binary path from `OTELCOL_BIN`, stop the process, and keep the generated config bundle for audit

## OTLP HTTP bridge fallback

When OTLP receive is healthy but the Collector Elasticsearch exporter is still incompatible, the generated bundle also includes a **logs/traces-only** bridge fallback:

- `otlphttpbridge.py`: local OTLP HTTP bridge that writes directly to `agent-obsv-events`
- `run-otlphttpbridge.sh`: launcher for the bridge process
- `agent-otel-bridge.env`: env template that points OTLP HTTP logs/traces at `http://127.0.0.1:14319`

This path is intentionally conservative:

- **Logs + traces only**: metrics stay on the Collector path
- **Fallback, not replacement**: prefer the native Collector → Elasticsearch path when it works
- **Local-first**: the default bind is `127.0.0.1:14319`, so it does not compete with the usual `4317` / `4318` Collector receiver ports

Minimal launch shape:

```bash
mkdir -p generated/bootstrap/runtime
nohup generated/bootstrap/run-otlphttpbridge.sh \
  > generated/bootstrap/runtime/otlphttpbridge.log 2>&1 &
echo $! > generated/bootstrap/runtime/otlphttpbridge.pid
```

Stop shape:

```bash
kill "$(cat generated/bootstrap/runtime/otlphttpbridge.pid)"
```

## Elastic-native APM / UX / profiling starter

When the operator chooses `elastic-agent-fleet` or `apm-otlp-hybrid`, the generated `elastic-native/` bundle is no longer just a thin policy stub:

- **APM / tracing**: `apm-agent.env`, `apm-entrypoints.md`, `surface-manifest.json`, and `trace-analysis-playbook.md` point you at Kibana `Services`, `Traces`, and `Service Map` instead of rebuilding trace analysis as custom dashboards
- **User experience monitoring**: `rum-config.json`, `rum-agent-snippet.js`, and `ux-observability-playbook.md` give a direct starter for `@elastic/apm-rum` plus frontend/backend trace correlation
- **Performance profiling**: `profiling-starter.md` documents the rollout contract for Elastic Universal Profiling so host-level performance analysis stays aligned with APM traces

This still stays honest: the repo generates the starter assets and operating contract, but it does not auto-enroll Fleet, auto-patch frontend entrypoints, or auto-install the profiling agent for you.

## Requirements

- Python 3.10+
- Elasticsearch 9.x
- Kibana 9.x
- `otelcol-contrib` 0.87.0+ with `spanmetrics` connector and the Elasticsearch exporter
- Basic license is enough

## Dependencies

- **Repository scripts**: Python standard library only
- **Generated instrumentation snippet** (Python only): install `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-grpc` in the target agent project. Go / Java / TypeScript agents should wire the OTel SDK directly; the generated Collector config and ES assets still work for any OTLP-capable runtime.
- **OTLP HTTP bridge protobuf path**: install `protobuf` and `opentelemetry-proto` only if the sender will post OTLP HTTP protobuf payloads to the generated bridge; OTLP JSON does not need extra packages
- **Optional auto-patching path**: if the target project wants auto-instrumented OpenAI or Anthropic calls, those SDKs must already exist in the target project

## Development

Run the test suite with:

```bash
python3 -m unittest discover -s tests
```

## Security model

- Credentials stay in environment variables by default
- Elasticsearch credentials are embedded into generated YAML only when explicitly requested
- The ingest pipeline redacts sensitive generative AI fields
- Generated files should still be reviewed before applying them to a live cluster

## Contributing

See `CONTRIBUTING.md` for contribution workflow and review expectations.

## Security reporting

See `SECURITY.md` for how to report vulnerabilities responsibly.

## License

Apache-2.0. See `LICENSE`.
