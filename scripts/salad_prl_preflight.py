#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ENV = pathlib.Path(os.environ.get("SALAD_PRL_ENV", str(REPO_ROOT / ".env")))
USER_AGENT = "salad-prl-preflight/1.0"
HTTP_TIMEOUT_SECONDS = float(os.environ.get("PRL_PREFLIGHT_HTTP_TIMEOUT_SECONDS", "12"))
SALAD_BASE = "https://api.salad.com/api/public"

DEFAULT_SALAD_ACCOUNTS = (
    ("kray", "SALAD_API_KEY_2"),
    ("kry1", "SALAD_API_KEY_KRY1"),
    ("kray2", "SALAD_API_KEY_2"),
    ("kray3", "SALAD_API_KEY_2"),
)

EXPECTED_TH_BY_PROFILE: dict[tuple[str, str], float] = {
    ("5090", "batch"): 315.0,
    ("4090", "batch"): 230.0,
    ("4080", "batch"): 160.47,
    ("4070tis", "batch"): 128.75,
    ("4070ti", "batch"): 125.0,
    ("5070", "batch"): 117.88,
    ("5070ti", "batch"): 145.0,
    ("5080", "batch"): 195.70,
    ("3080", "batch"): 65.0,
    ("3080ti", "batch"): 86.0,
    ("3070", "batch"): 61.76,
    ("3070ti", "batch"): 70.0,
    ("3060ti", "batch"): 45.0,
    ("3090", "batch"): 100.0,
    ("5060ti", "batch"): 85.0,
    ("5090laptop", "batch"): 131.0,
}

STATIC_HOURLY_USD_BY_PROFILE: dict[tuple[str, str], float] = {
    ("3060ti", "batch"): 0.03,
    ("3070", "batch"): 0.04,
    ("3070ti", "batch"): 0.06,
    ("3080", "batch"): 0.06,
    ("3080ti", "batch"): 0.08,
    ("3090", "batch"): 0.09,
    ("4070ti", "batch"): 0.08,
    ("4070tis", "batch"): 0.09,
    ("4080", "batch"): 0.11,
    ("4090", "batch"): 0.16,
    ("5070", "batch"): 0.08,
    ("5070ti", "batch"): 0.10,
    ("5060ti", "batch"): 0.07,
    ("5080", "batch"): 0.18,
    ("5090", "batch"): 0.25,
    ("5090laptop", "batch"): 0.10,
}


def load_env_file() -> None:
    if not ENV.exists():
        return
    for line in ENV.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def salad_accounts() -> tuple[tuple[str, str], ...]:
    fleet_orgs = tuple(org.strip() for org in os.environ.get("PRL_FLEET_ORGS", "").split(",") if org.strip())
    if fleet_orgs:
        default_key_env = os.environ.get("PRL_WATCH_DEFAULT_API_KEY_ENV", "SALAD_API_KEY")
        return tuple((org, os.environ.get(f"PRL_WATCH_API_KEY_ENV_{org.upper()}", default_key_env)) for org in fleet_orgs)
    return DEFAULT_SALAD_ACCOUNTS


def required_env() -> tuple[str, ...]:
    key_envs = tuple(dict.fromkeys(key_env for _org, key_env in salad_accounts()))
    return ("PRL_WALLET", *key_envs)


def env_missing() -> list[str]:
    missing: list[str] = []
    for key in required_env():
        value = os.environ.get(key, "").strip()
        if not value or value == "prl1...":
            missing.append(key)
    return missing


def external_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode() or "{}")


def open_json_with_retries(url: str, attempts: int = 3) -> dict[str, Any]:
    for attempt in range(attempts):
        try:
            return external_json(url)
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504, 522, 524} or attempt == attempts - 1:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts - 1:
                raise
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable retry loop")


