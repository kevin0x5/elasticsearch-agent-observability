#!/usr/bin/env python3
"""Render OTel Collector config for agent observability."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import (
    SkillError,
    build_data_stream_name,
    build_events_alias,
    print_error,
    read_json,
    validate_credential_pair,
    validate_index_prefix,
    write_text,
)


DEFAULT_ES_USER_ENV = "ELASTICSEARCH_USERNAME"
DEFAULT_ES_PASSWORD_ENV = "ELASTICSEARCH_PASSWORD"
DEFAULT_SPANMETRICS_DIMENSIONS = (
    "service.name",
    "gen_ai.tool.name",
    "gen_ai.request.model",
    "event.outcome",
)


def _normalize_spanmetrics_dimensions(dimensions: list[str] | tuple[str, ...] | None = None) -> list[str]:
    ordered = dimensions or list(DEFAULT_SPANMETRICS_DIMENSIONS)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in ordered:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render Collector config")
    parser.add_argument("--discovery", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--es-user", default="")
    parser.add_argument("--es-password", default="")
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--service-name", default="agent-runtime")
    parser.add_argument("--embed-es-credentials", action="store_true", help="Embed Elasticsearch credentials into the generated YAML")
    parser.add_argument("--grpc-port", type=int, default=4317)
    parser.add_argument("--http-port", type=int, default=4318)
    parser.add_argument("--telemetry-metrics-port", type=int, default=8888)
    parser.add_argument("--sampling-ratio", type=float, default=1.0, help="Probabilistic sampling ratio (0.0 to 1.0)")
    parser.add_argument("--enable-filelog", action="store_true", help="Add filelog receiver for local agent log files")
    parser.add_argument("--filelog-path", default="/var/log/agent/*.log")
    return parser.parse_args()


def _yaml_scalar(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def render_config(
    discovery: dict,
    *,
    es_url: str,
    index_prefix: str,
    environment: str,
    service_name: str,
    es_user: str = "",
    es_password: str = "",
    embed_credentials: bool = False,
    grpc_port: int = 4317,
    http_port: int = 4318,
    telemetry_metrics_port: int = 8888,
    sampling_ratio: float = 1.0,
    enable_filelog: bool = False,
    filelog_path: str = "/var/log/agent/*.log",
) -> str:
    validated_prefix = validate_index_prefix(index_prefix)
    credentials = validate_credential_pair(es_user, es_password)
    modules = ",".join(module["module_kind"] for module in discovery.get("detected_modules", [])[:12]) or "unknown"
    resource_actions = "\n".join(
        [
            "      - key: service.name",
            f"        value: {_yaml_scalar(service_name)}",
            "        action: upsert",
            "      - key: deployment.environment",
            f"        value: {_yaml_scalar(environment)}",
            "        action: upsert",
            "      - key: agent.discovery.modules",
            f"        value: {_yaml_scalar(modules)}",
            "        action: upsert",
            "      - key: observer.product",
            "        value: elasticsearch-agent-observability",
            "        action: upsert",
        ]
    )

    auth_lines = ""
    if credentials:
        if embed_credentials:
            auth_lines = (
                f"\n    user: {_yaml_scalar(credentials[0])}"
                f"\n    password: {_yaml_scalar(credentials[1])}"
            )
        else:
            auth_lines = (
                f"\n    user: {_yaml_scalar(f'${{env:{DEFAULT_ES_USER_ENV}}}') }"
                f"\n    password: {_yaml_scalar(f'${{env:{DEFAULT_ES_PASSWORD_ENV}}}') }"
            )

    events_alias = build_events_alias(validated_prefix)
    metrics_index = f"{validated_prefix}-metrics"

    filelog_block = ""
    filelog_receiver_ref = ""
    if enable_filelog:
        filelog_block = f"""
  filelog:
    include: [{_yaml_scalar(filelog_path)}]
    start_at: end
    operators:
      - type: json_parser
        if: 'body matches "^\\\\{{"'
"""
        filelog_receiver_ref = ", filelog"

    sampling_block = ""
    sampling_processor_ref = ""
    if 0.0 < sampling_ratio < 1.0:
        sampling_block = f"""
  probabilistic_sampler:
    sampling_percentage: {sampling_ratio * 100:.1f}
"""
        sampling_processor_ref = ", probabilistic_sampler"

    spanmetrics_dimensions = "\n".join(
        f"      - name: {dimension}" for dimension in _normalize_spanmetrics_dimensions()
    )

    return f"""receivers:
  otlp:
    protocols:
      grpc:
        endpoint: "127.0.0.1:{grpc_port}"
      http:
        endpoint: "127.0.0.1:{http_port}"
        # Change to 0.0.0.0 only if external OTLP senders need network access.
{filelog_block}
connectors:
  spanmetrics:
    dimensions:
{spanmetrics_dimensions}

processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 512
  batch:
    send_batch_size: 1024
    timeout: 5s
  resource/runtime:
    attributes:
{resource_actions}
  transform/elastic_mapping:
    error_mode: ignore
    log_statements:
      - context: scope
        statements:
          - set(attributes["elastic.mapping.mode"], "ecs")
    trace_statements:
      - context: scope
        statements:
          - set(attributes["elastic.mapping.mode"], "ecs")
    metric_statements:
      - context: scope
        statements:
          - set(attributes["elastic.mapping.mode"], "ecs")
  attributes/redact:
    actions:
      - key: gen_ai.prompt
        action: delete
      - key: gen_ai.completion
        action: delete
      - key: gen_ai.tool.call.arguments
        action: delete
      - key: gen_ai.tool.call.result
        action: delete
{sampling_block}
exporters:
  elasticsearch/events:
    endpoints: [{_yaml_scalar(es_url)}]{auth_lines}
    logs_index: {_yaml_scalar(events_alias)}
    traces_index: {_yaml_scalar(events_alias)}
    mapping:
      allowed_modes: [ecs]
  elasticsearch/metrics:
    endpoints: [{_yaml_scalar(es_url)}]{auth_lines}
    metrics_index: {_yaml_scalar(metrics_index)}
    mapping:
      allowed_modes: [ecs]

service:
  telemetry:
    metrics:
      address: "127.0.0.1:{telemetry_metrics_port}"
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, resource/runtime, transform/elastic_mapping, attributes/redact{sampling_processor_ref}, batch]
      exporters: [elasticsearch/events, spanmetrics]
    logs:
      receivers: [otlp{filelog_receiver_ref}]
      processors: [memory_limiter, resource/runtime, transform/elastic_mapping, attributes/redact, batch]
      exporters: [elasticsearch/events]
    metrics:
      receivers: [otlp, spanmetrics]
      processors: [memory_limiter, resource/runtime, transform/elastic_mapping, batch]
      exporters: [elasticsearch/metrics]
"""


def main() -> int:
    try:
        args = parse_args()
        discovery = read_json(Path(args.discovery).expanduser().resolve())
        output = Path(args.output).expanduser().resolve()
        rendered = render_config(
            discovery,
            es_url=args.es_url,
            index_prefix=args.index_prefix,
            environment=args.environment,
            service_name=args.service_name,
            es_user=args.es_user,
            es_password=args.es_password,
            embed_credentials=args.embed_es_credentials,
            grpc_port=args.grpc_port,
            http_port=args.http_port,
            telemetry_metrics_port=args.telemetry_metrics_port,
            sampling_ratio=args.sampling_ratio,
            enable_filelog=args.enable_filelog,
            filelog_path=args.filelog_path,
        )
        write_text(output, rendered)
        print(f"✅ collector config written: {output}")
        return 0
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Failed to render Collector config: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
