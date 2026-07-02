#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import fleet_scheduler
import state_db
from config_loader import OrgConfig, load_config
from fleet_common import env_bool, env_float, env_int, json_dumps, utc_now
from profit_model import profile_key


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
WATCH_PATH = SCRIPT_DIR / "salad_prl_watch.py"
DEFAULT_BALANCE_FILE = pathlib.Path("state/salad_balances.json")
NO_CREDITS_ERROR_TEXT = "no_credits_available"
LIVE_ACTION_LIMITED_ACTIONS = {
    "create",
    "patch",
    "start",
    "cooldown_pending",
    "restart_no_hash",
}


def parse_slot_filter(values: list[str] | None) -> set[str] | None:
    slots: set[str] = set()
    for value in values or []:
        for item in str(value).split(","):
            slot_name = item.strip()
            if slot_name:
                slots.add(slot_name)
    return slots or None


def load_watch_module(org: OrgConfig, *, decision_price: float, min_profit_day: float) -> Any:
    env = {
        **org.watch_env(),
        "PRL_WATCH_FIXED_DECISION_PRICE_USD": str(decision_price),
        "PRL_WATCH_DECISION_PRICE_CAP_USD": str(decision_price),
        "PRL_WATCH_MIN_PROFIT_USD_DAY": str(min_profit_day),
        "PRL_WATCH_ALLOWED_PRIORITIES": os.environ.get("PRL_WATCH_ALLOWED_PRIORITIES", "batch,low"),
    }
    old_env: dict[str, str | None] = {}
    for key, value in env.items():
        old_env[key] = os.environ.get(key)
        os.environ[key] = value
    name = f"salad_prl_watch_worker_{org.label}"
    spec = importlib.util.spec_from_file_location(name, WATCH_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {WATCH_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def acquire_api_budget(
    *,
    db_path: str | None,
    api_key_env: str,
    max_requests_per_minute: int,
) -> dict[str, Any]:
    total_wait = 0.0
    while True:
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            conn.execute("BEGIN IMMEDIATE")
            wait_seconds = state_db.reserve_api_request(
                conn,
                api_key_env,
                max_requests_per_minute=max_requests_per_minute,
            )
            if wait_seconds <= 0:
                conn.commit()
                return {
                    "api_key_env": api_key_env,
                    "max_requests_per_minute": max_requests_per_minute,
                    "waited_seconds": round(total_wait, 3),
                }
            conn.rollback()
        sleep_for = min(wait_seconds, 5.0)
        time.sleep(sleep_for)
        total_wait += sleep_for


def install_rate_limited_request(watch: Any, org: OrgConfig, *, db_path: str | None = None) -> None:
    max_requests = env_int("PRL_SALAD_API_MAX_REQUESTS_PER_MINUTE", 120)
    if max_requests <= 0 or getattr(watch, "_PRL_RATE_LIMIT_INSTALLED", False):
        return
    original_request = watch.request

    def limited_request(method: str, path: str, payload: Any | None = None, *args: Any, **kwargs: Any) -> Any:
        acquire_api_budget(
            db_path=db_path,
            api_key_env=org.api_key_env,
            max_requests_per_minute=max_requests,
        )
        return original_request(method, path, payload, *args, **kwargs)

    watch.request = limited_request
    watch._PRL_RATE_LIMIT_INSTALLED = True


def target_rows(conn, org_label: str) -> list[dict[str, Any]]:
    nohash_patch_lookback_seconds = max(60, env_int("PRL_RUNNING_NOHASH_PATCH_LOOKBACK_SECONDS", 1800))
    nohash_patch_since = (datetime.now(UTC) - timedelta(seconds=nohash_patch_lookback_seconds)).isoformat(
        timespec="seconds"
    )
    rows = conn.execute(
        """
        WITH live_workers AS (
          SELECT org_label, slot_name,
                 COUNT(*) AS live_worker_count,
                 SUM(reported_hashrate_th) AS live_worker_th
          FROM workers
          WHERE stale = 0 AND reported_hashrate_th > 0
          GROUP BY org_label, slot_name
        ),
        active_guard AS (
          SELECT org_label, slot_name, COUNT(*) AS active_guard_issues
          FROM guard_issues
          GROUP BY org_label, slot_name
        ),
        recent_nohash_patch AS (
          SELECT org_label, slot_name, COUNT(*) AS recent_running_nohash_patch_count
          FROM attempts
          WHERE ok = 1
            AND action = 'patch'
            AND datetime(at_utc) >= datetime(?)
            AND json_extract(payload_json, '$.reason') LIKE 'stale_running_no_hash_profile_mismatch:%'
          GROUP BY org_label, slot_name
        )
        SELECT t.*, p.gpu_key, p.priority, p.memory_mb, p.label,
               s.observed_profile_key AS slot_observed_profile_key,
               s.observed_status AS slot_observed_status,
               s.live_hashrate_th AS slot_live_hashrate_th,
               s.protected AS slot_protected,
               s.observed_status_since_utc, s.observed_profile_since_utc,
               observed_score.expected_profit_day AS observed_profile_expected_profit_day,
               observed_score.risk_tier AS observed_profile_risk_tier,
               COALESCE(lw.live_worker_count, 0) AS live_worker_count,
               COALESCE(lw.live_worker_th, 0) AS live_worker_th,
               COALESCE(ag.active_guard_issues, 0) AS active_guard_issues,
               COALESCE(rnp.recent_running_nohash_patch_count, 0) AS recent_running_nohash_patch_count
        FROM slot_targets t
        JOIN gpu_profiles p ON p.profile_key = t.profile_key
        LEFT JOIN slots s ON s.org_label = t.org_label AND s.slot_name = t.slot_name
        LEFT JOIN profile_scores observed_score
          ON observed_score.profile_key = s.observed_profile_key
         AND observed_score.mode = t.mode
        LEFT JOIN live_workers lw ON lw.org_label = t.org_label AND lw.slot_name = t.slot_name
        LEFT JOIN active_guard ag ON ag.org_label = t.org_label AND ag.slot_name = t.slot_name
        LEFT JOIN recent_nohash_patch rnp ON rnp.org_label = t.org_label AND rnp.slot_name = t.slot_name
        WHERE t.org_label = ?
        ORDER BY t.slot_name
        """,
        (nohash_patch_since, org_label),
    ).fetchall()
    return [dict(row) for row in rows]


def should_skip_live_hashing_target(target: dict[str, Any], *, apply: bool, allow_live_retarget: bool) -> bool:
    if not apply or allow_live_retarget:
        return False
    if int(target.get("active_guard_issues") or 0) > 0:
        return False
    if str(target.get("slot_observed_status") or "") != "running":
        return False
    return float(target.get("live_worker_th") or 0) > 0 and int(target.get("live_worker_count") or 0) > 0


def skipped_live_hashing_result(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot_name": str(target["slot_name"]),
        "action": "skip_live_hashing",
        "reason": "live_hashing_without_guard_issue",
        "target_profile_key": target["profile_key"],
        "current_profile_key": target.get("slot_observed_profile_key"),
        "observed_status": target.get("slot_observed_status") or "running",
        "protected": True,
        "counts": {"running": 1, "creating": 0, "allocating": 0, "stopping": 0},
        "instance_count": int(target.get("live_worker_count") or 0),
        "live_worker_th": float(target.get("live_worker_th") or 0),
        "ok": True,
        "applied": False,
    }


def observe_failed_result(target: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "slot_name": str(target["slot_name"]),
        "action": "observe_failed",
        "reason": "slot_state_or_action_error",
        "target_profile_key": target["profile_key"],
        "current_profile_key": target.get("slot_observed_profile_key"),
        "observed_status": target.get("slot_observed_status") or "unknown",
        "protected": False,
        "counts": {"running": 0, "creating": 0, "allocating": 0, "stopping": 0},
        "instance_count": 0,
        "pending_instance_ids": [],
        "running_instance_ids": [],
        "ok": False,
        "applied": False,
        "error": f"{type(exc).__name__}: {str(exc)[:180]}",
    }


def observed_profile_key_for_result(target: dict[str, Any], result: dict[str, Any], *, apply: bool) -> Any:
    if apply and result.get("applied") and result.get("action") in {"create", "patch", "start"}:
        return str(target["profile_key"])
    return result.get("current_profile_key")


def cooldown_profile_key_for_result(target: dict[str, Any], result: dict[str, Any]) -> str | None:
    action = str(result.get("action") or "")
    if action in {"cooldown_pending", "cooldown_failed_patch", "restart_failed_patch_pending"}:
        return str(target["profile_key"])
    if action in {"patch", "reallocate_pending_after_patch", "restart_empty_pending_after_patch"} and str(
        result.get("reason") or ""
    ).startswith("stale_pending_profile_mismatch:"):
        current = result.get("current_profile_key")
        return str(current) if current else None
    return None


def current_profile_key(watch: Any, group: dict[str, Any] | None) -> str | None:
    if not group:
        return None
    reverse_gpu = {gpu_id: gpu_key for gpu_key, gpu_id in watch.GPU.items()}
    priority = str(group.get("priority") or "").lower()
    resources = ((group.get("container") or {}).get("resources") or {})
    gpu_ids = resources.get("gpu_classes") or []
    if len(gpu_ids) != 1:
        return None
    gpu_key = reverse_gpu.get(str(gpu_ids[0]))
    if not gpu_key or not priority:
        return None
    return profile_key(gpu_key, priority, int(resources.get("memory") or 0))


def active_counts(group: dict[str, Any] | None) -> dict[str, int]:
    counts = ((group or {}).get("current_state") or {}).get("instance_status_counts") or {}
    return {
        "running": int(counts.get("running_count") or 0),
        "creating": int(counts.get("creating_count") or 0),
        "allocating": int(counts.get("allocating_count") or 0),
        "stopping": int(counts.get("stopping_count") or 0),
    }


def pending_instance_ids(instances: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for instance in instances:
        if instance.get("ready") or instance.get("started"):
            continue
        instance_id = str(instance.get("id") or "")
        if not instance_id or instance_id in seen:
            continue
        seen.add(instance_id)
        ids.append(instance_id)
    return ids


def running_instance_ids(instances: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for instance in instances:
        if not (instance.get("ready") or instance.get("started")):
            continue
        instance_id = str(instance.get("id") or "")
        if not instance_id or instance_id in seen:
            continue
        seen.add(instance_id)
        ids.append(instance_id)
    return ids


def observed_status(group: dict[str, Any] | None, counts: dict[str, int]) -> str:
    if group is None:
        return "missing"
    if counts["running"] > 0:
        return "running"
    if counts["creating"] > 0:
        return "creating"
    if counts["allocating"] > 0:
        return "allocating"
    if counts["stopping"] > 0:
        return "stopping"
    status = str(((group or {}).get("current_state") or {}).get("status") or "").lower()
    return status or "stopped"


def age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - at).total_seconds())


def balance_file_path() -> pathlib.Path:
    raw = (
        os.environ.get("PRL_ORG_BALANCE_FILE")
        or os.environ.get("PRL_BALANCE_FILE")
        or os.environ.get("SALAD_BALANCE_FILE")
    )
    return pathlib.Path(raw) if raw else DEFAULT_BALANCE_FILE


def explicit_org_balance(org_label: str, *, path: pathlib.Path | None = None) -> dict[str, Any] | None:
    if not env_bool("PRL_SKIP_ZERO_BALANCE_ORGS", True):
        return None
    selected_path = path or balance_file_path()
    if not selected_path.exists():
        return None
    max_age = env_float("PRL_ZERO_BALANCE_SKIP_MAX_AGE_SECONDS", 1800.0)
    age = max(0.0, time.time() - selected_path.stat().st_mtime)
    if max_age >= 0 and age > max_age:
        return None
    try:
        payload = json.loads(selected_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or org_label not in payload:
        return None
    try:
        balance = float(payload[org_label])
    except (TypeError, ValueError):
        return None
    threshold = env_float("PRL_ZERO_BALANCE_SKIP_THRESHOLD_USD", 0.0)
    return {
        "org_label": org_label,
        "balance_usd": balance,
        "threshold_usd": threshold,
        "balance_file": str(selected_path),
        "balance_age_seconds": round(age, 1),
    }


def explicit_zero_balance_skip(org_label: str, *, path: pathlib.Path | None = None) -> dict[str, Any] | None:
    balance = explicit_org_balance(org_label, path=path)
    if balance is None:
        return None
    if float(balance["balance_usd"]) > float(balance["threshold_usd"]):
        return None
    return balance


def explicit_positive_balance_restore(org_label: str, *, path: pathlib.Path | None = None) -> dict[str, Any] | None:
    balance = explicit_org_balance(org_label, path=path)
    if balance is None:
        return None
    if float(balance["balance_usd"]) <= float(balance["threshold_usd"]):
        return None
    return balance


def zero_balance_skip_result(target: dict[str, Any], skip: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot_name": str(target["slot_name"]),
        "action": "skip_zero_balance",
        "reason": (
            f"explicit_org_balance_{float(skip['balance_usd']):.2f}"
            f"_lte_{float(skip['threshold_usd']):.2f}"
        ),
        "target_profile_key": target["profile_key"],
        "current_profile_key": target.get("slot_observed_profile_key"),
        "observed_status": target.get("slot_observed_status") or "unknown",
        "protected": False,
        "counts": {"running": 0, "creating": 0, "allocating": 0, "stopping": 0},
        "instance_count": 0,
        "pending_instance_ids": [],
        "running_instance_ids": [],
        "ok": True,
        "applied": False,
        "balance_usd": skip["balance_usd"],
        "balance_file": skip["balance_file"],
    }


def replica_quota_status(watch: Any) -> dict[str, Any] | None:
    if not env_bool("PRL_SKIP_ZERO_REPLICA_QUOTA_ORGS", True):
        return None
    try:
        payload = watch.request("GET", f"/organizations/{watch.ORG}/quotas")
    except Exception:
        return None
    quotas = (payload.get("container_groups_quotas") or {}) if isinstance(payload, dict) else {}
    raw_quota = quotas.get("container_replicas_quota")
    if raw_quota is None:
        return None
    try:
        quota = int(raw_quota)
        used = int(quotas.get("container_replicas_used") or 0)
    except (TypeError, ValueError):
        return None
    available = max(0, quota - used)
    status = "available" if quota > 0 else "zero_quota"
    return {
        "quota": quota,
        "used": used,
        "available": available,
        "status": status,
        "reason": "container_replicas_quota_zero" if quota <= 0 else "container_replicas_quota_available",
        "quota_update_time": payload.get("update_time"),
        "quota_create_time": payload.get("create_time"),
    }


def zero_replica_quota_skip_from_status(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if status is None:
        return None
    try:
        quota = int(status.get("quota") or 0)
    except (TypeError, ValueError):
        return None
    if quota > 0:
        return None
    return status


def zero_replica_quota_skip(watch: Any) -> dict[str, Any] | None:
    return zero_replica_quota_skip_from_status(replica_quota_status(watch))


def record_replica_quota_status(
    conn: Any,
    *,
    org_label: str,
    quota_status: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    row = {
        "org_label": org_label,
        "source": source,
        "checked_at_utc": utc_now(),
        **quota_status,
        "payload": {
            "quota": quota_status.get("quota"),
            "used": quota_status.get("used"),
            "available": quota_status.get("available"),
            "status": quota_status.get("status"),
            "reason": quota_status.get("reason"),
            "quota_update_time": quota_status.get("quota_update_time"),
            "quota_create_time": quota_status.get("quota_create_time"),
        },
    }
    previous = state_db.upsert_org_replica_quota(conn, row)
    previous_quota = int(previous.get("quota") or 0) if previous is not None else None
    current_quota = int(row.get("quota") or 0)
    restored = previous_quota is not None and previous_quota <= 0 < current_quota
    blocked = previous_quota is not None and previous_quota > 0 and current_quota <= 0
    transition = {
        "previous": previous,
        "current": row,
        "restored": restored,
        "blocked": blocked,
    }
    if previous is None:
        return transition
    if restored:
        state_db.record_event(
            conn,
            "org_replica_quota_restored",
            source=source,
            message="Salad replica quota became available for an organization",
            payload=row,
        )
    elif blocked:
        state_db.record_event(
            conn,
            "org_replica_quota_blocked",
            source=source,
            message="Salad replica quota dropped to zero for an organization",
            payload=row,
        )
    return transition


def zero_replica_quota_skip_result(target: dict[str, Any], skip: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot_name": str(target["slot_name"]),
        "action": "skip_zero_replica_quota",
        "reason": str(skip.get("reason") or "container_replicas_quota_zero"),
        "target_profile_key": target["profile_key"],
        "current_profile_key": target.get("slot_observed_profile_key"),
        "observed_status": target.get("slot_observed_status") or "zero_quota",
        "protected": False,
        "counts": {"running": 0, "creating": 0, "allocating": 0, "stopping": 0},
        "instance_count": 0,
        "pending_instance_ids": [],
        "running_instance_ids": [],
        "ok": True,
        "applied": False,
        "replica_quota": skip.get("quota"),
        "replica_quota_used": skip.get("used"),
        "quota_update_time": skip.get("quota_update_time"),
    }


def no_credits_cooldown_seconds() -> int:
    return env_int(
        "PRL_NO_CREDITS_ORG_COOLDOWN_SECONDS",
        env_int("KRAY2_PRL_NO_CREDITS_BACKOFF_SECONDS", 120),
    )


def is_no_credits_error(error: Any) -> bool:
    return NO_CREDITS_ERROR_TEXT in str(error or "").lower() or "no credits" in str(error or "").lower()


def no_credits_skip_result(target: dict[str, Any], skip: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot_name": str(target["slot_name"]),
        "action": "skip_no_credits",
        "reason": str(skip.get("reason") or NO_CREDITS_ERROR_TEXT),
        "target_profile_key": target["profile_key"],
        "current_profile_key": target.get("slot_observed_profile_key"),
        "observed_status": target.get("slot_observed_status") or "unknown",
        "protected": False,
        "counts": {"running": 0, "creating": 0, "allocating": 0, "stopping": 0},
        "instance_count": 0,
        "pending_instance_ids": [],
        "running_instance_ids": [],
        "ok": True,
        "applied": False,
        "sleep_until_utc": skip.get("sleep_until_utc"),
    }


def no_credits_cooldown_row(org_label: str, *, error: str | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    seconds = max(30, no_credits_cooldown_seconds())
    return {
        "org_label": org_label,
        "slot_name": "*",
        "profile_key": "*",
        "no_gpu_since_utc": now.isoformat(timespec="seconds"),
        "sleep_until_utc": (now + timedelta(seconds=seconds)).isoformat(timespec="seconds"),
        "attempts": 1,
        "reason": error or NO_CREDITS_ERROR_TEXT,
        "updated_at_utc": now.isoformat(timespec="seconds"),
    }


def clear_no_credits_cooldown_row(
    org_label: str,
    *,
    cooldown: dict[str, Any],
    balance_restore: dict[str, Any],
) -> dict[str, Any]:
    return {
        "org_label": org_label,
        "slot_name": "*",
        "profile_key": "*",
        "no_gpu_since_utc": cooldown.get("no_gpu_since_utc"),
        "sleep_until_utc": None,
        "attempts": int(cooldown.get("attempts") or 0),
        "reason": "positive_balance_restored",
        "updated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "balance_restore": balance_restore,
    }


def pending_profile_age_seconds(target: dict[str, Any]) -> float | None:
    return age_seconds(
        target.get("observed_profile_since_utc")
        or target.get("observed_status_since_utc")
    )


def resolve_pending_status_retarget_after_seconds(
    pending_retarget_after_seconds: int,
    pending_status_retarget_after_seconds: int | None = None,
) -> int:
    no_hash_seconds = max(0, int(pending_retarget_after_seconds))
    if pending_status_retarget_after_seconds is None:
        return max(no_hash_seconds, 120)
    return max(0, int(pending_status_retarget_after_seconds))


def planned_action(
    watch: Any,
    slot_name: str,
    target: dict[str, Any],
    *,
    protect_running: bool = True,
    protect_pending: bool = True,
    pending_retarget_after_seconds: int = 900,
    pending_status_retarget_after_seconds: int | None = None,
    allow_running_nohash_retarget: bool | None = None,
) -> dict[str, Any]:
    pending_retarget_after_seconds = max(0, int(pending_retarget_after_seconds))
    pending_status_retarget_after_seconds = resolve_pending_status_retarget_after_seconds(
        pending_retarget_after_seconds,
        pending_status_retarget_after_seconds,
    )
    if allow_running_nohash_retarget is None:
        allow_running_nohash_retarget = env_bool("PRL_ALLOW_RUNNING_NOHASH_RETARGET", False)
    try:
        group, instances = watch.slot_state(slot_name)
    except KeyError:
        group, instances = None, []
    current = current_profile_key(watch, group)
    counts = active_counts(group)
    status = observed_status(group, counts)
    pending_active = counts["creating"] + counts["allocating"] > 0 or status == "deploying"
    live_hashing = int(target.get("live_worker_count") or 0) > 0 and float(target.get("live_worker_th") or 0) > 0
    if group is None:
        action = "create"
        reason = "missing_container_group"
    elif current != target["profile_key"]:
        if protect_running and counts["running"] > 0:
            if live_hashing:
                action = "observe"
                reason = f"protected_running_profile_mismatch:{current or 'unknown'}"
            else:
                running_age = pending_profile_age_seconds(target)
                if running_age is None or running_age < pending_retarget_after_seconds:
                    action = "observe"
                    age_text = "unknown" if running_age is None else f"{running_age:.1f}"
                    reason = (
                        f"running_no_hash_profile_mismatch_wait:{current or 'unknown'}:"
                        f"age_{age_text}_lt_{pending_retarget_after_seconds}"
                    )
                elif allow_running_nohash_retarget:
                    recent_nohash_patches = int(target.get("recent_running_nohash_patch_count") or 0)
                    restart_after_patches = env_int("PRL_RUNNING_NOHASH_MISMATCH_RESTART_AFTER_PATCHES", 3)
                    if restart_after_patches > 0 and recent_nohash_patches >= restart_after_patches:
                        action = "restart_no_hash"
                        reason = (
                            f"stale_running_no_hash_profile_mismatch_restart_after_patches:"
                            f"{current or 'unknown'}:age_{running_age:.1f}:patches_{recent_nohash_patches}"
                        )
                    else:
                        action = "patch"
                        reason = f"stale_running_no_hash_profile_mismatch:{current or 'unknown'}:age_{running_age:.1f}"
                else:
                    action = "observe"
                    reason = (
                        f"running_no_hash_profile_mismatch_protected:{current or 'unknown'}:"
                        f"age_{running_age:.1f}:retarget_disabled"
                    )
        elif pending_active:
            pending_age = pending_profile_age_seconds(target)
            if protect_pending:
                action = "observe"
                reason = f"protected_pending_profile_mismatch:{current or 'unknown'}"
            elif pending_age is None or pending_age < pending_status_retarget_after_seconds:
                action = "observe"
                age_text = "unknown" if pending_age is None else f"{pending_age:.1f}"
                reason = (
                    f"pending_profile_mismatch_wait:{current or 'unknown'}:"
                    f"age_{age_text}_lt_{pending_status_retarget_after_seconds}"
                )
            else:
                action = "patch"
                reason = f"stale_pending_profile_mismatch:{current or 'unknown'}:age_{pending_age:.1f}"
        else:
            action = "patch"
            reason = f"profile_mismatch:{current or 'unknown'}"
    elif counts["running"] <= 0 and not pending_active:
        action = "start"
        reason = "target_stopped_or_empty"
    elif pending_active:
        pending_age = pending_profile_age_seconds(target)
        if pending_age is not None and pending_age >= pending_status_retarget_after_seconds:
            action = "cooldown_pending"
            reason = f"stale_pending_same_profile:{current or 'unknown'}:age_{pending_age:.1f}"
        else:
            action = "observe"
            age_text = "unknown" if pending_age is None else f"{pending_age:.1f}"
            reason = f"target_pending_wait:age_{age_text}_lt_{pending_status_retarget_after_seconds}"
    elif counts["running"] > 0 and not live_hashing:
        running_age = pending_profile_age_seconds(target)
        if running_age is None or running_age < pending_retarget_after_seconds:
            action = "observe"
            age_text = "unknown" if running_age is None else f"{running_age:.1f}"
            reason = (
                f"running_no_hash_same_profile_wait:{current or 'unknown'}:"
                f"age_{age_text}_lt_{pending_retarget_after_seconds}"
            )
        elif allow_running_nohash_retarget:
            action = "restart_no_hash"
            reason = f"stale_running_no_hash_same_profile:{current or 'unknown'}:age_{running_age:.1f}"
        else:
            action = "observe"
            reason = (
                f"running_no_hash_same_profile_protected:{current or 'unknown'}:"
                f"age_{running_age:.1f}:restart_disabled"
            )
    else:
        action = "observe"
        reason = "target_already_active_or_pending"
    return {
        "slot_name": slot_name,
        "action": action,
        "reason": reason,
        "target_profile_key": target["profile_key"],
        "current_profile_key": current,
        "current_expected_profit_day": target.get("observed_profile_expected_profit_day"),
        "current_risk_tier": target.get("observed_profile_risk_tier"),
        "observed_status": status,
        "protected": counts["running"] > 0 and live_hashing,
        "counts": counts,
        "instance_count": len(instances),
        "pending_instance_ids": pending_instance_ids(instances),
        "running_instance_ids": running_instance_ids(instances),
    }


def candidate_from_target(watch: Any, target: dict[str, Any]) -> Any:
    return watch.Candidate(
        str(target["label"]),
        str(target["priority"]),
        (str(target["gpu_key"]),),
        int(target["memory_mb"]),
    )


def has_active_instances(plan: dict[str, Any]) -> bool:
    counts = plan.get("counts") or {}
    return any(int(counts.get(key) or 0) > 0 for key in ("allocating", "creating", "running", "stopping"))


def is_pending_patch_plan(plan: dict[str, Any]) -> bool:
    counts = plan.get("counts") or {}
    status = str(plan.get("observed_status") or "").lower()
    return (
        int(counts.get("allocating") or 0) > 0
        or int(counts.get("creating") or 0) > 0
        or status == "deploying"
    )


def should_restart_empty_pending_after_patch(plan: dict[str, Any]) -> bool:
    if not env_bool("PRL_STALE_EMPTY_PENDING_PATCH_RESTART", False):
        return False
    if not is_pending_patch_plan(plan):
        return False
    if plan.get("pending_instance_ids") or plan.get("running_instance_ids"):
        return False
    counts = plan.get("counts") or {}
    if int(counts.get("creating") or 0) > 0:
        return False
    return int(counts.get("allocating") or 0) > 0 or str(plan.get("observed_status") or "").lower() == "deploying"


def should_reallocate_pending_after_patch(plan: dict[str, Any]) -> bool:
    if not env_bool("PRL_STALE_PENDING_PATCH_REALLOCATE", False):
        return False
    if not is_pending_patch_plan(plan):
        return False
    return bool(plan.get("pending_instance_ids"))


def start_error_for_result(watch: Any, slot_name: str) -> str:
    getter = getattr(watch, "start_slot_error", None)
    if callable(getter):
        try:
            error = getter(slot_name)
        except Exception:
            error = None
        if error:
            return str(error)[:180]
    errors = getattr(watch, "START_SLOT_ERRORS", None)
    if isinstance(errors, dict) and errors.get(slot_name):
        return str(errors[slot_name])[:180]
    return "start_slot returned false"


def start_failed_result(watch: Any, slot_name: str, plan: dict[str, Any], original_action: str) -> dict[str, Any]:
    return {
        "ok": False,
        "applied": False,
        **plan,
        "action": "start_failed",
        "original_action": original_action,
        "error": start_error_for_result(watch, slot_name),
    }


def failed_action_result(
    watch: Any,
    slot_name: str,
    plan: dict[str, Any],
    original_action: str,
    exc: Exception,
) -> dict[str, Any]:
    fallback = f"{type(exc).__name__}: {str(exc)[:180]}"
    error = start_error_for_result(watch, slot_name)
    if error == "start_slot returned false":
        error = fallback
    return {
        "ok": False,
        "applied": False,
        **plan,
        "action": f"{original_action}_failed",
        "original_action": original_action,
        "error": error,
    }


def stopped_patch_failed_start_existing_allowed(plan: dict[str, Any]) -> bool:
    if not env_bool("PRL_STOPPED_PATCH_FAIL_START_EXISTING", True):
        return False
    if has_active_instances(plan):
        return False
    if str(plan.get("observed_status") or "").lower() != "stopped":
        return False
    current_profile = str(plan.get("current_profile_key") or "")
    if not current_profile:
        return False
    risk_tier = str(plan.get("current_risk_tier") or "")
    if risk_tier in {"negative", "marginal", "blocked_priority", "unstable_recent_spikes"}:
        return False
    try:
        expected_profit = float(plan.get("current_expected_profit_day"))
    except (TypeError, ValueError):
        return False
    min_profit = env_float("PRL_STOPPED_EXISTING_MIN_PROFIT_USD_DAY", 0.05)
    return expected_profit >= min_profit


def execute_action(watch: Any, target: dict[str, Any], plan: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    if not apply or plan["action"] == "observe":
        return {"ok": True, "applied": False, **plan}
    candidate = candidate_from_target(watch, target)
    slot_name = str(target["slot_name"])
    if plan["action"] == "create":
        try:
            watch.create_slot(slot_name, candidate)
        except Exception as exc:
            return failed_action_result(watch, slot_name, plan, "create", exc)
    elif plan["action"] == "patch":
        start_after_patch = not has_active_instances(plan)
        ok = watch.patch_slot(
            slot_name,
            candidate,
            "fleet_scheduler_target",
            start_after=not start_after_patch,
        )
        if not ok:
            if stopped_patch_failed_start_existing_allowed(plan):
                start_result = watch.start_slot(slot_name, "after_failed_patch:stopped_existing_profitable")
                if start_result is False:
                    result = start_failed_result(watch, slot_name, plan, "patch")
                    result.update(
                        {
                            "patch_failed": True,
                            "existing_profile_fallback": True,
                        }
                    )
                    return result
                return {
                    "ok": True,
                    "applied": True,
                    **plan,
                    "action": "start_existing_after_patch_failed",
                    "original_action": "patch",
                    "patch_failed": True,
                    "existing_profile_fallback": True,
                }
            if env_bool("PRL_PENDING_PATCH_FAIL_RESTART", False) and is_pending_patch_plan(plan):
                restart_reason = "patch_failed_pending"
                watch.request("POST", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers/{slot_name}/stop")
                start_result = watch.start_slot(slot_name, f"patch_failed:{restart_reason}")
                if start_result is False:
                    result = start_failed_result(watch, slot_name, plan, "patch_failed_pending")
                    result.update(
                        {
                            "patch_failed": True,
                            "restart_requested": True,
                            "restart_reason": restart_reason,
                        }
                    )
                    return result
                return {
                    "ok": True,
                    "applied": True,
                    **plan,
                    "action": "restart_failed_patch_pending",
                    "original_action": "patch",
                    "patch_failed": True,
                    "restart_requested": True,
                    "restart_reason": restart_reason,
                }
            return {
                "ok": True,
                "applied": False,
                **plan,
                "action": "cooldown_failed_patch",
                "original_action": "patch",
                "error": "patch_slot returned false",
            }
        if should_restart_empty_pending_after_patch(plan):
            restart_reason = "empty_pending_after_patch"
            watch.request("POST", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers/{slot_name}/stop")
            start_result = watch.start_slot(slot_name, f"stale_pending_patch:{restart_reason}")
            if start_result is False:
                result = start_failed_result(watch, slot_name, plan, "patch")
                result.update(
                    {
                        "patched": True,
                        "restart_requested": True,
                        "restart_reason": restart_reason,
                    }
                )
                return result
            return {
                "ok": True,
                "applied": True,
                **plan,
                "action": "restart_empty_pending_after_patch",
                "original_action": "patch",
                "patched": True,
                "restart_requested": True,
                "restart_reason": restart_reason,
            }
        if should_reallocate_pending_after_patch(plan):
            reallocated = []
            for instance_id in plan.get("pending_instance_ids") or []:
                watch.reallocate(slot_name, str(instance_id), "stale_pending_patch")
                reallocated.append(str(instance_id))
            return {
                "ok": True,
                "applied": True,
                **plan,
                "action": "reallocate_pending_after_patch",
                "original_action": "patch",
                "patched": True,
                "reallocated_pending_instances": reallocated,
            }
        if start_after_patch:
            start_result = watch.start_slot(slot_name, "after_patch:fleet_scheduler_target")
            if start_result is False:
                result = start_failed_result(watch, slot_name, plan, "patch")
                result["patched"] = True
                return result
            return {"ok": True, "applied": True, **plan, "start_requested_after_patch": True}
    elif plan["action"] == "start":
        start_result = watch.start_slot(slot_name, "fleet_scheduler_target")
        if start_result is False:
            return start_failed_result(watch, slot_name, plan, "start")
    elif plan["action"] == "cooldown_pending":
        recycled = []
        for instance_id in plan.get("pending_instance_ids") or []:
            watch.reallocate(slot_name, str(instance_id), "stale_pending_same_profile")
            recycled.append(str(instance_id))
        restart_requested = False
        restart_reason = None
        if not recycled:
            restart_reason = "stale_pending_without_visible_instances"
            watch.request("POST", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers/{slot_name}/stop")
            start_result = watch.start_slot(slot_name, f"stale_pending_same_profile:{restart_reason}")
            restart_requested = True
            if start_result is False:
                result = start_failed_result(watch, slot_name, plan, "cooldown_pending")
                result.update(
                    {
                        "recycled_pending_instances": recycled,
                        "restart_requested": restart_requested,
                        "restart_reason": restart_reason,
                    }
                )
                return result
        return {
            "ok": True,
            "applied": True,
            **plan,
            "recycled_pending_instances": recycled,
            "restart_requested": restart_requested,
            "restart_reason": restart_reason,
        }
    elif plan["action"] == "restart_no_hash":
        reallocated = []
        for instance_id in plan.get("running_instance_ids") or []:
            watch.reallocate(slot_name, str(instance_id), "running_no_hash_same_profile")
            reallocated.append(str(instance_id))
        restart_requested = False
        restart_reason = None
        if not reallocated:
            restart_reason = "running_no_hash_without_visible_instances"
            watch.request("POST", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers/{slot_name}/stop")
            start_result = watch.start_slot(slot_name, f"running_no_hash_same_profile:{restart_reason}")
            restart_requested = True
            if start_result is False:
                result = start_failed_result(watch, slot_name, plan, "restart_no_hash")
                result.update(
                    {
                        "reallocated_instances": reallocated,
                        "restart_requested": restart_requested,
                        "restart_reason": restart_reason,
                    }
                )
                return result
        return {
            "ok": True,
            "applied": True,
            **plan,
            "reallocated_instances": reallocated,
            "restart_requested": restart_requested,
            "restart_reason": restart_reason,
        }
    else:
        raise RuntimeError(f"unknown action {plan['action']}")
    return {"ok": True, "applied": True, **plan}


def is_live_limited_plan(plan: dict[str, Any]) -> bool:
    return str(plan.get("action") or "") in LIVE_ACTION_LIMITED_ACTIONS


def live_action_limit_result(target: dict[str, Any], plan: dict[str, Any], max_live_actions: int) -> dict[str, Any]:
    return {
        "ok": True,
        "applied": False,
        **plan,
        "action": "defer_live_action_limit",
        "original_action": plan.get("action"),
        "reason": f"live_action_limit:{plan.get('action')}:max_{max_live_actions}",
        "target_profile_key": target["profile_key"],
        "current_profile_key": plan.get("current_profile_key"),
        "live_action_limit": max_live_actions,
    }


def run_once(
    *,
    org_label: str,
    db_path: str | None = None,
    apply: bool = False,
    schedule_if_empty: bool = True,
    allow_live_retarget: bool = False,
    allow_pending_retarget: bool = False,
    pending_retarget_after_seconds: int = 900,
    pending_status_retarget_after_seconds: int | None = None,
    allow_running_nohash_retarget: bool | None = None,
    heartbeat_stale_after_seconds: int | None = None,
    slot_filter: set[str] | None = None,
    max_live_actions: int = 0,
) -> dict[str, Any]:
    config = load_config()
    orgs = {org.label: org for org in config.enabled_orgs()}
    if org_label not in orgs:
        raise SystemExit(f"unknown or disabled org: {org_label}")
    org = orgs[org_label]

    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.sync_config(conn, config)
        if schedule_if_empty and not target_rows(conn, org_label):
            conn.commit()
            fleet_scheduler.schedule_once(db_path=db_path, dry_run=False)
        risk = state_db.latest_risk_mode(conn)
        decision_price = float(risk["decision_price_usd"]) if risk else config.risk.decision_price_for_mode()
        min_profit = config.risk.min_profit_for_mode()
        targets = target_rows(conn, org_label)
        if slot_filter:
            targets = [target for target in targets if str(target["slot_name"]) in slot_filter]
        conn.commit()

    pending_retarget_after_seconds = max(0, int(pending_retarget_after_seconds))
    if pending_status_retarget_after_seconds is None:
        pending_status_retarget_after_seconds = env_int(
            "PRL_PENDING_STATUS_RETARGET_AFTER_SECONDS",
            max(pending_retarget_after_seconds, 120),
        )
    pending_status_retarget_after_seconds = resolve_pending_status_retarget_after_seconds(
        pending_retarget_after_seconds,
        pending_status_retarget_after_seconds,
    )
    if allow_running_nohash_retarget is None:
        allow_running_nohash_retarget = env_bool("PRL_ALLOW_RUNNING_NOHASH_RETARGET", False)
    if heartbeat_stale_after_seconds is None:
        heartbeat_stale_after_seconds = env_int("PRL_ORG_WORKER_STALE_AFTER_SECONDS", 300)
    max_live_actions = max(0, int(max_live_actions))

    zero_balance_skip = explicit_zero_balance_skip(org_label) if apply else None
    if zero_balance_skip is not None:
        results = [zero_balance_skip_result(target, zero_balance_skip) for target in targets]
        action_counts = {"skip_zero_balance": len(results)}
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            for target, result in zip(targets, results, strict=False):
                state_db.record_attempt(
                    conn,
                    {
                        "at_utc": utc_now(),
                        "org_label": org_label,
                        "slot_name": str(target["slot_name"]),
                        "action": "skip_zero_balance",
                        "profile_key": str(target["profile_key"]),
                        "ok": True,
                        "duration_ms": 0,
                        "error": None,
                        "payload": result,
                    },
                )
                state_db.update_slot_observation(
                    conn,
                    {
                        "org_label": org_label,
                        "slot_name": str(target["slot_name"]),
                        "observed_profile_key": result.get("current_profile_key") or str(target["profile_key"]),
                        "observed_status": "zero_balance",
                        "live_hashrate_th": 0,
                        "protected": False,
                        "reset_observed_age": True,
                    },
                )
            state_db.write_heartbeat(
                conn,
                f"org_worker:{org_label}",
                stale_after_seconds=heartbeat_stale_after_seconds,
                payload={
                    "apply": apply,
                    "allow_live_retarget": allow_live_retarget,
                    "allow_pending_retarget": allow_pending_retarget,
                    "pending_retarget_after_seconds": pending_retarget_after_seconds,
                    "pending_status_retarget_after_seconds": pending_status_retarget_after_seconds,
                    "targets": len(targets),
                    "actions": action_counts,
                    "zero_balance_skip": zero_balance_skip,
                },
            )
            state_db.record_event(
                conn,
                "org_worker_zero_balance_skip",
                source=f"org_worker:{org_label}",
                message="org worker skipped live actions for explicit zero balance",
                payload={
                    "apply": apply,
                    "targets": len(targets),
                    "actions": action_counts,
                    "zero_balance_skip": zero_balance_skip,
                },
            )
            conn.commit()
        return {
            "org": org_label,
            "apply": apply,
            "allow_live_retarget": allow_live_retarget,
            "allow_pending_retarget": allow_pending_retarget,
            "pending_retarget_after_seconds": pending_retarget_after_seconds,
            "pending_status_retarget_after_seconds": pending_status_retarget_after_seconds,
            "targets": len(targets),
            "action_counts": action_counts,
            "zero_balance_skip": zero_balance_skip,
            "results": results,
        }

    no_credits_skip: dict[str, Any] | None = None
    no_credits_cooldown_cleared: dict[str, Any] | None = None
    if apply:
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            no_credits_skip = state_db.active_org_cooldown(conn, org_label)
            if no_credits_skip is not None:
                positive_balance = explicit_positive_balance_restore(org_label)
                if positive_balance is not None:
                    clear_row = clear_no_credits_cooldown_row(
                        org_label,
                        cooldown=no_credits_skip,
                        balance_restore=positive_balance,
                    )
                    state_db.record_search_state(conn, clear_row)
                    state_db.record_event(
                        conn,
                        "org_worker_no_credits_cooldown_cleared",
                        source=f"org_worker:{org_label}",
                        message="org worker cleared no-credits cooldown after fresh positive balance",
                        payload={
                            "cooldown": no_credits_skip,
                            "balance_restore": positive_balance,
                        },
                    )
                    no_credits_cooldown_cleared = {
                        "cooldown": no_credits_skip,
                        "balance_restore": positive_balance,
                    }
                    no_credits_skip = None
            conn.commit()

    if no_credits_skip is not None:
        results = [no_credits_skip_result(target, no_credits_skip) for target in targets]
        action_counts = {"skip_no_credits": len(results)}
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            for target, result in zip(targets, results, strict=False):
                state_db.record_attempt(
                    conn,
                    {
                        "at_utc": utc_now(),
                        "org_label": org_label,
                        "slot_name": str(target["slot_name"]),
                        "action": "skip_no_credits",
                        "profile_key": str(target["profile_key"]),
                        "ok": True,
                        "duration_ms": 0,
                        "error": None,
                        "payload": result,
                    },
                )
                state_db.update_slot_observation(
                    conn,
                    {
                        "org_label": org_label,
                        "slot_name": str(target["slot_name"]),
                        "observed_profile_key": result.get("current_profile_key") or str(target["profile_key"]),
                        "observed_status": "zero_balance",
                        "live_hashrate_th": 0,
                        "protected": False,
                        "reset_observed_age": True,
                    },
                )
            state_db.write_heartbeat(
                conn,
                f"org_worker:{org_label}",
                stale_after_seconds=heartbeat_stale_after_seconds,
                payload={
                    "apply": apply,
                    "allow_live_retarget": allow_live_retarget,
                    "allow_pending_retarget": allow_pending_retarget,
                    "pending_retarget_after_seconds": pending_retarget_after_seconds,
                    "pending_status_retarget_after_seconds": pending_status_retarget_after_seconds,
                    "targets": len(targets),
                    "actions": action_counts,
                    "no_credits_skip": no_credits_skip,
                },
            )
            state_db.record_event(
                conn,
                "org_worker_no_credits_skip",
                source=f"org_worker:{org_label}",
                message="org worker skipped live actions for active no-credits cooldown",
                payload={
                    "apply": apply,
                    "targets": len(targets),
                    "actions": action_counts,
                    "no_credits_skip": no_credits_skip,
                },
            )
            conn.commit()
        return {
            "org": org_label,
            "apply": apply,
            "allow_live_retarget": allow_live_retarget,
            "allow_pending_retarget": allow_pending_retarget,
            "pending_retarget_after_seconds": pending_retarget_after_seconds,
            "pending_status_retarget_after_seconds": pending_status_retarget_after_seconds,
            "targets": len(targets),
            "action_counts": action_counts,
            "no_credits_skip": no_credits_skip,
            "results": results,
        }

    watch = load_watch_module(org, decision_price=decision_price, min_profit_day=min_profit)
    install_rate_limited_request(watch, org, db_path=db_path)
    replica_quota = replica_quota_status(watch) if apply else None
    if replica_quota is not None:
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            record_replica_quota_status(
                conn,
                org_label=org_label,
                quota_status=replica_quota,
                source=f"org_worker:{org_label}",
            )
            conn.commit()
    zero_quota_skip = zero_replica_quota_skip_from_status(replica_quota)
    if zero_quota_skip is not None:
        results = [
            (
                skipped_live_hashing_result(target)
                if should_skip_live_hashing_target(target, apply=apply, allow_live_retarget=allow_live_retarget)
                else zero_replica_quota_skip_result(target, zero_quota_skip)
            )
            for target in targets
        ]
        action_counts: dict[str, int] = {}
        for result in results:
            action_counts[str(result["action"])] = action_counts.get(str(result["action"]), 0) + 1
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            for target, result in zip(targets, results, strict=False):
                state_db.record_attempt(
                    conn,
                    {
                        "at_utc": utc_now(),
                        "org_label": org_label,
                        "slot_name": str(target["slot_name"]),
                        "action": str(result["action"]),
                        "profile_key": str(target["profile_key"]),
                        "ok": True,
                        "duration_ms": 0,
                        "error": None,
                        "payload": result,
                    },
                )
                if str(result["action"]) == "skip_live_hashing":
                    observed_status_value = result.get("observed_status") or target.get("slot_observed_status")
                    live_hashrate_th = float(target.get("live_worker_th") or target.get("slot_live_hashrate_th") or 0)
                    protected = True
                else:
                    observed_status_value = "zero_quota"
                    live_hashrate_th = 0
                    protected = False
                state_db.update_slot_observation(
                    conn,
                    {
                        "org_label": org_label,
                        "slot_name": str(target["slot_name"]),
                        "observed_profile_key": result.get("current_profile_key") or str(target["profile_key"]),
                        "observed_status": observed_status_value,
                        "live_hashrate_th": live_hashrate_th,
                        "protected": protected,
                        "reset_observed_age": True,
                    },
                )
            state_db.write_heartbeat(
                conn,
                f"org_worker:{org_label}",
                stale_after_seconds=heartbeat_stale_after_seconds,
                payload={
                    "apply": apply,
                    "allow_live_retarget": allow_live_retarget,
                    "allow_pending_retarget": allow_pending_retarget,
                    "pending_retarget_after_seconds": pending_retarget_after_seconds,
                    "pending_status_retarget_after_seconds": pending_status_retarget_after_seconds,
                    "targets": len(targets),
                    "actions": action_counts,
                    "zero_replica_quota_skip": zero_quota_skip,
                },
            )
            state_db.record_event(
                conn,
                "org_worker_zero_replica_quota_skip",
                source=f"org_worker:{org_label}",
                message="org worker skipped live actions for zero Salad replica quota",
                payload={
                    "apply": apply,
                    "targets": len(targets),
                    "actions": action_counts,
                    "zero_replica_quota_skip": zero_quota_skip,
                },
            )
            conn.commit()
        return {
            "org": org_label,
            "apply": apply,
            "allow_live_retarget": allow_live_retarget,
            "allow_pending_retarget": allow_pending_retarget,
            "pending_retarget_after_seconds": pending_retarget_after_seconds,
            "pending_status_retarget_after_seconds": pending_status_retarget_after_seconds,
            "targets": len(targets),
            "action_counts": action_counts,
            "zero_replica_quota_skip": zero_quota_skip,
            "results": results,
        }

    results: list[dict[str, Any]] = []
    attempt_rows: list[dict[str, Any]] = []
    observation_rows: list[dict[str, Any]] = []
    cooldown_rows: list[dict[str, Any]] = []
    active_no_credits_skip: dict[str, Any] | None = None
    pending_profile_cooldown_seconds = env_int("PRL_PENDING_PROFILE_COOLDOWN_SECONDS", 600)
    live_action_attempts = 0
    for target in targets:
        started = time.monotonic()
        if apply and active_no_credits_skip is not None:
            result = no_credits_skip_result(target, active_no_credits_skip)
            ok = True
            error = None
        elif should_skip_live_hashing_target(target, apply=apply, allow_live_retarget=allow_live_retarget):
            result = skipped_live_hashing_result(target)
            ok = True
            error = None
        else:
            plan = None
            try:
                plan = planned_action(
                    watch,
                    str(target["slot_name"]),
                    target,
                    protect_running=not allow_live_retarget,
                    protect_pending=not allow_pending_retarget,
                    pending_retarget_after_seconds=pending_retarget_after_seconds,
                    pending_status_retarget_after_seconds=pending_status_retarget_after_seconds,
                    allow_running_nohash_retarget=allow_running_nohash_retarget,
                )
                if apply and max_live_actions > 0 and is_live_limited_plan(plan):
                    if live_action_attempts >= max_live_actions:
                        result = live_action_limit_result(target, plan, max_live_actions)
                    else:
                        live_action_attempts += 1
                        result = execute_action(watch, target, plan, apply=apply)
                else:
                    result = execute_action(watch, target, plan, apply=apply)
                ok = bool(result.get("ok", True))
                error = None if ok else str(result.get("error") or "action failed")[:180]
            except Exception as exc:
                if plan is None:
                    result = observe_failed_result(target, exc)
                else:
                    result = {"ok": False, "applied": False, **plan, "error": type(exc).__name__}
                ok = False
                error = f"{type(exc).__name__}: {str(exc)[:180]}"
            if apply and not ok and is_no_credits_error(error):
                active_no_credits_skip = no_credits_cooldown_row(org_label, error=error)
                cooldown_rows.append(active_no_credits_skip)
        cooldown_profile_key = cooldown_profile_key_for_result(target, result)
        if apply and ok and cooldown_profile_key:
            now = datetime.now(UTC)
            cooldown_rows.append(
                {
                    "org_label": org_label,
                    "slot_name": str(target["slot_name"]),
                    "profile_key": cooldown_profile_key,
                    "no_gpu_since_utc": (
                        target.get("observed_profile_since_utc")
                        or target.get("observed_status_since_utc")
                        or utc_now()
                    ),
                    "sleep_until_utc": (now + timedelta(seconds=max(60, pending_profile_cooldown_seconds))).isoformat(
                        timespec="seconds"
                    ),
                    "attempts": 1,
                    "reason": result.get("reason") or result.get("error") or "stale_pending_same_profile",
                    "updated_at_utc": now.isoformat(timespec="seconds"),
                }
            )
        attempt_rows.append(
            {
                "at_utc": utc_now(),
                "org_label": org_label,
                "slot_name": str(target["slot_name"]),
                "action": result["action"] if apply else f"dry_run_{result['action']}",
                "profile_key": str(target["profile_key"]),
                "ok": ok,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error": error,
                "payload": result,
            }
        )
        result_action = str(result.get("action") or "")
        if result_action != "observe_failed":
            observation = {
                "org_label": org_label,
                "slot_name": str(target["slot_name"]),
                "observed_profile_key": observed_profile_key_for_result(target, result, apply=apply),
                "observed_status": "zero_balance" if result_action == "skip_no_credits" else result.get("observed_status"),
                "protected": False if result_action == "skip_no_credits" else bool(result.get("protected")),
                "reset_observed_age": bool(
                    apply
                    and (
                        result_action == "skip_no_credits"
                        or (
                            result.get("applied")
                            and result_action
                            in {
                                "cooldown_pending",
                                "reallocate_pending_after_patch",
                                "restart_failed_patch_pending",
                                "restart_empty_pending_after_patch",
                                "restart_no_hash",
                            }
                        )
                    )
                ),
            }
            if result_action == "skip_no_credits":
                observation["live_hashrate_th"] = 0
            observation_rows.append(observation)
        results.append(result)
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        for attempt in attempt_rows:
            state_db.record_attempt(conn, attempt)
        for observation in observation_rows:
            state_db.update_slot_observation(conn, observation)
        for cooldown in cooldown_rows:
            state_db.record_search_state(conn, cooldown)
        action_counts: dict[str, int] = {}
        for result in results:
            action_counts[str(result["action"])] = action_counts.get(str(result["action"]), 0) + 1
        state_db.write_heartbeat(
            conn,
            f"org_worker:{org_label}",
            stale_after_seconds=heartbeat_stale_after_seconds,
            payload={
                "apply": apply,
                "allow_live_retarget": allow_live_retarget,
                "allow_pending_retarget": allow_pending_retarget,
                "allow_running_nohash_retarget": allow_running_nohash_retarget,
                "max_live_actions": max_live_actions,
                "pending_retarget_after_seconds": pending_retarget_after_seconds,
                "pending_status_retarget_after_seconds": pending_status_retarget_after_seconds,
                "targets": len(targets),
                "actions": action_counts,
                "no_credits_cooldown_cleared": no_credits_cooldown_cleared,
            },
        )
        state_db.record_event(
            conn,
            "org_worker_tick",
            source=f"org_worker:{org_label}",
            message="org worker processed scheduler targets",
            payload={
                "apply": apply,
                "allow_live_retarget": allow_live_retarget,
                "allow_pending_retarget": allow_pending_retarget,
                "allow_running_nohash_retarget": allow_running_nohash_retarget,
                "max_live_actions": max_live_actions,
                "pending_retarget_after_seconds": pending_retarget_after_seconds,
                "pending_status_retarget_after_seconds": pending_status_retarget_after_seconds,
                "targets": len(targets),
                "no_credits_cooldown_cleared": no_credits_cooldown_cleared,
                "results": results,
            },
        )
        conn.commit()
    return {
        "org": org_label,
        "apply": apply,
        "allow_live_retarget": allow_live_retarget,
        "allow_pending_retarget": allow_pending_retarget,
        "allow_running_nohash_retarget": allow_running_nohash_retarget,
        "max_live_actions": max_live_actions,
        "pending_retarget_after_seconds": pending_retarget_after_seconds,
        "pending_status_retarget_after_seconds": pending_status_retarget_after_seconds,
        "targets": len(targets),
        "no_credits_cooldown_cleared": no_credits_cooldown_cleared,
        "action_counts": action_counts,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Per-org worker that consumes central Salad PRL scheduler targets.")
    parser.add_argument("--org", required=True)
    parser.add_argument("--db", default=None)
    parser.add_argument("--apply", action="store_true", help="Perform live Salad create/patch/start actions.")
    parser.add_argument("--allow-live-retarget", action="store_true", help="Allow patching already running slots.")
    parser.add_argument("--allow-pending-retarget", action="store_true", help="Allow patching creating/allocating slots.")
    parser.add_argument(
        "--allow-running-nohash-retarget",
        action="store_true",
        help="Allow patch/restart of running slots that have not yet appeared in the pool.",
    )
    parser.add_argument("--pending-retarget-after-seconds", type=int, default=900)
    parser.add_argument(
        "--pending-status-retarget-after-seconds",
        type=int,
        default=None,
        help="Grace for creating/allocating/deploying slots before recycling; defaults to max(pending retarget, 120).",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument(
        "--slot",
        action="append",
        default=[],
        help="Limit this pass to one slot. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--max-live-actions",
        type=int,
        default=0,
        help="Maximum mutating live actions to apply in this pass; 0 means unlimited.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    slot_filter = parse_slot_filter(args.slot)

    def emit(payload: dict[str, Any]) -> None:
        if args.json:
            print(json_dumps(payload))
        else:
            print(f"org={payload['org']} apply={payload['apply']} targets={payload['targets']} actions={payload['action_counts']}")

    if args.loop:
        while True:
            emit(
                run_once(
                    org_label=args.org,
                    db_path=args.db,
                    apply=args.apply,
                    allow_live_retarget=args.allow_live_retarget,
                    allow_pending_retarget=args.allow_pending_retarget,
                    allow_running_nohash_retarget=args.allow_running_nohash_retarget,
                    pending_retarget_after_seconds=args.pending_retarget_after_seconds,
                    pending_status_retarget_after_seconds=args.pending_status_retarget_after_seconds,
                    slot_filter=slot_filter,
                    max_live_actions=args.max_live_actions,
                )
            )
            time.sleep(args.interval)
    else:
        emit(
            run_once(
                org_label=args.org,
                db_path=args.db,
                apply=args.apply,
                allow_live_retarget=args.allow_live_retarget,
                allow_pending_retarget=args.allow_pending_retarget,
                allow_running_nohash_retarget=args.allow_running_nohash_retarget,
                pending_retarget_after_seconds=args.pending_retarget_after_seconds,
                pending_status_retarget_after_seconds=args.pending_status_retarget_after_seconds,
                slot_filter=slot_filter,
                max_live_actions=args.max_live_actions,
            )
        )


if __name__ == "__main__":
    main()
