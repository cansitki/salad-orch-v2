#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import pathlib
import shlex
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

import fleet_scheduler
import price_oracle
import state_db
from config_loader import load_config
from fleet_common import REPO_ROOT, json_dumps


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent


def _with_db(cmd: list[str], db_path: str | None) -> list[str]:
    return [*cmd, "--db", db_path] if db_path else cmd


def has_multi_balance_accounts() -> bool:
    if os.environ.get("SALAD_PORTAL_BALANCE_ACCOUNTS_JSON") or os.environ.get("SALAD_PORTAL_BALANCE_EMAILS"):
        return True
    account_state_dir = pathlib.Path(os.environ.get("PRL_PORTAL_BALANCE_ACCOUNT_STATE_DIR", "state/portal_balance_accounts"))
    if not account_state_dir.is_absolute():
        account_state_dir = REPO_ROOT / account_state_dir
    return any(account_state_dir.glob("*_cookies.txt"))


def process_plan(
    *,
    apply_workers: bool = False,
    runtime_monitor_apply: bool = False,
    db_path: str | None = None,
    include_audit: bool = True,
    include_spike_report: bool = True,
    include_maintenance: bool = False,
    maintenance_apply: bool = False,
    include_runtime_monitor: bool = True,
    include_workers: bool = False,
) -> list[dict[str, Any]]:
    config = load_config()
    portal_balance_interval = str(max(1, int(os.environ.get("PRL_PORTAL_BALANCE_INTERVAL_SECONDS", "60"))))
    availability_org_parallelism = str(max(1, int(os.environ.get("PRL_AVAILABILITY_ORG_PARALLELISM", "10"))))
    availability_profile_parallelism = str(max(1, int(os.environ.get("PRL_AVAILABILITY_PROFILE_PARALLELISM", "4"))))
    if has_multi_balance_accounts():
        portal_balance_cmd = [
            "python3",
            str(SCRIPT_DIR / "portal_multi_balances.py"),
            "--loop",
            "--interval",
            portal_balance_interval,
            "--balance-file",
            "state/salad_balances.json",
            "--cwd",
            str(REPO_ROOT),
        ]
    else:
        portal_balance_cmd = [
            "python3",
            str(SCRIPT_DIR / "portal_balances.py"),
            "--loop",
            "--interval",
            portal_balance_interval,
            "--balance-file",
            "state/salad_balances.json",
            "--cwd",
            str(REPO_ROOT),
            "--cookie-jar",
            "state/portal_cookies.txt",
        ]

    plan = [
        {
            "name": "salad-orch-v2-price",
            "heartbeat": "price_oracle",
            "cmd": _with_db(["python3", str(SCRIPT_DIR / "price_oracle.py"), "--loop", "--interval", "60"], db_path),
        },
        {
            "name": "salad-orch-v2-availability",
            "heartbeat": "availability_probe",
            "cmd": _with_db(
                [
                    "python3",
                    str(SCRIPT_DIR / "availability_probe.py"),
                    "--loop",
                    "--interval",
                    "60",
                    "--priorities",
                    "batch,low",
                    "--org-parallelism",
                    availability_org_parallelism,
                    "--profile-parallelism",
                    availability_profile_parallelism,
                ],
                db_path,
            ),
        },
        {
            "name": "salad-orch-v2-scheduler",
            "heartbeat": "fleet_scheduler",
            "cmd": _with_db(["python3", str(SCRIPT_DIR / "fleet_scheduler.py"), "--loop", "--interval", "60"], db_path),
        },
        {
            "name": "salad-orch-v2-guard",
            "heartbeat": "guard",
            "cmd": _with_db(["python3", str(SCRIPT_DIR / "guard.py"), "--loop", "--interval", "30"], db_path),
        },
        {
            "name": "salad-orch-v2-balances",
            "heartbeat": "portal_balances",
            "cmd": _with_db(portal_balance_cmd, db_path),
        },
    ]
    if include_audit:
        plan.append(
            {
                "name": "salad-orch-v2-audit",
                "heartbeat": "fleet_audit",
                "cmd": _with_db(
                    [
                        "python3",
                        str(SCRIPT_DIR / "fleet_audit.py"),
                        "--loop",
                        "--interval",
                        "300",
                        "--balance-interval",
                        "3600",
                        "--balance-file",
                        "state/salad_balances.json",
                    ],
                    db_path,
                ),
            }
        )
    if include_spike_report:
        plan.append(
            {
                "name": "salad-orch-v2-spike-report",
                "heartbeat": "spike_report",
                "cmd": _with_db(
                    [
                        "python3",
                        str(SCRIPT_DIR / "spike_report.py"),
                        "--heartbeat",
                        "--loop",
                        "--interval",
                        "300",
                    ],
                    db_path,
                ),
            }
        )
    if include_maintenance:
        cmd = ["python3", str(SCRIPT_DIR / "maintenance.py"), "--loop", "--interval", "21600"]
        if maintenance_apply:
            cmd.append("--apply")
        plan.append(
            {
                "name": "salad-orch-v2-maintenance",
                "heartbeat": "maintenance",
                "cmd": _with_db(cmd, db_path),
            }
        )
    if include_runtime_monitor:
        cmd = [
            "python3",
            str(SCRIPT_DIR / "runtime_monitor.py"),
            "--loop",
            "--interval",
            os.environ.get("PRL_RUNTIME_MONITOR_INTERVAL_SECONDS", "35"),
            "--runner-timeout-seconds",
            os.environ.get("PRL_RUNTIME_MONITOR_RUNNER_TIMEOUT_SECONDS", "420"),
            "--fee",
            os.environ.get("PRL_PEARL_FEE_RATE", "0.01"),
            "--guard-on-issues-every",
            os.environ.get("PRL_RUNTIME_MONITOR_GUARD_ON_ISSUES_EVERY", "1"),
            "--guard-actionable-only",
            "--pending-retarget-after-seconds",
            os.environ.get("PRL_RUNTIME_MONITOR_PENDING_RETARGET_SECONDS", "900"),
            "--pending-status-retarget-after-seconds",
            os.environ.get("PRL_RUNTIME_MONITOR_PENDING_STATUS_SECONDS", "900"),
            "--worker-parallelism",
            os.environ.get("PRL_RUNTIME_MONITOR_WORKER_PARALLELISM", "25"),
            "--skip-shadow-workers",
        ]
        if runtime_monitor_apply:
            cmd.extend(["--require-secrets", "--apply-all-orgs-pending", "--confirm-live-actions"])
        plan.append(
            {
                "name": "salad-orch-v2-monitor",
                "heartbeat": "runtime_monitor",
                "cmd": _with_db(cmd, db_path),
            }
        )
    if include_workers or apply_workers:
        for org in config.enabled_orgs():
            cmd = ["python3", str(SCRIPT_DIR / "org_worker.py"), "--org", org.label, "--loop", "--interval", "30"]
            if apply_workers:
                cmd.append("--apply")
            plan.append(
                {
                    "name": f"salad-orch-v2-worker-{org.label}",
                    "heartbeat": f"org_worker:{org.label}",
                    "cmd": _with_db(cmd, db_path),
                }
            )
    return plan


