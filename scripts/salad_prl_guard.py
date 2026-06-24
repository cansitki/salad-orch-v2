#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import time
from datetime import UTC, datetime
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
STATE_DIR = pathlib.Path(os.environ.get("SALAD_PRL_STATE_DIR", str(REPO_ROOT / "state")))
SNAPSHOT_PATH = pathlib.Path(os.environ.get("PRL_SNAPSHOT_PATH", str(SCRIPT_DIR / "salad_prl_profit_snapshot.py")))
KRAY2_PATH = pathlib.Path(os.environ.get("PRL_WATCH_SCRIPT_PATH", str(SCRIPT_DIR / "salad_prl_watch.py")))
LOG = pathlib.Path(os.environ.get("PRL_GUARD_LOG", str(STATE_DIR / "logs" / "prl_nohash_guard.log")))
FALLBACK_PRL_PRICE = float(os.environ.get("PRL_NOHASH_FALLBACK_PRICE", "0.62"))
PRICE_BAND_USD = float(os.environ.get("PRL_PRICE_BAND_USD", "0.02"))
DECISION_PRICE_CAP_USD = float(os.environ.get("PRL_DECISION_PRICE_CAP_USD", "0.63"))
FIXED_DECISION_PRICE_USD = float(os.environ.get("PRL_FIXED_DECISION_PRICE_USD", "0.62"))
POLL_SECONDS = int(os.environ.get("PRL_NOHASH_POLL_SECONDS", "20"))
NO_HASH_GRACE_SECONDS = int(os.environ.get("PRL_NOHASH_GRACE_SECONDS", "120"))
FORCE_NO_HASH_GRACE_SECONDS = int(os.environ.get("PRL_NOHASH_FORCE_GRACE_SECONDS", "90"))
NEGATIVE_PROFIT_GRACE_SECONDS = int(os.environ.get("PRL_NOHASH_NEGATIVE_GRACE_SECONDS", "60"))
NEGATIVE_SLOT_GRACE_SECONDS = int(os.environ.get("PRL_NEGATIVE_SLOT_GRACE_SECONDS", "120"))
NEGATIVE_SLOT_PROFIT_DAY = float(os.environ.get("PRL_NEGATIVE_SLOT_PROFIT_DAY", "0.01"))
UNDERPERFORM_GRACE_SECONDS = int(os.environ.get("PRL_UNDERPERFORM_GRACE_SECONDS", "120"))
UNDERPERFORM_RATIO = float(os.environ.get("PRL_UNDERPERFORM_RATIO", "0.85"))
UNDERPERFORM_MIN_DEFICIT_TH = float(os.environ.get("PRL_UNDERPERFORM_MIN_DEFICIT_TH", "10"))
GLOBAL_POOL_MIN_FRESH_WORKERS = 3
SEEN_SINCE: dict[tuple[str, str], float] = {}
NEGATIVE_SLOT_SEEN_SINCE: dict[tuple[str, str], float] = {}
UNDERPERFORM_SLOT_SEEN_SINCE: dict[tuple[str, str], float] = {}
INCLUDE_BMU = os.environ.get("PRL_INCLUDE_BMU", "").lower() in {"1", "true", "yes"}
ENABLED_ORGS = tuple(
    org.strip()
    for org in os.environ.get("PRL_GUARD_ORGS", "kray,kray2,kray3").split(",")
    if org.strip()
)


WATCH_COMMON_ENV = {
    "PRL_WATCH_ALLOWED_PRIORITIES": os.environ.get("PRL_WATCH_ALLOWED_PRIORITIES", "batch"),
    "PRL_WATCH_BLOCKED_PROFILES": os.environ.get(
        "PRL_WATCH_BLOCKED_PROFILES",
        "4080:low,4070tis:low,5070:low,5090:low",
    ),
    "PRL_WATCH_MIN_PROFIT_USD_DAY": os.environ.get("PRL_WATCH_MIN_PROFIT_USD_DAY", "0.01"),
    "PRL_WATCH_PRICE_BAND_USD": os.environ.get("PRL_WATCH_PRICE_BAND_USD", "0.02"),
    "PRL_WATCH_DECISION_PRICE_CAP_USD": os.environ.get(
        "PRL_WATCH_DECISION_PRICE_CAP_USD",
        str(DECISION_PRICE_CAP_USD),
    ),
    "PRL_WATCH_FIXED_DECISION_PRICE_USD": os.environ.get(
        "PRL_WATCH_FIXED_DECISION_PRICE_USD",
        str(FIXED_DECISION_PRICE_USD),
    ),
    "PRL_WATCH_MINER_RELEASE_TAG": os.environ.get("PRL_WATCH_MINER_RELEASE_TAG", "v.1.1.8"),
    "PRL_WATCH_MINER_PACKAGE_VERSION": os.environ.get("PRL_WATCH_MINER_PACKAGE_VERSION", "v1.1.8"),
    "PRL_WATCH_MINER_BINARY": os.environ.get("PRL_WATCH_MINER_BINARY", "miner-cuda12"),
    "PRL_WATCH_COORDINATED_ORGS": os.environ.get("PRL_WATCH_COORDINATED_ORGS", ",".join(ENABLED_ORGS)),
}


