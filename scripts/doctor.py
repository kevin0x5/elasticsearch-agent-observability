#!/usr/bin/env python3
"""End-to-end honesty check for the observability pipeline.

Why this exists
---------------

The bridge's ``/healthz`` returns 200 as soon as the HTTP server is up — it
does NOT prove that the Collector is alive, that port 4318 is listening, or
that real agent data is actually reaching Elasticsearch. In the wild we have
seen this exact failure mode:

- ``GET /healthz`` -> 200
- ``curl 127.0.0.1:4318`` -> connection refused
- ``ps aux | grep otelcol`` -> ``otelcol-contrib <defunct>``
- Result: monitoring dashboards look fine, downstream tasks get SIGTERM'd,
  no traces are being written.

This script refuses to let ``healthz`` lie. It runs five independent checks
and reports a single honest verdict:

1. bridge ``/healthz`` reachable
2. Collector / bridge processes alive (not zombie/defunct)
3. OTLP ports (4317, 4318, 14319) actually listening
4. ES has real agent documents in the last N minutes (not just sanity)
5. A fresh OTLP canary lands in ES within the timeout

Verdicts:

- ``healthy``      — all five checks pass. The pipeline is live.
- ``degraded``     — some checks pass; the pipeline is partly working but
                     cannot be trusted. The script lists exactly which leg
                     is broken and what to do.
- ``broken``       — the data plane is dead even if healthz is 200. The
                     agent will lose telemetry right now.
- ``unreachable``  — cannot even reach the endpoint the user specified.

Exit codes mirror verify_pipeline:

- ``0`` healthy
- ``2`` degraded / broken (loud middle state — the dangerous case)
- ``1`` unreachable / fatal error
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from common import (
    ESConfig,
    SkillError,
    build_data_stream_name,
    build_ssl_context,
    emit_skill_audit,
    es_request,
    print_error,
    validate_credential_pair,
    validate_index_prefix,
)
from verify_pipeline import _local_preflight, run_verify as verify_run


DEFAULT_FRESHNESS_MINUTES = 10
DEFAULT_HEALTHZ_URL = "http://127.0.0.1:14319/healthz"
DEFAULT_OTLP_HTTP_ENDPOINT = "http://127.0.0.1:14319"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Honest end-to-end diagnostic. Refuses to let /healthz lie.",
    )
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--es-user", default="")
    parser.add_argument("--es-password", default="")
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--healthz-url", default=DEFAULT_HEALTHZ_URL)
    parser.add_argument(
        "--otlp-http-endpoint",
        default=DEFAULT_OTLP_HTTP_ENDPOINT,
        help="OTLP/HTTP endpoint to probe with a canary. Point at the bridge or Collector HTTP receiver.",
    )
    parser.add_argument(
        "--freshness-minutes",
        type=int,
        default=DEFAULT_FRESHNESS_MINUTES,
        help="How far back to look for real agent data. Default: last 10 minutes.",
    )
    parser.add_argument(
        "--skip-canary",
        action="store_true",
        help="Skip the live OTLP canary (still checks processes, ports, healthz, recent ES data).",
    )
    parser.add_argument("--no-verify-tls", action="store_true")
    parser.add_argument("--collector-log", default="", help="Optional Collector log path for tail-on-failure")
    parser.add_argument("--output-format", choices=["text", "json"], default="text")
    parser.add_argument(
        "--audit",
        dest="audit",
        action="store_true",
        default=True,
        help="Write a self-audit record with the verdict and per-check statuses (default: enabled).",
    )
    parser.add_argument("--no-audit", dest="audit", action="store_false", help="Skip the self-audit write.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Individual probes. Each returns a ``{status, detail, ...}`` dict.
# Status is one of: ``pass`` | ``warn`` | ``fail`` | ``skipped``.
# ---------------------------------------------------------------------------


def _probe_healthz(healthz_url: str, *, verify_tls: bool) -> dict[str, Any]:
    context = build_ssl_context(verify_tls)
    try:
        req = urllib.request.Request(healthz_url, method="GET")
        with urllib.request.urlopen(req, timeout=5, context=context) as response:  # noqa: S310
            code = response.status
            body = response.read(2048).decode("utf-8", errors="replace")
        if code == 200:
            return {
                "status": "pass",
                "detail": f"healthz responded 200 at {healthz_url}",
                "body_snippet": body[:200],
                "warning": "healthz=200 only proves the HTTP listener is alive. It does NOT prove Collector/ES are healthy.",
            }
        return {"status": "fail", "detail": f"healthz responded {code} at {healthz_url}"}
    except urllib.error.HTTPError as exc:
        return {"status": "fail", "detail": f"HTTP {exc.code} at {healthz_url}"}
    except urllib.error.URLError as exc:
        return {"status": "fail", "detail": f"cannot reach {healthz_url}: {exc.reason}"}


def _probe_processes_and_ports(otlp_endpoint: str, collector_log: str) -> dict[str, Any]:
    """Wraps verify_pipeline._local_preflight. Fails loudly on zombies."""
    from pathlib import Path as _Path

    log_path = _Path(collector_log).expanduser().resolve() if collector_log else None
    preflight = _local_preflight(otlp_endpoint=otlp_endpoint, collector_log=log_path)
    zombies = preflight.get("zombie_processes") or []
    listening = preflight.get("listening_ports") or {}
    any_listening = any(listening.values())

    if zombies:
        return {
            "status": "fail",
            "detail": (
                f"Detected {len(zombies)} zombie/defunct Collector process(es). "
                "The OTLP listener is gone even though the process table still shows entries. "
                "This is exactly the failure mode where healthz lies."
            ),
            "zombies": zombies,
            "listening_ports": listening,
            "fix": "Reap: `pkill -9 -f otelcol-contrib` then relaunch via `run-collector.sh --daemon`.",
        }
    if not any_listening:
        return {
            "status": "fail",
            "detail": (
                f"No listener on any OTLP port ({', '.join(preflight.get('probed_ports', []))}). "
                "Collector/bridge are not running."
            ),
            "listening_ports": listening,
            "fix": "Start via `run-collector.sh --daemon` or `run-otlphttpbridge.sh --daemon`.",
        }
    partial = [p for p, ok in listening.items() if not ok]
    if partial:
        return {
            "status": "warn",
            "detail": f"Only some OTLP ports are listening. Missing: {partial}",
            "listening_ports": listening,
        }
    return {"status": "pass", "detail": "Processes alive and all probed OTLP ports listening", "listening_ports": listening}


def _probe_recent_data(config: ESConfig, *, index_prefix: str, freshness_minutes: int) -> dict[str, Any]:
    """Look for REAL agent documents in the last N minutes.

    "Real" means not ``internal.*`` datasets (sanity_check, pipeline_verify,
    alert_check). A cluster that only has internal heartbeat docs is not a
    working pipeline — it's a pipeline nobody is using.
    """
    ds_glob = f"{build_data_stream_name(index_prefix)}*"
    payload = {
        "size": 0,
        "timeout": "10s",
        "query": {
            "bool": {
                "filter": [{"range": {"@timestamp": {"gte": f"now-{freshness_minutes}m"}}}],
                "must_not": [{"prefix": {"event.dataset": "internal."}}],
            }
        },
        "aggs": {
            "by_service": {"terms": {"field": "service.name", "size": 5}},
        },
    }
    try:
        result = es_request(config, "POST", f"/{ds_glob}/_search", payload)
    except SkillError as exc:
        return {"status": "fail", "detail": f"cannot query ES: {exc}"}
    total = ((result.get("hits") or {}).get("total") or {}).get("value", 0)
    services = [
        {"name": b.get("key"), "count": b.get("doc_count", 0)}
        for b in (result.get("aggregations") or {}).get("by_service", {}).get("buckets", [])
    ]
    if total == 0:
        return {
            "status": "fail",
            "detail": (
                f"No real agent documents in the last {freshness_minutes} minutes. "
                "Either the agent is not emitting, or the Collector/bridge is not forwarding. "
                "This is the classic 'healthz ok, data plane dead' case."
            ),
            "doc_count": 0,
            "freshness_minutes": freshness_minutes,
            "fix": "Run the canary check (doctor without --skip-canary) to pin down whether the OTLP path is broken.",
        }
    return {
        "status": "pass",
        "detail": f"{total} real agent document(s) in the last {freshness_minutes} minutes",
        "doc_count": total,
        "top_services": services,
        "freshness_minutes": freshness_minutes,
    }


def _probe_canary(args: argparse.Namespace) -> dict[str, Any]:
    """Run a fresh OTLP canary through the endpoint and confirm it hits ES."""
    import argparse as _argparse

    verify_args = _argparse.Namespace(
        es_url=args.es_url,
        es_user=args.es_user,
        es_password=args.es_password,
        index_prefix=args.index_prefix,
        otlp_http_endpoint=args.otlp_http_endpoint,
        service_name="doctor-canary",
        poll_attempts=5,
        poll_backoff=1.5,
        no_verify_tls=args.no_verify_tls,
        collector_log=args.collector_log,
        output=None,
    )
    try:
        result = verify_run(verify_args)
    except Exception as exc:  # noqa: BLE001
        return {"status": "fail", "detail": f"canary crashed: {exc}"}
    verdict = result.get("verdict")
    if verdict == "ok":
        return {"status": "pass", "detail": "canary landed in ES", "verdict": verdict, "canary_id": result.get("canary_id")}
    return {
        "status": "fail",
        "detail": f"canary verdict: {verdict}",
        "verdict": verdict,
        "canary_id": result.get("canary_id"),
        "next_step": result.get("next_step"),
    }


# ---------------------------------------------------------------------------
# Verdict aggregation
# ---------------------------------------------------------------------------


def _aggregate(checks: dict[str, dict[str, Any]]) -> str:
    """Collapse individual check statuses into a single honest verdict.

    Rules:
    - Any ``fail`` in the data plane (processes_and_ports, recent_data, canary)
      -> ``broken`` (even if healthz is 200 — this is the whole point).
    - healthz=pass but data plane failing -> ``broken`` with explicit lie warning.
    - Any ``warn`` and no fail -> ``degraded``.
    - All ``pass`` -> ``healthy``.
    - healthz fail AND processes fail AND ES reachable -> ``broken``.
    - ES unreachable -> ``unreachable``.
    """
    statuses = {name: ch.get("status") for name, ch in checks.items()}

    # If ES itself is unreachable, nothing else can be trusted.
    if statuses.get("recent_data") == "fail" and "cannot query ES" in (checks.get("recent_data", {}).get("detail", "")):
        return "unreachable"

    data_plane = [statuses.get("processes_and_ports"), statuses.get("recent_data"), statuses.get("canary")]
    if "fail" in data_plane:
        return "broken"
    if any(s == "warn" for s in statuses.values()):
        return "degraded"
    if all(s in {"pass", "skipped"} for s in statuses.values()):
        return "healthy"
    return "degraded"


def run_doctor(args: argparse.Namespace) -> dict[str, Any]:
    credentials = validate_credential_pair(args.es_user, args.es_password)
    config = ESConfig(
        es_url=args.es_url,
        es_user=credentials[0] if credentials else None,
        es_password=credentials[1] if credentials else None,
        verify_tls=not args.no_verify_tls,
    )
    index_prefix = validate_index_prefix(args.index_prefix)

    checks: dict[str, dict[str, Any]] = {
        "healthz": _probe_healthz(args.healthz_url, verify_tls=not args.no_verify_tls),
        "processes_and_ports": _probe_processes_and_ports(args.otlp_http_endpoint, args.collector_log),
        "recent_data": _probe_recent_data(
            config, index_prefix=index_prefix, freshness_minutes=args.freshness_minutes
        ),
    }
    if args.skip_canary:
        checks["canary"] = {"status": "skipped", "detail": "skipped by --skip-canary"}
    else:
        checks["canary"] = _probe_canary(args)

    verdict = _aggregate(checks)

    # Build the "honest summary" — the one-liner the user can paste into a
    # report that will not get them yelled at later.
    if verdict == "healthy":
        summary = (
            f"Pipeline is live. {checks['recent_data'].get('doc_count', 0)} real document(s) in the last "
            f"{args.freshness_minutes} minutes; a fresh canary landed in ES."
        )
    elif verdict == "broken":
        broken = [name for name, ch in checks.items() if ch.get("status") == "fail"]
        healthz_ok = checks["healthz"].get("status") == "pass"
        lie_warning = (
            " /healthz is returning 200 but the data plane is dead — do NOT trust healthz as a pipeline indicator."
            if healthz_ok else ""
        )
        summary = (
            f"Pipeline is BROKEN on the data plane ({', '.join(broken)})."
            f"{lie_warning}"
            " See per-check detail for fix."
        )
    elif verdict == "degraded":
        degraded = [name for name, ch in checks.items() if ch.get("status") in {"warn", "fail"}]
        summary = f"Pipeline is degraded ({', '.join(degraded)}). Partial functionality only."
    elif verdict == "unreachable":
        summary = "Cannot reach Elasticsearch; the pipeline state is unknown."
    else:
        summary = "Pipeline state is ambiguous; review per-check detail."

    return {
        "verdict": verdict,
        "summary": summary,
        "index_prefix": index_prefix,
        "healthz_url": args.healthz_url,
        "otlp_http_endpoint": args.otlp_http_endpoint,
        "freshness_minutes": args.freshness_minutes,
        "checks": checks,
    }


def render_text(result: dict[str, Any]) -> str:
    icons = {"pass": "✓", "warn": "!", "fail": "✗", "skipped": "–"}
    verdict_icons = {"healthy": "✓", "degraded": "!", "broken": "✗", "unreachable": "?"}
    lines = [
        f"[{verdict_icons.get(result['verdict'], '?')} {result['verdict'].upper()}] {result['summary']}",
        "",
    ]
    for name, check in result["checks"].items():
        icon = icons.get(check.get("status"), "?")
        lines.append(f"  {icon} {name}: {check.get('detail', '')}")
        if check.get("warning"):
            lines.append(f"      ⚠ {check['warning']}")
        if check.get("fix"):
            lines.append(f"      → fix: {check['fix']}")
        if check.get("next_step"):
            snippet = str(check["next_step"]).splitlines()[0][:200]
            lines.append(f"      → next: {snippet}")
    return "\n".join(lines)


def main() -> int:
    try:
        args = parse_args()
        import time as _time
        start = _time.monotonic()
        result = run_doctor(args)
        duration_ms = int((_time.monotonic() - start) * 1000)
        if args.output_format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(render_text(result))
        if args.audit and result["verdict"] != "unreachable":
            # Audit writes to ES; when ES is unreachable we already know, skip.
            credentials = validate_credential_pair(args.es_user, args.es_password)
            config = ESConfig(
                es_url=args.es_url,
                es_user=credentials[0] if credentials else None,
                es_password=credentials[1] if credentials else None,
                verify_tls=not args.no_verify_tls,
            )
            emit_skill_audit(
                config,
                index_prefix=validate_index_prefix(args.index_prefix),
                tool_name="doctor",
                verdict=result["verdict"],
                duration_ms=duration_ms,
                inputs={
                    "healthz_url": args.healthz_url,
                    "otlp_http_endpoint": args.otlp_http_endpoint,
                    "freshness_minutes": args.freshness_minutes,
                    "skip_canary": args.skip_canary,
                },
                evidence={
                    name: check.get("status")
                    for name, check in result.get("checks", {}).items()
                },
            )
        verdict = result["verdict"]
        if verdict == "healthy":
            return 0
        if verdict in {"degraded", "broken"}:
            return 2
        return 1
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Doctor failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
