#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ENV = pathlib.Path(os.environ.get("SALAD_PRL_ENV", str(REPO_ROOT / ".env")))
DEFAULT_SNAPSHOT_CSV = REPO_ROOT / "state" / "prl_profit_snapshots.csv"
STATE_DIR = pathlib.Path(os.environ.get("PRL_STATE_DIR", str(REPO_ROOT / "state")))


def load_env_file() -> None:
    if not ENV.exists():
        return
    for line in ENV.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()
BASE = "https://api.salad.com/api/public"
WALLET = os.environ.get("PRL_WALLET", "")
USER_AGENT = "kray-prl-profit-snapshot/1.0"
HTTP_TIMEOUT_SECONDS = float(os.environ.get("PRL_SNAPSHOT_HTTP_TIMEOUT_SECONDS", "8"))
HTTP_ATTEMPTS = max(1, int(os.environ.get("PRL_SNAPSHOT_HTTP_ATTEMPTS", "3")))
PRL_ATOMIC_UNITS = float(os.environ.get("PRL_ATOMIC_UNITS", "100000000"))
STUCK_NON_LIVE_SECONDS = int(os.environ.get("PRL_STUCK_NON_LIVE_SECONDS", "3600"))
EMPTY_STUCK_NON_LIVE_SECONDS = int(
    os.environ.get("PRL_EMPTY_STUCK_NON_LIVE_SECONDS", str(STUCK_NON_LIVE_SECONDS))
)
SALAD_FETCH_WORKERS = int(os.environ.get("PRL_SNAPSHOT_SALAD_FETCH_WORKERS", "12"))
RUNNING_NO_LIVE_GRACE_SECONDS = int(os.environ.get("PRL_NOHASH_GRACE_SECONDS", "900"))
REWARD_CALIBRATION_FACTOR = float(os.environ.get("PRL_REWARD_CALIBRATION_FACTOR", "1.0"))
SLOT_ACTION_STATE_PATH = pathlib.Path(
    os.environ.get("PRL_SLOT_ACTION_STATE_PATH", str(STATE_DIR / "prl_slot_actions.json"))
)


def default_snapshot_price() -> float:
    for key in (
        "PRL_SNAPSHOT_PRICE_USD",
        "PRL_FIXED_DECISION_PRICE_USD",
        "PRL_WATCH_FIXED_DECISION_PRICE_USD",
        "PRL_FILL_FIXED_DECISION_PRICE_USD",
        "PRL_NOHASH_FALLBACK_PRICE",
    ):
        value = os.environ.get(key)
        if value:
            price = float(value)
            if price > 0:
                return price
    try:
        live_price = market_prl_price_usd()
    except Exception:
        live_price = 0.0
    if live_price > 0:
        return live_price
    return 0.62

DEFAULT_ACCOUNTS = [
    ("kray", "kray", "SALAD_API_KEY_2", [f"prl-kray-roi-{index:02d}" for index in range(1, 11)]),
    ("kry1", "kry1", "SALAD_API_KEY_KRY1", [f"prl-kry1-roi-{index:02d}" for index in range(1, 11)]),
    ("kray2", "kray2", "SALAD_API_KEY_2", [f"prl-kray2-roi-{index:02d}" for index in range(1, 11)]),
    ("kray3", "kray3", "SALAD_API_KEY_2", [f"prl-kray3-roi-{index:02d}" for index in range(1, 11)]),
]
ACCOUNTS = list(DEFAULT_ACCOUNTS)
if os.environ.get("PRL_INCLUDE_BMU", "").lower() in {"1", "true", "yes"}:
    ACCOUNTS.insert(0, ("bmu", "bmu", "SALAD_API_KEY", [f"prl-roi-fresh-{index:02d}" for index in range(1, 7)]))
    for bmu_org in ("bmu2", "bmu3", "bmu4", "bmu5"):
        ACCOUNTS.append(
            (
                bmu_org,
                bmu_org,
                "SALAD_API_KEY",
                [f"prl-{bmu_org}-roi-{index:02d}" for index in range(1, 11)],
            )
        )


def configured_accounts() -> list[tuple[str, str, str, list[str]]]:
    fleet_orgs = [org.strip() for org in os.environ.get("PRL_FLEET_ORGS", "").split(",") if org.strip()]
    if not fleet_orgs:
        return list(ACCOUNTS)
    default_key_env = os.environ.get("PRL_WATCH_DEFAULT_API_KEY_ENV", "SALAD_API_KEY")
    accounts: list[tuple[str, str, str, list[str]]] = []
    for org in fleet_orgs:
        key_env = os.environ.get(f"PRL_WATCH_API_KEY_ENV_{org.upper()}", default_key_env)
        prefix = os.environ.get(f"PRL_WATCH_SLOT_PREFIX_{org.upper()}", f"prl-{org}-roi")
        count = int(os.environ.get(f"PRL_WATCH_SLOT_COUNT_{org.upper()}", "10"))
        accounts.append((org, org, key_env, [f"{prefix}-{index:02d}" for index in range(1, count + 1)]))
    return accounts

