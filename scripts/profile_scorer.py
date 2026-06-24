#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
from typing import Any

import profit_model
import state_db
from config_loader import load_config
from fleet_common import json_dumps, utc_now


def _latest_float(row: Any, key: str, fallback: float) -> float:
    if row is None:
        return fallback
    value = row[key]
    return float(value) if value is not None else fallback


def _mode_from_db(conn, fallback: str) -> tuple[str, float, float]:
    risk = state_db.latest_risk_mode(conn)
    if risk is None:
        config = load_config()
        return fallback, config.risk.decision_price_for_mode(), config.risk.effective_fee_rate()
    return str(risk["mode"]), float(risk["decision_price_usd"]), float(risk["pearl_fee_rate"])


def score_profiles(
    *,
    db_path: str | None = None,
    mode: str | None = None,
    decision_price_usd: float | None = None,
    gross_prl_per_th_day: float | None = None,
    pearl_fee_rate: float | None = None,
    write: bool = True,
) -> list[dict[str, Any]]:
    config = load_config()
    profiles = profit_model.load_profiles()
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.sync_config(conn, config)
        if write:
            state_db.upsert_gpu_profiles(conn, profiles)
        db_mode, db_price, db_fee = _mode_from_db(conn, config.risk.fleet_mode)
        sample = state_db.latest_price_sample(conn)
        attempt_stats = state_db.attempt_stats(conn)
        availability = state_db.latest_profile_availability(conn)

        selected_mode = mode or db_mode
        decision_price = decision_price_usd if decision_price_usd is not None else db_price
        fee = pearl_fee_rate if pearl_fee_rate is not None else db_fee
        gross = gross_prl_per_th_day or _latest_float(
            sample,
            "gross_prl_per_th_day",
            profit_model.DEFAULT_GROSS_PRL_PER_TH_DAY,
        )
        min_profit = config.risk.min_profit_for_mode("optimize" if selected_mode == "optimize" else "fill")
        allowed = (
            config.risk.boost_allowed_priorities
            if selected_mode in {"boost_fill", "aggressive_boost", "optimize"}
            else config.risk.base_allowed_priorities
        )

        rows: list[dict[str, Any]] = []
        for profile in profiles:
            estimate = profit_model.expected_profit(
                profile,
                decision_price_usd=decision_price,
                gross_prl_per_th_day=gross,
                pearl_fee_rate=fee,
                min_profit_day=min_profit,
            )
            stats = attempt_stats.get(profile.profile_key, {})
            success = float(stats.get("success", 0))
            failure = float(stats.get("failure", 0))
            no_hash = float(stats.get("no_hash", 0))
            capacity_failure = float(stats.get("capacity_failure", 0))
            availability_rows = [
                org_rows[profile.profile_key]
                for org_rows in availability.values()
                if profile.profile_key in org_rows
            ]
            availability_known = bool(availability_rows)
            availability_total = sum(int(row.get("available_count") or 0) for row in availability_rows if row.get("ok"))
            availability_weight = min(30.0, availability_total * 3.0) if availability_known else 0.0
            total = success + failure
            success_rate = success / total if total else 0.5
            tier = profit_model.risk_tier(
                profile,
                base_price_usd=config.risk.base_decision_price,
                boost_price_usd=max(decision_price, config.risk.base_decision_price),
                gross_prl_per_th_day=gross,
                pearl_fee_rate=fee,
                min_profit_day=min_profit,
            )

            score = estimate.profit_day * 100
            score += success_rate * 20
            score += profile.expected_th * 0.03
            score += availability_weight
            score -= failure * 2.0
            score -= no_hash * 8.0
            score -= capacity_failure * 5.0
            if availability_known and availability_total <= 0:
                score -= 100.0

            priority_allowed = profile.priority in allowed
            if not priority_allowed:
                score -= 1000
            if estimate.profit_day < min_profit:
                score -= 500
            if selected_mode == "risk_off" and tier != "safe_base":
                score -= 500

            reason = {
                "profit_day": round(estimate.profit_day, 6),
                "success_rate": round(success_rate, 4),
                "success": success,
                "failure": failure,
                "no_hash": no_hash,
                "capacity_failure": capacity_failure,
                "availability_known": availability_known,
                "availability_total": availability_total,
                "availability_weight": availability_weight,
                "priority_allowed": priority_allowed,
                "min_profit_day": min_profit,
                "allowed_priorities": list(allowed),
            }
            row = {
                **asdict(profile),
                "mode": selected_mode,
                "decision_price_usd": decision_price,
                "gross_prl_per_th_day": gross,
                "pearl_fee_rate": fee,
                "expected_profit_day": estimate.profit_day,
                "cost_day": estimate.cost_day,
                "revenue_day": estimate.revenue_day,
                "break_even_price_usd": estimate.break_even_price_usd,
                "min_safe_price_usd": estimate.min_safe_price_usd,
                "risk_tier": "blocked_priority" if not priority_allowed else tier,
                "score": score,
                "eligible": priority_allowed and estimate.profit_day >= min_profit and (selected_mode != "risk_off" or tier == "safe_base"),
                "reason": reason,
                "scored_at_utc": utc_now(),
            }
            rows.append(row)
            if write:
                state_db.upsert_profile_score(conn, row)

        rows.sort(key=lambda item: (bool(item["eligible"]), float(item["score"])), reverse=True)
        if write:
            state_db.write_heartbeat(
                conn,
                "profile_scorer",
                payload={
                    "mode": selected_mode,
                    "decision_price_usd": decision_price,
                    "eligible_profiles": sum(1 for item in rows if item["eligible"]),
                },
            )
            state_db.record_event(
                conn,
                "profiles_scored",
                source="profile_scorer",
                message="GPU profiles scored",
                payload={"mode": selected_mode, "profiles": len(rows), "eligible": sum(1 for item in rows if item["eligible"])},
            )
            conn.commit()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Score Salad GPU profiles for scheduler target selection.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--mode", default=None)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--gross-prl-per-th-day", type=float, default=None)
    parser.add_argument("--fee", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rows = score_profiles(
        db_path=args.db,
        mode=args.mode,
        decision_price_usd=args.price,
        gross_prl_per_th_day=args.gross_prl_per_th_day,
        pearl_fee_rate=args.fee,
        write=not args.dry_run,
    )
    if args.json:
        print(json_dumps({"profiles": rows}))
        return
    for row in rows:
        mark = "OK" if row["eligible"] else "--"
        print(
            f"{mark} {row['profile_key']:<22} "
            f"score={float(row['score']):>8.2f} "
            f"profit=${float(row['expected_profit_day']):>7.3f}/day "
            f"tier={row['risk_tier']}"
        )


if __name__ == "__main__":
    main()
