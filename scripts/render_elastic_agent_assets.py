#!/usr/bin/env python3
"""Render Elastic-native starter assets for agent observability."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from common import (
    SkillError,
    ensure_dir,
    print_error,
    read_json,
    validate_index_prefix,
    write_json,
    write_text,
)

SUPPORTED_INGEST_MODES = ("collector", "elastic-agent-fleet", "apm-otlp-hybrid")
DEFAULT_APM_SERVER_URL = "https://apm.example.com:8200"
DEFAULT_KIBANA_URL = "https://kibana.example.com"
DEFAULT_RUM_SERVICE_VERSION = "0.1.0"
# Placeholder for rum-config.json; real origins should be replaced by the operator.
DEFAULT_DISTRIBUTED_TRACING_ORIGINS_JSON = ["https://your-app-origin.example.com"]
# JS expression for the generated snippet (evaluated at runtime).
DEFAULT_DISTRIBUTED_TRACING_ORIGINS_JS = "window.location.origin"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render Elastic Agent / Fleet starter assets"
    )
    parser.add_argument("--discovery", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--ingest-mode",
        choices=SUPPORTED_INGEST_MODES,
        default="elastic-agent-fleet",
    )
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--service-name", default="agent-runtime")
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--fleet-server-url", default="")
    parser.add_argument("--fleet-enrollment-token", default="")
    parser.add_argument("--apm-server-url", default="")
    parser.add_argument("--kibana-url", default="")
    parser.add_argument("--otlp-endpoint", default="http://127.0.0.1:4317")
    return parser.parse_args()


def _module_kind_set(discovery: dict[str, Any]) -> set[str]:
    return {
        str(module.get("module_kind"))
        for module in discovery.get("detected_modules", [])
        if module.get("module_kind")
    }


def _apm_server_hint(apm_server_url: str) -> str:
    return apm_server_url or DEFAULT_APM_SERVER_URL


def _kibana_hint(kibana_url: str) -> str:
    return kibana_url or DEFAULT_KIBANA_URL


def _verify_server_cert(server_url: str) -> str:
    return "false" if server_url.startswith("http://") else "true"


def _rum_service_name(service_name: str) -> str:
    return f"{service_name}-web"


def _build_preflight_check(
    *,
    key: str,
    label: str,
    required: bool,
    status: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "required": required,
        "status": status,
        "detail": detail,
    }


def _compute_preflight_status(checks: list[dict[str, Any]]) -> str:
    required_checks = [check for check in checks if check.get("required")]
    if any(check.get("status") == "failed" for check in required_checks):
        return "failed"
    if any(check.get("status") == "action_required" for check in required_checks):
        return "action_required"
    return "ready"


def build_policy(
    discovery: dict[str, Any],
    *,
    index_prefix: str,
    service_name: str,
    environment: str,
    ingest_mode: str,
) -> dict[str, Any]:
    module_kinds = _module_kind_set(discovery)
    integrations = [
        {
            "name": "system",
            "package": "system",
            "reason": "Baseline host + process telemetry for the agent runtime host.",
        },
        {
            "name": "custom_logs",
            "package": "logfile",
            "reason": "Catch CLI / runtime logs when the agent does not emit OTLP everywhere.",
        },
    ]
    if module_kinds & {
        "model_adapter",
        "tool_registry",
        "mcp_surface",
        "runtime_entrypoint",
        "web_service",
    }:
        integrations.append(
            {
                "name": "elastic_apm",
                "package": "apm",
                "reason": (
                    "Reuse Elastic APM for transactions, spans, service maps, and "
                    "trace waterfalls instead of rebuilding those surfaces as custom dashboards."
                ),
            }
        )
    if ingest_mode == "apm-otlp-hybrid":
        integrations.append(
            {
                "name": "otlp_bridge",
                "package": "otlp",
                "reason": (
                    "Keep OTLP ingress open for agents already instrumented with "
                    "OpenTelemetry while still leaning on Elastic-native APM analysis."
                ),
            }
        )
    if ingest_mode in {"elastic-agent-fleet", "apm-otlp-hybrid"}:
        integrations.append(
            {
                "name": "universal_profiling",
                "package": "profiling",
                "reason": (
                    "Prepare continuous host-level performance profiling so slow "
                    "transactions can be correlated with CPU-heavy stack traces."
                ),
            }
        )

    has_browser_frontend = "browser_frontend" in module_kinds
    rum_service_name = _rum_service_name(service_name)

    return {
        "name": f"{service_name}-{ingest_mode}",
        "namespace": environment,
        "description": (
            "Starter Elastic-native policy generated by "
            "elasticsearch-agent-observability."
        ),
        "monitoring_enabled": ["logs", "metrics", "traces"],
        "index_prefix": validate_index_prefix(index_prefix),
        "service_name": service_name,
        "recommended_signals": discovery.get("recommended_signals", []),
        "recommended_modules": sorted(module_kinds),
        "integrations": integrations,
        "experience_surfaces": [
            "apm",
            "traces",
            "service-map",
            "user-experience",
            "profiling",
        ],
        "browser_monitoring": {
            "enabled": has_browser_frontend,
            "service_name": rum_service_name,
            "distributed_tracing_origins": DEFAULT_DISTRIBUTED_TRACING_ORIGINS_JSON,
            "reason": (
                "Frontend-like files were detected; prefer Elastic RUM + APM trace "
                "correlation instead of inventing a separate browser telemetry path."
                if has_browser_frontend
                else "No browser frontend was detected from discovery; keep RUM as an optional extension."
            ),
        },
        "analysis_contract": {
            "backend_service_name": service_name,
            "frontend_service_name": rum_service_name,
            "environment": environment,
            "required_identity_fields": [
                "service.name",
                "deployment.environment",
                "trace.id",
                "span.id",
            ],
            "preferred_native_apps": [
                "apm-services",
                "apm-traces",
                "apm-service-map",
                "observability-user-experience",
                "universal-profiling",
            ],
        },
    }


def build_env_template(
    *,
    fleet_server_url: str,
    fleet_enrollment_token: str,
    apm_server_url: str,
    otlp_endpoint: str,
    service_name: str,
    environment: str,
) -> str:
    server_hint = _apm_server_hint(apm_server_url)
    rum_service_name = _rum_service_name(service_name)
    verify_server_cert = _verify_server_cert(server_hint)
    return "\n".join(
        [
            "# Elastic-native runtime defaults",
            f"FLEET_URL={fleet_server_url}",
            f"FLEET_ENROLLMENT_TOKEN={fleet_enrollment_token}",
            f"ELASTIC_APM_SERVER_URL={server_hint}",
            "ELASTIC_APM_SECRET_TOKEN=",
            "ELASTIC_APM_API_KEY=",
            f"ELASTIC_APM_SERVICE_NAME={service_name}",
            f"ELASTIC_APM_ENVIRONMENT={environment}",
            "ELASTIC_APM_TRANSACTION_SAMPLE_RATE=1.0",
            "ELASTIC_APM_BREAKDOWN_METRICS=true",
            "ELASTIC_APM_CAPTURE_BODY=off",
            "ELASTIC_APM_CAPTURE_HEADERS=false",
            f"ELASTIC_APM_VERIFY_SERVER_CERT={verify_server_cert}",
            f"OTEL_EXPORTER_OTLP_ENDPOINT={otlp_endpoint}",
            "OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
            f"OTEL_SERVICE_NAME={service_name}",
            (
                "OTEL_RESOURCE_ATTRIBUTES="
                f"deployment.environment={environment},"
                "observer.product=elasticsearch-agent-observability"
            ),
            f"RUM_APM_SERVER_URL={server_hint}",
            f"RUM_SERVICE_NAME={rum_service_name}",
            f"RUM_ENVIRONMENT={environment}",
            f"RUM_SERVICE_VERSION={DEFAULT_RUM_SERVICE_VERSION}",
            "RUM_TRANSACTION_SAMPLE_RATE=1.0",
            "RUM_DISTRIBUTED_TRACING_ORIGINS=https://your-app-origin.example.com",
            "# Replace with your actual origin(s); for JS snippet use window.location.origin",
            "ELASTIC_AGENT_TAGS=agent-observability,elastic-native",
            "",
        ]
    )


def build_apm_agent_env(
    *,
    apm_server_url: str,
    otlp_endpoint: str,
    service_name: str,
    environment: str,
) -> str:
    server_hint = _apm_server_hint(apm_server_url)
    verify_server_cert = _verify_server_cert(server_hint)
    return "\n".join(
        [
            "# Elastic APM / tracing starter defaults",
            f"ELASTIC_APM_SERVER_URL={server_hint}",
            "ELASTIC_APM_SECRET_TOKEN=",
            "ELASTIC_APM_API_KEY=",
            f"ELASTIC_APM_SERVICE_NAME={service_name}",
            f"ELASTIC_APM_ENVIRONMENT={environment}",
            "ELASTIC_APM_TRANSACTION_SAMPLE_RATE=1.0",
            "ELASTIC_APM_BREAKDOWN_METRICS=true",
            "ELASTIC_APM_CAPTURE_BODY=off",
            "ELASTIC_APM_CAPTURE_HEADERS=false",
            f"ELASTIC_APM_VERIFY_SERVER_CERT={verify_server_cert}",
            f"OTEL_EXPORTER_OTLP_ENDPOINT={otlp_endpoint}",
            "OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
            "",
        ]
    )


def build_run_script(*, ingest_mode: str, env_path: Path) -> str:
    env_name = env_path.name
    if ingest_mode == "elastic-agent-fleet":
        command = (
            'exec "${ELASTIC_AGENT_BIN:-elastic-agent}" enroll '
            '--url "${FLEET_URL}" --enrollment-token "${FLEET_ENROLLMENT_TOKEN}" '
            "--non-interactive"
        )
    else:
        command = (
            'exec "${ELASTIC_AGENT_BIN:-elastic-agent}" run '
            '--path.config "$SCRIPT_DIR" --path.home "$SCRIPT_DIR/.elastic-agent-home"'
        )
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            "set -a",
            f'source "$SCRIPT_DIR/{env_name}"',
            "set +a",
            command,
            "",
        ]
    )


def build_bootstrap_readme(
    *,
    ingest_mode: str,
    kibana_url: str,
    fleet_server_url: str,
    apm_server_url: str,
) -> str:
    lines = [
        "# Elastic-native bootstrap",
        "",
        f"- ingest_mode: `{ingest_mode}`",
        f"- fleet_server_url: `{fleet_server_url or 'set in elastic-agent.env'}`",
        f"- apm_server_url: `{_apm_server_hint(apm_server_url)}`",
        f"- kibana_url: `{_kibana_hint(kibana_url)}`",
        "",
        "## What this bundle is",
        "",
        "- a starter Elastic Agent / Fleet policy skeleton",
        "- an env template for enrollment plus APM / OTLP / RUM contract wiring",
        "- a launcher script for local bootstrap or operator review",
        "- a machine-readable surface manifest for Elastic native observability apps",
        "- a machine-readable preflight checklist for Kibana / Fleet / APM rollout prerequisites",
        "- a trace analysis playbook that points operators at APM / Traces / Service Map first",
        "- a browser RUM config and starter snippet for user-experience monitoring",
        "- a profiling rollout checklist for host-level performance analysis",
        "",
        "## Important boundary",
        "",
        (
            "This bundle intentionally reuses Elastic-native surfaces instead of "
            "rebuilding trace analysis in custom dashboards. It still does not "
            "auto-call Fleet APIs, auto-enroll hosts, auto-install Universal "
            "Profiling, or rewrite browser entrypoints for you."
        ),
        "",
        (
            "Use it to shorten the operator path to Kibana APM / Service Map / "
            "User Experience / Profiling, not to pretend those surfaces are already live."
        ),
    ]
    return "\n".join(lines) + "\n"


def build_surface_manifest(
    *,
    service_name: str,
    environment: str,
    apm_server_url: str,
    kibana_url: str,
    ingest_mode: str,
) -> dict[str, Any]:
    kibana_hint = _kibana_hint(kibana_url)
    rum_service_name = _rum_service_name(service_name)
    return {
        "product": "elasticsearch-agent-observability",
        "ingest_mode": ingest_mode,
        "apm_server_url": _apm_server_hint(apm_server_url),
        "services": {
            "backend": service_name,
            "frontend": rum_service_name,
            "environment": environment,
        },
        "kibana_apps": {
            "services": f"{kibana_hint}/app/apm/services",
            "traces": f"{kibana_hint}/app/apm/traces",
            "service_map": f"{kibana_hint}/app/apm/service-map",
            "user_experience": f"{kibana_hint}/app/ux",
            "profiling": "Open Universal Profiling from the Observability navigation.",
        },
        "correlation_contract": {
            "backend_service_name": service_name,
            "frontend_service_name": rum_service_name,
            "environment": environment,
            "distributed_tracing_origins": DEFAULT_DISTRIBUTED_TRACING_ORIGINS_JSON,
            "required_fields": [
                "service.name",
                "deployment.environment",
                "trace.id",
                "span.id",
            ],
        },
        "validation_flow": [
            "Validate backend transactions and spans in Kibana APM first.",
            "Validate dependency edges and upstream/downstream shape in Service Map.",
            "Validate page loads, route changes, and JS errors in User Experience after RUM ships.",
            "Use Universal Profiling for CPU-heavy paths after trace hotspots are visible.",
        ],
    }


def build_preflight_manifest(
    discovery: dict[str, Any],
    *,
    ingest_mode: str,
    service_name: str,
    environment: str,
    fleet_server_url: str,
    fleet_enrollment_token: str,
    apm_server_url: str,
    kibana_url: str,
    otlp_endpoint: str,
    surface_manifest: dict[str, Any],
) -> dict[str, Any]:
    module_kinds = _module_kind_set(discovery)
    has_browser_frontend = "browser_frontend" in module_kinds
    checks = [
        _build_preflight_check(
            key="kibana_url",
            label="Kibana base URL",
            required=True,
            status="ready" if kibana_url.strip() else "action_required",
            detail=(
                f"Kibana base URL ready: `{kibana_url.strip()}`."
                if kibana_url.strip()
                else "Set `--kibana-url` so the native bundle points to real APM / Traces / Service Map / UX entrypoints."
            ),
        )
    ]

    if ingest_mode == "elastic-agent-fleet":
        checks.extend(
            [
                _build_preflight_check(
                    key="fleet_server_url",
                    label="Fleet Server URL",
                    required=True,
                    status="ready" if fleet_server_url.strip() else "action_required",
                    detail=(
                        f"Fleet Server URL ready: `{fleet_server_url.strip()}`."
                        if fleet_server_url.strip()
                        else "Set `--fleet-server-url` before attempting Elastic Agent enrollment."
                    ),
                ),
                _build_preflight_check(
                    key="fleet_enrollment_token",
                    label="Fleet enrollment token",
                    required=True,
                    status="ready" if fleet_enrollment_token.strip() else "action_required",
                    detail=(
                        "Fleet enrollment token provided."
                        if fleet_enrollment_token.strip()
                        else "Provide `--fleet-enrollment-token` before using the generated Fleet enrollment launcher."
                    ),
                ),
            ]
        )
    else:
        checks.extend(
            [
                _build_preflight_check(
                    key="fleet_server_url",
                    label="Fleet Server URL",
                    required=False,
                    status="skipped",
                    detail="Fleet enrollment is only required for `elastic-agent-fleet` mode.",
                ),
                _build_preflight_check(
                    key="fleet_enrollment_token",
                    label="Fleet enrollment token",
                    required=False,
                    status="skipped",
                    detail="Fleet enrollment token is only required for `elastic-agent-fleet` mode.",
                ),
            ]
        )

    if ingest_mode == "apm-otlp-hybrid":
        checks.extend(
            [
                _build_preflight_check(
                    key="apm_server_url",
                    label="APM Server URL",
                    required=True,
                    status="ready" if apm_server_url.strip() else "action_required",
                    detail=(
                        f"APM Server URL ready: `{apm_server_url.strip()}`."
                        if apm_server_url.strip()
                        else "Set `--apm-server-url` so the hybrid path lands in Elastic APM semantics instead of an unresolved placeholder."
                    ),
                ),
                _build_preflight_check(
                    key="otlp_endpoint",
                    label="OTLP endpoint",
                    required=True,
                    status="ready" if otlp_endpoint.strip() else "action_required",
                    detail=(
                        f"OTLP endpoint ready: `{otlp_endpoint.strip()}`."
                        if otlp_endpoint.strip()
                        else "Set `--otlp-endpoint` for the OTLP side of `apm-otlp-hybrid`."
                    ),
                ),
            ]
        )
    else:
        checks.extend(
            [
                _build_preflight_check(
                    key="apm_server_url",
                    label="APM Server URL",
                    required=False,
                    status="ready" if apm_server_url.strip() else "skipped",
                    detail=(
                        f"APM Server URL ready: `{apm_server_url.strip()}`."
                        if apm_server_url.strip()
                        else "Direct APM server URL is optional here; Fleet policy or later operator wiring may provide it."
                    ),
                ),
                _build_preflight_check(
                    key="otlp_endpoint",
                    label="OTLP endpoint",
                    required=False,
                    status="ready" if otlp_endpoint.strip() else "skipped",
                    detail=(
                        f"OTLP endpoint ready: `{otlp_endpoint.strip()}`."
                        if otlp_endpoint.strip()
                        else "OTLP endpoint is only required when keeping an OTLP sidecar path alongside Elastic-native apps."
                    ),
                ),
            ]
        )

    checks.append(
        _build_preflight_check(
            key="rum_distributed_tracing_origins",
            label="Browser distributed tracing origins",
            required=has_browser_frontend,
            status="action_required" if has_browser_frontend else "skipped",
            detail=(
                "Replace the placeholder `RUM_DISTRIBUTED_TRACING_ORIGINS` value with the real browser/API origins before shipping the RUM snippet."
                if has_browser_frontend
                else "No browser frontend detected, so RUM origin wiring is optional."
            ),
        )
    )

    action_required = [check for check in checks if check.get("status") == "action_required"]
    next_steps = [check["detail"] for check in action_required]
    if not next_steps:
        next_steps.append("Proceed to runtime rollout, then validate the native Kibana apps in the order listed by `surface-manifest.json`.")

    return {
        "product": "elasticsearch-agent-observability",
        "ingest_mode": ingest_mode,
        "service_name": service_name,
        "environment": environment,
        "overall_status": _compute_preflight_status(checks),
        "action_required_count": len(action_required),
        "checks": checks,
        "native_apps": surface_manifest.get("kibana_apps", {}),
        "next_steps": next_steps,
    }


def build_apm_entrypoints_readme(
    *,
    service_name: str,
    environment: str,
    apm_server_url: str,
    otlp_endpoint: str,
    kibana_url: str,
    ingest_mode: str,
) -> str:
    kibana_hint = _kibana_hint(kibana_url)
    server_hint = _apm_server_hint(apm_server_url)
    rum_service_name = _rum_service_name(service_name)
    return "\n".join(
        [
            "# APM / trace analysis playbook",
            "",
            "## Goal",
            "",
            (
                "Use Elastic APM, Traces, and Service Map for performance analysis "
                "before creating any extra trace dashboard."
            ),
            "",
            "## Native Kibana surfaces",
            "",
            f"- Services: `{kibana_hint}/app/apm/services`",
            f"- Traces: `{kibana_hint}/app/apm/traces`",
            f"- Dependencies / Service Map: `{kibana_hint}/app/apm/service-map`",
            f"- User Experience: `{kibana_hint}/app/ux`",
            "",
            "## Correlation contract",
            "",
            f"- backend `service.name` = `{service_name}`",
            f"- frontend `service.name` = `{rum_service_name}` when RUM is enabled",
            f"- `deployment.environment` / `environment` = `{environment}`",
            f"- send traces to `{server_hint}` or keep OTLP on `{otlp_endpoint}` only when that path lands in Elastic APM semantics",
            "- keep W3C trace propagation intact so APM and RUM land in the same distributed trace tree",
            "",
            "## Recommended path",
            "",
            "1. Start with `apm-agent.env` so service identity and sampling are explicit.",
            (
                "2. For backend runtime telemetry, prefer Elastic APM or OTLP that ends in "
                "Elastic APM semantics instead of building a parallel trace-specific index path."
            ),
            "3. Validate transactions, spans, errors, and latency percentiles inside Kibana APM first.",
            "4. Use Service Map to confirm upstream/downstream edges before troubleshooting with custom searches.",
            "5. Keep the custom dashboard for agent-specific KPIs like model cost and tool failures; keep trace topology in APM.",
            "",
            "## Ingest mode note",
            "",
            (
                f"Current mode: `{ingest_mode}`. In `apm-otlp-hybrid`, the custom dashboard "
                "and Kibana APM should coexist: dashboard for agent KPIs, APM for transactions, traces, and dependencies."
            ),
            "",
        ]
    )


def build_trace_analysis_playbook(
    *,
    service_name: str,
    environment: str,
    kibana_url: str,
) -> str:
    kibana_hint = _kibana_hint(kibana_url)
    rum_service_name = _rum_service_name(service_name)
    return "\n".join(
        [
            "# Trace analysis playbook",
            "",
            "## Native-first rule",
            "",
            (
                "For trace analysis, prefer Kibana APM / Traces / Service Map over any "
                "custom trace dashboard in this repo. The custom dashboard is still useful "
                "for agent-specific KPIs, but trace topology belongs in Elastic-native apps."
            ),
            "",
            "## Analysis flow",
            "",
            f"1. Open Services: `{kibana_hint}/app/apm/services` and verify `{service_name}` in `{environment}`.",
            f"2. Open Traces: `{kibana_hint}/app/apm/traces` and inspect slow or failed traces first.",
            f"3. Open Service Map: `{kibana_hint}/app/apm/service-map` and confirm dependency edges.",
            "4. Pivot back to the custom dashboard only for GenAI-specific KPIs such as token cost and tool failures.",
            "",
            "## Frontend + backend correlation",
            "",
            f"- backend service: `{service_name}`",
            f"- frontend service: `{rum_service_name}`",
            "- keep distributed tracing origins aligned with the browser origin and API origin",
            "- if frontend traces exist but do not connect to backend transactions, fix trace header propagation before building more dashboards",
            "",
        ]
    ) + "\n"


def build_rum_config(*, apm_server_url: str, service_name: str, environment: str) -> dict[str, Any]:
    return {
        "serviceName": _rum_service_name(service_name),
        "serviceVersion": DEFAULT_RUM_SERVICE_VERSION,
        "serverUrl": _apm_server_hint(apm_server_url),
        "environment": environment,
        "transactionSampleRate": 1.0,
        "breakdownMetrics": True,
        "captureInteractions": True,
        "propagateTracestate": True,
        "distributedTracingOrigins": DEFAULT_DISTRIBUTED_TRACING_ORIGINS_JSON,
    }


def build_rum_bootstrap_script(
    *,
    apm_server_url: str,
    service_name: str,
    environment: str,
) -> str:
    rum_config = build_rum_config(
        apm_server_url=apm_server_url,
        service_name=service_name,
        environment=environment,
    )
    # In the JS snippet, use the runtime JS expression instead of the JSON placeholder.
    distributed_tracing_origins_js = DEFAULT_DISTRIBUTED_TRACING_ORIGINS_JS
    return "\n".join(
        [
            "import { init as initApm } from '@elastic/apm-rum';",
            "",
            "// Elastic User Experience / RUM starter.",
            "// Install first: npm install @elastic/apm-rum",
            "const apmConfig = {",
            f"  serviceName: '{rum_config['serviceName']}',",
            f"  serviceVersion: '{rum_config['serviceVersion']}',",
            f"  serverUrl: '{rum_config['serverUrl']}',",
            f"  environment: '{rum_config['environment']}',",
            f"  transactionSampleRate: {rum_config['transactionSampleRate']},",
            "  breakdownMetrics: true,",
            "  captureInteractions: true,",
            "  propagateTracestate: true,",
            f"  distributedTracingOrigins: [{distributed_tracing_origins_js}],",
            "  pageLoadTransactionName: window.location.pathname,",
            "};",
            "",
            "export const apm = initApm(apmConfig);",
            "",
            "// Optional: enrich correlation once auth / tenancy is available.",
            "// apm.setUserContext({ id: 'user-id' });",
            "// apm.addLabels({ product_area: 'agent-ui' });",
            "",
        ]
    )


def build_ux_playbook(*, service_name: str, environment: str, kibana_url: str) -> str:
    kibana_hint = _kibana_hint(kibana_url)
    rum_service_name = _rum_service_name(service_name)
    return "\n".join(
        [
            "# User experience monitoring playbook",
            "",
            "## Goal",
            "",
            (
                "Use Elastic RUM + the Kibana User Experience app for page loads, route changes, "
                "JS errors, and frontend/backend trace correlation. Do not duplicate those views as a custom frontend dashboard."
            ),
            "",
            "## Generated files",
            "",
            "- `rum-config.json`: machine-readable RUM defaults for app bootstrap",
            "- `rum-agent-snippet.js`: direct `@elastic/apm-rum` starter snippet",
            "- `surface-manifest.json`: native Kibana app entrypoints and correlation contract",
            "",
            "## Minimum contract",
            "",
            f"- frontend `service.name` = `{rum_service_name}`",
            f"- backend `service.name` = `{service_name}`",
            f"- environment = `{environment}` on both sides",
            "- distributed tracing origins must include the browser origin and any cross-origin API domain",
            "",
            "## Validation flow",
            "",
            f"1. Ship the snippet and confirm data lands in `{kibana_hint}/app/ux`.",
            "2. Confirm page-load and route-change transactions are visible before tuning dashboards.",
            "3. Open the linked APM trace and verify frontend spans join the backend trace tree.",
            "4. If browser telemetry appears without backend links, fix header propagation before adding more instrumentation.",
            "",
        ]
    ) + "\n"


def build_profiling_readme(
    *,
    service_name: str,
    environment: str,
    ingest_mode: str,
) -> str:
    return "\n".join(
        [
            "# Profiling starter",
            "",
            "## What this means here",
            "",
            "This repo does not auto-install Elastic Universal Profiling for you.",
            (
                "What it does provide is the rollout contract so CPU profiling stays "
                "aligned with Elastic-native APM and trace analysis instead of becoming an unrelated add-on."
            ),
            "",
            "## Recommended rollout",
            "",
            "1. Enable Elastic Agent / Fleet or Universal Profiling on the Linux hosts that run the agent workload.",
            f"2. Keep service naming aligned with APM: `{service_name}` in `{environment}`.",
            "3. Use APM traces to find slow transactions, then use Universal Profiling to isolate hot stack traces and CPU-heavy code paths behind those traces.",
            "4. Treat profiling as host-level observability that complements APM and UX rather than replacing them.",
            "",
            "## Boundary",
            "",
            f"Current ingest mode: `{ingest_mode}`. This file is a rollout checklist, not a host installer.",
            "",
        ]
    )


def render_assets(
    discovery: dict[str, Any],
    output_dir: Path,
    *,
    ingest_mode: str,
    index_prefix: str,
    service_name: str,
    environment: str,
    fleet_server_url: str,
    fleet_enrollment_token: str,
    apm_server_url: str,
    kibana_url: str,
    otlp_endpoint: str,
) -> dict[str, str]:
    if ingest_mode not in SUPPORTED_INGEST_MODES:
        raise SkillError(f"Unsupported ingest mode: {ingest_mode}")

    ensure_dir(output_dir)
    policy = build_policy(
        discovery,
        index_prefix=index_prefix,
        service_name=service_name,
        environment=environment,
        ingest_mode=ingest_mode,
    )
    env_text = build_env_template(
        fleet_server_url=fleet_server_url,
        fleet_enrollment_token=fleet_enrollment_token,
        apm_server_url=apm_server_url,
        otlp_endpoint=otlp_endpoint,
        service_name=service_name,
        environment=environment,
    )
    surface_manifest = build_surface_manifest(
        service_name=service_name,
        environment=environment,
        apm_server_url=apm_server_url,
        kibana_url=kibana_url,
        ingest_mode=ingest_mode,
    )
    rum_config = build_rum_config(
        apm_server_url=apm_server_url,
        service_name=service_name,
        environment=environment,
    )
    preflight_manifest = build_preflight_manifest(
        discovery,
        ingest_mode=ingest_mode,
        service_name=service_name,
        environment=environment,
        fleet_server_url=fleet_server_url,
        fleet_enrollment_token=fleet_enrollment_token,
        apm_server_url=apm_server_url,
        kibana_url=kibana_url,
        otlp_endpoint=otlp_endpoint,
        surface_manifest=surface_manifest,
    )

    env_path = output_dir / "elastic-agent.env"
    launcher_path = output_dir / "run-elastic-agent.sh"
    readme_path = output_dir / "README.md"
    policy_path = output_dir / "elastic-agent-policy.json"
    surface_manifest_path = output_dir / "surface-manifest.json"
    preflight_path = output_dir / "preflight-checklist.json"
    apm_env_path = output_dir / "apm-agent.env"
    apm_readme_path = output_dir / "apm-entrypoints.md"
    trace_playbook_path = output_dir / "trace-analysis-playbook.md"
    rum_config_path = output_dir / "rum-config.json"
    rum_snippet_path = output_dir / "rum-agent-snippet.js"
    ux_playbook_path = output_dir / "ux-observability-playbook.md"
    profiling_readme_path = output_dir / "profiling-starter.md"

    write_json(policy_path, policy)
    write_json(surface_manifest_path, surface_manifest)
    write_json(preflight_path, preflight_manifest)
    write_text(env_path, env_text)
    write_text(launcher_path, build_run_script(ingest_mode=ingest_mode, env_path=env_path))
    write_text(
        readme_path,
        build_bootstrap_readme(
            ingest_mode=ingest_mode,
            kibana_url=kibana_url,
            fleet_server_url=fleet_server_url,
            apm_server_url=apm_server_url,
        ),
    )
    write_text(
        apm_env_path,
        build_apm_agent_env(
            apm_server_url=apm_server_url,
            otlp_endpoint=otlp_endpoint,
            service_name=service_name,
            environment=environment,
        ),
    )
    write_text(
        apm_readme_path,
        build_apm_entrypoints_readme(
            service_name=service_name,
            environment=environment,
            apm_server_url=apm_server_url,
            otlp_endpoint=otlp_endpoint,
            kibana_url=kibana_url,
            ingest_mode=ingest_mode,
        ),
    )
    write_text(
        trace_playbook_path,
        build_trace_analysis_playbook(
            service_name=service_name,
            environment=environment,
            kibana_url=kibana_url,
        ),
    )
    write_json(rum_config_path, rum_config)
    write_text(
        rum_snippet_path,
        build_rum_bootstrap_script(
            apm_server_url=apm_server_url,
            service_name=service_name,
            environment=environment,
        ),
    )
    write_text(
        ux_playbook_path,
        build_ux_playbook(
            service_name=service_name,
            environment=environment,
            kibana_url=kibana_url,
        ),
    )
    write_text(
        profiling_readme_path,
        build_profiling_readme(
            service_name=service_name,
            environment=environment,
            ingest_mode=ingest_mode,
        ),
    )
    launcher_path.chmod(0o755)
    return {
        "policy": str(policy_path),
        "env": str(env_path),
        "launcher": str(launcher_path),
        "readme": str(readme_path),
        "surface_manifest": str(surface_manifest_path),
        "preflight": str(preflight_path),
        "apm_env": str(apm_env_path),
        "apm_readme": str(apm_readme_path),
        "trace_playbook": str(trace_playbook_path),
        "rum_config": str(rum_config_path),
        "rum_snippet": str(rum_snippet_path),
        "ux_playbook": str(ux_playbook_path),
        "profiling_readme": str(profiling_readme_path),
    }


def main() -> int:
    try:
        args = parse_args()
        discovery = read_json(Path(args.discovery).expanduser().resolve())
        output_dir = Path(args.output_dir).expanduser().resolve()
        paths = render_assets(
            discovery,
            output_dir,
            ingest_mode=args.ingest_mode,
            index_prefix=args.index_prefix,
            service_name=args.service_name,
            environment=args.environment,
            fleet_server_url=args.fleet_server_url,
            fleet_enrollment_token=args.fleet_enrollment_token,
            apm_server_url=args.apm_server_url,
            kibana_url=args.kibana_url,
            otlp_endpoint=args.otlp_endpoint,
        )
        print(f"✅ Elastic-native assets written to: {output_dir}")
        for name, path in paths.items():
            print(f"   {name}: {path}")
        return 0
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Failed to render Elastic-native assets: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
