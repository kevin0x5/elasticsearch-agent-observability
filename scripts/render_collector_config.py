#!/usr/bin/env python3
"""Render OTel Collector config for agent observability."""

from __future__ import annotations

import argparse
from pathlib import Path

from common import SkillError, print_error, read_json, write_text


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
    return parser.parse_args()


def render_config(
    discovery: dict,
    *,
    es_url: str,
    index_prefix: str,
    environment: str,
    service_name: str,
    es_user: str = "",
    es_password: str = "",
) -> str:
    modules = ",".join(module["module_kind"] for module in discovery.get("detected_modules", [])[:12])
    resource_actions = "\n".join(
        [
            "      - key: service.name",
            f"        value: {service_name}",
            "        action: upsert",
            "      - key: deployment.environment",
            f"        value: {environment}",
            "        action: upsert",
            "      - key: agent.discovery.modules",
            f"        value: {modules or 'unknown'}",
            "        action: upsert",
            "      - key: observer.product",
            "        value: elasticsearch-agent-observability",
            "        action: upsert",
        ]
    )

    # Build auth block for elasticsearch exporter
    auth_lines = ""
    if es_user and es_password:
        auth_lines = f"""
    user: "{es_user}"
    password: "{es_password}" """

    return f"""receivers:
  otlp:
    protocols:
      grpc:
      http:

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
  attributes/redact:
    actions:
      - key: gen_ai.prompt
        action: delete
      - key: gen_ai.tool.call.arguments
        action: delete
      - key: gen_ai.tool.call.result
        action: delete

exporters:
  elasticsearch:
    endpoints: ["{es_url}"]{auth_lines}
    logs_index: {index_prefix}-events-default
    traces_index: {index_prefix}-spans-default

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, resource/runtime, attributes/redact, batch]
      exporters: [elasticsearch]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, resource/runtime, attributes/redact, batch]
      exporters: [elasticsearch]
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
