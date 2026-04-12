#!/usr/bin/env python3
"""Render Elasticsearch 9.x assets for agent observability."""

from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    SkillError,
    build_events_alias,
    ensure_dir,
    print_error,
    read_json,
    validate_index_prefix,
    validate_positive_int,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render Elasticsearch assets")
    parser.add_argument("--discovery", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--retention-days", type=int, default=30)
    return parser.parse_args()


def build_index_template(index_prefix: str, modules: list[str]) -> dict:
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
                },
            },
        },
        "_meta": {
            "product": "elasticsearch-agent-observability",
            "recommended_modules": modules,
        },
    }


def build_ingest_pipeline(modules: list[str]) -> dict:
    return {
        "description": "Normalize agent observability events and apply light redaction",
        "processors": [
            {"set": {"field": "observer.product", "value": "elasticsearch-agent-observability"}},
            {"set": {"field": "labels.recommended_modules", "value": modules}},
            {"remove": {"field": "gen_ai.prompt", "ignore_missing": True}},
            {"remove": {"field": "gen_ai.tool.call.arguments", "ignore_missing": True}},
            {"remove": {"field": "gen_ai.tool.call.result", "ignore_missing": True}},
        ],
        "on_failure": [
            {"set": {"field": "observer.ingest_error", "value": "{{ _ingest.on_failure_message }}"}}
        ],
    }


def build_ilm_policy(retention_days: int) -> dict:
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


def build_report_config(index_prefix: str, discovery: dict) -> dict:
    modules = sorted({module["module_kind"] for module in discovery.get("detected_modules", [])})
    return {
        "time_range": "now-24h",
        "index_prefix": index_prefix,
        "events_alias": build_events_alias(index_prefix),
        "recommended_modules": modules,
        "metrics": [
            "success_rate",
            "p50_latency_ms",
            "p95_latency_ms",
            "tool_error_rate",
            "retry_breakdown",
            "token_totals",
            "cost_totals",
            "top_tools",
            "top_models",
            "mcp_method_breakdown",
        ],
    }


def render_assets(discovery: dict, output_dir: Path, *, index_prefix: str, retention_days: int) -> dict:
    ensure_dir(output_dir)
    validated_prefix = validate_index_prefix(index_prefix)
    validated_retention_days = validate_positive_int(retention_days, "Retention days")
    modules = sorted({module["module_kind"] for module in discovery.get("detected_modules", [])})
    index_template = build_index_template(validated_prefix, modules)
    ingest_pipeline = build_ingest_pipeline(modules)
    ilm_policy = build_ilm_policy(validated_retention_days)
    report_config = build_report_config(validated_prefix, discovery)
    paths = {
        "index_template": output_dir / "index-template.json",
        "ingest_pipeline": output_dir / "ingest-pipeline.json",
        "ilm_policy": output_dir / "ilm-policy.json",
        "report_config": output_dir / "report-config.json",
    }
    write_json(paths["index_template"], index_template)
    write_json(paths["ingest_pipeline"], ingest_pipeline)
    write_json(paths["ilm_policy"], ilm_policy)
    write_json(paths["report_config"], report_config)
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