GPU_IDS = {
    "3060ti": "cb6c1931-89b6-4f76-976f-54047320ccc6",
    "3070": "951131f6-5acf-489c-b303-0906be8b26ef",
    "3070ti": "d9fb0bd6-05c9-4cb9-b98e-9f7d1b5ba0e7",
    "3080": "43a49c0c-f860-40e9-a509-702d0dba0902",
    "3080ti": "65247de0-746f-45c6-8537-650ba613966a",
    "3090": "a5db5c50-cbcb-4596-ae80-6a0c8090d80f",
    "4070ti": "de00c90b-904b-4d9e-8fc9-1d9a08eb0932",
    "4070tis": "f1380143-51cd-4bad-80cb-1f86ee6b49fe",
    "4080": "0d062939-7c01-4aae-a2b1-30e315124e51",
    "4090": "ed563892-aacd-40f5-80b7-90c9be6c759b",
    "5060ti": "5d6b104d-c029-4357-b179-8b662d0a76b2",
    "5070": "61e8ceee-4479-40c5-9a05-1711f45f931c",
    "5070ti": "1b8747be-e789-475b-a339-3c1028010d84",
    "5080": "8065b30b-4a27-434c-8610-222e8df8fad7",
    "5090": "851399fb-7329-4195-a042-d6514b28cf33",
    "5090laptop": "83ef776e-ce34-4d89-8cf9-81898f1416fa",
}

GPU_TOKENS = [
    ("5090laptop", GPU_IDS["5090laptop"]),
    ("5090", GPU_IDS["5090"]),
    ("5080", GPU_IDS["5080"]),
    ("5070ti", GPU_IDS["5070ti"]),
    ("5070", GPU_IDS["5070"]),
    ("5060ti", GPU_IDS["5060ti"]),
    ("4070tis", GPU_IDS["4070tis"]),
    ("4070ti", GPU_IDS["4070ti"]),
    ("4080", GPU_IDS["4080"]),
    ("4090", GPU_IDS["4090"]),
    ("3090", GPU_IDS["3090"]),
    ("3080ti", GPU_IDS["3080ti"]),
    ("3080", GPU_IDS["3080"]),
    ("3070ti", GPU_IDS["3070ti"]),
    ("3070", GPU_IDS["3070"]),
    ("3060ti", GPU_IDS["3060ti"]),
]

STATIC_PRICES_HOURLY = {
    GPU_IDS["3060ti"]: {"high": 0.08, "medium": 0.063, "low": 0.047, "batch": 0.03},
    GPU_IDS["3070"]: {"high": 0.10, "medium": 0.08, "low": 0.06, "batch": 0.04},
    GPU_IDS["3070ti"]: {"high": 0.10, "medium": 0.087, "low": 0.073, "batch": 0.06},
    GPU_IDS["3080"]: {"high": 0.18, "medium": 0.14, "low": 0.10, "batch": 0.06},
    GPU_IDS["3080ti"]: {"high": 0.20, "medium": 0.16, "low": 0.12, "batch": 0.08},
    GPU_IDS["3090"]: {"high": 0.25, "medium": 0.197, "low": 0.143, "batch": 0.09},
    GPU_IDS["4070ti"]: {"high": 0.24, "medium": 0.187, "low": 0.133, "batch": 0.08},
    GPU_IDS["4070tis"]: {"high": 0.26, "medium": 0.203, "low": 0.147, "batch": 0.09},
    GPU_IDS["4080"]: {"high": 0.28, "medium": 0.223, "low": 0.167, "batch": 0.11},
    GPU_IDS["4090"]: {"high": 0.30, "medium": 0.253, "low": 0.207, "batch": 0.16},
    GPU_IDS["5060ti"]: {"high": 0.18, "medium": 0.143, "low": 0.107, "batch": 0.07},
    GPU_IDS["5070"]: {"high": 0.24, "medium": 0.187, "low": 0.133, "batch": 0.08},
    GPU_IDS["5070ti"]: {"high": 0.28, "medium": 0.22, "low": 0.16, "batch": 0.10},
    GPU_IDS["5080"]: {"high": 0.42, "medium": 0.335, "low": 0.25, "batch": 0.18},
    GPU_IDS["5090"]: {"high": 0.45, "medium": 0.38, "low": 0.31, "batch": 0.25},
    GPU_IDS["5090laptop"]: {"high": 0.28, "medium": 0.22, "low": 0.16, "batch": 0.10},
}


def load_env() -> None:
    load_env_file()


def external_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"accept": "application/json", "User-Agent": USER_AGENT})
    return open_json_with_retries(request)


def open_json_with_retries(request: urllib.request.Request, attempts: int | None = None) -> dict[str, Any]:
    attempts = HTTP_ATTEMPTS if attempts is None else attempts
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504, 522, 524} or attempt == attempts - 1:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts - 1:
                raise
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable retry loop")


def safetrade_prl_price_usd() -> float | None:
    try:
        payload = external_json("https://safe.trade/api/v2/peatio/public/markets/prlusdt/tickers")
    except Exception:
        return None
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
        pearl_price = float((external_json("https://pearlfortune.org/api/v1/market/price").get("data") or {}).get("price_usd") or 0)
        if pearl_price > 0:
            prices.append(pearl_price)
    except Exception:
        pass
    safetrade_price = safetrade_prl_price_usd()
    if safetrade_price:
        prices.append(safetrade_price)
    return min(prices, default=0.0)


