# Telemetry Schema

## Minimum shared fields

The repo normalizes around these fields first:

- `agent_id`
- `run_id`
- `turn_id`
- `span_id`
- `parent_span_id`
- `signal_type`
- `semantic_kind`
- `agent.module`
- `agent.module_kind`
- `tool_name`
- `model_name`
- `latency_ms`
- `token_input`
- `token_output`
- `cost`
- `error_type`
- `retry_count`
- `mcp_method_name`
- `session_id`
- `captured_at`

## Why this schema

These fields are enough to support:

- latency views
- tool and model breakdowns
- retry and error analysis
- MCP-aware breakdowns
- module-level aggregation after architecture discovery

The schema is intentionally small enough to stay usable, but rich enough to grow later.
