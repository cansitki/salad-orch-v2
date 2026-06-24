#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from typing import Any, Callable

import rollout
from fleet_common import json_dumps, utc_now


RolloutRunner = Callable[..., dict[str, Any]]


def _summarize_rollout(payload: dict[str, Any]) -> dict[str, Any]:
    gates = payload.get("gates") or {}
    coverage = gates.get("coverage") or {}
    report = payload.get("report") or {}
    health = payload.get("health") or {}
    shadow = payload.get("shadow_compare") or {}
    return {
        "stage": payload.get("stage"),
        "ok": bool(gates.get("ok")),
        "targets": coverage.get("assigned_targets"),
        "target_slots": coverage.get("target_slots"),
        "health": health.get("health"),
        "shadow_ok": shadow.get("ok"),
        "live_hashing_gpus": report.get("live_hashing_gpus"),
        "no_hash": report.get("no_hash"),
        "negative": report.get("negative"),
        "stuck": report.get("stuck"),
        "failed_gates": [item.get("gate") for item in gates.get("failed") or []],
        "warning_gates": [item.get("gate") for item in gates.get("warnings") or []],
    }


def _run_shadow(
    runner: RolloutRunner,
    *,
    price: float | None,
    fee: float | None,
    require_secrets: bool,
    require_fresh_heartbeats: bool,
    allow_degraded: bool,
) -> dict[str, Any]:
    return runner(
        stage="shadow",
        price=price,
        fee=fee,
        apply_workers=False,
        apply_guard=False,
        require_secrets=require_secrets,
        require_fresh_heartbeats=require_fresh_heartbeats,
        allow_degraded=allow_degraded,
    )


def _run_action(
    runner: RolloutRunner,
    *,
    action: str,
    org: str | None,
    price: float | None,
    fee: float | None,
    require_secrets: bool,
    allow_pending_retarget: bool,
    pending_retarget_after_seconds: int,
) -> dict[str, Any]:
    if action == "guard-apply":
        return runner(
            stage="guard-apply",
            price=price,
            fee=fee,
            apply_guard=True,
            require_secrets=require_secrets,
        )
    if action == "one-org-apply":
        if not org:
            raise SystemExit("--org is required with --apply-one-org")
        return runner(
            stage="one-org",
            org_label=org,
            price=price,
            fee=fee,
            apply_workers=True,
            allow_pending_retarget=allow_pending_retarget,
            pending_retarget_after_seconds=pending_retarget_after_seconds,
            require_secrets=require_secrets,
        )
    raise RuntimeError(f"unknown action {action}")


def run_monitor_tick(
    *,
    price: float | None = None,
    fee: float | None = None,
    require_secrets: bool = False,
    require_fresh_heartbeats: bool = False,
    allow_degraded_shadow: bool = False,
    apply_guard: bool = False,
    apply_one_org: bool = False,
    org: str | None = None,
    allow_pending_retarget: bool = False,
    pending_retarget_after_seconds: int = 45,
    confirm_live_actions: bool = False,
    runner: RolloutRunner = rollout.run_rollout,
) -> dict[str, Any]:
    if (apply_guard or apply_one_org) and not confirm_live_actions:
        raise SystemExit("live actions require --confirm-live-actions")
    if apply_guard and apply_one_org:
        raise SystemExit("choose only one live action per monitor tick")
    if apply_one_org and not org:
        raise SystemExit("--org is required with --apply-one-org")

    shadow_payload = _run_shadow(
        runner,
        price=price,
        fee=fee,
        require_secrets=require_secrets,
        require_fresh_heartbeats=require_fresh_heartbeats,
        allow_degraded=allow_degraded_shadow,
    )
    shadow_summary = _summarize_rollout(shadow_payload)
    action = "none"
    action_payload = None
    action_summary = None

    if shadow_summary["ok"]:
        if apply_guard:
            action = "guard-apply"
        elif apply_one_org:
            action = "one-org-apply"
        if action != "none":
            action_payload = _run_action(
                runner,
                action=action,
                org=org,
                price=price,
                fee=fee,
                require_secrets=require_secrets,
                allow_pending_retarget=allow_pending_retarget,
                pending_retarget_after_seconds=pending_retarget_after_seconds,
            )
            action_summary = _summarize_rollout(action_payload)

    return {
        "at_utc": utc_now(),
        "ok": bool(shadow_summary["ok"] and (action_summary is None or action_summary["ok"])),
        "action": action,
        "shadow": shadow_summary,
        "action_result": action_summary,
        "skipped_live_action": bool((apply_guard or apply_one_org) and not shadow_summary["ok"]),
    }


