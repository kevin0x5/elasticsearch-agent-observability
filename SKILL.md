---
name: elasticsearch-agent-observability
description: "Use this skill when a user wants to bootstrap agent observability on Elasticsearch, OpenTelemetry, and Kibana. Inspect the workspace, render Collector and Elasticsearch assets, prepare a Kibana entry surface, and optionally dry-run or apply those assets as a working starter setup."
---

# Elasticsearch Agent Observability

## Purpose

Bootstrap a practical **Elastic-side starter surface** for an agent.

Treat this skill as a builder for the base layer:

- inspect the workspace
- render artifacts
- preview the apply plan
- apply assets
- smoke-check the query path

Do not present it as a full observability platform if the repo only prepares the base layer.

## Trigger Conditions

Trigger for requests like:

- “给这个 agent 建可观测能力”
- “用 Elasticsearch 给当前 agent 接观测”
- “帮我生成 OTel / Elasticsearch / Kibana 这一套”
- “给某个 agent 准备 Collector、索引模板、ILM 和 Kibana 入口”
- "add observability to this agent"
- "set up OpenTelemetry, Elasticsearch, and Kibana for this workspace"
- "generate the Collector, Elasticsearch, and Kibana assets"
- "prepare drift checks and diagnosis for this agent"

## Preferred Operating Path

Prefer `scripts/bootstrap_observability.py` for the main flow.

That path should:

1. validate the workspace
2. discover likely monitorable modules
3. render Collector config, env file, launcher, and OTLP HTTP bridge fallback artifacts
4. render Elasticsearch assets
5. render Elastic-native starter assets for APM / RUM / profiling when the ingest mode calls for it
6. optionally generate a Python instrumentation starter file
7. optionally dry-run or apply Elasticsearch and Kibana assets
8. optionally generate a smoke report after a real apply

## Product Boundary

Keep the boundary honest.

Current repo capabilities are best described as:

- Collector-side integration artifacts (traces + logs + metrics pipelines)
- OTLP HTTP bridge fallback artifacts for logs/traces when the Collector Elasticsearch exporter is the blocked layer
- Elasticsearch storage assets (data streams, ECS mappings, component templates, tiered ILM)
- Kibana data view, saved search, Lens visualizations, and a starter dashboard
- Elastic-native starter assets for APM traces, Kibana native app entrypoints, trace-analysis playbooks, browser RUM, UX rollout, and profiling notes
- standalone alert + root-cause analysis script (no Kibana Alerting license needed)
- alert → insight-store bridge for automatic RCA conclusion archival
- auto-instrumentation starter snippet for Python agents (monkey-patches OpenAI / Anthropic on import), plus a Node.js / TypeScript preloadable bundle (`@opentelemetry/sdk-node` + HTTP/Undici + `tracedToolCall` / `tracedModelCall` wrappers) for TS-first runtimes such as `openclaw/openclaw`
- LLM proxy starter bundle (LiteLLM docker-compose) for zero-code observability of upstream OSS agents you do not want to fork; the proxy emits the same `gen_ai.*` span attributes that the rest of this pipeline already understands
- dry-run planning before touching a live ES / Kibana target
- configuration drift detection between local assets and live cluster
- observability maturity scoring with upgrade guidance
- dashboard extensions via external JSON/YAML panel declarations
- ECS / GenAI-native ingest contract with no legacy flat-field remap
- all features target the Basic (free) Elasticsearch license

Do not claim that the repo already:

- rewires the agent SDK automatically
- makes arbitrary runtime instrumentation disappear
- ships a complete Kibana observability suite
- performs deep semantic parsing of arbitrary telemetry

## Collector Rule

The generated Collector config uses contrib-only components such as `spanmetrics` and the Elasticsearch exporter.
Default the launcher to `otelcol-contrib`, or document the need for an equivalent custom Collector distribution.
Do not imply that a minimal core `otelcol` binary is enough.
If OTLP receive is healthy but the Elasticsearch exporter is the blocked layer, the generated OTLP HTTP bridge fallback is the honest short-term escape hatch for logs/traces.

## Security Rule

Prefer env-placeholder credentials by default.
Only embed Elasticsearch credentials into YAML when the operator explicitly asks for it.
Treat embedded YAML as secret material.

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
- `render_elastic_agent_assets.py`
- `render_instrument_snippet.py` (Python or Node/TS via `--runtime`)
- `render_llm_proxy_starter.py` (zero-code path for upstream OSS agents)
- `alert_and_diagnose.py`
- `apply_elasticsearch_assets.py`
- `generate_report.py`
- `validate_state.py`
- `verify_pipeline.py` (post-apply canary + ES poll; auto-runs after `--apply-es-assets`)

## Self-Extension Rule

Bootstrap is the first step, not the finish line. After assets are applied:

1. Trust `verify_pipeline.py` as the source of truth for "the pipeline works". It runs automatically after `--apply-es-assets`; read its `verdict` in `verify.json`. If it is not `ok`, follow the `next_step` it prints — do not move on to instrumentation work until the canary lands.
2. First-install default: point the agent at the OTLP HTTP bridge (`http://127.0.0.1:14319`). It's the narrower, more reliable path. Graduate to the native Collector ES exporter once the bridge path is stable; re-run verify when you do.
3. Follow `references/post_bootstrap_playbook.md` in order. Level 0 is verify; each Level 1 item then fills a specific empty Kibana panel.
4. Only emit fields listed in `references/instrumentation_contract.md` or `references/telemetry_schema.md`. Unknown fields do not feed any panel or alert.
5. If bootstrap left ES credentials in the YAML (for example when an agent took a shortcut to finish end-to-end), rotate and switch to env / API key per `references/credentials_playbook.md` before declaring the setup "production".
6. When adding a new field that needs a new panel or alert, update all four touchpoints in one PR: `instrumentation_contract.md`, `telemetry_schema.md`, `render_es_assets.py`, `alert_and_diagnose.py`.

## References

Read these before changing promises or output shape:

- `references/instrumentation_contract.md` — tiered field contract; which fields power which panel/alert
- `references/post_bootstrap_playbook.md` — ordered self-extension checklist
- `references/credentials_playbook.md` — how to move from "it runs" to "it's safe"
- `references/config_guide.md` — operational contract (bootstrap flags, dry-run, rollout rules)
- `references/telemetry_schema.md` — full field dictionary
- `references/architecture.md`
- `references/reporting.md`
- `references/runtime_compat.md`
