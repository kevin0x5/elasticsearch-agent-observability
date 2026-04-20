#!/usr/bin/env python3
"""Apply generated Elasticsearch observability assets to a cluster."""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from common import (
    ESConfig,
    SkillError,
    build_component_template_name,
    build_data_stream_name,
    build_events_alias,
    es_request,
    print_error,
    read_json,
    validate_credential_pair,
    validate_index_prefix,
    validate_workspace_dir,
)

RESOURCE_ALREADY_EXISTS = "resource_already_exists_exception"
NATIVE_KIBANA_APP_KEYS = ("services", "traces", "service_map", "user_experience")
PLACEHOLDER_HOST_MARKERS = (
    "kibana.example.com",
    "apm.example.com",
    "your-app-origin.example.com",
)


def sanity_check(config: ESConfig, *, index_prefix: str) -> dict[str, Any]:
    """Write a test document, refresh, query, and delete it to verify the pipeline is working end-to-end.

    The sanity doc is tagged with ``event.dataset = "internal.sanity_check"`` so that
    alert/report aggregations can filter it out and not count it as a real agent event.
    Cleanup is done in a ``finally`` block so a failed search still tries to delete the doc.
    """
    ds_name = build_data_stream_name(index_prefix)
    test_doc = {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "event.action": "_sanity_check",
        "event.kind": "event",
        "event.outcome": "success",
        "event.dataset": "internal.sanity_check",
        "service.name": "sanity-check",
        "gen_ai.agent.tool_name": "_sanity_check_tool",
        "gen_ai.agent.signal_type": "sanity_check",
        "message": "End-to-end sanity check document",
    }
    doc_id = ""
    try:
        index_result = es_request(config, "POST", f"/{ds_name}/_doc", test_doc)
        doc_id = index_result.get("_id", "")
        if not doc_id:
            return {"status": "failed", "reason": "Index returned no _id", "detail": index_result}
        es_request(config, "POST", f"/{ds_name}/_refresh")
        query = {"query": {"term": {"event.action": "_sanity_check"}}, "size": 1}
        search_result = es_request(config, "POST", f"/{ds_name}/_search", query)
        hits = search_result.get("hits", {}).get("total", {}).get("value", 0)
        if hits < 1:
            return {"status": "failed", "reason": "Sanity check doc not found after indexing", "doc_id": doc_id}
        found_doc = search_result["hits"]["hits"][0]["_source"]
        pipeline_applied = found_doc.get("observer.product") == "elasticsearch-agent-observability"
        return {
            "status": "passed",
            "doc_id": doc_id,
            "pipeline_applied": pipeline_applied,
            "indexed_fields_sample": list(found_doc.keys())[:10],
        }
    except SkillError as exc:
        return {"status": "failed", "reason": str(exc), "doc_id": doc_id}
    finally:
        if doc_id:
            try:
                es_request(
                    config,
                    "POST",
                    f"/{ds_name}/_delete_by_query?refresh=true",
                    {"query": {"term": {"event.action": "_sanity_check"}}},
                )
            except SkillError:
                # Best-effort cleanup; the dataset tag still lets consumers filter.
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply generated Elasticsearch observability assets")
    parser.add_argument("--assets-dir", required=True)
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--es-user", default="")
    parser.add_argument("--es-password", default="")
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--skip-bootstrap-index", action="store_true", help="Skip creating the data stream")
    parser.add_argument("--kibana-url", default="", help="Optional Kibana base URL for applying saved objects")
    parser.add_argument("--kibana-space", default="default")
    parser.add_argument("--native-assets-dir", default="", help="Optional elastic-native bundle directory for preflight inspection")
    parser.add_argument("--skip-kibana-assets", action="store_true", help="Skip applying Kibana saved objects even if present")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be applied without actually sending requests")
    return parser.parse_args()


