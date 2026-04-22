#!/usr/bin/env python3
"""Remove Elasticsearch and Kibana assets created by bootstrap_observability.

Only asset names that match the prefix convention are touched, so running this
against a cluster that also hosts unrelated workloads is safe. The default
mode is dry-run — nothing is deleted unless ``--confirm`` is passed.

Ordering matters. Data streams must be deleted before their backing index
template (ES will refuse otherwise), and the ingest pipeline must go after
the template stops referencing it. We do it in this order:

    data stream -> index template -> component templates -> ingest pipeline
    -> ILM policy -> (optional) Kibana saved objects

Nothing else, and definitely not ``_all``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from urllib.parse import quote

from apply_elasticsearch_assets import kibana_request
from common import (
    ESConfig,
    OBSERVER_PRODUCT_TAG,
    SkillError,
    asset_names,
    es_request,
    print_error,
    print_info,
    read_json,
    validate_credential_pair,
    validate_index_prefix,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remove bootstrap_observability assets from a cluster")
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--es-user", default="")
    parser.add_argument("--es-password", default="")
    parser.add_argument("--index-prefix", default="agent-obsv")
    parser.add_argument("--kibana-url", default="", help="Also remove Kibana saved objects when provided")
    parser.add_argument("--kibana-space", default="default")
    parser.add_argument(
        "--kibana-assets-file",
        default="",
        help="Path to the generated kibana-saved-objects.json. Required to remove Kibana objects.",
    )
    parser.add_argument("--no-verify-tls", action="store_true")
    parser.add_argument("--kibana-api-key", default="")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete. Without this flag the script prints the plan and exits.",
    )
    parser.add_argument(
        "--keep-data-stream",
        action="store_true",
        help="Keep the data stream (and its indexed documents). Useful when you only want to rerender assets.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Skip the ownership check and delete regardless of _meta.product. Only use this when you know the "
            "resources predate the _meta tagging (bootstrap before this version) or belong to a renamed fork."
        ),
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Ownership check
# ---------------------------------------------------------------------------
#
# Names alone are not a safety barrier: someone could install a foreign ILM
# policy called ``agent-obsv-lifecycle`` and our prefix-based delete would
# happily nuke it. To prevent that, every asset we render carries
# ``_meta.product = "elasticsearch-agent-observability"``. Before deleting
# we GET the resource and verify the tag. Mismatches are skipped with
# ``refused_foreign``; missing resources are ``already_absent``; resources
# that predate tagging return ``tag_missing`` and are also refused unless
# ``--force`` was passed (rerun bootstrap once to write the tag, then
# uninstall without --force).

_OWNERSHIP_PATHS: dict[str, str] = {
    "data_stream": "data_streams",           # no _meta on data_streams; fall back to backing-index presence
    "index_template": "index_templates",
    "component_template_ecs_base": "component_templates",
    "component_template_settings": "component_templates",
    "ingest_pipeline": "pipelines",
    "ilm_policy": "ilm_policy",
}


def _extract_meta_product(asset: str, path: str, response: dict) -> tuple[str, str]:
    """Return (status, owner) for a GET response on a managed resource.

    Status is one of: ``ours`` | ``foreign`` | ``tag_missing`` | ``absent``.

    This function tolerates shape surprises (empty dict, nested non-dict values
    from mocked responses or partial clusters): anything we cannot parse
    reduces to ``tag_missing`` rather than raising.
    """
    def _as_dict(v: Any) -> dict:
        return v if isinstance(v, dict) else {}

    if not isinstance(response, dict):
        return ("tag_missing", "")

    # ILM has the nested shape: {"<name>": {"policy": {"_meta": {...}}}}
    if asset == "ilm_policy":
        body = _as_dict(next(iter(response.values()), {}))
        meta = _as_dict(_as_dict(body.get("policy")).get("_meta"))
    elif asset == "ingest_pipeline":
        # {"<name>": {"_meta": {...}, "processors": [...]}}
        body = _as_dict(next(iter(response.values()), {}))
        meta = _as_dict(body.get("_meta"))
    elif asset == "index_template":
        # {"index_templates": [{"name": ..., "index_template": {"_meta": {...}}}]}
        templates = response.get("index_templates") or []
        body = _as_dict((templates[0] if templates else {})).get("index_template")
        meta = _as_dict(_as_dict(body).get("_meta"))
    elif asset.startswith("component_template"):
        templates = response.get("component_templates") or []
        body = _as_dict((templates[0] if templates else {})).get("component_template")
        meta = _as_dict(_as_dict(body).get("_meta"))
    elif asset == "data_stream":
        # Data streams don't carry _meta directly in the response shape we rely
        # on, but they are identified unambiguously by name + presence under
        # our managed index template. We accept presence as enough.
        streams = response.get("data_streams") or []
        if streams:
            return ("ours", OBSERVER_PRODUCT_TAG)
        return ("absent", "")
    else:
        meta = {}

    owner = str(meta.get("product") or "").strip()
    if not owner:
        return ("tag_missing", "")
    if owner == OBSERVER_PRODUCT_TAG:
        return ("ours", owner)
    return ("foreign", owner)


def _check_ownership(config: ESConfig, asset: str, path: str) -> tuple[str, str]:
    """GET the resource and classify its ownership.

    Returns ``("ours" | "foreign" | "tag_missing" | "absent", owner_or_error)``.
    """
    try:
        response = es_request(config, "GET", path)
    except SkillError as exc:
        msg = str(exc)
        if "404" in msg or "not_found" in msg.lower() or "index_not_found" in msg:
            return ("absent", "")
        return ("error", msg)
    return _extract_meta_product(asset, path, response)


def _delete(config: ESConfig, path: str) -> tuple[str, str]:
    """DELETE helper that treats 404 as a benign already-gone outcome."""
    try:
        es_request(config, "DELETE", path)
        return ("deleted", "")
    except SkillError as exc:
        msg = str(exc)
        if "404" in msg or "not_found" in msg.lower() or "index_not_found" in msg:
            return ("already_absent", "")
        return ("failed", msg)


def _plan(names: dict[str, str], *, keep_data_stream: bool) -> list[dict[str, str]]:
    plan: list[dict[str, str]] = []
    if not keep_data_stream:
        plan.append({"action": "DELETE", "path": f"/_data_stream/{names['data_stream']}", "asset": "data_stream"})
    plan.extend(
        [
            {"action": "DELETE", "path": f"/_index_template/{names['index_template']}", "asset": "index_template"},
            {"action": "DELETE", "path": f"/_component_template/{names['component_template_ecs_base']}", "asset": "component_template_ecs_base"},
            {"action": "DELETE", "path": f"/_component_template/{names['component_template_settings']}", "asset": "component_template_settings"},
            {"action": "DELETE", "path": f"/_ingest/pipeline/{names['ingest_pipeline']}", "asset": "ingest_pipeline"},
            {"action": "DELETE", "path": f"/_ilm/policy/{names['ilm_policy']}", "asset": "ilm_policy"},
        ]
    )
    return plan


def run_uninstall(
    config: ESConfig,
    *,
    index_prefix: str,
    confirm: bool,
    keep_data_stream: bool,
    kibana_url: str,
    kibana_space: str,
    kibana_assets_file: str,
    force: bool = False,
) -> dict[str, Any]:
    names = asset_names(index_prefix)
    plan = _plan(names, keep_data_stream=keep_data_stream)

    kibana_objects: list[dict[str, str]] = []
    if kibana_url and kibana_assets_file:
        bundle = read_json(Path(kibana_assets_file).expanduser().resolve())
        for obj in bundle.get("objects", []) or []:
            otype = str(obj.get("type", "")).strip()
            oid = str(obj.get("id", "")).strip()
            if otype and oid:
                kibana_objects.append({"type": otype, "id": oid})

    if not confirm:
        return {
            "dry_run": True,
            "index_prefix": index_prefix,
            "plan": plan,
            "kibana_objects": kibana_objects,
            "force": force,
        }

    results: list[dict[str, str]] = []
    for step in plan:
        asset = step["asset"]
        # Derive the GET path for ownership probing. DELETE /_data_stream/foo
        # => GET /_data_stream/foo, etc. Same path, different verb.
        get_path = step["path"]
        owner_status, owner_detail = ("bypassed", "") if force else _check_ownership(config, asset, get_path)

        if owner_status == "absent":
            results.append({**step, "status": "already_absent", "owner": owner_detail})
            continue
        if owner_status == "error":
            results.append({**step, "status": "failed", "detail": owner_detail})
            continue
        if owner_status == "foreign":
            results.append(
                {
                    **step,
                    "status": "refused_foreign",
                    "owner": owner_detail,
                    "detail": (
                        f"refused to delete `{asset}` — it is tagged as owned by `{owner_detail}`, "
                        f"not `{OBSERVER_PRODUCT_TAG}`. Rename your --index-prefix, or pass --force if you are sure."
                    ),
                }
            )
            continue
        if owner_status == "tag_missing":
            results.append(
                {
                    **step,
                    "status": "refused_untagged",
                    "detail": (
                        f"refused to delete `{asset}` — it carries no _meta.product tag. "
                        "This usually means it was installed by an older version of the skill. "
                        "Re-run `bootstrap_observability.py --apply-es-assets` once to re-tag it, "
                        "then uninstall again. Pass --force to delete anyway."
                    ),
                }
            )
            continue
        # owner_status is "ours" or "bypassed"
        status, detail = _delete(config, step["path"])
        results.append({**step, "status": status, "detail": detail, "owner": owner_detail})

    kibana_results: list[dict[str, str]] = []
    if kibana_url and kibana_objects:
        space_prefix = "" if kibana_space == "default" else f"/s/{quote(kibana_space, safe='')}"
        for obj in kibana_objects:
            path = f"{space_prefix}/api/saved_objects/{quote(obj['type'], safe='')}/{quote(obj['id'], safe='')}"
            try:
                kibana_request(config, kibana_url, "DELETE", path)
                kibana_results.append({**obj, "status": "deleted"})
            except SkillError as exc:
                msg = str(exc)
                if "404" in msg:
                    kibana_results.append({**obj, "status": "already_absent"})
                else:
                    kibana_results.append({**obj, "status": "failed", "detail": msg})

    return {
        "index_prefix": index_prefix,
        "results": results,
        "kibana_results": kibana_results,
        "force": force,
    }


def main() -> int:
    try:
        args = parse_args()
        credentials = validate_credential_pair(args.es_user, args.es_password)
        config = ESConfig(
            es_url=args.es_url,
            es_user=credentials[0] if credentials else None,
            es_password=credentials[1] if credentials else None,
            verify_tls=not args.no_verify_tls,
            kibana_api_key=args.kibana_api_key.strip() or None,
        )
        index_prefix = validate_index_prefix(args.index_prefix)
        summary = run_uninstall(
            config,
            index_prefix=index_prefix,
            confirm=args.confirm,
            keep_data_stream=args.keep_data_stream,
            kibana_url=args.kibana_url.strip(),
            kibana_space=args.kibana_space,
            kibana_assets_file=args.kibana_assets_file.strip(),
            force=args.force,
        )
        if summary.get("dry_run"):
            print(f"🔍 Dry-run uninstall plan for `{index_prefix}` ({len(summary['plan'])} ES op(s)):")
            for step in summary["plan"]:
                print(f"   {step['action']} {step['path']}  ({step['asset']})")
            if summary["kibana_objects"]:
                print(f"   plus {len(summary['kibana_objects'])} Kibana saved object(s)")
            if args.force:
                print_info("--force is set: ownership check will be bypassed at --confirm time.")
            else:
                print_info("Ownership of each asset will be checked via _meta.product before delete.")
            print_info("Re-run with --confirm to actually delete.")
            return 0

        print(f"✅ Uninstall for `{index_prefix}`:")
        bad = 0
        refused = 0
        for item in summary["results"]:
            status = item["status"]
            if status in {"deleted", "already_absent"}:
                icon = "✓"
            elif status in {"refused_foreign", "refused_untagged"}:
                icon = "⊘"
                refused += 1
            else:
                icon = "✗"
            owner = f" [owner={item['owner']}]" if item.get("owner") and item["owner"] != OBSERVER_PRODUCT_TAG else ""
            line = f"   {icon} {item['asset']}: {status}{owner}"
            if item.get("detail"):
                line += f"  — {item['detail']}"
            print(line)
            if status == "failed":
                bad += 1
        for item in summary.get("kibana_results", []):
            icon = "✓" if item["status"] in {"deleted", "already_absent"} else "✗"
            label = f"kibana:{item['type']}/{item['id']}"
            line = f"   {icon} {label}: {item['status']}"
            if item.get("detail"):
                line += f"  — {item['detail']}"
            print(line)
            if item["status"] == "failed":
                bad += 1
        if refused:
            print_info(f"{refused} asset(s) refused for safety. Use --force to override (know what you're doing).")
        return 0 if bad == 0 else 2
    except SkillError as exc:
        print_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print_error(f"Uninstall failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
