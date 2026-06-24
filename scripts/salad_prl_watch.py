#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
STATE_DIR = pathlib.Path(os.environ.get("SALAD_PRL_STATE_DIR", str(REPO_ROOT / "state")))
ENV = pathlib.Path(os.environ.get("SALAD_PRL_ENV", str(REPO_ROOT / ".env")))
SLOT_ACTION_STATE_PATH = pathlib.Path(
    os.environ.get("PRL_SLOT_ACTION_STATE_PATH", str(STATE_DIR / "prl_slot_actions.json"))
)


def load_env_file() -> None:
    if not ENV.exists():
        return
    for line in ENV.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()
WATCH_NAME = os.environ.get("PRL_WATCH_NAME", "kray2-prl-watch")
LOG = pathlib.Path(os.environ.get("PRL_WATCH_LOG", str(STATE_DIR / "logs" / f"{WATCH_NAME}.log")))
BASE = "https://api.salad.com/api/public"
ORG = os.environ.get("PRL_WATCH_ORG", "kray2")
PUBLIC_ORG = os.environ.get("PRL_WATCH_PUBLIC_ORG", ORG)
PROJECT = "default"
API_KEY_ENV = os.environ.get("PRL_WATCH_API_KEY_ENV", "SALAD_API_KEY_2")
WATCH_HTTP_TIMEOUT_SECONDS = float(os.environ.get("PRL_WATCH_HTTP_TIMEOUT_SECONDS", "15"))
WALLET = os.environ.get("PRL_WALLET", "")
SLOTS_ENV = os.environ.get("PRL_WATCH_SLOTS")
SLOTS = [slot.strip() for slot in SLOTS_ENV.split(",") if slot.strip()] if SLOTS_ENV else [f"prl-kray2-roi-{i:02d}" for i in range(1, 11)]
POOL_WORKER_PREFIX = os.environ.get("PRL_WATCH_POOL_WORKER_PREFIX", "kray2-prl-kray2")
WORKER_PREFIX = os.environ.get("PRL_WATCH_WORKER_PREFIX", "kray2-prl")
WORKER_SLOT_PREFIX = os.environ.get("PRL_WATCH_WORKER_SLOT_PREFIX", "kray2-roi-")
DISPLAY_PREFIX = os.environ.get("PRL_WATCH_DISPLAY_PREFIX", f"PearlFortune {ORG.upper()}")
TRY_SECONDS = int(os.environ.get("KRAY2_PRL_TRY_SECONDS", "420"))
POLL_SECONDS = int(os.environ.get("KRAY2_PRL_POLL_SECONDS", "60"))
RUNNING_WITHOUT_POOL_SECONDS = int(os.environ.get("KRAY2_PRL_RUNNING_WITHOUT_POOL_SECONDS", "360"))
CREATE_PROGRESS_SECONDS = int(os.environ.get("KRAY2_PRL_CREATE_PROGRESS_SECONDS", "600"))
EMPTY_CREATING_SECONDS = int(
    os.environ.get("KRAY2_PRL_EMPTY_CREATING_SECONDS", str(min(CREATE_PROGRESS_SECONDS, TRY_SECONDS)))
)
PROFILE_MISMATCH_GRACE_SECONDS = int(os.environ.get("KRAY2_PRL_PROFILE_MISMATCH_GRACE_SECONDS", "300"))
ALLOCATING_RETARGET_AVAILABLE_SECONDS = int(
    os.environ.get("KRAY2_PRL_ALLOCATING_RETARGET_AVAILABLE_SECONDS", "180")
)
LOW_LIVE_MIN_LIVE_WORKERS = int(
    os.environ.get("KRAY2_PRL_LOW_LIVE_MIN_LIVE_WORKERS", str(max(1, len(SLOTS) - 2)))
)
LOW_LIVE_ALLOCATING_RETARGET_AVAILABLE_SECONDS = int(
    os.environ.get("KRAY2_PRL_LOW_LIVE_ALLOCATING_RETARGET_AVAILABLE_SECONDS", str(TRY_SECONDS))
)
LOW_LIVE_IGNORE_PENDING_CAPACITY = os.environ.get(
    "KRAY2_PRL_LOW_LIVE_IGNORE_PENDING_CAPACITY",
    "1",
).lower() in {"1", "true", "yes"}
STOPPED_RESTART_COOLDOWN_SECONDS = int(os.environ.get("KRAY2_PRL_STOPPED_RESTART_COOLDOWN_SECONDS", "300"))
UPGRADE_TO_BEST_SECONDS = int(os.environ.get("KRAY2_PRL_UPGRADE_TO_BEST_SECONDS", "999999"))
OPTIMIZE_LIVE_SECONDS = int(os.environ.get("KRAY2_PRL_OPTIMIZE_LIVE_SECONDS", "999999"))
OPTIMIZE_LIVE_MIN_PROFIT_DELTA = float(os.environ.get("KRAY2_PRL_OPTIMIZE_LIVE_MIN_PROFIT_DELTA", "0.25"))
OPTIMIZE_LIVE_MIN_TH_DELTA = float(os.environ.get("KRAY2_PRL_OPTIMIZE_LIVE_MIN_TH_DELTA", "10"))
OPTIMIZE_LIVE_INTERVAL_SECONDS = int(os.environ.get("KRAY2_PRL_OPTIMIZE_LIVE_INTERVAL_SECONDS", "600"))
OPTIMIZE_LIVE_MIN_LIVE_WORKERS = int(
    os.environ.get("KRAY2_PRL_OPTIMIZE_LIVE_MIN_LIVE_WORKERS", str(max(1, len(SLOTS) - 2)))
)
OPTIMIZE_LIVE_REQUIRE_FULL_SLOTS = os.environ.get("KRAY2_PRL_OPTIMIZE_LIVE_REQUIRE_FULL_SLOTS", "0").lower() in {
    "1",
    "true",
    "yes",
}
OPTIMIZE_LIVE_REQUIRE_REPORTED_AVAILABLE = os.environ.get(
    "KRAY2_PRL_OPTIMIZE_LIVE_REQUIRE_REPORTED_AVAILABLE",
    "1",
).lower() in {
    "1",
    "true",
    "yes",
}
NO_GPU_SLEEP_AFTER_SECONDS = int(os.environ.get("KRAY2_PRL_NO_GPU_SLEEP_AFTER_SECONDS", "3600"))
NO_GPU_SLEEP_SECONDS = int(os.environ.get("KRAY2_PRL_NO_GPU_SLEEP_SECONDS", "900"))
NO_GPU_STATE_PATH = pathlib.Path(
    os.environ.get("KRAY2_PRL_NO_GPU_STATE_PATH", str(STATE_DIR / f"{WATCH_NAME}_no_gpu_state.json"))
)
NO_CREDITS_BACKOFF_SECONDS = int(os.environ.get("KRAY2_PRL_NO_CREDITS_BACKOFF_SECONDS", "120"))
NO_CREDITS_TARGET_REFRESH_SECONDS = int(os.environ.get("PRL_WATCH_NO_CREDITS_TARGET_REFRESH_SECONDS", "900"))
TARGET_OFFSET_RECENT_NO_CREDITS_SECONDS = int(
    os.environ.get(
        "PRL_WATCH_TARGET_OFFSET_RECENT_NO_CREDITS_SECONDS",
        str(max(600, NO_CREDITS_BACKOFF_SECONDS * 3, NO_CREDITS_TARGET_REFRESH_SECONDS * 3)),
    )
)
SALAD_MONITOR_DB = pathlib.Path(os.environ.get("PRL_WATCH_SALAD_MONITOR_DB", str(STATE_DIR / "salad_pearl_monitor.db")))
ORG_BALANCE_CACHE_SECONDS = int(os.environ.get("PRL_WATCH_ORG_BALANCE_CACHE_SECONDS", "30"))
ORG_BALANCE_MAX_AGE_SECONDS = int(os.environ.get("PRL_WATCH_ORG_BALANCE_MAX_AGE_SECONDS", "300"))
COORDINATED_ORGS = tuple(
    org.strip()
    for org in os.environ.get("PRL_WATCH_COORDINATED_ORGS", "kray,kray2,kray3").split(",")
    if org.strip()
)
PRICE_GUARD_ENABLED = os.environ.get("PRL_WATCH_PRICE_GUARD", "1").lower() not in {"0", "false", "no"}
PRICE_GUARD_MIN_PROFIT_DAY = float(os.environ.get("PRL_WATCH_MIN_PROFIT_USD_DAY", "0.01"))
PRICE_GUARD_PRICE_BAND_USD = float(os.environ.get("PRL_WATCH_PRICE_BAND_USD", "0.02"))
PRICE_GUARD_DECISION_PRICE_CAP_USD = float(os.environ.get("PRL_WATCH_DECISION_PRICE_CAP_USD", "0.63"))
PRICE_GUARD_FIXED_DECISION_PRICE_USD = float(os.environ.get("PRL_WATCH_FIXED_DECISION_PRICE_USD", "0.62"))
PRICE_GUARD_CACHE_SECONDS = int(os.environ.get("PRL_WATCH_PRICE_GUARD_CACHE_SECONDS", "300"))
PRICE_CATALOG_CACHE_SECONDS = int(os.environ.get("PRL_WATCH_PRICE_CATALOG_CACHE_SECONDS", "900"))
MINER_RELEASE_TAG = os.environ.get("PRL_WATCH_MINER_RELEASE_TAG", "v.1.1.8")
MINER_PACKAGE_VERSION = os.environ.get("PRL_WATCH_MINER_PACKAGE_VERSION", "v1.1.8")
MINER_BINARY = os.environ.get("PRL_WATCH_MINER_BINARY", "miner-cuda12")
MINER_PACKAGE_URL = os.environ.get(
    "PRL_WATCH_MINER_URL",
    f"https://github.com/pearlfortune/pearl-miner/releases/download/{MINER_RELEASE_TAG}/pearlfortune-{MINER_PACKAGE_VERSION}.tar.gz",
)
TARGET_OFFSET = int(os.environ.get("PRL_WATCH_TARGET_OFFSET", "0"))
COORDINATED_TARGET_OFFSETS = os.environ.get("PRL_WATCH_COORDINATED_TARGET_OFFSETS", "0").lower() in {
    "1",
    "true",
    "yes",
}
ALLOWED_PRIORITIES = tuple(
    priority.strip().lower()
    for priority in os.environ.get("PRL_WATCH_ALLOWED_PRIORITIES", "batch,low").split(",")
    if priority.strip()
)
CAPACITY_ZERO_AVAILABLE_PROBE_BUDGET = int(os.environ.get("PRL_WATCH_ZERO_AVAILABLE_PROBE_BUDGET", "0"))
CAPACITY_ZERO_AVAILABLE_PROBE_MAX_LIVE_WORKERS = int(
    os.environ.get("PRL_WATCH_ZERO_AVAILABLE_PROBE_MAX_LIVE_WORKERS", "0")
)
BLOCKED_PROFILES = {
    tuple(part.strip().lower() for part in item.split(":", 1))
    for item in os.environ.get("PRL_WATCH_BLOCKED_PROFILES", "").split(",")
    if ":" in item
}


GPU = {
    "3060ti": "cb6c1931-89b6-4f76-976f-54047320ccc6",
    "3070": "951131f6-5acf-489c-b303-0906be8b26ef",
    "3070ti": "d9fb0bd6-05c9-4cb9-b98e-9f7d1b5ba0e7",
    "3080": "43a49c0c-f860-40e9-a509-702d0dba0902",
    "3080ti": "65247de0-746f-45c6-8537-650ba613966a",
    "3090": "a5db5c50-cbcb-4596-ae80-6a0c8090d80f",
    "3090ti": "9998fe42-04a5-4807-b3a5-849943f16c38",
    "4070": "0798d5aa-2d17-42ee-81b8-ea92e3bc088e",
    "4070ti": "de00c90b-904b-4d9e-8fc9-1d9a08eb0932",
    "4070tis": "f1380143-51cd-4bad-80cb-1f86ee6b49fe",
    "4080": "0d062939-7c01-4aae-a2b1-30e315124e51",
    "4090": "ed563892-aacd-40f5-80b7-90c9be6c759b",
    "5070": "61e8ceee-4479-40c5-9a05-1711f45f931c",
    "5070ti": "1b8747be-e789-475b-a339-3c1028010d84",
    "5060ti": "5d6b104d-c029-4357-b179-8b662d0a76b2",
    "5080": "8065b30b-4a27-434c-8610-222e8df8fad7",
    "5090": "851399fb-7329-4195-a042-d6514b28cf33",
    "5090laptop": "83ef776e-ce34-4d89-8cf9-81898f1416fa",
}


@dataclass(frozen=True)
class Candidate:
    label: str
    priority: str
    gpu_keys: tuple[str, ...]
    memory: int = 2048

    @property
    def gpu_ids(self) -> list[str]:
        return [GPU[key] for key in self.gpu_keys]


