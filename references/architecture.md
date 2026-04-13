# Elasticsearch Agent Observability — Architecture

## Core idea

Observe an agent like a structured runtime, not like a flat log emitter.

The repo therefore starts with one extra step that many logging repos do not have:
**architecture discovery**.

Before rendering config, it tries to infer what the agent actually contains:

- runtime entrypoints
- command surfaces
- workflows
- tool registries
- model adapters
- memory or cache layers
- MCP-related surfaces
- evaluation or regression hooks
- OTel SDK / Elastic APM / Elastic Agent presence
- web service entrypoints (FastAPI, Flask, Express, Gin, Spring, etc.)
- browser frontends (React, Vue, Next.js, etc.)
- guardrails / safety checks / content filters
- knowledge bases / RAG retrieval (Qdrant, Pinecone, Chroma, FAISS, etc.)

## Pipeline

1. discover agent architecture
2. map monitorable modules and signals
3. render Collector config
4. render Elasticsearch assets (templates, pipeline, ILM, Kibana objects)
5. render OTLP HTTP bridge fallback (for when Collector ES exporter is blocked)
6. render Elastic-native APM / RUM / profiling starter bundle
7. render instrumentation snippet (Python auto-setup + monkey-patch)
8. generate reports
9. optionally apply assets to live ES / Kibana

## Additional components

- **Alert & diagnosis**: standalone cron-style check (`alert_and_diagnose.py`) for error rate spikes, latency degradation, and token anomalies. Outputs structured RCA.
- **Knowledge archival**: diagnosis results can be piped into `elasticsearch-insight-store` for persistent RCA storage.
- **Drift validation**: `validate_state.py` compares local generated assets against the live ES cluster and reports structural drift.
- **Maturity scoring**: discovery computes a multi-dimensional maturity score (basic_logging, structured_telemetry, genai_instrumentation, operational_readiness) to guide upgrade paths.
- **Dashboard extensions**: operators can supply external JSON/YAML panel declarations to extend the generated Kibana dashboard.

## Why this matters

A generic config cannot tell whether a workspace is:

- a single-script skill
- a workflow-heavy multi-step agent
- a tool-call-heavy runtime
- a memory-centric retrieval agent

This repo uses discovery to choose better defaults.
That is its main product difference.

## First-class outputs

Each bootstrap run should preserve:

- `discovery.json`
- generated Collector config
- generated Elasticsearch asset files
- report config
- bootstrap summary
