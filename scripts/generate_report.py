#!/usr/bin/env python3
"""Generate Markdown or JSON reports from Elasticsearch agent observability data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import ESConfig, SkillError, es_request, print_error, read_json, write_json, write_text


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


def search_payload(time_range: str) -> dict[str, Any]:
    return {
        "size": 0,
        "query": {"range": {"captured_at": {"gte": time_range}}},
        "aggs": {
            "with_errors": {"filter": {"exists": {"field": "error_type"}}},
            "tool_calls": {"filter": {"exists": {"field": "tool_name"}}},
            "tool_errors": {
                "filter": {
                    "bool": {
                        "must": [{"exists": {"field": "tool_name"}}, {"exists": {"field": "error_type"}}]
                    }
                }
            },
            "latency_percentiles": {"percentiles": {"field": "latency_ms", "percents": [50, 95]}},
            "retry_sum": {"sum": {"field": "retry_count"}},
            "token_input_sum": {"sum": {"field": "token_input"}},
            "token_output_sum": {"sum": {"field": "token_output"}},
            "cost_sum": {"sum": {"field": "cost"}},
            "top_tools": {"terms": {"field": "tool_name", "size": 5}},
            "top_models": {"terms": {"field": "model_name", "size": 5}},
            "mcp_methods": {"terms": {"field": "mcp_method_name", "size": 5}},
            "error_types": {"terms": {"field": "error_type", "size": 5}},
        },
    }


def build_report(result: dict) -> dict[str, Any]:
    total = result.get("hits", {}).get("total", {}).get("value", 0)
    with_errors = result.get("aggregations", {}).get("with_errors", {}).get("doc_count", 0)
    tool_calls = result.get("aggregations", {}).get("tool_calls", {}).get("doc_count", 0)
    tool_errors = result.get("aggregations", {}).get("tool_errors", {}).get("doc_count", 0)
    success_rate = round((total - with_errors) / total, 4) if total else 0.0
    tool_error_rate = round(tool_errors / tool_calls, 4) if tool_calls else 0.0
    percentiles = result.get("aggregations", {}).get("latency_percentiles", {}).get("values", {})
    return {
        "documents": total,
        "success_rate": success_rate,
        "tool_error_rate": tool_error_rate,
        "p50_latency_ms": percentiles.get("50.0", 0),
        "p95_latency_ms": percentiles.get("95.0", 0),
        "retry_total": result.get("aggregations", {}).get("retry_sum", {}).get("value", 0),
        "token_input_total": result.get("aggregations", {}).get("token_input_sum", {}).get("value", 0),
        "token_output_total": result.get("aggregations", {}).get("token_output_sum", {}).get("value", 0),
        "cost_total": result.get("aggregations", {}).get("cost_sum", {}).get("value", 0),
        "top_tools": result.get("aggregations", {}).get("top_tools", {}).get("buckets", []),
        "top_models": result.get("aggregations", {}).get("top_models", {}).get("buckets", []),
        "mcp_methods": result.get("aggregations", {}).get("mcp_methods", {}).get("buckets", []),
        "error_types": result.get("aggregations", {}).get("error_types", {}).get("buckets", []),
    }


def render_markdown(report: dict, config: dict) -> str:
    def render_terms(items: list[dict]) -> str:
        if not items:
            return "- none"
        return "\n".join(f"- {item.get('key')}: {item.get('doc_count')}" for item in items)

    return "\n".join(
        [
            "# Agent Observability Report",
            "",
            f"- time_range: `{config.get('time_range', '24h')}`",
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
        es_config = ESConfig(es_url=args.es_url, es_user=args.es_user, es_password=args.es_password)
        index_prefix = config.get("index_prefix", "agent-obsv")
        time_range = args.time_range if args.time_range != "now-24h" else config.get("time_range", "now-24h")
        result = es_request(es_config, "POST", f"/{index_prefix}-*/_search", search_payload(time_range))
        report = build_report(result)
        output = Path(args.output).expanduser().resolve()
        output_format = args.format or ("json" if output.suffix.lower() == ".json" else "markdown")
        if output_format == "json":
            write_json(output, report)
        else:
            write_text(output, render_markdown(report, {**config, "time_range": time_range}))
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
