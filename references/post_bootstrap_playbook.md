# Post-Bootstrap Playbook

Bootstrap is over; something (you, an AI agent, or CI) just ran `bootstrap_observability.py` and got a `generated/` directory plus working ES/Kibana assets.

**Tier 1** is live. The dashboard shows latency, error rate, token totals. Half the panels are empty because Tier 2 fields haven't been emitted yet. That's correct — each empty panel is a TODO.

This playbook tells an agent (human or AI) which TODO to do next, in what order.

## Level 0 — Confirm the pipeline actually ingests (do this first)

Before filling any panel, confirm data really reaches Elasticsearch. This is where most "Collector is up but ES is empty" failures hide.

Bootstrap now runs this automatically when `--apply-es-assets` is on; the result is written to `verify.json` next to the other artifacts. If you skipped it or need to re-run:

```bash
python scripts/verify_pipeline.py \
  --es-url <url> --es-user <user> --es-password <pass> \
  --index-prefix <prefix> \
  --otlp-http-endpoint http://127.0.0.1:14319   # bridge by default, or 4318 for Collector OTLP HTTP
```

Exit code contract:

- `0` canary was sent and indexed — you can move on to Level 1.
- `2` sent but lost, or indexed with the wrong shape — read the `next_step` field in the JSON output and apply it. Most common resolution: switch `--otlp-http-endpoint` from the Collector to the bridge (`:14319`) to unblock ingestion, then fix the Collector ES exporter separately.
- `1` could not send or could not reach ES at all — nothing downstream will work until the transport or credentials are fixed; do not continue.

Recommended first-install posture: **point the agent at the OTLP HTTP bridge first** (`http://127.0.0.1:14319`). It's a narrower, more reliable path and gets real traffic flowing through the same data stream / dashboards. Graduate to the native Collector ES exporter once the bridge path is stable.

## Level 1 — Tier 2 business fields (biggest ROI)

Goal: fill the empty tool/model/session/turn panels.

- [ ] Wrap every **tool call** site with `tracedToolCall("<tool_name>", ...)` or manual span + `gen_ai.tool.name`.
- [ ] Wrap every **model call** site with `tracedModelCall("<model_name>", ...)` or set `gen_ai.request.model` + token fields.
- [ ] At the **session boundary** (inbound request / conversation starter), open a span with `gen_ai.conversation.id`. All child spans inherit it via OTel context.
- [ ] At each **conversation turn** boundary, open a child span with `gen_ai.agent_ext.turn_id`.
- [ ] Tag every span with `gen_ai.agent_ext.component_type` (`tool` / `llm` / `mcp` / `memory` / `knowledge` / `guardrail` / `runtime`).

Verify: after traffic, the dashboard's "tool mix", "model mix", "sessions", and "slow turns" panels should have data.

## Level 2 — Sharper error/retry signals

Goal: stop getting generic "HTTP 500" alerts; start seeing "timeout concentrated in tool X".

- [ ] Classify exceptions into `error.type`. Suggested values: `timeout` / `rate_limit` / `api_error` / `auth_error` / `tool_error` / `validation_error` / `unknown`.
- [ ] At the retry point, set `gen_ai.agent_ext.retry_count` to the running count (not a boolean).
- [ ] If the agent has a native latency measurement already, also set `gen_ai.agent_ext.latency_ms` (the alert uses it for long-turn detection independent of span duration).

Verify: `alert_and_diagnose.py --time-range now-15m` starts citing specific `error_type` / tool / retry counts in the RCA section.

## Level 3 — Optional enrichment

Goal: cost panels, safety panels, regression tracking. Only do these when Levels 1 and 2 are solid.

- [ ] `gen_ai.agent_ext.cost` — compute per-call USD cost (needs a model-price table; keep it out of the agent's hot path).
- [ ] `gen_ai.guardrail.*` — if the agent has safety filters.
- [ ] `gen_ai.evaluation.*` — if the agent has an eval harness.
- [ ] Custom Kibana panels — add them via `--dashboard-extensions` on a follow-up bootstrap; don't hand-edit the generated saved objects.

## What to do with the `generated/` directory

`generated/` is output, not source. Treat it like a build artifact:

- **Do** review it before applying to production.
- **Do** add it to `.gitignore` in your own workspace.
- **Don't** hand-edit the files and expect the changes to survive the next bootstrap — re-render instead with the right flags.
- **Don't** check ES credentials in (see [`credentials_playbook.md`](credentials_playbook.md)).

## When NOT to keep extending

Stop when:

- The dashboard tells you something new each week.
- Alerts fire less often _and_ more accurately.
- You can answer "why was last Tuesday slow?" in under a minute.

More fields do not automatically mean more value. The dashboard is a fixed surface; fields that no panel consumes are just bytes in ES.

## Propagating changes back upstream

If you added a new span type that deserves its own dashboard panel, update in one PR:

1. `references/instrumentation_contract.md` — list the new field and what it powers.
2. `references/telemetry_schema.md` — add to the field dictionary.
3. `scripts/render_es_assets.py` — add the Kibana saved object.
4. `scripts/alert_and_diagnose.py` — if the field should drive a rule.

Skipping step 1 is the single most common way to re-introduce "ghost fields" (fields that exist in data but no panel knows about).
