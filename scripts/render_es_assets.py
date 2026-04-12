#!/usr/bin/env python3
"""Render Elasticsearch 9.x assets for agent observability."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    SkillError,
    build_events_alias,
    ensure_dir,
    print_error,
    read_json,
    validate_index_prefix,
    validate_positive_int,
    write_json,
    write_text,
)

DEFAULT_KIBANA_COLUMNS = ["agent_id", "run_id", "tool_name", "model_name", "latency_ms", "error_type"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render Elasticsearch assets")
    parser.add_argument("--discovery", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--retention-days", type=int, default=30)
    return parser.parse_args()


def build_index_template(index_prefix: str, modules: list[str]) -> dict[str, Any]:
    events_alias = build_events_alias(index_prefix)
    return {
        "index_patterns": [f"{events_alias}-*"],
        "priority": 200,
        "template": {
            "settings": {
                "number_of_shards": 1,
                "index.default_pipeline": f"{index_prefix}-normalize",
                "index.lifecycle.name": f"{index_prefix}-lifecycle",
                "index.lifecycle.rollover_alias": events_alias,
            },
            "mappings": {
                "dynamic": True,
                "properties": {
                    "agent_id": {"type": "keyword"},
                    "run_id": {"type": "keyword"},
                    "turn_id": {"type": "keyword"},
                    "span_id": {"type": "keyword"},
                    "parent_span_id": {"type": "keyword"},
                    "signal_type": {"type": "keyword"},
                    "semantic_kind": {"type": "keyword"},
                    "agent.module": {"type": "keyword"},
                    "agent.module_kind": {"type": "keyword"},
                    "tool_name": {"type": "keyword"},
                    "model_name": {"type": "keyword"},
                    "mcp_method_name": {"type": "keyword"},
                    "error_type": {"type": "keyword"},
                    "retry_count": {"type": "integer"},
                    "latency_ms": {"type": "float"},
                    "token_input": {"type": "long"},
                    "token_output": {"type": "long"},
                    "cost": {"type": "double"},
                    "session_id": {"type": "keyword"},
                    "captured_at": {"type": "date"},
                    "agent.discovery.modules": {"type": "keyword"},
                    "labels.recommended_modules": {"type": "keyword"},
                    "observer.ingest_error": {"type": "keyword"},
                },
            },
        },
        "_meta": {
            "product": "elasticsearch-agent-observability",
            "recommended_modules": modules,
        },
    }


def build_ingest_pipeline(modules: list[str]) -> dict[str, Any]:
    return {
        "description": "Normalize agent observability events, stamp captured_at, and apply light redaction",
        "processors": [
            {"set": {"field": "observer.product", "value": "elasticsearch-agent-observability"}},
            {"set": {"field": "labels.recommended_modules", "value": modules}},
            {"set": {"field": "captured_at", "value": "{{{_ingest.timestamp}}}", "override": False}},
            {"remove": {"field": "gen_ai.prompt", "ignore_missing": True}},
            {"remove": {"field": "gen_ai.tool.call.arguments", "ignore_missing": True}},
            {"remove": {"field": "gen_ai.tool.call.result", "ignore_missing": True}},
        ],
        "on_failure": [
            {"set": {"field": "observer.ingest_error", "value": "{{ _ingest.on_failure_message }}"}}
        ],
    }


def build_ilm_policy(retention_days: int) -> dict[str, Any]:
    return {
        "policy": {
            "phases": {
                "hot": {
                    "actions": {
                        "rollover": {
                            "max_age": "7d",
                            "max_primary_shard_size": "25gb",
                        }
                    }
                },
                "delete": {
                    "min_age": f"{retention_days}d",
                    "actions": {"delete": {}},
                },
            }
        }
    }


def _search_source(data_view_id: str, query: str = "") -> dict[str, Any]:
    return {
        "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
        "query": {"language": "kuery", "query": query},
        "filter": [],
    }


def build_search_saved_object(*, object_id: str, title: str, description: str, data_view_id: str, query: str = "") -> dict[str, Any]:
    return {
        "type": "search",
        "id": object_id,
        "attributes": {
            "title": title,
            "description": description,
            "columns": DEFAULT_KIBANA_COLUMNS,
            "sort": [["captured_at", "desc"]],
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


def build_dashboard_saved_object(*, object_id: str, title: str, description: str, search_ids: list[str]) -> dict[str, Any]:
    panels = []
    references = []
    for index, search_id in enumerate(search_ids):
        ref_name = f"panel_{index}"
        panels.append(
            {
                "version": "9.0.0",
                "type": "search",
                "panelIndex": str(index + 1),
                "gridData": {"x": 0 if index == 0 else 24, "y": 0, "w": 24, "h": 15, "i": str(index + 1)},
                "panelRefName": ref_name,
                "embeddableConfig": {},
            }
        )
        references.append({"type": "search", "name": ref_name, "id": search_id})
    return {
        "type": "dashboard",
        "id": object_id,
        "attributes": {
            "title": title,
            "description": description,
            "panelsJSON": json.dumps(panels, separators=(",", ":")),
            "optionsJSON": json.dumps({"useMargins": True, "syncColors": False, "syncCursor": True, "syncTooltips": True}, separators=(",", ":")),
            "timeRestore": False,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({"query": {"language": "kuery", "query": ""}, "filter": []}, separators=(",", ":")),
            },
        },
        "references": references,
    }


def build_kibana_saved_objects(index_prefix: str) -> dict[str, Any]:
    events_alias = build_events_alias(index_prefix)
    data_view_id = f"{index_prefix}-events-view"
    saved_search_id = f"{index_prefix}-event-stream"
    failure_search_id = f"{index_prefix}-event-failures"
    dashboard_id = f"{index_prefix}-overview"
    objects = [
        {
            "type": "index-pattern",
            "id": data_view_id,
            "attributes": {
                "title": f"{events_alias}*",
                "name": "Agent observability events",
                "timeFieldName": "captured_at",
            },
        },
        build_search_saved_object(
            object_id=saved_search_id,
            title="Agent observability event stream",
            description="Default Kibana Discover surface for agent observability events.",
            data_view_id=data_view_id,
            query="",
        ),
        build_search_saved_object(
            object_id=failure_search_id,
            title="Agent observability failures",
            description="Starter search focused on failure and ingest-error events.",
            data_view_id=data_view_id,
            query="error_type:* or observer.ingest_error:*",
        ),
        build_dashboard_saved_object(
            object_id=dashboard_id,
            title="Agent observability overview",
            description="Starter dashboard that pins the full event stream and failure stream side-by-side.",
            search_ids=[saved_search_id, failure_search_id],
        ),
    ]
    return {
        "space": "default",
        "objects": objects,
        "summary": {
            "data_view_id": data_view_id,
            "saved_search_id": saved_search_id,
            "failure_search_id": failure_search_id,
            "dashboard_id": dashboard_id,
            "events_alias_pattern": f"{events_alias}*",
            "object_count": len(objects),
        },
    }


def build_report_config(index_prefix: str, discovery: dict[str, Any]) -> dict[str, Any]:
    modules = sorted({module["module_kind"] for module in discovery.get("detected_modules", []) if module.get("module_kind")})
    kibana_bundle = build_kibana_saved_objects(index_prefix)
    return {
        "time_range": "now-24h",
        "time_field": "captured_at",
        "index_prefix": index_prefix,
        "events_alias": build_events_alias(index_prefix),
        "recommended_modules": modules,
        "human_surface": "kibana_dashboard",
        "kibana": kibana_bundle["summary"],
        "metrics": [
            "success_rate",
            "p50_latency_ms",
            "p95_latency_ms",
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


def render_assets(discovery: dict[str, Any], output_dir: Path, *, index_prefix: str, retention_days: int) -> dict[str, str]:
    ensure_dir(output_dir)
    validated_prefix = validate_index_prefix(index_prefix)
    validated_retention_days = validate_positive_int(retention_days, "Retention days")
    modules = sorted({module["module_kind"] for module in discovery.get("detected_modules", []) if module.get("module_kind")})
    index_template = build_index_template(validated_prefix, modules)
    ingest_pipeline = build_ingest_pipeline(modules)
    ilm_policy = build_ilm_policy(validated_retention_days)
    kibana_saved_objects = build_kibana_saved_objects(validated_prefix)
    report_config = build_report_config(validated_prefix, discovery)
    paths = {
        "index_template": output_dir / "index-template.json",
        "ingest_pipeline": output_dir / "ingest-pipeline.json",
        "ilm_policy": output_dir / "ilm-policy.json",
        "report_config": output_dir / "report-config.json",
        "kibana_saved_objects_json": output_dir / "kibana-saved-objects.json",
        "kibana_saved_objects_ndjson": output_dir / "kibana-saved-objects.ndjson",
    }
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
