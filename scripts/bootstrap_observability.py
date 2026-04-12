#!/usr/bin/env python3
"""Bootstrap agent observability assets and optional Elasticsearch setup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from apply_elasticsearch_assets import apply_assets
from common import (
    ESConfig,
    SkillError,
    ensure_dir,
    es_request,
    print_error,
    validate_credential_pair,
    validate_index_prefix,
    validate_positive_int,
    validate_workspace_dir,
    write_json,
    write_text,
)
from discover_agent_architecture import discover_workspace
from generate_report import build_report, render_markdown, search_payload
from render_collector_config import render_config
from render_elastic_agent_assets import SUPPORTED_INGEST_MODES, render_assets as render_elastic_native_assets
from render_es_assets import render_assets

DEFAULT_OTLP_ENDPOINT = "http://127.0.0.1:4317"
DEFAULT_COLLECTOR_BIN = "otelcol"
DEFAULT_INGEST_MODE = "collector"


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
    parser.add_argument("--apply-es-assets", action="store_true", help="Apply generated Elasticsearch assets to the target cluster")
    parser.add_argument("--skip-bootstrap-index", action="store_true", help="Do not create the first rollover write index")
    parser.add_argument("--kibana-url", default="", help="Optional Kibana base URL for applying saved objects")
    parser.add_argument("--kibana-space", default="default")
    parser.add_argument("--apply-kibana-assets", action="store_true", help="Apply generated Kibana saved objects to the target Kibana instance")
    parser.add_argument("--report-output", help="Optional path for a generated markdown/json report")
    parser.add_argument("--report-format", choices=["markdown", "json"], help="Optional report output format override")
    parser.add_argument("--time-range", default="now-24h")
    parser.add_argument("--otlp-endpoint", default=DEFAULT_OTLP_ENDPOINT)
    parser.add_argument("--collector-bin", default=DEFAULT_COLLECTOR_BIN)
    parser.add_argument("--ingest-mode", choices=SUPPORTED_INGEST_MODES, default=DEFAULT_INGEST_MODE)
    parser.add_argument("--fleet-server-url", default="")
    parser.add_argument("--fleet-enrollment-token", default="")
    parser.add_argument("--apm-server-url", default="")
    return parser.parse_args()


def collect_summary_notes(
    discovery: dict[str, Any],
    *,
    max_files: int,
    auth_mode: str,
    index_prefix: str,
    ingest_mode: str,
    apply_kibana_assets: bool = False,
    has_elastic_native_bundle: bool = False,
) -> list[str]:
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
    recommendations = discovery.get("recommended_ingest_modes", [])
    if recommendations:
        preview = ", ".join(f"{item['mode']}({item['score']})" for item in recommendations[:3] if item.get("mode"))
        notes.append(f"Discovery recommended ingest modes: {preview}.")
    notes.append(f"Selected ingest mode: `{ingest_mode}`.")
    if has_elastic_native_bundle:
        notes.append("Elastic-native starter assets were rendered, including Elastic Agent / Fleet / APM bootstrap files for operator review.")
    if apply_kibana_assets:
        notes.append("Kibana saved objects are part of the generated asset surface, so the human-facing report path can land in Kibana instead of living only in Markdown.")
    return notes


def build_runtime_env(*, service_name: str, environment: str, otlp_endpoint: str, apm_server_url: str = "") -> str:
    return "\n".join(
        [
            "# Agent OTLP runtime defaults",
            f"OTEL_EXPORTER_OTLP_ENDPOINT={otlp_endpoint}",
            "OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
            f"OTEL_SERVICE_NAME={service_name}",
            f"OTEL_RESOURCE_ATTRIBUTES=deployment.environment={environment},observer.product=elasticsearch-agent-observability",
            f"ELASTIC_APM_SERVER_URL={apm_server_url}",
            "# Fill these only if your Collector exporter uses env placeholders.",
            "ELASTICSEARCH_USERNAME=",
            "ELASTICSEARCH_PASSWORD=",
            "",
        ]
    )


def build_collector_run_script(*, collector_bin: str, collector_path: Path, env_path: Path) -> str:
    collector_name = collector_path.name
    env_name = env_path.name
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            "set -a",
            f'source "$SCRIPT_DIR/{env_name}"',
            "set +a",
            f': "${{OTELCOL_BIN:={collector_bin}}}"',
            f'exec "${{OTELCOL_BIN}}" --config "$SCRIPT_DIR/{collector_name}"',
            "",
        ]
    )


def build_summary(
    *,
    discovery_path: Path,
    assets_paths: dict[str, str],
    notes: list[str],
    ingest_mode: str,
    collector_path: Path | None,
    env_path: Path | None,
    collector_run_path: Path | None,
    native_assets_paths: dict[str, str] | None,
    apply_summary_path: Path | None,
    report_output: Path | None,
) -> str:
    lines = [
        "# Agent Observability Bootstrap Summary",
        "",
        f"- discovery: `{discovery_path}`",
        f"- ingest mode: `{ingest_mode}`",
        f"- index template: `{assets_paths['index_template']}`",
        f"- ingest pipeline: `{assets_paths['ingest_pipeline']}`",
        f"- ilm policy: `{assets_paths['ilm_policy']}`",
        f"- report config: `{assets_paths['report_config']}`",
        f"- kibana bundle (json): `{assets_paths['kibana_saved_objects_json']}`",
        f"- kibana bundle (ndjson): `{assets_paths['kibana_saved_objects_ndjson']}`",
    ]
    if collector_path and env_path and collector_run_path:
        lines.extend(
            [
                f"- collector config: `{collector_path}`",
                f"- collector launcher: `{collector_run_path}`",
                f"- agent OTLP env: `{env_path}`",
            ]
        )
    if native_assets_paths:
        lines.extend(
            [
                f"- elastic-native policy: `{native_assets_paths['policy']}`",
                f"- elastic-native env: `{native_assets_paths['env']}`",
                f"- elastic-native launcher: `{native_assets_paths['launcher']}`",
                f"- elastic-native readme: `{native_assets_paths['readme']}`",
            ]
        )
    if apply_summary_path:
        lines.append(f"- apply summary: `{apply_summary_path}`")
    if report_output:
        lines.append(f"- smoke report: `{report_output}`")
    if notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines) + "\n"


def write_report(*, es_config: ESConfig, report_config_path: Path, output: Path, time_range: str, output_format: str | None) -> Path:
    report_config = json.loads(Path(report_config_path).read_text(encoding="utf-8"))
    events_alias = str(report_config.get("events_alias", "")).strip()
    time_field = str(report_config.get("time_field") or "captured_at").strip() or "captured_at"
    result = es_request(es_config, "POST", f"/{events_alias}/_search", search_payload(time_range, time_field=time_field))
    report = build_report(result)
    resolved_output = output.expanduser().resolve()
    format_name = output_format or ("json" if resolved_output.suffix.lower() == ".json" else "markdown")
    if format_name == "json":
        write_json(resolved_output, report)
    else:
        write_text(resolved_output, render_markdown(report, {**report_config, "time_range": time_range, "events_alias": events_alias, "time_field": time_field}))
    return resolved_output


def main() -> int:
    try:
        args = parse_args()
        if args.apply_kibana_assets and not args.kibana_url.strip():
            raise SkillError("--kibana-url is required when --apply-kibana-assets is enabled")
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

        collector_path: Path | None = None
        env_path: Path | None = None
        collector_run_path: Path | None = None
        if args.ingest_mode in {"collector", "apm-otlp-hybrid"}:
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
            env_path = output_dir / "agent-otel.env"
            write_text(
                env_path,
                build_runtime_env(
                    service_name=args.service_name,
                    environment=args.environment,
                    otlp_endpoint=args.otlp_endpoint,
                    apm_server_url=args.apm_server_url,
                ),
            )
            collector_run_path = output_dir / "run-collector.sh"
            write_text(
                collector_run_path,
                build_collector_run_script(collector_bin=args.collector_bin, collector_path=collector_path, env_path=env_path),
            )
            collector_run_path.chmod(0o755)

        native_assets_paths = None
        if args.ingest_mode in {"elastic-agent-fleet", "apm-otlp-hybrid"}:
            native_dir = output_dir / "elastic-native"
            native_assets_paths = render_elastic_native_assets(
                discovery,
                native_dir,
                ingest_mode=args.ingest_mode,
                index_prefix=index_prefix,
                service_name=args.service_name,
                environment=args.environment,
                fleet_server_url=args.fleet_server_url,
                fleet_enrollment_token=args.fleet_enrollment_token,
                apm_server_url=args.apm_server_url,
                kibana_url=args.kibana_url,
                otlp_endpoint=args.otlp_endpoint,
            )

        assets_dir = output_dir / "elasticsearch"
        assets_paths = render_assets(
            discovery,
            assets_dir,
            index_prefix=index_prefix,
            retention_days=retention_days,
        )

        apply_summary_path = None
        es_config = ESConfig(
            es_url=args.es_url,
            es_user=credentials[0] if credentials else None,
            es_password=credentials[1] if credentials else None,
        )
        if args.apply_es_assets or args.apply_kibana_assets:
            apply_summary = apply_assets(
                es_config,
                assets_dir=assets_dir,
                index_prefix=index_prefix,
                bootstrap_index=not args.skip_bootstrap_index,
                kibana_url=args.kibana_url.strip() or None,
                kibana_space=args.kibana_space,
                apply_kibana=args.apply_kibana_assets,
            )
            apply_summary_path = assets_dir / "apply-summary.json"
            write_json(apply_summary_path, apply_summary)

        report_output_arg = args.report_output
        if not report_output_arg and args.apply_es_assets:
            report_output_arg = str(output_dir / "report.md")
        report_output_path = None
        if report_output_arg:
            report_output_path = write_report(
                es_config=es_config,
                report_config_path=Path(assets_paths["report_config"]),
                output=Path(report_output_arg),
                time_range=args.time_range,
                output_format=args.report_format,
            )

        notes = collect_summary_notes(
            discovery,
            max_files=max_files,
            auth_mode=auth_mode,
            index_prefix=index_prefix,
            ingest_mode=args.ingest_mode,
            apply_kibana_assets=args.apply_kibana_assets,
            has_elastic_native_bundle=bool(native_assets_paths),
        )
        if apply_summary_path:
            notes.append("Elasticsearch assets were applied to the target cluster, including template, pipeline, ILM policy, and optional write-index bootstrap.")
        if args.apply_kibana_assets:
            notes.append("Kibana saved objects were applied, so the default human-facing observability surface now lives in Kibana dashboards / Discover entrypoints.")
        if report_output_path:
            notes.append("A markdown/json smoke report was also generated so you can validate the query path before opening Kibana.")
        if collector_run_path and env_path:
            notes.append("Use `run-collector.sh` to start the Collector and `agent-otel.env` as the runtime env template for the agent process.")
        if native_assets_paths:
            notes.append("Use the `elastic-native` bundle when the operator prefers Fleet enrollment or APM/OTLP hybrid wiring instead of Collector-only mode.")

        summary_path = output_dir / "bootstrap-summary.md"
        write_text(
            summary_path,
            build_summary(
                discovery_path=discovery_path,
                assets_paths=assets_paths,
                notes=notes,
                ingest_mode=args.ingest_mode,
                collector_path=collector_path,
                env_path=env_path,
                collector_run_path=collector_run_path,
                native_assets_paths=native_assets_paths,
                apply_summary_path=apply_summary_path,
                report_output=report_output_path,
            ),
        )

        print(f"✅ bootstrap complete: {output_dir}")
        print(f"   discovery: {discovery_path}")
        if collector_path:
            print(f"   collector: {collector_path}")
        if collector_run_path:
            print(f"   launcher: {collector_run_path}")
        if native_assets_paths:
            print(f"   elastic-native policy: {native_assets_paths['policy']}")
        print(f"   kibana bundle: {assets_paths['kibana_saved_objects_ndjson']}")
        if apply_summary_path:
            print(f"   apply summary: {apply_summary_path}")
        if report_output_path:
            print(f"   smoke report: {report_output_path}")
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
