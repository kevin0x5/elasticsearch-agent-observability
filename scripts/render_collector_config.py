#!/usr/bin/env python3
"""Render OTel Collector config for agent observability."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import (
    SkillError,
    build_events_alias,
    print_error,
    read_json,
    validate_credential_pair,
    validate_index_prefix,
    write_text,
)


DEFAULT_ES_USER_ENV = "ELASTICSEARCH_USERNAME"
DEFAULT_ES_PASSWORD_ENV = "ELASTICSEARCH_PASSWORD"


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
    endpoints: [{_yaml_scalar(es_url)}]{auth_lines}
    logs_index: {_yaml_scalar(events_alias)}
    traces_index: {_yaml_scalar(events_alias)}

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
            embed_credentials=args.embed_es_credentials,
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
