# Reporting

## Primary Human Surface

The primary human-facing surface is **Kibana**.

In the current version, that specifically means:

- a data view
- saved searches for the event stream, failures, and session drilldown
- a session-first dashboard bundle that can be applied through the API or imported via `kibana-saved-objects.ndjson`
- Lens visualizations for event rate, latency, session hotspots, component hotspots, tool distribution, and token usage

That is now a stronger Kibana starting point.
It is still not a full platform-grade dashboard suite.

## Smoke And Machine Outputs

The repo also supports:

- Markdown reports for quick smoke validation
- JSON reports for automation

These outputs are supporting surfaces.
They are not the long-term UI story.

## Current Metric Set

Keep the reporting language aligned with what the repo really emits today:

- success rate
- p50 latency
- p95 latency
- tool error rate
- retry total
- token input total
- token output total
- cost total
- top sessions
- failed sessions
- slow turns
- top components
- failed components
- top tools
- top models
- MCP methods
- error types

Do not claim model-fallback analysis, guardrail drilldown, or evaluation regression unless the implementation actually emits them.

## Time Field Contract

`report-config.json` defines the reporting time field.
Current default:

- `@timestamp`

Both the smoke report query and the Kibana entry surface should follow the same time-field contract.

## Practical Rule

If Markdown and Kibana disagree, fix the shared config and ingest contract first.
Do not let the repo drift into one time field for smoke output and another for Kibana.
