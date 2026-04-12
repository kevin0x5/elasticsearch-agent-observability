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
- optional apply summary
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

## Launcher Rule

`run-collector.sh` now uses sibling-relative paths.
That means the launcher, env file, and Collector YAML can move together as one bundle without rewriting absolute paths.

## Time Field Contract

`report-config.json` declares the reporting time field.
Current default is:

- `time_field = captured_at`

The ingest pipeline now stamps `captured_at` from ingest time when upstream telemetry does not provide it.
That keeps Kibana and the smoke report on the same time-field contract.

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

Treat it as a strong Elastic-side starter, not as the whole runtime plane.
