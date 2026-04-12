#!/usr/bin/env python3
"""Render Elasticsearch 9.x assets for agent observability.

Upgraded to use data streams, ECS-compatible mappings, component templates,
tiered ILM, structured ingest parsing, Lens visualizations, and alerting rules.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    SkillError,
    build_component_template_name,
    build_data_stream_name,
    build_events_alias,
    ensure_dir,
    print_error,
    read_json,
    validate_index_prefix,
    validate_positive_int,
    write_json,
    write_text,
)

DEFAULT_KIBANA_COLUMNS = [
    "agent.id", "trace.id", "event.action", "service.name",
    "gen_ai.agent.tool_name", "gen_ai.agent.model_name",
    "event.duration", "event.outcome",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render Elasticsearch assets")
    parser.add_argument("--discovery", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--retention-days", type=int, default=30)
    return parser.parse_args()


# ---------------------------------------------------------------------------
# ECS-compatible field mappings
# ---------------------------------------------------------------------------

def _ecs_base_properties() -> dict[str, Any]:
    """ECS base + agent-observability custom fields using ECS naming."""
    return {
        # --- ECS base ---
        "@timestamp": {"type": "date"},
        "message": {"type": "text"},
        "event.action": {"type": "keyword"},
        "event.category": {"type": "keyword"},
        "event.kind": {"type": "keyword"},
        "event.outcome": {"type": "keyword"},
        "event.duration": {"type": "long", "doc_values": True},
        "event.module": {"type": "keyword"},
        "event.dataset": {"type": "keyword"},
        # --- service / agent ---
        "service.name": {"type": "keyword"},
        "service.version": {"type": "keyword"},
        "service.environment": {"type": "keyword"},
        "agent.id": {"type": "keyword"},
        "agent.name": {"type": "keyword"},
        "agent.type": {"type": "keyword"},
        # --- trace / span ---
        "trace.id": {"type": "keyword"},
        "span.id": {"type": "keyword"},
        "parent.id": {"type": "keyword"},
        "transaction.id": {"type": "keyword"},
        # --- observer (this product) ---
        "observer.product": {"type": "keyword"},
        "observer.type": {"type": "keyword"},
        "observer.version": {"type": "keyword"},
        "observer.ingest_error": {"type": "keyword"},
        # --- host (for Elastic Agent host metrics) ---
        "host.name": {"type": "keyword"},
        "host.hostname": {"type": "keyword"},
        "host.os.platform": {"type": "keyword"},
        # --- labels ---
        "labels.recommended_modules": {"type": "keyword"},
        "labels.ingest_mode": {"type": "keyword"},
        # --- gen_ai (OpenTelemetry GenAI Semantic Conventions) ---
        "gen_ai.system": {"type": "keyword"},
        "gen_ai.request.model": {"type": "keyword"},
        "gen_ai.response.model": {"type": "keyword"},
        "gen_ai.usage.input_tokens": {"type": "long"},
        "gen_ai.usage.output_tokens": {"type": "long"},
        "gen_ai.usage.total_tokens": {"type": "long"},
        # --- agent-observability custom (under gen_ai.agent.*) ---
        "gen_ai.agent.run_id": {"type": "keyword"},
        "gen_ai.agent.turn_id": {"type": "keyword"},
        "gen_ai.agent.session_id": {"type": "keyword"},
        "gen_ai.agent.tool_name": {"type": "keyword"},
        "gen_ai.agent.model_name": {"type": "keyword"},
        "gen_ai.agent.mcp_method_name": {"type": "keyword"},
        "gen_ai.agent.module": {"type": "keyword"},
        "gen_ai.agent.module_kind": {"type": "keyword"},
        "gen_ai.agent.signal_type": {"type": "keyword"},
        "gen_ai.agent.semantic_kind": {"type": "keyword"},
        "gen_ai.agent.error_type": {"type": "keyword"},
        "gen_ai.agent.retry_count": {"type": "integer"},
        "gen_ai.agent.latency_ms": {"type": "float"},
        "gen_ai.agent.cost": {"type": "double"},
        # --- backward compat aliases (old field → new) ---
        "captured_at": {"type": "alias", "path": "@timestamp"},
    }


def build_component_template_ecs_base(index_prefix: str) -> dict[str, Any]:
    return {
        "template": {
            "mappings": {
                "dynamic": "true",
                "dynamic_templates": [
                    {"strings_as_keywords": {"match_mapping_type": "string", "mapping": {"type": "keyword", "ignore_above": 1024}}}
                ],
                "properties": _ecs_base_properties(),
            },
        },
        "_meta": {
            "product": "elasticsearch-agent-observability",
            "description": "ECS-compatible base mappings for agent observability data streams",
        },
    }


def build_component_template_settings(index_prefix: str, retention_days: int) -> dict[str, Any]:
    return {
        "template": {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 1,
                "index.default_pipeline": f"{index_prefix}-normalize",
                "index.lifecycle.name": f"{index_prefix}-lifecycle",
                "index.codec": "best_compression",
            },
        },
        "_meta": {
            "product": "elasticsearch-agent-observability",
            "retention_days": retention_days,
        },
    }


def build_index_template(index_prefix: str, modules: list[str]) -> dict[str, Any]:
    ds_name = build_data_stream_name(index_prefix)
    return {
        "index_patterns": [f"{ds_name}*"],
        "data_stream": {},
        "priority": 500,
        "composed_of": [
            build_component_template_name(index_prefix, "ecs-base"),
            build_component_template_name(index_prefix, "settings"),
        ],
        "_meta": {
            "product": "elasticsearch-agent-observability",
            "recommended_modules": modules,
        },
    }


# ---------------------------------------------------------------------------
# Ingest pipeline — structured parsing
# ---------------------------------------------------------------------------

def build_ingest_pipeline(modules: list[str]) -> dict[str, Any]:
    return {
        "description": "Normalize agent observability events: ECS alignment, structured parsing, GenAI field preservation, and redaction",
        "processors": [
            # --- ECS stamping ---
            {"set": {"field": "observer.product", "value": "elasticsearch-agent-observability"}},
            {"set": {"field": "observer.type", "value": "agent-observability"}},
            {"set": {"field": "labels.recommended_modules", "value": modules}},
            {"set": {"field": "@timestamp", "value": "{{{_ingest.timestamp}}}", "override": False}},
            # --- ECS event defaults ---
            {"set": {"field": "event.kind", "value": "event", "override": False}},
            {"set": {"field": "event.category", "value": "process", "override": False}},
            # --- backward compat: copy legacy fields to ECS ---
            {"rename": {"field": "agent_id", "target_field": "agent.id", "ignore_missing": True}},
            {"rename": {"field": "run_id", "target_field": "gen_ai.agent.run_id", "ignore_missing": True}},
            {"rename": {"field": "turn_id", "target_field": "gen_ai.agent.turn_id", "ignore_missing": True}},
            {"rename": {"field": "span_id", "target_field": "span.id", "ignore_missing": True}},
            {"rename": {"field": "parent_span_id", "target_field": "parent.id", "ignore_missing": True}},
            {"rename": {"field": "session_id", "target_field": "gen_ai.agent.session_id", "ignore_missing": True}},
            {"rename": {"field": "tool_name", "target_field": "gen_ai.agent.tool_name", "ignore_missing": True}},
            {"rename": {"field": "model_name", "target_field": "gen_ai.agent.model_name", "ignore_missing": True}},
            {"rename": {"field": "mcp_method_name", "target_field": "gen_ai.agent.mcp_method_name", "ignore_missing": True}},
            {"rename": {"field": "error_type", "target_field": "gen_ai.agent.error_type", "ignore_missing": True}},
            {"rename": {"field": "latency_ms", "target_field": "gen_ai.agent.latency_ms", "ignore_missing": True}},
            {"rename": {"field": "retry_count", "target_field": "gen_ai.agent.retry_count", "ignore_missing": True}},
            {"rename": {"field": "signal_type", "target_field": "gen_ai.agent.signal_type", "ignore_missing": True}},
            {"rename": {"field": "semantic_kind", "target_field": "gen_ai.agent.semantic_kind", "ignore_missing": True}},
            {"rename": {"field": "token_input", "target_field": "gen_ai.usage.input_tokens", "ignore_missing": True}},
            {"rename": {"field": "token_output", "target_field": "gen_ai.usage.output_tokens", "ignore_missing": True}},
            {"rename": {"field": "cost", "target_field": "gen_ai.agent.cost", "ignore_missing": True}},
            # --- compute event.duration from latency_ms if present ---
            {
                "script": {
                    "lang": "painless",
                    "source": "if (ctx.gen_ai?.agent?.latency_ms != null && ctx.event?.duration == null) { ctx.event = ctx.event ?: new HashMap(); ctx.event.duration = (long)(ctx.gen_ai.agent.latency_ms * 1000000L); }",
                    "ignore_failure": True,
                }
            },
            # --- compute event.outcome ---
            {
                "script": {
                    "lang": "painless",
                    "source": "if (ctx.event?.outcome == null) { ctx.event = ctx.event ?: new HashMap(); ctx.event.outcome = (ctx.gen_ai?.agent?.error_type != null) ? 'failure' : 'success'; }",
                    "ignore_failure": True,
                }
            },
            # --- structured log parsing (JSON body) ---
            {"json": {"field": "message", "target_field": "_parsed_message", "ignore_failure": True}},
            {
                "script": {
                    "lang": "painless",
                    "source": "if (ctx._parsed_message instanceof Map) { for (entry in ctx._parsed_message.entrySet()) { if (!ctx.containsKey(entry.getKey())) ctx[entry.getKey()] = entry.getValue(); } } ctx.remove('_parsed_message');",
                    "ignore_failure": True,
                }
            },
            # --- redact sensitive GenAI payloads ---
            {"remove": {"field": "gen_ai.prompt", "ignore_missing": True}},
            {"remove": {"field": "gen_ai.completion", "ignore_missing": True}},
            {"remove": {"field": "gen_ai.tool.call.arguments", "ignore_missing": True}},
            {"remove": {"field": "gen_ai.tool.call.result", "ignore_missing": True}},
        ],
        "on_failure": [
            {"set": {"field": "observer.ingest_error", "value": "{{ _ingest.on_failure_message }}"}}
        ],
    }


# ---------------------------------------------------------------------------
# ILM — tiered lifecycle
# ---------------------------------------------------------------------------

def build_ilm_policy(retention_days: int) -> dict[str, Any]:
    warm_age = max(1, retention_days // 5)
    cold_age = max(warm_age + 1, retention_days // 2)
    frozen_age = max(cold_age + 1, int(retention_days * 0.8))
    return {
        "policy": {
            "phases": {
                "hot": {
                    "actions": {
                        "rollover": {
                            "max_age": "7d",
                            "max_primary_shard_size": "25gb",
                            "max_docs": 50_000_000,
                        }
                    }
                },
                "warm": {
                    "min_age": f"{warm_age}d",
                    "actions": {
                        "shrink": {"number_of_shards": 1},
                        "forcemerge": {"max_num_segments": 1},
                        "readonly": {},
                    },
                },
                "cold": {
                    "min_age": f"{cold_age}d",
                    "actions": {
                        "readonly": {},
                    },
                },
                "frozen": {
                    "min_age": f"{frozen_age}d",
                    "actions": {
                        "searchable_snapshot": {
                            "snapshot_repository": "found-snapshots",
                        }
                    },
                },
                "delete": {
                    "min_age": f"{retention_days}d",
                    "actions": {"delete": {}},
                },
            }
        }
    }


# ---------------------------------------------------------------------------
# Kibana saved objects — with Lens visualizations and alerting
# ---------------------------------------------------------------------------

def _search_source(data_view_id: str, query: str = "") -> dict[str, Any]:
    return {
        "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
        "query": {"language": "kuery", "query": query},
        "filter": [],
    }


def build_search_saved_object(*, object_id: str, title: str, description: str, data_view_id: str, columns: list[str] | None = None, query: str = "") -> dict[str, Any]:
    return {
        "type": "search",
        "id": object_id,
        "attributes": {
            "title": title,
            "description": description,
            "columns": columns or DEFAULT_KIBANA_COLUMNS,
            "sort": [["@timestamp", "desc"]],
            "grid": {},
            "hideChart": False,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(_search_source(data_view_id, query), separators=(",", ":")),
            },
        },
        "references": [
            {
                "id": data_view_id,
                "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
                "type": "index-pattern",
            }
        ],
    }


def _build_lens_event_rate_visualization(*, object_id: str, data_view_id: str) -> dict[str, Any]:
    """Lens XY chart: event count over time, broken down by event.outcome."""
    state = {
        "visualization": {
            "title": "Agent event rate",
            "visualizationType": "lnsXY",
            "state": {
                "datasourceStates": {"formBased": {"layers": {"layer1": {"columns": {
                    "col-x": {"operationType": "date_histogram", "sourceField": "@timestamp", "params": {"interval": "auto"}},
                    "col-y": {"operationType": "count", "label": "Events"},
                    "col-breakdown": {"operationType": "terms", "sourceField": "event.outcome", "params": {"size": 5}},
                }, "columnOrder": ["col-x", "col-breakdown", "col-y"]}}}},
                "visualization": {"preferredSeriesType": "bar_stacked", "layers": [{"layerId": "layer1", "xAccessor": "col-x", "accessors": ["col-y"], "splitAccessor": "col-breakdown"}]},
            },
        },
    }
    return {
        "type": "lens",
        "id": object_id,
        "attributes": {
            "title": "Agent event rate",
            "description": "Event volume over time, split by success/failure.",
            "visualizationType": "lnsXY",
            "state": state,
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({"query": {"language": "kuery", "query": ""}, "filter": []}, separators=(",", ":"))},
        },
        "references": [{"id": data_view_id, "type": "index-pattern", "name": "indexpattern-datasource-layer-layer1"}],
    }


def _build_lens_latency_percentiles(*, object_id: str, data_view_id: str) -> dict[str, Any]:
    """Lens metric: P50 and P95 latency."""
    state = {
        "visualization": {
            "title": "Agent latency P50 / P95",
            "visualizationType": "lnsMetric",
            "state": {
                "datasourceStates": {"formBased": {"layers": {"layer1": {"columns": {
                    "col-p50": {"operationType": "percentile", "sourceField": "event.duration", "params": {"percentile": 50}, "label": "P50 ns"},
                    "col-p95": {"operationType": "percentile", "sourceField": "event.duration", "params": {"percentile": 95}, "label": "P95 ns"},
                }, "columnOrder": ["col-p50", "col-p95"]}}}},
                "visualization": {"layerId": "layer1", "accessor": "col-p50"},
            },
        },
    }
    return {
        "type": "lens",
        "id": object_id,
        "attributes": {
            "title": "Agent latency P50 / P95",
            "description": "P50 and P95 event.duration.",
            "visualizationType": "lnsMetric",
            "state": state,
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({"query": {"language": "kuery", "query": ""}, "filter": []}, separators=(",", ":"))},
        },
        "references": [{"id": data_view_id, "type": "index-pattern", "name": "indexpattern-datasource-layer-layer1"}],
    }


def _build_lens_top_tools(*, object_id: str, data_view_id: str) -> dict[str, Any]:
    """Lens pie: top tools by call count."""
    state = {
        "visualization": {
            "title": "Top tools by call count",
            "visualizationType": "lnsPie",
            "state": {
                "datasourceStates": {"formBased": {"layers": {"layer1": {"columns": {
                    "col-slice": {"operationType": "terms", "sourceField": "gen_ai.agent.tool_name", "params": {"size": 10}},
                    "col-metric": {"operationType": "count", "label": "Calls"},
                }, "columnOrder": ["col-slice", "col-metric"]}}}},
                "visualization": {"shape": "pie", "layers": [{"layerId": "layer1", "primaryGroups": ["col-slice"], "metric": "col-metric"}]},
            },
        },
    }
    return {
        "type": "lens",
        "id": object_id,
        "attributes": {
            "title": "Top tools by call count",
            "description": "Pie chart of most-called agent tools.",
            "visualizationType": "lnsPie",
            "state": state,
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({"query": {"language": "kuery", "query": ""}, "filter": []}, separators=(",", ":"))},
        },
        "references": [{"id": data_view_id, "type": "index-pattern", "name": "indexpattern-datasource-layer-layer1"}],
    }


def _build_lens_token_usage(*, object_id: str, data_view_id: str) -> dict[str, Any]:
    """Lens XY: token usage over time (input vs output)."""
    state = {
        "visualization": {
            "title": "Token usage over time",
            "visualizationType": "lnsXY",
            "state": {
                "datasourceStates": {"formBased": {"layers": {"layer1": {"columns": {
                    "col-x": {"operationType": "date_histogram", "sourceField": "@timestamp", "params": {"interval": "auto"}},
                    "col-input": {"operationType": "sum", "sourceField": "gen_ai.usage.input_tokens", "label": "Input tokens"},
                    "col-output": {"operationType": "sum", "sourceField": "gen_ai.usage.output_tokens", "label": "Output tokens"},
                }, "columnOrder": ["col-x", "col-input", "col-output"]}}}},
                "visualization": {"preferredSeriesType": "area_stacked", "layers": [{"layerId": "layer1", "xAccessor": "col-x", "accessors": ["col-input", "col-output"]}]},
            },
        },
    }
    return {
        "type": "lens",
        "id": object_id,
        "attributes": {
            "title": "Token usage over time",
            "description": "Input vs output token consumption per time bucket.",
            "visualizationType": "lnsXY",
            "state": state,
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({"query": {"language": "kuery", "query": ""}, "filter": []}, separators=(",", ":"))},
        },
        "references": [{"id": data_view_id, "type": "index-pattern", "name": "indexpattern-datasource-layer-layer1"}],
    }


def _build_alert_rule(*, object_id: str, data_view_id: str, index_prefix: str) -> dict[str, Any]:
    """Kibana alert rule definition for error rate threshold."""
    return {
        "type": "alert",
        "id": object_id,
        "attributes": {
            "name": f"Agent error rate threshold ({index_prefix})",
            "alertTypeId": ".es-query",
            "consumer": "alerts",
            "enabled": False,
            "schedule": {"interval": "5m"},
            "params": {
                "index": [f"{build_data_stream_name(index_prefix)}*"],
                "timeField": "@timestamp",
                "esQuery": json.dumps({"query": {"bool": {"filter": [{"exists": {"field": "gen_ai.agent.error_type"}}]}}}, separators=(",", ":")),
                "thresholdComparator": ">",
                "threshold": [10],
                "timeWindowSize": 5,
                "timeWindowUnit": "m",
                "size": 100,
            },
            "actions": [],
            "tags": ["agent-observability", index_prefix],
        },
        "references": [],
    }


def build_dashboard_saved_object(*, object_id: str, title: str, description: str, panel_refs: list[dict[str, str]]) -> dict[str, Any]:
    panels = []
    references = []
    row = 0
    for index, ref in enumerate(panel_refs):
        ref_name = f"panel_{index}"
        panel_type = ref.get("type", "search")
        width = int(ref.get("width", "24"))
        height = int(ref.get("height", "15"))
        panels.append(
            {
                "version": "9.0.0",
                "type": panel_type,
                "panelIndex": str(index + 1),
                "gridData": {"x": (index % 2) * 24, "y": row, "w": width, "h": height, "i": str(index + 1)},
                "panelRefName": ref_name,
                "embeddableConfig": {},
            }
        )
        references.append({"type": panel_type, "name": ref_name, "id": ref["id"]})
        if index % 2 == 1:
            row += height
    return {
        "type": "dashboard",
        "id": object_id,
        "attributes": {
            "title": title,
            "description": description,
            "panelsJSON": json.dumps(panels, separators=(",", ":")),
            "optionsJSON": json.dumps({"useMargins": True, "syncColors": True, "syncCursor": True, "syncTooltips": True}, separators=(",", ":")),
            "timeRestore": True,
            "timeTo": "now",
            "timeFrom": "now-24h",
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({"query": {"language": "kuery", "query": ""}, "filter": []}, separators=(",", ":")),
            },
        },
        "references": references,
    }


def build_kibana_saved_objects(index_prefix: str) -> dict[str, Any]:
    ds_name = build_data_stream_name(index_prefix)
    data_view_id = f"{index_prefix}-events-view"
    saved_search_id = f"{index_prefix}-event-stream"
    failure_search_id = f"{index_prefix}-event-failures"
    dashboard_id = f"{index_prefix}-overview"
    lens_event_rate_id = f"{index_prefix}-lens-event-rate"
    lens_latency_id = f"{index_prefix}-lens-latency"
    lens_top_tools_id = f"{index_prefix}-lens-top-tools"
    lens_token_usage_id = f"{index_prefix}-lens-token-usage"
    alert_error_rate_id = f"{index_prefix}-alert-error-rate"

    objects: list[dict[str, Any]] = [
        {
            "type": "index-pattern",
            "id": data_view_id,
            "attributes": {
                "title": f"{ds_name}*",
                "name": "Agent observability events",
                "timeFieldName": "@timestamp",
            },
        },
        build_search_saved_object(
            object_id=saved_search_id,
            title="Agent observability event stream",
            description="Default Kibana Discover surface for agent observability events.",
            data_view_id=data_view_id,
        ),
        build_search_saved_object(
            object_id=failure_search_id,
            title="Agent observability failures",
            description="Search focused on failure and ingest-error events.",
            data_view_id=data_view_id,
            query="event.outcome:failure or observer.ingest_error:*",
        ),
        _build_lens_event_rate_visualization(object_id=lens_event_rate_id, data_view_id=data_view_id),
        _build_lens_latency_percentiles(object_id=lens_latency_id, data_view_id=data_view_id),
        _build_lens_top_tools(object_id=lens_top_tools_id, data_view_id=data_view_id),
        _build_lens_token_usage(object_id=lens_token_usage_id, data_view_id=data_view_id),
        build_dashboard_saved_object(
            object_id=dashboard_id,
            title="Agent observability overview",
            description="Dashboard with event rate, latency, tool distribution, token usage, event stream, and failure stream.",
            panel_refs=[
                {"id": lens_event_rate_id, "type": "lens", "width": "24", "height": "12"},
                {"id": lens_latency_id, "type": "lens", "width": "24", "height": "12"},
                {"id": lens_top_tools_id, "type": "lens", "width": "24", "height": "12"},
                {"id": lens_token_usage_id, "type": "lens", "width": "24", "height": "12"},
                {"id": saved_search_id, "type": "search", "width": "24", "height": "15"},
                {"id": failure_search_id, "type": "search", "width": "24", "height": "15"},
            ],
        ),
        _build_alert_rule(object_id=alert_error_rate_id, data_view_id=data_view_id, index_prefix=index_prefix),
    ]
    return {
        "space": "default",
        "objects": objects,
        "summary": {
            "data_view_id": data_view_id,
            "saved_search_id": saved_search_id,
            "failure_search_id": failure_search_id,
            "dashboard_id": dashboard_id,
            "lens_ids": [lens_event_rate_id, lens_latency_id, lens_top_tools_id, lens_token_usage_id],
            "alert_ids": [alert_error_rate_id],
            "events_alias_pattern": f"{ds_name}*",
            "object_count": len(objects),
        },
    }


# ---------------------------------------------------------------------------
# Report config
# ---------------------------------------------------------------------------

def build_report_config(index_prefix: str, discovery: dict[str, Any]) -> dict[str, Any]:
    modules = sorted({module["module_kind"] for module in discovery.get("detected_modules", []) if module.get("module_kind")})
    kibana_bundle = build_kibana_saved_objects(index_prefix)
    return {
        "time_range": "now-24h",
        "time_field": "@timestamp",
        "index_prefix": index_prefix,
        "events_alias": build_events_alias(index_prefix),
        "data_stream": build_data_stream_name(index_prefix),
        "recommended_modules": modules,
        "human_surface": "kibana_dashboard",
        "kibana": kibana_bundle["summary"],
        "metrics": [
            "success_rate",
            "p50_latency_ns",
            "p95_latency_ns",
            "tool_error_rate",
            "retry_total",
            "token_input_total",
            "token_output_total",
            "cost_total",
            "top_tools",
            "top_models",
            "mcp_methods",
            "error_types",
        ],
    }


# ---------------------------------------------------------------------------
# Main render function
# ---------------------------------------------------------------------------

def render_assets(discovery: dict[str, Any], output_dir: Path, *, index_prefix: str, retention_days: int) -> dict[str, str]:
    ensure_dir(output_dir)
    validated_prefix = validate_index_prefix(index_prefix)
    validated_retention_days = validate_positive_int(retention_days, "Retention days")
    modules = sorted({module["module_kind"] for module in discovery.get("detected_modules", []) if module.get("module_kind")})

    component_ecs_base = build_component_template_ecs_base(validated_prefix)
    component_settings = build_component_template_settings(validated_prefix, validated_retention_days)
    index_template = build_index_template(validated_prefix, modules)
    ingest_pipeline = build_ingest_pipeline(modules)
    ilm_policy = build_ilm_policy(validated_retention_days)
    kibana_saved_objects = build_kibana_saved_objects(validated_prefix)
    report_config = build_report_config(validated_prefix, discovery)

    paths: dict[str, Path] = {
        "component_template_ecs_base": output_dir / "component-template-ecs-base.json",
        "component_template_settings": output_dir / "component-template-settings.json",
        "index_template": output_dir / "index-template.json",
        "ingest_pipeline": output_dir / "ingest-pipeline.json",
        "ilm_policy": output_dir / "ilm-policy.json",
        "report_config": output_dir / "report-config.json",
        "kibana_saved_objects_json": output_dir / "kibana-saved-objects.json",
        "kibana_saved_objects_ndjson": output_dir / "kibana-saved-objects.ndjson",
    }
    write_json(paths["component_template_ecs_base"], component_ecs_base)
    write_json(paths["component_template_settings"], component_settings)
    write_json(paths["index_template"], index_template)
    write_json(paths["ingest_pipeline"], ingest_pipeline)
    write_json(paths["ilm_policy"], ilm_policy)
    write_json(paths["report_config"], report_config)
    write_json(paths["kibana_saved_objects_json"], kibana_saved_objects)
    write_text(
        paths["kibana_saved_objects_ndjson"],
        "\n".join(json.dumps(item, ensure_ascii=False) for item in kibana_saved_objects["objects"]) + "\n",
    )
    return {key: str(path) for key, path in paths.items()}


def main() -> int:
    try:
        args = parse_args()
        discovery = read_json(Path(args.discovery).expanduser().resolve())
        output_dir = Path(args.output_dir).expanduser().resolve()
        paths = render_assets(discovery, output_dir, index_prefix=args.index_prefix, retention_days=args.retention_days)
        print(f"✅ Elasticsearch assets written to: {output_dir}")
        for name, path in paths.items():
            print(f"   {name}: {path}")
        return 0
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Failed to render Elasticsearch assets: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
