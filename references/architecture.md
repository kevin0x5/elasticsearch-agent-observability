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

## Pipeline

1. discover agent architecture
2. map monitorable modules and signals
3. render Collector config
4. render Elasticsearch assets
5. generate reports

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
