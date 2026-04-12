---
name: elasticsearch-agent-observability
description: "Bootstrap observability for an agent on self-hosted Elasticsearch 9.x or Tencent Cloud Elasticsearch Service 9.x. Trigger when the user wants to monitor an agent, trace tool/model calls, generate observability configs, or auto-discover what parts of the agent architecture should be monitored."
---

# Elasticsearch Agent Observability

## What This Skill Is

`Elasticsearch Agent Observability` bootstraps observability for an agent runtime.

This skill is for requests like:
- “给这个 agent 建可观测能力”
- “用 Elasticsearch 监控当前 agent”
- “自动发现这个 agent 里该监测哪些模块”
- “给某个 agent 生成 OTel + ES 9.x 配置和报表”

It is built for:
- self-hosted Elasticsearch 9.x
- Tencent Cloud Elasticsearch Service 9.x

## Core Behavior

The skill should:
1. inspect the target workspace or agent layout
2. auto-discover monitorable modules
3. render Collector config and Elasticsearch assets
4. generate a report definition and runtime outputs

## Resolve the Script Path

Scripts live under `scripts/` relative to this `SKILL.md` file.
Resolve absolute paths from the directory that contains this file.

## Output Surface

The main outputs are:
- architecture discovery result
- rendered OTel Collector config
- Elasticsearch assets
- Markdown / JSON report

## Important Notes

- Use Elasticsearch 9.x compatible assets
- Prefer direct, practical outputs over abstract observability theory
- Keep sensitive prompts, args, and results in redacted or summarized form by default
- For deeper rules, read `references/architecture.md`, `references/config_guide.md`, `references/telemetry_schema.md`, and `references/reporting.md`
