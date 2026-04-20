#!/usr/bin/env python3
"""Generate Markdown or JSON reports from Elasticsearch agent observability data."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from common import (
    ESConfig,
    SkillError,
    build_events_alias,
    es_request,
    print_error,
    read_json,
    validate_credential_pair,
    validate_index_prefix,
    write_json,
    write_text,
)


TERM_BUCKET_SIZE = 5
COMPONENT_BUCKET_SIZE = 8


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate observability report")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--es-user")
    parser.add_argument("--es-password")
    parser.add_argument("--time-range", default="now-24h")
    parser.add_argument("--format", choices=["markdown", "json"], help="Optional output format override")
    return parser.parse_args()


def search_payload(time_range: str, time_field: str = "@timestamp") -> dict[str, Any]:
    return {
        "size": 0,
        "query": {
            "bool": {
                "filter": [{"range": {time_field: {"gte": time_range}}}],
                "must_not": [{"term": {"event.dataset": "internal.sanity_check"}}],
            }
        },
        "aggs": {
            "with_errors": {"filter": {"term": {"event.outcome": "failure"}}},
            "tool_calls": {"filter": {"exists": {"field": "gen_ai.agent.tool_name"}}},
            "tool_errors": {
                "filter": {
                    "bool": {
                        "must": [{"exists": {"field": "gen_ai.agent.tool_name"}}, {"term": {"event.outcome": "failure"}}]
                    }
                }
            },
            "latency_percentiles": {"percentiles": {"field": "event.duration", "percents": [50, 95]}},
            "retry_sum": {"sum": {"field": "gen_ai.agent.retry_count"}},
            "token_input_sum": {"sum": {"field": "gen_ai.usage.input_tokens"}},
            "token_output_sum": {"sum": {"field": "gen_ai.usage.output_tokens"}},
            "cost_sum": {"sum": {"field": "gen_ai.agent.cost"}},
            "top_sessions": {"terms": {"field": "gen_ai.agent.session_id", "size": TERM_BUCKET_SIZE}},
            "failed_sessions": {
                "filter": {"term": {"event.outcome": "failure"}},
                "aggs": {
                    "sessions": {"terms": {"field": "gen_ai.agent.session_id", "size": TERM_BUCKET_SIZE}},
                },
            },
            "slow_turns": {
                "terms": {
                    "field": "gen_ai.agent.turn_id",
                    "size": TERM_BUCKET_SIZE,
                    "order": {"avg_latency": "desc"},
                },
                "aggs": {
                    "avg_latency": {"avg": {"field": "gen_ai.agent.latency_ms"}},
                    "sessions": {"terms": {"field": "gen_ai.agent.session_id", "size": 1}},
                    "failure_count": {"filter": {"term": {"event.outcome": "failure"}}},
                },
            },
            "top_components": {"terms": {"field": "gen_ai.agent.component_type", "size": COMPONENT_BUCKET_SIZE}},
            "failed_components": {
                "filter": {"term": {"event.outcome": "failure"}},
                "aggs": {
                    "components": {"terms": {"field": "gen_ai.agent.component_type", "size": COMPONENT_BUCKET_SIZE}},
                },
            },
            "top_tools": {"terms": {"field": "gen_ai.agent.tool_name", "size": TERM_BUCKET_SIZE}},
            "top_models": {"terms": {"field": "gen_ai.agent.model_name", "size": TERM_BUCKET_SIZE}},
            "mcp_methods": {"terms": {"field": "gen_ai.agent.mcp_method_name", "size": TERM_BUCKET_SIZE}},
            "error_types": {"terms": {"field": "gen_ai.agent.error_type", "size": TERM_BUCKET_SIZE}},
        },
    }


def _extract_terms(agg: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"key": bucket.get("key"), "doc_count": bucket.get("doc_count", 0)}
        for bucket in agg.get("buckets", [])
    ]


def _extract_nested_terms(agg: dict[str, Any], bucket_name: str) -> list[dict[str, Any]]:
    return _extract_terms(agg.get(bucket_name, {}))


def _extract_slow_turns(agg: dict[str, Any]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for bucket in agg.get("buckets", []):
        session_bucket = (bucket.get("sessions", {}).get("buckets", []) or [{}])[0]
        avg_latency = bucket.get("avg_latency", {}).get("value") or 0
        turns.append(
            {
                "key": bucket.get("key"),
                "doc_count": bucket.get("doc_count", 0),
                "avg_latency_ms": round(avg_latency, 2),
                "failure_count": bucket.get("failure_count", {}).get("doc_count", 0),
                "session_id": session_bucket.get("key"),
            }
        )
    return turns


def build_report(result: dict) -> dict[str, Any]:
    total = result.get("hits", {}).get("total", {}).get("value", 0)
    aggs = result.get("aggregations", {})
    with_errors = aggs.get("with_errors", {}).get("doc_count", 0)
    tool_calls = aggs.get("tool_calls", {}).get("doc_count", 0)
    tool_errors = aggs.get("tool_errors", {}).get("doc_count", 0)
    success_rate = round((total - with_errors) / total, 4) if total else 0.0
    tool_error_rate = round(tool_errors / tool_calls, 4) if tool_calls else 0.0
    percentiles = aggs.get("latency_percentiles", {}).get("values", {})
    p50_ns = percentiles.get("50.0", 0) or 0
    p95_ns = percentiles.get("95.0", 0) or 0
    return {
        "documents": total,
        "success_rate": success_rate,
        "tool_error_rate": tool_error_rate,
        "p50_latency_ms": round(p50_ns / 1_000_000, 2) if p50_ns else 0,
        "p95_latency_ms": round(p95_ns / 1_000_000, 2) if p95_ns else 0,
        "retry_total": aggs.get("retry_sum", {}).get("value", 0),
        "token_input_total": aggs.get("token_input_sum", {}).get("value", 0),
        "token_output_total": aggs.get("token_output_sum", {}).get("value", 0),
        "cost_total": aggs.get("cost_sum", {}).get("value", 0),
        "top_sessions": _extract_terms(aggs.get("top_sessions", {})),
        "failed_sessions": _extract_nested_terms(aggs.get("failed_sessions", {}), "sessions"),
        "slow_turns": _extract_slow_turns(aggs.get("slow_turns", {})),
        "top_components": _extract_terms(aggs.get("top_components", {})),
        "failed_components": _extract_nested_terms(aggs.get("failed_components", {}), "components"),
        "top_tools": _extract_terms(aggs.get("top_tools", {})),
        "top_models": _extract_terms(aggs.get("top_models", {})),
        "mcp_methods": _extract_terms(aggs.get("mcp_methods", {})),
        "error_types": _extract_terms(aggs.get("error_types", {})),
    }


def render_markdown(report: dict, config: dict) -> str:
    def render_terms(items: list[dict[str, Any]]) -> str:
        if not items:
            return "- none"
        return "\n".join(f"- {item.get('key')}: {item.get('doc_count')}" for item in items)

    def render_slow_turns(items: list[dict[str, Any]]) -> str:
        if not items:
            return "- none"
        lines = []
        for item in items:
            suffix = f" | session={item.get('session_id')}" if item.get("session_id") else ""
            lines.append(
                f"- {item.get('key')}: avg_latency_ms={item.get('avg_latency_ms')} | failures={item.get('failure_count')} | docs={item.get('doc_count')}{suffix}"
            )
        return "\n".join(lines)

    return "\n".join(
        [
            "# Agent Observability Report",
            "",
            f"- time_range: `{config.get('time_range', '24h')}`",
            f"- query_target: `{config.get('events_alias') or build_events_alias(config.get('index_prefix', 'agent-obsv'))}`",
            f"- documents: `{report['documents']}`",
            f"- success_rate: `{report['success_rate']}`",
            f"- tool_error_rate: `{report['tool_error_rate']}`",
            f"- p50_latency_ms: `{report['p50_latency_ms']}`",
            f"- p95_latency_ms: `{report['p95_latency_ms']}`",
            f"- retry_total: `{report['retry_total']}`",
            f"- token_input_total: `{report['token_input_total']}`",
            f"- token_output_total: `{report['token_output_total']}`",
            f"- cost_total: `{report['cost_total']}`",
            "",
            "## Session hotspots",
            render_terms(report["top_sessions"]),
            "",
            "## Failed sessions",
            render_terms(report["failed_sessions"]),
            "",
            "## Slow turns",
            render_slow_turns(report["slow_turns"]),
            "",
            "## Component mix",
            render_terms(report["top_components"]),
            "",
            "## Failed components",
            render_terms(report["failed_components"]),
            "",
            "## Top tools",
            render_terms(report["top_tools"]),
            "",
            "## Top models",
            render_terms(report["top_models"]),
            "",
            "## MCP methods",
            render_terms(report["mcp_methods"]),
            "",
            "## Error types",
            render_terms(report["error_types"]),
            "",
        ]
    )


def main() -> int:
    try:
        args = parse_args()
        config = read_json(Path(args.config).expanduser().resolve())
        if not isinstance(config, dict):
            raise SkillError("Report config must be a JSON object")
        credentials = validate_credential_pair(args.es_user, args.es_password)
        index_prefix = validate_index_prefix(config.get("index_prefix", "agent-obsv"))
        events_alias = str(config.get("events_alias") or build_events_alias(index_prefix)).strip()
        time_field = str(config.get("time_field") or "@timestamp").strip() or "@timestamp"
        time_range = args.time_range if args.time_range != "now-24h" else config.get("time_range", "now-24h")
        es_config = ESConfig(
            es_url=args.es_url,
            es_user=credentials[0] if credentials else None,
            es_password=credentials[1] if credentials else None,
        )
        result = es_request(es_config, "POST", f"/{events_alias}/_search", search_payload(time_range, time_field=time_field))
        report = build_report(result)
        output = Path(args.output).expanduser().resolve()
        output_format = args.format or ("json" if output.suffix.lower() == ".json" else "markdown")
        if output_format == "json":
            write_json(output, report)
        else:
            write_text(output, render_markdown(report, {**config, "time_range": time_range, "events_alias": events_alias, "time_field": time_field}))
        print(f"✅ report written: {output}")
        return 0
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Failed to generate report: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
