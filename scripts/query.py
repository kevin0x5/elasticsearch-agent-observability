#!/usr/bin/env python3
"""High-frequency query templates for agent observability.

Provides pre-built queries so users never need to write raw ES DSL for the
most common debugging and inspection workflows.

Subcommands:
    trace       Show all events for a given trace ID, ordered by time.
    tools       Top tool calls in a time window, with error rates.
    errors      Recent errors with context.
    sessions    Activity summary per conversation/session.
    timeline    Step-by-step timeline of an agent run.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from common import (
    ESConfig,
    SkillError,
    build_data_stream_name,
    es_request,
    print_error,
    validate_credential_pair,
    validate_index_prefix,
)


DEFAULT_SIZE = 50
DEFAULT_TIME_RANGE = "now-24h"
INTERNAL_DATASET_FILTER = {"term": {"event.dataset": "internal.sanity_check"}}

# ---------------------------------------------------------------------------
# Query builders — each returns a (path, payload) tuple
# ---------------------------------------------------------------------------


def query_trace(index: str, trace_id: str, size: int = 200) -> tuple[str, dict]:
    """All events for a single trace, sorted chronologically."""
    return f"/{index}*/_search", {
        "size": size,
        "sort": [{"@timestamp": "asc"}],
        "query": {"bool": {
            "must": [{"term": {"trace.id": trace_id}}],
            "must_not": [INTERNAL_DATASET_FILTER],
        }},
        "_source": [
            "@timestamp", "event.action", "event.outcome", "service.name",
            "gen_ai.tool.name", "gen_ai.request.model", "gen_ai.agent_ext.latency_ms",
            "gen_ai.agent_ext.turn_id", "gen_ai.agent_ext.component_type",
            "gen_ai.conversation.id", "gen_ai.agent.id", "span.id",
            "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
            "error.type", "message",
        ],
    }


def query_tools(index: str, time_range: str, size: int = 10) -> tuple[str, dict]:
    """Top tools by call count with error breakdown."""
    return f"/{index}*/_search", {
        "size": 0,
        "query": {"bool": {
            "filter": [
                {"range": {"@timestamp": {"gte": time_range}}},
                {"exists": {"field": "gen_ai.tool.name"}},
            ],
            "must_not": [INTERNAL_DATASET_FILTER],
        }},
        "aggs": {
            "tools": {
                "terms": {"field": "gen_ai.tool.name", "size": size},
                "aggs": {
                    "errors": {"filter": {"term": {"event.outcome": "failure"}}},
                    "avg_latency": {"avg": {"field": "gen_ai.agent_ext.latency_ms"}},
                    "p95_latency": {"percentiles": {"field": "gen_ai.agent_ext.latency_ms", "percents": [95]}},
                },
            }
        },
    }


def query_errors(index: str, time_range: str, size: int = 20) -> tuple[str, dict]:
    """Recent errors with full context."""
    return f"/{index}*/_search", {
        "size": size,
        "sort": [{"@timestamp": "desc"}],
        "query": {"bool": {
            "must": [{"term": {"event.outcome": "failure"}}],
            "filter": [{"range": {"@timestamp": {"gte": time_range}}}],
            "must_not": [INTERNAL_DATASET_FILTER],
        }},
        "_source": [
            "@timestamp", "event.action", "service.name", "error.type",
            "gen_ai.tool.name", "gen_ai.request.model", "gen_ai.agent_ext.latency_ms",
            "gen_ai.conversation.id", "gen_ai.agent.id", "trace.id",
            "gen_ai.agent_ext.component_type", "message",
        ],
    }


def query_sessions(index: str, time_range: str, size: int = 10) -> tuple[str, dict]:
    """Activity summary per conversation session."""
    return f"/{index}*/_search", {
        "size": 0,
        "query": {"bool": {
            "filter": [
                {"range": {"@timestamp": {"gte": time_range}}},
                {"exists": {"field": "gen_ai.conversation.id"}},
            ],
            "must_not": [INTERNAL_DATASET_FILTER],
        }},
        "aggs": {
            "sessions": {
                "terms": {"field": "gen_ai.conversation.id", "size": size, "order": {"_count": "desc"}},
                "aggs": {
                    "errors": {"filter": {"term": {"event.outcome": "failure"}}},
                    "tools_used": {"cardinality": {"field": "gen_ai.tool.name"}},
                    "total_tokens": {"sum": {"field": "gen_ai.usage.input_tokens"}},
                    "total_cost": {"sum": {"field": "gen_ai.agent_ext.cost"}},
                    "time_range": {"stats": {"field": "@timestamp"}},
                },
            }
        },
    }


def query_timeline(index: str, agent_run_id: str, size: int = 200) -> tuple[str, dict]:
    """Step-by-step timeline of a single agent run."""
    return f"/{index}*/_search", {
        "size": size,
        "sort": [{"@timestamp": "asc"}],
        "query": {"bool": {
            "should": [
                {"term": {"gen_ai.agent.id": agent_run_id}},
                {"term": {"trace.id": agent_run_id}},
            ],
            "minimum_should_match": 1,
            "must_not": [INTERNAL_DATASET_FILTER],
        }},
        "_source": [
            "@timestamp", "event.action", "event.outcome",
            "gen_ai.tool.name", "gen_ai.request.model",
            "gen_ai.agent_ext.latency_ms", "gen_ai.agent_ext.turn_id",
            "gen_ai.agent_ext.component_type", "gen_ai.agent_ext.semantic_kind",
            "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
            "gen_ai.agent_ext.cost", "span.id", "error.type", "message",
        ],
    }


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _render_hits(hits: list[dict]) -> str:
    if not hits:
        return "(no results)"
    lines: list[str] = []
    for hit in hits:
        src = hit.get("_source", {})
        ts = src.get("@timestamp", "?")
        action = src.get("event.action", "?")
        outcome = src.get("event.outcome", "?")
        tool = src.get("gen_ai.tool.name", "")
        model = src.get("gen_ai.request.model", "")
        latency = src.get("gen_ai.agent_ext.latency_ms", "")
        component = src.get("gen_ai.agent_ext.component_type", "")
        parts = [ts, action, outcome]
        if tool:
            parts.append(f"tool={tool}")
        if model:
            parts.append(f"model={model}")
        if component:
            parts.append(f"component={component}")
        if latency:
            parts.append(f"{latency}ms")
        err = src.get("error.type", "")
        if err:
            parts.append(f"error={err}")
        lines.append("  ".join(str(p) for p in parts))
    return "\n".join(lines)


def _render_tool_aggs(aggs: dict) -> str:
    buckets = aggs.get("tools", {}).get("buckets", [])
    if not buckets:
        return "(no tool calls)"
    lines: list[str] = []
    for b in buckets:
        name = b["key"]
        total = b["doc_count"]
        errors = b.get("errors", {}).get("doc_count", 0)
        avg_lat = b.get("avg_latency", {}).get("value")
        p95 = (b.get("p95_latency", {}).get("values") or {}).get("95.0")
        parts = [f"{name}: {total} calls, {errors} errors"]
        if avg_lat is not None:
            parts.append(f"avg={avg_lat:.1f}ms")
        if p95 is not None:
            parts.append(f"p95={p95:.1f}ms")
        lines.append("  ".join(parts))
    return "\n".join(lines)


def _render_session_aggs(aggs: dict) -> str:
    buckets = aggs.get("sessions", {}).get("buckets", [])
    if not buckets:
        return "(no sessions)"
    lines: list[str] = []
    for b in buckets:
        sid = b["key"]
        total = b["doc_count"]
        errors = b.get("errors", {}).get("doc_count", 0)
        tools = b.get("tools_used", {}).get("value", 0)
        tokens = b.get("total_tokens", {}).get("value", 0)
        cost = b.get("total_cost", {}).get("value", 0)
        parts = [f"{sid}: {total} events, {errors} errors, {tools} tools, {int(tokens)} tokens"]
        if cost:
            parts.append(f"${cost:.4f}")
        lines.append("  ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pre-built query templates for agent observability",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--es-user", default="")
    parser.add_argument("--es-password", default="")
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--no-verify-tls", action="store_true")
    parser.add_argument("--json", action="store_true", help="Raw JSON output")

    sub = parser.add_subparsers(dest="command", required=True)

    p_trace = sub.add_parser("trace", help="Events for a trace ID")
    p_trace.add_argument("trace_id")
    p_trace.add_argument("--size", type=int, default=200)

    p_tools = sub.add_parser("tools", help="Top tools with error rates")
    p_tools.add_argument("--time-range", default=DEFAULT_TIME_RANGE)
    p_tools.add_argument("--size", type=int, default=10)

    p_errors = sub.add_parser("errors", help="Recent errors")
    p_errors.add_argument("--time-range", default=DEFAULT_TIME_RANGE)
    p_errors.add_argument("--size", type=int, default=20)

    p_sessions = sub.add_parser("sessions", help="Session activity summary")
    p_sessions.add_argument("--time-range", default=DEFAULT_TIME_RANGE)
    p_sessions.add_argument("--size", type=int, default=10)

    p_timeline = sub.add_parser("timeline", help="Step-by-step agent run timeline")
    p_timeline.add_argument("run_id", help="gen_ai.agent.id or trace.id")
    p_timeline.add_argument("--size", type=int, default=200)

    return parser.parse_args()


def run_query(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the selected query and return raw ES response."""
    credentials = validate_credential_pair(args.es_user, args.es_password)
    config = ESConfig(
        es_url=args.es_url,
        es_user=credentials[0] if credentials else None,
        es_password=credentials[1] if credentials else None,
        verify_tls=not args.no_verify_tls,
    )
    index_prefix = validate_index_prefix(args.index_prefix)
    ds = build_data_stream_name(index_prefix)

    builders = {
        "trace": lambda: query_trace(ds, args.trace_id, args.size),
        "tools": lambda: query_tools(ds, args.time_range, args.size),
        "errors": lambda: query_errors(ds, args.time_range, args.size),
        "sessions": lambda: query_sessions(ds, args.time_range, args.size),
        "timeline": lambda: query_timeline(ds, args.run_id, args.size),
    }

    path, payload = builders[args.command]()
    return es_request(config, "POST", path, payload)


def main() -> int:
    try:
        args = parse_args()
        result = run_query(args)

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        renderers = {
            "trace": lambda r: _render_hits(r.get("hits", {}).get("hits", [])),
            "tools": lambda r: _render_tool_aggs(r.get("aggregations", {})),
            "errors": lambda r: _render_hits(r.get("hits", {}).get("hits", [])),
            "sessions": lambda r: _render_session_aggs(r.get("aggregations", {})),
            "timeline": lambda r: _render_hits(r.get("hits", {}).get("hits", [])),
        }
        print(renderers[args.command](result))
        return 0
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Query failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
