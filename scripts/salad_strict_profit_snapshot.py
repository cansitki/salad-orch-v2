#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import os
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import salad_prl_profit_snapshot as base
from config_loader import load_config
from fleet_common import json_dumps


ACTIVE_STATUSES = {"deploying", "running", "allocating", "creating"}


def active_count_for_group(group: dict[str, Any]) -> tuple[str, int]:
    current = group.get("current_state") or {}
    counts = current.get("instance_status_counts") or {}
    status = str(current.get("status") or "").lower()
    active_instances = sum(
        int(counts.get(key) or 0)
        for key in ("running_count", "creating_count", "allocating_count")
    )
    if active_instances > 0:
        return status, active_instances
    return status, 1 if status in ACTIVE_STATUSES else 0


def worker_revenue_by_slot(accounts: list[tuple[str, str, str, list[str]]], prl_per_th_day: float, price: float) -> dict[str, dict[str, float]]:
    configured_slots = {slot for _label, _org, _key_env, slots in accounts for slot in slots}
    revenue: dict[str, dict[str, float]] = {}
    for worker in base.parse_workers():
        if worker.get("stale") or float(worker.get("th") or 0) <= 0:
            continue
        named_slot = base.worker_named_slot(str(worker.get("worker") or ""), accounts)
        if named_slot not in configured_slots:
            continue
        th = float(worker.get("th") or 0)
        item = revenue.setdefault(named_slot, {"workers": 0, "th": 0.0, "prl_day": 0.0, "revenue_day": 0.0})
        item["workers"] += 1
        item["th"] += th
        item["prl_day"] += th * prl_per_th_day
        item["revenue_day"] += th * prl_per_th_day * price
    return revenue


def fetch_slot_cost(org: Any, api_key: str, catalog: dict[str, dict[str, float]], slot_name: str) -> dict[str, Any]:
    group = base.salad_json(f"/organizations/{org.slug}/projects/default/containers/{slot_name}", api_key)
    status, active_instances = active_count_for_group(group)
    priority = str(group.get("priority") or "").lower()
    resources = ((group.get("container") or {}).get("resources") or {})
    hourly = base.fallback_hourly(group, org.label, {org.label: catalog})
    gpus = base.gpu_names(resources.get("gpu_classes") or [])
    return {
        "org": org.label,
        "slot": slot_name,
        "status": status,
        "active_instances": active_instances,
        "priority": priority,
        "gpus": gpus,
        "hourly_usd": hourly,
        "cost_day": 24.0 * hourly * active_instances,
    }


def org_snapshot(org: Any, *, price: float, prl_per_th_day: float, max_workers: int) -> dict[str, Any]:
    api_key = os.environ[org.api_key_env]
    accounts = [(org.label, org.slug, org.api_key_env, org.slot_names())]
    catalog = base.price_catalog(org.slug, api_key)
    revenue_by_slot = worker_revenue_by_slot(accounts, prl_per_th_day, price)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        rows = list(
            executor.map(
                lambda slot_name: fetch_slot_cost(org, api_key, catalog, slot_name),
                org.slot_names(),
            )
        )

    cost_by_gpu: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"active_slots": 0, "cost_day": 0.0}
    )
    for row in rows:
        revenue = revenue_by_slot.get(row["slot"], {})
        row["workers"] = int(revenue.get("workers") or 0)
        row["th"] = float(revenue.get("th") or 0.0)
        row["prl_day"] = float(revenue.get("prl_day") or 0.0)
        row["revenue_day"] = float(revenue.get("revenue_day") or 0.0)
        row["profit_day"] = float(row["revenue_day"]) - float(row["cost_day"])
        if int(row["active_instances"] or 0) > 0:
            key = f"{'+'.join(row['gpus']) or 'none'}:{row['priority']}"
            cost_by_gpu[key]["active_slots"] = int(cost_by_gpu[key]["active_slots"]) + int(row["active_instances"])
            cost_by_gpu[key]["cost_day"] = float(cost_by_gpu[key]["cost_day"]) + float(row["cost_day"])

    totals = {
        "active_slots": sum(1 for row in rows if int(row["active_instances"] or 0) > 0),
        "active_instances": sum(int(row["active_instances"] or 0) for row in rows),
        "workers": sum(int(row["workers"] or 0) for row in rows),
        "th": sum(float(row["th"] or 0.0) for row in rows),
        "prl_day": sum(float(row["prl_day"] or 0.0) for row in rows),
        "revenue_day": sum(float(row["revenue_day"] or 0.0) for row in rows),
        "cost_day": sum(float(row["cost_day"] or 0.0) for row in rows),
    }
    totals["profit_day"] = totals["revenue_day"] - totals["cost_day"]

    return {
        "org": org.label,
        "totals": totals,
        "cost_by_gpu": [
            {"profile": key, **value}
            for key, value in sorted(cost_by_gpu.items(), key=lambda item: float(item[1]["cost_day"]), reverse=True)
        ],
        "worst_slots": sorted(rows, key=lambda row: float(row["profit_day"]))[:30],
        "slots": rows,
    }


