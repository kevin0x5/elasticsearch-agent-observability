# elasticsearch-agent-observability

Bootstrap observability for AI agents on Elasticsearch + OpenTelemetry + Kibana.
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

| Layer | Asset | Notes |
|---|---|---|
| Collection | `otel-collector.generated.yaml` + `run-collector.sh` + `agent-otel.env` | Requires `otelcol-contrib` (spanmetrics + ES exporter) |
| Bridge fallback | `otlphttpbridge.py` + launcher | Recommended path for first install; logs/traces only |
| Storage | data stream + component templates + index template + ingest pipeline + ILM | ECS / GenAI native, ES 9.x |
| Kibana | data view + saved searches + Lens visualizations + dashboard | Imported via saved-objects API |
| Instrumentation starter | Python (auto-patches OpenAI / Anthropic) **or** Node/TS (preloadable `@opentelemetry/sdk-node`) | Choose with `--instrument-runtime python\|node\|auto` |
| LLM proxy starter | LiteLLM `docker-compose.yaml` + config + README | Zero-code path for upstream OSS agents (e.g. `openclaw/openclaw`) |
| Diagnose | `alert_and_diagnose.py` — 6 anomaly rules + RCA | Standalone, no Kibana Alerting license needed |
| Drift check | `validate_state.py` — local assets vs live cluster | |
| Verify | `verify_pipeline.py` — canary OTLP + ES poll | Auto-runs after `--apply-es-assets` |
| Knowledge archival | RCA → `elasticsearch-insight-store` | Optional bridge |

What it **does not** do: rewire your agent SDK automatically, ship a complete Kibana suite, auto-enroll Fleet, or instrument arbitrary runtimes for you.

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

### 2. Re-verify on demand

```bash
python scripts/verify_pipeline.py \
  --es-url <url> --es-user <user> --es-password '<pwd>' \
  --otlp-http-endpoint http://127.0.0.1:14319
```

Exit `0` = live, `2` = sent but lost / shape wrong (read `next_step`), `1` = transport unreachable.

### 3. Diagnose recent traffic

```bash
python scripts/alert_and_diagnose.py \
  --es-url <url> --index-prefix <prefix> --time-range now-15m
```

Add `--store-to-insight <path-to-store.py>` to archive RCA conclusions to [`elasticsearch-insight-store`](https://github.com/kevin0x5/elasticsearch-insight-store).

### 4. Detect cluster drift

```bash
python scripts/validate_state.py \
  --es-url <url> --assets-dir generated/bootstrap/elasticsearch
```

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
