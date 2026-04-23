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
    parser.add_argument("--sampling-ratio", type=float, default=1.0, help="Probabilistic trace sampling ratio (0.0 to 1.0)")
    parser.add_argument("--send-queue-size", type=int, default=2048, help="Elasticsearch exporter sending queue size")
    parser.add_argument("--retry-initial-interval", default="1s", help="Collector exporter retry initial interval")
    parser.add_argument("--retry-max-interval", default="30s", help="Collector exporter retry max interval")
    parser.add_argument("--enable-filelog", action="store_true", help="Add filelog receiver for local agent log files")
    parser.add_argument("--filelog-path", default="/var/log/agent/*.log")
    parser.add_argument("--log-min-severity", default="", help="Minimum log severity to keep (e.g. WARN, ERROR). Empty = keep all.")
    return parser.parse_args()


def _yaml_scalar(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _build_base_topology(
    *,
    discovery: dict,
    es_url: str,
    validated_prefix: str,
    environment: str,
    service_name: str,
    credentials: tuple[str, str] | None,
    embed_credentials: bool,
    grpc_port: int,
    http_port: int,
    enable_filelog: bool,
    filelog_path: str,
) -> dict[str, str]:
    """Return the structural pieces of the Collector config that define *where*
    data flows (receivers → processors → exporters). These rarely change across
    environments."""
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

    spanmetrics_dimensions = "\n".join(
        f"      - name: {dimension}" for dimension in _normalize_spanmetrics_dimensions()
    )

    return {
        "resource_actions": resource_actions,
        "auth_lines": auth_lines,
        "events_alias": events_alias,
        "metrics_index": metrics_index,
        "filelog_block": filelog_block,
        "filelog_receiver_ref": filelog_receiver_ref,
        "spanmetrics_dimensions": spanmetrics_dimensions,
        "grpc_port": str(grpc_port),
        "http_port": str(http_port),
        "es_url": es_url,
    }


def _build_governance_overrides(
    *,
    sampling_ratio: float,
    send_queue_size: int,
    retry_initial_interval: str,
    retry_max_interval: str,
    telemetry_metrics_port: int,
    log_min_severity: str,
) -> dict[str, str]:
    """Return the operational tuning pieces that vary per environment or SLA
    tier: sampling, queue sizing, retry policy, telemetry port, log severity filter."""
    sampling_block = ""
    sampling_processor_ref = ""
    if 0.0 < sampling_ratio < 1.0:
        sampling_block = f"""
  probabilistic_sampler:
    sampling_percentage: {sampling_ratio * 100:.1f}
"""
        sampling_processor_ref = ", probabilistic_sampler"

    severity_filter_block = ""
    severity_filter_ref = ""
    if log_min_severity and log_min_severity.upper() != "TRACE":
        severity_filter_block = f"""
  filter/log_severity:
    error_mode: ignore
    logs:
      log_record:
        - 'severity_number < SEVERITY_NUMBER_{log_min_severity.upper()}'
"""
        severity_filter_ref = ", filter/log_severity"

    exporter_resilience_block = f"""
    sending_queue:
      enabled: true
      queue_size: {send_queue_size}
    retry_on_failure:
      enabled: true
      initial_interval: {retry_initial_interval}
      max_interval: {retry_max_interval}
      max_elapsed_time: 300s"""

    return {
        "sampling_block": sampling_block,
        "sampling_processor_ref": sampling_processor_ref,
        "severity_filter_block": severity_filter_block,
        "severity_filter_ref": severity_filter_ref,
        "exporter_resilience_block": exporter_resilience_block,
        "telemetry_metrics_port": str(telemetry_metrics_port),
    }


def _assemble_yaml(base: dict[str, str], gov: dict[str, str]) -> str:
    """Merge base topology and governance overrides into the final YAML string."""
    ctx = {**base, **gov}
    return f"""receivers:
  otlp:
    protocols:
      grpc:
        endpoint: "127.0.0.1:{ctx['grpc_port']}"
      http:
        endpoint: "127.0.0.1:{ctx['http_port']}"
        # Change to 0.0.0.0 only if external OTLP senders need network access.
{ctx['filelog_block']}
connectors:
  spanmetrics:
    dimensions:
{ctx['spanmetrics_dimensions']}

processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 512
  batch:
    send_batch_size: 1024
    timeout: 5s
  resource/runtime:
    attributes:
{ctx['resource_actions']}
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
{ctx['sampling_block']}{ctx['severity_filter_block']}
exporters:
  elasticsearch/events:
    endpoints: [{_yaml_scalar(ctx['es_url'])}]{ctx['auth_lines']}
    logs_index: {_yaml_scalar(ctx['events_alias'])}
    traces_index: {_yaml_scalar(ctx['events_alias'])}
    mapping:
      allowed_modes: [ecs]{ctx['exporter_resilience_block']}
  elasticsearch/metrics:
    endpoints: [{_yaml_scalar(ctx['es_url'])}]{ctx['auth_lines']}
    metrics_index: {_yaml_scalar(ctx['metrics_index'])}
    mapping:
      allowed_modes: [ecs]{ctx['exporter_resilience_block']}

service:
  telemetry:
    metrics:
      address: "127.0.0.1:{ctx['telemetry_metrics_port']}"
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, resource/runtime, transform/elastic_mapping, attributes/redact{ctx['sampling_processor_ref']}, batch]
      exporters: [elasticsearch/events, spanmetrics]
    logs:
      receivers: [otlp{ctx['filelog_receiver_ref']}]
      processors: [memory_limiter, resource/runtime, transform/elastic_mapping, attributes/redact{ctx['severity_filter_ref']}, batch]
      exporters: [elasticsearch/events]
    metrics:
      receivers: [otlp, spanmetrics]
      processors: [memory_limiter, resource/runtime, transform/elastic_mapping, batch]
      exporters: [elasticsearch/metrics]
"""


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
    send_queue_size: int = 2048,
    retry_initial_interval: str = "1s",
    retry_max_interval: str = "30s",
    enable_filelog: bool = False,
    filelog_path: str = "/var/log/agent/*.log",
    log_min_severity: str = "",
) -> str:
    validated_prefix = validate_index_prefix(index_prefix)
    credentials = validate_credential_pair(es_user, es_password)
    if not 0.0 <= sampling_ratio <= 1.0:
        raise SkillError(f"Sampling ratio must be between 0.0 and 1.0, got: {sampling_ratio}")
    if send_queue_size < 1:
        raise SkillError(f"Send queue size must be >= 1, got: {send_queue_size}")

    base = _build_base_topology(
        discovery=discovery,
        es_url=es_url,
        validated_prefix=validated_prefix,
        environment=environment,
        service_name=service_name,
        credentials=credentials,
        embed_credentials=embed_credentials,
        grpc_port=grpc_port,
        http_port=http_port,
        enable_filelog=enable_filelog,
        filelog_path=filelog_path,
    )
    gov = _build_governance_overrides(
        sampling_ratio=sampling_ratio,
        send_queue_size=send_queue_size,
        retry_initial_interval=retry_initial_interval,
        retry_max_interval=retry_max_interval,
        telemetry_metrics_port=telemetry_metrics_port,
        log_min_severity=log_min_severity,
    )
    return _assemble_yaml(base, gov)


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
            send_queue_size=args.send_queue_size,
            retry_initial_interval=args.retry_initial_interval,
            retry_max_interval=args.retry_max_interval,
            enable_filelog=args.enable_filelog,
            filelog_path=args.filelog_path,
            log_min_severity=args.log_min_severity,
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
