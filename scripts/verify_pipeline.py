#!/usr/bin/env python3
"""End-to-end pipeline verification.

Sends one canary OTLP/HTTP JSON log to the configured endpoint (Collector or
bridge), then polls Elasticsearch to confirm the event actually landed in the
``<prefix>-events`` data stream. If it did not, classify the failure and print
an actionable next step so the driving agent can self-correct without asking
the human.

Why this exists
---------------

The Collector -> Elasticsearch exporter in ES 9.x + otelcol-contrib has a
real-world "last mile" failure mode: OTLP receive is green, the Collector
reports ``smoke-sent``, but the document never appears in ES because of
mapping conflicts, flush backlogs, or an index name that doesn't match what
the exporter writes to. Diagnosing that by hand means tailing Collector
logs and guessing. This script compresses that to a single command that
returns a verdict + a next step.

Exit codes
----------

- ``0`` canary was indexed and has the expected shape
- ``2`` canary was sent but not indexed (or indexed but the important fields
  are missing) — pipeline alive, contract broken
- ``1`` could not send or could not reach ES at all
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import (
    ESConfig,
    KNOWN_OTLP_PORTS,
    SkillError,
    build_data_stream_name,
    es_request,
    print_error,
    print_info,
    validate_credential_pair,
    validate_index_prefix,
)


CANARY_SIGNAL_TYPE = "pipeline_verify"
CANARY_DATASET = "internal.pipeline_verify"
DEFAULT_POLL_ATTEMPTS = 5
DEFAULT_POLL_BACKOFF = 1.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the agent-observability ingest pipeline end-to-end")
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--es-user", default="")
    parser.add_argument("--es-password", default="")
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument(
        "--otlp-http-endpoint",
        default="http://127.0.0.1:14319",
        help="OTLP/HTTP base URL. Point at the bridge (:14319) or the Collector's HTTP receiver (:4318).",
    )
    parser.add_argument("--service-name", default="pipeline-verify")
    parser.add_argument("--poll-attempts", type=int, default=DEFAULT_POLL_ATTEMPTS)
    parser.add_argument("--poll-backoff", type=float, default=DEFAULT_POLL_BACKOFF)
    parser.add_argument("--no-verify-tls", action="store_true")
    parser.add_argument("--collector-log", default="", help="Optional path to Collector log for tail-on-failure")
    parser.add_argument("--output", help="Optional path to write JSON verdict")
    return parser.parse_args()


def _build_canary_log(*, service_name: str, canary_id: str) -> dict[str, Any]:
    """Build an OTLP/HTTP JSON log payload carrying a single canary record."""
    now_ns = str(int(datetime.now(timezone.utc).timestamp() * 1_000_000_000))
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}},
                        {"key": "deployment.environment", "value": {"stringValue": "verify"}},
                        {"key": "observer.product", "value": {"stringValue": "elasticsearch-agent-observability"}},
                    ]
                },
                "scopeLogs": [
                    {
                        "scope": {"name": "pipeline-verify"},
                        "logRecords": [
                            {
                                "timeUnixNano": now_ns,
                                "observedTimeUnixNano": now_ns,
                                "severityNumber": 9,
                                "severityText": "INFO",
                                "body": {"stringValue": f"pipeline verify canary {canary_id}"},
                                "attributes": [
                                    {"key": "event.action", "value": {"stringValue": "_pipeline_verify"}},
                                    {"key": "event.kind", "value": {"stringValue": "event"}},
                                    {"key": "event.outcome", "value": {"stringValue": "success"}},
                                    {"key": "event.dataset", "value": {"stringValue": CANARY_DATASET}},
                                    {"key": "gen_ai.operation.name", "value": {"stringValue": CANARY_SIGNAL_TYPE}},
                                    {"key": "gen_ai.agent_ext.verify_id", "value": {"stringValue": canary_id}},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _send_canary(endpoint: str, payload: dict[str, Any], *, timeout: int = 10) -> dict[str, Any]:
    """POST the canary log to OTLP/HTTP. Returns a transport status dict."""
    url = endpoint.rstrip("/") + "/v1/logs"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return {
                "ok": True,
                "status_code": response.status,
                "url": url,
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return {"ok": False, "status_code": exc.code, "url": url, "detail": detail[:500]}
    except urllib.error.URLError as exc:
        return {"ok": False, "status_code": None, "url": url, "detail": str(exc.reason)}


def _poll_elasticsearch(
    config: ESConfig,
    *,
    index_prefix: str,
    canary_id: str,
    attempts: int,
    backoff: float,
) -> dict[str, Any]:
    """Poll the data stream for the canary document. Returns the first hit or a miss summary."""
    ds_name = build_data_stream_name(index_prefix)
    # Search across the events data stream and any alerts data stream the bridge might write to.
    index_glob = f"{ds_name}*,{index_prefix}-*"
    query = {
        "size": 1,
        "query": {"term": {"gen_ai.agent_ext.verify_id": canary_id}},
        "sort": [{"@timestamp": {"order": "desc"}}],
    }
    wait = backoff
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            result = es_request(config, "POST", f"/{index_glob}/_search", query)
            hits = result.get("hits", {}).get("hits", [])
            if hits:
                hit = hits[0]
                return {
                    "found": True,
                    "attempt": attempt,
                    "index": hit.get("_index"),
                    "doc_id": hit.get("_id"),
                    "source_keys": sorted((hit.get("_source") or {}).keys())[:15],
                    "source": hit.get("_source"),
                }
        except SkillError as exc:
            last_error = str(exc)
        time.sleep(wait)
        wait *= 1.5
    return {"found": False, "attempts": attempts, "last_error": last_error}


def _classify_failure(
    *,
    send_result: dict[str, Any],
    poll_result: dict[str, Any],
    otlp_endpoint: str,
    ds_name: str,
    collector_log: Path | None,
) -> dict[str, Any]:
    """Turn a mixed send+poll outcome into a single actionable next step."""
    if not send_result.get("ok"):
        code = send_result.get("status_code")
        if code is None:
            preflight = _local_preflight(
                otlp_endpoint=otlp_endpoint,
                collector_log=collector_log,
            )
            return {
                "verdict": "transport_unreachable",
                "next_step": _unreachable_next_step(preflight, otlp_endpoint),
                "preflight": preflight,
            }
        return {
            "verdict": "transport_rejected",
            "next_step": (
                f"The OTLP/HTTP endpoint at `{otlp_endpoint}` rejected the canary with HTTP {code}. "
                f"Detail: {send_result.get('detail', '')[:200]}. "
                "This is almost always a wrong URL (should end in `/v1/logs`) or an auth/TLS mismatch on the Collector receiver. "
                "Fix the endpoint URL, or retry against the OTLP HTTP bridge (`:14319`) which has no auth."
            ),
        }

    if poll_result.get("found"):
        # Document is in ES. Check contract.
        source = poll_result.get("source") or {}
        dataset_ok = source.get("event.dataset") == CANARY_DATASET
        service_ok = bool(source.get("service.name"))
        if dataset_ok and service_ok:
            return {"verdict": "ok", "next_step": ""}
        missing = []
        if not dataset_ok:
            missing.append("`event.dataset`")
        if not service_ok:
            missing.append("`service.name`")
        return {
            "verdict": "contract_broken",
            "next_step": (
                "The canary reached Elasticsearch but is missing "
                f"{', '.join(missing)}. "
                "This means the ingest pipeline is not applying the ECS field shape. "
                "Re-run `bootstrap_observability.py --apply-es-assets` to refresh the pipeline, "
                f"then verify the ingest pipeline named `{ds_name.split('-')[0]}-normalize` exists "
                "and is referenced by the index template."
            ),
        }

    # Transport succeeded, doc never appeared. The classic last-mile failure.
    log_hint = ""
    if collector_log and collector_log.exists():
        tail = _tail_file(collector_log, 40)
        log_hint = (
            f"\n\nLast 40 lines of `{collector_log}`:\n```\n{tail}\n```\n"
            "Look for `exporter` errors, `flush` timeouts, or `mapping_parsing_exception`."
        )
    return {
        "verdict": "sent_but_lost",
        "next_step": (
            "Transport returned 2xx but the canary never landed in Elasticsearch. "
            "This is the known Collector->ES exporter failure mode. Do this in order:\n"
            "  1. Check the Collector log for `elasticsearch` exporter errors (connection refused, auth failure, mapping rejection).\n"
            "  2. Confirm ES credentials the Collector is using — a silently wrong password shows up here, not in OTLP receive.\n"
            "  3. If still failing, switch traffic to the OTLP HTTP bridge at `http://127.0.0.1:14319` as the reliable path. "
            "     That bridge writes to the same data stream, so dashboards keep working. Re-run verify against the bridge URL to confirm."
            + log_hint
        ),
    }


def _tail_file(path: Path, n: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return "".join(lines[-n:])
    except OSError as exc:
        return f"(could not read {path}: {exc})"


def _run_cmd(cmd: list[str], timeout: float = 3.0) -> str:
    """Best-effort command runner. Never raises. Empty string if the tool is absent."""
    import shutil
    import subprocess

    if not shutil.which(cmd[0]):
        return ""
    try:
        out = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return (out.stdout or "") + (out.stderr or "")
    except Exception:  # noqa: BLE001
        return ""


def _local_preflight(*, otlp_endpoint: str, collector_log: Path | None) -> dict[str, Any]:
    """Collect local-host diagnostics when OTLP transport is unreachable.

    All probes are best-effort: missing tools return empty strings; the function
    never raises. The goal is to let a driving agent see zombie collectors,
    unlistened ports, or crash logs without having to spawn its own shell.
    """
    import re

    # Extract the port the agent is trying to hit (default 4318/14319 if we can't tell).
    port = ""
    match = re.search(r":(\d+)(?:/|$)", otlp_endpoint)
    if match:
        port = match.group(1)
    known_ports = sorted({p for p in [port, *KNOWN_OTLP_PORTS] if p})

    # ps: look for otelcol-contrib / otelcol / bridge processes and surface zombies.
    ps_out = _run_cmd(["ps", "-eo", "stat,pid,ppid,comm,args"])
    otel_lines: list[str] = []
    zombies: list[str] = []
    if ps_out:
        for line in ps_out.splitlines():
            lowered = line.lower()
            if "otelcol" in lowered or "otlphttpbridge" in lowered or "opentelemetry" in lowered:
                otel_lines.append(line.strip())
                # stat beginning with Z = zombie; Linux defunct entries also match "<defunct>"
                stat = line.strip().split()[0] if line.strip() else ""
                if stat.startswith("Z") or "<defunct>" in line:
                    zombies.append(line.strip())

    # Listening ports: prefer ss, fallback to lsof, then netstat.
    listen_probe = _run_cmd(["ss", "-lntp"])
    if not listen_probe:
        listen_probe = _run_cmd(["lsof", "-iTCP", "-sTCP:LISTEN", "-nP"])
    if not listen_probe:
        listen_probe = _run_cmd(["netstat", "-lntp"])
    listening: dict[str, bool] = {p: False for p in known_ports}
    if listen_probe:
        for p in known_ports:
            # Match :<port> boundary on word end to avoid 43180 vs 4318 ambiguity.
            pattern = re.compile(rf"[.:]{re.escape(p)}\b")
            listening[p] = bool(pattern.search(listen_probe))

    log_tail = ""
    if collector_log and collector_log.exists():
        log_tail = _tail_file(collector_log, 30)

    return {
        "otel_processes": otel_lines[:10],
        "zombie_processes": zombies[:5],
        "listening_ports": listening,
        "probed_ports": known_ports,
        "collector_log_tail": log_tail,
    }


def _unreachable_next_step(preflight: dict[str, Any], otlp_endpoint: str) -> str:
    zombies = preflight.get("zombie_processes") or []
    listening = preflight.get("listening_ports") or {}
    any_listening = any(listening.values())
    lines: list[str] = [f"Cannot reach `{otlp_endpoint}/v1/logs`."]

    if zombies:
        lines.append(
            "Detected zombie/defunct Collector processes: "
            + "; ".join(zombies[:2])
            + ". The parent that launched them has exited, so the OTLP listener is gone even though the process table still shows entries. "
            "Reap and relaunch: `pkill -9 -f otelcol-contrib` then restart with `run-collector.sh` under a proper supervisor (systemd / nohup with disown / a tmux session)."
        )
    elif not any_listening:
        lines.append(
            "No listener on any of the expected OTLP ports "
            f"({', '.join(preflight.get('probed_ports', []))}). "
            "The Collector or bridge simply isn't running. Start it via `run-collector.sh` or `run-otlphttpbridge.sh` and re-verify."
        )
    else:
        live = [p for p, ok in listening.items() if ok]
        lines.append(
            f"Ports {live} are listening but `{otlp_endpoint}` is not reachable from here. "
            "Likely causes: wrong host (127.0.0.1 vs container gateway), wrong port in the agent's `OTEL_EXPORTER_OTLP_ENDPOINT`, or a local firewall. "
            "Point the agent at a port that is actually listening."
        )

    tail = preflight.get("collector_log_tail") or ""
    if tail:
        lines.append("\nLast 30 lines of Collector log:")
        lines.append("```\n" + tail + "\n```")
    return " ".join(lines[:3]) + ("\n\n" + "\n".join(lines[3:]) if len(lines) > 3 else "")


def render_text(verdict: dict[str, Any]) -> str:
    status = verdict["verdict"]
    lines = [f"[{status.upper()}] pipeline verify"]
    lines.append(f"  otlp-http-endpoint: {verdict['otlp_endpoint']}")
    lines.append(f"  data stream:       {verdict['data_stream']}")
    lines.append(f"  canary id:         {verdict['canary_id']}")
    if verdict.get("send"):
        send = verdict["send"]
        lines.append(f"  send:              status={send.get('status_code')} ok={send.get('ok')}")
    if verdict.get("poll"):
        poll = verdict["poll"]
        if poll.get("found"):
            lines.append(f"  poll:              found on attempt {poll.get('attempt')} in index `{poll.get('index')}`")
        else:
            lines.append(f"  poll:              not found after {poll.get('attempts')} attempts")
    preflight = verdict.get("preflight")
    if preflight:
        listening = preflight.get("listening_ports", {})
        live = [p for p, ok in listening.items() if ok]
        dead = [p for p, ok in listening.items() if not ok]
        lines.append(f"  listening ports:   live={live or '[]'} not-listening={dead or '[]'}")
        zombies = preflight.get("zombie_processes") or []
        if zombies:
            lines.append(f"  zombie collectors: {len(zombies)} (first: {zombies[0][:120]})")
    if verdict.get("next_step"):
        lines.append("")
        lines.append("Next step:")
        lines.append(verdict["next_step"])
    return "\n".join(lines)


def run_verify(
    args: argparse.Namespace | None = None,
    *,
    es_url: str | None = None,
    es_user: str = "",
    es_password: str = "",
    index_prefix: str = "agent-obsv",
    otlp_http_endpoint: str = "http://127.0.0.1:14319",
    service_name: str = "pipeline-verify",
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_backoff: float = DEFAULT_POLL_BACKOFF,
    no_verify_tls: bool = False,
    collector_log: str = "",
) -> dict[str, Any]:
    """Run the end-to-end verify. Accepts either kwargs or a Namespace.

    Callers that already have a Namespace (the CLI ``main()``) can keep
    passing it; internal callers (``doctor._probe_canary``,
    ``bootstrap_observability``) should use kwargs and stop constructing
    argparse.Namespace objects just to satisfy the old signature.
    """
    if args is not None:
        # Back-compat path. Pull everything off the namespace and recurse via
        # kwargs so the real body lives in exactly one place.
        return run_verify(
            es_url=args.es_url,
            es_user=args.es_user,
            es_password=args.es_password,
            index_prefix=args.index_prefix,
            otlp_http_endpoint=args.otlp_http_endpoint,
            service_name=getattr(args, "service_name", "pipeline-verify"),
            poll_attempts=getattr(args, "poll_attempts", DEFAULT_POLL_ATTEMPTS),
            poll_backoff=getattr(args, "poll_backoff", DEFAULT_POLL_BACKOFF),
            no_verify_tls=getattr(args, "no_verify_tls", False),
            collector_log=getattr(args, "collector_log", "") or "",
        )
    if es_url is None:
        raise SkillError("run_verify requires es_url when args is not provided")
    credentials = validate_credential_pair(es_user, es_password)
    config = ESConfig(
        es_url=es_url,
        es_user=credentials[0] if credentials else None,
        es_password=credentials[1] if credentials else None,
        verify_tls=not no_verify_tls,
    )
    resolved_prefix = validate_index_prefix(index_prefix)
    ds_name = build_data_stream_name(resolved_prefix)
    canary_id = f"verify-{uuid.uuid4().hex[:12]}"

    payload = _build_canary_log(service_name=service_name, canary_id=canary_id)
    send = _send_canary(otlp_http_endpoint, payload)
    poll: dict[str, Any] = {"found": False, "attempts": 0}
    if send.get("ok"):
        poll = _poll_elasticsearch(
            config,
            index_prefix=resolved_prefix,
            canary_id=canary_id,
            attempts=poll_attempts,
            backoff=poll_backoff,
        )

    classification = _classify_failure(
        send_result=send,
        poll_result=poll,
        otlp_endpoint=otlp_http_endpoint,
        ds_name=ds_name,
        collector_log=Path(collector_log).expanduser().resolve() if collector_log else None,
    )
    return {
        "canary_id": canary_id,
        "otlp_endpoint": otlp_http_endpoint,
        "data_stream": ds_name,
        "send": send,
        "poll": poll,
        "verdict": classification["verdict"],
        "next_step": classification["next_step"],
        "preflight": classification.get("preflight"),
    }


def main() -> int:
    try:
        args = parse_args()
        result = run_verify(
            es_url=args.es_url,
            es_user=args.es_user,
            es_password=args.es_password,
            index_prefix=args.index_prefix,
            otlp_http_endpoint=args.otlp_http_endpoint,
            service_name=args.service_name,
            poll_attempts=args.poll_attempts,
            poll_backoff=args.poll_backoff,
            no_verify_tls=args.no_verify_tls,
            collector_log=args.collector_log,
        )
        print(render_text(result))
        if args.output:
            Path(args.output).expanduser().resolve().write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print_info(f"verdict written: {args.output}")
        verdict = result["verdict"]
        if verdict == "ok":
            return 0
        if verdict in {"contract_broken", "sent_but_lost"}:
            return 2
        return 1
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Verify failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
