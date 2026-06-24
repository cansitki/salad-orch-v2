#!/usr/bin/env python3
from __future__ import annotations

import argparse
from typing import Any

import state_db
from config_loader import load_config
from fleet_common import json_dumps


def build_report(db_path: str | None = None) -> dict[str, Any]:
    config = load_config()
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        status = state_db.status_payload(conn)
        targets = [
            dict(row)
            for row in conn.execute(
                """
                SELECT org_label, slot_name, profile_key, mode, decision_price_usd,
                       expected_profit_day, reason, assigned_at_utc
                FROM slot_targets
                ORDER BY org_label, slot_name
                """
            ).fetchall()
        ]
        scores = [
            dict(row)
            for row in conn.execute(
                """
                SELECT profile_key, mode, decision_price_usd, expected_profit_day,
                       score, risk_tier, scored_at_utc
                FROM profile_scores
                ORDER BY score DESC
                LIMIT 15
                """
            ).fetchall()
        ]
        latest_profit = conn.execute(
            """
            SELECT *
            FROM profit_snapshots
            WHERE scope = 'fleet'
            ORDER BY at_utc DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    profile_counts: dict[str, int] = {}
    for target in targets:
        profile_counts[str(target["profile_key"])] = profile_counts.get(str(target["profile_key"]), 0) + 1
    return {
        "target_slots": config.target_slot_count(),
        "assigned_targets": len(targets),
        "profile_counts": profile_counts,
        "latest_profit": dict(latest_profit) if latest_profit else None,
        "status": status,
        "top_scores": scores,
        "targets": targets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Operator report for the Salad PRL fleet scheduler.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(args.db)
    if args.json:
        print(json_dumps(report))
        return
    print(f"targets={report['assigned_targets']}/{report['target_slots']}")
    latest = report.get("latest_profit")
    if latest:
        print(
            f"latest_profit=${float(latest.get('profit_day') or 0):.3f}/day "
            f"th={float(latest.get('th') or 0):.3f} cost=${float(latest.get('cost_day') or 0):.3f}/day"
        )
    print("profile targets:")
    for profile_key, count in sorted(report["profile_counts"].items(), key=lambda item: item[1], reverse=True):
        print(f"  {count:>3} {profile_key}")
    print("top scores:")
    for score in report["top_scores"][:8]:
        print(
            f"  {float(score['score']):>8.2f} {score['profile_key']:<22} "
            f"profit=${float(score['expected_profit_day']):>7.3f}/day tier={score['risk_tier']}"
        )


if __name__ == "__main__":
    main()
