#!/usr/bin/env python3
from __future__ import annotations

import argparse
import multiprocessing
import os
import queue
import sys
import time
from typing import Any

import fleet_scheduler
import guard
import health
import org_worker
import reporter
import shadow_compare
import state_db
from config_loader import load_config
from fleet_common import json_dumps


STAGES = {"shadow", "one-org", "all-orgs", "guard-apply"}
NONBLOCKING_RUNTIME_FAILURE_COMPONENTS = {"portal_balances"}
TRANSIENT_WORKER_FAILURE_ERRORS = {
    "http_400:cannot_start_container_group_with_current_status",
    "http_400:replicas_quota_exceeded",
}


def _enabled_org_labels() -> list[str]:
    return [org.label for org in load_config().enabled_orgs()]


def _missing_secret_envs() -> list[str]:
    config = load_config()
    return sorted(
        {
            org.api_key_env
            for org in config.enabled_orgs()
            if not os.environ.get(org.api_key_env)
        }
    )


def _target_profit_violations(db_path: str | None, min_profit_day: float) -> list[dict[str, Any]]:
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        rows = conn.execute(
            """
            SELECT t.org_label, t.slot_name, t.profile_key, t.expected_profit_day,
                   t.mode, t.reason, t.protected,
                   s.observed_status, s.live_hashrate_th,
                   s.protected AS observed_protected,
                   sp.profit_day AS observed_profit_day
            FROM slot_targets t
            LEFT JOIN slots s ON s.org_label = t.org_label AND s.slot_name = t.slot_name
            LEFT JOIN profit_snapshots sp
              ON sp.scope = 'slot'
             AND sp.org_label = t.org_label
             AND sp.slot_name = t.slot_name
             AND sp.at_utc = (
                SELECT at_utc
                FROM profit_snapshots
                WHERE scope = 'slot'
                ORDER BY at_utc DESC, id DESC
                LIMIT 1
             )
            WHERE t.expected_profit_day < ?
            ORDER BY t.expected_profit_day ASC
            """,
            (min_profit_day,),
        ).fetchall()
    violations: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        expected_profit = float(payload.get("expected_profit_day") or 0)
        observed_profit_raw = payload.get("observed_profit_day")
        observed_profit = float(observed_profit_raw) if observed_profit_raw is not None else None
        profit_floor = observed_profit if observed_profit is not None else expected_profit
        protected_positive_fill = (
            str(payload.get("mode") or "") != "optimize"
            and int(payload.get("protected") or 0) > 0
            and str(payload.get("observed_status") or "") == "running"
            and int(payload.get("observed_protected") or 0) > 0
            and float(payload.get("live_hashrate_th") or 0) > 0
            and profit_floor >= 0
        )
        if protected_positive_fill:
            continue
        violations.append(payload)
    return violations


