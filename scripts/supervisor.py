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


def process_plan(
    *,
    apply_workers: bool = False,
    db_path: str | None = None,
    include_audit: bool = True,
    include_maintenance: bool = False,
    maintenance_apply: bool = False,
) -> list[dict[str, Any]]:
    config = load_config()
    if os.environ.get("SALAD_PORTAL_BALANCE_ACCOUNTS_JSON") or os.environ.get("SALAD_PORTAL_BALANCE_EMAILS"):
        portal_balance_cmd = [
            "python3",
            str(SCRIPT_DIR / "portal_multi_balances.py"),
            "--loop",
            "--interval",
            "900",
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
            "900",
            "--balance-file",
            "state/salad_balances.json",
            "--cwd",
            str(REPO_ROOT),
            "--cookie-jar",
            "state/portal_cookies.txt",
        ]

    plan = [
        {
            "name": "salad-price-oracle",
            "heartbeat": "price_oracle",
            "cmd": _with_db(["python3", str(SCRIPT_DIR / "price_oracle.py"), "--loop", "--interval", "60"], db_path),
        },
        {
            "name": "salad-availability-probe",
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
                    "2",
                    "--profile-parallelism",
                    "4",
                ],
                db_path,
            ),
        },
        {
            "name": "salad-fleet-scheduler",
            "heartbeat": "fleet_scheduler",
            "cmd": _with_db(["python3", str(SCRIPT_DIR / "fleet_scheduler.py"), "--loop", "--interval", "60"], db_path),
        },
        {
            "name": "salad-guard-shadow",
            "heartbeat": "guard",
            "cmd": _with_db(["python3", str(SCRIPT_DIR / "guard.py"), "--loop", "--interval", "30"], db_path),
        },
        {
            "name": "salad-portal-balances",
            "heartbeat": "portal_balances",
            "cmd": _with_db(portal_balance_cmd, db_path),
        },
    ]
    if include_audit:
        plan.append(
            {
                "name": "salad-fleet-audit",
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
    if include_maintenance:
        cmd = ["python3", str(SCRIPT_DIR / "maintenance.py"), "--loop", "--interval", "21600"]
        if maintenance_apply:
            cmd.append("--apply")
        plan.append(
            {
                "name": "salad-maintenance",
                "heartbeat": "maintenance",
                "cmd": _with_db(cmd, db_path),
            }
        )
    for org in config.enabled_orgs():
        cmd = ["python3", str(SCRIPT_DIR / "org_worker.py"), "--org", org.label, "--loop", "--interval", "30"]
        if apply_workers:
            cmd.append("--apply")
        plan.append(
            {
                "name": f"salad-org-worker-{org.label}",
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
        + " ".join(shlex.quote(part) for part in cmd)
    )
    return ["tmux", "new-session", "-d", "-s", session, joined]


def start_tmux_sessions(
    *,
    apply_workers: bool = False,
    db_path: str | None = None,
    include_audit: bool = True,
    include_maintenance: bool = False,
    maintenance_apply: bool = False,
) -> list[dict[str, Any]]:
    results = []
    for item in process_plan(
        apply_workers=apply_workers,
        db_path=db_path,
        include_audit=include_audit,
        include_maintenance=include_maintenance,
        maintenance_apply=maintenance_apply,
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
    db_path: str | None = None,
    include_audit: bool = True,
    include_maintenance: bool = False,
    maintenance_apply: bool = False,
    restart_stale: bool = True,
) -> list[dict[str, Any]]:
    plan = process_plan(
        apply_workers=apply_workers,
        db_path=db_path,
        include_audit=include_audit,
        include_maintenance=include_maintenance,
        maintenance_apply=maintenance_apply,
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
    parser.add_argument("--no-audit", action="store_true", help="Do not include fleet_audit.py in tmux process plans.")
    parser.add_argument("--include-maintenance", action="store_true", help="Include maintenance.py loop in the tmux plan.")
    parser.add_argument("--maintenance-apply", action="store_true", help="Let maintenance.py delete old historical rows.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.print_plan:
        payload = process_plan(
            apply_workers=args.apply_workers,
            db_path=args.db,
            include_audit=not args.no_audit,
            include_maintenance=args.include_maintenance,
            maintenance_apply=args.maintenance_apply,
        )
        print(json_dumps(payload) if args.json else "\n".join(f"{item['name']}: {' '.join(item['cmd'])}" for item in payload))
        return
    if args.start_tmux:
        payload = start_tmux_sessions(
            apply_workers=args.apply_workers,
            db_path=args.db,
            include_audit=not args.no_audit,
            include_maintenance=args.include_maintenance,
            maintenance_apply=args.maintenance_apply,
        )
        print(json_dumps(payload) if args.json else "\n".join(f"{item['name']}: rc={item['returncode']}" for item in payload))
        return
    if args.ensure:
        payload = ensure_tmux_sessions(
            apply_workers=args.apply_workers,
            db_path=args.db,
            include_audit=not args.no_audit,
            include_maintenance=args.include_maintenance,
            maintenance_apply=args.maintenance_apply,
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