KRAY_WATCH_ENV = {
    **WATCH_COMMON_ENV,
    "PRL_WATCH_NAME": "kray-prl-watch",
    "PRL_WATCH_LOG": str(STATE_DIR / "logs" / "kray_prl_watch.log"),
    "PRL_WATCH_ORG": "kray",
    "PRL_WATCH_API_KEY_ENV": "SALAD_API_KEY_2",
    "PRL_WATCH_SLOTS": ",".join(f"prl-kray-roi-{index:02d}" for index in range(1, 11)),
    "PRL_WATCH_WORKER_PREFIX": "kray-prl",
    "PRL_WATCH_WORKER_SLOT_PREFIX": "kray-roi-",
    "PRL_WATCH_POOL_WORKER_PREFIX": "kray-prl-kray",
    "PRL_WATCH_DISPLAY_PREFIX": "PearlFortune KRAY",
}


KRY1_WATCH_ENV = {
    **WATCH_COMMON_ENV,
    "PRL_WATCH_NAME": "kry1-prl-watch",
    "PRL_WATCH_LOG": str(STATE_DIR / "logs" / "kry1_prl_watch.log"),
    "PRL_WATCH_ORG": "kry1",
    "PRL_WATCH_PUBLIC_ORG": "kry1",
    "PRL_WATCH_API_KEY_ENV": "SALAD_API_KEY_KRY1",
    "PRL_WATCH_SLOTS": ",".join(f"prl-kry1-roi-{index:02d}" for index in range(1, 11)),
    "PRL_WATCH_WORKER_PREFIX": "kry1-prl",
    "PRL_WATCH_WORKER_SLOT_PREFIX": "kry1-roi-",
    "PRL_WATCH_POOL_WORKER_PREFIX": "kry1-prl-kry1",
    "PRL_WATCH_DISPLAY_PREFIX": "PearlFortune KRY1",
}


KRAY2_WATCH_ENV = {
    **WATCH_COMMON_ENV,
    "PRL_WATCH_NAME": "kray2-prl-watch",
    "PRL_WATCH_LOG": str(STATE_DIR / "logs" / "kray2_prl_watch.log"),
    "PRL_WATCH_ORG": "kray2",
    "PRL_WATCH_PUBLIC_ORG": "kray2",
    "PRL_WATCH_API_KEY_ENV": "SALAD_API_KEY_2",
    "PRL_WATCH_SLOTS": ",".join(f"prl-kray2-roi-{index:02d}" for index in range(1, 11)),
    "PRL_WATCH_WORKER_PREFIX": "kray2-prl",
    "PRL_WATCH_WORKER_SLOT_PREFIX": "kray2-roi-",
    "PRL_WATCH_POOL_WORKER_PREFIX": "kray2-prl-kray2",
    "PRL_WATCH_DISPLAY_PREFIX": "PearlFortune KRAY2",
}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(event: str, **fields: Any) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now(), "event": event, **fields}, sort_keys=True) + "\n")


BMU_WATCH_ENV = {
    "PRL_WATCH_NAME": "bmu-prl-watch",
    "PRL_WATCH_LOG": str(STATE_DIR / "logs" / "bmu_prl_watch.log"),
    "PRL_WATCH_ORG": "bmu",
    "PRL_WATCH_API_KEY_ENV": "SALAD_API_KEY",
    "PRL_WATCH_SLOTS": ",".join(f"prl-roi-fresh-{index:02d}" for index in range(1, 7)),
    "PRL_WATCH_WORKER_PREFIX": "bmu",
    "PRL_WATCH_WORKER_SLOT_PREFIX": "prl-roi-fresh-",
    "PRL_WATCH_POOL_WORKER_PREFIX": "bmu-prl-roi-fresh",
    "PRL_WATCH_DISPLAY_PREFIX": "PearlFortune BMU",
}


