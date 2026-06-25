#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

import state_db
from config_loader import load_config
from fleet_common import json_dumps, utc_now


USER_AGENT = "salad-prl-price-oracle/1.0"
HTTP_TIMEOUT_SECONDS = 8


def external_json(url: str, timeout: float = HTTP_TIMEOUT_SECONDS) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode() or "{}")


def fetch_pearl_market_price() -> float | None:
    payload = external_json("https://pearlfortune.org/api/v1/market/price")
    value = float((payload.get("data") or {}).get("price_usd") or 0)
    return value if value > 0 else None


def fetch_safetrade_ticker() -> dict[str, float | None]:
    payload = external_json("https://safe.trade/api/v2/peatio/public/markets/prlusdt/tickers")
    ticker = payload.get("ticker") or {}
    result: dict[str, float | None] = {"last": None, "buy": None, "sell": None, "selected": None}
    values = []
    for key in ("last", "buy", "sell"):
        try:
            value = float(ticker.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            result[key] = value
            values.append(value)
    result["selected"] = min(values) if values else None
    return result


def fetch_pool_fee_rate() -> float | None:
    payload = external_json("https://pearlfortune.org/api/v1/stats/pool-fee-rate")
    value = float((payload.get("data") or {}).get("pool_fee_rate") or 0)
    return value if value >= 0 else None


def fetch_gross_prl_per_th_day(hours: int = 24) -> tuple[float | None, int]:
    payload = external_json(f"https://pearlfortune.org/api/v1/summary?hours={hours}")
    hourly_stats = ((payload.get("data") or {}).get("pool_stats") or {}).get("hourly_stats") or []
    gross = 0.0
    points = 0
    for item in hourly_stats:
        pool_hashrate = float(item.get("pool_hashrate") or 0)
        if pool_hashrate <= 0:
            continue
        gross += float(item.get("pool_reward") or 0) / (pool_hashrate / 1e12)
        points += 1
    return (gross if points else None), points


def reward_calibration_factor() -> float:
    try:
        return float(os.environ.get("PRL_REWARD_CALIBRATION_FACTOR", "1.0"))
    except ValueError:
        return 1.0


def sample_price(config_fee_rate: float) -> dict[str, Any]:
    errors: list[str] = []
    pearl_price: float | None = None
    safetrade: dict[str, float | None] = {"last": None, "buy": None, "sell": None, "selected": None}
    gross_prl: float | None = None
    pool_fee: float | None = None
    points = 0

    try:
        pearl_price = fetch_pearl_market_price()
    except Exception as exc:
        errors.append(f"pearl_market:{type(exc).__name__}")

    try:
        safetrade = fetch_safetrade_ticker()
    except Exception as exc:
        errors.append(f"safetrade:{type(exc).__name__}")

    try:
        gross_prl, points = fetch_gross_prl_per_th_day()
    except Exception as exc:
        errors.append(f"pool_summary:{type(exc).__name__}")

    try:
        pool_fee = fetch_pool_fee_rate()
    except Exception as exc:
        errors.append(f"pool_fee:{type(exc).__name__}")

    price_values = [value for value in (pearl_price, safetrade.get("selected")) if value and value > 0]
    selected = min(price_values) if price_values else None
    source_spread = (max(price_values) - min(price_values)) if len(price_values) > 1 else None
    calibration = reward_calibration_factor()
    calibrated_gross_prl = gross_prl * calibration if gross_prl is not None else None

    return {
        "sampled_at_utc": utc_now(),
        "pearl_price_usd": pearl_price,
        "safetrade_last_usd": safetrade.get("last"),
        "safetrade_buy_usd": safetrade.get("buy"),
        "safetrade_sell_usd": safetrade.get("sell"),
        "selected_price_usd": selected,
        "source_spread_usd": source_spread,
        "gross_prl_per_th_day": calibrated_gross_prl,
        "raw_gross_prl_per_th_day": gross_prl,
        "reward_calibration_factor": calibration,
        "pool_fee_rate": pool_fee,
        "configured_pearl_fee_rate": config_fee_rate,
        "pool_summary_points": points,
        "error": ";".join(errors) if errors else None,
    }


def compute_risk_mode(conn, sample: dict[str, Any], config) -> dict[str, Any]:
    win15 = state_db.price_window(conn, 15)
    win30 = state_db.price_window(conn, 30)
    win60 = state_db.price_window(conn, 60)
    current = sample.get("selected_price_usd")
    fee = config.risk.effective_fee_rate()

    mode = "base_fill"
    decision = config.risk.base_decision_price
    reason = "base decision price"
    trailing_min_15m = win15["min"]
    trailing_min_30m = win30["min"]
    trailing_min_1h = win60["min"]
    boost_confirmed = (
        float(win30.get("count") or 0) >= 5
        and float(win30.get("span_seconds") or 0) >= config.risk.boost_min_window_seconds
    )
    aggressive_confirmed = (
        float(win60.get("count") or 0) >= 5
        and float(win60.get("span_seconds") or 0) >= config.risk.aggressive_min_window_seconds
    )

    if trailing_min_15m is not None and trailing_min_15m < config.risk.risk_off_trailing_min_15m:
        mode = "risk_off"
        decision = config.risk.base_decision_price
        reason = "trailing_min_15m below risk-off trigger"
    elif (
        current is not None
        and trailing_min_1h is not None
        and trailing_min_1h >= config.risk.aggressive_trailing_min_1h
        and aggressive_confirmed
    ):
        mode = "aggressive_boost"
        decision = max(0.0, min(float(current), float(trailing_min_1h)) - config.risk.boost_price_band)
        reason = "trailing_min_1h supports aggressive boost"
    elif (
        current is not None
        and trailing_min_30m is not None
        and trailing_min_30m >= config.risk.boost_trailing_min_30m
        and boost_confirmed
    ):
        mode = "boost_fill"
        decision = max(0.0, min(float(current), float(trailing_min_30m)) - config.risk.boost_price_band)
        reason = "trailing_min_30m supports boost fill"
    elif current is not None and trailing_min_30m is not None and trailing_min_30m >= config.risk.boost_trailing_min_30m:
        reason = "boost price seen but trailing window is not confirmed yet"
    elif current is None:
        reason = "current price unavailable; using base decision price"

    return {
        "at_utc": utc_now(),
        "mode": mode,
        "decision_price_usd": round(decision, 6),
        "trailing_min_15m": trailing_min_15m,
        "trailing_min_30m": trailing_min_30m,
        "trailing_min_1h": trailing_min_1h,
        "trailing_avg_30m": win30["avg"],
        "trailing_avg_1h": win60["avg"],
        "trailing_count_30m": win30["count"],
        "trailing_count_1h": win60["count"],
        "trailing_span_30m_seconds": win30["span_seconds"],
        "trailing_span_1h_seconds": win60["span_seconds"],
        "pearl_fee_rate": fee,
        "reason": reason,
        "sample": sample,
    }


def run_once(db_path: str | None = None) -> dict[str, Any]:
    config = load_config()
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.sync_config(conn, config)
        sample = sample_price(config.risk.effective_fee_rate())
        state_db.insert_price_sample(conn, sample)
        risk = compute_risk_mode(conn, sample, config)
        state_db.set_risk_mode(conn, risk)
        state_db.write_heartbeat(
            conn,
            "price_oracle",
            payload={
                "mode": risk["mode"],
                "decision_price_usd": risk["decision_price_usd"],
                "selected_price_usd": sample.get("selected_price_usd"),
                "error": sample.get("error"),
            },
        )
        state_db.record_event(
            conn,
            "price_sampled",
            source="price_oracle",
            level="warning" if sample.get("error") else "info",
            message="price oracle sampled PRL market data",
            payload={"sample": sample, "risk": risk},
        )
        conn.commit()
    return {"sample": sample, "risk": risk}


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample PRL price and compute fleet risk mode.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.loop:
        while True:
            payload = run_once(args.db)
            print(json_dumps(payload) if args.json else f"{payload['risk']['mode']} price={payload['risk']['decision_price_usd']}")
            time.sleep(args.interval)
    else:
        payload = run_once(args.db)
        print(json_dumps(payload) if args.json else f"{payload['risk']['mode']} price={payload['risk']['decision_price_usd']}")


if __name__ == "__main__":
    main()
