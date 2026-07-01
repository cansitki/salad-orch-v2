#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

import profit_model
import state_db
from config_loader import load_config
from fleet_common import env_bool, env_float, env_int, json_dumps, utc_now


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


def _parse_payload(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _seconds_between(start: Any, end: Any) -> float | None:
    start_dt = _parse_utc(start)
    end_dt = _parse_utc(end)
    if start_dt is None or end_dt is None or end_dt < start_dt:
        return None
    return (end_dt - start_dt).total_seconds()


def _snapshot_profile_key(row: Any, payload: dict[str, Any]) -> str | None:
    if row["profile_key"]:
        return str(row["profile_key"])
    return profit_model.observed_profile_key(payload.get("gpu"), payload.get("priority"))


def profile_runtime_stats(conn) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    lookback_hours = max(1.0, env_float("PRL_PROFILE_RUNTIME_LOOKBACK_HOURS", 24.0))
    lookback_window = f"-{lookback_hours:g} hours"
    rows = conn.execute(
        """
        SELECT *
        FROM profit_snapshots
        WHERE scope = 'slot'
          AND julianday(at_utc) >= julianday('now', ?)
        ORDER BY at_utc ASC, id ASC
        """,
        (lookback_window,),
    ).fetchall()
    live_snapshots: list[dict[str, Any]] = []
    for row in rows:
        payload = _parse_payload(row)
        profile_key = _snapshot_profile_key(row, payload)
        if not profile_key:
            continue
        item = stats.setdefault(
            profile_key,
            {
                "profit_samples": 0,
                "live_hash_samples": 0,
                "no_hash_samples": 0,
                "negative_samples": 0,
                "th_total": 0.0,
                "live_th_total": 0.0,
                "profit_total": 0.0,
                "time_to_hash_samples": 0,
                "time_to_hash_total_seconds": 0.0,
            },
        )
        th = float(row["th"] or 0)
        profit_day = float(row["profit_day"] or 0)
        item["profit_samples"] += 1
        item["th_total"] += th
        item["profit_total"] += profit_day
        if th > 0:
            item["live_hash_samples"] += 1
            item["live_th_total"] += th
            live_snapshots.append(
                {
                    "org_label": row["org_label"],
                    "slot_name": row["slot_name"],
                    "profile_key": profile_key,
                    "at_utc": row["at_utc"],
                }
            )
        else:
            item["no_hash_samples"] += 1
        if profit_day < 0:
            item["negative_samples"] += 1

    attempts = conn.execute(
        """
        SELECT at_utc, org_label, slot_name, profile_key, action
        FROM attempts
        WHERE profile_key IS NOT NULL
          AND ok = 1
          AND julianday(at_utc) >= julianday('now', ?)
          AND action NOT LIKE 'dry_run_%'
          AND action IN ('create', 'patch', 'start', 'guard_retarget')
        ORDER BY at_utc ASC
        """,
        (lookback_window,),
    ).fetchall()
    for attempt in attempts:
        attempt_profile = str(attempt["profile_key"])
        first_live = next(
            (
                snapshot
                for snapshot in live_snapshots
                if snapshot["org_label"] == attempt["org_label"]
                and snapshot["slot_name"] == attempt["slot_name"]
                and snapshot["profile_key"] == attempt_profile
                and _seconds_between(attempt["at_utc"], snapshot["at_utc"]) is not None
            ),
            None,
        )
        if not first_live:
            continue
        seconds = _seconds_between(attempt["at_utc"], first_live["at_utc"])
        if seconds is None:
            continue
        item = stats.setdefault(
            attempt_profile,
            {
                "profit_samples": 0,
                "live_hash_samples": 0,
                "no_hash_samples": 0,
                "negative_samples": 0,
                "th_total": 0.0,
                "live_th_total": 0.0,
                "profit_total": 0.0,
                "time_to_hash_samples": 0,
                "time_to_hash_total_seconds": 0.0,
            },
        )
        item["time_to_hash_samples"] += 1
        item["time_to_hash_total_seconds"] += seconds
    return stats


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
        runtime_stats = profile_runtime_stats(conn)
        spike_summary = state_db.recent_spike_summary(conn, limit=200)
        spike_stats = {str(row["profile_key"]): row for row in spike_summary.get("profiles") or []}
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
        use_observed_th = env_bool("PRL_SCORER_USE_OBSERVED_TH", False)
        observed_min_live_samples = max(1, env_int("PRL_SCORER_OBSERVED_TH_MIN_LIVE_SAMPLES", 20))
        observed_max_multiplier = env_float("PRL_SCORER_OBSERVED_TH_MAX_MULTIPLIER", 2.0)
        observed_min_multiplier = env_float("PRL_SCORER_OBSERVED_TH_MIN_MULTIPLIER", 0.0)

        rows: list[dict[str, Any]] = []
        for profile in profiles:
            runtime = runtime_stats.get(profile.profile_key, {})
            live_hash_samples = float(runtime.get("live_hash_samples", 0))
            avg_observed_live_th = (
                float(runtime.get("live_th_total", 0)) / live_hash_samples if live_hash_samples else 0.0
            )
            effective_expected_th = float(profile.expected_th)
            expected_th_source = "static"
            if (
                use_observed_th
                and live_hash_samples >= observed_min_live_samples
                and avg_observed_live_th > 0
            ):
                observed_th = avg_observed_live_th
                if observed_max_multiplier > 0:
                    observed_th = min(observed_th, profile.expected_th * observed_max_multiplier)
                if observed_min_multiplier > 0:
                    observed_th = max(observed_th, profile.expected_th * observed_min_multiplier)
                effective_expected_th = observed_th
                expected_th_source = "observed_live_avg"
            estimate_profile = replace(profile, expected_th=effective_expected_th)
            estimate = profit_model.expected_profit(
                estimate_profile,
                decision_price_usd=decision_price,
                gross_prl_per_th_day=gross,
                pearl_fee_rate=fee,
                min_profit_day=min_profit,
            )
            stats = attempt_stats.get(profile.profile_key, {})
            success = float(stats.get("success", 0))
            failure = float(stats.get("failure", 0))
            spikes = spike_stats.get(profile.profile_key, {})
            attempt_no_hash = float(stats.get("no_hash", 0))
            runtime_no_hash = float(runtime.get("no_hash_samples", 0))
            no_hash = attempt_no_hash + runtime_no_hash
            negative = float(runtime.get("negative_samples", 0))
            recent_spikes_30m = float(spikes.get("spikes_30m") or 0)
            recent_spikes_60m = float(spikes.get("spikes_60m") or 0)
            recent_affected_slots_60m = float(spikes.get("affected_slots_60m") or 0)
            recent_spike_unstable = bool(spikes.get("unstable"))
            spike_penalty = (
                recent_spikes_30m * env_float("PRL_SPIKE_SCORE_PENALTY_30M", 14.0)
                + recent_spikes_60m * env_float("PRL_SPIKE_SCORE_PENALTY_60M", 4.0)
                + recent_affected_slots_60m * env_float("PRL_SPIKE_SCORE_PENALTY_AFFECTED_SLOT", 8.0)
            )
            if recent_spike_unstable:
                spike_penalty += env_float("PRL_SPIKE_SCORE_UNSTABLE_PENALTY", 80.0)
            capacity_failure = float(stats.get("capacity_failure", 0))
            profit_samples = float(runtime.get("profit_samples", 0))
            avg_observed_th = avg_observed_live_th
            live_hash_sample_rate = live_hash_samples / profit_samples if profit_samples else None
            no_hash_sample_rate = float(runtime.get("no_hash_samples", 0)) / profit_samples if profit_samples else 0.0
            negative_sample_rate = negative / profit_samples if profit_samples else 0.0
            time_to_hash_samples = float(runtime.get("time_to_hash_samples", 0))
            avg_time_to_hash_seconds = (
                float(runtime.get("time_to_hash_total_seconds", 0)) / time_to_hash_samples
                if time_to_hash_samples
                else None
            )
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
                estimate_profile,
                base_price_usd=config.risk.base_decision_price,
                boost_price_usd=max(decision_price, config.risk.base_decision_price),
                gross_prl_per_th_day=gross,
                pearl_fee_rate=fee,
                min_profit_day=min_profit,
            )
            if recent_spike_unstable and env_bool("PRL_SPIKE_BLOCK_UNSTABLE_PROFILES", True):
                tier = "unstable_recent_spikes"

            score = estimate.profit_day * 100
            score += success_rate * 20
            score += effective_expected_th * 0.03
            score += availability_weight
            if live_hash_sample_rate is not None:
                score += live_hash_sample_rate * 15
            if avg_time_to_hash_seconds is not None:
                score += max(0.0, 12.0 - (avg_time_to_hash_seconds / 60.0))
            score -= failure * 2.0
            score -= attempt_no_hash * 8.0
            score -= no_hash_sample_rate * 40.0
            score -= negative_sample_rate * 45.0
            score -= spike_penalty
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
            if tier == "unstable_recent_spikes":
                score -= 1000

            reason = {
                "profit_day": round(estimate.profit_day, 6),
                "success_rate": round(success_rate, 4),
                "success": success,
                "failure": failure,
                "no_hash": no_hash,
                "attempt_no_hash": attempt_no_hash,
                "runtime_no_hash": runtime_no_hash,
                "negative": negative,
                "recent_spikes_30m": recent_spikes_30m,
                "recent_spikes_60m": recent_spikes_60m,
                "recent_spike_affected_slots_60m": recent_affected_slots_60m,
                "recent_spike_unstable": recent_spike_unstable,
                "recent_spike_penalty": round(spike_penalty, 4),
                "recent_spike_worst_profit_day_60m": spikes.get("worst_profit_day_60m"),
                "capacity_failure": capacity_failure,
                "profit_samples": profit_samples,
                "live_hash_samples": live_hash_samples,
                "avg_observed_th": round(avg_observed_th, 4),
                "expected_th_source": expected_th_source,
                "static_expected_th": round(float(profile.expected_th), 4),
                "effective_expected_th": round(effective_expected_th, 4),
                "live_hash_sample_rate": round(live_hash_sample_rate, 4) if live_hash_sample_rate is not None else None,
                "no_hash_sample_rate": round(no_hash_sample_rate, 4),
                "negative_sample_rate": round(negative_sample_rate, 4),
                "time_to_hash_samples": time_to_hash_samples,
                "avg_time_to_hash_seconds": (
                    round(avg_time_to_hash_seconds, 1) if avg_time_to_hash_seconds is not None else None
                ),
                "availability_known": availability_known,
                "availability_total": availability_total,
                "availability_weight": availability_weight,
                "priority_allowed": priority_allowed,
                "min_profit_day": min_profit,
                "allowed_priorities": list(allowed),
            }
            row = {
                **asdict(estimate_profile),
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
                "eligible": (
                    priority_allowed
                    and estimate.profit_day >= min_profit
                    and tier != "unstable_recent_spikes"
                    and (selected_mode != "risk_off" or tier == "safe_base")
                ),
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
