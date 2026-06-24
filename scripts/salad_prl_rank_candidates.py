#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
WATCH_PATH = SCRIPT_DIR / "salad_prl_watch.py"


def load_watch_module(price: float, org: str, timeout_seconds: float):
    os.environ.setdefault("PRL_WATCH_ORG", org)
    os.environ.setdefault("PRL_WATCH_PUBLIC_ORG", org)
    os.environ.setdefault("PRL_WATCH_FIXED_DECISION_PRICE_USD", str(price))
    os.environ.setdefault("PRL_WATCH_MIN_PROFIT_USD_DAY", "0.05")
    os.environ.setdefault("PRL_WATCH_ALLOWED_PRIORITIES", "batch,low")
    os.environ.setdefault("PRL_WATCH_HTTP_TIMEOUT_SECONDS", str(timeout_seconds))

    spec = importlib.util.spec_from_file_location("salad_prl_watch_rank_source", WATCH_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {WATCH_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["salad_prl_watch_rank_source"] = module
    spec.loader.exec_module(module)

    module.PRICE_GUARD_FIXED_DECISION_PRICE_USD = price
    module.WATCH_HTTP_TIMEOUT_SECONDS = timeout_seconds
    module.PRICE_GUARD_CACHE["decision_price_usd"] = price
    return module


def unique_single_gpu_candidates(watch: Any) -> list[Any]:
    rows: list[Any] = []
    seen: set[tuple[Any, ...]] = set()
    for candidate in list(watch.FALLBACKS) + list(watch.INITIAL.values()):
        key = (candidate.gpu_keys, candidate.priority, candidate.memory, candidate.label)
        if key in seen or len(candidate.gpu_keys) != 1:
            continue
        seen.add(key)
        if not watch.candidate_allowed(candidate):
            continue
        rows.append(candidate)
    return rows


def build_ranking(watch: Any, *, include_availability: bool, availability_slot: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in unique_single_gpu_candidates(watch):
        estimate = watch.candidate_profit_estimate(candidate)
        if estimate is None:
            continue
        row: dict[str, Any] = {
            "candidate": candidate.label,
            "gpu": estimate["gpu"],
            "priority": estimate["priority"],
            "memory": candidate.memory,
            "expected_th": round(float(estimate["expected_th"]), 3),
            "cost_day": round(float(estimate["cost_day"]), 3),
            "revenue_day": round(float(estimate["revenue_day"]), 3),
            "profit_day": round(float(estimate["profit_day"]), 3),
        }
        if include_availability:
            try:
                row["available_now"] = watch.candidate_availability(availability_slot, candidate)
            except Exception as exc:
                row["available_error"] = type(exc).__name__
        rows.append(row)
    rows.sort(key=lambda item: float(item["profit_day"]), reverse=True)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank Salad PRL GPU candidates by estimated daily profit.")
    parser.add_argument("--price", type=float, default=0.64, help="Decision PRL/USD price for profit calculation.")
    parser.add_argument("--org", default=os.environ.get("PRL_WATCH_ORG", "kray"), help="Salad org for price catalog lookups.")
    parser.add_argument("--min-profit", type=float, default=0.05, help="Profit threshold used for the profitable flag.")
    parser.add_argument("--availability", action="store_true", help="Also query Salad availability. This can be slow.")
    parser.add_argument("--availability-slot", default="prl-kray-roi-01", help="Slot name used for availability checks.")
    parser.add_argument("--http-timeout", type=float, default=4.0, help="HTTP timeout for optional live calls.")
    args = parser.parse_args()

    os.environ["PRL_WATCH_MIN_PROFIT_USD_DAY"] = str(args.min_profit)
    watch = load_watch_module(args.price, args.org, args.http_timeout)
    ranking = build_ranking(watch, include_availability=args.availability, availability_slot=args.availability_slot)
    print(
        json.dumps(
            {
                "decision_price": args.price,
                "min_profit_day": args.min_profit,
                "org": args.org,
                "availability_included": args.availability,
                "ranking": ranking,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
