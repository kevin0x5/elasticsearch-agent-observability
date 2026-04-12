# Elasticsearch Agent Observability

Bootstrap observability for an agent on self-hosted Elasticsearch 9.x or Tencent Cloud Elasticsearch Service 9.x.

## What this repo does

This repo helps an agent do four practical things:

- inspect a workspace or agent layout
- auto-discover what should be monitored
- generate OpenTelemetry Collector + Elasticsearch assets
- generate Markdown / JSON reports

## Why this repo is different

This is not a flat logging template.
It first builds a `discovery.json` that explains what kind of agent structure it found, then uses that discovery result to decide what to monitor.

That means the repo can adapt to:

- single-script skills with command surfaces
- multi-step agent workflows
- tool-heavy runtimes
- MCP-aware runtimes
- local cache / memory-heavy runtimes

## Quick flow

```bash
# 1. discover monitorable modules from a workspace
python scripts/discover_agent_architecture.py \
  --workspace /path/to/agent-workspace \
  --output generated/discovery.json

# 2. bootstrap collector config + ES assets + summary in one shot
python scripts/bootstrap_observability.py \
  --workspace /path/to/agent-workspace \
  --output-dir generated/bootstrap

# 3. generate a report from Elasticsearch
python scripts/generate_report.py \
  --config generated/bootstrap/elasticsearch/report-config.json \
  --es-url http://localhost:9200 \
  --output generated/report.md
```

## Main outputs

- `discovery.json`
- generated Collector config
- generated Elasticsearch 9.x assets
- report config
- Markdown / JSON reports

## Repo layout

- `SKILL.md`: runtime behavior protocol
- `scripts/`: discovery, rendering, bootstrap, and reporting
- `references/`: architecture, config, schema, reporting, runtime notes
- `assets/`: default templates for Collector, Elasticsearch, and report config
- `generated/`: repo-local rendered outputs

## Product boundary in v1

This repo is anchored to:

- self-hosted Elasticsearch 9.x
- Tencent Cloud Elasticsearch Service 9.x

It focuses on:

- discovery
- collection setup
- storage assets
- lifecycle defaults
- report generation

It does **not** depend on full Kibana saved-object automation to be useful.
