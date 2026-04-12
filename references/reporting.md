# Reporting

## Human-facing reporting surface

The primary human-facing reporting surface for this repo should be **Kibana**.

That means the repo should prepare:

- a data view / index pattern for agent observability events
- a saved search or other directly usable Kibana objects
- an asset bundle that can be imported or applied automatically

## Smoke and machine outputs

The repo still supports:

- Markdown reports for quick smoke validation
- JSON reports for automation or follow-up processing

But these are supporting outputs.
They are not the main long-term UI story.

## Default metric focus

The initial report surface focuses on:

- success rate
- p50 / p95 latency
- tool error rate
- retry / timeout breakdown
- token / cost totals
- top tools
- top models
- MCP method breakdown

## Why these first

These metrics answer the first operational questions quickly:

- is the agent healthy
- where is it slow
- which tools fail most often
- is retry pressure rising
- which models cost the most
