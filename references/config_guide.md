# Config Guide

## Target Shape

This repo is designed for:

- self-hosted Elasticsearch 9.x
- Tencent Cloud Elasticsearch Service 9.x
- Kibana instances that accept standard saved-object APIs

## What The Bootstrap Leaves Behind

The normal bootstrap path should leave:

- Collector config
- Collector launcher script
- agent OTLP env template
- Elasticsearch index template
- ingest pipeline
- ILM policy
- report config
- Kibana saved objects bundle
- optional Python instrumentation starter file
- optional apply summary
- optional sanity-check result
- optional smoke report

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
`captured_at` is kept as an alias to `@timestamp` for backward compatibility.
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
