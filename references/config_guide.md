# Config Guide

## Target Shape

This repo is designed for:

- self-hosted Elasticsearch 9.x
- Tencent Cloud Elasticsearch Service 9.x
- Kibana instances that accept standard saved-object APIs

## What The Bootstrap Leaves Behind

The normal bootstrap path should leave:

- Collector config (`otel-collector.generated.yaml`)
- Collector launcher script (`run-collector.sh`)
- agent OTLP env template (`agent-otel.env`)
- OTLP HTTP bridge script (`otlphttpbridge.py`)
- bridge launcher script (`run-otlphttpbridge.sh`)
- bridge env template (`agent-otel-bridge.env`)
- Elasticsearch component templates (`component-template-ecs-base.json`, `component-template-settings.json`)
- Elasticsearch index template (`index-template.json`)
- ingest pipeline (`ingest-pipeline.json`)
- ILM policy (`ilm-policy.json`)
- report config (`report-config.json`)
- Kibana saved objects bundle (`kibana-saved-objects.json`, `kibana-saved-objects.ndjson`)
- Elastic-native APM / RUM / profiling starter bundle when using `elastic-agent-fleet` or `apm-otlp-hybrid`
- Elastic-native preflight checklist (`preflight-checklist.json`) for Kibana / Fleet / APM rollout readiness
- optional Python instrumentation starter file (`agent_otel_bootstrap.py`)
- optional apply summary (`apply-summary.json`)
- optional sanity-check result
- optional smoke report
- bootstrap summary (`bootstrap-summary.md`)

## Workspace Rule

Point `--workspace` at the real agent code root.

Do not rely on generated sample folders, test-only directories, or doc-heavy folders to represent the runtime.
The current discovery pass already ignores common noise directories such as:

- `generated/`
- `references/`
- `tests/`
- `assets/`

## Credential Rule

Default path:

- pass `--es-user` and `--es-password`
- keep them out of YAML
- let the generated Collector config reference env placeholders

Only use `--embed-es-credentials` when the file can be treated as secret material.

## Collector Distribution Rule

The generated Collector config depends on components such as:

- `spanmetrics`
- Elasticsearch exporter
- optional `filelog`

Assume **`otelcol-contrib`** by default, or a custom Collector build with equivalent components.
Do not assume a minimal core `otelcol` binary will run the generated config.

## Launcher Rule

`run-collector.sh` uses sibling-relative paths.
That means the launcher, env file, and Collector YAML can move together as one bundle without rewriting absolute paths.

The launcher also respects `OTELCOL_BIN`, so operators can override the binary path without editing the script.

## Workspace-Local Binary Rule

If the host does not already provide `otelcol-contrib`, a workspace-local binary is acceptable.
Treat it as an explicit software rollout, not as a hidden implementation detail.

Minimum rules:

- pin an exact Collector version
- download only from the official release source
- record the release URL and checksum in rollout notes
- keep the binary outside `generated/`, for example under `tools/otelcol/<version>/`
- keep runtime logs and PID files under `generated/bootstrap/runtime/`
- do not modify global shell profiles or system paths just to satisfy this skill
- if you run a second Collector for diagnosis, rerender with a different `--telemetry-metrics-port`; the default self-telemetry listener is `127.0.0.1:8888`

## Exporter Triage Rule

If a debug Collector confirms `/v1/logs` and `/v1/traces` are received and the debug exporter prints the payload, treat the remaining issue as **Collector → Elasticsearch exporter** until proven otherwise.

For the generated bundle in this repo:

- `logs_index` and `traces_index` statically target `<index-prefix>-events`
- `metrics_index` statically targets `<index-prefix>-metrics`
- scope attributes force `elastic.mapping.mode=ecs`
- exporter config restricts `mapping.allowed_modes` to `ecs`

That means the first file to inspect is `otel-collector.generated.yaml`, specifically the `exporters.elasticsearch/*` blocks.

## Verify Rule

Every apply run should be followed by `verify_pipeline.py`. `bootstrap_observability.py` does this automatically when `--apply-es-assets` is on unless `--no-verify` is passed.

What it does:

1. Sends one OTLP/HTTP JSON log carrying a unique `gen_ai.agent.verify_id` to the configured endpoint (bridge by default, Collector HTTP receiver if overridden).
2. Polls `<prefix>-events*` for that id with a short exponential backoff.
3. Emits a verdict: `ok` / `contract_broken` / `sent_but_lost` / `transport_unreachable` / `transport_rejected`, each with a concrete `next_step`.

Exit codes:

- `0` `ok`
- `2` pipeline partially alive (`contract_broken` or `sent_but_lost`)
- `1` transport never completed

Recommended default target is the OTLP HTTP bridge at `http://127.0.0.1:14319`. It's a narrower, more reliable path for the first install. Move to the native Collector ES exporter once the bridge path is stable; verify again when you do.

Do not declare a pipeline "production-ready" until verify returns `ok`. `verify.json` is the durable record of that verdict.

## Elastic-native Surface Rule

When bootstrap renders the `elastic-native/` bundle, treat it as the operator-facing starter for Kibana APM / Traces / User Experience / profiling surfaces:

- `apm-agent.env` is the runtime env template for Elastic APM / trace analysis
- `surface-manifest.json` is the machine-readable map of Kibana native apps and correlation contract
- `preflight-checklist.json` is the machine-readable readiness summary for required Kibana / Fleet / APM inputs and still-missing operator actions
- `apm-entrypoints.md` and `trace-analysis-playbook.md` point operators at the right Kibana apps and trace workflow
- `rum-config.json`, `rum-agent-snippet.js`, and `ux-observability-playbook.md` cover browser-side UX monitoring and frontend/backend trace correlation
- `profiling-starter.md` is a rollout checklist for Elastic Universal Profiling, not an installer

These files extend the base dashboard surface; they do not magically make APM, RUM, or profiling live without runtime / host rollout work.
`apply-summary.json` now also carries the native preflight result when the elastic-native bundle is present, including contract checks, blocking checks, and ready counts derived from `surface-manifest.json`, `rum-config.json`, and optional Kibana/Fleet runtime reachability.

## Bridge Fallback Rule

The generated bootstrap bundle also includes a local OTLP HTTP bridge fallback for the narrowed failure mode above.

Current contract:

- `otlphttpbridge.py` listens on `127.0.0.1:14319` by default
- it accepts `POST /v1/logs` and `POST /v1/traces`
- it writes directly to `<index-prefix>-events`
- it is **not** the metrics path; keep metrics on the Collector route
- OTLP JSON works out of the box; OTLP protobuf requires runtime access to `protobuf` and `opentelemetry-proto`

Use this path when you need stable Elasticsearch ingest first and can finish Collector exporter compatibility later.

## Startup / Stop Rule

A background Collector launch is only acceptable when all of these are true:

- `OTELCOL_BIN` points at a verified binary
- stdout/stderr are redirected to a real log file
- the process ID is written to a PID file
- operators know the stop command and rollback path

Minimal shape:

```bash
OTELCOL_BIN="/abs/path/to/otelcol-contrib" \
  nohup generated/bootstrap/run-collector.sh \
  > generated/bootstrap/runtime/collector.log 2>&1 &
echo $! > generated/bootstrap/runtime/collector.pid
```

Stop shape:

```bash
kill "$(cat generated/bootstrap/runtime/collector.pid)"
```

Rollback means:

- stop the process
- remove the `OTELCOL_BIN` override
- keep the generated config bundle for inspection
- switch ingest mode if the environment should not carry a Collector binary

## Dry-Run Rule

`bootstrap_observability.py --dry-run` should be treated as a planning pass:

- assets are rendered
- apply plan is written
- no ES / Kibana requests are sent
- no sanity check runs
- no smoke report query runs

Use it before touching a real cluster, especially when reviewing generated Kibana objects or rollout impact.

## Time Field Contract

`report-config.json` declares the reporting time field.
Current default is:

- `time_field = @timestamp`

The ingest pipeline stamps `@timestamp` from ingest time when upstream telemetry does not provide it.
`@timestamp` is the only default reporting field in the 9.x contract.
That keeps Kibana and the smoke report on the same time-field contract.

## Report Contract Rule

`report-config.json` should only list metrics that the current implementation really emits.
Current latency keys are:

- `p50_latency_ms`
- `p95_latency_ms`

If the report payload changes, refresh the contract and example generated assets together.

## Minimal Bootstrap

```bash
python scripts/bootstrap_observability.py \
  --workspace /path/to/workspace \
  --output-dir generated/bootstrap \
  --es-url http://localhost:9200 \
  --apply-es-assets \
  --kibana-url http://localhost:5601 \
  --apply-kibana-assets
```

## What This Does Not Guarantee

This repo does not guarantee:

- auto-instrumentation of arbitrary agent code
- perfect schema extraction from every telemetry source
- a full dashboard pack in Kibana
- end-to-end runtime → Collector → ES → Kibana validation for every target app

Treat it as a strong Elastic-side starter, not as the whole runtime plane.