def load_assets(assets_dir: Path) -> dict[str, Any]:
    resolved = validate_workspace_dir(assets_dir, "Assets directory")
    kibana_json = resolved / "kibana-saved-objects.json"
    result: dict[str, Any] = {
        "index_template": read_json(resolved / "index-template.json"),
        "ingest_pipeline": read_json(resolved / "ingest-pipeline.json"),
        "ilm_policy": read_json(resolved / "ilm-policy.json"),
        "report_config": read_json(resolved / "report-config.json"),
        "kibana_saved_objects": read_json(kibana_json) if kibana_json.exists() else None,
    }
    ecs_base_path = resolved / "component-template-ecs-base.json"
    settings_path = resolved / "component-template-settings.json"
    if ecs_base_path.exists():
        result["component_template_ecs_base"] = read_json(ecs_base_path)
    if settings_path.exists():
        result["component_template_settings"] = read_json(settings_path)
    return result


def load_native_assets(native_assets_dir: Path) -> dict[str, Any]:
    resolved = validate_workspace_dir(native_assets_dir, "Native assets directory")
    result: dict[str, Any] = {}
    preflight_path = resolved / "preflight-checklist.json"
    surface_manifest_path = resolved / "surface-manifest.json"
    rum_config_path = resolved / "rum-config.json"
    if preflight_path.exists():
        result["preflight"] = read_json(preflight_path)
    if surface_manifest_path.exists():
        result["surface_manifest"] = read_json(surface_manifest_path)
    if rum_config_path.exists():
        result["rum_config"] = read_json(rum_config_path)
    if not result:
        raise SkillError("Native assets directory must contain `preflight-checklist.json`, `surface-manifest.json`, or `rum-config.json`")
    result["path"] = str(resolved)
    return result


def _compute_native_overall_status(static_checks: list[dict[str, Any]], runtime_checks: list[dict[str, Any]]) -> str:
    required_checks = [check for check in [*static_checks, *runtime_checks] if check.get("required")]
    if any(check.get("status") == "failed" for check in required_checks):
        return "failed"
    if any(check.get("status") == "action_required" for check in required_checks):
        return "action_required"
    return "ready"


def _build_native_check(*, key: str, label: str, required: bool, status: str, detail: str) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "required": required,
        "status": status,
        "detail": detail,
    }


