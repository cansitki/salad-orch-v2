#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import shlex
import subprocess
import time
from typing import Any

import fleet_scheduler
import price_oracle
import state_db
from config_loader import load_config
from fleet_common import REPO_ROOT, json_dumps


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent


def process_plan(*, apply_workers: bool = False) -> list[dict[str, Any]]:
    config = load_config()
    plan = [
        {
            "name": "salad-price-oracle",
            "cmd": ["python3", str(SCRIPT_DIR / "price_oracle.py"), "--loop", "--interval", "60"],
        },
        {
            "name": "salad-fleet-scheduler",
            "cmd": ["python3", str(SCRIPT_DIR / "fleet_scheduler.py"), "--loop", "--interval", "60"],
        },
        {
            "name": "salad-guard-shadow",
            "cmd": ["python3", str(SCRIPT_DIR / "guard.py"), "--loop", "--interval", "30"],
        },
    ]
    for org in config.enabled_orgs():
        cmd = ["python3", str(SCRIPT_DIR / "org_worker.py"), "--org", org.label, "--loop", "--interval", "30"]
        if apply_workers:
            cmd.append("--apply")
        plan.append({"name": f"salad-org-worker-{org.label}", "cmd": cmd})
    return plan


def tmux_command(session: str, cmd: list[str]) -> list[str]:
    joined = "cd " + shlex.quote(str(REPO_ROOT)) + " && " + " ".join(shlex.quote(part) for part in cmd)
    return ["tmux", "new-session", "-d", "-s", session, joined]


def start_tmux_sessions(*, apply_workers: bool = False) -> list[dict[str, Any]]:
    results = []
    for item in process_plan(apply_workers=apply_workers):
        subprocess.run(["tmux", "has-session", "-t", item["name"]], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "kill-session", "-t", item["name"]], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = subprocess.run(tmux_command(item["name"], item["cmd"]), check=False, capture_output=True, text=True)
        results.append({"name": item["name"], "returncode": result.returncode, "stderr": result.stderr.strip()})
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
    parser.add_argument("--apply-workers", action="store_true", help="Include --apply for org workers when starting tmux.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.print_plan:
        payload = process_plan(apply_workers=args.apply_workers)
        print(json_dumps(payload) if args.json else "\n".join(f"{item['name']}: {' '.join(item['cmd'])}" for item in payload))
        return
    if args.start_tmux:
        payload = start_tmux_sessions(apply_workers=args.apply_workers)
        print(json_dumps(payload) if args.json else "\n".join(f"{item['name']}: rc={item['returncode']}" for item in payload))
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
