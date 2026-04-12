# Config Guide

## Target environments

This repo is designed for:

- self-hosted Elasticsearch 9.x
- Tencent Cloud Elasticsearch Service 9.x
- Kibana instances that can import or create saved objects through the standard API

## Main outputs

The bootstrap flow should leave you with a working observability starter surface across three layers:

- OTel Collector config
- Collector launcher script
- agent OTLP env template
- Elasticsearch index template
- ingest pipeline
- ILM policy
- Kibana saved objects bundle
- apply summary
- optional smoke report output

## Default assumptions

- OpenTelemetry is the main ingestion path
- Elasticsearch is the storage and analytics backend
- Kibana is the main human-facing report surface
- prompts and tool payloads should be redacted or summarized by default
- generated assets should stay reviewable JSON / YAML / shell files, not hidden runtime state

## Minimal bootstrap

```bash
python scripts/bootstrap_observability.py \
  --workspace /path/to/workspace \
  --output-dir generated/bootstrap \
  --es-url http://localhost:9200 \
  --apply-es-assets \
  --kibana-url http://localhost:5601 \
  --apply-kibana-assets
```

## What this gives you

At minimum, the command above should leave you with:

- generated Collector config
- generated Elasticsearch assets
- applied template / pipeline / ILM policy
- bootstrapped first write index alias
- generated Kibana saved objects bundle
- optionally applied Kibana saved objects
- generated smoke report output for quick validation

## Rule

Prefer generated config and assets over hand-written setup notes.
That keeps the repo deterministic, publishable, and easier to automate.
