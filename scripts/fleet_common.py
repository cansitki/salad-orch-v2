#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
from datetime import UTC, datetime
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
STATE_DIR = pathlib.Path(os.environ.get("SALAD_PRL_STATE_DIR", str(REPO_ROOT / "state")))
ENV_PATH = pathlib.Path(os.environ.get("SALAD_PRL_ENV", str(REPO_ROOT / ".env")))


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_env_file(path: pathlib.Path | None = None) -> None:
    env_path = path or ENV_PATH
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return float(value)


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def read_json_env(name: str) -> Any | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    value = raw.strip()
    if value.startswith(("{", "[")):
        return json.loads(value)
    possible_path = pathlib.Path(value)
    try:
        path_exists = possible_path.exists()
    except OSError:
        path_exists = False
    if path_exists:
        return json.loads(possible_path.read_text(encoding="utf-8"))
    return json.loads(value)


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def compact_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def safe_public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    blocked_tokens = ("api_key", "authorization", "cookie", "token", "secret", "password")
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        normalized = key.lower()
        if any(token in normalized for token in blocked_tokens):
            safe[key] = "<redacted>"
        elif isinstance(value, dict):
            safe[key] = safe_public_payload(value)
        elif isinstance(value, list):
            safe[key] = [safe_public_payload(item) if isinstance(item, dict) else item for item in value]
        else:
            safe[key] = value
    return safe
