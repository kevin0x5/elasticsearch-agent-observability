#!/usr/bin/env python3
"""Report what bootstrap_observability has currently deployed on a cluster.

This closes the operator-loop gap: after bootstrap you have generated files on
disk, but the only way to know the cluster's actual state was to ``curl`` it by
hand. This script queries each asset the skill is supposed to manage and reports
``present``, ``absent``, or ``error``, plus a handful of health signals.

Exit codes mirror ``verify_pipeline`` so automation can key off them:

- ``0`` — everything expected is present
- ``2`` — some assets missing or degraded (the loud middle state)
- ``1`` — could not reach ES at all
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from common import (
    ESConfig,
    OBSERVER_PRODUCT_TAG,
    SkillError,
    asset_names,
    es_request,
    print_error,
    validate_credential_pair,
    validate_index_prefix,
)
from uninstall import _extract_meta_product


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report current deployment status for the observability stack")
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--es-user", default="")
    parser.add_argument("--es-password", default="")
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--no-verify-tls", action="store_true")
    parser.add_argument("--output-format", choices=["text", "json"], default="text")
    return parser.parse_args()


# Assets we spot-check for ownership. Data streams and ILM/pipeline/templates
# are the resources where a foreign install could silently collide on the
# prefixed name — ``status`` reporting ``ready`` on those would mislead an
# operator into thinking the skill is installed correctly when in reality it
# is somebody else's. We piggy-back on the same parser uninstall uses so the
# two scripts cannot drift.
_OWNERSHIP_AWARE_ASSETS = {
    "ilm_policy",
    "ingest_pipeline",
    "component_template_ecs_base",
    "component_template_settings",
    "index_template",
}


def _probe(config: ESConfig, path: str, *, asset: str | None = None) -> tuple[str, str, str]:
    """GET probe that also returns ownership when an asset name is given.

    Returns ``(status, detail, owner)`` where status is one of:
    ``present`` (ours), ``foreign`` (exists but tagged differently),
    ``untagged`` (exists, predates _meta tagging — treat as degraded),
    ``absent`` or ``error``. When ``asset`` is ``None`` we keep the old
    presence-only behaviour.
    """
    try:
        response = es_request(config, "GET", path)
    except SkillError as exc:
        msg = str(exc)
        if "404" in msg or "not_found" in msg.lower():
            return ("absent", "", "")
        return ("error", msg, "")
    if asset is None or asset not in _OWNERSHIP_AWARE_ASSETS:
        return ("present", "", "")
    owner_status, owner = _extract_meta_product(asset, path, response)
    if owner_status == "ours":
        return ("present", "", owner)
    if owner_status == "foreign":
        return (
            "foreign",
            f"exists but tagged `{owner}`, not `{OBSERVER_PRODUCT_TAG}`",
            owner,
        )
    if owner_status == "tag_missing":
        return (
            "untagged",
            "exists without a _meta.product tag (legacy install?)",
            "",
        )
    if owner_status == "absent":
        return ("absent", "", "")
    return ("error", owner, "")


def _data_stream_health(config: ESConfig, name: str) -> dict[str, Any]:
    """Return a compact health snapshot for the events data stream."""
    try:
        response = es_request(config, "GET", f"/_data_stream/{name}")
    except SkillError as exc:
        if "404" in str(exc):
            return {"status": "absent"}
        return {"status": "error", "detail": str(exc)}
    streams = response.get("data_streams") or []
    if not streams:
        return {"status": "absent"}
    ds = streams[0]
    indices = ds.get("indices") or []
    # Grab doc count via _count — cheap and doesn't need aggregation perms.
    try:
        count_resp = es_request(config, "GET", f"/{name}/_count")
        doc_count = int(count_resp.get("count", 0) or 0)
    except SkillError:
        doc_count = -1
    return {
        "status": "present",
        "generation": ds.get("generation"),
        "template": ds.get("template"),
        "backing_indices": len(indices),
        "write_index": (indices[-1].get("index_name") if indices else None),
        "doc_count": doc_count,
    }


def run_status(config: ESConfig, *, index_prefix: str) -> dict[str, Any]:
    names = asset_names(index_prefix)

    # Fail fast if ES is unreachable — every probe would otherwise time out.
    es_request(config, "GET", "/")

    checks: list[dict[str, str]] = []
    for label, path in [
        ("ilm_policy", f"/_ilm/policy/{names['ilm_policy']}"),
        ("ingest_pipeline", f"/_ingest/pipeline/{names['ingest_pipeline']}"),
        ("component_template_ecs_base", f"/_component_template/{names['component_template_ecs_base']}"),
        ("component_template_settings", f"/_component_template/{names['component_template_settings']}"),
        ("index_template", f"/_index_template/{names['index_template']}"),
    ]:
        status_value, detail, owner = _probe(config, path, asset=label)
        entry: dict[str, str] = {"asset": label, "status": status_value}
        if detail:
            entry["detail"] = detail
        if owner:
            entry["owner"] = owner
        checks.append(entry)

    data_stream = _data_stream_health(config, names["data_stream"])

    missing = [c["asset"] for c in checks if c["status"] == "absent"]
    errored = [c["asset"] for c in checks if c["status"] == "error"]
    foreign = [c["asset"] for c in checks if c["status"] == "foreign"]
    untagged = [c["asset"] for c in checks if c["status"] == "untagged"]
    if data_stream.get("status") == "absent":
        missing.append("data_stream")
    if data_stream.get("status") == "error":
        errored.append("data_stream")

    if errored:
        overall = "error"
    elif foreign:
        # Foreign ownership is worse than "missing" — deleting via uninstall
        # would refuse (correctly), and operators should be told that another
        # product is squatting on these names before they proceed.
        overall = "foreign"
    elif missing or untagged:
        overall = "degraded"
    else:
        overall = "ready"

    return {
        "index_prefix": index_prefix,
        "overall": overall,
        "checks": checks,
        "data_stream": data_stream,
        "missing": missing,
        "errored": errored,
        "foreign": foreign,
        "untagged": untagged,
    }


def render_text(result: dict[str, Any]) -> str:
    icons = {
        "present": "✓",
        "absent": "✗",
        "error": "!",
        "foreign": "⚠",
        "untagged": "?",
        "ready": "✓",
        "degraded": "✗",
        "unknown": "?",
    }
    lines = [f"[{icons.get(result['overall'], '?')} {result['overall'].upper()}] prefix=`{result['index_prefix']}`"]
    for check in result["checks"]:
        icon = icons.get(check["status"], "?")
        line = f"  {icon} {check['asset']}: {check['status']}"
        if check.get("owner"):
            line += f"  [owner={check['owner']}]"
        if check.get("detail"):
            line += f"  — {check['detail']}"
        lines.append(line)
    ds = result["data_stream"]
    if ds.get("status") == "present":
        lines.append(
            f"  ✓ data_stream: present  (backing_indices={ds.get('backing_indices')}, "
            f"doc_count={ds.get('doc_count')}, write_index={ds.get('write_index')})"
        )
    elif ds.get("status") == "absent":
        lines.append("  ✗ data_stream: absent")
    else:
        lines.append(f"  ! data_stream: error — {ds.get('detail', 'unknown')}")
    if result.get("foreign"):
        lines.append("")
        lines.append(f"Foreign: {', '.join(result['foreign'])}")
        lines.append("Another product owns these names; uninstall will refuse by design.")
    if result.get("untagged"):
        lines.append("")
        lines.append(f"Untagged: {', '.join(result['untagged'])}")
        lines.append("Legacy install detected (no _meta.product). Re-run bootstrap to re-tag.")
    if result["missing"]:
        lines.append("")
        lines.append(f"Missing: {', '.join(result['missing'])}")
        lines.append("Run `scripts/bootstrap_observability.py --apply-es-assets ...` to (re)install.")
    return "\n".join(lines)


def main() -> int:
    try:
        args = parse_args()
        credentials = validate_credential_pair(args.es_user, args.es_password)
        config = ESConfig(
            es_url=args.es_url,
            es_user=credentials[0] if credentials else None,
            es_password=credentials[1] if credentials else None,
            verify_tls=not args.no_verify_tls,
        )
        index_prefix = validate_index_prefix(args.index_prefix)
        result = run_status(config, index_prefix=index_prefix)

        if args.output_format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(render_text(result))

        if result["overall"] == "ready":
            return 0
        if result["overall"] == "degraded":
            return 2
        # foreign / error → 1 (loud failure)
        return 1
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Status check failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
