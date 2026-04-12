#!/usr/bin/env python3
"""Bootstrap agent observability assets."""

from __future__ import annotations

import argparse
from pathlib import Path

from common import SkillError, ensure_dir, print_error, write_json, write_text
from discover_agent_architecture import discover_workspace
from render_collector_config import render_config
from render_es_assets import render_assets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap agent observability")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--service-name", default="agent-runtime")
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--max-files", type=int, default=400)
    return parser.parse_args()


def build_summary(discovery_path: Path, collector_path: Path, assets_paths: dict[str, str]) -> str:
    lines = [
        "# Agent Observability Bootstrap Summary",
        "",
        f"- discovery: `{discovery_path}`",
        f"- collector config: `{collector_path}`",
        f"- index template: `{assets_paths['index_template']}`",
        f"- ingest pipeline: `{assets_paths['ingest_pipeline']}`",
        f"- ilm policy: `{assets_paths['ilm_policy']}`",
        f"- report config: `{assets_paths['report_config']}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        args = parse_args()
        workspace = Path(args.workspace).expanduser().resolve()
        output_dir = ensure_dir(Path(args.output_dir).expanduser().resolve())
        discovery = discover_workspace(workspace, max_files=args.max_files)
        discovery_path = output_dir / "discovery.json"
        write_json(discovery_path, discovery)

        collector_path = output_dir / "otel-collector.generated.yaml"
        collector_text = render_config(
            discovery,
            es_url=args.es_url,
            index_prefix=args.index_prefix,
            environment=args.environment,
            service_name=args.service_name,
        )
        write_text(collector_path, collector_text)

        assets_dir = output_dir / "elasticsearch"
        assets_paths = render_assets(
            discovery,
            assets_dir,
            index_prefix=args.index_prefix,
            retention_days=args.retention_days,
        )
        summary_path = output_dir / "bootstrap-summary.md"
        write_text(summary_path, build_summary(discovery_path, collector_path, assets_paths))

        print(f"✅ bootstrap complete: {output_dir}")
        print(f"   discovery: {discovery_path}")
        print(f"   collector: {collector_path}")
        print(f"   summary: {summary_path}")
        return 0
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Failed to bootstrap observability: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