KRAY3_WATCH_ENV = {
    **WATCH_COMMON_ENV,
    "PRL_WATCH_NAME": "kray3-prl-watch",
    "PRL_WATCH_LOG": str(STATE_DIR / "logs" / "kray3_prl_watch.log"),
    "PRL_WATCH_ORG": "kray3",
    "PRL_WATCH_API_KEY_ENV": "SALAD_API_KEY_2",
    "PRL_WATCH_SLOTS": ",".join(f"prl-kray3-roi-{index:02d}" for index in range(1, 11)),
    "PRL_WATCH_WORKER_PREFIX": "kray3-prl",
    "PRL_WATCH_WORKER_SLOT_PREFIX": "kray3-roi-",
    "PRL_WATCH_POOL_WORKER_PREFIX": "kray3-prl-kray3",
    "PRL_WATCH_DISPLAY_PREFIX": "PearlFortune KRAY3",
}


def bmu_extra_watch_env(org: str) -> dict[str, str]:
    return {
        **WATCH_COMMON_ENV,
        "PRL_WATCH_NAME": f"{org}-prl-watch",
        "PRL_WATCH_LOG": str(STATE_DIR / "logs" / f"{org}_prl_watch.log"),
        "PRL_WATCH_ORG": org,
        "PRL_WATCH_API_KEY_ENV": "SALAD_API_KEY",
        "PRL_WATCH_SLOTS": ",".join(f"prl-{org}-roi-{index:02d}" for index in range(1, 11)),
        "PRL_WATCH_WORKER_PREFIX": f"{org}-prl",
        "PRL_WATCH_WORKER_SLOT_PREFIX": f"{org}-roi-",
        "PRL_WATCH_POOL_WORKER_PREFIX": f"{org}-prl-{org}",
        "PRL_WATCH_DISPLAY_PREFIX": f"PearlFortune {org.upper()}",
    }


def load_module(path: pathlib.Path, name: str, env: dict[str, str] | None = None):
    old_env: dict[str, str | None] = {}
    if env:
        for key, value in env.items():
            old_env[key] = os.environ.get(key)
            os.environ[key] = value
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        sys.modules[name] = module
        spec.loader.exec_module(module)
        if hasattr(module, "load_env"):
            module.load_env()
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module


snapshot = load_module(SNAPSHOT_PATH, "prl_profit_snapshot_guard")
watchers = {
    "kray": load_module(KRAY2_PATH, "kray_prl_watch_guard", KRAY_WATCH_ENV),
    "kry1": load_module(KRAY2_PATH, "kry1_prl_watch_guard", KRY1_WATCH_ENV),
    "kray2": load_module(KRAY2_PATH, "kray2_prl_watch_guard", KRAY2_WATCH_ENV),
    "kray3": load_module(KRAY2_PATH, "kray3_prl_watch_guard", KRAY3_WATCH_ENV),
}
if INCLUDE_BMU:
    watchers["bmu"] = load_module(KRAY2_PATH, "bmu_prl_watch_guard", BMU_WATCH_ENV)
    for bmu_org in ("bmu2", "bmu3", "bmu4", "bmu5"):
        watchers[bmu_org] = load_module(KRAY2_PATH, f"{bmu_org}_prl_watch_guard", bmu_extra_watch_env(bmu_org))

watchers = {org: module for org, module in watchers.items() if org in ENABLED_ORGS}
snapshot.ACCOUNTS = [
    account
    for account in snapshot.ACCOUNTS
    if len(account) > 1 and str(account[0]) in ENABLED_ORGS
]


def safetrade_prl_price_usd() -> float | None:
    try:
        payload = snapshot.external_json("https://safe.trade/api/v2/peatio/public/markets/prlusdt/tickers")
    except Exception as exc:
        log("decision_price_safetrade_fetch_failed", error=type(exc).__name__, detail=str(exc)[:180])
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


