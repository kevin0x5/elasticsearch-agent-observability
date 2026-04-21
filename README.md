# elasticsearch-agent-observability

Bootstrap observability for AI agents on Elasticsearch + OpenTelemetry + Kibana.

**Three signals, one data stream.** Traces, logs, and metrics all land in the same `<prefix>-events` data stream with a shared ECS schema, so correlation across signals is a KQL filter, not a join. The rendered Collector config wires OTLP → spanmetrics → Elasticsearch exporters for all three; the Kibana dashboard ships with latency P50/P95 lines, token-usage area chart, and event-rate breakdown side by side with Discover drilldown.

One command renders the Collector config, Elasticsearch index/pipeline/ILM assets, Kibana dashboards, an end-to-end verification canary, and (optionally) a Node/TS instrumentation starter or a zero-code LLM proxy bundle.

## Quick start

```bash
git clone https://github.com/kevin0x5/elasticsearch-agent-observability.git
cd elasticsearch-agent-observability

# Apply ES + Kibana assets, run the canary, leave artifacts in generated/bootstrap/
python scripts/bootstrap_observability.py \
  --workspace /path/to/your/agent \
  --output-dir generated/bootstrap \
  --es-url http://localhost:9200 \
  --es-user elastic --es-password '<pwd>' \
  --apply-es-assets \
  --kibana-url http://localhost:5601 \
  --apply-kibana-assets
```

When the run finishes:

- `generated/bootstrap/verify.json` says whether the OTLP → ES path is actually live. **If `verdict != "ok"`, follow the `next_step` field — usually "switch the agent to the OTLP HTTP bridge at `http://127.0.0.1:14319`".**
- `generated/bootstrap/bootstrap-summary.md` is the human readable index.
- Kibana already has the data view and the starter dashboard.

To go further, read [`references/post_bootstrap_playbook.md`](references/post_bootstrap_playbook.md).

## What you get

After one bootstrap you have a working observability backbone for your agent: a Kibana surface that shows real cost, latency, and failure patterns; an alert flow that doesn't just chart anomalies but also explains them; and a lightweight feedback loop that catches config drift and pipeline breakage before they become outages. The skill keeps you honest — every dashboard panel and every alert rule is wired to a specific data field, so what you see is what your agent actually emitted, not a smoothed-over template.

Concretely, the questions it lets you answer:

- Where is my token budget going, and when did it spike?
- Which tools and models are slow, error-prone, or both?
- Why was last Tuesday slow — down to the session, the turn, and a generated root cause?
- When multiple alerts fire in the same window, are they the same incident? (`alert_and_diagnose` now emits `correlation.chains` — alerts sharing a session/tool/model/component are merged into a single chain with a confidence score.)
- Did the agent actually run the diagnostic, or claim it did? (every `doctor` / `alert_and_diagnose` run writes a `internal.skill_audit` record with verdict + evidence keys, so the skill is observable about itself.)
- Did anyone change my cluster config since the last deploy?
- Is the pipeline live right now, or am I looking at stale data?
- How do I keep an incident's findings around after the fix ships?

Two ways to feed it data, picked by whether you own the agent code:

- **You own the agent (Python or Node/TS)** — install the generated instrumentation starter and OpenAI/Anthropic calls become traced spans automatically.
- **You don't own the agent** — run the generated LLM proxy bundle (`docker compose up -d`) and point the agent's `OPENAI_API_BASE` at it. No source changes.

## Common commands

Each step builds on the previous one.

### 1. Bootstrap

```bash
python scripts/bootstrap_observability.py \
  --workspace <workspace> --output-dir generated/bootstrap \
  --es-url <url> --es-user <user> --es-password '<pwd>' \
  --apply-es-assets --apply-kibana-assets
```

The run ends with an automatic verify; skip it with `--no-verify` if needed.

### 2. Is the pipeline actually live? (use this before trusting anything)

