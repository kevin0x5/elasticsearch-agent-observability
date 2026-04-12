# Config Guide

## Target environments

This repo is designed for:

- self-hosted Elasticsearch 9.x
- Tencent Cloud Elasticsearch Service 9.x

## Main outputs

The bootstrap flow generates:

- Collector config
- Elasticsearch index template
- ingest pipeline
- ILM policy
- report config

## Default assumptions

- OTLP is the main ingestion path
- Elasticsearch is the main storage and query backend
- prompts and tool payloads should be redacted or summarized by default
- generated assets should be reviewable JSON / YAML files, not hidden runtime state

## Minimal bootstrap

```bash
python scripts/bootstrap_observability.py \
  --workspace /path/to/workspace \
  --output-dir generated/bootstrap \
  --es-url http://localhost:9200
```

## Rule

Prefer generated config and assets over hand-written setup notes.
That keeps the repo deterministic and easier to publish.