def market_prl_price_usd() -> float | None:
    prices: list[float] = []
    pearl_price: float | None = None
    try:
        pearl_price = float((snapshot.external_json("https://pearlfortune.org/api/v1/market/price").get("data") or {}).get("price_usd") or 0)
        if pearl_price > 0:
            prices.append(pearl_price)
    except Exception as exc:
        log("decision_price_fetch_failed", error=type(exc).__name__, detail=str(exc)[:180])
    safetrade_price = safetrade_prl_price_usd()
    if safetrade_price:
        prices.append(safetrade_price)
    if not prices:
        return None
    price = min(prices)
    if pearl_price and safetrade_price and abs(pearl_price - safetrade_price) >= 0.015:
        log(
            "decision_price_source_spread",
            pearlfortune_price_usd=round(pearl_price, 6),
            safetrade_price_usd=round(safetrade_price, 6),
            selected_price_usd=round(price, 6),
        )
    return price


def decision_prl_price() -> float:
    if FIXED_DECISION_PRICE_USD > 0:
        return FIXED_DECISION_PRICE_USD
    price = market_prl_price_usd()
    if price and price > 0:
        decision_price = max(0.0, price - PRICE_BAND_USD)
    else:
        decision_price = max(0.0, FALLBACK_PRL_PRICE - PRICE_BAND_USD)
    if DECISION_PRICE_CAP_USD > 0:
        decision_price = min(decision_price, DECISION_PRICE_CAP_USD)
    return decision_price


def running_instances(module: Any, slot: str) -> list[dict[str, Any]]:
    _group, instances = module.slot_state(slot)
    return [
        instance
        for instance in instances
        if instance.get("ready")
        or instance.get("started")
        or str(instance.get("state") or "").lower() in {"running", "starting"}
    ]


def retarget_slot(org: str, slot: str, reason: str) -> dict[str, Any] | None:
    module = watchers.get(org)
    if module is None:
        return None
    try:
        group, _instances = module.slot_state(slot)
    except Exception as exc:
        log("slot_retarget_lookup_failed", org=org, slot=slot, reason=reason, error=type(exc).__name__, detail=str(exc)[:180])
        return None

    actual = None
    try:
        if group:
            actual = module.candidate_matching_group(slot, group)
    except Exception as exc:
        log("slot_retarget_match_failed", org=org, slot=slot, reason=reason, error=type(exc).__name__, detail=str(exc)[:180])

    try:
        if actual is not None:
            module.set_current_candidate(slot, actual)
        else:
            module.SLOT_CANDIDATE_INDEX[slot] = -1
        candidate = module.advance_candidate(slot)
    except Exception as exc:
        log("slot_retarget_candidate_failed", org=org, slot=slot, reason=reason, error=type(exc).__name__, detail=str(exc)[:180])
        return None

    if candidate is None:
        log("slot_retarget_no_candidate", org=org, slot=slot, reason=reason)
        return None

    try:
        payload = module.container_payload(slot, candidate)
        payload.pop("name", None)
        module.request(
            "PATCH",
            f"/organizations/{module.ORG}/projects/{module.PROJECT}/containers/{slot}",
            payload,
            patch=True,
        )
        module.SLOT_LAST_PATCH[slot] = time.time()
        module.log(
            "slot_patched",
            slot=slot,
            candidate=candidate.label,
            gpu_ids=candidate.gpu_ids,
            memory=candidate.memory,
            reason=reason,
        )
        log(
            "slot_retargeted",
            org=org,
            slot=slot,
            candidate=candidate.label,
            gpu_ids=candidate.gpu_ids,
            memory=candidate.memory,
            reason=reason,
        )
        return {"candidate": candidate.label, "gpu_ids": candidate.gpu_ids, "memory": candidate.memory}
    except Exception as exc:
        log("slot_retarget_failed", org=org, slot=slot, candidate=candidate.label, reason=reason, error=type(exc).__name__, detail=str(exc)[:180])
        return None


def reallocate_slot(org: str, slot: str, reason: str, *, retarget: bool = True) -> list[dict[str, Any]]:
    module = watchers.get(org)
    if module is None:
        return []
    retargeted = retarget_slot(org, slot, reason) if retarget else None
    actions: list[dict[str, Any]] = []
    for instance in running_instances(module, slot):
        instance_id = str(instance.get("id") or "")
        if not instance_id:
            continue
        module.reallocate(slot, instance_id, reason)
        actions.append(
            {
                "org": org,
                "slot": slot,
                "instance_id": instance_id,
                "state": instance.get("state"),
                "ready": instance.get("ready"),
                "started": instance.get("started"),
                "retargeted": retargeted,
            }
        )
    return actions