BROAD_HIGH_ROI = (
    "5090",
    "4080",
    "4070tis",
    "5070",
    "4090",
    "5070ti",
    "5080",
    "5090laptop",
    "5060ti",
    "3080",
    "3080ti",
    "3070",
    "3070ti",
    "4070ti",
    "3090",
    "3060ti",
)

EXPECTED_TH_BY_PROFILE: dict[tuple[str, str], float] = {
    ("5090", "batch"): 315.0,
    ("5090", "low"): 315.0,
    ("4090", "batch"): 230.0,
    ("4080", "batch"): 160.47,
    ("4080", "low"): 171.15,
    ("4070tis", "batch"): 128.75,
    ("4070tis", "low"): 147.05,
    ("4070ti", "batch"): 125.0,
    ("5070", "batch"): 117.88,
    ("5070", "low"): 117.88,
    ("5070ti", "batch"): 145.0,
    ("5080", "batch"): 195.70,
    ("3080", "batch"): 65.0,
    ("3080ti", "batch"): 86.0,
    ("3070", "batch"): 61.76,
    ("3070ti", "batch"): 70.0,
    ("3060ti", "batch"): 45.0,
    ("3090", "batch"): 100.0,
    ("5060ti", "batch"): 85.0,
    ("5090laptop", "batch"): 131.0,
}

STATIC_HOURLY_USD_BY_PROFILE: dict[tuple[str, str], float] = {
    ("3060ti", "batch"): 0.03,
    ("3070", "batch"): 0.04,
    ("3070ti", "batch"): 0.06,
    ("3080", "batch"): 0.06,
    ("3080ti", "batch"): 0.08,
    ("3090", "batch"): 0.09,
    ("4070ti", "batch"): 0.08,
    ("4070tis", "batch"): 0.09,
    ("4070tis", "low"): 0.147,
    ("4080", "batch"): 0.11,
    ("4080", "low"): 0.167,
    ("4090", "batch"): 0.16,
    ("5070", "batch"): 0.08,
    ("5070", "low"): 0.133,
    ("5070ti", "batch"): 0.10,
    ("5060ti", "batch"): 0.07,
    ("5080", "batch"): 0.18,
    ("5090", "batch"): 0.25,
    ("5090", "low"): 0.31,
    ("5090laptop", "batch"): 0.10,
}

INITIAL: dict[str, Candidate] = {
    "01": Candidate("RTX 5090 batch", "batch", ("5090",), 2048),
    "02": Candidate("RTX 4090 batch", "batch", ("4090",), 2048),
    "03": Candidate("RTX 4080 batch", "batch", ("4080",), 2048),
    "04": Candidate("RTX 4080 batch", "batch", ("4080",), 2048),
    "05": Candidate("RTX 5070 Ti batch", "batch", ("5070ti",), 2048),
    "06": Candidate("RTX 5070 Ti batch", "batch", ("5070ti",), 2048),
    "07": Candidate("RTX 5070 Ti batch", "batch", ("5070ti",), 2048),
    "08": Candidate("RTX 4070 Ti Super batch 4GB", "batch", ("4070tis",), 4096),
    "09": Candidate("RTX 5070 batch", "batch", ("5070",), 2048),
    "10": Candidate("RTX 5070 batch", "batch", ("5070",), 2048),
}

FALLBACKS = [
    Candidate("RTX 5090 batch", "batch", ("5090",)),
    Candidate("RTX 4090 batch", "batch", ("4090",)),
    Candidate("RTX 4080 batch", "batch", ("4080",)),
    Candidate("RTX 5070 Ti batch", "batch", ("5070ti",)),
    Candidate("RTX 4070 Ti batch", "batch", ("4070ti",)),
    Candidate("RTX 4070 Ti Super batch 4GB", "batch", ("4070tis",), 4096),
    Candidate("RTX 5070 batch", "batch", ("5070",)),
    Candidate("RTX 5080 batch", "batch", ("5080",)),
    Candidate("RTX 5090 low", "low", ("5090",)),
    Candidate("RTX 5090 Laptop batch", "batch", ("5090laptop",)),
    Candidate("RTX 5060 Ti batch", "batch", ("5060ti",)),
    Candidate("RTX 3060 Ti batch", "batch", ("3060ti",)),
    Candidate("RTX 3070 batch 4GB", "batch", ("3070",), 4096),
    Candidate("RTX 3070 Ti batch 4GB", "batch", ("3070ti",), 4096),
    Candidate("RTX 4080 low", "low", ("4080",)),
    Candidate("RTX 3090 batch", "batch", ("3090",)),
    Candidate("RTX 4070 Ti Super low 4GB", "low", ("4070tis",), 4096),
    Candidate("RTX 3080 batch", "batch", ("3080",)),
    Candidate("RTX 3080 Ti batch", "batch", ("3080ti",)),
    Candidate("RTX 5070 low", "low", ("5070",)),
]

SLOT_CANDIDATE_INDEX: dict[str, int] = {}
SLOT_LAST_PATCH: dict[str, float] = {}
SLOT_RUNNING_WITHOUT_POOL_SINCE: dict[str, float] = {}
SLOT_EMPTY_DEPLOYING_SINCE: dict[str, float] = {}
SLOT_EMPTY_CREATING_SINCE: dict[str, float] = {}
SLOT_ALLOCATING_SINCE: dict[str, float] = {}
SLOT_ALLOCATING_RETARGET_SINCE: dict[str, float] = {}
SLOT_CREATING_SINCE: dict[str, float] = {}
SLOT_CREATING_PROGRESS: dict[str, float] = {}
SLOT_PROFILE_MISMATCH_SINCE: dict[str, float] = {}
NO_CREDITS_UNTIL = 0.0
NO_CREDITS_TARGETS_UNTIL = 0.0
LAST_NO_CREDITS_AT = 0.0
STOPPED_SLOT_STATE_PATH = pathlib.Path(
    os.environ.get("PRL_STOPPED_SLOT_STATE_PATH", str(STATE_DIR / "prl_stopped_slots.json"))
)
BEST_UPGRADE_REMAINING = 0
CANDIDATE_BUDGET_REMAINING: dict[tuple[str, tuple[str, ...], int], int] = {}
CANDIDATE_REPORTED_AVAILABLE: dict[tuple[str, tuple[str, ...], int], int] = {}
SLOT_TARGETS: dict[str, Candidate] = {}
SLOT_TARGETS_OFFSET = -1
NO_GPU_SINCE = 0.0
NO_GPU_UNTIL = 0.0
LAST_LIVE_UPGRADE_AT = 0.0
ALLOW_LIVE_UPGRADES_THIS_TICK = False
LOW_LIVE_THIS_TICK = False
PRICE_GUARD_CACHE: dict[str, float] = {}
PRICE_CATALOG_CACHE: dict[str, Any] = {}
ORG_BALANCE_CACHE: dict[str, Any] = {}
ORG_BALANCES_CACHE: dict[str, Any] = {}
PEER_CAPACITY_ORG = os.environ.get(
    "PRL_WATCH_PEER_CAPACITY_ORG",
    "kray" if ORG == "kray2" and SLOTS_ENV is None else "",
)
PEER_CAPACITY_SLOTS_ENV = os.environ.get("PRL_WATCH_PEER_CAPACITY_SLOTS")
PEER_CAPACITY_SLOTS = (
    [slot.strip() for slot in PEER_CAPACITY_SLOTS_ENV.split(",") if slot.strip()]
    if PEER_CAPACITY_SLOTS_ENV
    else [f"prl-kray-roi-{i:02d}" for i in range(1, 16)]
)
PEER_WORKER_PREFIX = os.environ.get("PRL_WATCH_PEER_WORKER_PREFIX", "kray-prl-kray")
PEER_WORKER_SLOT_PREFIX = os.environ.get("PRL_WATCH_PEER_WORKER_SLOT_PREFIX", "kray-roi-")


def load_env() -> None:
    load_env_file()


def now() -> datetime:
    return datetime.now(UTC)


def log(event: str, **fields: Any) -> None:
    def public_value(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(ORG, PUBLIC_ORG) if ORG != PUBLIC_ORG else value
        if isinstance(value, list):
            return [public_value(item) for item in value]
        if isinstance(value, tuple):
            return [public_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): public_value(item) for key, item in value.items()}
        return value

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        safe_fields = {key: public_value(value) for key, value in fields.items()}
        fh.write(json.dumps({"at": now().isoformat(timespec="seconds"), "event": event, **safe_fields}, sort_keys=True) + "\n")


def load_no_gpu_state() -> None:
    global NO_GPU_SINCE, NO_GPU_UNTIL
    try:
        payload = json.loads(NO_GPU_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if payload.get("org") not in {ORG, PUBLIC_ORG}:
        return
    try:
        NO_GPU_SINCE = float(payload.get("since") or 0)
        NO_GPU_UNTIL = float(payload.get("until") or 0)
    except (TypeError, ValueError):
        NO_GPU_SINCE = 0.0
        NO_GPU_UNTIL = 0.0


def save_no_gpu_state() -> None:
    NO_GPU_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "watch_name": WATCH_NAME,
        "org": PUBLIC_ORG,
        "since": NO_GPU_SINCE,
        "until": NO_GPU_UNTIL,
        "updated_at": now().isoformat(timespec="seconds"),
    }
    NO_GPU_STATE_PATH.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def safe_slot_action_token(org: str, slot: str) -> str:
    text = f"{org}__{slot}"
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in text)


def slot_action_detail_path(org: str, slot: str) -> pathlib.Path:
    return SLOT_ACTION_STATE_PATH.parent / f"{SLOT_ACTION_STATE_PATH.stem}.d" / f"{safe_slot_action_token(org, slot)}.json"