def _print_tick(payload: dict[str, Any]) -> None:
    shadow = payload["shadow"]
    print(
        f"monitor ok={payload['ok']} action={payload['action']} "
        f"shadow={shadow['ok']} health={shadow['health']} "
        f"targets={shadow['targets']}/{shadow['target_slots']} "
        f"live_hashing={shadow['live_hashing_gpus']} "
        f"no_hash={shadow['no_hash']} negative={shadow['negative']} stuck={shadow['stuck']}"
    )
    if shadow["failed_gates"]:
        print(f"shadow_failed={','.join(str(item) for item in shadow['failed_gates'])}")
    if shadow["warning_gates"]:
        print(f"shadow_warnings={','.join(str(item) for item in shadow['warning_gates'])}")
    if payload["action_result"]:
        result = payload["action_result"]
        print(
            f"action_result ok={result['ok']} stage={result['stage']} "
            f"health={result['health']} no_hash={result['no_hash']} negative={result['negative']}"
        )
        if result["failed_gates"]:
            print(f"action_failed={','.join(str(item) for item in result['failed_gates'])}")
        if result["warning_gates"]:
            print(f"action_warnings={','.join(str(item) for item in result['warning_gates'])}")
    if payload["skipped_live_action"]:
        print("live_action_skipped=shadow_gate_failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe continuous monitor for Salad PRL rollout gates.")
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--fee", type=float, default=None)
    parser.add_argument("--require-secrets", action="store_true")
    parser.add_argument("--require-fresh-heartbeats", action="store_true")
    parser.add_argument(
        "--allow-degraded-shadow",
        action="store_true",
        help="Allow degraded shadow preflight before a confirmed live action; action result remains strict.",
    )
    parser.add_argument("--apply-guard", action="store_true", help="Run guard-apply after a passing shadow gate.")
    parser.add_argument("--apply-one-org", action="store_true", help="Run one-org worker apply after a passing shadow gate.")
    parser.add_argument("--org", default=None, help="Organization label for --apply-one-org.")
    parser.add_argument("--allow-pending-retarget", action="store_true", help="Allow one-org apply to patch stale creating/allocating slots.")
    parser.add_argument("--pending-retarget-after-seconds", type=int, default=45)
    parser.add_argument("--confirm-live-actions", action="store_true", help="Required for any live action.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--max-ticks", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ticks = 0
    while True:
        payload = run_monitor_tick(
            price=args.price,
            fee=args.fee,
            require_secrets=args.require_secrets,
            require_fresh_heartbeats=args.require_fresh_heartbeats,
            allow_degraded_shadow=args.allow_degraded_shadow,
            apply_guard=args.apply_guard,
            apply_one_org=args.apply_one_org,
            org=args.org,
            allow_pending_retarget=args.allow_pending_retarget,
            pending_retarget_after_seconds=args.pending_retarget_after_seconds,
            confirm_live_actions=args.confirm_live_actions,
        )
        if args.json:
            print(json_dumps(payload))
        else:
            _print_tick(payload)
        ticks += 1
        if args.once or not args.loop or (args.max_ticks and ticks >= args.max_ticks):
            break
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