def _worker_failures(worker_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures = []
    for payload in worker_payloads:
        for result in payload.get("results") or []:
            if not result.get("ok", True) and not _is_transient_worker_failure(result):
                failures.append(
                    {
                        "org": payload.get("org"),
                        "slot_name": result.get("slot_name"),
                        "action": result.get("action"),
                        "error": result.get("error"),
                    }
                )
    return failures


def _transient_worker_failures(worker_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures = []
    for payload in worker_payloads:
        for result in payload.get("results") or []:
            if not result.get("ok", True) and _is_transient_worker_failure(result):
                failures.append(
                    {
                        "org": payload.get("org"),
                        "slot_name": result.get("slot_name"),
                        "action": result.get("action"),
                        "error": result.get("error"),
                    }
                )
    return failures


def _is_transient_worker_failure(result: dict[str, Any]) -> bool:
    if str(result.get("action") or "") != "start_failed":
        return False
    error = str(result.get("error") or "")
    return error in TRANSIENT_WORKER_FAILURE_ERRORS


def _worker_payloads_have_action(worker_payloads: list[dict[str, Any]], action: str) -> bool:
    for payload in worker_payloads:
        for result in payload.get("results") or []:
            if str(result.get("action") or "") == action:
                return True
    return False


def _blocking_runtime_failures(runtime_failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        failure
        for failure in runtime_failures
        if str(failure.get("component") or "") not in NONBLOCKING_RUNTIME_FAILURE_COMPONENTS
    ]


def _nonblocking_runtime_failures(runtime_failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        failure
        for failure in runtime_failures
        if str(failure.get("component") or "") in NONBLOCKING_RUNTIME_FAILURE_COMPONENTS
    ]


def _scheduler_payload_has_replacement_targets(scheduler_payload: dict[str, Any]) -> bool:
    for target in scheduler_payload.get("targets") or []:
        reason = str(target.get("reason") or "")
        if ":replace_nohash_observed_profile:" in reason:
            return True
        if ":replace_negative_observed_profile:" in reason:
            return True
    return False


def evaluate_gates(
    *,
    db_path: str | None,
    scheduler_payload: dict[str, Any],
    worker_payloads: list[dict[str, Any]],
    guard_payload: dict[str, Any] | None,
    report_payload: dict[str, Any],
    health_payload: dict[str, Any],
    allow_degraded: bool,
    require_fresh_heartbeats: bool = False,
) -> dict[str, Any]:
    config = load_config()
    min_profit = config.risk.min_profit_for_mode(str(scheduler_payload.get("mode") or None))
    target_violations = _target_profit_violations(db_path, min_profit)
    worker_failures = _worker_failures(worker_payloads)
    transient_worker_failures = _transient_worker_failures(worker_payloads)
    runtime_failures = health_payload.get("runtime_failures") or []
    blocking_runtime_failures = _blocking_runtime_failures(runtime_failures)
    nonblocking_runtime_failures = _nonblocking_runtime_failures(runtime_failures)
    stale_heartbeats = health_payload.get("stale_heartbeats") or []
    failed: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    assigned = int(scheduler_payload.get("assigned_targets") or 0)
    target_slots = int(scheduler_payload.get("target_slots") or 0)
    if assigned < target_slots:
        failed.append(
            {
                "gate": "target_coverage",
                "message": f"assigned {assigned}/{target_slots} scheduler targets",
            }
        )
    if target_violations:
        failed.append(
            {
                "gate": "target_profit",
                "message": f"{len(target_violations)} targets below min profit {min_profit:.3f}",
                "examples": target_violations[:5],
            }
        )
    if worker_failures:
        failed.append(
            {
                "gate": "worker_actions",
                "message": f"{len(worker_failures)} worker action failures",
                "examples": worker_failures[:5],
            }
        )
    if transient_worker_failures:
        warnings.append(
            {
                "gate": "worker_actions",
                "message": f"{len(transient_worker_failures)} transient worker action failures",
                "examples": transient_worker_failures[:5],
            }
        )
    if health_payload.get("health") == "down":
        failed.append({"gate": "health", "message": "health.py reports down"})
    if blocking_runtime_failures and not allow_degraded:
        failed.append(
            {
                "gate": "runtime_failures",
                "message": f"{len(blocking_runtime_failures)} blocking runtime failures present",
                "examples": blocking_runtime_failures[:5],
            }
        )
    if stale_heartbeats and require_fresh_heartbeats and not allow_degraded:
        failed.append(
            {
                "gate": "stale_heartbeats",
                "message": f"{len(stale_heartbeats)} stale heartbeats present",
                "examples": stale_heartbeats[:5],
            }
        )

    if health_payload.get("health") == "degraded" and allow_degraded:
        warnings.append({"gate": "health", "message": "health.py reports degraded but allow_degraded is set"})
    if nonblocking_runtime_failures:
        warnings.append(
            {
                "gate": "runtime_failures",
                "message": f"{len(nonblocking_runtime_failures)} non-blocking runtime failures present",
                "examples": nonblocking_runtime_failures[:5],
            }
        )
    if stale_heartbeats and not require_fresh_heartbeats:
        warnings.append(
            {
                "gate": "stale_heartbeats",
                "message": f"{len(stale_heartbeats)} stale heartbeats present; pass --require-fresh-heartbeats to make this a hard gate",
                "examples": stale_heartbeats[:5],
            }
        )
    if report_payload.get("running_no_live_billable_slots"):
        warnings.append(
            {
                "gate": "no_hash",
                "message": f"{len(report_payload['running_no_live_billable_slots'])} billable no-hash slots in latest report",
            }
        )
    if report_payload.get("negative_slots"):
        warnings.append(
            {
                "gate": "negative_slots",
                "message": f"{len(report_payload['negative_slots'])} negative slots in latest report",
            }
        )
    if guard_payload and guard_payload.get("decisions"):
        actions = {}
        for decision in guard_payload.get("decisions") or []:
            action = str(decision.get("action") or "unknown")
            actions[action] = actions.get(action, 0) + 1
        warnings.append({"gate": "guard_decisions", "message": "guard has active decisions", "actions": actions})

    return {
        "ok": not failed,
        "failed": failed,
        "warnings": warnings,
        "coverage": {"assigned_targets": assigned, "target_slots": target_slots},
        "min_profit_day": min_profit,
        "require_fresh_heartbeats": require_fresh_heartbeats,
    }


def _worker_orgs_for_stage(stage: str, org_label: str | None) -> list[str]:
    if stage == "shadow":
        return _enabled_org_labels()
    if stage == "one-org":
        if not org_label:
            raise SystemExit("--org is required for --stage one-org")
        if org_label not in _enabled_org_labels():
            raise SystemExit(f"unknown or disabled org: {org_label}")
        return [org_label]
    if stage == "all-orgs":
        return _enabled_org_labels()
    return []


def _run_org_worker_task(kwargs: dict[str, Any]) -> dict[str, Any]:
    worker_kwargs = {key: value for key, value in kwargs.items() if key != "_api_key_env"}
    return _rollout_worker_payload(org_worker.run_once(**worker_kwargs))


def _rollout_worker_payload(payload: dict[str, Any]) -> dict[str, Any]:
    results = []
    for result in payload.get("results") or []:
        results.append(
            {
                "slot_name": result.get("slot_name"),
                "action": result.get("action"),
                "ok": result.get("ok", True),
                "error": result.get("error"),
            }
        )
    return {
        "org": payload.get("org"),
        "apply": payload.get("apply"),
        "targets": payload.get("targets"),
        "action_counts": payload.get("action_counts") or {},
        "results": results,
    }


def _run_org_worker_process(kwargs: dict[str, Any], result_queue: Any) -> None:
    try:
        result_queue.put(("ok", _run_org_worker_task(kwargs)))
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)[:500]))


