---
name: elasticsearch-agent-observability
description: "Use Elasticsearch, OpenTelemetry, and Kibana to build observability for the current agent or a specified agent: generate collection config, storage assets, lifecycle policy, and Kibana report surfaces, then apply them when the user wants a working bootstrap."
---

# Elasticsearch Agent Observability

## What This Skill Is

`Elasticsearch Agent Observability` is not a Markdown report generator.
It is a **native Elastic-stack observability builder for agents**.

Trigger it for requests like:
- “给这个 agent 建可观测能力”
- “用 Elasticsearch 给当前 agent 接观测”
- “帮我把这个 agent 的 OTel / ES / Kibana 观测面搭起来”
- “给某个 agent 生成采集、存储、生命周期和 Kibana 报表入口”

## Default Product Behavior

When triggered, the skill should push toward this product path:

1. inspect the current or specified agent workspace
2. auto-discover monitorable modules
3. generate OTel Collector config and runtime env / launcher artifacts
4. generate Elasticsearch template, ingest pipeline, lifecycle policy, and write-index bootstrap path
5. generate Kibana saved objects for the human-facing report surface
6. apply ES assets and Kibana assets when the user wants a working bootstrap
7. optionally output a smoke Markdown / JSON report to validate the query path

## Core Behavior

The skill should aim to cover these surfaces together:
- collection
- normalization / ingest
- storage
- lifecycle management
- Kibana report surface

Do not frame the product as “just bootstrap docs” when the request is clearly asking for a working observability capability.

## Resolve the Script Path

Scripts live under `scripts/` relative to this `SKILL.md` file.
Resolve absolute paths from the directory that contains this file.

## Output Surface

The main outputs are:
- architecture discovery result
- rendered OTel Collector config
- Collector launcher and agent env template
- Elasticsearch assets and apply summary
- Kibana saved objects bundle
- optional smoke Markdown / JSON report

## Commands

- `bootstrap_observability.py`
- `apply_elasticsearch_assets.py`
- `discover_agent_architecture.py`
- `render_collector_config.py`
- `render_es_assets.py`
- `generate_report.py`

## Important Notes

- Use Elasticsearch 9.x compatible assets
- Keep the human-facing report story on Kibana, not on Markdown alone
- Treat Markdown / JSON reports as smoke / fallback outputs, not the main long-term UI story
- Keep sensitive prompts, args, and results in redacted or summarized form by default
- Do not pretend the repo rewired the agent SDK if it only generated Collector + env artifacts
- For deeper rules, read `references/architecture.md`, `references/config_guide.md`, `references/telemetry_schema.md`, and `references/reporting.md`