def _contains_placeholder_host(value: str) -> bool:
    normalized = value.strip()
    return bool(normalized) and any(marker in normalized for marker in PLACEHOLDER_HOST_MARKERS)


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _build_native_contract_checks(
    *,
    preflight: dict[str, Any],
    surface_manifest: dict[str, Any],
    rum_config: dict[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    kibana_apps = surface_manifest.get("kibana_apps", {}) if isinstance(surface_manifest, dict) else {}
    missing_apps = [key for key in NATIVE_KIBANA_APP_KEYS if not str(kibana_apps.get(key, "")).strip()]
    placeholder_apps = [
        key for key in NATIVE_KIBANA_APP_KEYS if _contains_placeholder_host(str(kibana_apps.get(key, "")))
    ]
    if missing_apps:
        entrypoint_status = "failed"
        entrypoint_detail = (
            "surface-manifest.json is missing native Kibana app entrypoints for "
            f"{', '.join(missing_apps)}. Re-render the elastic-native bundle before rollout."
        )
    elif placeholder_apps:
        entrypoint_status = "action_required"
        entrypoint_detail = (
            "Native Kibana app entrypoints still point at placeholder hosts for "
            f"{', '.join(placeholder_apps)}. Set a real `--kibana-url` and re-render the bundle."
        )
    else:
        entrypoint_status = "ready"
        entrypoint_detail = "Native Kibana app entrypoints are present in surface-manifest.json."
    checks.append(
        _build_native_check(
            key="native_kibana_entrypoints",
            label="Native Kibana entrypoints",
            required=True,
            status=entrypoint_status,
            detail=entrypoint_detail,
        )
    )

    services = surface_manifest.get("services", {}) if isinstance(surface_manifest, dict) else {}
    backend_service = str(services.get("backend", "")).strip()
    frontend_service = str(services.get("frontend", "")).strip()
    manifest_environment = str(services.get("environment", "")).strip()
    expected_service_name = str(preflight.get("service_name", "")).strip()
    expected_environment = str(preflight.get("environment", "")).strip()
    rum_service_name = str(rum_config.get("serviceName", "")).strip() if isinstance(rum_config, dict) else ""
    identity_issues: list[str] = []
    if not backend_service:
        identity_issues.append("surface manifest backend service is missing")
    elif expected_service_name and backend_service != expected_service_name:
        identity_issues.append(
            f"surface manifest backend service `{backend_service}` does not match preflight service `{expected_service_name}`"
        )
    if not manifest_environment:
        identity_issues.append("surface manifest environment is missing")
    elif expected_environment and manifest_environment != expected_environment:
        identity_issues.append(
            f"surface manifest environment `{manifest_environment}` does not match preflight environment `{expected_environment}`"
        )
    if frontend_service and rum_service_name and frontend_service != rum_service_name:
        identity_issues.append(
            f"RUM service `{rum_service_name}` does not match surface manifest frontend service `{frontend_service}`"
        )
    checks.append(
        _build_native_check(
            key="native_service_contract",
            label="Native service identity contract",
            required=True,
            status="action_required" if identity_issues else "ready",
            detail=(
                "Native backend/frontend service identity is aligned across preflight, surface manifest, and RUM config."
                if not identity_issues
                else "; ".join(identity_issues)
            ),
        )
    )

    rum_origins = _normalize_string_list(rum_config.get("distributedTracingOrigins", [])) if isinstance(rum_config, dict) else []
    rum_required = any(
        check.get("key") == "rum_distributed_tracing_origins" and check.get("required")
        for check in preflight.get("checks", [])
    )
    if rum_required:
        if not rum_origins:
            rum_status = "failed"
            rum_detail = "rum-config.json is missing distributedTracingOrigins for the detected browser frontend."
        elif any(_contains_placeholder_host(origin) for origin in rum_origins):
            rum_status = "action_required"
            rum_detail = (
                "rum-config.json still uses placeholder distributed tracing origins. "
                "Replace them with the real browser/API origins before shipping the UX path."
            )
        else:
            rum_status = "ready"
            rum_detail = "RUM distributed tracing origins are explicitly configured for frontend/backend trace correlation."
    elif rum_origins:
        rum_status = "ready" if not any(_contains_placeholder_host(origin) for origin in rum_origins) else "action_required"
        rum_detail = (
            "RUM distributed tracing origins are configured."
            if rum_status == "ready"
            else "RUM distributed tracing origins exist but still include placeholder hosts."
        )
    else:
        rum_status = "skipped"
        rum_detail = "No browser frontend contract was required by the native preflight bundle."
    checks.append(
        _build_native_check(
            key="rum_trace_correlation_contract",
            label="RUM trace correlation contract",
            required=rum_required,
            status=rum_status,
            detail=rum_detail,
        )
    )
    return checks


def inspect_native_assets(
    config: ESConfig,
    *,
    native_assets: dict[str, Any],
    kibana_url: str | None,
    perform_runtime_checks: bool,
) -> dict[str, Any]:
    preflight = native_assets.get("preflight") if isinstance(native_assets, dict) else {}
    surface_manifest = native_assets.get("surface_manifest") if isinstance(native_assets, dict) else {}
    rum_config = native_assets.get("rum_config") if isinstance(native_assets, dict) else {}
    preflight_checks = list(preflight.get("checks", [])) if isinstance(preflight, dict) else []
    contract_checks = _build_native_contract_checks(
        preflight=preflight if isinstance(preflight, dict) else {},
        surface_manifest=surface_manifest if isinstance(surface_manifest, dict) else {},
        rum_config=rum_config if isinstance(rum_config, dict) else {},
    )
    static_checks = [*preflight_checks, *contract_checks]
    ingest_mode = str(preflight.get("ingest_mode", "")).strip() if isinstance(preflight, dict) else ""
    runtime_checks: list[dict[str, Any]] = []

    if perform_runtime_checks and kibana_url:
        try:
            status_response = kibana_request(config, kibana_url, "GET", "/api/status")
            overall = status_response.get("status", {}).get("overall", {}) if isinstance(status_response, dict) else {}
            level = overall.get("level") or overall.get("state") or "unknown"
            summary = overall.get("summary") or overall.get("title") or "Kibana status API reachable"
            runtime_checks.append(
                _build_native_check(
                    key="kibana_status_api",
                    label="Kibana status API",
                    required=True,
                    status="ready",
                    detail=f"Kibana API reachable ({level}): {summary}",
                )
            )
        except SkillError as exc:
            runtime_checks.append(
                _build_native_check(
                    key="kibana_status_api",
                    label="Kibana status API",
                    required=True,
                    status="failed",
                    detail=str(exc),
                )
            )

        if ingest_mode == "elastic-agent-fleet":
            try:
                fleet_response = kibana_request(config, kibana_url, "GET", "/api/fleet/agent_policies?page=1&perPage=1")
                total = fleet_response.get("total") if isinstance(fleet_response, dict) else None
                total_text = f" total={total}" if total is not None else ""
                runtime_checks.append(
                    _build_native_check(
                        key="fleet_agent_policies_api",
                        label="Fleet agent policies API",
                        required=True,
                        status="ready",
                        detail=f"Fleet API reachable via Kibana.{total_text}",
                    )
                )
            except SkillError as exc:
                runtime_checks.append(
                    _build_native_check(
                        key="fleet_agent_policies_api",
                        label="Fleet agent policies API",
                        required=True,
                        status="failed",
                        detail=str(exc),
                    )
                )
    elif perform_runtime_checks:
        runtime_checks.append(
            _build_native_check(
                key="kibana_status_api",
                label="Kibana status API",
                required=True,
                status="action_required",
                detail="Set `--kibana-url` to run native Kibana / Fleet reachability checks.",
            )
        )

    combined_checks = [*static_checks, *runtime_checks]
    blocking_checks = [
        {
            "key": str(check.get("key", "")).strip(),
            "status": str(check.get("status", "")).strip(),
            "detail": str(check.get("detail", "")).strip(),
        }
        for check in combined_checks
        if check.get("status") in {"action_required", "failed"}
    ]
    action_required_count = sum(1 for check in combined_checks if check.get("status") == "action_required")
    failed_count = sum(1 for check in combined_checks if check.get("status") == "failed")
    return {
        "path": native_assets.get("path"),
        "ingest_mode": ingest_mode or surface_manifest.get("ingest_mode"),
        "overall_status": _compute_native_overall_status(static_checks, runtime_checks),
        "action_required_count": action_required_count,
        "failed_count": failed_count,
        "ready_count": sum(1 for check in combined_checks if check.get("status") == "ready"),
        "static_checks": static_checks,
        "contract_checks": contract_checks,
        "runtime_checks": runtime_checks,
        "blocking_checks": blocking_checks,
        "native_apps": surface_manifest.get("kibana_apps", {}),
        "next_steps": list(preflight.get("next_steps", [])) if isinstance(preflight, dict) else [],
    }


def kibana_request(config: ESConfig, kibana_url: str, method: str, path: str, payload: dict | None = None, *, body_bytes: bytes | None = None) -> dict[str, Any]:
    url = kibana_url.rstrip("/") + path
    request = urllib.request.Request(url, method=method.upper())
    request.add_header("Content-Type", "application/json")
    request.add_header("kbn-xsrf", "true")
    if config.kibana_api_key:
        request.add_header("Authorization", f"ApiKey {config.kibana_api_key}")
    elif config.es_user and config.es_password:
        token = base64.b64encode(f"{config.es_user}:{config.es_password}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
    body = body_bytes
    if body is None and payload is not None:
        body = json.dumps(payload).encode("utf-8")
    import ssl
    context = None
    if not config.verify_tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, data=body, timeout=config.timeout_seconds, context=context) as response:  # noqa: S310
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise SkillError(f"Kibana HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SkillError(f"Unable to reach Kibana: {exc.reason}") from exc
    if not text:
        return {"acknowledged": True}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SkillError(f"Invalid JSON response from Kibana: {text[:200]}") from exc


def build_space_prefix(space: str) -> str:
    normalized = space.strip() or "default"
    return "" if normalized == "default" else f"/s/{quote(normalized, safe='')}"


def ensure_data_stream(config: ESConfig, index_prefix: str) -> dict[str, str]:
    ds_name = build_data_stream_name(index_prefix)
    status = "created"
    try:
        es_request(config, "PUT", f"/_data_stream/{ds_name}")
    except SkillError as exc:
        if RESOURCE_ALREADY_EXISTS in str(exc) or "already exists" in str(exc).lower():
            status = "already_exists"
        else:
            raise
    return {"data_stream": ds_name, "status": status}


def ensure_bootstrap_data_stream(config: ESConfig, index_prefix: str) -> dict[str, str]:
    """Create the data stream required by the 9.x data-stream-first contract."""
    return ensure_data_stream(config, index_prefix)


def apply_kibana_saved_objects(config: ESConfig, *, kibana_url: str, kibana_space: str, bundle: dict[str, Any]) -> dict[str, Any]:
    objects = bundle.get("objects", []) if isinstance(bundle, dict) else []
    if not objects:
        return {"status": "skipped", "count": 0, "objects": []}
    space_prefix = build_space_prefix(kibana_space)
    applied: list[dict[str, str]] = []
    for saved_object in objects:
        object_type = str(saved_object.get("type", "")).strip()
        object_id = str(saved_object.get("id", "")).strip()
        if not object_type or not object_id:
            raise SkillError("Each Kibana saved object must include type and id")
        payload = {
            "attributes": saved_object.get("attributes", {}),
            "references": saved_object.get("references", []),
        }
        path = f"{space_prefix}/api/saved_objects/{quote(object_type, safe='')}/{quote(object_id, safe='')}?overwrite=true"
        response = kibana_request(config, kibana_url, "POST", path, payload)
        applied.append(
            {
                "type": object_type,
                "id": object_id,
                "title": str(saved_object.get("attributes", {}).get("title", object_id)),
                "response_id": str(response.get("id", object_id)),
            }
        )
    return {
        "status": "applied",
        "space": kibana_space,
        "count": len(applied),
        "objects": applied,
    }


def apply_assets(
    config: ESConfig,
    *,
    assets_dir: Path,
    index_prefix: str,
    bootstrap_index: bool = True,
    kibana_url: str | None = None,
    kibana_space: str = "default",
    apply_kibana: bool = True,
    native_assets_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    validated_prefix = validate_index_prefix(index_prefix)
    assets = load_assets(assets_dir)
    native_assets = load_native_assets(native_assets_dir) if native_assets_dir else None
    native_summary = inspect_native_assets(
        config,
        native_assets=native_assets,
        kibana_url=kibana_url,
        perform_runtime_checks=bool(native_assets and not dry_run),
    ) if native_assets else None
    template_name = f"{validated_prefix}-events-template"
    pipeline_name = f"{validated_prefix}-normalize"
    ilm_name = f"{validated_prefix}-lifecycle"

    if dry_run:
        plan: list[dict[str, str]] = [
            {"action": "PUT", "path": f"/_ilm/policy/{ilm_name}", "asset": "ilm_policy"},
            {"action": "PUT", "path": f"/_ingest/pipeline/{pipeline_name}", "asset": "ingest_pipeline"},
        ]
        if assets.get("component_template_ecs_base"):
            plan.append({"action": "PUT", "path": f"/_component_template/{build_component_template_name(validated_prefix, 'ecs-base')}", "asset": "component_template_ecs_base"})
        if assets.get("component_template_settings"):
            plan.append({"action": "PUT", "path": f"/_component_template/{build_component_template_name(validated_prefix, 'settings')}", "asset": "component_template_settings"})
        plan.append({"action": "PUT", "path": f"/_index_template/{template_name}", "asset": "index_template"})
        if bootstrap_index:
            plan.append({"action": "PUT", "path": f"/_data_stream/{build_data_stream_name(validated_prefix)}", "asset": "data_stream"})
        if apply_kibana and assets.get("kibana_saved_objects"):
            objects = assets["kibana_saved_objects"].get("objects", [])
            for obj in objects:
                plan.append({"action": "POST", "path": f"/api/saved_objects/{obj.get('type')}/{obj.get('id')}", "asset": f"kibana:{obj.get('type')}"})
        if native_summary and kibana_url:
            plan.append({"action": "CHECK", "path": "/api/status", "asset": "native:kibana_status_api"})
            if native_summary.get("ingest_mode") == "elastic-agent-fleet":
                plan.append({"action": "CHECK", "path": "/api/fleet/agent_policies?page=1&perPage=1", "asset": "native:fleet_agent_policies_api"})
        return {
            "dry_run": True,
            "plan": plan,
            "plan_count": len(plan),
            "index_prefix": validated_prefix,
            "native_bundle": native_summary,
        }

    responses: dict[str, Any] = {
        "ilm_policy": es_request(config, "PUT", f"/_ilm/policy/{ilm_name}", assets["ilm_policy"]),
        "ingest_pipeline": es_request(config, "PUT", f"/_ingest/pipeline/{pipeline_name}", assets["ingest_pipeline"]),
    }

    if assets.get("component_template_ecs_base"):
        ecs_base_name = build_component_template_name(validated_prefix, "ecs-base")
        responses["component_template_ecs_base"] = es_request(
            config, "PUT", f"/_component_template/{ecs_base_name}", assets["component_template_ecs_base"]
        )
    if assets.get("component_template_settings"):
        settings_name = build_component_template_name(validated_prefix, "settings")
        responses["component_template_settings"] = es_request(
            config, "PUT", f"/_component_template/{settings_name}", assets["component_template_settings"]
        )

    responses["index_template"] = es_request(config, "PUT", f"/_index_template/{template_name}", assets["index_template"])
    responses["report_config"] = assets["report_config"]

    bootstrap_summary = None
    if bootstrap_index:
        bootstrap_summary = ensure_bootstrap_data_stream(config, validated_prefix)

    kibana_summary = None
    if apply_kibana and kibana_url and assets.get("kibana_saved_objects"):
        kibana_summary = apply_kibana_saved_objects(
            config,
            kibana_url=kibana_url,
            kibana_space=kibana_space,
            bundle=assets["kibana_saved_objects"],
        )

    return {
        "assets_dir": str(assets_dir),
        "index_prefix": validated_prefix,
        "template_name": template_name,
        "pipeline_name": pipeline_name,
        "ilm_policy_name": ilm_name,
        "events_alias": build_events_alias(validated_prefix),
        "data_stream": build_data_stream_name(validated_prefix),
        "bootstrap_index": bootstrap_summary,
        "kibana": kibana_summary,
        "native_bundle": native_summary,
        "responses": responses,
    }


def main() -> int:
    try:
        args = parse_args()
        credentials = validate_credential_pair(args.es_user, args.es_password)
        config = ESConfig(
            es_url=args.es_url,
            es_user=credentials[0] if credentials else None,
            es_password=credentials[1] if credentials else None,
        )
        summary = apply_assets(
            config,
            assets_dir=Path(args.assets_dir).expanduser().resolve(),
            index_prefix=args.index_prefix,
            bootstrap_index=not args.skip_bootstrap_index,
            kibana_url=args.kibana_url or None,
            kibana_space=args.kibana_space,
            apply_kibana=not args.skip_kibana_assets,
            native_assets_dir=Path(args.native_assets_dir).expanduser().resolve() if args.native_assets_dir else None,
            dry_run=args.dry_run,
        )
        if summary.get("dry_run"):
            print(f"🔍 Dry-run: {summary['plan_count']} operation(s) would be applied")
            for step in summary["plan"]:
                print(f"   {step['action']} {step['path']}  ({step['asset']})")
            return 0
        print("✅ Elasticsearch assets applied")
        print(f"   data stream: {summary['data_stream']}")
        if summary["bootstrap_index"]:
            bs = summary["bootstrap_index"]
            print(f"   bootstrap: {bs['data_stream']} ({bs['status']})")
        if summary.get("kibana"):
            print(f"   kibana objects: {summary['kibana']['count']}")
        if summary.get("native_bundle"):
            native = summary["native_bundle"]
            print(
                "   native preflight: "
                f"{native['overall_status']} "
                f"(ready={native.get('ready_count', 0)}, action_required={native.get('action_required_count', 0)}, failed={native.get('failed_count', 0)})"
            )
        return 0
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Failed to apply Elasticsearch assets: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
