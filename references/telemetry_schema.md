# Telemetry Schema

## Primary contract

This repo targets a **9.x ECS + OTel GenAI Semantic Conventions** ingest contract.

Fields are split into three namespaces:

- **OTel GenAI standard** — `gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.conversation.id`, `gen_ai.tool.name`, `gen_ai.operation.name`, `gen_ai.agent.id/name/version`, `error.type`
- **ECS standard** — `@timestamp`, `event.*`, `service.*`, `agent.*`, `trace.id`, `span.id`, `parent.id`, `transaction.id`
- **Project extensions** — `gen_ai.agent_ext.*` (fields awaiting OTel SemConv proposal)

Send canonical fields directly:

- `@timestamp`
- `event.*`
- `service.*`
- `agent.*`
- `trace.id`, `span.id`, `parent.id`, `transaction.id`
- `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.system`
- `gen_ai.operation.name`
- `gen_ai.usage.*`
- `gen_ai.agent.id`, `gen_ai.agent.name`, `gen_ai.agent.version`
- `gen_ai.conversation.id`
- `gen_ai.tool.name`, `gen_ai.tool.call.id`
- `error.type`
- `gen_ai.agent_ext.*`
- `gen_ai.guardrail.*`
- `gen_ai.evaluation.*`

## Component type tagging

Use `gen_ai.agent_ext.component_type` to tag spans with their component category:

- `runtime` — agent runtime entrypoint
- `llm` — model inference call
- `tool` — tool execution
- `mcp` — MCP protocol call
- `memory` — memory store read/write
- `knowledge` — knowledge base / RAG retrieval
- `guardrail` — safety check / content filter

## Extension fields (`gen_ai.agent_ext.*`)

These fields extend OTel GenAI Semantic Conventions for agent runtime observability. They are candidates for upstream OTel SemConv proposals.

- `gen_ai.agent_ext.turn_id` — conversation turn identifier
- `gen_ai.agent_ext.component_type` — see above
- `gen_ai.agent_ext.retry_count` — retry attempts
- `gen_ai.agent_ext.latency_ms` — explicit latency in milliseconds
- `gen_ai.agent_ext.cost` — monetary cost of the operation
- `gen_ai.agent_ext.module` — source module name
- `gen_ai.agent_ext.module_kind` — module category
- `gen_ai.agent_ext.semantic_kind` — semantic operation kind
- `gen_ai.agent_ext.verify_id` — pipeline verification canary id

## Memory / knowledge monitoring fields

- `gen_ai.agent_ext.retrieval_latency_ms` — retrieval round-trip time
- `gen_ai.agent_ext.cache_hit` — whether the retrieval hit a cache
- `gen_ai.agent_ext.retrieval_score` — similarity / relevance score
- `gen_ai.agent_ext.knowledge_source` — knowledge base identifier

## Guardrail / safety fields

- `gen_ai.guardrail.action` — `pass` / `block` / `redact`
- `gen_ai.guardrail.rule_id` — which guardrail rule fired
- `gen_ai.guardrail.category` — `content_safety` / `prompt_injection` / `pii` / `custom`
- `gen_ai.guardrail.latency_ms` — guardrail check latency

## Evaluation observability fields

- `gen_ai.evaluation.run_id` — evaluation run identifier
- `gen_ai.evaluation.evaluator` — evaluator name
- `gen_ai.evaluation.score` — numeric score
- `gen_ai.evaluation.outcome` — `pass` / `fail` / `degraded`
- `gen_ai.evaluation.dimension` — `quality` / `safety` / `latency` / `cost`

## Important rule

Do **not** rely on flat legacy fields such as `agent_id`, `tool_name`, `token_input`, or `captured_at`.
Do **not** use pre-v2 field names like `gen_ai.agent.tool_name` or `gen_ai.agent.session_id` — they have been replaced by OTel standard equivalents.

## Time field

The canonical and default reporting time field is `@timestamp`.
