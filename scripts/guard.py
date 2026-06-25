#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import org_worker
import profit_model
import profile_scorer
import salad_prl_profit_snapshot as snapshot
import state_db
from config_loader import load_config
from fleet_common import env_int, json_dumps, utc_now


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
LEGACY_GUARD = SCRIPT_DIR / "salad_prl_guard.py"
DEFAULT_NO_HASH_GRACE_SECONDS = 60
DEFAULT_NEGATIVE_GRACE_SECONDS = 90


def age_seconds(at_utc: str | None) -> float:
    if not at_utc:
        return 0.0
    try:
        at = datetime.fromisoformat(str(at_utc).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - at).total_seconds())


def issue_age_seconds(issue_row: Any, payload_row: dict[str, Any]) -> float:
    age = age_seconds(str(issue_row["first_seen_utc"]))
    try:
        state_age = float(payload_row.get("state_age_seconds") or 0)
    except (TypeError, ValueError):
        state_age = 0.0
    return max(age, state_age)


def analyze_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    slots = payload.get("slots") or []
    no_hash = payload.get("running_no_live_billable_slots") or []
    negative = [
        row
        for row in slots
        if float(row.get("profit_day") or 0) < 0
    ]
    return {
        "totals": payload.get("totals") or {},
        "fresh_workers": payload.get("fresh_workers"),
        "running_no_live_billable_slots": no_hash,
        "negative_slots": negative,
        "issue_count": len(no_hash) + len(negative),
    }


def issue_current_profile_key(conn, row: dict[str, Any]) -> str | None:
    slot_name = str(row.get("slot") or "")
    org_label = str(row.get("org") or "")
    slot_row = conn.execute(
        "SELECT observed_profile_key FROM slots WHERE org_label = ? AND slot_name = ?",
        (org_label, slot_name),
    ).fetchone()
    if slot_row and slot_row["observed_profile_key"]:
        return str(slot_row["observed_profile_key"])
    gpu_key = str(row.get("gpu") or "").lower()
    priority = str(row.get("priority") or "").lower()
    if not gpu_key or not priority or gpu_key == "requested":
        return None
    profile = conn.execute(
        """
        SELECT profile_key
        FROM gpu_profiles
        WHERE gpu_key = ? AND priority = ?
        ORDER BY memory_mb DESC
        LIMIT 1
        """,
        (gpu_key, priority),
    ).fetchone()
    return str(profile["profile_key"]) if profile else None


def snapshot_profile_key(conn, row: dict[str, Any]) -> str | None:
    from_payload = profit_model.observed_profile_key(row.get("gpu"), row.get("priority"))
    if from_payload:
        return from_payload
    return issue_current_profile_key(conn, row)


def snapshot_worker_row(row: dict[str, Any]) -> dict[str, Any] | None:
    worker_name = str(row.get("worker") or "")
    if not worker_name or worker_name == "NO_POOL_HASHRATE":
        return None
    slot_name = str(row.get("slot") or "")
    org_label = str(row.get("org") or "")
    if not slot_name or not org_label:
        return None
    return {
        "worker_name": worker_name,
        "org_label": org_label,
        "slot_name": slot_name,
        "instance_id": snapshot.worker_instance_id(worker_name),
        "gpu_key": row.get("gpu"),
        "reported_hashrate_th": row.get("th"),
        "stale": False,
        "last_stats_at": row.get("last_stats_at"),
    }