def write_slot_action_detail(org: str, slot: str, payload: dict[str, Any]) -> None:
    path = slot_action_detail_path(org, slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def record_slot_action_state(slot: str, action: str, reason: str = "", candidate: str = "") -> None:
    try:
        SLOT_ACTION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = json.loads(SLOT_ACTION_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        now_ts = time.time()
        payload = {
            "at": now_ts,
            "at_utc": now().isoformat(timespec="seconds"),
            "org": PUBLIC_ORG,
            "slot": slot,
            "action": action,
            "reason": reason,
            "candidate": candidate,
        }
        state[f"{PUBLIC_ORG}/{slot}"] = payload
        write_slot_action_detail(PUBLIC_ORG, slot, payload)
        if ORG != PUBLIC_ORG:
            state[f"{ORG}/{slot}"] = payload
            write_slot_action_detail(ORG, slot, payload)
        tmp = SLOT_ACTION_STATE_PATH.with_suffix(f"{SLOT_ACTION_STATE_PATH.suffix}.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(SLOT_ACTION_STATE_PATH)
    except Exception as exc:
        log("slot_action_state_write_failed", slot=slot, action=action, error=type(exc).__name__, detail=str(exc)[:180])


def remove_no_gpu_state() -> None:
    try:
        NO_GPU_STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def note_no_credits(detail: str) -> None:
    global LAST_NO_CREDITS_AT, NO_CREDITS_TARGETS_UNTIL, NO_CREDITS_UNTIL
    now_ts = time.time()
    LAST_NO_CREDITS_AT = now_ts
    NO_CREDITS_UNTIL = max(NO_CREDITS_UNTIL, now_ts + NO_CREDITS_BACKOFF_SECONDS)
    target_offset = effective_target_offset(now_ts)
    refresh_targets_now = False
    if SLOT_TARGETS and SLOT_TARGETS_OFFSET != target_offset:
        NO_CREDITS_TARGETS_UNTIL = 0.0
        refresh_targets_now = True
    elif now_ts >= NO_CREDITS_TARGETS_UNTIL:
        NO_CREDITS_TARGETS_UNTIL = now_ts + NO_CREDITS_TARGET_REFRESH_SECONDS
    log(
        "no_credits_backoff_set",
        org=ORG,
        backoff_seconds=NO_CREDITS_BACKOFF_SECONDS,
        recent_no_credits_window_seconds=TARGET_OFFSET_RECENT_NO_CREDITS_SECONDS,
        target_refresh_seconds=NO_CREDITS_TARGET_REFRESH_SECONDS,
        configured_target_offset=TARGET_OFFSET,
        target_offset=target_offset,
        until=round(NO_CREDITS_UNTIL, 3),
        targets_until=round(NO_CREDITS_TARGETS_UNTIL, 3),
        detail=detail[:180],
    )
    if refresh_targets_now:
        try:
            refresh_slot_targets()
        except Exception as exc:
            log("slot_targets_refresh_after_no_credits_failed", org=ORG, error=type(exc).__name__, detail=str(exc)[:180])


def clear_no_credits_state(reason: str, **fields: Any) -> None:
    global LAST_NO_CREDITS_AT, NO_CREDITS_TARGETS_UNTIL, NO_CREDITS_UNTIL, SLOT_TARGETS_OFFSET
    had_no_credits = LAST_NO_CREDITS_AT > 0 or NO_CREDITS_UNTIL > 0 or NO_CREDITS_TARGETS_UNTIL > 0
    if not had_no_credits:
        return
    refresh_targets_now = SLOT_TARGETS_OFFSET > 0
    LAST_NO_CREDITS_AT = 0.0
    NO_CREDITS_UNTIL = 0.0
    NO_CREDITS_TARGETS_UNTIL = 0.0
    if refresh_targets_now:
        SLOT_TARGETS.clear()
        SLOT_TARGETS_OFFSET = -1
    log("no_credits_state_cleared", org=ORG, reason=reason, **fields)
    if refresh_targets_now:
        try:
            refresh_slot_targets()
        except Exception as exc:
            log("slot_targets_refresh_after_credit_restore_failed", org=ORG, error=type(exc).__name__, detail=str(exc)[:180])


def note_no_gpu_available(reason: str, **fields: Any) -> None:
    global NO_GPU_SINCE, NO_GPU_UNTIL
    if NO_GPU_SLEEP_AFTER_SECONDS <= 0 or NO_GPU_SLEEP_SECONDS <= 0:
        clear_no_gpu_state("no_gpu_sleep_disabled", reason=reason, **fields)
        log("no_gpu_available_no_backoff", reason=reason, **fields)
        return
    now_ts = time.time()
    if NO_GPU_SINCE <= 0:
        NO_GPU_SINCE = now_ts
        save_no_gpu_state()
    age_seconds = now_ts - NO_GPU_SINCE
    if age_seconds < NO_GPU_SLEEP_AFTER_SECONDS:
        log(
            "no_gpu_available_observed",
            reason=reason,
            age_seconds=round(age_seconds, 1),
            sleep_after_seconds=NO_GPU_SLEEP_AFTER_SECONDS,
            **fields,
        )
        return
    NO_GPU_UNTIL = max(NO_GPU_UNTIL, now_ts + NO_GPU_SLEEP_SECONDS)
    NO_GPU_SINCE = now_ts
    save_no_gpu_state()
    log(
        "no_gpu_sleep_set",
        reason=reason,
        sleep_seconds=NO_GPU_SLEEP_SECONDS,
        until=round(NO_GPU_UNTIL, 3),
        **fields,
    )


def clear_no_gpu_state(reason: str, **fields: Any) -> None:
    global NO_GPU_SINCE, NO_GPU_UNTIL
    if NO_GPU_SINCE <= 0 and NO_GPU_UNTIL <= 0:
        return
    NO_GPU_SINCE = 0.0
    NO_GPU_UNTIL = 0.0
    remove_no_gpu_state()
    log("no_gpu_state_cleared", reason=reason, **fields)


def latest_org_balance_cents(now_ts: float | None = None) -> tuple[int | None, float | None]:
    now_ts = time.time() if now_ts is None else now_ts
    cached = ORG_BALANCE_CACHE.get(ORG)
    if cached and now_ts - float(cached.get("checked_at") or 0) < ORG_BALANCE_CACHE_SECONDS:
        return cached.get("amount_cents"), cached.get("age_seconds")
    if not SALAD_MONITOR_DB.exists():
        ORG_BALANCE_CACHE[ORG] = {"checked_at": now_ts, "amount_cents": None, "age_seconds": None}
        return None, None
    try:
        with sqlite3.connect(f"file:{SALAD_MONITOR_DB}?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT b.amount_cents, s.checked_at_utc
                FROM salad_org_balances b
                JOIN monitor_snapshots s ON s.id = b.snapshot_id
                WHERE b.org = ? AND b.ok = 1 AND b.amount_cents IS NOT NULL
                ORDER BY b.snapshot_id DESC
                LIMIT 1
                """,
                (ORG,),
            ).fetchone()
    except Exception as exc:
        log("org_balance_lookup_failed", org=ORG, error=type(exc).__name__, detail=str(exc)[:180])
        ORG_BALANCE_CACHE[ORG] = {"checked_at": now_ts, "amount_cents": None, "age_seconds": None}
        return None, None
    if row is None:
        ORG_BALANCE_CACHE[ORG] = {"checked_at": now_ts, "amount_cents": None, "age_seconds": None}
        return None, None
    try:
        checked = str(row["checked_at_utc"])
        if checked.endswith("Z"):
            checked = f"{checked[:-1]}+00:00"
        checked_ts = datetime.fromisoformat(checked).timestamp()
        age_seconds: float | None = max(0.0, now_ts - checked_ts)
    except Exception:
        age_seconds = None
    amount_cents = int(row["amount_cents"])
    if age_seconds is not None and age_seconds > ORG_BALANCE_MAX_AGE_SECONDS:
        amount_cents = None
    ORG_BALANCE_CACHE[ORG] = {"checked_at": now_ts, "amount_cents": amount_cents, "age_seconds": age_seconds}
    return amount_cents, age_seconds


def latest_org_balances_cents(now_ts: float | None = None) -> tuple[dict[str, int | None], float | None]:
    now_ts = time.time() if now_ts is None else now_ts
    cached = ORG_BALANCES_CACHE.get("all")
    if cached and now_ts - float(cached.get("checked_at") or 0) < ORG_BALANCE_CACHE_SECONDS:
        return dict(cached.get("balances") or {}), cached.get("age_seconds")
    if not SALAD_MONITOR_DB.exists():
        balances = {org: None for org in COORDINATED_ORGS}
        ORG_BALANCES_CACHE["all"] = {"checked_at": now_ts, "balances": balances, "age_seconds": None}
        return balances, None
    try:
        with sqlite3.connect(f"file:{SALAD_MONITOR_DB}?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT b.org, b.amount_cents, s.checked_at_utc
                FROM salad_org_balances b
                JOIN monitor_snapshots s ON s.id = b.snapshot_id
                JOIN (
                    SELECT org, MAX(snapshot_id) AS snapshot_id
                    FROM salad_org_balances
                    WHERE ok = 1 AND amount_cents IS NOT NULL
                    GROUP BY org
                ) latest ON latest.org = b.org AND latest.snapshot_id = b.snapshot_id
                WHERE b.ok = 1
                  AND b.amount_cents IS NOT NULL
                """
            ).fetchall()
    except Exception as exc:
        log("org_balances_lookup_failed", org=ORG, error=type(exc).__name__, detail=str(exc)[:180])
        balances = {org: None for org in COORDINATED_ORGS}
        ORG_BALANCES_CACHE["all"] = {"checked_at": now_ts, "balances": balances, "age_seconds": None}
        return balances, None
    balances = {org: None for org in COORDINATED_ORGS}
    ages: list[float] = []
    for row in rows:
        org = str(row["org"])
        if org not in balances:
            continue
        try:
            checked = str(row["checked_at_utc"])
            if checked.endswith("Z"):
                checked = f"{checked[:-1]}+00:00"
            checked_ts = datetime.fromisoformat(checked).timestamp()
            row_age = max(0.0, now_ts - checked_ts)
        except Exception:
            row_age = None
        if row_age is not None:
            ages.append(row_age)
        if row_age is not None and row_age > ORG_BALANCE_MAX_AGE_SECONDS:
            balances[org] = None
        else:
            balances[org] = int(row["amount_cents"])
    age_seconds: float | None = max(ages) if ages else None
    ORG_BALANCES_CACHE["all"] = {"checked_at": now_ts, "balances": balances, "age_seconds": age_seconds}
    return balances, age_seconds


def org_has_confirmed_credits(now_ts: float | None = None) -> bool:
    amount_cents, _age_seconds = latest_org_balance_cents(now_ts)
    return amount_cents is not None and amount_cents > 0


def funded_org_target_offset(now_ts: float | None = None) -> int | None:
    balances, _age_seconds = latest_org_balances_cents(now_ts)
    if balances.get(ORG) is None or int(balances.get(ORG) or 0) <= 0:
        return None
    funded_orgs = [org for org in COORDINATED_ORGS if int(balances.get(org) or 0) > 0]
    if ORG not in funded_orgs:
        return None
    return funded_orgs.index(ORG) * len(SLOTS)


def headers(*, patch: bool = False) -> dict[str, str]:
    return {
        "Salad-Api-Key": os.environ[API_KEY_ENV],
        "Content-Type": "application/merge-patch+json" if patch else "application/json",
        "accept": "application/json",
        "User-Agent": f"{WATCH_NAME}/1.0",
    }


def request(method: str, path: str, payload: dict[str, Any] | None = None, *, patch: bool = False) -> dict[str, Any]:
    response = requests.request(
        method,
        f"{BASE}{path}",
        headers=headers(patch=patch),
        json=payload,
        timeout=WATCH_HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        raise KeyError(path)
    response.raise_for_status()
    return response.json() if response.text else {}


def external_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": f"{WATCH_NAME}/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode() or "{}")


def timestamp_age_seconds(value: Any, now_ts: float) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, now_ts - parsed.astimezone(UTC).timestamp())


def state_since_timestamp(current_state: dict[str, Any], now_ts: float) -> float:
    age = timestamp_age_seconds(current_state.get("start_time") or current_state.get("finish_time"), now_ts)
    if age is None:
        return now_ts
    return max(0.0, now_ts - age)


def recent_guard_stop_age_seconds(slot: str, now_ts: float) -> float | None:
    try:
        state = json.loads(STOPPED_SLOT_STATE_PATH.read_text())
    except Exception:
        return None
    entry = state.get(f"{ORG}/{slot}") or state.get(f"{PUBLIC_ORG}/{slot}")
    if not isinstance(entry, dict):
        return None
    try:
        stopped_at = float(entry.get("stopped_at") or 0)
    except (TypeError, ValueError):
        return None
    if stopped_at <= 0:
        return None
    return max(0.0, now_ts - stopped_at)


def safetrade_prl_price_usd() -> float | None:
    try:
        payload = external_json("https://safe.trade/api/v2/peatio/public/markets/prlusdt/tickers")
    except Exception as exc:
        log("price_guard_safetrade_fetch_failed", error=type(exc).__name__, detail=str(exc)[:180])
        return None
    ticker = payload.get("ticker") or {}
    values: list[float] = []
    for key in ("last", "buy", "sell"):
        try:
            value = float(ticker.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    return min(values) if values else None


def pool_prl_per_th_day_net() -> float | None:
    try:
        fee = float(
            (external_json("https://pearlfortune.org/api/v1/stats/pool-fee-rate").get("data") or {}).get(
                "pool_fee_rate"
            )
            or 0
        )
        summary = external_json("https://pearlfortune.org/api/v1/summary?hours=24")
    except Exception as exc:
        log("price_guard_pool_fetch_failed", error=type(exc).__name__, detail=str(exc)[:180])
        return None
    hourly_stats = ((summary.get("data") or {}).get("pool_stats") or {}).get("hourly_stats") or []
    gross = 0.0
    points = 0
    for item in hourly_stats:
        pool_hashrate = float(item.get("pool_hashrate") or 0)
        if pool_hashrate <= 0:
            continue
        gross += float(item.get("pool_reward") or 0) / (pool_hashrate / 1e12)
        points += 1
    if points <= 0:
        return None
    return gross * (1 - fee)


def market_prl_price_usd() -> float | None:
    prices: list[float] = []
    pearl_price: float | None = None
    try:
        pearl_price = float((external_json("https://pearlfortune.org/api/v1/market/price").get("data") or {}).get("price_usd") or 0)
        if pearl_price > 0:
            prices.append(pearl_price)
    except Exception as exc:
        log("price_guard_market_fetch_failed", error=type(exc).__name__, detail=str(exc)[:180])
    safetrade_price = safetrade_prl_price_usd()
    if safetrade_price:
        prices.append(safetrade_price)
    if not prices:
        return None
    price = min(prices)
    if pearl_price and safetrade_price and abs(pearl_price - safetrade_price) >= 0.015:
        log(
            "price_guard_market_source_spread",
            pearlfortune_price_usd=round(pearl_price, 6),
            safetrade_price_usd=round(safetrade_price, 6),
            selected_price_usd=round(price, 6),
        )
    return price


def revenue_usd_per_th_day() -> float | None:
    now_ts = time.time()
    cached_at = PRICE_GUARD_CACHE.get("at", 0.0)
    if now_ts - cached_at <= PRICE_GUARD_CACHE_SECONDS:
        value = PRICE_GUARD_CACHE.get("usd_per_th_day")
        return value if value and value > 0 else None

    prl_per_th_day = pool_prl_per_th_day_net()
    if not prl_per_th_day:
        value = PRICE_GUARD_CACHE.get("usd_per_th_day")
        return value if value and value > 0 else None

    price = market_prl_price_usd()
    if PRICE_GUARD_FIXED_DECISION_PRICE_USD > 0:
        decision_price = PRICE_GUARD_FIXED_DECISION_PRICE_USD
    else:
        if not price:
            value = PRICE_GUARD_CACHE.get("usd_per_th_day")
            return value if value and value > 0 else None
        decision_price = max(0.0, price - PRICE_GUARD_PRICE_BAND_USD)
        if PRICE_GUARD_DECISION_PRICE_CAP_USD > 0:
            decision_price = min(decision_price, PRICE_GUARD_DECISION_PRICE_CAP_USD)
    value = decision_price * prl_per_th_day
    PRICE_GUARD_CACHE.update(
        {
            "at": now_ts,
            "market_price_usd": price or 0.0,
            "decision_price_usd": decision_price,
            "price_band_usd": PRICE_GUARD_PRICE_BAND_USD,
            "decision_price_cap_usd": PRICE_GUARD_DECISION_PRICE_CAP_USD,
            "fixed_decision_price_usd": PRICE_GUARD_FIXED_DECISION_PRICE_USD,
            "prl_per_th_day": prl_per_th_day,
            "usd_per_th_day": value,
        }
    )
    log(
        "price_guard_refresh",
        market_prl_price_usd=round(price or 0.0, 6),
        decision_prl_price_usd=round(decision_price, 6),
        price_band_usd=round(PRICE_GUARD_PRICE_BAND_USD, 6),
        decision_price_cap_usd=round(PRICE_GUARD_DECISION_PRICE_CAP_USD, 6),
        fixed_decision_price_usd=round(PRICE_GUARD_FIXED_DECISION_PRICE_USD, 6),
        prl_per_th_day=round(prl_per_th_day, 8),
        usd_per_th_day=round(value, 6),
    )
    return value


def live_hourly_price(candidate: Candidate) -> float | None:
    if len(candidate.gpu_ids) != 1:
        return None
    now_ts = time.time()
    cached_at = float(PRICE_CATALOG_CACHE.get("at") or 0.0)
    catalog = PRICE_CATALOG_CACHE.get("catalog")
    failed = bool(PRICE_CATALOG_CACHE.get("failed"))
    if catalog is None and failed and now_ts - cached_at <= PRICE_CATALOG_CACHE_SECONDS:
        return None
    if catalog is None or now_ts - cached_at > PRICE_CATALOG_CACHE_SECONDS:
        try:
            payload = request("GET", f"/organizations/{ORG}/gpu-classes")
            refreshed: dict[str, dict[str, float]] = {}
            for item in payload.get("items") or []:
                gpu_id = str(item.get("id") or "")
                if not gpu_id:
                    continue
                prices: dict[str, float] = {}
                for price in item.get("prices") or []:
                    priority = str(price.get("priority") or "").lower()
                    if not priority:
                        continue
                    try:
                        prices[priority] = float(price.get("price"))
                    except (TypeError, ValueError):
                        continue
                refreshed[gpu_id] = prices
            PRICE_CATALOG_CACHE.update({"at": now_ts, "catalog": refreshed, "failed": False})
            catalog = refreshed
            log("price_catalog_refresh", org=ORG, gpu_classes=len(refreshed))
        except Exception as exc:
            log("price_catalog_fetch_failed", error=type(exc).__name__, detail=str(exc)[:180])
            PRICE_CATALOG_CACHE.update({"at": now_ts, "catalog": catalog, "failed": True})
            catalog = PRICE_CATALOG_CACHE.get("catalog")
    if not isinstance(catalog, dict):
        return None
    value = catalog.get(candidate.gpu_ids[0], {}).get(candidate.priority)
    return float(value) if value is not None else None


def candidate_profit_estimate(candidate: Candidate) -> dict[str, float | str] | None:
    if not PRICE_GUARD_ENABLED or len(candidate.gpu_keys) != 1:
        return None
    key = (candidate.gpu_keys[0], candidate.priority)
    expected_th = EXPECTED_TH_BY_PROFILE.get(key)
    hourly = live_hourly_price(candidate)
    if hourly is None:
        hourly = STATIC_HOURLY_USD_BY_PROFILE.get(key)
    usd_per_th_day = revenue_usd_per_th_day()
    if expected_th is None or hourly is None or not usd_per_th_day:
        return None
    revenue_day = expected_th * usd_per_th_day
    cost_day = hourly * 24
    profit_day = revenue_day - cost_day
    return {
        "gpu": key[0],
        "priority": key[1],
        "expected_th": expected_th,
        "cost_day": cost_day,
        "revenue_day": revenue_day,
        "profit_day": profit_day,
        "decision_price_usd": float(PRICE_GUARD_CACHE.get("decision_price_usd") or 0.0),
        "market_price_usd": float(PRICE_GUARD_CACHE.get("market_price_usd") or 0.0),
        "price_band_usd": PRICE_GUARD_PRICE_BAND_USD,
    }


def candidate_is_profitable(slot: str, candidate: Candidate) -> bool:
    estimate = candidate_profit_estimate(candidate)
    if estimate is None:
        log("candidate_profit_unknown_skipped", slot=slot, candidate=candidate.label)
        return False
    profit_day = float(estimate["profit_day"])
    if profit_day < PRICE_GUARD_MIN_PROFIT_DAY:
        log(
            "candidate_skipped_low_expected_profit",
            slot=slot,
            candidate=candidate.label,
            min_profit_day=PRICE_GUARD_MIN_PROFIT_DAY,
            **{key: round(value, 6) if isinstance(value, float) else value for key, value in estimate.items()},
        )
        return False
    log(
        "candidate_profit_ok",
        slot=slot,
        candidate=candidate.label,
        min_profit_day=PRICE_GUARD_MIN_PROFIT_DAY,
        **{key: round(value, 6) if isinstance(value, float) else value for key, value in estimate.items()},
    )
    return True


def slot_short(slot: str) -> str:
    return slot.rsplit("-", 1)[-1]


def slot_token(slot: str) -> str:
    return f"{WORKER_SLOT_PREFIX}{slot_short(slot)}"


def initial_candidate(slot: str) -> Candidate:
    return INITIAL.get(slot_short(slot), Candidate("RTX 4090 batch", "batch", ("4090",), 2048))


def candidate_allowed(candidate: Candidate) -> bool:
    profile = (candidate.gpu_keys[0].lower(), candidate.priority.lower())
    if profile in BLOCKED_PROFILES:
        return False
    return not ALLOWED_PRIORITIES or candidate.priority in ALLOWED_PRIORITIES


def known_candidates_for(slot: str) -> list[Candidate]:
    first = initial_candidate(slot)
    seen = {(first.priority, first.gpu_keys, first.memory)}
    result = [first]
    for candidate in FALLBACKS:
        key = (candidate.priority, candidate.gpu_keys, candidate.memory)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def candidates_for(slot: str) -> list[Candidate]:
    return [candidate for candidate in known_candidates_for(slot) if candidate_allowed(candidate)]


def candidate_availability(slot: str, candidate: Candidate) -> int | None:
    field = f"available_gpu_{candidate.priority}"
    counts: list[int] = []
    for gpu_id in candidate.gpu_ids:
        payload = {
            "gpu_classes": [gpu_id],
            "cpu": 1,
            "memory": candidate.memory,
            "storage_amount": 10737418240,
        }
        try:
            data = request("POST", f"/organizations/{ORG}/availability/sce-gpu-availability", payload)
            counts.append(int(data.get(field) or 0))
        except Exception as exc:
            log(
                "availability_check_failed",
                slot=slot,
                candidate=candidate.label,
                gpu_id=gpu_id,
                memory=candidate.memory,
                priority=candidate.priority,
                error=type(exc).__name__,
                detail=str(exc)[:180],
            )
            return None
    return max(counts, default=0)


def candidate_key(candidate: Candidate) -> tuple[str, tuple[str, ...], int] | None:
    if len(candidate.gpu_ids) != 1:
        return None
    return (candidate.priority, tuple(candidate.gpu_ids), candidate.memory)


def reserve_candidate(candidate: Candidate) -> None:
    key = candidate_key(candidate)
    if key is None or key not in CANDIDATE_BUDGET_REMAINING:
        return
    CANDIDATE_BUDGET_REMAINING[key] = max(0, CANDIDATE_BUDGET_REMAINING[key] - 1)


def capacity_slot_sources() -> list[tuple[str, list[str], str]]:
    sources = [(ORG, SLOTS, WORKER_SLOT_PREFIX)]
    if PEER_CAPACITY_ORG:
        sources.append((PEER_CAPACITY_ORG, PEER_CAPACITY_SLOTS, PEER_WORKER_SLOT_PREFIX))
    return sources


def slot_state_for(org: str, slot: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    try:
        group = request("GET", f"/organizations/{org}/projects/{PROJECT}/containers/{slot}")
    except KeyError:
        return None, []
    instances_payload = request("GET", f"/organizations/{org}/projects/{PROJECT}/containers/{slot}/instances")
    return group, list(instances_payload.get("items") or instances_payload.get("instances") or [])


def pool_workers_for_prefixes(prefixes: tuple[str, ...]) -> list[dict[str, Any]]:
    data = external_json(f"https://pearlfortune.org/api/v1/miners/{WALLET}/connections")
    rows = ((data.get("data") or {}).get("workers") or [])
    workers: list[dict[str, Any]] = []
    for worker in rows:
        name = str(worker.get("worker") or "")
        if not any(name.startswith(prefix) for prefix in prefixes):
            continue
        gpu = (((worker.get("client_info") or {}).get("gpus") or [{}])[0] or {}).get("model")
        workers.append(
            {
                "worker": name,
                "gpu": gpu,
                "stale": worker.get("stale"),
                "reported_hashrate_th": round(float(worker.get("reported_hashrate") or 0) / 1e12, 3),
                "last_stats_at": worker.get("last_stats_at"),
            }
        )
    return workers


def capacity_workers(local_workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not PEER_CAPACITY_ORG:
        return local_workers
    by_name = {str(worker.get("worker") or ""): worker for worker in local_workers}
    for worker in pool_workers_for_prefixes((PEER_WORKER_PREFIX,)):
        by_name[str(worker.get("worker") or "")] = worker
    return list(by_name.values())


def worker_instance_id(worker_name: str) -> str | None:
    marker = "-pearlfortune-"
    if marker not in worker_name:
        return None
    return worker_name.rsplit(marker, 1)[-1] or None


def live_worker_for_slot_prefix(
    slot: str,
    workers: list[dict[str, Any]],
    worker_slot_prefix: str,
    instance_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    token = f"{worker_slot_prefix}{slot_short(slot)}"
    for worker in workers:
        name = str(worker.get("worker") or "")
        if token not in name:
            continue
        if instance_ids is not None:
            current_instance = worker_instance_id(name)
            if not current_instance or current_instance not in instance_ids:
                continue
        if worker.get("stale") or float(worker.get("reported_hashrate_th") or 0) <= 0:
            continue
        return worker
    return None


def active_single_profile_counts(workers: list[dict[str, Any]]) -> dict[tuple[str, tuple[str, ...], int], int]:
    active: dict[tuple[str, tuple[str, ...], int], int] = {}
    for org, slots, worker_slot_prefix in capacity_slot_sources():
        for slot in slots:
            try:
                group, instances = slot_state_for(org, slot)
            except Exception:
                continue
            resources = ((group or {}).get("container") or {}).get("resources") or {}
            gpu_ids = tuple(resources.get("gpu_classes") or ())
            if len(gpu_ids) != 1:
                continue
            instance_ids = {str(item.get("id")) for item in instances if item.get("id")}
            if live_worker_for_slot_prefix(slot, workers, worker_slot_prefix, instance_ids):
                continue
            counts = ((group or {}).get("current_state") or {}).get("instance_status_counts") or {}
            if not (
                int(counts.get("running_count") or 0) > 0
                or int(counts.get("creating_count") or 0) > 0
                or int(counts.get("allocating_count") or 0) > 0
            ):
                continue
            key = (str((group or {}).get("priority") or "").lower(), gpu_ids, int(resources.get("memory") or 0))
            active[key] = active.get(key, 0) + 1
    return active


def active_or_pending_slot_count() -> int:
    total = 0
    for slot in SLOTS:
        try:
            group, _instances = slot_state_for(ORG, slot)
        except Exception as exc:
            log("active_slot_lookup_failed", slot=slot, error=type(exc).__name__, detail=str(exc)[:180])
            continue
        counts = ((group or {}).get("current_state") or {}).get("instance_status_counts") or {}
        if (
            int(counts.get("running_count") or 0) > 0
            or int(counts.get("creating_count") or 0) > 0
            or int(counts.get("allocating_count") or 0) > 0
        ):
            total += 1
    return total


def refresh_candidate_budgets(workers: list[dict[str, Any]]) -> None:
    CANDIDATE_BUDGET_REMAINING.clear()
    CANDIDATE_REPORTED_AVAILABLE.clear()
    active = active_single_profile_counts(workers)
    live_worker_count = sum(
        1 for worker in workers if not worker.get("stale") and float(worker.get("reported_hashrate_th") or 0) > 0
    )
    under_probe_threshold = (
        CAPACITY_ZERO_AVAILABLE_PROBE_MAX_LIVE_WORKERS > 0
        and live_worker_count < CAPACITY_ZERO_AVAILABLE_PROBE_MAX_LIVE_WORKERS
    )
    unique: dict[tuple[str, tuple[str, ...], int], Candidate] = {}
    for slot in SLOTS:
        for candidate in candidates_for(slot):
            key = candidate_key(candidate)
            if key is not None:
                unique.setdefault(key, candidate)
    for key, candidate in unique.items():
        available = candidate_availability("__capacity_budget__", candidate)
        if available is None:
            continue
        CANDIDATE_REPORTED_AVAILABLE[key] = max(0, available)
        observed_pending = active.get(key, 0)
        pending = 0 if LOW_LIVE_THIS_TICK and LOW_LIVE_IGNORE_PENDING_CAPACITY else observed_pending
        remaining = max(0, available - pending)
        probe_remaining = 0
        if available <= 0 and under_probe_threshold and CAPACITY_ZERO_AVAILABLE_PROBE_BUDGET > 0:
            probe_remaining = max(0, CAPACITY_ZERO_AVAILABLE_PROBE_BUDGET - pending)
            remaining = max(remaining, probe_remaining)
        CANDIDATE_BUDGET_REMAINING[key] = remaining
        if observed_pending or available or probe_remaining:
            log(
                "candidate_capacity_budget",
                candidate=candidate.label,
                available=available,
                pending=pending,
                observed_pending=observed_pending,
                low_live_pending_capacity_ignored=LOW_LIVE_THIS_TICK and LOW_LIVE_IGNORE_PENDING_CAPACITY,
                probe_budget=CAPACITY_ZERO_AVAILABLE_PROBE_BUDGET if under_probe_threshold else 0,
                probe_remaining=probe_remaining,
                live_workers=live_worker_count,
                probe_max_live_workers=CAPACITY_ZERO_AVAILABLE_PROBE_MAX_LIVE_WORKERS,
                remaining=remaining,
            )


def candidate_profit_day(candidate: Candidate) -> float | None:
    estimate = candidate_profit_estimate(candidate)
    if estimate is None:
        return None
    return float(estimate["profit_day"])


def candidate_expected_th(candidate: Candidate) -> float | None:
    if len(candidate.gpu_keys) != 1:
        return None
    return EXPECTED_TH_BY_PROFILE.get((candidate.gpu_keys[0], candidate.priority))


def recent_no_credits_age_seconds(now_ts: float | None = None) -> float | None:
    if LAST_NO_CREDITS_AT <= 0:
        return None
    now_ts = time.time() if now_ts is None else now_ts
    return max(0.0, now_ts - LAST_NO_CREDITS_AT)


def no_credits_backoff_active(now_ts: float | None = None) -> bool:
    now_ts = time.time() if now_ts is None else now_ts
    if now_ts >= NO_CREDITS_UNTIL:
        return False
    age = recent_no_credits_age_seconds(now_ts)
    return age is not None and age <= NO_CREDITS_BACKOFF_SECONDS


def effective_target_offset(now_ts: float | None = None) -> int:
    if TARGET_OFFSET <= 0:
        if COORDINATED_TARGET_OFFSETS and ORG in COORDINATED_ORGS:
            return COORDINATED_ORGS.index(ORG) * len(SLOTS)
        return 0
    funded_offset = funded_org_target_offset(now_ts)
    if funded_offset is not None:
        return funded_offset
    if COORDINATED_TARGET_OFFSETS and ORG in COORDINATED_ORGS:
        return COORDINATED_ORGS.index(ORG) * len(SLOTS)
    age = recent_no_credits_age_seconds(now_ts)
    if age is None:
        return 0
    return TARGET_OFFSET if age <= TARGET_OFFSET_RECENT_NO_CREDITS_SECONDS else 0


def refresh_slot_targets() -> None:
    global NO_CREDITS_TARGETS_UNTIL, SLOT_TARGETS_OFFSET
    now_ts = time.time()
    requested_target_offset = effective_target_offset(now_ts)
    recent_age = recent_no_credits_age_seconds(now_ts)
    org_balance_cents, org_balance_age_seconds = latest_org_balance_cents(now_ts)
    ranked_real: list[tuple[float, str, Candidate, int]] = []
    ranked_probe: list[tuple[float, str, Candidate, int]] = []
    unique: dict[tuple[str, tuple[str, ...], int], Candidate] = {}
    for slot in SLOTS:
        for candidate in candidates_for(slot):
            key = candidate_key(candidate)
            if key is not None:
                unique.setdefault(key, candidate)

    for key, candidate in unique.items():
        remaining = CANDIDATE_BUDGET_REMAINING.get(key, 0)
        if remaining <= 0:
            continue
        profit_day = candidate_profit_day(candidate)
        if profit_day is None or profit_day < PRICE_GUARD_MIN_PROFIT_DAY:
            continue
        reported_available = CANDIDATE_REPORTED_AVAILABLE.get(key, 0)
        if reported_available > 0:
            ranked_real.append((profit_day, candidate.label, candidate, min(remaining, reported_available)))
            probe_remaining = max(0, remaining - reported_available)
            if probe_remaining > 0:
                ranked_probe.append((profit_day, candidate.label, candidate, probe_remaining))
        else:
            ranked_probe.append((profit_day, candidate.label, candidate, remaining))

    ranked_real.sort(key=lambda item: (item[0], item[1]), reverse=True)
    ranked_probe.sort(key=lambda item: (item[0], item[1]), reverse=True)
    ranked_targets: list[Candidate] = []

    def append_rank_rounds(items: list[tuple[float, str, Candidate, int]]) -> None:
        # Fill targets in rank rounds instead of exhausting the top profile first.
        # Reported Salad capacity is tried before zero-availability probes.
        remaining_by_candidate = [(candidate, remaining) for _profit_day, _label, candidate, remaining in items]
        while len(ranked_targets) < requested_target_offset + len(SLOTS):
            appended = False
            next_remaining: list[tuple[Candidate, int]] = []
            for candidate, remaining in remaining_by_candidate:
                if remaining <= 0:
                    continue
                ranked_targets.append(candidate)
                appended = True
                remaining -= 1
                if remaining > 0:
                    next_remaining.append((candidate, remaining))
                if len(ranked_targets) >= requested_target_offset + len(SLOTS):
                    break
            if not appended:
                break
            remaining_by_candidate = next_remaining

    append_rank_rounds(ranked_real)
    append_rank_rounds(ranked_probe)

    target_offset = requested_target_offset
    if ranked_targets:
        max_full_window_offset = max(0, len(ranked_targets) - len(SLOTS))
        if max_full_window_offset > 0:
            target_offset = min(requested_target_offset, max_full_window_offset)
            targets = ranked_targets[target_offset : target_offset + len(SLOTS)]
        elif requested_target_offset > 0:
            target_offset = requested_target_offset % len(ranked_targets)
            targets = ranked_targets[target_offset:] + ranked_targets[:target_offset]
            targets = targets[: len(SLOTS)]
        else:
            target_offset = 0
            targets = ranked_targets[: len(SLOTS)]
    else:
        targets = []
    if not targets:
        log(
            "slot_targets_empty",
            configured_target_offset=TARGET_OFFSET,
            requested_target_offset=requested_target_offset,
            org_balance_age_seconds=round(org_balance_age_seconds, 1) if org_balance_age_seconds is not None else None,
            org_balance_cents=org_balance_cents,
            target_offset=target_offset,
            recent_no_credits_age_seconds=round(recent_age, 1) if recent_age is not None else None,
            ranked_targets=len(ranked_targets),
            ranked_real_targets=sum(item[3] for item in ranked_real),
            ranked_probe_targets=sum(item[3] for item in ranked_probe),
        )
        return
    if len(targets) < len(SLOTS):
        log(
            "slot_targets_underfilled",
            configured_target_offset=TARGET_OFFSET,
            requested_target_offset=requested_target_offset,
            org_balance_age_seconds=round(org_balance_age_seconds, 1) if org_balance_age_seconds is not None else None,
            org_balance_cents=org_balance_cents,
            target_offset=target_offset,
            recent_no_credits_age_seconds=round(recent_age, 1) if recent_age is not None else None,
            target_count=len(targets),
            slot_count=len(SLOTS),
            ranked_targets=len(ranked_targets),
            ranked_real_targets=sum(item[3] for item in ranked_real),
            ranked_probe_targets=sum(item[3] for item in ranked_probe),
        )

    SLOT_TARGETS.clear()
    for slot, candidate in zip(SLOTS, targets, strict=False):
        SLOT_TARGETS[slot] = candidate
    SLOT_TARGETS_OFFSET = requested_target_offset
    if requested_target_offset > 0 and now_ts >= NO_CREDITS_TARGETS_UNTIL:
        NO_CREDITS_TARGETS_UNTIL = now_ts + NO_CREDITS_TARGET_REFRESH_SECONDS
    log(
        "slot_targets_refreshed",
        configured_target_offset=TARGET_OFFSET,
        requested_target_offset=requested_target_offset,
        org_balance_age_seconds=round(org_balance_age_seconds, 1) if org_balance_age_seconds is not None else None,
        org_balance_cents=org_balance_cents,
        target_offset=target_offset,
        recent_no_credits_age_seconds=round(recent_age, 1) if recent_age is not None else None,
        ranked_targets=len(ranked_targets),
        ranked_real_targets=sum(item[3] for item in ranked_real),
        ranked_probe_targets=sum(item[3] for item in ranked_probe),
        targets_until=round(NO_CREDITS_TARGETS_UNTIL, 3),
        targets=[{"slot": slot, "candidate": candidate.label} for slot, candidate in SLOT_TARGETS.items()],
    )


def candidate_is_available(slot: str, candidate: Candidate) -> bool:
    if not candidate_is_profitable(slot, candidate):
        return False
    key = candidate_key(candidate)
    if key is not None and key in CANDIDATE_BUDGET_REMAINING:
        if CANDIDATE_BUDGET_REMAINING[key] <= 0:
            log(
                "candidate_skipped_capacity_reserved",
                slot=slot,
                candidate=candidate.label,
                gpu_ids=candidate.gpu_ids,
                memory=candidate.memory,
                priority=candidate.priority,
            )
            return False
        return True
    available = candidate_availability(slot, candidate)
    if available == 0:
        log(
            "candidate_skipped_no_availability",
            slot=slot,
            candidate=candidate.label,
            gpu_ids=candidate.gpu_ids,
            memory=candidate.memory,
            priority=candidate.priority,
        )
        return False
    return True


def candidate_has_reported_availability(candidate: Candidate) -> bool:
    key = candidate_key(candidate)
    if key is None:
        return False
    return CANDIDATE_REPORTED_AVAILABLE.get(key, 0) > 0


def available_candidate_from(slot: str, start_index: int) -> Candidate | None:
    candidates = candidates_for(slot)
    for offset in range(len(candidates)):
        index = (start_index + offset) % len(candidates)
        candidate = candidates[index]
        if candidate_is_available(slot, candidate):
            SLOT_CANDIDATE_INDEX[slot] = index
            reserve_candidate(candidate)
            clear_no_gpu_state("candidate_available", slot=slot, candidate=candidate.label)
            return candidate
    log("candidate_none_available", slot=slot, start_index=start_index)
    note_no_gpu_available("candidate_none_available", slot=slot, start_index=start_index)
    return None


def current_candidate(slot: str) -> Candidate:
    candidates = candidates_for(slot)
    index = SLOT_CANDIDATE_INDEX.get(slot, 0) % len(candidates)
    return candidates[index]


def set_current_candidate(slot: str, candidate: Candidate) -> None:
    candidates = candidates_for(slot)
    for index, item in enumerate(candidates):
        if item == candidate:
            SLOT_CANDIDATE_INDEX[slot] = index
            return
    SLOT_CANDIDATE_INDEX[slot] = 0


def advance_candidate(slot: str) -> Candidate | None:
    return available_candidate_from(slot, SLOT_CANDIDATE_INDEX.get(slot, 0) + 1)


def best_available_candidate(slot: str, *, exclude: Candidate | None = None, reason: str = "best_available") -> Candidate | None:
    ranked: list[tuple[float, str, Candidate]] = []
    for candidate in candidates_for(slot):
        if exclude is not None and candidate == exclude:
            continue
        profit_day = candidate_profit_day(candidate)
        ranked.append((profit_day if profit_day is not None else float("-inf"), candidate.label, candidate))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _profit_day, _label, candidate in ranked:
        if candidate_is_available(slot, candidate):
            set_current_candidate(slot, candidate)
            reserve_candidate(candidate)
            clear_no_gpu_state("best_candidate_available", slot=slot, candidate=candidate.label, rotation_reason=reason)
            return candidate
    log("best_candidate_none_available", slot=slot, reason=reason)
    note_no_gpu_available("best_candidate_none_available", slot=slot, rotation_reason=reason)
    return None


def coordinated_rotation_candidate(
    slot: str,
    *,
    exclude: Candidate | None = None,
    reason: str = "coordinated_rotation",
) -> Candidate | None:
    target = SLOT_TARGETS.get(slot)
    if target is not None and target != exclude and candidate_is_available(slot, target):
        set_current_candidate(slot, target)
        reserve_candidate(target)
        clear_no_gpu_state("coordinated_candidate_available", slot=slot, candidate=target.label, rotation_reason=reason)
        return target
    return best_available_candidate(slot, exclude=exclude, reason=reason)


def select_available_candidate(slot: str, candidate: Candidate) -> Candidate | None:
    if candidate_is_available(slot, candidate):
        reserve_candidate(candidate)
        return candidate
    return advance_candidate(slot)


def miner_command(slot: str) -> list[str]:
    worker = f"{WORKER_PREFIX}-{slot_token(slot)}-pearlfortune-${{HOSTNAME:-node}}"
    script = f"""set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
WORKER="{worker}"
URL="{MINER_PACKAGE_URL}"
PROXY="global.pearlfortune.org:443"
WALLET="{WALLET}"
ROOT=/opt/pearlfortune
MINER_BINARY="{MINER_BINARY}"
MINER="$ROOT/pearlfortune/$MINER_BINARY"
log() {{ echo "[pearlfortune-prl-{MINER_PACKAGE_VERSION}] $*"; }}
log "starting worker $WORKER"
for attempt in 1 2 3; do
  if apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tar procps pciutils; then break; fi
  log "apt setup failed attempt $attempt"; sleep 15
done
mkdir -p "$ROOT" /var/log/pearlfortune
cd "$ROOT"
until [ -x "$MINER" ]; do
  rm -f pearlfortune.tar.gz
  rm -rf pearlfortune
  log "downloading PearlFortune miner $URL"
  if curl -L --fail --retry 20 --retry-delay 5 --connect-timeout 20 --max-time 240 -o pearlfortune.tar.gz "$URL"; then
    if tar xzf pearlfortune.tar.gz; then
      if [ -f "$MINER" ]; then chmod +x "$MINER" && break; fi
      if [ -f "$ROOT/pearlfortune/miner" ]; then
        MINER="$ROOT/pearlfortune/miner"
        chmod +x "$MINER"
        break
      fi
      log "miner binary not found: $MINER_BINARY"
    fi
  fi
  log "download/extract failed; retrying in 20s"; sleep 20
done
while true; do
  log "launching PearlFortune miner worker=$WORKER proxy=$PROXY"
  cd "$ROOT/pearlfortune"
  "$MINER" --proxy "$PROXY" --address "$WALLET" --worker "$WORKER" -gpu
  rc=$?; log "miner exited rc=$rc; restarting in 10s"; sleep 10
done
"""
    return ["/bin/bash", "-lc", script]


def container_payload(slot: str, candidate: Candidate) -> dict[str, Any]:
    shm_size = min(1024, max(64, candidate.memory // 2))
    return {
        "name": slot,
        "display_name": expected_display_name(slot, candidate),
        "replicas": 1,
        "priority": candidate.priority,
        "autostart_policy": False,
        "restart_policy": "always",
        "country_codes": [],
        "container": {
            "image": "docker.io/nvidia/cuda:12.8.0-runtime-ubuntu24.04",
            "image_caching": True,
            "command": miner_command(slot),
            "environment_variables": {},
            "priority": candidate.priority,
            "resources": {
                "cpu": 1,
                "memory": candidate.memory,
                "shm_size": shm_size,
                "storage_amount": 10737418240,
                "gpu_classes": candidate.gpu_ids,
            },
        },
    }


def slot_state(slot: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    return slot_state_for(ORG, slot)


def create_slot(slot: str, candidate: Candidate, *, start_after: bool = True) -> None:
    payload = container_payload(slot, candidate)
    try:
        request("POST", f"/organizations/{ORG}/projects/{PROJECT}/containers", payload)
        SLOT_LAST_PATCH[slot] = time.time()
        record_slot_action_state(slot, "created", "create_slot", candidate.label)
        log("slot_created", slot=slot, candidate=candidate.label, gpu_ids=candidate.gpu_ids, memory=candidate.memory)
        if start_after:
            start_slot(slot, "after_create")
        else:
            log("slot_start_deferred_no_credits", slot=slot, candidate=candidate.label, reason="after_create")
    except requests.HTTPError as exc:
        log("slot_create_failed", slot=slot, candidate=candidate.label, status=exc.response.status_code, error=exc.response.text[:180])
        raise


def patch_slot(slot: str, candidate: Candidate, reason: str, *, start_after: bool = True) -> bool:
    payload = container_payload(slot, candidate)
    payload.pop("name", None)
    try:
        request(
            "PATCH",
            f"/organizations/{ORG}/projects/{PROJECT}/containers/{slot}",
            payload,
            patch=True,
        )
        SLOT_LAST_PATCH[slot] = time.time()
        record_slot_action_state(slot, "patched", reason, candidate.label)
        log("slot_patched", slot=slot, candidate=candidate.label, gpu_ids=candidate.gpu_ids, memory=candidate.memory, reason=reason)
        if start_after:
            start_slot(slot, f"after_patch:{reason}")
        else:
            log("slot_start_deferred_after_patch", slot=slot, candidate=candidate.label, reason=f"after_patch:{reason}")
        return True
    except requests.HTTPError as exc:
        log("slot_patch_failed", slot=slot, candidate=candidate.label, status=exc.response.status_code, error=exc.response.text[:180])
        return False


def reallocate(slot: str, instance_id: str, reason: str) -> None:
    try:
        request("POST", f"/organizations/{ORG}/projects/{PROJECT}/containers/{slot}/instances/{instance_id}/reallocate")
        log("instance_reallocated", slot=slot, instance_id=instance_id, reason=reason)
    except requests.HTTPError as exc:
        log("reallocate_failed", slot=slot, instance_id=instance_id, status=exc.response.status_code, error=exc.response.text[:180])
    except Exception as exc:
        log("reallocate_failed", slot=slot, instance_id=instance_id, error=type(exc).__name__)


def reallocate_pending_instances(slot: str, instances: list[dict[str, Any]], reason: str) -> list[str]:
    reallocated: list[str] = []
    for instance in instances:
        if instance.get("ready") or instance.get("started"):
            continue
        instance_id = str(instance.get("id") or "")
        if not instance_id:
            continue
        reallocate(slot, instance_id, reason)
        reallocated.append(instance_id)
    return reallocated


def start_slot(slot: str, reason: str) -> None:
    try:
        request("POST", f"/organizations/{ORG}/projects/{PROJECT}/containers/{slot}/start")
        SLOT_LAST_PATCH[slot] = time.time()
        record_slot_action_state(slot, "started", reason)
        clear_no_credits_state("slot_start_requested", slot=slot, start_reason=reason)
        log("slot_start_requested", slot=slot, reason=reason)
    except requests.HTTPError as exc:
        error_text = exc.response.text
        if "no_credits_available" in error_text or "no credits" in error_text.lower():
            note_no_credits(error_text)
        log("slot_start_failed", slot=slot, reason=reason, status=exc.response.status_code, error=error_text[:180])
    except Exception as exc:
        log("slot_start_failed", slot=slot, reason=reason, error=type(exc).__name__)


def fresh_workers() -> list[dict[str, Any]]:
    data = external_json(f"https://pearlfortune.org/api/v1/miners/{WALLET}/connections")
    rows = ((data.get("data") or {}).get("workers") or [])
    workers: list[dict[str, Any]] = []
    for worker in rows:
        name = str(worker.get("worker") or "")
        if not name.startswith(POOL_WORKER_PREFIX):
            continue
        gpu = (((worker.get("client_info") or {}).get("gpus") or [{}])[0] or {}).get("model")
        workers.append(
            {
                "worker": name,
                "gpu": gpu,
                "stale": worker.get("stale"),
                "reported_hashrate_th": round(float(worker.get("reported_hashrate") or 0) / 1e12, 3),
                "last_stats_at": worker.get("last_stats_at"),
            }
        )
    return workers


def live_worker_for_slot(slot: str, workers: list[dict[str, Any]], instance_ids: set[str]) -> dict[str, Any] | None:
    return live_worker_for_slot_prefix(slot, workers, WORKER_SLOT_PREFIX, instance_ids)


def active_best_profile_slots(workers: list[dict[str, Any]], best_candidate: Candidate) -> int:
    count = 0
    for org, slots, worker_slot_prefix in capacity_slot_sources():
        for slot in slots:
            try:
                group, instances = slot_state_for(org, slot)
            except Exception:
                continue
            if not desired_matches(group, best_candidate):
                continue
            _group, instances = slot_state_for(org, slot)
            instance_ids = {str(item.get("id")) for item in instances if item.get("id")}
            if live_worker_for_slot_prefix(slot, workers, worker_slot_prefix, instance_ids):
                continue
            counts = (group.get("current_state") or {}).get("instance_status_counts") or {}
            if (
                int(counts.get("running_count") or 0) > 0
                or int(counts.get("creating_count") or 0) > 0
                or int(counts.get("allocating_count") or 0) > 0
            ):
                count += 1
    return count


def desired_matches(group: dict[str, Any], candidate: Candidate) -> bool:
    resources = ((group.get("container") or {}).get("resources") or {})
    return (
        str(group.get("priority") or "").lower() == candidate.priority
        and list(resources.get("gpu_classes") or []) == candidate.gpu_ids
        and int(group.get("replicas") or 0) == 1
        and int(resources.get("cpu") or 0) == 1
        and int(resources.get("memory") or 0) == candidate.memory
    )


def expected_display_name(slot: str, candidate: Candidate) -> str:
    return f"{DISPLAY_PREFIX} {candidate.label} {slot_short(slot)}"


def display_matches(slot: str, group: dict[str, Any], candidate: Candidate) -> bool:
    return str(group.get("display_name") or "") == expected_display_name(slot, candidate)


def candidate_matching_group(slot: str, group: dict[str, Any]) -> Candidate | None:
    for candidate in candidates_for(slot):
        if desired_matches(group, candidate):
            return candidate
    return None


def existing_candidate_matching_group(slot: str, group: dict[str, Any]) -> Candidate | None:
    for candidate in known_candidates_for(slot):
        if desired_matches(group, candidate):
            return candidate
    return None


def best_available_upgrade(slot: str, current: Candidate) -> tuple[Candidate, float, float, float, float] | None:
    current_profit = candidate_profit_day(current)
    current_th = candidate_expected_th(current)
    if current_profit is None or current_th is None:
        return None
    ranked: list[tuple[float, float, str, Candidate]] = []
    for candidate in candidates_for(slot):
        if candidate == current:
            continue
        profit_day = candidate_profit_day(candidate)
        if profit_day is None or profit_day < current_profit + OPTIMIZE_LIVE_MIN_PROFIT_DELTA:
            continue
        expected_th = candidate_expected_th(candidate)
        if expected_th is None or expected_th < current_th + OPTIMIZE_LIVE_MIN_TH_DELTA:
            continue
        key = candidate_key(candidate)
        if key is not None and key in CANDIDATE_BUDGET_REMAINING and CANDIDATE_BUDGET_REMAINING[key] <= 0:
            continue
        if OPTIMIZE_LIVE_REQUIRE_REPORTED_AVAILABLE and not candidate_has_reported_availability(candidate):
            log(
                "live_upgrade_skipped_no_reported_availability",
                slot=slot,
                candidate=candidate.label,
            )
            continue
        ranked.append((profit_day, expected_th, candidate.label, candidate))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for profit_day, expected_th, _label, candidate in ranked:
        if candidate_is_available(slot, candidate):
            return candidate, profit_day, current_profit, expected_th, current_th
    return None


def reconcile_slot(slot: str, workers: list[dict[str, Any]]) -> dict[str, Any]:
    global BEST_UPGRADE_REMAINING, LAST_LIVE_UPGRADE_AT
    group, instances = slot_state(slot)
    if group is None:
        now_ts = time.time()
        no_credits_active = no_credits_backoff_active(now_ts)
        candidate = SLOT_TARGETS.get(slot) or current_candidate(slot)
        candidate = select_available_candidate(slot, candidate)
        if candidate is None:
            return {"slot": slot, "state": "no_viable_candidate"}
        create_slot(slot, candidate, start_after=not no_credits_active)
        state = "created" if not no_credits_active else "created_no_credits_backoff"
        result = {"slot": slot, "state": state, "candidate": candidate.label}
        if no_credits_active:
            result["backoff_remaining_seconds"] = round(NO_CREDITS_UNTIL - now_ts, 1)
        return result
    actual_candidate = candidate_matching_group(slot, group)
    if actual_candidate:
        set_current_candidate(slot, actual_candidate)
    candidate = actual_candidate or current_candidate(slot)

    instance_ids = {str(item.get("id")) for item in instances if item.get("ready") or item.get("started")}
    live = live_worker_for_slot(slot, workers, instance_ids)
    current_state = group.get("current_state") or {}
    counts = current_state.get("instance_status_counts") or {}
    running = int(counts.get("running_count") or 0)
    creating = int(counts.get("creating_count") or 0)
    allocating = int(counts.get("allocating_count") or 0)
    status = str(current_state.get("status") or "")
    now_ts = time.time()
    no_credits_active = no_credits_backoff_active(now_ts)

    if status != "stopped" and running == 0 and creating == 0 and allocating == 0 and no_credits_active:
        return {
            "slot": slot,
            "state": "no_credits_backoff",
            "candidate": candidate.label,
            "counts": counts,
            "backoff_remaining_seconds": round(NO_CREDITS_UNTIL - now_ts, 1),
        }

    if status == "stopped":
        stopped_age = recent_guard_stop_age_seconds(slot, now_ts)
        stopped_age = (
            stopped_age
            if stopped_age is not None
            else timestamp_age_seconds(current_state.get("finish_time") or current_state.get("start_time"), now_ts)
        )
        if (
            STOPPED_RESTART_COOLDOWN_SECONDS > 0
            and stopped_age is not None
            and stopped_age < STOPPED_RESTART_COOLDOWN_SECONDS
        ):
            return {
                "slot": slot,
                "state": "stopped_restart_cooldown",
                "candidate": candidate.label,
                "counts": counts,
                "stopped_age_seconds": round(stopped_age, 1),
                "cooldown_seconds": STOPPED_RESTART_COOLDOWN_SECONDS,
            }
        candidate = SLOT_TARGETS.get(slot) or actual_candidate or initial_candidate(slot)
        set_current_candidate(slot, candidate)
        SLOT_ALLOCATING_SINCE.pop(slot, None)
        SLOT_EMPTY_CREATING_SINCE.pop(slot, None)
        SLOT_CREATING_SINCE.pop(slot, None)
        SLOT_CREATING_PROGRESS.pop(slot, None)
        SLOT_PROFILE_MISMATCH_SINCE.pop(slot, None)
        if not desired_matches(group, candidate):
            if not candidate_is_profitable(slot, candidate):
                return {"slot": slot, "state": "stopped_no_viable_candidate", "counts": counts}
            patched = patch_slot(slot, candidate, "stopped_profile_mismatch", start_after=not no_credits_active)
            if not patched:
                existing = actual_candidate or existing_candidate_matching_group(slot, group)
                if existing is not None and candidate_is_profitable(slot, existing) and not no_credits_active:
                    start_slot(slot, "after_failed_patch:stopped_profile_mismatch_existing_profitable")
                    return {
                        "slot": slot,
                        "state": "started_existing_after_patch_failed",
                        "candidate": existing.label,
                        "wanted_candidate": candidate.label,
                        "counts": counts,
                    }
                return {
                    "slot": slot,
                    "state": "stopped_profile_patch_failed",
                    "candidate": candidate.label,
                    "counts": counts,
                }
            state = "patched_stopped_profile" if not no_credits_active else "patched_stopped_profile_no_credits_backoff"
            result = {"slot": slot, "state": state, "candidate": candidate.label, "counts": counts}
            if no_credits_active:
                result["backoff_remaining_seconds"] = round(NO_CREDITS_UNTIL - now_ts, 1)
            return result
        if not display_matches(slot, group, candidate):
            patched = patch_slot(slot, candidate, "stopped_display_mismatch", start_after=not no_credits_active)
            if not patched:
                if candidate_is_profitable(slot, candidate) and not no_credits_active:
                    start_slot(slot, "after_failed_patch:stopped_display_mismatch_existing_profitable")
                    return {
                        "slot": slot,
                        "state": "started_existing_after_display_patch_failed",
                        "candidate": candidate.label,
                        "counts": counts,
                    }
                return {
                    "slot": slot,
                    "state": "stopped_display_patch_failed",
                    "candidate": candidate.label,
                    "counts": counts,
                }
            state = "patched_stopped_display" if not no_credits_active else "patched_stopped_display_no_credits_backoff"
            result = {"slot": slot, "state": state, "candidate": candidate.label, "counts": counts}
            if no_credits_active:
                result["backoff_remaining_seconds"] = round(NO_CREDITS_UNTIL - now_ts, 1)
            return result
        if not candidate_is_profitable(slot, candidate):
            return {"slot": slot, "state": "stopped_no_viable_candidate", "candidate": candidate.label, "counts": counts}
        if no_credits_active:
            return {
                "slot": slot,
                "state": "no_credits_backoff",
                "candidate": candidate.label,
                "counts": counts,
                "backoff_remaining_seconds": round(NO_CREDITS_UNTIL - now_ts, 1),
            }
        start_slot(slot, "stopped_with_replicas")
        return {"slot": slot, "state": "start_requested", "candidate": candidate.label, "counts": counts}

    if live:
        SLOT_RUNNING_WITHOUT_POOL_SINCE.pop(slot, None)
        SLOT_EMPTY_DEPLOYING_SINCE.pop(slot, None)
        SLOT_EMPTY_CREATING_SINCE.pop(slot, None)
        SLOT_ALLOCATING_SINCE.pop(slot, None)
        SLOT_CREATING_SINCE.pop(slot, None)
        SLOT_CREATING_PROGRESS.pop(slot, None)
        SLOT_PROFILE_MISMATCH_SINCE.pop(slot, None)
        current_for_profit = actual_candidate or existing_candidate_matching_group(slot, group) or candidate
        last_patch = SLOT_LAST_PATCH.get(slot, 0.0)
        if (
            ALLOW_LIVE_UPGRADES_THIS_TICK
            and OPTIMIZE_LIVE_SECONDS < 999999
            and now_ts - last_patch >= OPTIMIZE_LIVE_SECONDS
            and now_ts - LAST_LIVE_UPGRADE_AT >= OPTIMIZE_LIVE_INTERVAL_SECONDS
        ):
            upgrade = best_available_upgrade(slot, current_for_profit)
            if upgrade is not None:
                upgrade_candidate, upgrade_profit_day, current_profit_day, upgrade_expected_th, current_expected_th = upgrade
                patch_slot(slot, upgrade_candidate, "live_profit_upgrade", start_after=False)
                reserve_candidate(upgrade_candidate)
                LAST_LIVE_UPGRADE_AT = now_ts
                reallocated = []
                for instance in instances:
                    if not (instance.get("ready") or instance.get("started")):
                        continue
                    instance_id = str(instance.get("id") or "")
                    if not instance_id:
                        continue
                    reallocate(slot, instance_id, "live_profit_upgrade")
                    reallocated.append(instance_id)
                return {
                    "slot": slot,
                    "state": "live_profit_upgrade_requested",
                    "candidate": upgrade_candidate.label,
                    "previous_candidate": current_for_profit.label,
                    "expected_profit_day": round(upgrade_profit_day, 6),
                    "previous_expected_profit_day": round(current_profit_day, 6),
                    "expected_th": round(upgrade_expected_th, 6),
                    "previous_expected_th": round(current_expected_th, 6),
                    "expected_th_delta": round(upgrade_expected_th - current_expected_th, 6),
                    "reallocated_instances": reallocated,
                    "counts": counts,
                }
        return {
            "slot": slot,
            "state": "live_protected",
            "candidate": group.get("display_name"),
            "worker": live,
            "counts": counts,
        }

    best_candidate = Candidate("RTX 4090 batch", "batch", ("4090",), 2048)
    if (
        UPGRADE_TO_BEST_SECONDS < 999999
        and running == 0
        and creating == 0
        and not desired_matches(group, best_candidate)
    ):
        last_patch = SLOT_LAST_PATCH.get(slot, now_ts if UPGRADE_TO_BEST_SECONDS > 0 else 0.0)
        if (
            now_ts - last_patch >= UPGRADE_TO_BEST_SECONDS
            and BEST_UPGRADE_REMAINING > 0
            and candidate_is_available(slot, best_candidate)
        ):
            set_current_candidate(slot, best_candidate)
            SLOT_PROFILE_MISMATCH_SINCE.pop(slot, None)
            SLOT_ALLOCATING_SINCE.pop(slot, None)
            SLOT_EMPTY_CREATING_SINCE.pop(slot, None)
            SLOT_CREATING_SINCE.pop(slot, None)
            SLOT_CREATING_PROGRESS.pop(slot, None)
            patch_slot(slot, best_candidate, "upgrade_to_available_4090", start_after=allocating == 0 and status != "deploying")
            BEST_UPGRADE_REMAINING -= 1
            reserve_candidate(best_candidate)
            return {
                "slot": slot,
                "state": "upgraded_to_available_4090",
                "candidate": best_candidate.label,
                "available": BEST_UPGRADE_REMAINING,
                "counts": counts,
            }

    if not desired_matches(group, candidate) and running == 0 and creating == 0:
        since = SLOT_PROFILE_MISMATCH_SINCE.setdefault(slot, now_ts)
        if now_ts - since < PROFILE_MISMATCH_GRACE_SECONDS:
            return {
                "slot": slot,
                "state": "profile_mismatch_grace",
                "candidate": candidate.label,
                "counts": counts,
                "grace_remaining_seconds": round(PROFILE_MISMATCH_GRACE_SECONDS - (now_ts - since), 1),
            }
        SLOT_PROFILE_MISMATCH_SINCE.pop(slot, None)
        SLOT_ALLOCATING_SINCE.pop(slot, None)
        SLOT_EMPTY_CREATING_SINCE.pop(slot, None)
        SLOT_CREATING_SINCE.pop(slot, None)
        SLOT_CREATING_PROGRESS.pop(slot, None)
        candidate = select_available_candidate(slot, candidate)
        if candidate is None:
            return {"slot": slot, "state": "profile_mismatch_no_viable_candidate", "counts": counts}
        patched = patch_slot(slot, candidate, "desired_profile_mismatch", start_after=allocating == 0 and status != "deploying")
        reallocated = reallocate_pending_instances(slot, instances, "desired_profile_mismatch") if patched and allocating > 0 else []
        return {
            "slot": slot,
            "state": "patched_profile",
            "candidate": candidate.label,
            "reallocated_instances": reallocated,
            "counts": counts,
        }
    if desired_matches(group, candidate):
        SLOT_PROFILE_MISMATCH_SINCE.pop(slot, None)

    if running > 0:
        SLOT_EMPTY_DEPLOYING_SINCE.pop(slot, None)
        SLOT_EMPTY_CREATING_SINCE.pop(slot, None)
        SLOT_ALLOCATING_SINCE.pop(slot, None)
        SLOT_CREATING_SINCE.pop(slot, None)
        SLOT_CREATING_PROGRESS.pop(slot, None)
        SLOT_PROFILE_MISMATCH_SINCE.pop(slot, None)
        since = SLOT_RUNNING_WITHOUT_POOL_SINCE.setdefault(slot, now_ts)
        if now_ts - since >= RUNNING_WITHOUT_POOL_SECONDS:
            for instance in instances:
                if instance.get("ready") or instance.get("started"):
                    reallocate(slot, str(instance["id"]), "running_without_pool_worker")
            SLOT_RUNNING_WITHOUT_POOL_SINCE[slot] = now_ts
        return {"slot": slot, "state": "running_without_pool", "candidate": candidate.label, "counts": counts}

    SLOT_RUNNING_WITHOUT_POOL_SINCE.pop(slot, None)
    if creating > 0:
        SLOT_EMPTY_DEPLOYING_SINCE.pop(slot, None)
        SLOT_ALLOCATING_SINCE.pop(slot, None)
        SLOT_PROFILE_MISMATCH_SINCE.pop(slot, None)
        if not instances:
            since = SLOT_EMPTY_CREATING_SINCE.setdefault(slot, state_since_timestamp(current_state, now_ts))
            if now_ts - since >= EMPTY_CREATING_SECONDS:
                SLOT_EMPTY_CREATING_SINCE[slot] = now_ts
                SLOT_CREATING_SINCE.pop(slot, None)
                SLOT_CREATING_PROGRESS.pop(slot, None)
                next_candidate = coordinated_rotation_candidate(
                    slot,
                    exclude=candidate,
                    reason=f"creating_empty_{EMPTY_CREATING_SECONDS}s",
                )
                if next_candidate is None:
                    return {
                        "slot": slot,
                        "state": "creating_empty_no_viable_rotation_candidate",
                        "candidate": candidate.label,
                        "counts": counts,
                    }
                patch_slot(slot, next_candidate, f"creating_empty_{EMPTY_CREATING_SECONDS}s", start_after=False)
                return {
                    "slot": slot,
                    "state": "rotated_empty_creating",
                    "candidate": next_candidate.label,
                    "counts": counts,
                }
            return {
                "slot": slot,
                "state": "creating_empty",
                "candidate": candidate.label,
                "counts": counts,
                "grace_remaining_seconds": round(EMPTY_CREATING_SECONDS - (now_ts - since), 1),
            }
        SLOT_EMPTY_CREATING_SINCE.pop(slot, None)
        since = SLOT_CREATING_SINCE.setdefault(slot, state_since_timestamp(current_state, now_ts))
        max_progress = max((float(instance.get("pulling_progress") or 0) for instance in instances), default=0.0)
        previous_progress = SLOT_CREATING_PROGRESS.get(slot, -1.0)
        if max_progress > previous_progress + 0.001:
            SLOT_CREATING_PROGRESS[slot] = max_progress
            SLOT_CREATING_SINCE[slot] = now_ts
        elif now_ts - since >= CREATE_PROGRESS_SECONDS:
            SLOT_CREATING_SINCE[slot] = now_ts
            next_candidate = coordinated_rotation_candidate(
                slot,
                exclude=candidate,
                reason=f"creating_no_progress_{CREATE_PROGRESS_SECONDS}s",
            )
            if next_candidate is None:
                return {"slot": slot, "state": "creating_no_viable_rotation_candidate", "candidate": candidate.label, "counts": counts}
            patched = patch_slot(
                slot,
                next_candidate,
                f"creating_no_progress_{CREATE_PROGRESS_SECONDS}s",
                start_after=False,
            )
            reallocated = reallocate_pending_instances(slot, instances, f"creating_no_progress_{CREATE_PROGRESS_SECONDS}s") if patched else []
            return {
                "slot": slot,
                "state": "rotated_creating_no_progress",
                "candidate": next_candidate.label,
                "reallocated_instances": reallocated,
                "counts": counts,
            }
        return {"slot": slot, "state": "creating", "candidate": candidate.label, "counts": counts}

    if allocating > 0:
        SLOT_EMPTY_DEPLOYING_SINCE.pop(slot, None)
        SLOT_EMPTY_CREATING_SINCE.pop(slot, None)
        SLOT_CREATING_SINCE.pop(slot, None)
        SLOT_CREATING_PROGRESS.pop(slot, None)
        SLOT_PROFILE_MISMATCH_SINCE.pop(slot, None)
        since = SLOT_ALLOCATING_SINCE.setdefault(slot, state_since_timestamp(current_state, now_ts))
        target = SLOT_TARGETS.get(slot)
        retarget_grace_seconds = (
            LOW_LIVE_ALLOCATING_RETARGET_AVAILABLE_SECONDS
            if LOW_LIVE_THIS_TICK and LOW_LIVE_ALLOCATING_RETARGET_AVAILABLE_SECONDS >= 0
            else ALLOCATING_RETARGET_AVAILABLE_SECONDS
        )
        if (
            target is not None
            and target != candidate
            and retarget_grace_seconds < 999999
            and candidate_has_reported_availability(target)
            and candidate_is_profitable(slot, target)
            and candidate_is_available(slot, target)
        ):
            retarget_since = SLOT_ALLOCATING_RETARGET_SINCE.setdefault(slot, state_since_timestamp(current_state, now_ts))
            if now_ts - retarget_since >= retarget_grace_seconds:
                SLOT_ALLOCATING_RETARGET_SINCE[slot] = now_ts
                SLOT_ALLOCATING_SINCE[slot] = now_ts
                set_current_candidate(slot, target)
                patched = patch_slot(
                    slot,
                    target,
                    f"allocating_retarget_reported_available_{retarget_grace_seconds}s",
                    start_after=False,
                )
                reallocated = (
                    reallocate_pending_instances(
                        slot,
                        instances,
                        f"allocating_retarget_reported_available_{retarget_grace_seconds}s",
                    )
                    if patched
                    else []
                )
                if patched:
                    reserve_candidate(target)
                return {
                    "slot": slot,
                    "state": "retargeted_allocating_reported_available",
                    "candidate": target.label,
                    "previous_candidate": candidate.label,
                    "low_live": LOW_LIVE_THIS_TICK,
                    "retarget_grace_seconds": retarget_grace_seconds,
                    "reallocated_instances": reallocated,
                    "counts": counts,
                }
            return {
                "slot": slot,
                "state": "allocating_retarget_available_grace",
                "candidate": candidate.label,
                "target_candidate": target.label,
                "low_live": LOW_LIVE_THIS_TICK,
                "retarget_grace_seconds": retarget_grace_seconds,
                "counts": counts,
                "grace_remaining_seconds": round(
                    retarget_grace_seconds - (now_ts - retarget_since),
                    1,
                ),
            }
        SLOT_ALLOCATING_RETARGET_SINCE.pop(slot, None)
        if now_ts - since >= TRY_SECONDS:
            SLOT_ALLOCATING_SINCE[slot] = now_ts
            next_candidate = coordinated_rotation_candidate(
                slot,
                exclude=candidate,
                reason=f"allocating_timeout_{TRY_SECONDS}s",
            )
            if next_candidate is None:
                return {"slot": slot, "state": "allocating_no_viable_rotation_candidate", "candidate": candidate.label, "counts": counts}
            patched = patch_slot(
                slot,
                next_candidate,
                f"allocating_timeout_{TRY_SECONDS}s",
                start_after=False,
            )
            reallocated = reallocate_pending_instances(slot, instances, f"allocating_timeout_{TRY_SECONDS}s") if patched else []
            return {
                "slot": slot,
                "state": "rotated",
                "candidate": next_candidate.label,
                "reallocated_instances": reallocated,
                "counts": counts,
            }
        return {"slot": slot, "state": "allocating", "candidate": candidate.label, "counts": counts}

    if status == "deploying":
        SLOT_ALLOCATING_SINCE.pop(slot, None)
        SLOT_EMPTY_CREATING_SINCE.pop(slot, None)
        SLOT_CREATING_SINCE.pop(slot, None)
        SLOT_CREATING_PROGRESS.pop(slot, None)
        SLOT_PROFILE_MISMATCH_SINCE.pop(slot, None)
        since = SLOT_EMPTY_DEPLOYING_SINCE.setdefault(slot, state_since_timestamp(current_state, now_ts))
        if now_ts - since >= TRY_SECONDS:
            next_candidate = coordinated_rotation_candidate(
                slot,
                exclude=candidate,
                reason=f"empty_deploying_timeout_{TRY_SECONDS}s",
            )
            if next_candidate is None:
                return {"slot": slot, "state": "deploying_no_viable_rotation_candidate", "candidate": candidate.label, "counts": counts}
            SLOT_EMPTY_DEPLOYING_SINCE[slot] = now_ts
            patch_slot(slot, next_candidate, f"empty_deploying_timeout_{TRY_SECONDS}s", start_after=False)
            return {"slot": slot, "state": "rotated_empty_deploying", "candidate": next_candidate.label, "counts": counts}
        return {"slot": slot, "state": "deploying_empty", "candidate": candidate.label, "counts": counts}

    SLOT_ALLOCATING_SINCE.pop(slot, None)
    SLOT_EMPTY_CREATING_SINCE.pop(slot, None)
    SLOT_CREATING_SINCE.pop(slot, None)
    SLOT_CREATING_PROGRESS.pop(slot, None)
    SLOT_PROFILE_MISMATCH_SINCE.pop(slot, None)
    return {"slot": slot, "state": "waiting", "candidate": candidate.label, "counts": counts}


def tick() -> None:
    global BEST_UPGRADE_REMAINING, ALLOW_LIVE_UPGRADES_THIS_TICK, LOW_LIVE_THIS_TICK
    try:
        workers = fresh_workers()
    except Exception as exc:
        log("pool_fetch_failed_skip_tick", error=type(exc).__name__, detail=str(exc)[:180])
        return
    best_candidate = Candidate("RTX 4090 batch", "batch", ("4090",), 2048)
    live_workers = [w for w in workers if not w.get("stale") and float(w.get("reported_hashrate_th") or 0) > 0]
    LOW_LIVE_THIS_TICK = len(live_workers) < LOW_LIVE_MIN_LIVE_WORKERS
    now_ts = time.time()
    if now_ts < NO_GPU_UNTIL:
        log(
            "no_gpu_sleep",
            remaining_seconds=round(NO_GPU_UNTIL - now_ts, 1),
            live_workers=len(live_workers),
            total_th=round(sum(float(w.get("reported_hashrate_th") or 0) for w in live_workers), 3),
        )
    active_or_pending_slots = active_or_pending_slot_count()
    if OPTIMIZE_LIVE_REQUIRE_FULL_SLOTS:
        ALLOW_LIVE_UPGRADES_THIS_TICK = len(live_workers) >= len(SLOTS) or (
            active_or_pending_slots >= len(SLOTS)
            and len(live_workers) >= OPTIMIZE_LIVE_MIN_LIVE_WORKERS
        )
    else:
        ALLOW_LIVE_UPGRADES_THIS_TICK = len(live_workers) >= OPTIMIZE_LIVE_MIN_LIVE_WORKERS
    refresh_candidate_budgets(capacity_workers(workers))
    org_balance_cents, org_balance_age_seconds = latest_org_balance_cents()
    if org_balance_cents is not None and org_balance_cents > 0:
        clear_no_credits_state(
            "confirmed_org_balance",
            org_balance_cents=org_balance_cents,
            org_balance_age_seconds=round(org_balance_age_seconds, 1) if org_balance_age_seconds is not None else None,
        )
    if not SLOT_TARGETS or now_ts >= NO_CREDITS_TARGETS_UNTIL:
        refresh_slot_targets()
    else:
        log(
            "slot_targets_retained_no_credits",
            targets_until=round(NO_CREDITS_TARGETS_UNTIL, 3),
            remaining_seconds=round(NO_CREDITS_TARGETS_UNTIL - now_ts, 1),
            targets=[{"slot": slot, "candidate": candidate.label} for slot, candidate in SLOT_TARGETS.items()],
        )
    best_key = candidate_key(best_candidate)
    BEST_UPGRADE_REMAINING = CANDIDATE_BUDGET_REMAINING.get(best_key, 0) if best_key else 0
    results = []
    for slot in SLOTS:
        try:
            results.append(reconcile_slot(slot, workers))
        except Exception as exc:
            log("slot_reconcile_failed", slot=slot, error=type(exc).__name__, detail=str(exc)[:180])
    log(
        "snapshot",
        active_or_pending_slots=active_or_pending_slots,
        allow_live_upgrades=ALLOW_LIVE_UPGRADES_THIS_TICK,
        live_workers=len(live_workers),
        low_live=LOW_LIVE_THIS_TICK,
        low_live_min_live_workers=LOW_LIVE_MIN_LIVE_WORKERS,
        low_live_allocating_retarget_available_seconds=LOW_LIVE_ALLOCATING_RETARGET_AVAILABLE_SECONDS,
        low_live_ignore_pending_capacity=LOW_LIVE_IGNORE_PENDING_CAPACITY,
        live_upgrade_min_live_workers=OPTIMIZE_LIVE_MIN_LIVE_WORKERS,
        live_upgrade_min_profit_delta=OPTIMIZE_LIVE_MIN_PROFIT_DELTA,
        live_upgrade_min_th_delta=OPTIMIZE_LIVE_MIN_TH_DELTA,
        live_upgrade_require_full_slots=OPTIMIZE_LIVE_REQUIRE_FULL_SLOTS,
        live_upgrade_require_reported_available=OPTIMIZE_LIVE_REQUIRE_REPORTED_AVAILABLE,
        total_th=round(sum(float(w.get("reported_hashrate_th") or 0) for w in live_workers), 3),
        results=results,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    load_env()
    if not WALLET or WALLET == "prl1...":
        raise RuntimeError("PRL_WALLET must be set in the environment or .env file")
    load_no_gpu_state()
    log(
        "started",
        org=ORG,
        slots=SLOTS,
        try_seconds=TRY_SECONDS,
        allocating_retarget_available_seconds=ALLOCATING_RETARGET_AVAILABLE_SECONDS,
        low_live_min_live_workers=LOW_LIVE_MIN_LIVE_WORKERS,
        low_live_allocating_retarget_available_seconds=LOW_LIVE_ALLOCATING_RETARGET_AVAILABLE_SECONDS,
        low_live_ignore_pending_capacity=LOW_LIVE_IGNORE_PENDING_CAPACITY,
        allowed_priorities=ALLOWED_PRIORITIES,
        blocked_profiles=sorted(f"{gpu}:{priority}" for gpu, priority in BLOCKED_PROFILES),
        miner_release_tag=MINER_RELEASE_TAG,
        miner_package_version=MINER_PACKAGE_VERSION,
        miner_binary=MINER_BINARY,
        miner_url=MINER_PACKAGE_URL,
    )
    while True:
        try:
            tick()
        except Exception as exc:
            log("tick_failed", error=type(exc).__name__, detail=str(exc)[:180])
        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
