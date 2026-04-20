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
- **Elastic-native starter bundle**: APM env + entrypoint guide, surface manifest, preflight checklist, trace-analysis playbook, browser RUM config/snippet, UX playbook, and profiling rollout checklist
- **Instrumentation starter (Python *or* Node/TS)**: Python auto-patches OpenAI / Anthropic on import; Node/TS emits a preloadable `@opentelemetry/sdk-node` bundle with HTTP/Undici auto-instrumentation plus `tracedToolCall` / `tracedModelCall` wrappers
- **LLM proxy starter**: Docker Compose bundle that runs LiteLLM in front of OpenAI / Anthropic — zero-code observability for upstream OSS agents you do not want to fork (e.g. `openclaw/openclaw`)
- **Diagnosis flow**: alert checks for error-rate spikes, latency regressions, and token anomalies with RCA output
- **Drift validation**: compare the live Elasticsearch cluster with locally generated assets
- **Knowledge archival**: write RCA results into `elasticsearch-insight-store`
- **Multiple ingest modes**: `collector`, `elastic-agent-fleet`, and `apm-otlp-hybrid`

## Installation

This repository is designed to be used as a skill repository.
Clone it into your agent's skill directory:

```bash
git clone https://github.com/kevin0x5/elasticsearch-agent-observability.git <skill-dir>/elasticsearch-agent-observability
```

The repo uses the `SKILL.md + scripts + references` shape so an agent runtime can call a shared observability workflow.

### Avoiding the "embedded git repo" warning

If `<skill-dir>` is itself inside a git-tracked workspace, the clone above drops a nested `.git/` into that workspace and git will flag it as an embedded repo (commit succeeds but only as a gitlink, not file contents).

Pick one:

- **Quickest**: clone with `--depth 1` and add the path to your outer `.gitignore`.
  ```bash
  git clone --depth 1 https://github.com/kevin0x5/elasticsearch-agent-observability.git <skill-dir>/elasticsearch-agent-observability
  echo "<skill-dir>/elasticsearch-agent-observability/" >> .gitignore
  ```
- **Clean**: install the skill **outside** your agent workspace and point your agent runtime at the absolute path via its skill config.
- **Strict**: add it as a `git submodule`. Most verbose, keeps a pinned SHA.

## Who this is for

- **Agent operators / platform engineers** who own the runtime, deployment environment, or observability stack
- **Application engineers** who can review generated config and coordinate with the operator side
- **Not end users** of the agent product; this repo does not assume a chat user can deploy a Collector, enroll Fleet, or change Kibana/Elasticsearch directly

## Operating model

This repo generates the observability bundle and rollout contract for an agent system.
It is meant to help the people who run or maintain that system, not the people who merely use the agent.

## When to use it

Use this skill for requests like:

- "add observability to this agent service"
- "generate the Collector, Elasticsearch, and Kibana assets for operator review"
- "prepare an Elastic-native rollout bundle for this agent runtime"
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

### Bootstrap with Node.js / TypeScript instrumentation starter

```bash
python scripts/bootstrap_observability.py \
  --workspace <ts-agent-workspace> \
  --es-url <elasticsearch-url> \
  --generate-instrument-snippet \
  --instrument-runtime node
```

Produces `node-instrumentation/agent-otel-bootstrap.mjs` (preload with
`node --import ./agent-otel-bootstrap.mjs`) and a README that documents
the `tracedToolCall` / `tracedModelCall` wrappers.

### Zero-code path for upstream OSS agents (LLM proxy)

```bash
python scripts/bootstrap_observability.py \
  --workspace <agent-workspace> \
  --es-url <elasticsearch-url> \
  --generate-llm-proxy
```

Produces `llm-proxy/docker-compose.yaml` and config for a LiteLLM proxy
that emits OTel spans with the same `gen_ai.*` attributes the generated
dashboards already consume. Point the agent at `http://localhost:4000/v1`
and no source changes are needed.

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
│   ├── preflight-checklist.json
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
- **Preflight / readiness**: `preflight-checklist.json` captures the required Kibana / Fleet / APM rollout inputs, while `apply-summary.json` now also reports native contract drift, blocking checks, and ready counts across `surface-manifest.json` / `rum-config.json` / runtime reachability
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

## Extending after bootstrap

Bootstrap delivers Tier 1 observability (latency, error rate, token totals) out of the box. Tool-level, session-level, and per-turn panels stay empty until the agent emits the corresponding `gen_ai.*` fields — that's by design, so empty panels serve as TODO markers.

`bootstrap_observability.py` ends with an automatic end-to-end verify (a canary OTLP log + ES poll, written to `verify.json`). If it does not return `ok`, follow its `next_step` before anything else — the most common fix on a first install is to point the agent at the OTLP HTTP bridge (`http://127.0.0.1:14319`) instead of the native Collector ES exporter.

Three short docs cover the self-extension path, in recommended reading order:

1. [`references/instrumentation_contract.md`](references/instrumentation_contract.md) — the three tiers of fields and what each one unlocks.
2. [`references/post_bootstrap_playbook.md`](references/post_bootstrap_playbook.md) — ordered checklist for an agent (human or AI) to keep filling the dashboard.
3. [`references/credentials_playbook.md`](references/credentials_playbook.md) — what to do when bootstrap left credentials on disk and you're moving toward production.

## Development

Run the test suite with:

```bash
python3 -m unittest discover -s tests
```

## Security model

- Credentials default to env placeholders; only `--embed-es-credentials` puts them on disk.
- The ingest pipeline redacts sensitive generative AI fields.
- For rotation, least-privilege API keys, and post-bootstrap cleanup, see [`references/credentials_playbook.md`](references/credentials_playbook.md).
- Generated files should still be reviewed before applying them to a live cluster.

## Contributing

See `CONTRIBUTING.md` for contribution workflow and review expectations.

## Security reporting

See `SECURITY.md` for how to report vulnerabilities responsibly.

## License

Apache-2.0. See `LICENSE`.