def replacement_target(
    conn,
    *,
    org_label: str,
    slot_name: str,
    issue_type: str,
    current_profile_key: str | None,
    decision_price: float,
    min_profit_day: float,
) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT s.profile_key, s.mode, s.decision_price_usd, s.expected_profit_day,
               s.score, s.risk_tier, p.gpu_key, p.priority, p.memory_mb, p.label
        FROM profile_scores s
        JOIN gpu_profiles p ON p.profile_key = s.profile_key
        WHERE s.expected_profit_day >= ?
          AND s.risk_tier NOT IN ('negative', 'marginal', 'blocked_priority')
        ORDER BY s.score DESC, s.expected_profit_day DESC
        """,
        (min_profit_day,),
    ).fetchall()
    cooldowns = state_db.active_search_cooldowns(conn)
    availability = state_db.latest_profile_availability(conn)
    org_availability = availability.get(org_label, {})

    def available(row: Any, *, allow_probe_fallback: bool) -> bool:
        profile = str(row["profile_key"])
        if current_profile_key and profile == current_profile_key:
            return False
        if (org_label, slot_name, profile) in cooldowns or (org_label, "*", profile) in cooldowns:
            return False
        if profile in org_availability:
            avail = org_availability[profile]
            if avail.get("ok") and int(avail.get("available_count") or 0) <= 0:
                return allow_probe_fallback
        return True

    for allow_probe_fallback in (False, True):
        for row in rows:
            if not available(row, allow_probe_fallback=allow_probe_fallback):
                continue
            reason = f"guard_{issue_type}_retarget"
            if allow_probe_fallback:
                reason += ":availability_probe_fallback"
            return {
                "org_label": org_label,
                "slot_name": slot_name,
                "profile_key": str(row["profile_key"]),
                "gpu_key": row["gpu_key"],
                "priority": row["priority"],
                "memory_mb": row["memory_mb"],
                "label": row["label"],
                "mode": row["mode"],
                "decision_price_usd": decision_price,
                "expected_profit_day": row["expected_profit_day"],
                "protected": False,
                "reason": reason,
                "assigned_at_utc": utc_now(),
            }
    return None


def org_by_label() -> dict[str, Any]:
    return {org.label: org for org in load_config().enabled_orgs()}


def guard_failure_component(org_label: str, slot_name: str, issue_type: str) -> str:
    return f"guard:{org_label}:{slot_name}:{issue_type}"


def guard_patch_cooldown_seconds() -> int:
    try:
        value = int(os.environ.get("PRL_PENDING_PROFILE_COOLDOWN_SECONDS", "600"))
    except ValueError:
        value = 600
    return max(60, value)


def record_guard_patch_cooldown(conn, target: dict[str, Any], *, reason: str) -> None:
    now = datetime.now(UTC)
    state_db.record_search_state(
        conn,
        {
            "org_label": str(target["org_label"]),
            "slot_name": str(target["slot_name"]),
            "profile_key": str(target["profile_key"]),
            "no_gpu_since_utc": now.isoformat(timespec="seconds"),
            "sleep_until_utc": (now + timedelta(seconds=guard_patch_cooldown_seconds())).isoformat(timespec="seconds"),
            "attempts": 1,
            "reason": reason,
            "updated_at_utc": now.isoformat(timespec="seconds"),
        },
    )


def instance_ids_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        instance_id = str(row.get("id") or "")
        if not instance_id or instance_id in seen:
            continue
        seen.add(instance_id)
        ids.append(instance_id)
    return ids


def append_unique_instance_id(ids: list[str], instance_id: str | None) -> None:
    value = str(instance_id or "")
    if value and value not in ids:
        ids.append(value)


def apply_guard_target(
    target: dict[str, Any],
    *,
    db_path: str | None = None,
    stop_if_no_target: bool = False,
    reason: str,
) -> dict[str, Any]:
    orgs = org_by_label()
    org = orgs.get(str(target["org_label"]))
    if org is None:
        raise RuntimeError(f"unknown org {target['org_label']}")
    config = load_config()
    watch = org_worker.load_watch_module(
        org,
        decision_price=float(target["decision_price_usd"]),
        min_profit_day=config.risk.min_profit_for_mode(),
    )
    org_worker.install_rate_limited_request(watch, org, db_path=db_path)
    slot_name = str(target["slot_name"])
    if stop_if_no_target:
        watch.request("POST", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers/{slot_name}/stop")
        return {"action": "stop", "slot_name": slot_name, "applied": True}
    candidate = org_worker.candidate_from_target(watch, target)
    try:
        _before_group, before_instances = watch.slot_state(slot_name)
    except KeyError:
        before_instances = []
    ok = watch.patch_slot(slot_name, candidate, reason, start_after=False)
    if not ok:
        fallback_ids = instance_ids_from_rows(before_instances)
        append_unique_instance_id(fallback_ids, target.get("snapshot_instance_id"))
        reallocated = []
        for instance_id in fallback_ids:
            watch.reallocate(slot_name, instance_id, f"{reason}:patch_failed")
            reallocated.append(instance_id)
        restart_requested = False
        restart_reason = None
        if not reallocated:
            restart_reason = "patch_failed_without_visible_instances"
            watch.request("POST", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers/{slot_name}/stop")
            watch.start_slot(slot_name, f"{reason}:{restart_reason}")
            restart_requested = True
        return {
            "action": "retarget_fallback",
            "slot_name": slot_name,
            "profile_key": target["profile_key"],
            "applied": True,
            "patch_failed": True,
            "fallback_reason": "patch_slot_returned_false",
            "reallocated_instances": reallocated,
            "pre_patch_instances": len(before_instances),
            "post_patch_instances": None,
            "restart_requested": restart_requested,
            "restart_reason": restart_reason,
        }

    reallocate_ids = instance_ids_from_rows(before_instances)
    append_unique_instance_id(reallocate_ids, target.get("snapshot_instance_id"))
    try:
        _after_group, after_instances = watch.slot_state(slot_name)
    except KeyError:
        after_instances = []
    for instance_id in instance_ids_from_rows(after_instances):
        append_unique_instance_id(reallocate_ids, instance_id)

    reallocated = []
    for instance_id in reallocate_ids:
        watch.reallocate(slot_name, instance_id, reason)
        reallocated.append(instance_id)

    restart_requested = False
    restart_reason = None
    if not before_instances and not after_instances:
        if target.get("snapshot_instance_id"):
            restart_reason = "snapshot_instance_without_salad_instances"
        else:
            restart_reason = "retarget_without_visible_instances"
    elif target.get("force_restart_reason"):
        restart_reason = str(target["force_restart_reason"])
    if restart_reason:
        watch.request("POST", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers/{slot_name}/stop")
        watch.start_slot(slot_name, f"{reason}:{restart_reason}")
        restart_requested = True
    return {
        "action": "retarget",
        "slot_name": slot_name,
        "profile_key": target["profile_key"],
        "applied": True,
        "reallocated_instances": reallocated,
        "pre_patch_instances": len(before_instances),
        "post_patch_instances": len(after_instances),
        "restart_requested": restart_requested,
        "restart_reason": restart_reason,
    }


def enforce_issues(
    *,
    db_path: str | None,
    analysis: dict[str, Any],
    decision_price: float,
    apply: bool,
) -> list[dict[str, Any]]:
    config = load_config()
    min_profit = config.risk.min_profit_for_mode()
    if not analysis.get("fresh_workers") or int(analysis.get("fresh_workers") or 0) < 1:
        return []
    issues: list[dict[str, Any]] = []
    no_hash_grace_seconds = env_int("PRL_GUARD_NOHASH_GRACE_SECONDS", DEFAULT_NO_HASH_GRACE_SECONDS)
    negative_grace_seconds = env_int("PRL_GUARD_NEGATIVE_GRACE_SECONDS", DEFAULT_NEGATIVE_GRACE_SECONDS)
    for row in analysis.get("running_no_live_billable_slots") or []:
        issues.append({"issue_type": "no_hash", "grace_seconds": no_hash_grace_seconds, "row": row})
    for row in analysis.get("negative_slots") or []:
        issues.append({"issue_type": "negative", "grace_seconds": negative_grace_seconds, "row": row})

    decisions: list[dict[str, Any]] = []
    active_keys: set[tuple[str, str, str]] = set()
    actioned_slots: set[tuple[str, str]] = set()
    restart_no_hash_after_actions = env_int("PRL_GUARD_RESTART_AFTER_NOHASH_ACTIONS", 1)
    restart_no_hash_after_seconds = env_int("PRL_GUARD_RESTART_NOHASH_AFTER_SECONDS", 0)
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        profile_scorer.score_profiles(db_path=db_path, decision_price_usd=decision_price, write=True)
        for issue in issues:
            row = dict(issue["row"])
            org_label = str(row.get("org") or "")
            slot_name = str(row.get("slot") or "")
            issue_type = str(issue["issue_type"])
            if not org_label or not slot_name:
                continue
            active_keys.add((org_label, slot_name, issue_type))
            issue_row = state_db.record_guard_issue(
                conn,
                {
                    "org_label": org_label,
                    "slot_name": slot_name,
                    "issue_type": issue_type,
                    "payload": row,
                },
            )
            issue_age = issue_age_seconds(issue_row, row)
            current = issue_current_profile_key(conn, row)
            target = replacement_target(
                conn,
                org_label=org_label,
                slot_name=slot_name,
                issue_type=issue_type,
                current_profile_key=current,
                decision_price=decision_price,
                min_profit_day=min_profit,
            )
            if target is not None:
                target["snapshot_instance_id"] = snapshot.worker_instance_id(str(row.get("worker") or ""))
                if issue_type == "no_hash":
                    if int(issue_row["action_count"] or 0) >= restart_no_hash_after_actions:
                        target["force_restart_reason"] = "repeated_no_hash_after_guard_action"
                    elif restart_no_hash_after_seconds > 0 and issue_age >= restart_no_hash_after_seconds:
                        target["force_restart_reason"] = "prolonged_no_hash"
            decision = {
                "org_label": org_label,
                "slot_name": slot_name,
                "issue_type": issue_type,
                "age_seconds": round(issue_age, 1),
                "grace_seconds": issue["grace_seconds"],
                "current_profile_key": current,
                "target_profile_key": target.get("profile_key") if target else None,
                "action": "wait",
                "apply": apply,
            }
            if issue_age >= float(issue["grace_seconds"]):
                slot_key = (org_label, slot_name)
                if apply and slot_key in actioned_slots:
                    decision["action"] = "skip_duplicate"
                    decision["reason"] = "slot_already_actioned_this_tick"
                elif target is not None:
                    state_db.set_slot_target(conn, target)
                    decision["action"] = "retarget"
                    decision["target"] = target
                else:
                    decision["action"] = "stop"
                if apply:
                    if decision["action"] == "skip_duplicate":
                        pass
                    else:
                        actioned_slots.add(slot_key)
                        # Release SQLite write locks before live Salad calls; the rate limiter also writes to this DB.
                        conn.commit()
                        try:
                            applied = apply_guard_target(
                                target
                                or {
                                    "org_label": org_label,
                                    "slot_name": slot_name,
                                    "decision_price_usd": decision_price,
                                },
                                db_path=db_path,
                                stop_if_no_target=target is None,
                                reason=f"guard_{issue_type}",
                            )
                            decision["applied"] = applied
                            if target is not None and applied.get("patch_failed"):
                                record_guard_patch_cooldown(conn, target, reason=str(applied.get("fallback_reason")))
                                decision["cooldown_profile_key"] = target.get("profile_key")
                                decision["cooldown_seconds"] = guard_patch_cooldown_seconds()
                            state_db.increment_guard_issue_action(conn, org_label, slot_name, issue_type)
                            state_db.clear_failure(conn, guard_failure_component(org_label, slot_name, issue_type))
                            state_db.record_attempt(
                                conn,
                                {
                                    "org_label": org_label,
                                    "slot_name": slot_name,
                                    "action": f"guard_{decision['action']}",
                                    "profile_key": target.get("profile_key") if target else None,
                                    "ok": True,
                                    "payload": decision,
                                },
                            )
                        except Exception as exc:
                            decision["apply_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                            if target is not None and "patch_slot returned false" in str(exc):
                                record_guard_patch_cooldown(conn, target, reason=decision["apply_error"])
                                decision["cooldown_profile_key"] = target.get("profile_key")
                                decision["cooldown_seconds"] = guard_patch_cooldown_seconds()
                            state_db.record_failure(
                                conn,
                                guard_failure_component(org_label, slot_name, issue_type),
                                severity="warning",
                                error_type=type(exc).__name__,
                                message=str(exc)[:180],
                                payload=decision,
                            )
                            state_db.record_attempt(
                                conn,
                                {
                                    "org_label": org_label,
                                    "slot_name": slot_name,
                                    "action": f"guard_{decision['action']}",
                                    "profile_key": target.get("profile_key") if target else None,
                                    "ok": False,
                                    "error": decision["apply_error"],
                                    "payload": decision,
                                },
                            )
                else:
                    state_db.record_attempt(
                        conn,
                        {
                            "org_label": org_label,
                            "slot_name": slot_name,
                            "action": f"dry_run_guard_{decision['action']}",
                            "profile_key": target.get("profile_key") if target else None,
                            "ok": True,
                            "payload": decision,
                        },
                    )
            state_db.record_event(
                conn,
                "guard_decision",
                source="guard",
                level="warning" if decision["action"] != "wait" else "info",
                message=f"guard decision {issue_type} {org_label}/{slot_name}",
                payload=decision,
            )
            decisions.append(decision)
        state_db.clear_guard_issues(conn, active_keys)
        active_components = {
            guard_failure_component(org_label, slot_name, issue_type)
            for org_label, slot_name, issue_type in active_keys
        }
        rows = conn.execute("SELECT component FROM runtime_failures WHERE component LIKE 'guard:%'").fetchall()
        for row in rows:
            component = str(row["component"])
            if component not in active_components:
                state_db.clear_failure(conn, component)
        conn.commit()
    return decisions


def run_once(*, db_path: str | None = None, price: float | None = None, apply: bool = False) -> dict[str, Any]:
    config = load_config()
    decision_price = price
    if decision_price is None:
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            risk = state_db.latest_risk_mode(conn)
            decision_price = float(risk["decision_price_usd"]) if risk else config.risk.decision_price_for_mode()
    try:
        payload = snapshot.build_snapshot(decision_price)
        analysis = analyze_snapshot(payload)
        decisions = enforce_issues(db_path=db_path, analysis=analysis, decision_price=decision_price, apply=apply)
    except Exception as exc:
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            state_db.record_failure(
                conn,
                "guard",
                severity="error",
                error_type=type(exc).__name__,
                message=str(exc)[:180],
                payload={"decision_price": decision_price},
            )
            state_db.write_heartbeat(conn, "guard", status="degraded", payload={"error": type(exc).__name__})
            conn.commit()
        raise
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.clear_failure(conn, "guard")
        snapshot_at = utc_now()
        state_db.record_profit_snapshot(
            conn,
            {
                "at_utc": snapshot_at,
                "scope": "fleet",
                "decision_price_usd": decision_price,
                "live_price_usd": payload.get("live_market_prl_price"),
                "th": (payload.get("totals") or {}).get("th"),
                "cost_day": (payload.get("totals") or {}).get("cost_day"),
                "revenue_day": (payload.get("totals") or {}).get("revenue_day"),
                "profit_day": (payload.get("totals") or {}).get("profit_day"),
                "payload": analysis,
            },
        )
        state_db.reset_slot_hashrates(conn)
        worker_rows = []
        for row in payload.get("slots") or []:
            profile_key = snapshot_profile_key(conn, row)
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": row.get("org"),
                    "slot_name": row.get("slot"),
                    "observed_profile_key": profile_key,
                    "observed_status": "running",
                    "live_hashrate_th": row.get("th"),
                    "protected": True,
                },
            )
            worker_row = snapshot_worker_row(row)
            if worker_row:
                worker_rows.append(worker_row)
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": snapshot_at,
                    "scope": "slot",
                    "org_label": row.get("org"),
                    "slot_name": row.get("slot"),
                    "profile_key": profile_key,
                    "decision_price_usd": decision_price,
                    "live_price_usd": payload.get("live_market_prl_price"),
                    "th": row.get("th"),
                    "cost_day": row.get("cost_day"),
                    "profit_day": row.get("profit_day"),
                    "payload": row,
                },
            )
        state_db.sync_worker_rows(conn, worker_rows)
        state_db.write_heartbeat(
            conn,
            "guard",
            payload={
                "issue_count": analysis["issue_count"],
                "decisions": len(decisions),
                "apply": apply,
                "live_workers": len(worker_rows),
            },
        )
        state_db.record_event(
            conn,
            "guard_snapshot_analyzed",
            source="guard",
            level="warning" if analysis["issue_count"] else "info",
            message="guard analyzed current profit/no-hash state",
            payload={**analysis, "decisions": decisions, "apply": apply},
        )
        conn.commit()
    analysis["decisions"] = decisions
    analysis["apply"] = apply
    return analysis


def exec_legacy_guard() -> None:
    os.execvpe(sys.executable, [sys.executable, str(LEGACY_GUARD)], os.environ.copy())


def main() -> None:
    parser = argparse.ArgumentParser(description="Global guard facade for Salad PRL fleet safety checks.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--once", action="store_true", help="Analyze once without live actions.")
    parser.add_argument("--loop", action="store_true", help="Analyze repeatedly without live actions.")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--apply-legacy", action="store_true", help="Run the existing live guard loop.")
    parser.add_argument("--apply", action="store_true", help="Apply guard v2 retarget/stop actions.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.apply_legacy:
        exec_legacy_guard()
    def run_and_print() -> None:
        payload = run_once(db_path=args.db, price=args.price, apply=args.apply)
        if args.json:
            print(json_dumps(payload))
        else:
            print(
                f"guard issue_count={payload['issue_count']} "
                f"profit_day=${float((payload.get('totals') or {}).get('profit_day') or 0):.3f}"
            )

    if args.loop:
        while True:
            run_and_print()
            time.sleep(args.interval)
    else:
        run_and_print()


if __name__ == "__main__":
    main()
