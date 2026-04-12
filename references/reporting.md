# Reporting

## First reports

The default report config focuses on:

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

## Output formats

The repo supports:

- Markdown reports for humans
- JSON reports for machines or follow-up automation