def salad_json(path: str, api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        SALAD_BASE + path,
        headers={"Salad-Api-Key": api_key, "accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode() or "{}")


def validate_salad_access() -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for org, key_env in salad_accounts():
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            failures.append({"org": org, "key_env": key_env, "error": "missing_api_key"})
            continue
        try:
            payload = salad_json(f"/organizations/{org}/gpu-classes", api_key)
        except urllib.error.HTTPError as error:
            failures.append({"org": org, "key_env": key_env, "error": f"http_{error.code}"})
            continue
        except Exception as error:
            failures.append({"org": org, "key_env": key_env, "error": type(error).__name__})
            continue
        if not isinstance(payload.get("items"), list):
            failures.append({"org": org, "key_env": key_env, "error": "unexpected_gpu_classes_response"})
    return failures


def safetrade_prl_price_usd() -> float | None:
    payload = open_json_with_retries("https://safe.trade/api/v2/peatio/public/markets/prlusdt/tickers")
    ticker = payload.get("ticker") or {}
    values: list[float] = []
    for key in ("last", "buy", "sell"):
        try:
            value = float(ticker.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    return min(values) if values else None


def market_prl_price_usd() -> float:
    prices: list[float] = []
    try:
        value = float(
            (open_json_with_retries("https://pearlfortune.org/api/v1/market/price").get("data") or {}).get(
                "price_usd"
            )
            or 0
        )
        if value > 0:
            prices.append(value)
    except Exception:
        pass
    try:
        safetrade_price = safetrade_prl_price_usd()
        if safetrade_price:
            prices.append(safetrade_price)
    except Exception:
        pass
    return min(prices, default=0.0)


def pool_prl_per_th_day() -> tuple[float, int, float]:
    fee = float(
        (open_json_with_retries("https://pearlfortune.org/api/v1/stats/pool-fee-rate").get("data") or {}).get(
            "pool_fee_rate"
        )
        or 0
    )
    summary = open_json_with_retries("https://pearlfortune.org/api/v1/summary?hours=24")
    hourly_stats = ((summary.get("data") or {}).get("pool_stats") or {}).get("hourly_stats") or []
    gross = 0.0
    points = 0
    for item in hourly_stats:
        pool_hashrate = float(item.get("pool_hashrate") or 0)
        if pool_hashrate <= 0:
            continue
        gross += float(item.get("pool_reward") or 0) / (pool_hashrate / 1e12)
        points += 1
    return gross * (1 - fee), points, fee


def profitability_rows(decision_price: float) -> list[dict[str, Any]]:
    prl_per_th_day, hourly_points, pool_fee_rate = pool_prl_per_th_day()
    rows: list[dict[str, Any]] = []
    for profile, th in EXPECTED_TH_BY_PROFILE.items():
        hourly = STATIC_HOURLY_USD_BY_PROFILE[profile]
        revenue_day = th * prl_per_th_day * decision_price
        cost_day = hourly * 24
        rows.append(
            {
                "gpu": profile[0],
                "priority": profile[1],
                "th": round(th, 3),
                "cost_day": round(cost_day, 6),
                "revenue_day": round(revenue_day, 6),
                "profit_day": round(revenue_day - cost_day, 6),
                "pool_fee_rate": pool_fee_rate,
                "hourly_points": hourly_points,
                "prl_per_th_day_net": prl_per_th_day,
            }
        )
    return sorted(rows, key=lambda row: float(row["profit_day"]), reverse=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decision-price", type=float, default=float(os.environ.get("PRL_FILL_FIXED_DECISION_PRICE_USD", "0.64")))
    parser.add_argument("--min-profitable-profiles", type=int, default=1)
    parser.add_argument("--skip-env", action="store_true")
    parser.add_argument("--skip-salad", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    load_env_file()
    missing = [] if args.skip_env else env_missing()
    rows = profitability_rows(args.decision_price)
    profitable = [row for row in rows if float(row["profit_day"]) >= 0]
    salad_failures = [] if missing or args.skip_salad else validate_salad_access()
    payload = {
        "ok": not missing and not salad_failures and len(profitable) >= args.min_profitable_profiles,
        "missing_env": missing,
        "salad_access_failures": salad_failures,
        "decision_price_usd": args.decision_price,
        "live_market_prl_price_usd": market_prl_price_usd(),
        "profitable_profiles": len(profitable),
        "top_profiles": rows[:8],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if missing:
            print(f"missing required config: {', '.join(missing)}", file=sys.stderr)
        for failure in salad_failures:
            print(
                f"Salad access failed for org {failure['org']} using {failure['key_env']}: {failure['error']}",
                file=sys.stderr,
            )
        print(
            "profitable profiles at "
            f"${args.decision_price:.4f}/PRL: {len(profitable)}; "
            f"live market price: ${payload['live_market_prl_price_usd']:.4f}"
        )
        for row in rows[:8]:
            print(
                f"{row['gpu']} {row['priority']}: "
                f"profit=${row['profit_day']:.3f}/day, th={row['th']:.2f}, cost=${row['cost_day']:.2f}/day"
            )
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
