#!/usr/bin/env python3
"""Shared helpers for elasticsearch-agent-observability."""

from __future__ import annotations

import base64
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GENERATED_DIR = ROOT_DIR / "generated"
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".sh", ".js", ".ts", ".tsx", ".jsx",
    ".go", ".java", ".kt", ".kts", ".scala", ".rs", ".rb", ".cs", ".swift",
    ".html", ".css", ".scss", ".vue", ".svelte",
    ".c", ".cpp", ".h", ".hpp",
    ".env", ".properties", ".gradle",
}
IGNORE_DIR_NAMES = {
    ".git", ".codebuddy", "node_modules", "vendor", "dist", "build", "coverage", "__pycache__", ".idea", ".vscode",
    "generated", "references", "tests", "assets", ".pytest_cache",
}
INDEX_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._\-]{1,63}$")


class SkillError(Exception):
    """User-facing skill error."""


@dataclass
class ESConfig:
    es_url: str
    es_user: str | None = None
    es_password: str | None = None
    timeout_seconds: int = 15
    verify_tls: bool = True
    kibana_api_key: str | None = None
    max_retries: int = 2  # total attempts = max_retries + 1
    retry_backoff_seconds: float = 0.5


IDEMPOTENT_METHODS = {"GET", "HEAD", "PUT", "DELETE"}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def build_ssl_context(verify_tls: bool):
    """Return an ssl.SSLContext with verification disabled when requested, else None.

    Centralised so every HTTP caller (ES, Kibana, verify canary) shares one
    policy and we don't sprinkle `ssl.CERT_NONE` across modules.
    """
    if verify_tls:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SkillError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SkillError(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, payload: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    path.write_text(text, encoding="utf-8")


def read_text_file(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise SkillError(f"Unable to decode text file: {path}")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def slugify(value: str, fallback: str = "item") -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or fallback


def safe_relative(path: Path, base: Path | None = None) -> str:
    base = base or ROOT_DIR
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def validate_workspace_dir(path: Path, label: str = "Workspace") -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise SkillError(f"{label} not found: {resolved}")
    if not resolved.is_dir():
        raise SkillError(f"{label} must be a directory: {resolved}")
    return resolved


def validate_positive_int(value: int, label: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if value < minimum:
        raise SkillError(f"{label} must be >= {minimum}, got: {value}")
    if maximum is not None and value > maximum:
        raise SkillError(f"{label} must be <= {maximum}, got: {value}")
    return value


def validate_index_prefix(value: str) -> str:
    prefix = value.strip().lower()
    if not INDEX_PREFIX_PATTERN.fullmatch(prefix):
        raise SkillError(
            "Index prefix must start with a lowercase letter or digit and contain only lowercase letters, digits, '.', '_' or '-'."
        )
    return prefix


def validate_credential_pair(user: str | None, password: str | None) -> tuple[str, str] | None:
    normalized_user = (user or "").strip()
    normalized_password = (password or "").strip()
    if bool(normalized_user) ^ bool(normalized_password):
        raise SkillError("--es-user and --es-password must be provided together")
    if not normalized_user:
        return None
    return normalized_user, normalized_password


def build_events_alias(index_prefix: str) -> str:
    return f"{index_prefix}-events"


def build_data_stream_name(index_prefix: str, signal_type: str = "events") -> str:
    return f"{index_prefix}-{signal_type}"


def build_component_template_name(index_prefix: str, component: str) -> str:
    return f"{index_prefix}-{component}"


def build_index_template_name(index_prefix: str) -> str:
    return f"{index_prefix}-events-template"


def build_ingest_pipeline_name(index_prefix: str) -> str:
    return f"{index_prefix}-normalize"


def build_ilm_policy_name(index_prefix: str) -> str:
    return f"{index_prefix}-lifecycle"


def asset_names(index_prefix: str) -> dict[str, str]:
    """Return the canonical ES asset names for a given index prefix.

    Centralising the naming contract lets uninstall/status scripts stay in sync
    with apply without copy-pasting string templates.
    """
    return {
        "index_template": build_index_template_name(index_prefix),
        "ingest_pipeline": build_ingest_pipeline_name(index_prefix),
        "ilm_policy": build_ilm_policy_name(index_prefix),
        "component_template_ecs_base": build_component_template_name(index_prefix, "ecs-base"),
        "component_template_settings": build_component_template_name(index_prefix, "settings"),
        "data_stream": build_data_stream_name(index_prefix),
        "events_alias": build_events_alias(index_prefix),
    }


def iter_text_files(workspace: Path, max_files: int = 400, max_bytes: int = 200_000) -> list[Path]:
    discovered: list[Path] = []
    for path in workspace.rglob("*"):
        if len(discovered) >= max_files:
            break
        if any(part in IGNORE_DIR_NAMES for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"Dockerfile", "SKILL.md", "README.md"}:
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        discovered.append(path)
    return discovered


def print_error(message: str) -> None:
    print(f"❌ {message}", file=sys.stderr)


def print_info(message: str) -> None:
    print(f"ℹ️  {message}")


# ---------------------------------------------------------------------------
# ES version compatibility
# ---------------------------------------------------------------------------

# Baseline: the asset shapes we render rely on data-stream-first APIs and
# composable index templates. Both are stable from 8.0 onwards. 7.x will
# silently accept some calls and reject others (e.g. stricter data-stream
# enforcement around 7.16 landed in weird shapes) — safer to refuse.
MIN_SUPPORTED_MAJOR = 8
# Anything major >= this is explicitly tested. Higher majors (ES 10+ when it
# ships) will just print a warning — the assets are data-stream/ECS-based,
# not exotic, so breakage in a future major is possible but unlikely.
TESTED_MAX_MAJOR = 9

# The product tag we stamp into _meta on every asset we render, so uninstall
# can tell ours apart from a foreign resource that happens to share the name.
OBSERVER_PRODUCT_TAG = "elasticsearch-agent-observability"

# Dataset tag used by skill self-audit events. Shares the `internal.*`
# namespace with sanity_check / pipeline_verify / alert_check so alerting
# and report aggregations already filter it out.
SKILL_AUDIT_DATASET = "internal.skill_audit"


def emit_skill_audit(
    config: "ESConfig",
    *,
    index_prefix: str,
    tool_name: str,
    verdict: str,
    duration_ms: int | float | None = None,
    inputs: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Write one skill-self-audit record to ``<prefix>-events``.

    Why this exists: when an agent claims "I ran doctor and it was fine", we
    want a trace we can look up. The audit record captures tool_name,
    verdict, duration, inputs (URLs/prefix, no secrets), and a small
    evidence dict (e.g. check names with their statuses). Written via
    ``_create`` so data-stream rules are satisfied. Best-effort: failures
    are swallowed with a stderr warning so audit never masks the real
    result of the caller.

    Audit records carry ``event.dataset = internal.skill_audit`` so the
    same ``must_not`` filter that already keeps sanity_check out of alert
    statistics keeps these out too.
    """
    ds_name = build_data_stream_name(index_prefix)
    doc: dict[str, Any] = {
        "@timestamp": utcnow_iso(),
        "event.kind": "event",
        "event.category": "process",
        "event.action": "skill_run",
        "event.outcome": "success" if verdict in {"ok", "healthy", "ready", "passed"} else "failure",
        "event.dataset": SKILL_AUDIT_DATASET,
        "service.name": "elasticsearch-agent-observability",
        "observer.product": OBSERVER_PRODUCT_TAG,
        "gen_ai.agent.signal_type": "skill_audit",
        "gen_ai.agent.tool_name": tool_name,
        "skill.verdict": verdict,
    }
    if duration_ms is not None:
        doc["skill.duration_ms"] = duration_ms
    if inputs:
        doc["skill.inputs"] = inputs
    if evidence:
        doc["skill.evidence"] = evidence
    if extra:
        for key, value in extra.items():
            doc.setdefault(key, value)
    try:
        es_request(config, "POST", f"/{ds_name}/_create", doc)
        return True
    except SkillError as exc:
        print(f"⚠️  skill self-audit write failed ({tool_name}): {exc}", file=sys.stderr)
        return False


def parse_es_version(version_string: str) -> tuple[int, int, int]:
    """Parse an Elasticsearch version string like ``8.13.2`` into a tuple.

    Returns ``(0, 0, 0)`` for unparseable input so callers can special-case it.
    """
    text = (version_string or "").strip().split("-", 1)[0].split("+", 1)[0]
    parts = text.split(".")
    if len(parts) < 1 or not parts[0].isdigit():
        return (0, 0, 0)
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return (major, minor, patch)


def check_es_version(config: "ESConfig") -> dict[str, Any]:
    """Probe ``GET /`` and classify the ES version.

    Returns a dict with ``version``, ``major/minor/patch``, ``status`` and
    ``detail``. Status values:

    - ``supported`` — major is within the tested window
    - ``warn``      — reachable but either too new (future major) or unparseable
    - ``unsupported`` — major is below ``MIN_SUPPORTED_MAJOR``; callers should refuse to proceed

    We deliberately do NOT raise from here. Callers decide how strict to be
    (bootstrap refuses unsupported, doctor reports warn as ``degraded``).
    """
    response = es_request(config, "GET", "/")
    version_info = response.get("version") or {}
    number = str(version_info.get("number") or "").strip()
    major, minor, patch = parse_es_version(number)

    if major == 0:
        return {
            "version": number or "unknown",
            "major": 0,
            "minor": 0,
            "patch": 0,
            "status": "warn",
            "detail": f"Could not parse ES version `{number}`. Proceeding but expect surprises.",
        }
    if major < MIN_SUPPORTED_MAJOR:
        return {
            "version": number,
            "major": major,
            "minor": minor,
            "patch": patch,
            "status": "unsupported",
            "detail": (
                f"Elasticsearch {number} is below the minimum supported major ({MIN_SUPPORTED_MAJOR}.x). "
                "This skill renders data-stream-first assets and ECS-aligned mappings that 7.x handles inconsistently. "
                "Upgrade the cluster (or pin this skill to its pre-8.x version if you are maintaining a 7.x fork)."
            ),
        }
    if major > TESTED_MAX_MAJOR:
        return {
            "version": number,
            "major": major,
            "minor": minor,
            "patch": patch,
            "status": "warn",
            "detail": (
                f"Elasticsearch {number} is newer than the latest tested major ({TESTED_MAX_MAJOR}.x). "
                "The assets should still work, but run `doctor.py` after bootstrap and report any 400 Bad Request in an issue."
            ),
        }
    return {
        "version": number,
        "major": major,
        "minor": minor,
        "patch": patch,
        "status": "supported",
        "detail": f"Elasticsearch {number} is within the tested range.",
    }


def es_request(config: ESConfig, method: str, path: str, payload: dict | None = None) -> dict:
    """Send a request to Elasticsearch with bounded retries on transient failures.

    Idempotent methods (GET/HEAD/PUT/DELETE) retry on 429/5xx and network errors.
    POST retries only on network errors — we do not retry 5xx for POST to avoid
    double-indexing documents.
    """
    url = config.es_url.rstrip("/") + path
    upper_method = method.upper()
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    context = build_ssl_context(config.verify_tls)

    attempts = max(1, config.max_retries + 1)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, method=upper_method)
        request.add_header("Content-Type", "application/json")
        if config.es_user and config.es_password:
            token = base64.b64encode(f"{config.es_user}:{config.es_password}".encode("utf-8")).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, data=body, timeout=config.timeout_seconds, context=context) as response:  # noqa: S310
                text = response.read().decode("utf-8")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise SkillError(f"Invalid JSON response from Elasticsearch: {text[:200]}") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            retryable = upper_method in IDEMPOTENT_METHODS and exc.code in RETRYABLE_STATUS
            if retryable and attempt < attempts - 1:
                time.sleep(config.retry_backoff_seconds * (2 ** attempt))
                last_exc = exc
                continue
            raise SkillError(f"Elasticsearch HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            if attempt < attempts - 1:
                time.sleep(config.retry_backoff_seconds * (2 ** attempt))
                last_exc = exc
                continue
            raise SkillError(f"Unable to reach Elasticsearch: {exc.reason}") from exc
    # Defensive fallback; the loop above always either returns or raises.
    raise SkillError(f"Elasticsearch request failed after {attempts} attempts: {last_exc}")
