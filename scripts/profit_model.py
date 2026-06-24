#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from typing import Any

import salad_prl_watch as watch
from config_loader import load_config
from fleet_common import json_dumps


DEFAULT_GROSS_PRL_PER_TH_DAY = 0.03556


@dataclass(frozen=True)
class Profile:
    profile_key: str
    gpu_key: str
    gpu_id: str
    priority: str
    label: str
    memory_mb: int
    expected_th: float
    static_hourly_usd: float
    enabled: bool = True


@dataclass(frozen=True)
class ProfitEstimate:
    profile_key: str
    gpu_key: str
    priority: str
    memory_mb: int
    decision_price_usd: float
    gross_prl_per_th_day: float
    pearl_fee_rate: float
    expected_th: float
    hourly_usd: float
    revenue_day: float
    cost_day: float
    profit_day: float
    break_even_price_usd: float
    min_safe_price_usd: float
    margin_pct: float


def profile_key(gpu_key: str, priority: str, memory_mb: int) -> str:
    return f"{gpu_key}:{priority}:{memory_mb}"


def _candidate_profiles() -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    candidates = list(watch.FALLBACKS) + list(watch.INITIAL.values())
    for candidate in candidates:
        if len(candidate.gpu_keys) != 1:
            continue
        gpu_key = candidate.gpu_keys[0]
        key = (gpu_key, candidate.priority)
        expected_th = watch.EXPECTED_TH_BY_PROFILE.get(key)
        static_hourly = watch.STATIC_HOURLY_USD_BY_PROFILE.get(key)
        gpu_id = watch.GPU.get(gpu_key)
        if expected_th is None or static_hourly is None or gpu_id is None:
            continue
        pkey = profile_key(gpu_key, candidate.priority, candidate.memory)
        profiles.setdefault(
            pkey,
            Profile(
                profile_key=pkey,
                gpu_key=gpu_key,
                gpu_id=gpu_id,
                priority=candidate.priority,
                label=candidate.label,
                memory_mb=int(candidate.memory),
                expected_th=float(expected_th),
                static_hourly_usd=float(static_hourly),
            ),
        )
    return profiles


def load_profiles() -> list[Profile]:
    profiles = _candidate_profiles()
    for (gpu_key, priority), expected_th in watch.EXPECTED_TH_BY_PROFILE.items():
        static_hourly = watch.STATIC_HOURLY_USD_BY_PROFILE.get((gpu_key, priority))
        gpu_id = watch.GPU.get(gpu_key)
        if static_hourly is None or gpu_id is None:
            continue
        memory = 4096 if gpu_key in {"3070", "3070ti", "4070tis"} and priority == "batch" else 2048
        pkey = profile_key(gpu_key, priority, memory)
        profiles.setdefault(
            pkey,
            Profile(
                profile_key=pkey,
                gpu_key=gpu_key,
                gpu_id=gpu_id,
                priority=priority,
                label=f"RTX {gpu_key.upper()} {priority}",
                memory_mb=memory,
                expected_th=float(expected_th),
                static_hourly_usd=float(static_hourly),
            ),
        )
    return sorted(profiles.values(), key=lambda item: (item.priority != "batch", item.gpu_key, item.memory_mb))


def observed_profile_key(gpu_key: Any, priority: Any) -> str | None:
    normalized_gpu = str(gpu_key or "").lower().strip()
    normalized_priority = str(priority or "").lower().strip()
    if not normalized_gpu or not normalized_priority or normalized_gpu == "requested":
        return None
    matches = [
        profile.profile_key
        for profile in load_profiles()
        if profile.gpu_key == normalized_gpu and profile.priority == normalized_priority
    ]
    return matches[0] if len(matches) == 1 else None


def expected_profit(
    profile: Profile,
    *,
    decision_price_usd: float,
    gross_prl_per_th_day: float,
    pearl_fee_rate: float,
    hourly_usd: float | None = None,
    min_profit_day: float = 0.0,
) -> ProfitEstimate:
    hourly = profile.static_hourly_usd if hourly_usd is None else hourly_usd
    effective_prl_per_th_day = gross_prl_per_th_day * (1 - pearl_fee_rate)
    revenue_day = profile.expected_th * effective_prl_per_th_day * decision_price_usd
    cost_day = hourly * 24
    profit_day = revenue_day - cost_day
    denominator = profile.expected_th * effective_prl_per_th_day
    break_even = cost_day / denominator if denominator > 0 else float("inf")
    min_safe = (cost_day + min_profit_day) / denominator if denominator > 0 else float("inf")
    margin_pct = (profit_day / cost_day) if cost_day > 0 else 0.0
    return ProfitEstimate(
        profile_key=profile.profile_key,
        gpu_key=profile.gpu_key,
        priority=profile.priority,
        memory_mb=profile.memory_mb,
        decision_price_usd=decision_price_usd,
        gross_prl_per_th_day=gross_prl_per_th_day,
        pearl_fee_rate=pearl_fee_rate,
        expected_th=profile.expected_th,
        hourly_usd=hourly,
        revenue_day=revenue_day,
        cost_day=cost_day,
        profit_day=profit_day,
        break_even_price_usd=break_even,
        min_safe_price_usd=min_safe,
        margin_pct=margin_pct,
    )