def build_snapshot(org_filter: set[str] | None = None, *, max_workers: int = 16) -> dict[str, Any]:
    base.load_env()
    config = load_config()
    price = base.market_prl_price_usd()
    prl_per_th_day, hourly_points, pool_fee_rate = base.pool_prl_per_th_day()
    orgs = [org for org in config.enabled_orgs() if not org_filter or org.label in org_filter]
    orgs = [org for org in orgs if os.environ.get(org.api_key_env)]
    orgs_payload = [
        org_snapshot(org, price=price, prl_per_th_day=prl_per_th_day, max_workers=max_workers)
        for org in orgs
    ]
    totals = {
        "active_slots": sum(int(org["totals"]["active_slots"]) for org in orgs_payload),
        "active_instances": sum(int(org["totals"]["active_instances"]) for org in orgs_payload),
        "workers": sum(int(org["totals"]["workers"]) for org in orgs_payload),
        "th": sum(float(org["totals"]["th"]) for org in orgs_payload),
        "prl_day": sum(float(org["totals"]["prl_day"]) for org in orgs_payload),
        "revenue_day": sum(float(org["totals"]["revenue_day"]) for org in orgs_payload),
        "cost_day": sum(float(org["totals"]["cost_day"]) for org in orgs_payload),
    }
    totals["profit_day"] = totals["revenue_day"] - totals["cost_day"]
    return {
        "at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "price_usd": price,
        "prl_per_th_day": prl_per_th_day,
        "pool_fee_rate": pool_fee_rate,
        "pool_summary_points": hourly_points,
        "totals": totals,
        "orgs": orgs_payload,
    }


def parse_org_filter(values: list[str]) -> set[str] | None:
    selected = {item.strip() for value in values for item in value.split(",") if item.strip()}
    return selected or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Strict Salad-only profit snapshot using active Salad cost and named Pearl workers.")
    parser.add_argument("--org", action="append", default=[], help="Org label to include. Can be repeated or comma-separated.")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = build_snapshot(parse_org_filter(args.org), max_workers=args.workers)
    if args.json:
        print(json_dumps(payload))
        return
    totals = payload["totals"]
    print(
        f"strict_profit price=${float(payload['price_usd']):.4f} "
        f"active={totals['active_slots']} workers={totals['workers']} "
        f"cost=${float(totals['cost_day']):.2f}/day "
        f"revenue=${float(totals['revenue_day']):.2f}/day "
        f"profit=${float(totals['profit_day']):.2f}/day"
    )
    for org in payload["orgs"]:
        item = org["totals"]
        print(
            f"{org['org']}: active={item['active_slots']} workers={item['workers']} "
            f"cost=${float(item['cost_day']):.2f}/day revenue=${float(item['revenue_day']):.2f}/day "
            f"profit=${float(item['profit_day']):.2f}/day"
        )


if __name__ == "__main__":
    main()