```bash
python scripts/doctor.py \
  --es-url <url> --es-user <user> --es-password '<pwd>' \
  --index-prefix <prefix>
```

**Why this and not `/healthz`:** the bridge's `/healthz` returns 200 as soon as its HTTP listener is up. It does **not** prove the Collector is alive, that port 4318 is listening, or that real data is reaching ES. We have seen setups where healthz is green, the Collector is `<defunct>`, and agents silently lose telemetry. `doctor.py` runs five independent checks — healthz, process/port state (including zombie detection), real agent data in the last N minutes, and a live OTLP canary — and collapses them into a single honest verdict: `healthy` / `degraded` / `broken` / `unreachable`.

Exit `0` = healthy, `2` = degraded or broken (read per-check `fix` lines), `1` = ES unreachable.

### 3. Re-verify OTLP → ES end to end

```bash
python scripts/verify_pipeline.py \
  --es-url <url> --es-user <user> --es-password '<pwd>' \
  --otlp-http-endpoint http://127.0.0.1:14319
```

Exit `0` = live, `2` = sent but lost / shape wrong (read `next_step`), `1` = transport unreachable.

### 4. Diagnose recent traffic

```bash
python scripts/alert_and_diagnose.py \
  --es-url <url> --index-prefix <prefix> --time-range now-15m
```

