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
                                    {"key": "gen_ai.agent.signal_type", "value": {"stringValue": CANARY_SIGNAL_TYPE}},
                                    {"key": "gen_ai.agent.verify_id", "value": {"stringValue": canary_id}},
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
        "query": {"term": {"gen_ai.agent.verify_id": canary_id}},
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
            return {
                "verdict": "transport_unreachable",
                "next_step": (
                    f"Cannot reach `{otlp_endpoint}/v1/logs`. "
                    "Check whether the Collector or the OTLP HTTP bridge is actually listening "
                    "(`ss -lntp | grep -E '4318|14319'` or `lsof -iTCP -sTCP:LISTEN | grep -E '4318|14319'`). "
                    "If you are targeting the Collector and it is not up, start it via `run-collector.sh`; "
                    "if you are targeting the bridge, start it via `run-otlphttpbridge.sh`."
                ),
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
    if verdict.get("next_step"):
        lines.append("")
        lines.append("Next step:")
        lines.append(verdict["next_step"])
    return "\n".join(lines)


def run_verify(args: argparse.Namespace) -> dict[str, Any]:
    credentials = validate_credential_pair(args.es_user, args.es_password)
    config = ESConfig(
        es_url=args.es_url,
        es_user=credentials[0] if credentials else None,
        es_password=credentials[1] if credentials else None,
        verify_tls=not args.no_verify_tls,
    )
    index_prefix = validate_index_prefix(args.index_prefix)
    ds_name = build_data_stream_name(index_prefix)
    canary_id = f"verify-{uuid.uuid4().hex[:12]}"

    payload = _build_canary_log(service_name=args.service_name, canary_id=canary_id)
    send = _send_canary(args.otlp_http_endpoint, payload)
    poll: dict[str, Any] = {"found": False, "attempts": 0}
    if send.get("ok"):
        poll = _poll_elasticsearch(
            config,
            index_prefix=index_prefix,
            canary_id=canary_id,
            attempts=args.poll_attempts,
            backoff=args.poll_backoff,
        )

    classification = _classify_failure(
        send_result=send,
        poll_result=poll,
        otlp_endpoint=args.otlp_http_endpoint,
        ds_name=ds_name,
        collector_log=Path(args.collector_log).expanduser().resolve() if args.collector_log else None,
    )
    return {
        "canary_id": canary_id,
        "otlp_endpoint": args.otlp_http_endpoint,
        "data_stream": ds_name,
        "send": send,
        "poll": poll,
        "verdict": classification["verdict"],
        "next_step": classification["next_step"],
    }


def main() -> int:
    try:
        args = parse_args()
        result = run_verify(args)
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
