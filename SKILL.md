---
name: elasticsearch-agent-observability
description: "Use this skill when a user wants to set up Elasticsearch as the backend for OpenLLMetry or any OTel GenAI instrumentation. Renders ES index templates, ingest pipelines, ILM, Kibana dashboards, and RCA alerting — all aligned to OTel GenAI Semantic Conventions."
---

# Elasticsearch Agent Observability

## Purpose

**OpenLLMetry → Elasticsearch adapter.** Bootstrap ES storage, Kibana dashboards, and RCA alerting for GenAI agent telemetry collected via OpenLLMetry or any OTel-based instrumentation.

Treat this skill as a builder for the ES backend layer:

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

When launching the Collector from an interactive agent shell, use `run-collector.sh --daemon` (not the bare foreground mode). `--daemon` uses `setsid` + `nohup` + a PID file so the process survives the shell's exit. The default foreground mode is only correct when something else is supervising it (systemd, docker, tmux). Foreground launched from a throwaway shell is how Collectors become `<defunct>` while `/healthz` keeps returning 200.

## Honesty Rule

Never report "observability is done" based on a single `/healthz` call. The bridge's `/healthz` comes up the moment the HTTP listener binds — it does not prove the Collector is alive, that OTLP ports are listening, or that real data is reaching Elasticsearch. The canonical failure mode in the wild: healthz=200, Collector `<defunct>`, port 4318 refused, ES empty, downstream tasks SIGTERM'd.

When asked "is the pipeline working?", run `doctor.py` and report its verdict. `healthy` is the only status that counts as done. Any other verdict must be reported verbatim, not softened:

- `degraded_collector_path` is the specific "bridge ok, Collector dead" state — agents are still shipping data via the fallback, but the standard OTLP receiver is broken. Do not round this up to "done" just because data is flowing.
- `degraded` / `broken` / `unreachable` mean the pipeline is not trustworthy end-to-end; partial success is worse than visible failure because it lies to every downstream check.

If the downstream consumer only understands `healthy/degraded/broken/unreachable`, treat `degraded_collector_path` as `degraded` — but never as `healthy`.

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
- `doctor.py` (honest five-check diagnostic; use this instead of `/healthz` when reporting pipeline status)
- `status.py` (reports which assets are currently deployed on the target cluster)
- `uninstall.py` (dry-run by default; removes only assets that match the prefix)

## Self-Extension Rule

Bootstrap is the first step, not the finish line. After assets are applied:

1. Trust `doctor.py` (and `verify_pipeline.py` under the hood) as the source of truth for "the pipeline works". `doctor.py` checks healthz, process/port state, recent real data in ES, and a live canary — any single one failing downgrades the verdict below `healthy`. `verify_pipeline.py` still auto-runs after `--apply-es-assets`; read its `verdict` in `verify.json`. If `doctor.py` returns anything other than `healthy`, follow the per-check `fix` lines before declaring the setup done.
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