def tmux_command(session: str, cmd: list[str]) -> list[str]:
    joined = (
        "cd "
        + shlex.quote(str(REPO_ROOT))
        + " && if [ -f .env ]; then set -a; . ./.env; set +a; fi && "
        + "unset PRL_ENABLED_ORGS && "
        + "export SALAD_FLEET_CONFIG_PATH=${SALAD_FLEET_CONFIG_PATH:-config/fleet.current.json} && "
        + "export PRL_AVAILABILITY_ZERO_BALANCE_CREDIT_PROBE=${PRL_AVAILABILITY_ZERO_BALANCE_CREDIT_PROBE:-1} && "
        + "export PRL_AVAILABILITY_ZERO_BALANCE_CREDIT_PROBE_COOLDOWN_SECONDS=${PRL_AVAILABILITY_ZERO_BALANCE_CREDIT_PROBE_COOLDOWN_SECONDS:-900} && "
        + " ".join(shlex.quote(part) for part in cmd)
    )
    return ["tmux", "new-session", "-d", "-s", session, joined]


def start_tmux_sessions(
    *,
    apply_workers: bool = False,
    runtime_monitor_apply: bool = False,
    db_path: str | None = None,
    include_audit: bool = True,
    include_spike_report: bool = True,
    include_maintenance: bool = False,
    maintenance_apply: bool = False,
    include_runtime_monitor: bool = True,
    include_workers: bool = False,
) -> list[dict[str, Any]]:
    results = []
    for item in process_plan(
        apply_workers=apply_workers,
        runtime_monitor_apply=runtime_monitor_apply,
        db_path=db_path,
        include_audit=include_audit,
        include_spike_report=include_spike_report,
        include_maintenance=include_maintenance,
        maintenance_apply=maintenance_apply,
        include_runtime_monitor=include_runtime_monitor,
        include_workers=include_workers,
    ):
        subprocess.run(["tmux", "has-session", "-t", item["name"]], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "kill-session", "-t", item["name"]], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = subprocess.run(tmux_command(item["name"], item["cmd"]), check=False, capture_output=True, text=True)
        results.append({"name": item["name"], "returncode": result.returncode, "stderr": result.stderr.strip()})
    return results


