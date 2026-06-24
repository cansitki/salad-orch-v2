#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from typing import Any

import profile_scorer
import state_db
from config_loader import FleetConfig, load_config
from fleet_common import json_dumps, utc_now


def _scheduler_mode(config: FleetConfig, db_mode: str | None) -> str:
    if db_mode:
        return db_mode
    if config.risk.fleet_mode == "optimize":
        return "optimize"
    return "base_fill"


def _top_eligible_profiles(scores: list[dict[str, Any]], *, width: int) -> list[dict[str, Any]]:
    eligible = [row for row in scores if row.get("eligible")]
    eligible.sort(key=lambda item: (float(item["score"]), float(item["expected_profit_day"])), reverse=True)
    return eligible[: max(1, min(width, len(eligible)))]


def build_targets(
    config: FleetConfig,
    scores: list[dict[str, Any]],
    *,
    mode: str,
    decision_price_usd: float,
    width: int = 10,
) -> list[dict[str, Any]]:
    profiles = _top_eligible_profiles(scores, width=width)
    if not profiles:
        return []
    targets: list[dict[str, Any]] = []
    assigned_at = utc_now()
    enabled_orgs = config.enabled_orgs()
    for org_index, org in enumerate(enabled_orgs):
        for slot_index, slot_name in enumerate(org.slot_names(), start=1):
            profile_index = (slot_index - 1 + org_index * 3) % len(profiles)
            profile = profiles[profile_index]
            targets.append(
                {
                    "org_label": org.label,
                    "slot_name": slot_name,
                    "slot_index": slot_index,
                    "profile_key": profile["profile_key"],
                    "gpu_key": profile["gpu_key"],
                    "priority": profile["priority"],
                    "memory_mb": profile["memory_mb"],
                    "mode": mode,
                    "decision_price_usd": decision_price_usd,
                    "expected_profit_day": float(profile["expected_profit_day"]),
                    "protected": False,
                    "reason": f"{mode}:diversified_rank_{profile_index + 1}_of_{len(profiles)}",
                    "assigned_at_utc": assigned_at,
                }
            )
    return targets


def schedule_once(
    *,
    db_path: str | None = None,
    mode: str | None = None,
    price: float | None = None,
    fee: float | None = None,
    gross_prl_per_th_day: float | None = None,
    dry_run: bool = False,
    width: int = 10,
) -> dict[str, Any]:
    config = load_config()
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.sync_config(conn, config)
        risk = state_db.latest_risk_mode(conn)
        db_mode = str(risk["mode"]) if risk else None
        db_price = float(risk["decision_price_usd"]) if risk else config.risk.decision_price_for_mode()
        selected_mode = _scheduler_mode(config, mode or db_mode)
        decision_price = price if price is not None else db_price
        selected_fee = fee if fee is not None else (float(risk["pearl_fee_rate"]) if risk else config.risk.effective_fee_rate())

    scores = profile_scorer.score_profiles(
        db_path=db_path,
        mode=selected_mode,
        decision_price_usd=decision_price,
        gross_prl_per_th_day=gross_prl_per_th_day,
        pearl_fee_rate=selected_fee,
        write=not dry_run,
    )
    targets = build_targets(
        config,
        scores,
        mode=selected_mode,
        decision_price_usd=decision_price,
        width=width,
    )

    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        if not dry_run:
            for target in targets:
                state_db.set_slot_target(conn, target)
            state_db.write_heartbeat(
                conn,
                "fleet_scheduler",
                payload={
                    "mode": selected_mode,
                    "decision_price_usd": decision_price,
                    "targets": len(targets),
                    "target_slots": config.target_slot_count(),
                },
            )
            state_db.record_event(
                conn,
                "slot_targets_assigned",
                source="fleet_scheduler",
                message="central scheduler assigned slot targets",
                payload={
                    "mode": selected_mode,
                    "targets": len(targets),
                    "profiles": sorted({target["profile_key"] for target in targets}),
                    "dry_run": dry_run,
                },
            )
            conn.commit()

    profile_counts: dict[str, int] = {}
    for target in targets:
        profile_counts[target["profile_key"]] = profile_counts.get(target["profile_key"], 0) + 1
    return {
        "mode": selected_mode,
        "decision_price_usd": decision_price,
        "pearl_fee_rate": selected_fee,
        "target_slots": config.target_slot_count(),
        "assigned_targets": len(targets),
        "dry_run": dry_run,
        "profile_counts": profile_counts,
        "targets": targets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Central deterministic scheduler for Salad PRL fleet targets.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mode", default=None)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--fee", type=float, default=None)
    parser.add_argument("--gross-prl-per-th-day", type=float, default=None)
    parser.add_argument("--width", type=int, default=10, help="Number of top profiles to diversify across.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    def run_and_print() -> None:
        payload = schedule_once(
            db_path=args.db,
            mode=args.mode,
            price=args.price,
            fee=args.fee,
            gross_prl_per_th_day=args.gross_prl_per_th_day,
            dry_run=args.dry_run,
            width=args.width,
        )
        if args.json:
            print(json_dumps(payload))
            return
        print(
            f"mode={payload['mode']} targets={payload['assigned_targets']}/{payload['target_slots']} "
            f"price={float(payload['decision_price_usd']):.4f} fee={float(payload['pearl_fee_rate']):.2%}"
        )
        for profile_key, count in sorted(payload["profile_counts"].items(), key=lambda item: item[1], reverse=True):
            print(f"{count:>3} {profile_key}")

    if args.loop:
        while True:
            run_and_print()
            time.sleep(args.interval)
    else:
        run_and_print()


if __name__ == "__main__":
    main()