Add `--store-to-insight <path-to-store.py>` to archive RCA conclusions to [`elasticsearch-insight-store`](https://github.com/kevin0x5/elasticsearch-insight-store).

### 5. Detect cluster drift

```bash
python scripts/validate_state.py \
  --es-url <url> --assets-dir generated/bootstrap/elasticsearch
```

### 6. Inspect what is actually deployed

```bash
python scripts/status.py \
  --es-url <url> --index-prefix <prefix>
```

Exit `0` = all assets present, `2` = some missing (names are listed), `1` = ES unreachable.

### 7. Tear it all down

```bash
# Dry-run first (default): prints the delete plan, touches nothing.
python scripts/uninstall.py --es-url <url> --index-prefix <prefix>

# Actually delete:
python scripts/uninstall.py --es-url <url> --index-prefix <prefix> --confirm
```

Only assets matching the prefix are removed; 404s are treated as "already gone". Add `--keep-data-stream` when you only want to rerender templates. Pass `--kibana-url` + `--kibana-assets-file generated/bootstrap/elasticsearch/kibana-saved-objects.json` to also remove the dashboards.

### Running the Collector without orphaning it

```bash
./generated/bootstrap/run-collector.sh --daemon   # survives shell exit
./generated/bootstrap/run-collector.sh --status   # alive?
./generated/bootstrap/run-collector.sh --stop     # stop daemon
```

The default mode (no args) is foreground, which is what `systemd` / `docker` / `tmux` expect. `--daemon` uses `setsid` + `nohup` + a PID file so the Collector does not become `<defunct>` when the shell that launched it exits — the exact failure mode that keeps `/healthz` returning 200 while the data plane is dead. `run-otlphttpbridge.sh` supports the same contract.

### Optional: zero-code path for an upstream OSS agent

```bash
python scripts/bootstrap_observability.py ... --generate-llm-proxy
cd generated/bootstrap/llm-proxy
cp .env.example .env   # paste OPENAI_API_KEY
docker compose up -d
# then point the agent at http://localhost:4000/v1
```

### Optional: Node.js / TypeScript instrumentation starter

```bash
python scripts/bootstrap_observability.py ... \
  --generate-instrument-snippet --instrument-runtime node
# then in the TS agent project:
node --import ./generated/bootstrap/node-instrumentation/agent-otel-bootstrap.mjs dist/index.js
```

## Generated output

Default `--output-dir generated/bootstrap/`:

```text
generated/bootstrap/
├── discovery.json
├── bootstrap-summary.md
├── verify.json                      # canary verdict (auto-runs after apply)
├── otel-collector.generated.yaml
├── run-collector.sh + agent-otel.env
├── otlphttpbridge.py
├── run-otlphttpbridge.sh + agent-otel-bridge.env
├── elasticsearch/
│   ├── component-template-*.json
│   ├── index-template.json
│   ├── ingest-pipeline.json
│   ├── ilm-policy.json
│   ├── kibana-saved-objects.{json,ndjson}
│   ├── apply-summary.json
│   └── sanity-check.json
├── node-instrumentation/            # only with --instrument-runtime node|auto on a TS workspace
│   ├── agent-otel-bootstrap.mjs
│   └── README.md
├── llm-proxy/                       # only with --generate-llm-proxy
│   ├── docker-compose.yaml
│   ├── config.yaml
│   ├── .env.example
│   └── README.md
├── elastic-native/                  # only with --ingest-mode elastic-agent-fleet | apm-otlp-hybrid
│   └── ... (APM env, surface manifest, RUM, profiling, ...)
└── agent_otel_bootstrap.py          # only with --generate-instrument-snippet --instrument-runtime python
```

## Extending after bootstrap

Bootstrap delivers Tier 1 observability (latency, error rate, token totals) for free. Tool / model / session / turn panels stay empty until the agent emits the matching `gen_ai.*` fields — empty panels are intentional TODO markers.

Read in this order:

1. [`references/instrumentation_contract.md`](references/instrumentation_contract.md) — three field tiers, what each one unlocks
2. [`references/post_bootstrap_playbook.md`](references/post_bootstrap_playbook.md) — Level 0/1/2/3 self-extension checklist (Level 0 = run verify)
3. [`references/credentials_playbook.md`](references/credentials_playbook.md) — what to do when bootstrap left credentials on disk

## Requirements

- Python 3.10+ (skill itself uses stdlib only)
- Elasticsearch 9.x + Kibana 9.x (Basic license is enough)
- `otelcol-contrib` 0.87.0+ if running the Collector path
- For the generated Python instrument snippet: install `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-grpc` in the target agent project. Node/TS, Go, Java agents wire their own OTel SDK; the rest of the pipeline stays the same.

## Installation as a skill

For agent runtimes that load skills from a directory:

```bash
git clone https://github.com/kevin0x5/elasticsearch-agent-observability.git <skill-dir>/elasticsearch-agent-observability
```

If `<skill-dir>` is itself inside a git-tracked workspace, the nested `.git/` triggers an "embedded git repo" warning. Three escapes: clone with `--depth 1` and add the path to your outer `.gitignore`; install **outside** your workspace and reference the absolute path in your skill config; or add it as a `git submodule`.

## References

- [`references/config_guide.md`](references/config_guide.md) — operational rules: Collector binary policy, env vs inline credentials, dry-run, exporter triage, verify rule, startup/stop/rollback shape
- [`references/instrumentation_contract.md`](references/instrumentation_contract.md) — field tiers
- [`references/post_bootstrap_playbook.md`](references/post_bootstrap_playbook.md) — self-extension checklist
- [`references/credentials_playbook.md`](references/credentials_playbook.md) — production credential discipline
- [`references/telemetry_schema.md`](references/telemetry_schema.md) — full field dictionary
- [`references/architecture.md`](references/architecture.md), [`references/reporting.md`](references/reporting.md), [`references/runtime_compat.md`](references/runtime_compat.md)

## Security

- Credentials default to env placeholders; `--embed-es-credentials` puts them on disk and the file should be treated as secret material.
- The ingest pipeline redacts sensitive generative AI fields.
- Rotation, least-privilege API keys, and post-bootstrap cleanup live in [`references/credentials_playbook.md`](references/credentials_playbook.md).
- See `SECURITY.md` for vulnerability reporting.

## Development

```bash
python3 -m unittest discover -s tests
```

## Contributing

See `CONTRIBUTING.md`.

## License

Apache-2.0. See `LICENSE`.