def risk_tier(
    profile: Profile,
    *,
    base_price_usd: float,
    boost_price_usd: float,
    gross_prl_per_th_day: float,
    pearl_fee_rate: float,
    min_profit_day: float,
) -> str:
    base = expected_profit(
        profile,
        decision_price_usd=base_price_usd,
        gross_prl_per_th_day=gross_prl_per_th_day,
        pearl_fee_rate=pearl_fee_rate,
        min_profit_day=min_profit_day,
    )
    boost = expected_profit(
        profile,
        decision_price_usd=boost_price_usd,
        gross_prl_per_th_day=gross_prl_per_th_day,
        pearl_fee_rate=pearl_fee_rate,
        min_profit_day=min_profit_day,
    )
    if base.profit_day >= min_profit_day:
        return "safe_base"
    if boost.profit_day >= min_profit_day:
        return "boost_only"
    if boost.profit_day >= 0:
        return "marginal"
    return "negative"


def estimate_table(
    *,
    decision_price_usd: float,
    gross_prl_per_th_day: float,
    pearl_fee_rate: float,
    min_profit_day: float,
) -> list[dict[str, Any]]:
    rows = []
    for profile in load_profiles():
        estimate = expected_profit(
            profile,
            decision_price_usd=decision_price_usd,
            gross_prl_per_th_day=gross_prl_per_th_day,
            pearl_fee_rate=pearl_fee_rate,
            min_profit_day=min_profit_day,
        )
        rows.append(
            {
                **asdict(profile),
                **{
                    key: round(value, 6) if isinstance(value, float) else value
                    for key, value in asdict(estimate).items()
                    if key not in {"profile_key", "gpu_key", "priority", "memory_mb"}
                },
                "profitable": estimate.profit_day >= min_profit_day,
                "positive": estimate.profit_day >= 0,
            }
        )
    rows.sort(key=lambda item: (float(item["profit_day"]), float(item["expected_th"])), reverse=True)
    return rows


def sync_profiles_to_db(db_path: str | None = None) -> None:
    import state_db

    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.upsert_gpu_profiles(conn, load_profiles())
        state_db.record_event(
            conn,
            "gpu_profiles_synced",
            source="profit_model",
            message="static GPU profile catalog synced",
            payload={"profiles": len(load_profiles())},
        )
        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Profit model for Salad PRL GPU profiles.")
    parser.add_argument("--price", type=float, default=None, help="Decision PRL/USD price.")
    parser.add_argument("--fee", type=float, default=None, help="Pearl fee rate. 0.01 means 1%%.")
    parser.add_argument("--gross-prl-per-th-day", type=float, default=DEFAULT_GROSS_PRL_PER_TH_DAY)
    parser.add_argument("--min-profit", type=float, default=None)
    parser.add_argument("--sync-db", action="store_true")
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_config()
    decision_price = args.price if args.price is not None else config.risk.decision_price_for_mode()
    fee = args.fee if args.fee is not None else config.risk.effective_fee_rate()
    min_profit = args.min_profit if args.min_profit is not None else config.risk.min_profit_for_mode()

    if args.sync_db:
        sync_profiles_to_db(args.db)

    rows = estimate_table(
        decision_price_usd=decision_price,
        gross_prl_per_th_day=args.gross_prl_per_th_day,
        pearl_fee_rate=fee,
        min_profit_day=min_profit,
    )
    payload = {
        "decision_price_usd": decision_price,
        "gross_prl_per_th_day": args.gross_prl_per_th_day,
        "pearl_fee_rate": fee,
        "min_profit_day": min_profit,
        "profiles": rows,
    }
    if args.json:
        print(json_dumps(payload))
        return

    print(f"decision_price={decision_price:.4f} fee={fee:.2%} min_profit_day={min_profit:.3f}")
    for row in rows:
        mark = "OK" if row["profitable"] else ("BE" if row["positive"] else "NO")
        print(
            f"{mark:2} {row['profile_key']:<22} "
            f"profit=${float(row['profit_day']):>7.3f}/day "
            f"breakeven=${float(row['break_even_price_usd']):.3f} "
            f"th={float(row['expected_th']):>6.2f} cost=${float(row['cost_day']):.2f}/day"
        )


if __name__ == "__main__":
    main()
