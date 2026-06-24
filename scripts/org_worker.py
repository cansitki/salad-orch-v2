#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import sys
import time
from typing import Any

import fleet_scheduler
import state_db
from config_loader import OrgConfig, load_config
from fleet_common import json_dumps, utc_now
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


def target_rows(conn, org_label: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT t.*, p.gpu_key, p.priority, p.memory_mb, p.label
        FROM slot_targets t
        JOIN gpu_profiles p ON p.profile_key = t.profile_key
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


def planned_action(
    watch: Any,
    slot_name: str,
    target: dict[str, Any],
    *,
    protect_running: bool = True,
    protect_pending: bool = True,
) -> dict[str, Any]:
    try:
        group, instances = watch.slot_state(slot_name)
    except KeyError:
        group, instances = None, []
    current = current_profile_key(watch, group)
    counts = active_counts(group)
    if group is None:
        action = "create"
        reason = "missing_container_group"
    elif current != target["profile_key"]:
        if protect_running and counts["running"] > 0:
            action = "observe"
            reason = f"protected_running_profile_mismatch:{current or 'unknown'}"
        elif protect_pending and counts["creating"] + counts["allocating"] > 0:
            action = "observe"
            reason = f"protected_pending_profile_mismatch:{current or 'unknown'}"
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
    results: list[dict[str, Any]] = []
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        for target in targets:
            started = time.monotonic()
            plan = planned_action(
                watch,
                str(target["slot_name"]),
                target,
                protect_running=not allow_live_retarget,
                protect_pending=not allow_pending_retarget,
            )
            try:
                result = execute_action(watch, target, plan, apply=apply)
                ok = True
                error = None
            except Exception as exc:
                result = {"ok": False, "applied": False, **plan, "error": type(exc).__name__}
                ok = False
                error = f"{type(exc).__name__}: {str(exc)[:180]}"
            state_db.record_attempt(
                conn,
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
                },
            )
            results.append(result)
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
            )
        )


if __name__ == "__main__":
    main()
