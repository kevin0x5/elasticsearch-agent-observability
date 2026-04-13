#!/usr/bin/env python3
"""Alert check with intelligent root-cause analysis.

This script runs as a cron-style check (no Kibana Alerting license needed).
When an anomaly is detected, it queries ES for context, builds a root-cause
analysis, and outputs a structured report that can be piped to any notification
channel (webhook, Slack, email, stdout).

Checks:
1. Error rate spike — too many event.outcome:failure in the window
2. Token consumption anomaly — total tokens exceed a dynamic threshold
3. Latency degradation — P95 latency exceeds threshold
4. Session failure hotspot — failures are concentrated in a small number of sessions
5. Retry storm — retries are concentrated in a session or tool
6. Long turn hotspot — a turn becomes much slower than the window baseline

For each triggered alert, the script:
- Queries the top contributing factors (which tool, model, session, component)
- Builds a root-cause hypothesis
- Produces a structured JSON + human-readable summary with:
  - Phenomenon (what happened)
  - Root cause (why it happened, based on evidence)
  - Recommendation (what to do)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import (
    ESConfig,
    SkillError,
    build_data_stream_name,
    es_request,
    print_error,
    validate_credential_pair,
    validate_index_prefix,
    write_json,
    write_text,
)

DEFAULT_TIME_RANGE = "now-15m"
DEFAULT_ERROR_THRESHOLD = 10
DEFAULT_P95_LATENCY_THRESHOLD_MS = 5000
DEFAULT_TOKEN_THRESHOLD_MULTIPLIER = 3.0


TERM_BUCKET_SIZE = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent observability alert check with root-cause analysis")
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--es-user", default="")
    parser.add_argument("--es-password", default="")
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--time-range", default=DEFAULT_TIME_RANGE)
    parser.add_argument("--baseline-range", default="now-24h/now-15m", help="Baseline window for anomaly comparison (start/end)")
    parser.add_argument("--error-threshold", type=int, default=DEFAULT_ERROR_THRESHOLD)
    parser.add_argument("--p95-latency-threshold-ms", type=float, default=DEFAULT_P95_LATENCY_THRESHOLD_MS)
    parser.add_argument("--token-threshold-multiplier", type=float, default=DEFAULT_TOKEN_THRESHOLD_MULTIPLIER)
    parser.add_argument("--output", help="Optional output file (JSON)")
    parser.add_argument("--output-format", choices=["json", "markdown", "text"], default="text")
    parser.add_argument("--webhook-url", default="", help="Optional webhook URL for push notification")
    parser.add_argument("--write-to-es", action="store_true", help="Write alert results back to ES as a .alerts data stream")
    parser.add_argument("--generate-crontab", action="store_true", help="Print a crontab entry for scheduling this check")
    parser.add_argument("--store-to-insight", default="", help="Path to elasticsearch-insight-store scripts/store.py for auto-storing RCA conclusions")
    parser.add_argument("--insight-es-url", default="", help="ES URL for insight-store (defaults to --es-url)")
    parser.add_argument("--insight-es-user", default="", help="ES user for insight-store (defaults to --es-user)")
    parser.add_argument("--insight-es-password", default="", help="ES password for insight-store (defaults to --es-password)")
    return parser.parse_args()


def _query_current_window(config: ESConfig, ds_name: str, time_range: str) -> dict[str, Any]:
    """Query current window stats."""
    payload = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": time_range}}},
        "aggs": {
            "error_count": {"filter": {"term": {"event.outcome": "failure"}}},
            "total_events": {"value_count": {"field": "@timestamp"}},
            "p95_latency": {"percentiles": {"field": "event.duration", "percents": [95]}},
            "token_sum": {"sum": {"field": "gen_ai.usage.input_tokens"}},
            "token_output_sum": {"sum": {"field": "gen_ai.usage.output_tokens"}},
            "retry_sum": {"sum": {"field": "gen_ai.agent.retry_count"}},
            "top_error_types": {"terms": {"field": "gen_ai.agent.error_type", "size": TERM_BUCKET_SIZE}},
            "top_error_tools": {
                "filter": {"term": {"event.outcome": "failure"}},
                "aggs": {"tools": {"terms": {"field": "gen_ai.agent.tool_name", "size": TERM_BUCKET_SIZE}}},
            },
            "top_error_models": {
                "filter": {"term": {"event.outcome": "failure"}},
                "aggs": {"models": {"terms": {"field": "gen_ai.agent.model_name", "size": TERM_BUCKET_SIZE}}},
            },
            "top_failure_sessions": {
                "filter": {"term": {"event.outcome": "failure"}},
                "aggs": {"sessions": {"terms": {"field": "gen_ai.agent.session_id", "size": TERM_BUCKET_SIZE}}},
            },
            "top_failure_components": {
                "filter": {"term": {"event.outcome": "failure"}},
                "aggs": {"components": {"terms": {"field": "gen_ai.agent.component_type", "size": TERM_BUCKET_SIZE}}},
            },
            "top_token_tools": {
                "terms": {"field": "gen_ai.agent.tool_name", "size": TERM_BUCKET_SIZE, "order": {"token_sum": "desc"}},
                "aggs": {"token_sum": {"sum": {"field": "gen_ai.usage.input_tokens"}}},
            },
            "top_token_models": {
                "terms": {"field": "gen_ai.agent.model_name", "size": TERM_BUCKET_SIZE, "order": {"token_sum": "desc"}},
                "aggs": {"token_sum": {"sum": {"field": "gen_ai.usage.input_tokens"}}},
            },
            "top_latency_tools": {
                "terms": {"field": "gen_ai.agent.tool_name", "size": TERM_BUCKET_SIZE, "order": {"p95": "desc"}},
                "aggs": {"p95": {"percentiles": {"field": "event.duration", "percents": [95]}}},
            },
            "top_retry_sessions": {
                "terms": {"field": "gen_ai.agent.session_id", "size": TERM_BUCKET_SIZE, "order": {"retry_sum": "desc"}},
                "aggs": {"retry_sum": {"sum": {"field": "gen_ai.agent.retry_count"}}},
            },
            "top_retry_tools": {
                "terms": {"field": "gen_ai.agent.tool_name", "size": TERM_BUCKET_SIZE, "order": {"retry_sum": "desc"}},
                "aggs": {"retry_sum": {"sum": {"field": "gen_ai.agent.retry_count"}}},
            },
            "top_turns_by_latency": {
                "terms": {"field": "gen_ai.agent.turn_id", "size": TERM_BUCKET_SIZE, "order": {"avg_latency": "desc"}},
                "aggs": {
                    "avg_latency": {"avg": {"field": "gen_ai.agent.latency_ms"}},
                    "sessions": {"terms": {"field": "gen_ai.agent.session_id", "size": 1}},
                    "components": {"terms": {"field": "gen_ai.agent.component_type", "size": 1}},
                    "failure_count": {"filter": {"term": {"event.outcome": "failure"}}},
                },
            },
        },
    }
    return es_request(config, "POST", f"/{ds_name}*/_search", payload)


def _query_baseline_window(config: ESConfig, ds_name: str, baseline_range: str) -> dict[str, Any]:
    """Query baseline window for comparison."""
    parts = baseline_range.split("/")
    gte = parts[0] if parts else "now-24h"
    lte = parts[1] if len(parts) > 1 else "now-15m"
    payload = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": gte, "lte": lte}}},
        "aggs": {
            "error_count": {"filter": {"term": {"event.outcome": "failure"}}},
            "total_events": {"value_count": {"field": "@timestamp"}},
            "p95_latency": {"percentiles": {"field": "event.duration", "percents": [95]}},
            "token_sum": {"sum": {"field": "gen_ai.usage.input_tokens"}},
            "token_output_sum": {"sum": {"field": "gen_ai.usage.output_tokens"}},
            "retry_sum": {"sum": {"field": "gen_ai.agent.retry_count"}},
        },
    }
    return es_request(config, "POST", f"/{ds_name}*/_search", payload)


def _extract_terms(agg: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": b.get("key"), "count": b.get("doc_count", 0)} for b in agg.get("buckets", [])]


def _extract_value_terms(agg: dict[str, Any], value_agg: str) -> list[dict[str, Any]]:
    return [
        {
            "key": b.get("key"),
            "count": b.get("doc_count", 0),
            "value": b.get(value_agg, {}).get("value", 0) or 0,
        }
        for b in agg.get("buckets", [])
    ]


def _extract_nested_terms(agg: dict[str, Any], bucket_name: str) -> list[dict[str, Any]]:
    return _extract_terms(agg.get(bucket_name, {}))


def _extract_turns(agg: dict[str, Any]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for bucket in agg.get("buckets", []):
        session_bucket = (bucket.get("sessions", {}).get("buckets", []) or [{}])[0]
        component_bucket = (bucket.get("components", {}).get("buckets", []) or [{}])[0]
        turns.append(
            {
                "key": bucket.get("key"),
                "count": bucket.get("doc_count", 0),
                "avg_latency_ms": round(bucket.get("avg_latency", {}).get("value", 0) or 0, 2),
                "failure_count": bucket.get("failure_count", {}).get("doc_count", 0),
                "session_id": session_bucket.get("key"),
                "component_type": component_bucket.get("key"),
            }
        )
    return turns


def _analyze_error_spike(current: dict[str, Any], baseline: dict[str, Any], threshold: int) -> dict[str, Any] | None:
    aggs = current.get("aggregations", {})
    error_count = aggs.get("error_count", {}).get("doc_count", 0)
    if error_count < threshold:
        return None
    total = aggs.get("total_events", {}).get("value", 0)
    error_rate = round(error_count / max(1, total), 4)
    baseline_errors = baseline.get("aggregations", {}).get("error_count", {}).get("doc_count", 0)
    baseline_total = baseline.get("aggregations", {}).get("total_events", {}).get("value", 0)
    baseline_rate = round(baseline_errors / max(1, baseline_total), 4)
    top_types = _extract_terms(aggs.get("top_error_types", {}))
    top_tools = _extract_nested_terms(aggs.get("top_error_tools", {}), "tools")
    top_models = _extract_nested_terms(aggs.get("top_error_models", {}), "models")
    top_sessions = _extract_nested_terms(aggs.get("top_failure_sessions", {}), "sessions")
    primary_type = top_types[0]["key"] if top_types else "unknown"
    primary_tool = top_tools[0]["key"] if top_tools else "unknown"
    primary_model = top_models[0]["key"] if top_models else "unknown"
    primary_session = top_sessions[0]["key"] if top_sessions else "unknown"
    issue_hint = "a downstream dependency failure" if "timeout" in str(primary_type).lower() or "connection" in str(primary_type).lower() else "an application-level issue in the tool or model integration"
    return {
        "alert_type": "error_rate_spike",
        "severity": "critical" if error_rate > 0.5 else "warning",
        "phenomenon": f"Error rate spiked to {error_rate:.1%} ({error_count} errors in window) vs baseline {baseline_rate:.1%}.",
        "root_cause": f"Top error type is `{primary_type}`, mainly from tool `{primary_tool}` using model `{primary_model}` and concentrated in session `{primary_session}`. This pattern suggests {issue_hint}.",
        "recommendation": f"1. Check `{primary_tool}` for recent changes or dependency failures. 2. Inspect `{primary_model}` availability/quota. 3. Drill into session `{primary_session}` to confirm whether the failures share one conversation path.",
        "evidence": {
            "error_count": error_count,
            "error_rate": error_rate,
            "baseline_rate": baseline_rate,
            "top_error_types": top_types,
            "top_error_tools": top_tools,
            "top_error_models": top_models,
            "top_failure_sessions": top_sessions,
        },
    }


def _analyze_token_anomaly(current: dict[str, Any], baseline: dict[str, Any], multiplier: float) -> dict[str, Any] | None:
    aggs = current.get("aggregations", {})
    b_aggs = baseline.get("aggregations", {})
    current_tokens = (aggs.get("token_sum", {}).get("value", 0) or 0) + (aggs.get("token_output_sum", {}).get("value", 0) or 0)
    baseline_tokens = (b_aggs.get("token_sum", {}).get("value", 0) or 0) + (b_aggs.get("token_output_sum", {}).get("value", 0) or 0)
    if baseline_tokens <= 0 or current_tokens <= baseline_tokens * multiplier:
        return None
    ratio = round(current_tokens / max(1, baseline_tokens), 2)
    top_tools = _extract_value_terms(aggs.get("top_token_tools", {}), value_agg="token_sum")
    top_models = _extract_value_terms(aggs.get("top_token_models", {}), value_agg="token_sum")
    top_sessions = _extract_value_terms(aggs.get("top_retry_sessions", {}), value_agg="retry_sum")
    primary_tool = top_tools[0]["key"] if top_tools else "unknown"
    primary_model = top_models[0]["key"] if top_models else "unknown"
    primary_session = top_sessions[0]["key"] if top_sessions else "unknown"
    return {
        "alert_type": "token_consumption_anomaly",
        "severity": "warning" if ratio < 5 else "critical",
        "phenomenon": f"Token consumption is {ratio}x the baseline ({current_tokens:,.0f} vs {baseline_tokens:,.0f} baseline tokens in comparable windows).",
        "root_cause": f"Tool `{primary_tool}` with model `{primary_model}` is the top consumer, and session `{primary_session}` also shows abnormal retry concentration. This could indicate {'a retry storm or looped turn' if ratio > 5 else 'increased workload or prompt bloat'}.",
        "recommendation": f"1. Check whether `{primary_tool}` is re-entering the same turn repeatedly. 2. Review recent prompt changes for `{primary_model}`. 3. Consider adding a per-turn token budget or circuit breaker for session `{primary_session}`.",
        "evidence": {
            "current_tokens": current_tokens,
            "baseline_tokens": baseline_tokens,
            "ratio": ratio,
            "top_tools": top_tools,
            "top_models": top_models,
            "top_retry_sessions": top_sessions,
        },
    }


def _analyze_latency_degradation(current: dict[str, Any], baseline: dict[str, Any], threshold_ms: float) -> dict[str, Any] | None:
    aggs = current.get("aggregations", {})
    b_aggs = baseline.get("aggregations", {})
    p95_ns = aggs.get("p95_latency", {}).get("values", {}).get("95.0", 0) or 0
    p95_ms = p95_ns / 1_000_000
    if p95_ms < threshold_ms:
        return None
    baseline_p95_ns = b_aggs.get("p95_latency", {}).get("values", {}).get("95.0", 0) or 0
    baseline_p95_ms = baseline_p95_ns / 1_000_000
    top_tools = _extract_value_terms(aggs.get("top_latency_tools", {}), value_agg="p95")
    top_turns = _extract_turns(aggs.get("top_turns_by_latency", {}))
    primary_tool = top_tools[0]["key"] if top_tools else "unknown"
    primary_turn = top_turns[0]["key"] if top_turns else "unknown"
    primary_session = top_turns[0].get("session_id") if top_turns else "unknown"
    return {
        "alert_type": "latency_degradation",
        "severity": "warning" if p95_ms < threshold_ms * 2 else "critical",
        "phenomenon": f"P95 latency is {p95_ms:,.0f}ms (threshold: {threshold_ms:,.0f}ms, baseline: {baseline_p95_ms:,.0f}ms).",
        "root_cause": f"Tool `{primary_tool}` is the top contributor and turn `{primary_turn}` in session `{primary_session}` is one of the slowest turns. This usually means {'a slow downstream API or model endpoint' if p95_ms > 10000 else 'increased concurrency or resource contention'}.",
        "recommendation": f"1. Profile `{primary_tool}` call duration. 2. Inspect slow turn `{primary_turn}` in session `{primary_session}`. 3. Consider request-level timeouts or caching for the slow path.",
        "evidence": {
            "p95_ms": round(p95_ms, 1),
            "baseline_p95_ms": round(baseline_p95_ms, 1),
            "threshold_ms": threshold_ms,
            "top_latency_tools": top_tools,
            "top_turns": top_turns,
        },
    }


def _analyze_session_failure_hotspot(current: dict[str, Any], threshold: int) -> dict[str, Any] | None:
    aggs = current.get("aggregations", {})
    total_failures = aggs.get("error_count", {}).get("doc_count", 0)
    failed_sessions = _extract_nested_terms(aggs.get("top_failure_sessions", {}), "sessions")
    failed_components = _extract_nested_terms(aggs.get("top_failure_components", {}), "components")
    if not failed_sessions:
        return None
    hottest_session = failed_sessions[0]
    session_failures = hottest_session.get("count", 0)
    concentration = session_failures / max(1, total_failures)
    if session_failures < max(3, threshold // 2) or concentration < 0.35:
        return None
    primary_component = failed_components[0]["key"] if failed_components else "unknown"
    return {
        "alert_type": "session_failure_hotspot",
        "severity": "critical" if concentration >= 0.6 else "warning",
        "phenomenon": f"Session `{hottest_session.get('key')}` accounts for {session_failures}/{max(1, total_failures)} failures in the current window ({concentration:.1%}).",
        "root_cause": f"Failures are unusually concentrated in one conversation path, with component `{primary_component}` showing up most often. This usually means a single bad workflow branch, poisoned state, or repeated bad tool/model handoff inside the session.",
        "recommendation": f"1. Open session `{hottest_session.get('key')}` in Discover. 2. Compare the failing turns against successful sessions. 3. Check whether component `{primary_component}` starts the cascade or only fails downstream.",
        "evidence": {
            "total_failures": total_failures,
            "session_failures": session_failures,
            "concentration": round(concentration, 4),
            "top_failure_sessions": failed_sessions,
            "top_failure_components": failed_components,
        },
    }


def _analyze_retry_storm(current: dict[str, Any], baseline: dict[str, Any], threshold: int) -> dict[str, Any] | None:
    aggs = current.get("aggregations", {})
    b_aggs = baseline.get("aggregations", {})
    total_retries = aggs.get("retry_sum", {}).get("value", 0) or 0
    baseline_retries = b_aggs.get("retry_sum", {}).get("value", 0) or 0
    retry_sessions = _extract_value_terms(aggs.get("top_retry_sessions", {}), value_agg="retry_sum")
    retry_tools = _extract_value_terms(aggs.get("top_retry_tools", {}), value_agg="retry_sum")
    if not retry_sessions:
        return None
    hottest_session = retry_sessions[0]
    hottest_tool = retry_tools[0] if retry_tools else {"key": "unknown", "value": 0}
    retry_value = hottest_session.get("value", 0)
    baseline_multiplier = total_retries / max(1, baseline_retries) if baseline_retries else float(total_retries)
    if retry_value < max(4, threshold // 2) and total_retries < max(6, threshold):
        return None
    return {
        "alert_type": "retry_storm",
        "severity": "critical" if retry_value >= max(10, threshold) else "warning",
        "phenomenon": f"Retries are concentrated in session `{hottest_session.get('key')}` ({retry_value:.0f} retries; total retries this window: {total_retries:.0f}).",
        "root_cause": f"Tool `{hottest_tool.get('key')}` is the main retry source. Compared with baseline, retry pressure changed by about {baseline_multiplier:.2f}x, which strongly suggests a retry storm, looped tool invocation, or missing circuit breaker.",
        "recommendation": f"1. Inspect the retry loop in session `{hottest_session.get('key')}`. 2. Add retry budget / backoff around `{hottest_tool.get('key')}`. 3. Check whether the tool is retrying due to an upstream model or network error.",
        "evidence": {
            "total_retries": total_retries,
            "baseline_retries": baseline_retries,
            "baseline_multiplier": round(baseline_multiplier, 2),
            "top_retry_sessions": retry_sessions,
            "top_retry_tools": retry_tools,
        },
    }


def _analyze_long_turn_hotspot(current: dict[str, Any], threshold_ms: float) -> dict[str, Any] | None:
    aggs = current.get("aggregations", {})
    top_turns = _extract_turns(aggs.get("top_turns_by_latency", {}))
    if not top_turns:
        return None
    slowest_turn = top_turns[0]
    avg_latency_ms = slowest_turn.get("avg_latency_ms", 0)
    if avg_latency_ms < max(1000, threshold_ms * 0.6):
        return None
    component_type = slowest_turn.get("component_type") or "unknown"
    session_id = slowest_turn.get("session_id") or "unknown"
    return {
        "alert_type": "long_turn_hotspot",
        "severity": "critical" if avg_latency_ms >= threshold_ms * 1.5 else "warning",
        "phenomenon": f"Turn `{slowest_turn.get('key')}` in session `{session_id}` averages {avg_latency_ms:,.0f}ms and is the slowest turn in the window.",
        "root_cause": f"Component `{component_type}` dominates this turn's slow path. Long-turn hotspots usually come from one stalled tool/model step, not uniform system slowdown.",
        "recommendation": f"1. Drill into turn `{slowest_turn.get('key')}` and compare child spans. 2. Check whether component `{component_type}` is waiting on a model/tool dependency. 3. Add per-turn timeout or partial-fail handling if this path is user-facing.",
        "evidence": {
            "top_turns": top_turns,
            "slowest_turn": slowest_turn,
            "threshold_ms": threshold_ms,
        },
    }


def run_alert_check(
    config: ESConfig,
    *,
    index_prefix: str,
    time_range: str,
    baseline_range: str,
    error_threshold: int,
    p95_latency_threshold_ms: float,
    token_threshold_multiplier: float,
) -> dict[str, Any]:
    ds_name = build_data_stream_name(index_prefix)
    current = _query_current_window(config, ds_name, time_range)
    baseline = _query_baseline_window(config, ds_name, baseline_range)
    alerts: list[dict[str, Any]] = []
    for alert in [
        _analyze_error_spike(current, baseline, error_threshold),
        _analyze_token_anomaly(current, baseline, token_threshold_multiplier),
        _analyze_latency_degradation(current, baseline, p95_latency_threshold_ms),
        _analyze_session_failure_hotspot(current, error_threshold),
        _analyze_retry_storm(current, baseline, error_threshold),
        _analyze_long_turn_hotspot(current, p95_latency_threshold_ms),
    ]:
        if alert:
            alerts.append(alert)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "index_prefix": index_prefix,
        "time_range": time_range,
        "baseline_range": baseline_range,
        "alert_count": len(alerts),
        "status": "alert" if alerts else "ok",
        "alerts": alerts,
    }


def _send_webhook(url: str, payload: dict[str, Any]) -> None:
    import urllib.request

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            _ = response.read()
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ webhook delivery failed: {exc}", file=sys.stderr)


def _write_alert_to_es(config: ESConfig, index_prefix: str, result: dict[str, Any]) -> None:
    """Write alert check results back to ES as a .alerts data stream for Kibana consumption."""
    alerts_ds = f"{index_prefix}-alerts"
    for alert in result.get("alerts", []):
        evidence = alert.get("evidence", {})
        top_sessions = evidence.get("top_failure_sessions") or evidence.get("top_retry_sessions") or []
        top_turns = evidence.get("top_turns") or []
        top_components = evidence.get("top_failure_components") or []
        doc = {
            "@timestamp": result["checked_at"],
            "event.kind": "alert",
            "event.category": "process",
            "event.action": alert["alert_type"],
            "event.outcome": "failure",
            "service.name": "alert-and-diagnose",
            "gen_ai.agent.signal_type": "alert_check",
            "alert.severity": alert["severity"],
            "alert.phenomenon": alert["phenomenon"],
            "alert.root_cause": alert["root_cause"],
            "alert.recommendation": alert["recommendation"],
            "message": f"[{alert['severity'].upper()}] {alert['alert_type']}: {alert['phenomenon']}",
        }
        if top_sessions:
            doc["gen_ai.agent.session_id"] = top_sessions[0].get("key")
        if top_turns:
            doc["gen_ai.agent.turn_id"] = top_turns[0].get("key")
        if top_components:
            doc["gen_ai.agent.component_type"] = top_components[0].get("key")
        try:
            es_request(config, "POST", f"/{alerts_ds}/_doc", doc)
        except SkillError as exc:
            print(f"⚠️ failed to write alert to ES: {exc}", file=sys.stderr)
    if not result.get("alerts"):
        doc = {
            "@timestamp": result["checked_at"],
            "event.kind": "alert",
            "event.category": "process",
            "event.action": "alert_check_ok",
            "event.outcome": "success",
            "service.name": "alert-and-diagnose",
            "gen_ai.agent.signal_type": "alert_check",
            "message": "No alerts triggered",
        }
        try:
            es_request(config, "POST", f"/{alerts_ds}/_doc", doc)
        except SkillError as exc:
            print(f"⚠️ failed to write alert status to ES: {exc}", file=sys.stderr)


def _store_to_insight(*, store_script: str, result: dict[str, Any], es_url: str, es_user: str, es_password: str) -> None:
    """Store each RCA conclusion into elasticsearch-insight-store."""
    import subprocess
    import tempfile

    store_path = Path(store_script).expanduser().resolve()
    if not store_path.exists():
        print(f"⚠️ insight-store script not found: {store_path}", file=sys.stderr)
        return

    for alert in result.get("alerts", []):
        title = f"[{alert['severity'].upper()}] {alert['alert_type']} — {result.get('checked_at', 'unknown')}"
        content_lines = [
            f"# {alert['alert_type']}",
            "",
            f"**Severity**: {alert['severity']}",
            f"**Checked at**: {result.get('checked_at', 'unknown')}",
            f"**Time range**: {result.get('time_range', 'unknown')}",
            "",
            "## Phenomenon",
            "",
            alert.get("phenomenon", ""),
            "",
            "## Root Cause",
            "",
            alert.get("root_cause", ""),
            "",
            "## Recommendation",
            "",
            alert.get("recommendation", ""),
            "",
            "## Evidence",
            "",
            f"```json\n{json.dumps(alert.get('evidence', {}), indent=2, ensure_ascii=False)}\n```",
        ]
        tags = f"alert,{alert['alert_type']},{alert['severity']}"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as tmp:
            tmp.write("\n".join(content_lines))
            tmp_path = tmp.name

        cmd = [
            sys.executable,
            str(store_path),
            "--es-url",
            es_url,
        ]
        if es_user:
            cmd.extend(["--es-user", es_user])
        if es_password:
            cmd.extend(["--es-pass", es_password])
        cmd.extend([
            "store",
            "--title",
            title,
            "--tags",
            tags,
            "--file",
            tmp_path,
        ])

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)  # noqa: S603
            print(f"   📝 RCA stored to insight-store: {title}")
        except subprocess.CalledProcessError as exc:
            print(f"⚠️ failed to store RCA to insight-store: {exc.stderr.decode('utf-8', errors='ignore')[:200]}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ insight-store call failed: {exc}", file=sys.stderr)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


def _print_crontab(args: Any) -> None:
    """Print a ready-to-use crontab entry for scheduling this check."""
    cmd_parts = [
        "python scripts/alert_and_diagnose.py",
        f"--es-url {args.es_url}",
        f"--index-prefix {args.index_prefix}",
        f"--time-range {args.time_range}",
    ]
    if args.es_user:
        cmd_parts.append(f"--es-user {args.es_user}")
        cmd_parts.append("--es-password $ALERT_ES_PASSWORD")
    if args.webhook_url:
        cmd_parts.append(f"--webhook-url {args.webhook_url}")
    if args.write_to_es:
        cmd_parts.append("--write-to-es")
    cmd = " ".join(cmd_parts)
    print("\n# --- Crontab entry (every 15 minutes) ---")
    print(f"*/15 * * * * cd /path/to/elasticsearch-agent-observability && {cmd}")
    print("# ---")
    print("\n# --- systemd timer alternative ---")
    print(f"# ExecStart={cmd}")
    print("# OnCalendar=*:0/15")
    print("# ---")


def render_text(result: dict[str, Any]) -> str:
    if result["status"] == "ok":
        return f"✅ [{result['checked_at']}] No alerts triggered. ({result['time_range']})"
    lines = [f"🚨 [{result['checked_at']}] {result['alert_count']} alert(s) triggered ({result['time_range']})", ""]
    for alert in result["alerts"]:
        lines.extend(
            [
                f"--- [{alert['severity'].upper()}] {alert['alert_type']} ---",
                f"Phenomenon: {alert['phenomenon']}",
                f"Root cause:  {alert['root_cause']}",
                f"Recommendation: {alert['recommendation']}",
                "",
            ]
        )
    return "\n".join(lines)


def render_markdown(result: dict[str, Any]) -> str:
    if result["status"] == "ok":
        return f"# ✅ No alerts\n\nChecked at `{result['checked_at']}` for window `{result['time_range']}`.\n"
    lines = [f"# 🚨 {result['alert_count']} Alert(s)", "", f"- checked_at: `{result['checked_at']}`", f"- window: `{result['time_range']}`", ""]
    for alert in result["alerts"]:
        lines.extend(
            [
                f"## [{alert['severity'].upper()}] {alert['alert_type']}",
                "",
                f"**Phenomenon**: {alert['phenomenon']}",
                "",
                f"**Root cause**: {alert['root_cause']}",
                "",
                f"**Recommendation**: {alert['recommendation']}",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    try:
        args = parse_args()
        credentials = validate_credential_pair(args.es_user, args.es_password)
        config = ESConfig(
            es_url=args.es_url,
            es_user=credentials[0] if credentials else None,
            es_password=credentials[1] if credentials else None,
        )
        result = run_alert_check(
            config,
            index_prefix=validate_index_prefix(args.index_prefix),
            time_range=args.time_range,
            baseline_range=args.baseline_range,
            error_threshold=args.error_threshold,
            p95_latency_threshold_ms=args.p95_latency_threshold_ms,
            token_threshold_multiplier=args.token_threshold_multiplier,
        )
        if args.output:
            output_path = Path(args.output).expanduser().resolve()
            if args.output_format == "json":
                write_json(output_path, result)
            elif args.output_format == "markdown":
                write_text(output_path, render_markdown(result))
            else:
                write_text(output_path, render_text(result))
            print(f"✅ alert check written: {output_path}")
        else:
            print(render_text(result))
        if args.webhook_url and result["status"] == "alert":
            _send_webhook(args.webhook_url, result)
        if args.write_to_es:
            _write_alert_to_es(config, args.index_prefix, result)
        if args.store_to_insight and result["status"] == "alert":
            _store_to_insight(
                store_script=args.store_to_insight,
                result=result,
                es_url=args.insight_es_url or args.es_url,
                es_user=args.insight_es_user or args.es_user,
                es_password=args.insight_es_password or args.es_password,
            )
        if args.generate_crontab:
            _print_crontab(args)
        return 0 if result["status"] == "ok" else 2
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Alert check failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
