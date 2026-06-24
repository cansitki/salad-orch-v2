#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ENV = pathlib.Path(os.environ.get("SALAD_PRL_ENV", str(REPO_ROOT / ".env")))


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

ACCOUNTS = [
    ("kray", "kray", "SALAD_API_KEY_2", [f"prl-kray-roi-{index:02d}" for index in range(1, 11)]),
    ("kry1", "kry1", "SALAD_API_KEY_KRY1", [f"prl-kry1-roi-{index:02d}" for index in range(1, 11)]),
    ("kray2", "kray2", "SALAD_API_KEY_2", [f"prl-kray2-roi-{index:02d}" for index in range(1, 11)]),
    ("kray3", "kray3", "SALAD_API_KEY_2", [f"prl-kray3-roi-{index:02d}" for index in range(1, 11)]),
]
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
    return gross * (1 - fee), points, fee


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


def parse_workers() -> list[dict[str, Any]]:
    payload = external_json(f"https://pearlfortune.org/api/v1/miners/{WALLET}/connections")
    workers = ((payload.get("data") or {}).get("workers") or [])
    rows: list[dict[str, Any]] = []
    for worker in workers:
        if worker.get("stale"):
            continue
        name = str(worker.get("worker") or "")
        th = float(worker.get("reported_hashrate") or 0) / 1e12
        if th <= 0:
            continue
        gpu = (((worker.get("client_info") or {}).get("gpus") or [{}])[0]).get("model") or ""
        gpu_id, gpu_token = gpu_from_model(gpu)
        rows.append(
            {
                "worker": name,
                "slot": None,
                "gpu": gpu,
                "gpu_id": gpu_id,
                "gpu_token": gpu_token,
                "th": th,
                "last_stats_at": worker.get("last_stats_at"),
            }
        )
    return rows


def build_snapshot(price: float) -> dict[str, Any]:
    load_env()
    if not WALLET or WALLET == "prl1...":
        raise RuntimeError("PRL_WALLET must be set in the environment or .env file")
    prl_per_th_day, hourly_points, pool_fee_rate = pool_prl_per_th_day()
    market_price = market_prl_price_usd()

    catalogs: dict[str, dict[str, dict[str, float]]] = {}
    groups: dict[str, tuple[str, str, dict[str, Any]]] = {}
    group_instance_ids: dict[str, set[str]] = {}
    for label, org, key_env, slots in ACCOUNTS:
        api_key = os.environ[key_env]
        catalogs[label] = price_catalog(org, api_key)
        for slot in slots:
            try:
                groups[slot] = (label, org, salad_json(f"/organizations/{org}/projects/default/containers/{slot}", api_key))
                instances_payload = salad_json(
                    f"/organizations/{org}/projects/default/containers/{slot}/instances",
                    api_key,
                )
                group_instance_ids[slot] = {
                    str(item.get("id"))
                    for item in (instances_payload.get("items") or instances_payload.get("instances") or [])
                    if item.get("id")
                }
            except Exception:
                continue

    workers = parse_workers()
    for row in workers:
        worker_instance = worker_instance_id(str(row.get("worker") or ""))
        for _label, _org, _key_env, slots in ACCOUNTS:
            for slot in slots:
                if slot in row["worker"] and worker_instance and worker_instance in group_instance_ids.get(slot, set()):
                    row["slot"] = slot
                    break
            if row["slot"]:
                break

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
    for slot, (public_org, _api_org, group) in groups.items():
        current = group.get("current_state") or {}
        counts = current.get("instance_status_counts") or {}
        resources = ((group.get("container") or {}).get("resources") or {})
        status = str(current.get("status") or "").lower()
        running = int(counts.get("running_count") or 0)
        creating = int(counts.get("creating_count") or 0)
        allocating = int(counts.get("allocating_count") or 0)
        stopping = int(counts.get("stopping_count") or 0)
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
                "requested_gpus": gpu_names(resources.get("gpu_classes") or []),
                "live": slot in known_slots,
            }
        )
        if running > 0 and slot not in known_slots:
            cost_day = 24 * fallback_hourly(group, public_org, catalogs) * running
            running_no_live.append({"slot": slot, "org": public_org, "priority": priority, "cost_day": cost_day})
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

    return {
        "at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "assumed_prl_price": price,
        "live_market_prl_price": market_price,
        "pool_fee_rate": pool_fee_rate,
        "hourly_points": hourly_points,
        "prl_per_th_day_net": prl_per_th_day,
        "fresh_workers": len(workers),
        "running_no_live_billable_slots": running_no_live,
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
            }
            for row in rows
        ],
        "pending_slots": pending_slots,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--price", type=float, default=0.62)
    args = parser.parse_args()
    print(json.dumps(build_snapshot(args.price), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
