---
name: elasticsearch-agent-observability
description: "Use this skill when a user wants to bootstrap agent observability on Elasticsearch, OpenTelemetry, and Kibana. Inspect the workspace, render Collector and Elasticsearch assets, prepare a Kibana entry surface, and apply those assets when the user wants a working starter setup."
---

# Elasticsearch Agent Observability

## Purpose

Bootstrap a practical Elastic-side observability surface for an agent.

Treat this skill as a **starter builder**:

- inspect the workspace
- render artifacts
- apply assets
- smoke-check the query path

Do not present it as a full observability platform if the underlying repo only prepares the base layer.

## Trigger Conditions

Trigger for requests like:

- “给这个 agent 建可观测能力”
- “用 Elasticsearch 给当前 agent 接观测”
- “帮我生成 OTel / ES / Kibana 这一套”
- “给某个 agent 准备 Collector、索引模板、ILM 和 Kibana 入口”

## Preferred Operating Path

Prefer `scripts/bootstrap_observability.py` for the main flow.

That path should:

1. validate the workspace
2. discover likely monitorable modules
3. render Collector config, env file, and launcher
4. render Elasticsearch assets
5. optionally apply Elasticsearch and Kibana assets
6. optionally generate a smoke report

## Product Boundary

Keep the boundary honest.

Current repo capabilities are best described as:

- Collector-side integration artifacts
- Elasticsearch storage assets
- ILM and write-index bootstrap
- Kibana data view and saved-search entry surface

Do not claim that the repo already:

- rewires the agent SDK automatically
- ships a full Kibana dashboard suite
- performs deep semantic parsing of arbitrary telemetry

## Security Rule

Prefer env-placeholder credentials by default.
Only embed Elasticsearch credentials into YAML when the operator explicitly asks for it.
Treat that embedded YAML as secret material.

## Reporting Rule

Treat Kibana as the main human-facing surface.
Treat Markdown / JSON output as smoke or automation output.

Keep the report contract aligned with `report-config.json`, including:

- `events_alias`
- `time_field`
- metric names that actually exist in the current implementation

## Discovery Rule

Use workspace discovery as a heuristic helper, not as absolute truth.
Ignore generated output, docs, references, tests, and asset bundles when scanning for runtime modules.

## Commands

- `bootstrap_observability.py`
- `discover_agent_architecture.py`
- `render_collector_config.py`
- `render_es_assets.py`
- `apply_elasticsearch_assets.py`
- `generate_report.py`

## References

Read these before changing promises or output shape:

- `references/config_guide.md`
- `references/reporting.md`
- `references/telemetry_schema.md`
- `references/architecture.md`