def _join_worker_process(process: Any) -> None:
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)


def _org_worker_timeout_seconds() -> float:
    raw = os.environ.get("PRL_ORG_WORKER_TIMEOUT_SECONDS", "180")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 180.0


def _run_org_worker_batch(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ctx = multiprocessing.get_context("fork")
    workers = []
    for task in tasks:
        result_queue = ctx.Queue(maxsize=1)
        process = ctx.Process(target=_run_org_worker_process, args=(task, result_queue))
        process.start()
        workers.append((task, process, result_queue))

    results: list[dict[str, Any]] = []
    timeout_seconds = _org_worker_timeout_seconds()
    try:
        for task, process, result_queue in workers:
            deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
            while True:
                try:
                    item = result_queue.get(timeout=0.5)
                    break
                except queue.Empty:
                    if not process.is_alive():
                        process.join(5)
                        raise RuntimeError(
                            f"org worker {task['org_label']} exited without result rc={process.exitcode}"
                        )
                    if deadline is not None and time.monotonic() >= deadline:
                        _join_worker_process(process)
                        raise TimeoutError(
                            f"org worker {task['org_label']} timed out after {timeout_seconds:.1f}s"
                        )
            _join_worker_process(process)
            if item[0] == "ok":
                results.append(item[1])
            else:
                raise RuntimeError(f"org worker {task['org_label']} failed: {item[1]}: {item[2]}")
    except BaseException:
        for _, process, _ in workers:
            if process.is_alive():
                _join_worker_process(process)
        raise
    return results


def _batch_org_worker_tasks(tasks: list[dict[str, Any]], max_workers: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    remaining = list(tasks)
    while remaining:
        batch: list[dict[str, Any]] = []
        used_api_keys: set[str] = set()
        next_remaining: list[dict[str, Any]] = []
        for task in remaining:
            api_key_env = str(task.get("_api_key_env") or task["org_label"])
            if len(batch) < max_workers and api_key_env not in used_api_keys:
                batch.append(task)
                used_api_keys.add(api_key_env)
            else:
                next_remaining.append(task)
        batches.append(batch)
        remaining = next_remaining
    return batches


def _run_org_workers(
    orgs: list[str],
    *,
    db_path: str | None,
    apply_workers: bool,
    allow_live_retarget: bool,
    allow_pending_retarget: bool,
    pending_retarget_after_seconds: int,
    pending_status_retarget_after_seconds: int | None,
    worker_parallelism: int,
) -> list[dict[str, Any]]:
    config = load_config()
    api_key_env_by_org = {org.label: org.api_key_env for org in config.enabled_orgs()}
    tasks = [
        {
            "org_label": org,
            "_api_key_env": api_key_env_by_org.get(org),
            "db_path": db_path,
            "apply": apply_workers,
            "allow_live_retarget": allow_live_retarget,
            "allow_pending_retarget": allow_pending_retarget,
            "pending_retarget_after_seconds": pending_retarget_after_seconds,
            "pending_status_retarget_after_seconds": pending_status_retarget_after_seconds,
            "heartbeat_stale_after_seconds": 0,
        }
        for org in orgs
    ]
    if worker_parallelism <= 1 or len(tasks) <= 1:
        return [_run_org_worker_task(task) for task in tasks]

    max_workers = min(worker_parallelism, len(tasks))
    # org_worker loads the legacy watcher through process-global environment
    # variables. Processes keep each organization isolated; threads would not.
    # Organizations sharing one Salad API key are also kept out of the same
    # process batch so they do not consume the same per-minute budget at once.
    results: list[dict[str, Any]] = []
    for batch in _batch_org_worker_tasks(tasks, max_workers):
        results.extend(_run_org_worker_batch(batch))
    return results


def run_rollout(
    *,
    stage: str,
    db_path: str | None = None,
    org_label: str | None = None,
    price: float | None = None,
    fee: float | None = None,
    apply_workers: bool = False,
    apply_guard: bool = False,
    confirm_all_orgs: bool = False,
    allow_live_retarget: bool = False,
    allow_pending_retarget: bool = False,
    confirm_live_retarget: bool = False,
    skip_workers: bool = False,
    skip_guard: bool = False,
    refresh_report: bool = False,
    refresh_timeout_seconds: int = 45,
    allow_degraded: bool = False,
    require_secrets: bool = False,
    require_fresh_heartbeats: bool = False,
    schedule_width: int = 10,
    pending_retarget_after_seconds: int = 45,
    pending_status_retarget_after_seconds: int | None = None,
    worker_parallelism: int = 1,
) -> dict[str, Any]:
    if stage not in STAGES:
        raise SystemExit(f"unknown stage {stage!r}; expected one of {', '.join(sorted(STAGES))}")
    if stage == "all-orgs" and apply_workers and not confirm_all_orgs:
        raise SystemExit("confirm_all_orgs is required for stage='all-orgs' with apply_workers=True")
    if stage == "guard-apply" and not apply_guard:
        raise SystemExit("apply_guard is required for stage='guard-apply'")
    if allow_live_retarget and not apply_workers:
        raise SystemExit("apply_workers is required when allow_live_retarget is set")
    if allow_live_retarget and not confirm_live_retarget:
        raise SystemExit("confirm_live_retarget is required when allow_live_retarget is set")
    if require_secrets:
        missing = _missing_secret_envs()
        if missing:
            raise SystemExit(f"missing env vars: {', '.join(missing)}")
    if worker_parallelism < 1:
        raise SystemExit("worker_parallelism must be at least 1")

    checkpoint = None
    if apply_workers or apply_guard:
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            row = state_db.create_rollout_checkpoint(
                conn,
                name=f"pre-{stage}",
                stage=stage,
                payload={
                    "apply_workers": apply_workers,
                    "apply_guard": apply_guard,
                    "allow_live_retarget": allow_live_retarget,
                    "allow_pending_retarget": allow_pending_retarget,
                },
            )
            conn.commit()
            checkpoint = {
                "id": row["id"],
                "name": row["name"],
                "stage": row["stage"],
                "target_count": row["target_count"],
                "created_at_utc": row["created_at_utc"],
            }

    scheduler_payload = fleet_scheduler.schedule_once(
        db_path=db_path,
        price=price,
        fee=fee,
        width=schedule_width,
        dry_run=False,
    )

    worker_payloads: list[dict[str, Any]] = []
    if not skip_workers:
        worker_orgs = _worker_orgs_for_stage(stage, org_label)
        worker_payloads = _run_org_workers(
            worker_orgs,
            db_path=db_path,
            apply_workers=apply_workers,
            allow_live_retarget=allow_live_retarget,
            allow_pending_retarget=allow_pending_retarget,
            pending_retarget_after_seconds=pending_retarget_after_seconds,
            pending_status_retarget_after_seconds=pending_status_retarget_after_seconds,
            worker_parallelism=worker_parallelism,
        )
        scheduler_payload = fleet_scheduler.schedule_once(
            db_path=db_path,
            price=price,
            fee=fee,
            width=schedule_width,
            dry_run=False,
        )
        if apply_workers and (
            _worker_payloads_have_action(worker_payloads, "cooldown_pending")
            or _scheduler_payload_has_replacement_targets(scheduler_payload)
        ):
            second_pass_payloads = _run_org_workers(
                worker_orgs,
                db_path=db_path,
                apply_workers=apply_workers,
                allow_live_retarget=allow_live_retarget,
                allow_pending_retarget=allow_pending_retarget,
                pending_retarget_after_seconds=pending_retarget_after_seconds,
                pending_status_retarget_after_seconds=pending_status_retarget_after_seconds,
                worker_parallelism=worker_parallelism,
            )
            worker_payloads.extend(second_pass_payloads)
            scheduler_payload = fleet_scheduler.schedule_once(
                db_path=db_path,
                price=price,
                fee=fee,
                width=schedule_width,
                dry_run=False,
            )

    guard_payload = None
    if not skip_guard:
        guard_payload = guard.run_once(db_path=db_path, price=price, apply=apply_guard)

    report_payload = reporter.build_report(
        db_path,
        refresh=refresh_report,
        refresh_timeout_seconds=refresh_timeout_seconds,
    )
    health_payload = health.build_health(db_path)
    shadow_payload = shadow_compare.build_shadow_compare(db_path)
    gates = evaluate_gates(
        db_path=db_path,
        scheduler_payload=scheduler_payload,
        worker_payloads=worker_payloads,
        guard_payload=guard_payload,
        report_payload=report_payload,
        health_payload=health_payload,
        allow_degraded=allow_degraded,
        require_fresh_heartbeats=require_fresh_heartbeats,
    )
    if not shadow_payload.get("ok"):
        gates["failed"].append(
            {
                "gate": "shadow_compare",
                "message": f"{len(shadow_payload.get('gate_failures') or [])} shadow comparison gate failures",
                "examples": shadow_payload.get("gate_failures") or [],
            }
        )
        gates["ok"] = False
    if shadow_payload.get("warnings"):
        gates["warnings"].append(
            {
                "gate": "shadow_compare",
                "message": f"{len(shadow_payload.get('warnings') or [])} shadow comparison warnings",
                "examples": (shadow_payload.get("warnings") or [])[:5],
            }
        )
    return {
        "stage": stage,
        "apply_workers": apply_workers,
        "apply_guard": apply_guard,
        "allow_live_retarget": allow_live_retarget,
        "allow_pending_retarget": allow_pending_retarget,
        "pending_retarget_after_seconds": pending_retarget_after_seconds,
        "pending_status_retarget_after_seconds": pending_status_retarget_after_seconds,
        "worker_parallelism": worker_parallelism,
        "checkpoint": checkpoint,
        "scheduler": {key: value for key, value in scheduler_payload.items() if key != "targets"},
        "workers": [
            {
                "org": payload["org"],
                "apply": payload["apply"],
                "targets": payload["targets"],
                "action_counts": payload["action_counts"],
            }
            for payload in worker_payloads
        ],
        "guard": (
            {
                "issue_count": guard_payload.get("issue_count"),
                "decisions": len(guard_payload.get("decisions") or []),
                "apply": guard_payload.get("apply"),
            }
            if guard_payload
            else None
        ),
        "report": {
            "assigned_targets": report_payload.get("assigned_targets"),
            "target_slots": report_payload.get("target_slots"),
            "active_pending_slots": report_payload.get("active_pending_slots"),
            "live_hashing_gpus": report_payload.get("live_hashing_gpus"),
            "no_hash": len(report_payload.get("running_no_live_billable_slots") or []),
            "negative": len(report_payload.get("negative_slots") or []),
            "stuck": len(report_payload.get("stuck_slots") or []),
            "refresh_error": report_payload.get("refresh_error"),
        },
        "health": {
            "health": health_payload.get("health"),
            "target_count": health_payload.get("target_count"),
            "slot_count": health_payload.get("slot_count"),
            "runtime_failures": len(health_payload.get("runtime_failures") or []),
            "guard_issues": len(health_payload.get("guard_issues") or []),
            "stale_heartbeats": len(health_payload.get("stale_heartbeats") or []),
        },
        "shadow_compare": {
            "ok": shadow_payload.get("ok"),
            "unsafe_targets": len(shadow_payload.get("unsafe_targets") or []),
            "missing_targets": len(shadow_payload.get("missing_targets") or []),
            "mismatches": len(shadow_payload.get("mismatches") or []),
            "warnings": len(shadow_payload.get("warnings") or []),
            "unique_target_profiles": (shadow_payload.get("diversification") or {}).get("unique_target_profiles"),
            "top_profile_share": (shadow_payload.get("diversification") or {}).get("top_profile_share"),
        },
        "gates": gates,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled rollout runner for the Salad PRL scheduler stack.")
    parser.add_argument("--stage", choices=sorted(STAGES), default="shadow")
    parser.add_argument("--org", default=None, help="Required for --stage one-org.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--fee", type=float, default=None)
    parser.add_argument("--width", type=int, default=10)
    parser.add_argument("--apply-workers", action="store_true", help="Allow org_worker live Salad actions.")
    parser.add_argument("--apply-guard", action="store_true", help="Allow guard v2 live retarget/stop actions.")
    parser.add_argument("--allow-live-retarget", action="store_true", help="Allow org_worker to patch running slots.")
    parser.add_argument("--allow-pending-retarget", action="store_true", help="Allow org_worker to patch creating/allocating slots.")
    parser.add_argument("--pending-retarget-after-seconds", type=int, default=45)
    parser.add_argument(
        "--pending-status-retarget-after-seconds",
        type=int,
        default=None,
        help="Grace for creating/allocating/deploying slots; defaults to max(pending retarget, 120).",
    )
    parser.add_argument("--worker-parallelism", type=int, default=1)
    parser.add_argument("--confirm-live-retarget", action="store_true", help="Required with --allow-live-retarget.")
    parser.add_argument("--confirm-all-orgs", action="store_true", help="Required with --stage all-orgs --apply-workers.")
    parser.add_argument("--skip-workers", action="store_true")
    parser.add_argument("--skip-guard", action="store_true")
    parser.add_argument("--refresh-report", action="store_true", help="Fetch a fresh live report snapshot.")
    parser.add_argument("--refresh-timeout", type=int, default=45)
    parser.add_argument("--allow-degraded", action="store_true")
    parser.add_argument("--require-secrets", action="store_true")
    parser.add_argument("--require-fresh-heartbeats", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = run_rollout(
        stage=args.stage,
        db_path=args.db,
        org_label=args.org,
        price=args.price,
        fee=args.fee,
        apply_workers=args.apply_workers,
        apply_guard=args.apply_guard,
        confirm_all_orgs=args.confirm_all_orgs,
        allow_live_retarget=args.allow_live_retarget,
        allow_pending_retarget=args.allow_pending_retarget,
        confirm_live_retarget=args.confirm_live_retarget,
        skip_workers=args.skip_workers,
        skip_guard=args.skip_guard,
        refresh_report=args.refresh_report,
        refresh_timeout_seconds=args.refresh_timeout,
        allow_degraded=args.allow_degraded,
        require_secrets=args.require_secrets,
        require_fresh_heartbeats=args.require_fresh_heartbeats,
        schedule_width=args.width,
        pending_retarget_after_seconds=args.pending_retarget_after_seconds,
        pending_status_retarget_after_seconds=args.pending_status_retarget_after_seconds,
        worker_parallelism=args.worker_parallelism,
    )
    if args.json:
        print(json_dumps(payload))
    else:
        gates = payload["gates"]
        print(
            f"stage={payload['stage']} ok={gates['ok']} "
            f"targets={gates['coverage']['assigned_targets']}/{gates['coverage']['target_slots']} "
            f"health={payload['health']['health']} shadow={payload['shadow_compare']['ok']}"
        )
        if payload["workers"]:
            for worker in payload["workers"]:
                print(f"worker {worker['org']}: apply={worker['apply']} actions={worker['action_counts']}")
        if payload.get("checkpoint"):
            print(f"checkpoint: id={payload['checkpoint']['id']} targets={payload['checkpoint']['target_count']}")
        if payload["guard"]:
            print(f"guard: apply={payload['guard']['apply']} issues={payload['guard']['issue_count']} decisions={payload['guard']['decisions']}")
        for failure in gates["failed"]:
            print(f"FAIL {failure['gate']}: {failure['message']}")
        for warning in gates["warnings"]:
            print(f"WARN {warning['gate']}: {warning['message']}")
    if not payload["gates"]["ok"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
