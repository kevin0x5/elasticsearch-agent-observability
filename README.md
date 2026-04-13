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
It turns a workspace into a ready observability starter for Elasticsearch and Kibana: OpenTelemetry Collector configuration, Elasticsearch assets, Kibana dashboards, alert diagnosis, and drift validation.

## Advantages

- **Less setup drag**: the agent does not need to hand-wire OpenTelemetry Collector configuration, Elasticsearch assets, and Kibana assets one by one.
- **One operational loop**: bootstrap, diagnose, validate, and archive RCA all live in one skill instead of scattered scripts.
- **Built for agent systems**: the discovery flow understands model calls, tool calls, MCP surfaces, token usage, and agent failure paths better than a generic Elasticsearch starter.
- **Closed knowledge loop**: diagnosis output can go straight into `elasticsearch-insight-store`, so incident knowledge does not disappear after the fix.
- **Safe defaults**: credentials stay in environment variables by default, and the ingest pipeline redacts sensitive generative AI fields.

## What the skill generates

- **Workspace discovery**: detect runtime modules, model adapters, tool registries, and MCP surfaces
- **Collection layer**: OpenTelemetry Collector configuration, environment files, and launch scripts
- **Elasticsearch assets**: data streams, component templates, index templates, ingest pipelines, and lifecycle policies
- **Kibana assets**: data views, saved searches, Lens visualizations, and an overview dashboard
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
  --generated-dir <generated-dir>
```

## Generated output

```text
generated/bootstrap/
├── discovery.json
├── otel-collector.generated.yaml
├── run-collector.sh
├── agent-otel.env
├── agent_otel_bootstrap.py
├── elastic-native/
├── elasticsearch/
│   ├── component-template-*.json
│   ├── index-template.json
│   ├── ingest-pipeline.json
│   ├── ilm-policy.json
│   ├── kibana-saved-objects.json
│   └── apply-summary.json
└── bootstrap-summary.md
```

## Running the Collector

The generated launcher expects a working `otelcol-contrib` binary.
Use one of these paths:

- **Preferred**: use an already installed `otelcol-contrib`
- **Safe local fallback**: download a pinned official `otelcol-contrib` release into a workspace-local tools directory and point `OTELCOL_BIN` at it
- **Do not claim the Collector is running** if neither of those is true; switch to another ingest mode instead of pretending the collection layer is live

A safe workspace-local launch looks like this:

```bash
mkdir -p tools/otelcol/0.102.1 generated/bootstrap/runtime
curl -L -o tools/otelcol/0.102.1/otelcol-contrib.tar.gz <official-release-url>
tar -xzf tools/otelcol/0.102.1/otelcol-contrib.tar.gz -C tools/otelcol/0.102.1
OTELCOL_BIN="$PWD/tools/otelcol/0.102.1/otelcol-contrib" \
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

## Requirements

- Python 3.10+
- Elasticsearch 9.x
- Kibana
- `otelcol-contrib` with `spanmetrics` and the Elasticsearch exporter
- Basic license is enough

## Dependencies

- **Repository scripts**: Python standard library only
- **Generated instrumentation snippet**: install `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-grpc` in the target agent project
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
