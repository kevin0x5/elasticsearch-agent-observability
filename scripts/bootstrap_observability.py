#!/usr/bin/env python3
"""Bootstrap agent observability assets."""

from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    SkillError,
    ensure_dir,
    print_error,
    validate_credential_pair,
    validate_index_prefix,
    validate_positive_int,
    validate_workspace_dir,
    write_json,
    write_text,
)
from discover_agent_architecture import discover_workspace
from render_collector_config import render_config
from render_es_assets import render_assets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap agent observability")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--es-user", default="")
    parser.add_argument("--es-password", default="")
    parser.add_argument("--embed-es-credentials", action="store_true", help="Embed Elasticsearch credentials into the generated Collector YAML")
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--service-name", default="agent-runtime")
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--max-files", type=int, default=400)
    return parser.parse_args()


def collect_summary_notes(discovery: dict, *, max_files: int, auth_mode: str, index_prefix: str) -> list[str]:
    notes: list[str] = []
    if discovery.get("files_scanned", 0) >= max_files:
        notes.append(
            f"Discovery reached the --max-files limit ({max_files}); some files may have been skipped and the monitoring plan may be incomplete."
        )
    if not discovery.get("detected_modules"):
        notes.append("No monitorable modules were detected; double-check the workspace path or review the discovery heuristics before applying assets.")
    if auth_mode == "env":
        notes.append(
            "Collector YAML uses `${env:ELASTICSEARCH_USERNAME}` and `${env:ELASTICSEARCH_PASSWORD}` placeholders; credentials were not written to disk."
        )
    elif auth_mode == "inline":
        notes.append("Collector YAML includes inline Elasticsearch credentials; treat the generated file as secret material.")
    notes.append(f"Logs and traces both write to `{index_prefix}-events`, which matches the generated rollover alias and index template.")
    return notes


def build_summary(discovery_path: Path, collector_path: Path, assets_paths: dict[str, str], notes: list[str]) -> str:
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
    if notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        args = parse_args()
        workspace = validate_workspace_dir(Path(args.workspace), "Workspace")
        output_dir = ensure_dir(Path(args.output_dir).expanduser().resolve())
        index_prefix = validate_index_prefix(args.index_prefix)
        retention_days = validate_positive_int(args.retention_days, "Retention days")
        max_files = validate_positive_int(args.max_files, "Max files")
        credentials = validate_credential_pair(args.es_user, args.es_password)
        auth_mode = "none"
        if credentials:
            auth_mode = "inline" if args.embed_es_credentials else "env"

        discovery = discover_workspace(workspace, max_files=max_files)
        discovery_path = output_dir / "discovery.json"
        write_json(discovery_path, discovery)

        collector_path = output_dir / "otel-collector.generated.yaml"
        collector_text = render_config(
            discovery,
            es_url=args.es_url,
            index_prefix=index_prefix,
            environment=args.environment,
            service_name=args.service_name,
            es_user=credentials[0] if credentials else "",
            es_password=credentials[1] if credentials else "",
            embed_credentials=args.embed_es_credentials,
        )
        write_text(collector_path, collector_text)

        assets_dir = output_dir / "elasticsearch"
        assets_paths = render_assets(
            discovery,
            assets_dir,
            index_prefix=index_prefix,
            retention_days=retention_days,
        )
        notes = collect_summary_notes(discovery, max_files=max_files, auth_mode=auth_mode, index_prefix=index_prefix)
        summary_path = output_dir / "bootstrap-summary.md"
        write_text(summary_path, build_summary(discovery_path, collector_path, assets_paths, notes))

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
