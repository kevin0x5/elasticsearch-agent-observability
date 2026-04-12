# elasticsearch-agent-observability

> Bootstrap agent observability on the Elastic stack: OpenTelemetry for collection, Elasticsearch for storage, Kibana for the first human-facing surface.

## What This Repo Actually Is

This repo is an **observability bootstrap tool** for agents.

It is not a full observability platform.
It does not rewrite your agent runtime.
It does not auto-instrument arbitrary code.

What it does well:

- inspect a workspace
- discover likely monitorable modules
- render OTel Collector config
- render Elasticsearch assets
- apply those assets when asked
- generate a small smoke report
- prepare a Kibana entry surface

The product path is:

**discover -> render -> apply -> smoke-check**

## What Is Real In The Current Version

This version can already:

- validate the target workspace path
- scan code-like files while ignoring generated samples, docs, tests, and assets
- generate a Collector config, env file, and launcher script
- keep credentials out of YAML by default
- render index template, ingest pipeline, ILM policy, report config, and Kibana saved objects
- stamp `captured_at` at ingest time when upstream data does not provide it
- apply Elasticsearch assets and optional Kibana saved objects
- generate a smoke Markdown or JSON report from the same report config contract

## Quick Start

```bash
python scripts/bootstrap_observability.py \
  --workspace /path/to/your-agent \
  --output-dir generated/bootstrap \
  --es-url http://localhost:9200 \
  --apply-es-assets \
  --kibana-url http://localhost:5601 \
  --apply-kibana-assets
```

## What You Get

```text
generated/bootstrap/
├── discovery.json
├── otel-collector.generated.yaml
├── run-collector.sh
├── agent-otel.env
├── report.md
├── elasticsearch/
│   ├── index-template.json
│   ├── ingest-pipeline.json
│   ├── ilm-policy.json
│   ├── report-config.json
│   ├── kibana-saved-objects.json
│   ├── kibana-saved-objects.ndjson
│   └── apply-summary.json
└── bootstrap-summary.md
```

Key outputs:

- `discovery.json`: detected modules and recommended signals
- `otel-collector.generated.yaml`: Collector config
- `agent-otel.env`: runtime env template for the agent process
- `run-collector.sh`: portable launcher that uses sibling-relative paths
- `index-template.json`: mapping and rollover settings
- `ingest-pipeline.json`: light normalization, redaction, and `captured_at` fallback
- `report-config.json`: shared reporting contract, including `time_field`
- `kibana-saved-objects.*`: current Kibana entry bundle
- `apply-summary.json`: what was actually applied
- `report.md`: smoke output, not the long-term UI

## What Kibana Means Here

Kibana is the main human-facing surface.

But be precise about the current state:

- current bundle gives you a **data view + event stream search + failure search + starter dashboard**
- it still does **not** try to be a complete Lens-heavy observability product

So the repo now prepares a stronger Kibana entrypoint, while still staying in bootstrap territory instead of pretending to be a full observability console.

## Current Boundaries

Keep these boundaries explicit:

- this repo does **not** rewrite the agent SDK or runtime code
- it assumes the agent can emit OTLP or can be wired to do so
- normalization is light; it is not deep semantic parsing
- the main storage target is one shared events alias for logs and traces
- apply mode assumes Elasticsearch and Kibana are reachable

## Security Defaults

The default path is conservative:

- credentials stay in env placeholders unless `--embed-es-credentials` is used
- prompt and tool payload fields are removed in the ingest pipeline by default
- generated files stay readable JSON / YAML / shell, not hidden state

If `--embed-es-credentials` is used, treat the Collector YAML as secret material.

## When This Repo Is Useful

This repo is a good fit when:

- Elasticsearch is already the target stack
- the team wants a fast bootstrap instead of building assets by hand
- the user needs a traceable Collector + ES + Kibana starter surface

This repo is not the right description if the claim is:

- “point it at any agent and it auto-instruments everything”
- “it already ships a full Kibana dashboard product”
- “it deeply understands every agent event schema out of the box”

## Repo Layout

```text
SKILL.md      Trigger and execution contract
scripts/      Discovery, rendering, apply, reporting
references/   Config and reporting rules
generated/    Default output directory
```