def tmux_session_exists(session: str) -> bool:
    result = subprocess.run(["tmux", "has-session", "-t", session], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def heartbeat_age_seconds(row: Any) -> float | None:
    if row is None:
        return None
    try:
        at = datetime.fromisoformat(str(row["at_utc"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - at).total_seconds())


def restart_tmux_session(item: dict[str, Any]) -> dict[str, Any]:
    subprocess.run(["tmux", "kill-session", "-t", item["name"]], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = subprocess.run(tmux_command(item["name"], item["cmd"]), check=False, capture_output=True, text=True)
    return {"name": item["name"], "action": "restart", "returncode": result.returncode, "stderr": result.stderr.strip()}


def ensure_tmux_sessions(
    *,
    apply_workers: bool = False,
    runtime_monitor_apply: bool = False,
    db_path: str | None = None,
    include_audit: bool = True,
    include_spike_report: bool = True,
    include_maintenance: bool = False,
    maintenance_apply: bool = False,
    include_runtime_monitor: bool = True,
    include_workers: bool = False,
    restart_stale: bool = True,
) -> list[dict[str, Any]]:
    plan = process_plan(
        apply_workers=apply_workers,
        runtime_monitor_apply=runtime_monitor_apply,
        db_path=db_path,
        include_audit=include_audit,
        include_spike_report=include_spike_report,
        include_maintenance=include_maintenance,
        maintenance_apply=maintenance_apply,
        include_runtime_monitor=include_runtime_monitor,
        include_workers=include_workers,
    )
    results: list[dict[str, Any]] = []
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        for item in plan:
            exists = tmux_session_exists(str(item["name"]))
            row = conn.execute("SELECT * FROM heartbeats WHERE process_name = ?", (item["heartbeat"],)).fetchone()
            age = heartbeat_age_seconds(row)
            stale_after = int(row["stale_after_seconds"]) if row is not None else None
            stale = bool(age is not None and stale_after is not None and age > stale_after)
            if not exists:
                result = restart_tmux_session(item)
                result["reason"] = "missing_tmux_session"
            elif restart_stale and stale:
                result = restart_tmux_session(item)
                result["reason"] = f"stale_heartbeat:{age:.1f}s>{stale_after}s"
            else:
                result = {
                    "name": item["name"],
                    "action": "ok",
                    "exists": exists,
                    "heartbeat": item["heartbeat"],
                    "age_seconds": round(age, 1) if age is not None else None,
                    "stale_after_seconds": stale_after,
                }
            state_db.record_event(
                conn,
                "supervisor_ensure_process",
                source="supervisor",
                message=f"supervisor ensure {item['name']}",
                payload=result,
            )
            results.append(result)
        state_db.write_heartbeat(conn, "supervisor", payload={"ensured": len(results)})
        conn.commit()
    return results


def heartbeat_report(db_path: str | None = None) -> dict[str, Any]:
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        status = state_db.status_payload(conn)
        state_db.write_heartbeat(conn, "supervisor", payload={"heartbeats": len(status.get("heartbeats") or [])})
        conn.commit()
    return status


def run_control_tick(db_path: str | None = None) -> dict[str, Any]:
    price = price_oracle.run_once(db_path)
    schedule = fleet_scheduler.schedule_once(db_path=db_path)
    health = heartbeat_report(db_path)
    return {"price": price["risk"], "schedule": {k: v for k, v in schedule.items() if k != "targets"}, "health": health}


def main() -> None:
    parser = argparse.ArgumentParser(description="Supervisor for Salad PRL fleet scheduler processes.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--once", action="store_true", help="Run one price+scheduler control tick.")
    parser.add_argument("--loop", action="store_true", help="Run price+scheduler ticks in this process.")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--print-plan", action="store_true")
    parser.add_argument("--start-tmux", action="store_true")
    parser.add_argument("--ensure", action="store_true", help="Start missing tmux sessions and restart stale heartbeats.")
    parser.add_argument("--no-restart-stale", action="store_true", help="Only start missing sessions during --ensure.")
    parser.add_argument("--apply-workers", action="store_true", help="Include --apply for org workers when starting tmux.")
    parser.add_argument("--runtime-monitor-apply", action="store_true", help="Let runtime_monitor.py run confirmed all-org pending fill actions.")
    parser.add_argument("--no-audit", action="store_true", help="Do not include fleet_audit.py in tmux process plans.")
    parser.add_argument("--no-spike-report", action="store_true", help="Do not include spike_report.py in tmux process plans.")
    parser.add_argument("--no-runtime-monitor", action="store_true", help="Do not include runtime_monitor.py in tmux process plans.")
    parser.add_argument("--include-workers", action="store_true", help="Include read-only per-org worker loops in the tmux process plan.")
    parser.add_argument("--include-maintenance", action="store_true", help="Include maintenance.py loop in the tmux plan.")
    parser.add_argument("--maintenance-apply", action="store_true", help="Let maintenance.py delete old historical rows.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.print_plan:
        payload = process_plan(
            apply_workers=args.apply_workers,
            runtime_monitor_apply=args.runtime_monitor_apply,
            db_path=args.db,
            include_audit=not args.no_audit,
            include_spike_report=not args.no_spike_report,
            include_maintenance=args.include_maintenance,
            maintenance_apply=args.maintenance_apply,
            include_runtime_monitor=not args.no_runtime_monitor,
            include_workers=args.include_workers,
        )
        print(json_dumps(payload) if args.json else "\n".join(f"{item['name']}: {' '.join(item['cmd'])}" for item in payload))
        return
    if args.start_tmux:
        payload = start_tmux_sessions(
            apply_workers=args.apply_workers,
            runtime_monitor_apply=args.runtime_monitor_apply,
            db_path=args.db,
            include_audit=not args.no_audit,
            include_spike_report=not args.no_spike_report,
            include_maintenance=args.include_maintenance,
            maintenance_apply=args.maintenance_apply,
            include_runtime_monitor=not args.no_runtime_monitor,
            include_workers=args.include_workers,
        )
        print(json_dumps(payload) if args.json else "\n".join(f"{item['name']}: rc={item['returncode']}" for item in payload))
        return
    if args.ensure:
        payload = ensure_tmux_sessions(
            apply_workers=args.apply_workers,
            runtime_monitor_apply=args.runtime_monitor_apply,
            db_path=args.db,
            include_audit=not args.no_audit,
            include_spike_report=not args.no_spike_report,
            include_maintenance=args.include_maintenance,
            maintenance_apply=args.maintenance_apply,
            include_runtime_monitor=not args.no_runtime_monitor,
            include_workers=args.include_workers,
            restart_stale=not args.no_restart_stale,
        )
        print(json_dumps(payload) if args.json else "\n".join(f"{item['name']}: {item['action']}" for item in payload))
        return
    if args.loop:
        while True:
            payload = run_control_tick(args.db)
            print(json_dumps(payload) if args.json else f"tick mode={payload['price']['mode']} targets={payload['schedule']['assigned_targets']}")
            time.sleep(args.interval)
        return

    payload = run_control_tick(args.db) if args.once else heartbeat_report(args.db)
    print(json_dumps(payload) if args.json else "supervisor ok")


if __name__ == "__main__":
    main()
