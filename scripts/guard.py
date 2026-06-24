#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
from typing import Any

import salad_prl_profit_snapshot as snapshot
import state_db
from config_loader import load_config
from fleet_common import json_dumps, utc_now


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
LEGACY_GUARD = SCRIPT_DIR / "salad_prl_guard.py"


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


def run_once(*, db_path: str | None = None, price: float | None = None) -> dict[str, Any]:
    config = load_config()
    decision_price = price or config.risk.decision_price_for_mode()
    payload = snapshot.build_snapshot(decision_price)
    analysis = analyze_snapshot(payload)
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.record_profit_snapshot(
            conn,
            {
                "at_utc": utc_now(),
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
        for row in payload.get("slots") or []:
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": utc_now(),
                    "scope": "slot",
                    "org_label": row.get("org"),
                    "slot_name": row.get("slot"),
                    "decision_price_usd": decision_price,
                    "live_price_usd": payload.get("live_market_prl_price"),
                    "th": row.get("th"),
                    "cost_day": row.get("cost_day"),
                    "profit_day": row.get("profit_day"),
                    "payload": row,
                },
            )
        state_db.write_heartbeat(conn, "guard", payload={"issue_count": analysis["issue_count"]})
        state_db.record_event(
            conn,
            "guard_snapshot_analyzed",
            source="guard",
            level="warning" if analysis["issue_count"] else "info",
            message="guard analyzed current profit/no-hash state",
            payload=analysis,
        )
        conn.commit()
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
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.apply_legacy:
        exec_legacy_guard()
    def run_and_print() -> None:
        payload = run_once(db_path=args.db, price=args.price)
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