def org_for_slot(slot: str) -> str:
    if slot.startswith("prl-kry1-roi-"):
        return "kry1"
    if slot.startswith("prl-kray2-roi-"):
        return "kray2"
    if slot.startswith("prl-kray3-roi-"):
        return "kray3"
    if slot.startswith("prl-kray-roi-"):
        return "kray"
    if slot.startswith("prl-roi-fresh-"):
        return "bmu"
    for org in ("bmu2", "bmu3", "bmu4", "bmu5"):
        if slot.startswith(f"prl-{org}-roi-"):
            return org
    return ""


def expected_th_for_slot(org: str, row: dict[str, Any]) -> float | None:
    module = watchers.get(org)
    if module is None:
        return None
    expected_by_profile = getattr(module, "EXPECTED_TH_BY_PROFILE", {})
    key = (str(row.get("gpu") or "").lower(), str(row.get("priority") or "").lower())
    try:
        expected = float(expected_by_profile.get(key) or 0)
    except (TypeError, ValueError):
        return None
    return expected if expected > 0 else None


def tick() -> None:
    price = decision_prl_price()
    snap = snapshot.build_snapshot(price)
    no_hash = list(snap.get("running_no_live_billable_slots") or [])
    totals = snap.get("totals") or {}
    observed_keys = {(str(item.get("org")), str(item.get("slot"))) for item in no_hash}
    for key in list(SEEN_SINCE):
        if key not in observed_keys:
            SEEN_SINCE.pop(key, None)
    no_hash_slots = {slot for _org, slot in observed_keys}

    if int(snap.get("fresh_workers") or 0) < GLOBAL_POOL_MIN_FRESH_WORKERS:
        log(
            "global_pool_guard_skip",
            fresh_workers=snap.get("fresh_workers"),
            no_hash=no_hash,
            profit_day=totals.get("profit_day"),
        )
        return

    actions: list[dict[str, Any]] = []
    total_no_hash_cost_day = sum(float(item.get("cost_day") or 0) for item in no_hash)
    for item in no_hash:
        org = str(item.get("org") or "")
        slot = str(item.get("slot") or "")
        key = (org, slot)
        first_seen = SEEN_SINCE.setdefault(key, time.time())
        age = time.time() - first_seen
        profit_day = float(totals.get("profit_day") or 0)
        force_now = profit_day < 0 or total_no_hash_cost_day > max(0.0, profit_day)
        grace_seconds = (
            NEGATIVE_PROFIT_GRACE_SECONDS
            if profit_day < 0
            else FORCE_NO_HASH_GRACE_SECONDS
            if force_now
            else NO_HASH_GRACE_SECONDS
        )
        if age < grace_seconds:
            log(
                "no_hash_observed",
                org=org,
                slot=slot,
                age_seconds=round(age, 1),
                grace_seconds=grace_seconds,
                force_pending=force_now,
                cost_day=item.get("cost_day"),
                market_profit_day=totals.get("market_profit_day"),
                profit_day=totals.get("profit_day"),
                decision_price_usd=price,
            )
            continue
        reason = (
            "auto_nohash_guard_negative_profit"
            if profit_day < 0
            else "auto_nohash_guard_profit_drag"
            if force_now
            else f"auto_nohash_guard_{NO_HASH_GRACE_SECONDS}s"
        )
        actions.extend(reallocate_slot(org, slot, reason))
        SEEN_SINCE.pop(key, None)

    negative_rows = []
    for row in snap.get("slots") or []:
        slot = str(row.get("slot") or "")
        if not slot or slot in no_hash_slots or str(row.get("gpu") or "") == "requested":
            continue
        profit_day = float(row.get("profit_day") or 0)
        if profit_day >= NEGATIVE_SLOT_PROFIT_DAY:
            continue
        org = org_for_slot(slot)
        if org not in watchers:
            continue
        negative_rows.append({**row, "org": org})

    negative_keys = {(str(item.get("org")), str(item.get("slot"))) for item in negative_rows}
    for key in list(NEGATIVE_SLOT_SEEN_SINCE):
        if key not in negative_keys:
            NEGATIVE_SLOT_SEEN_SINCE.pop(key, None)

    for item in negative_rows:
        org = str(item.get("org") or "")
        slot = str(item.get("slot") or "")
        key = (org, slot)
        first_seen = NEGATIVE_SLOT_SEEN_SINCE.setdefault(key, time.time())
        age = time.time() - first_seen
        if age < NEGATIVE_SLOT_GRACE_SECONDS:
            log(
                "negative_slot_observed",
                org=org,
                slot=slot,
                age_seconds=round(age, 1),
                grace_seconds=NEGATIVE_SLOT_GRACE_SECONDS,
                profit_day=item.get("profit_day"),
                market_profit_day=item.get("market_profit_day"),
                decision_price_usd=price,
                th=item.get("th"),
                gpu=item.get("gpu"),
                priority=item.get("priority"),
            )
            continue
        actions.extend(reallocate_slot(org, slot, f"auto_negative_slot_guard_{NEGATIVE_SLOT_GRACE_SECONDS}s"))
        NEGATIVE_SLOT_SEEN_SINCE.pop(key, None)

    underperform_rows = []
    for row in snap.get("slots") or []:
        slot = str(row.get("slot") or "")
        if not slot or slot in no_hash_slots or str(row.get("gpu") or "") == "requested":
            continue
        org = org_for_slot(slot)
        if org not in watchers or (org, slot) in negative_keys:
            continue
        expected_th = expected_th_for_slot(org, row)
        if expected_th is None:
            continue
        th = float(row.get("th") or 0)
        deficit_th = expected_th - th
        if th <= 0 or th / expected_th >= UNDERPERFORM_RATIO or deficit_th < UNDERPERFORM_MIN_DEFICIT_TH:
            continue
        underperform_rows.append(
            {
                **row,
                "org": org,
                "expected_th": round(expected_th, 3),
                "underperform_ratio": round(th / expected_th, 4),
                "deficit_th": round(deficit_th, 3),
            }
        )

    underperform_keys = {(str(item.get("org")), str(item.get("slot"))) for item in underperform_rows}
    for key in list(UNDERPERFORM_SLOT_SEEN_SINCE):
        if key not in underperform_keys:
            UNDERPERFORM_SLOT_SEEN_SINCE.pop(key, None)

    for item in underperform_rows:
        org = str(item.get("org") or "")
        slot = str(item.get("slot") or "")
        key = (org, slot)
        first_seen = UNDERPERFORM_SLOT_SEEN_SINCE.setdefault(key, time.time())
        age = time.time() - first_seen
        if age < UNDERPERFORM_GRACE_SECONDS:
            log(
                "underperform_slot_observed",
                org=org,
                slot=slot,
                age_seconds=round(age, 1),
                grace_seconds=UNDERPERFORM_GRACE_SECONDS,
                th=item.get("th"),
                expected_th=item.get("expected_th"),
                underperform_ratio=item.get("underperform_ratio"),
                deficit_th=item.get("deficit_th"),
                profit_day=item.get("profit_day"),
                market_profit_day=item.get("market_profit_day"),
                decision_price_usd=price,
                gpu=item.get("gpu"),
                priority=item.get("priority"),
            )
            continue
        actions.extend(reallocate_slot(org, slot, f"auto_underperform_slot_guard_{UNDERPERFORM_GRACE_SECONDS}s"))
        UNDERPERFORM_SLOT_SEEN_SINCE.pop(key, None)

    log(
        "snapshot",
        fresh_workers=snap.get("fresh_workers"),
        profit_day=totals.get("profit_day"),
        market_profit_day=totals.get("market_profit_day"),
        decision_price_usd=price,
        cost_day=totals.get("cost_day"),
        no_hash=no_hash,
        negative_slots=negative_rows,
        underperform_slots=underperform_rows,
        actions=actions,
    )


def main() -> int:
    log(
        "started",
        poll_seconds=POLL_SECONDS,
        no_hash_grace_seconds=NO_HASH_GRACE_SECONDS,
        fallback_prl_price=FALLBACK_PRL_PRICE,
        price_band_usd=PRICE_BAND_USD,
        decision_price_cap_usd=DECISION_PRICE_CAP_USD,
        fixed_decision_price_usd=FIXED_DECISION_PRICE_USD,
        decision_profit_basis="profit_day_at_market_minus_band",
        negative_slot_grace_seconds=NEGATIVE_SLOT_GRACE_SECONDS,
        negative_slot_profit_day=NEGATIVE_SLOT_PROFIT_DAY,
        underperform_grace_seconds=UNDERPERFORM_GRACE_SECONDS,
        underperform_ratio=UNDERPERFORM_RATIO,
        underperform_min_deficit_th=UNDERPERFORM_MIN_DEFICIT_TH,
        enabled_orgs=ENABLED_ORGS,
    )
    while True:
        try:
            tick()
        except Exception as exc:
            log("tick_failed", error=type(exc).__name__, detail=str(exc)[:180])
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