def salad_json(path: str, api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        BASE + path,
        headers={"Salad-Api-Key": api_key, "accept": "application/json", "User-Agent": USER_AGENT},
    )
    return open_json_with_retries(request)


def worker_instance_id(worker_name: str) -> str | None:
    marker = "-pearlfortune-"
    if marker not in worker_name:
        return None
    return worker_name.rsplit(marker, 1)[-1] or None


def worker_named_slot(worker_name: str, accounts: list[tuple[str, str, str, list[str]]]) -> str | None:
    for _label, _org, _key_env, slots in accounts:
        for slot in slots:
            if slot in worker_name:
                return slot
    return None


def normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def gpu_from_model(model: str) -> tuple[str | None, str | None]:
    normalized = normalize(model)
    for token, gpu_id in GPU_TOKENS:
        if token in normalized:
            return gpu_id, token
    return None, None


def gpu_names(gpu_ids: list[Any]) -> list[str]:
    by_id = {gpu_id: token for token, gpu_id in GPU_IDS.items()}
    return [by_id.get(str(gpu_id), str(gpu_id)) for gpu_id in gpu_ids]


def safe_slot_action_token(org: str, slot: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{org}_{slot}").strip("_") or "slot"


def slot_action_detail_path(org: str, slot: str) -> pathlib.Path:
    return SLOT_ACTION_STATE_PATH.parent / f"{SLOT_ACTION_STATE_PATH.stem}.d" / f"{safe_slot_action_token(org, slot)}.json"


def slot_action_with_age(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    try:
        at_ts = float(entry.get("at") or entry.get("at_ts") or 0)
    except (TypeError, ValueError):
        at_ts = 0.0
    if at_ts <= 0:
        return None
    action = dict(entry)
    action["age_seconds"] = max(0.0, time.time() - at_ts)
    return action


def recent_slot_action(org: str, slot: str) -> dict[str, Any] | None:
    try:
        detail = json.loads(slot_action_detail_path(org, slot).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        detail = None
    action = slot_action_with_age(detail)
    if action:
        return action
    try:
        state = json.loads(SLOT_ACTION_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return slot_action_with_age(state.get(f"{org}/{slot}") or state.get(slot))


def effective_state_age_seconds(org: str, slot: str, observed_age: float | None) -> tuple[float | None, dict[str, Any] | None]:
    action = recent_slot_action(org, slot)
    if observed_age is None:
        return None, action
    try:
        action_age = float(action.get("age_seconds") or 0) if action else 0.0
    except (TypeError, ValueError):
        action_age = 0.0
    if action_age > 0 and action_age < observed_age:
        return action_age, action
    return observed_age, action


def pool_prl_per_th_day() -> tuple[float, int, float]:
    fee = float(
        (external_json("https://pearlfortune.org/api/v1/stats/pool-fee-rate").get("data") or {}).get(
            "pool_fee_rate"
        )
        or 0
    )
    summary = external_json("https://pearlfortune.org/api/v1/summary?hours=24")
    hourly_stats = ((summary.get("data") or {}).get("pool_stats") or {}).get("hourly_stats") or []
    gross = 0.0
    points = 0
    for item in hourly_stats:
        pool_hashrate = float(item.get("pool_hashrate") or 0)
        if pool_hashrate <= 0:
            continue
        gross += float(item.get("pool_reward") or 0) / (pool_hashrate / 1e12)
        points += 1
    return gross * (1 - fee) * REWARD_CALIBRATION_FACTOR, points, fee


def atomic_to_prl(value: Any) -> float:
    try:
        return float(value or 0) / PRL_ATOMIC_UNITS
    except (TypeError, ValueError):
        return 0.0


def amount_by_window_prl(payload: dict[str, Any], key: str) -> float:
    values = payload.get(key) or {}
    return atomic_to_prl(values.get("h24"))


def wallet_observed_rewards(prl_per_th_day: float) -> dict[str, Any] | None:
    try:
        shares = external_json(f"https://pearlfortune.org/api/v1/miners/{WALLET}/hourly-shares?hours=24")
        miner = external_json(f"https://pearlfortune.org/api/v1/miners/{WALLET}?hours=24")
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    shares_data = shares.get("data") or {}
    miner_data = miner.get("data") or {}
    pending = miner_data.get("pending_shares") or {}
    credited_24h = amount_by_window_prl(shares_data, "credited_amount_by_window_atomic")
    pending_24h = amount_by_window_prl(pending, "pending_estimate_by_window_atomic")
    total_24h = credited_24h + pending_24h

    rolling = shares_data.get("rolling_hashrates") or []
    hashrate_h24 = next(
        (
            float(item.get("hashrate") or 0)
            for item in rolling
            if int(float(item.get("hours") or 0)) == 24
        ),
        0.0,
    )
    th_24h = hashrate_h24 / 1e12
    expected_24h = th_24h * prl_per_th_day if th_24h > 0 else 0.0
    observed_per_th_day = total_24h / th_24h if th_24h > 0 else 0.0
    ratio = total_24h / expected_24h if expected_24h > 0 else None

    return {
        "credited_prl_24h": round(credited_24h, 8),
        "pending_prl_24h": round(pending_24h, 8),
        "total_prl_24h": round(total_24h, 8),
        "rolling_hashrate_th_24h": round(th_24h, 6),
        "observed_prl_per_th_day_24h": round(observed_per_th_day, 8),
        "model_prl_per_th_day": round(prl_per_th_day, 8),
        "expected_prl_24h_at_rolling_hashrate": round(expected_24h, 8),
        "observed_to_model_ratio_24h": round(ratio, 6) if ratio is not None else None,
    }


def price_catalog(org: str, api_key: str) -> dict[str, dict[str, float]]:
    catalog: dict[str, dict[str, float]] = {}
    payload = salad_json(f"/organizations/{org}/gpu-classes", api_key)
    for item in payload.get("items") or []:
        gpu_id = str(item.get("id") or "")
        prices: dict[str, float] = {}
        for price in item.get("prices") or []:
            priority = str(price.get("priority") or "").lower()
            if not priority:
                continue
            try:
                prices[priority] = float(price.get("price"))
            except (TypeError, ValueError):
                pass
        if gpu_id:
            catalog[gpu_id] = prices
    return catalog


def hourly_price(
    gpu_id: str | None,
    priority: str,
    org: str | None,
    catalogs: dict[str, dict[str, dict[str, float]]],
) -> float | None:
    if not gpu_id:
        return None
    if org:
        live_value = catalogs.get(org, {}).get(gpu_id, {}).get(priority)
        if live_value is not None:
            return float(live_value)
    static_value = STATIC_PRICES_HOURLY.get(gpu_id, {}).get(priority)
    return float(static_value) if static_value is not None else None


def fallback_hourly(
    group: dict[str, Any],
    org: str,
    catalogs: dict[str, dict[str, dict[str, float]]],
) -> float:
    priority = str(group.get("priority") or "").lower()
    resources = ((group.get("container") or {}).get("resources") or {})
    values = [
        value
        for gpu_id in resources.get("gpu_classes") or []
        if (value := hourly_price(str(gpu_id), priority, org, catalogs)) is not None
    ]
    return max(values, default=0.0)


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def state_age_seconds(value: Any, now_dt: datetime) -> float | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return max(0.0, (now_dt - parsed.astimezone(UTC)).total_seconds())


def parse_workers() -> list[dict[str, Any]]:
    payload = external_json(f"https://pearlfortune.org/api/v1/miners/{WALLET}/connections")
    workers = ((payload.get("data") or {}).get("workers") or [])
    rows: list[dict[str, Any]] = []
    for worker in workers:
        name = str(worker.get("worker") or "")
        th = float(worker.get("reported_hashrate") or 0) / 1e12
        gpu = (((worker.get("client_info") or {}).get("gpus") or [{}])[0]).get("model") or ""
        gpu_id, gpu_token = gpu_from_model(gpu)
        rows.append(
            {
                "worker": name,
                "slot": None,
                "named_slot": None,
                "gpu": gpu,
                "gpu_id": gpu_id,
                "gpu_token": gpu_token,
                "th": th,
                "stale": bool(worker.get("stale")),
                "last_stats_at": worker.get("last_stats_at"),
            }
        )
    return rows


CSV_FIELDS = [
    "at_utc",
    "assumed_prl_price",
    "live_market_prl_price",
    "pool_fee_rate",
    "reward_calibration_factor",
    "hourly_points",
    "prl_per_th_day_net",
    "wallet_observed_credited_prl_24h",
    "wallet_observed_pending_prl_24h",
    "wallet_observed_total_prl_24h",
    "wallet_observed_rolling_hashrate_th_24h",
    "wallet_observed_prl_per_th_day_24h",
    "wallet_observed_to_model_ratio_24h",
    "wallet_observed_error",
    "fresh_workers",
    "slot_count",
    "live_slot_count",
    "pending_slot_count",
    "running_no_live_count",
    "running_no_live_slots",
    "stuck_non_live_count",
    "stuck_non_live_slots",
    "pool_worker_count",
    "pool_stale_worker_count",
    "stale_current_worker_count",
    "stale_current_workers",
    "org_discrepancies",
    "unmapped_live_worker_count",
    "total_th",
    "total_prl_day",
    "total_revenue_day",
    "total_cost_day",
    "total_profit_day",
    "market_revenue_day",
    "market_profit_day",
    "unmapped_th",
    "unmapped_prl_day",
    "unmapped_revenue_day",
    "unmapped_market_revenue_day",
    "by_gpu_priority",
    "slots",
]


def snapshot_csv_path() -> pathlib.Path | None:
    if os.environ.get("PRL_SNAPSHOT_CSV_DISABLE", "").lower() in {"1", "true", "yes"}:
        return None
    return pathlib.Path(os.environ.get("PRL_SNAPSHOT_CSV_PATH", str(DEFAULT_SNAPSHOT_CSV)))


def append_snapshot_csv(snapshot: dict[str, Any]) -> None:
    path = snapshot_csv_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        totals = snapshot.get("totals") or {}
        unmapped_totals = snapshot.get("unmapped_totals") or {}
        observed_rewards = snapshot.get("wallet_observed_rewards") or {}
        pending_slots = snapshot.get("pending_slots") or []
        live_slot_count = sum(1 for slot in pending_slots if slot.get("live"))
        pending_slot_count = sum(
            1
            for slot in pending_slots
            if int(slot.get("allocating") or 0) > 0 or int(slot.get("creating") or 0) > 0
        )
        row = {
            "at_utc": snapshot.get("at_utc"),
            "assumed_prl_price": snapshot.get("assumed_prl_price"),
            "live_market_prl_price": snapshot.get("live_market_prl_price"),
            "pool_fee_rate": snapshot.get("pool_fee_rate"),
            "reward_calibration_factor": snapshot.get("reward_calibration_factor"),
            "hourly_points": snapshot.get("hourly_points"),
            "prl_per_th_day_net": snapshot.get("prl_per_th_day_net"),
            "wallet_observed_credited_prl_24h": observed_rewards.get("credited_prl_24h"),
            "wallet_observed_pending_prl_24h": observed_rewards.get("pending_prl_24h"),
            "wallet_observed_total_prl_24h": observed_rewards.get("total_prl_24h"),
            "wallet_observed_rolling_hashrate_th_24h": observed_rewards.get("rolling_hashrate_th_24h"),
            "wallet_observed_prl_per_th_day_24h": observed_rewards.get("observed_prl_per_th_day_24h"),
            "wallet_observed_to_model_ratio_24h": observed_rewards.get("observed_to_model_ratio_24h"),
            "wallet_observed_error": observed_rewards.get("error"),
            "fresh_workers": snapshot.get("fresh_workers"),
            "slot_count": len(pending_slots),
            "live_slot_count": live_slot_count,
            "pending_slot_count": pending_slot_count,
            "running_no_live_count": len(snapshot.get("running_no_live_billable_slots") or []),
            "running_no_live_slots": json.dumps(snapshot.get("running_no_live_billable_slots") or [], sort_keys=True),
            "stuck_non_live_count": len(snapshot.get("stuck_non_live_slots") or []),
            "stuck_non_live_slots": json.dumps(snapshot.get("stuck_non_live_slots") or [], sort_keys=True),
            "pool_worker_count": snapshot.get("pool_worker_count"),
            "pool_stale_worker_count": snapshot.get("pool_stale_worker_count"),
            "stale_current_worker_count": len(snapshot.get("stale_current_workers") or []),
            "stale_current_workers": json.dumps(snapshot.get("stale_current_workers") or [], sort_keys=True),
            "org_discrepancies": json.dumps(snapshot.get("org_discrepancies") or [], sort_keys=True),
            "unmapped_live_worker_count": len(snapshot.get("unmapped_live_workers") or []),
            "total_th": totals.get("th"),
            "total_prl_day": totals.get("prl_day"),
            "total_revenue_day": totals.get("revenue_day"),
            "total_cost_day": totals.get("cost_day"),
            "total_profit_day": totals.get("profit_day"),
            "market_revenue_day": totals.get("market_revenue_day"),
            "market_profit_day": totals.get("market_profit_day"),
            "unmapped_th": unmapped_totals.get("th"),
            "unmapped_prl_day": unmapped_totals.get("prl_day"),
            "unmapped_revenue_day": unmapped_totals.get("revenue_day"),
            "unmapped_market_revenue_day": unmapped_totals.get("market_revenue_day"),
            "by_gpu_priority": json.dumps(snapshot.get("by_gpu_priority") or [], sort_keys=True),
            "slots": json.dumps(snapshot.get("slots") or [], sort_keys=True),
        }
        write_header = True
        if path.exists() and path.stat().st_size > 0:
            try:
                with path.open(newline="") as existing:
                    write_header = not any(line.strip() for line in existing)
            except OSError:
                write_header = True
        with path.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:
        print(f"warning: failed to append snapshot CSV {path}: {type(exc).__name__}: {exc}", file=sys.stderr)


def build_snapshot(price: float) -> dict[str, Any]:
    load_env()
    if not WALLET or WALLET == "prl1...":
        raise RuntimeError("PRL_WALLET must be set in the environment or .env file")
    accounts = configured_accounts()
    snapshot_at = datetime.now(UTC)
    prl_per_th_day, hourly_points, pool_fee_rate = pool_prl_per_th_day()
    market_price = market_prl_price_usd()
    observed_rewards = wallet_observed_rewards(prl_per_th_day)

    catalogs: dict[str, dict[str, dict[str, float]]] = {}
    groups: dict[str, tuple[str, str, dict[str, Any]]] = {}
    group_instance_ids: dict[str, set[str]] = {}
    for label, org, key_env, slots in accounts:
        api_key = os.environ[key_env]
        catalogs[label] = price_catalog(org, api_key)

    def fetch_slot(label: str, org: str, key_env: str, slot: str) -> tuple[str, str, str, dict[str, Any], set[str]] | None:
        api_key = os.environ[key_env]
        try:
            group = salad_json(f"/organizations/{org}/projects/default/containers/{slot}", api_key)
            instances_payload = salad_json(
                f"/organizations/{org}/projects/default/containers/{slot}/instances",
                api_key,
            )
            instance_ids = {
                str(item.get("id"))
                for item in (instances_payload.get("items") or instances_payload.get("instances") or [])
                if item.get("id")
            }
            return slot, label, org, group, instance_ids
        except Exception:
            return None

    slot_jobs = [
        (label, org, key_env, slot)
        for label, org, key_env, slots in accounts
        for slot in slots
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, SALAD_FETCH_WORKERS)) as executor:
        futures = [executor.submit(fetch_slot, *job) for job in slot_jobs]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result is None:
                continue
            slot, label, org, group, instance_ids = result
            groups[slot] = (label, org, group)
            group_instance_ids[slot] = instance_ids

    pool_workers = parse_workers()
    for row in pool_workers:
        worker_instance = worker_instance_id(str(row.get("worker") or ""))
        row["named_slot"] = worker_named_slot(str(row.get("worker") or ""), accounts)
        for _label, _org, _key_env, slots in accounts:
            for slot in slots:
                if slot in row["worker"] and worker_instance and worker_instance in group_instance_ids.get(slot, set()):
                    row["slot"] = slot
                    row["slot_match"] = "instance"
                    break
            if row["slot"]:
                break
        if not row["slot"] and row["named_slot"] in groups:
            named_slot = str(row["named_slot"])
            _label, _org, group = groups[named_slot]
            current = group.get("current_state") or {}
            counts = current.get("instance_status_counts") or {}
            active = any(int(counts.get(key) or 0) > 0 for key in ("running_count", "creating_count", "allocating_count"))
            if not worker_instance or (active and not group_instance_ids.get(named_slot)):
                row["slot"] = named_slot
                row["slot_match"] = "worker_name"
            elif not active:
                row["inactive_named_slot"] = named_slot
                row["slot_match"] = "worker_name_inactive"

    workers = [
        worker
        for worker in pool_workers
        if not worker.get("stale") and float(worker.get("th") or 0) > 0
    ]
    stale_current_workers = [
        worker
        for worker in pool_workers
        if worker.get("slot") and (worker.get("stale") or float(worker.get("th") or 0) <= 0)
    ]

    unmapped_workers = []
    for worker in workers:
        if worker.get("slot"):
            continue
        prl_day = float(worker["th"]) * prl_per_th_day
        revenue_day = prl_day * price
        market_revenue_day = prl_day * market_price
        unmapped_workers.append(
            {
                **worker,
                "prl_day": prl_day,
                "revenue_day": revenue_day,
                "market_revenue_day": market_revenue_day,
            }
        )

    rows: list[dict[str, Any]] = []
    for worker in workers:
        if not worker.get("slot"):
            continue
        public_org, _api_org, group = groups.get(str(worker["slot"]), (None, None, {}))
        priority = str(group.get("priority") or "").lower()
        hourly = hourly_price(worker.get("gpu_id"), priority, public_org, catalogs) if public_org else None
        if hourly is None and public_org:
            hourly = fallback_hourly(group, public_org, catalogs)
        cost_day = 24 * float(hourly or 0)
        prl_day = float(worker["th"]) * prl_per_th_day
        revenue_day = prl_day * price
        market_revenue_day = prl_day * market_price
        rows.append(
            {
                **worker,
                "org": public_org,
                "priority": priority,
                "cost_day": cost_day,
                "prl_day": prl_day,
                "revenue_day": revenue_day,
                "market_revenue_day": market_revenue_day,
                "profit_day": revenue_day - cost_day,
                "market_profit_day": market_revenue_day - cost_day,
            }
        )

    known_slots = {row.get("slot") for row in rows}
    pending_slots = []
    running_no_live = []
    stuck_non_live = []
    for slot, (public_org, _api_org, group) in groups.items():
        current = group.get("current_state") or {}
        counts = current.get("instance_status_counts") or {}
        resources = ((group.get("container") or {}).get("resources") or {})
        status = str(current.get("status") or "").lower()
        age = state_age_seconds(current.get("start_time") or current.get("finish_time"), snapshot_at)
        running = int(counts.get("running_count") or 0)
        creating = int(counts.get("creating_count") or 0)
        allocating = int(counts.get("allocating_count") or 0)
        stopping = int(counts.get("stopping_count") or 0)
        instance_count = len(group_instance_ids.get(slot, set()))
        empty_pending = instance_count == 0 and (creating > 0 or allocating > 0)
        priority = str(group.get("priority") or "").lower()
        pending_slots.append(
            {
                "slot": slot,
                "org": public_org,
                "display": group.get("display_name"),
                "priority": priority,
                "status": status,
                "running": running,
                "creating": creating,
                "allocating": allocating,
                "stopping": stopping,
                "instance_count": instance_count,
                "empty_pending": empty_pending,
                "requested_gpus": gpu_names(resources.get("gpu_classes") or []),
                "live": slot in known_slots,
                "state_age_seconds": round(age, 1) if age is not None else None,
            }
        )
        if slot not in known_slots and (running > 0 or creating > 0 or allocating > 0):
            stuck_item = {
                "slot": slot,
                "org": public_org,
                "priority": priority,
                "status": status,
                "running": running,
                "creating": creating,
                "allocating": allocating,
                "instance_count": instance_count,
                "empty_pending": empty_pending,
                "state_age_seconds": round(age, 1) if age is not None else None,
                "requested_gpus": gpu_names(resources.get("gpu_classes") or []),
            }
            stuck_after = EMPTY_STUCK_NON_LIVE_SECONDS if empty_pending else STUCK_NON_LIVE_SECONDS
            if age is not None and age >= stuck_after:
                stuck_non_live.append(stuck_item)
        if running > 0 and slot not in known_slots and age is not None and age >= RUNNING_NO_LIVE_GRACE_SECONDS:
            effective_age, recent_action = effective_state_age_seconds(public_org or "", slot, age)
            cost_day = 24 * fallback_hourly(group, public_org, catalogs) * running
            item = {
                "slot": slot,
                "org": public_org,
                "priority": priority,
                "cost_day": cost_day,
                "state_age_seconds": round(age, 1),
                "effective_age_seconds": round(effective_age, 1) if effective_age is not None else None,
                "grace_seconds": RUNNING_NO_LIVE_GRACE_SECONDS,
            }
            if recent_action:
                item["recent_action"] = {
                    "action": recent_action.get("action"),
                    "reason": recent_action.get("reason"),
                    "candidate": recent_action.get("candidate"),
                    "age_seconds": round(float(recent_action.get("age_seconds") or 0), 1),
                }
            running_no_live.append(item)
            rows.append(
                {
                    "worker": "NO_POOL_HASHRATE",
                    "slot": slot,
                    "gpu": "requested",
                    "gpu_token": "requested",
                    "th": 0,
                    "org": public_org,
                    "priority": priority,
                    "cost_day": cost_day,
                    "prl_day": 0,
                    "revenue_day": 0,
                    "market_revenue_day": 0,
                    "profit_day": -cost_day,
                    "market_profit_day": -cost_day,
                }
            )

    fresh_slots_by_org: dict[str, set[str]] = {}
    fresh_named_slots_by_org: dict[str, set[str]] = {}
    fresh_unmapped_named_slots_by_org: dict[str, set[str]] = {}
    for row in rows:
        if str(row.get("worker") or "") == "NO_POOL_HASHRATE":
            continue
        org = str(row.get("org") or "")
        slot = str(row.get("slot") or "")
        if org and slot:
            fresh_slots_by_org.setdefault(org, set()).add(slot)
    for worker in workers:
        named_slot = str(worker.get("named_slot") or "")
        if not named_slot:
            continue
        for label, _org, _key_env, slots in accounts:
            if named_slot in slots:
                fresh_named_slots_by_org.setdefault(label, set()).add(named_slot)
                if not worker.get("slot"):
                    fresh_unmapped_named_slots_by_org.setdefault(label, set()).add(named_slot)
                break
    stale_slots_by_org: dict[str, set[str]] = {}
    for worker in stale_current_workers:
        slot = str(worker.get("slot") or "")
        if not slot:
            continue
        for label, _org, _key_env, slots in accounts:
            if slot in slots:
                stale_slots_by_org.setdefault(label, set()).add(slot)
                break
    active_non_fresh_by_org: dict[str, list[str]] = {}
    active_slots_by_org: dict[str, set[str]] = {}
    for slot in pending_slots:
        org = str(slot.get("org") or "")
        name = str(slot.get("slot") or "")
        if not org or not name:
            continue
        active = any(int(slot.get(key) or 0) > 0 for key in ("running", "creating", "allocating"))
        if not active:
            continue
        active_slots_by_org.setdefault(org, set()).add(name)
        if name not in fresh_slots_by_org.get(org, set()):
            active_non_fresh_by_org.setdefault(org, []).append(name)

    org_discrepancies = []
    for label, _org, _key_env, slots in accounts:
        fresh_slots = fresh_slots_by_org.get(label, set())
        fresh_unmapped_named_slots = sorted(fresh_unmapped_named_slots_by_org.get(label, set()))
        stale_slots = stale_slots_by_org.get(label, set())
        active_slots = active_slots_by_org.get(label, set())
        active_non_fresh_slots = sorted(active_non_fresh_by_org.get(label, []))
        running_no_live_slots = sorted(
            str(item.get("slot") or "")
            for item in running_no_live
            if str(item.get("org") or "") == label and item.get("slot")
        )
        stuck_non_live_slots = sorted(
            str(item.get("slot") or "")
            for item in stuck_non_live
            if str(item.get("org") or "") == label and item.get("slot")
        )
        org_discrepancies.append(
            {
                "org": label,
                "configured_slots": len(slots),
                "salad_slots_seen": sum(1 for slot in slots if slot in groups),
                "active_salad_slots": len(active_slots),
                "fresh_pool_slots": len(fresh_slots),
                "fresh_pool_mapped_slots": len(fresh_slots),
                "fresh_pool_named_unmapped_slots": len(fresh_unmapped_named_slots),
                "stale_pool_slots": len(stale_slots),
                "active_without_fresh_pool": len(active_non_fresh_slots),
                "running_no_live_slots": len(running_no_live_slots),
                "stuck_non_live_slots": len(stuck_non_live_slots),
                "active_without_fresh_pool_slots": active_non_fresh_slots,
                "fresh_pool_named_unmapped_slot_names": fresh_unmapped_named_slots,
                "billable_no_live_slots": running_no_live_slots,
                "stuck_slots": stuck_non_live_slots,
            }
        )

    totals = {key: sum(float(row[key]) for row in rows) for key in ("th", "prl_day", "revenue_day", "cost_day", "profit_day")}
    market_revenue_day = totals["prl_day"] * market_price
    unmapped_totals = {
        "count": len(unmapped_workers),
        "th": sum(float(row["th"]) for row in unmapped_workers),
        "prl_day": sum(float(row["prl_day"]) for row in unmapped_workers),
        "revenue_day": sum(float(row["revenue_day"]) for row in unmapped_workers),
        "market_revenue_day": sum(float(row["market_revenue_day"]) for row in unmapped_workers),
    }

    by_gpu: dict[tuple[str, str], dict[str, float | int]] = {}
    for row in rows:
        key = (str(row.get("gpu_token") or row.get("gpu") or "unknown"), str(row.get("priority") or "?"))
        item = by_gpu.setdefault(key, {"count": 0, "th": 0.0, "cost_day": 0.0, "profit_day": 0.0})
        item["count"] = int(item["count"]) + 1
        item["th"] = float(item["th"]) + float(row["th"])
        item["cost_day"] = float(item["cost_day"]) + float(row["cost_day"])
        item["profit_day"] = float(item["profit_day"]) + float(row["profit_day"])

    result = {
        "at_utc": snapshot_at.isoformat(timespec="seconds"),
        "assumed_prl_price": price,
        "live_market_prl_price": market_price,
        "pool_fee_rate": pool_fee_rate,
        "reward_calibration_factor": REWARD_CALIBRATION_FACTOR,
        "hourly_points": hourly_points,
        "prl_per_th_day_net": prl_per_th_day,
        "wallet_observed_rewards": observed_rewards,
        "fresh_workers": len(workers),
        "pool_worker_count": len(pool_workers),
        "pool_stale_worker_count": sum(1 for worker in pool_workers if worker.get("stale")),
        "stale_current_workers": [
            {
                "worker": row["worker"],
                "slot": row.get("slot"),
                "gpu": row.get("gpu_token") or row.get("gpu"),
                "named_slot": row.get("named_slot"),
                "inactive_named_slot": row.get("inactive_named_slot"),
                "th": round(float(row["th"]), 3),
                "last_stats_at": row.get("last_stats_at"),
            }
            for row in stale_current_workers
        ],
        "org_discrepancies": org_discrepancies,
        "running_no_live_billable_slots": running_no_live,
        "stuck_non_live_slots": stuck_non_live,
        "totals": {
            **{key: round(value, 6) for key, value in totals.items()},
            "market_revenue_day": round(market_revenue_day, 6),
            "market_profit_day": round(market_revenue_day - totals["cost_day"], 6),
        },
        "unmapped_live_workers": [
            {
                "worker": row["worker"],
                "gpu": row.get("gpu_token") or row.get("gpu"),
                "th": round(float(row["th"]), 3),
                "prl_day": round(float(row["prl_day"]), 6),
                "revenue_day": round(float(row["revenue_day"]), 6),
                "market_revenue_day": round(float(row["market_revenue_day"]), 6),
                "last_stats_at": row.get("last_stats_at"),
            }
            for row in unmapped_workers
        ],
        "unmapped_totals": {
            key: round(value, 6) if isinstance(value, float) else value
            for key, value in unmapped_totals.items()
        },
        "totals_if_unmapped_unbilled": {
            "th": round(totals["th"] + unmapped_totals["th"], 6),
            "prl_day": round(totals["prl_day"] + unmapped_totals["prl_day"], 6),
            "revenue_day": round(totals["revenue_day"] + unmapped_totals["revenue_day"], 6),
            "cost_day": round(totals["cost_day"], 6),
            "profit_day": round(totals["profit_day"] + unmapped_totals["revenue_day"], 6),
            "market_revenue_day": round(market_revenue_day + unmapped_totals["market_revenue_day"], 6),
            "market_profit_day": round(
                market_revenue_day + unmapped_totals["market_revenue_day"] - totals["cost_day"],
                6,
            ),
        },
        "by_gpu_priority": [
            {
                "gpu": gpu,
                "priority": priority,
                **{key: round(value, 6) if isinstance(value, float) else value for key, value in item.items()},
            }
            for (gpu, priority), item in sorted(by_gpu.items())
        ],
        "slots": [
            {
                "worker": row.get("worker"),
                "slot": row["slot"],
                "org": row.get("org"),
                "gpu": row.get("gpu_token"),
                "priority": row.get("priority"),
                "th": round(float(row["th"]), 3),
                "cost_day": round(float(row["cost_day"]), 3),
                "market_profit_day": round(float(row.get("market_profit_day", row["profit_day"])), 3),
                "profit_day": round(float(row["profit_day"]), 3),
                "last_stats_at": row.get("last_stats_at"),
            }
            for row in rows
        ],
        "pending_slots": pending_slots,
    }
    append_snapshot_csv(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--price", type=float, default=default_snapshot_price())
    args = parser.parse_args()
    print(json.dumps(build_snapshot(args.price), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
