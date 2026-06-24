#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import sys
import time
from datetime import UTC, datetime
from typing import Any

import fleet_scheduler
import state_db
from config_loader import OrgConfig, load_config
from fleet_common import env_int, json_dumps, utc_now
from profit_model import profile_key


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
WATCH_PATH = SCRIPT_DIR / "salad_prl_watch.py"


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
    rows = conn.execute(
        """
        SELECT t.*, p.gpu_key, p.priority, p.memory_mb, p.label,
               s.observed_status_since_utc, s.observed_profile_since_utc
        FROM slot_targets t
        JOIN gpu_profiles p ON p.profile_key = t.profile_key
        LEFT JOIN slots s ON s.org_label = t.org_label AND s.slot_name = t.slot_name
        WHERE t.org_label = ?
        ORDER BY t.slot_name
        """,
        (org_label,),
    ).fetchall()
    return [dict(row) for row in rows]


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


def planned_action(
    watch: Any,
    slot_name: str,
    target: dict[str, Any],
    *,
    protect_running: bool = True,
    protect_pending: bool = True,
    pending_retarget_after_seconds: int = 45,
) -> dict[str, Any]:
    try:
        group, instances = watch.slot_state(slot_name)
    except KeyError:
        group, instances = None, []
    current = current_profile_key(watch, group)
    counts = active_counts(group)
    status = observed_status(group, counts)
    if group is None:
        action = "create"
        reason = "missing_container_group"
    elif current != target["profile_key"]:
        if protect_running and counts["running"] > 0:
            action = "observe"
            reason = f"protected_running_profile_mismatch:{current or 'unknown'}"
        elif counts["creating"] + counts["allocating"] > 0:
            pending_age = age_seconds(target.get("observed_status_since_utc"))
            if protect_pending:
                action = "observe"
                reason = f"protected_pending_profile_mismatch:{current or 'unknown'}"
            elif pending_age is None or pending_age < pending_retarget_after_seconds:
                action = "observe"
                age_text = "unknown" if pending_age is None else f"{pending_age:.1f}"
                reason = (
                    f"pending_profile_mismatch_wait:{current or 'unknown'}:"
                    f"age_{age_text}_lt_{pending_retarget_after_seconds}"
                )
            else:
                action = "patch"
                reason = f"stale_pending_profile_mismatch:{current or 'unknown'}:age_{pending_age:.1f}"
        else:
            action = "patch"
            reason = f"profile_mismatch:{current or 'unknown'}"
    elif counts["running"] + counts["creating"] + counts["allocating"] <= 0:
        action = "start"
        reason = "target_stopped_or_empty"
    else:
        action = "observe"
        reason = "target_already_active_or_pending"
    return {
        "slot_name": slot_name,
        "action": action,
        "reason": reason,
        "target_profile_key": target["profile_key"],
        "current_profile_key": current,
        "observed_status": status,
        "protected": counts["running"] > 0,
        "counts": counts,
        "instance_count": len(instances),
    }


def candidate_from_target(watch: Any, target: dict[str, Any]) -> Any:
    return watch.Candidate(
        str(target["label"]),
        str(target["priority"]),
        (str(target["gpu_key"]),),
        int(target["memory_mb"]),
    )


def execute_action(watch: Any, target: dict[str, Any], plan: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    if not apply or plan["action"] == "observe":
        return {"ok": True, "applied": False, **plan}
    candidate = candidate_from_target(watch, target)
    slot_name = str(target["slot_name"])
    if plan["action"] == "create":
        watch.create_slot(slot_name, candidate)
    elif plan["action"] == "patch":
        ok = watch.patch_slot(slot_name, candidate, "fleet_scheduler_target")
        if not ok:
            raise RuntimeError("patch_slot returned false")
    elif plan["action"] == "start":
        watch.start_slot(slot_name, "fleet_scheduler_target")
    else:
        raise RuntimeError(f"unknown action {plan['action']}")
    return {"ok": True, "applied": True, **plan}


def run_once(
    *,
    org_label: str,
    db_path: str | None = None,
    apply: bool = False,
    schedule_if_empty: bool = True,
    allow_live_retarget: bool = False,
    allow_pending_retarget: bool = False,
    pending_retarget_after_seconds: int = 45,
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
        conn.commit()

    watch = load_watch_module(org, decision_price=decision_price, min_profit_day=min_profit)
    install_rate_limited_request(watch, org, db_path=db_path)
    results: list[dict[str, Any]] = []
    attempt_rows: list[dict[str, Any]] = []
    observation_rows: list[dict[str, Any]] = []
    for target in targets:
        started = time.monotonic()
        plan = planned_action(
            watch,
            str(target["slot_name"]),
            target,
            protect_running=not allow_live_retarget,
            protect_pending=not allow_pending_retarget,
            pending_retarget_after_seconds=pending_retarget_after_seconds,
        )
        try:
            result = execute_action(watch, target, plan, apply=apply)
            ok = True
            error = None
        except Exception as exc:
            result = {"ok": False, "applied": False, **plan, "error": type(exc).__name__}
            ok = False
            error = f"{type(exc).__name__}: {str(exc)[:180]}"
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
        observation_rows.append(
            {
                "org_label": org_label,
                "slot_name": str(target["slot_name"]),
                "observed_profile_key": result.get("current_profile_key"),
                "observed_status": result.get("observed_status"),
                "live_hashrate_th": 0.0,
                "protected": bool(result.get("protected")),
            }
        )
        results.append(result)
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        for attempt in attempt_rows:
            state_db.record_attempt(conn, attempt)
        for observation in observation_rows:
            state_db.update_slot_observation(conn, observation)
        action_counts: dict[str, int] = {}
        for result in results:
            action_counts[str(result["action"])] = action_counts.get(str(result["action"]), 0) + 1
        state_db.write_heartbeat(
            conn,
            f"org_worker:{org_label}",
            payload={
                "apply": apply,
                "allow_live_retarget": allow_live_retarget,
                "allow_pending_retarget": allow_pending_retarget,
                "pending_retarget_after_seconds": pending_retarget_after_seconds,
                "targets": len(targets),
                "actions": action_counts,
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
                "pending_retarget_after_seconds": pending_retarget_after_seconds,
                "targets": len(targets),
                "results": results,
            },
        )
        conn.commit()
    return {
        "org": org_label,
        "apply": apply,
        "allow_live_retarget": allow_live_retarget,
        "allow_pending_retarget": allow_pending_retarget,
        "pending_retarget_after_seconds": pending_retarget_after_seconds,
        "targets": len(targets),
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
    parser.add_argument("--pending-retarget-after-seconds", type=int, default=45)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

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
                    pending_retarget_after_seconds=args.pending_retarget_after_seconds,
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
                pending_retarget_after_seconds=args.pending_retarget_after_seconds,
            )
        )


if __name__ == "__main__":
    main()
