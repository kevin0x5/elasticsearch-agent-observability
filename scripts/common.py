#!/usr/bin/env python3
"""Shared helpers for elasticsearch-agent-observability."""

from __future__ import annotations

import base64
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GENERATED_DIR = ROOT_DIR / "generated"
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".js", ".ts", ".tsx", ".jsx",
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


def es_request(config: ESConfig, method: str, path: str, payload: dict | None = None) -> dict:
    url = config.es_url.rstrip("/") + path
    request = urllib.request.Request(url, method=method.upper())
    request.add_header("Content-Type", "application/json")
    if config.es_user and config.es_password:
        token = base64.b64encode(f"{config.es_user}:{config.es_password}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(request, data=body, timeout=config.timeout_seconds) as response:  # noqa: S310
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise SkillError(f"Elasticsearch HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SkillError(f"Unable to reach Elasticsearch: {exc.reason}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SkillError(f"Invalid JSON response from Elasticsearch: {text[:200]}") from exc
