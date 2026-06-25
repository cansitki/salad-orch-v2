#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import csv
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
DEFAULT_SNAPSHOT_CSV = STATE_DIR / "prl_profit_snapshots.csv"
SNAPSHOT_PATH = pathlib.Path(os.environ.get("PRL_SNAPSHOT_PATH", str(SCRIPT_DIR / "salad_prl_profit_snapshot.py")))
KRAY2_PATH = pathlib.Path(os.environ.get("PRL_WATCH_SCRIPT_PATH", str(SCRIPT_DIR / "salad_prl_watch.py")))
LOG = pathlib.Path(os.environ.get("PRL_GUARD_LOG", str(STATE_DIR / "logs" / "prl_nohash_guard.log")))
STOPPED_SLOT_STATE_PATH = pathlib.Path(
    os.environ.get("PRL_STOPPED_SLOT_STATE_PATH", str(STATE_DIR / "prl_stopped_slots.json"))
)
OBSERVATION_STATE_PATH = pathlib.Path(
    os.environ.get("PRL_GUARD_OBSERVATION_STATE_PATH", str(STATE_DIR / "prl_guard_observations.json"))
)
SLOT_ACTION_STATE_PATH = pathlib.Path(
    os.environ.get("PRL_SLOT_ACTION_STATE_PATH", str(STATE_DIR / "prl_slot_actions.json"))
)
FALLBACK_PRL_PRICE = float(os.environ.get("PRL_NOHASH_FALLBACK_PRICE", "0.62"))
PRICE_BAND_USD = float(os.environ.get("PRL_PRICE_BAND_USD", "0.02"))
DECISION_PRICE_CAP_USD = float(os.environ.get("PRL_DECISION_PRICE_CAP_USD", "0.63"))
FIXED_DECISION_PRICE_USD = float(os.environ.get("PRL_FIXED_DECISION_PRICE_USD", "0.62"))
POLL_SECONDS = int(os.environ.get("PRL_NOHASH_POLL_SECONDS", "20"))
NO_HASH_GRACE_SECONDS = int(os.environ.get("PRL_NOHASH_GRACE_SECONDS", "900"))
FORCE_NO_HASH_GRACE_SECONDS = int(os.environ.get("PRL_NOHASH_FORCE_GRACE_SECONDS", "900"))
NEGATIVE_PROFIT_GRACE_SECONDS = int(os.environ.get("PRL_NOHASH_NEGATIVE_GRACE_SECONDS", "900"))
NEGATIVE_SLOT_GRACE_SECONDS = int(os.environ.get("PRL_NEGATIVE_SLOT_GRACE_SECONDS", "900"))
NEGATIVE_SLOT_PROFIT_DAY = float(os.environ.get("PRL_NEGATIVE_SLOT_PROFIT_DAY", "0.01"))
UNDERPERFORM_GRACE_SECONDS = int(os.environ.get("PRL_UNDERPERFORM_GRACE_SECONDS", "120"))
UNDERPERFORM_RATIO = float(os.environ.get("PRL_UNDERPERFORM_RATIO", "0.85"))
UNDERPERFORM_MIN_DEFICIT_TH = float(os.environ.get("PRL_UNDERPERFORM_MIN_DEFICIT_TH", "10"))
STALE_WORKER_GRACE_SECONDS = int(os.environ.get("PRL_STALE_WORKER_GRACE_SECONDS", "900"))
STUCK_NON_LIVE_GRACE_SECONDS = int(os.environ.get("PRL_STUCK_NON_LIVE_SECONDS", "3600"))
EMPTY_STUCK_NON_LIVE_GRACE_SECONDS = int(
    os.environ.get("PRL_EMPTY_STUCK_NON_LIVE_SECONDS", str(STUCK_NON_LIVE_GRACE_SECONDS))
)
STUCK_NON_LIVE_MAX_ACTIONS = int(os.environ.get("PRL_STUCK_NON_LIVE_MAX_ACTIONS", "1"))
STUCK_NON_LIVE_MIN_ACTIVE_SLOTS = int(os.environ.get("PRL_STUCK_NON_LIVE_MIN_ACTIVE_SLOTS", "28"))
STUCK_NON_LIVE_RETARGET_COOLDOWN_SECONDS = int(os.environ.get("PRL_STUCK_NON_LIVE_RETARGET_COOLDOWN_SECONDS", "1800"))
STUCK_NON_LIVE_TICK_BUDGET_SECONDS = float(os.environ.get("PRL_STUCK_NON_LIVE_TICK_BUDGET_SECONDS", "20"))
STUCK_RUNNING_ZERO_DEFER_SECONDS = int(os.environ.get("PRL_STUCK_RUNNING_ZERO_DEFER_SECONDS", "3600"))
GLOBAL_POOL_MIN_FRESH_WORKERS = int(os.environ.get("PRL_GLOBAL_POOL_MIN_FRESH_WORKERS", "8"))
SEEN_SINCE: dict[tuple[str, str], float] = {}
NEGATIVE_SLOT_SEEN_SINCE: dict[tuple[str, str], float] = {}
UNDERPERFORM_SLOT_SEEN_SINCE: dict[tuple[str, str], float] = {}
STALE_WORKER_SEEN_SINCE: dict[tuple[str, str], float] = {}
STUCK_NON_LIVE_RETARGETED_AT: dict[tuple[str, str], float] = {}
INCLUDE_BMU = os.environ.get("PRL_INCLUDE_BMU", "").lower() in {"1", "true", "yes"}
ENABLED_ORGS = tuple(
    org.strip()
    for org in os.environ.get("PRL_GUARD_ORGS", "kray,kray2,kray3").split(",")
    if org.strip()
)
DEFAULT_API_KEY_ENV = os.environ.get("PRL_WATCH_DEFAULT_API_KEY_ENV", "SALAD_API_KEY")
OBSERVATION_STORES: dict[str, dict[tuple[str, str], float]] = {
    "no_hash": SEEN_SINCE,
    "negative": NEGATIVE_SLOT_SEEN_SINCE,
    "underperform": UNDERPERFORM_SLOT_SEEN_SINCE,
    "stale": STALE_WORKER_SEEN_SINCE,
}
OBSERVATION_EVENTS = {
    "no_hash_observed": "no_hash",
    "negative_slot_observed": "negative",
    "underperform_slot_observed": "underperform",
    "stale_worker_observed": "stale",
}


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


def roi_slots(org: str) -> str:
    if org == "kray2":
        names = [f"prl-{org}-roi-{index:02d}" for index in range(1, 5)]
        names.append(f"prl-{org}-roi-05b")
        names.extend(f"prl-{org}-roi-{index:02d}" for index in range(6, 11))
        return ",".join(names)
    return ",".join(f"prl-{org}-roi-{index:02d}" for index in range(1, 11))


KRAY_WATCH_ENV = {
    **WATCH_COMMON_ENV,
    "PRL_WATCH_NAME": "kray-prl-watch",
    "PRL_WATCH_LOG": str(STATE_DIR / "logs" / "kray_prl_watch.log"),
    "PRL_WATCH_ORG": "kray",
    "PRL_WATCH_API_KEY_ENV": "SALAD_API_KEY_2",
    "PRL_WATCH_SLOTS": roi_slots("kray"),
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
    "PRL_WATCH_SLOTS": roi_slots("kry1"),
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
    "PRL_WATCH_SLOTS": roi_slots("kray2"),
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


def parse_log_timestamp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(UTC).timestamp()
    except ValueError:
        return None


def observation_key_text(key: tuple[str, str]) -> str:
    org, slot = key
    return f"{org}/{slot}"


def observation_key_from_text(text: str) -> tuple[str, str] | None:
    if "/" not in text:
        return None
    org, slot = text.split("/", 1)
    org = org.strip()
    slot = slot.strip()
    if not org or not slot:
        return None
    return org, slot


def load_observation_section(payload: dict[str, Any], name: str, store: dict[tuple[str, str], float]) -> None:
    rows = payload.get(name) or {}
    if not isinstance(rows, dict):
        return
    for raw_key, raw_ts in rows.items():
        key = observation_key_from_text(str(raw_key))
        if key is None:
            continue
        try:
            first_seen = float(raw_ts)
        except (TypeError, ValueError):
            continue
        if first_seen > 0:
            store[key] = min(store.get(key, first_seen), first_seen)


def seed_observations_from_log(max_lines: int = 5000) -> None:
    if not LOG.exists():
        return
    try:
        lines = LOG.read_text(errors="ignore").splitlines()[-max_lines:]
    except OSError as exc:
        log("guard_observation_log_seed_failed", error=type(exc).__name__, detail=str(exc)[:180])
        return
    seeded = 0
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        section = OBSERVATION_EVENTS.get(str(row.get("event") or ""))
        if section is None:
            continue
        org = str(row.get("org") or "").strip()
        slot = str(row.get("slot") or "").strip()
        if not org or not slot:
            continue
        seen_at = parse_log_timestamp(row.get("at"))
        if seen_at is None:
            continue
        key = (org, slot)
        store = OBSERVATION_STORES[section]
        previous = store.get(key)
        if previous is None or seen_at < previous:
            store[key] = seen_at
            seeded += 1
    if seeded:
        log("guard_observation_state_seeded_from_log", seeded=seeded, max_lines=max_lines)


def load_guard_observation_state() -> None:
    try:
        payload = json.loads(OBSERVATION_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        seed_observations_from_log()
        return
    except (OSError, json.JSONDecodeError) as exc:
        log("guard_observation_state_load_failed", error=type(exc).__name__, detail=str(exc)[:180])
        seed_observations_from_log()
        return
    if not isinstance(payload, dict):
        seed_observations_from_log()
        return
    for name, store in OBSERVATION_STORES.items():
        load_observation_section(payload, name, store)
    seed_observations_from_log()


def save_guard_observation_state() -> None:
    try:
        OBSERVATION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": now(),
            **{
                name: {observation_key_text(key): first_seen for key, first_seen in sorted(store.items())}
                for name, store in OBSERVATION_STORES.items()
            },
        }
        tmp = OBSERVATION_STATE_PATH.with_suffix(f"{OBSERVATION_STATE_PATH.suffix}.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(OBSERVATION_STATE_PATH)
    except Exception as exc:
        log("guard_observation_state_write_failed", error=type(exc).__name__, detail=str(exc)[:180])


def record_stopped_slot(org: str, slot: str, reason: str) -> None:
    try:
        STOPPED_SLOT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = json.loads(STOPPED_SLOT_STATE_PATH.read_text())
        except Exception:
            state = {}
        state[f"{org}/{slot}"] = {"stopped_at": time.time(), "reason": reason}
        tmp = STOPPED_SLOT_STATE_PATH.with_suffix(f"{STOPPED_SLOT_STATE_PATH.suffix}.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        tmp.replace(STOPPED_SLOT_STATE_PATH)
    except Exception as exc:
        log("stopped_slot_state_write_failed", org=org, slot=slot, error=type(exc).__name__, detail=str(exc)[:180])


def recent_stopped_slot_age_seconds(org: str, slot: str) -> float | None:
    try:
        state = json.loads(STOPPED_SLOT_STATE_PATH.read_text())
    except Exception:
        return None
    entry = state.get(f"{org}/{slot}")
    if not isinstance(entry, dict):
        return None
    try:
        stopped_at = float(entry.get("stopped_at") or 0)
    except (TypeError, ValueError):
        return None
    if stopped_at <= 0:
        return None
    return max(0.0, time.time() - stopped_at)


def safe_slot_action_token(org: str, slot: str) -> str:
    text = f"{org}__{slot}"
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in text)


def slot_action_detail_path(org: str, slot: str) -> pathlib.Path:
    return SLOT_ACTION_STATE_PATH.parent / f"{SLOT_ACTION_STATE_PATH.stem}.d" / f"{safe_slot_action_token(org, slot)}.json"


def slot_action_with_age(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    try:
        action_at = float(entry.get("at") or 0)
    except (TypeError, ValueError):
        return None
    if action_at <= 0:
        return None
    return {
        **entry,
        "age_seconds": max(0.0, time.time() - action_at),
    }


def recent_slot_action(org: str, slot: str) -> dict[str, Any] | None:
    try:
        detail = json.loads(slot_action_detail_path(org, slot).read_text(encoding="utf-8"))
    except Exception:
        detail = None
    action = slot_action_with_age(detail)
    if action is not None:
        return action
    try:
        state = json.loads(SLOT_ACTION_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    return slot_action_with_age(state.get(f"{org}/{slot}") or state.get(slot))


def record_guard_slot_action(org: str, slot: str, action: str, reason: str, candidate: str = "") -> None:
    payload = {
        "at": time.time(),
        "at_utc": now(),
        "org": org,
        "slot": slot,
        "action": action,
        "reason": reason,
        "candidate": candidate,
    }
    try:
        SLOT_ACTION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = json.loads(SLOT_ACTION_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        state[f"{org}/{slot}"] = payload
        detail_path = slot_action_detail_path(org, slot)
        detail_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_detail = detail_path.with_suffix(f"{detail_path.suffix}.tmp")
        tmp_detail.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        tmp_detail.replace(detail_path)
        tmp = SLOT_ACTION_STATE_PATH.with_suffix(f"{SLOT_ACTION_STATE_PATH.suffix}.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(SLOT_ACTION_STATE_PATH)
    except Exception as exc:
        log(
            "guard_slot_action_state_write_failed",
            org=org,
            slot=slot,
            action=action,
            error=type(exc).__name__,
            detail=str(exc)[:180],
        )


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
    "PRL_WATCH_SLOTS": roi_slots("kray3"),
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
        "PRL_WATCH_SLOTS": roi_slots(org),
        "PRL_WATCH_WORKER_PREFIX": f"{org}-prl",
        "PRL_WATCH_WORKER_SLOT_PREFIX": f"{org}-roi-",
        "PRL_WATCH_POOL_WORKER_PREFIX": f"{org}-prl-{org}",
        "PRL_WATCH_DISPLAY_PREFIX": f"PearlFortune {org.upper()}",
    }


def generic_watch_env(org: str) -> dict[str, str]:
    key_env = os.environ.get(f"PRL_WATCH_API_KEY_ENV_{org.upper()}", DEFAULT_API_KEY_ENV)
    return {
        **WATCH_COMMON_ENV,
        "PRL_WATCH_NAME": f"{org}-prl-watch",
        "PRL_WATCH_LOG": str(STATE_DIR / "logs" / f"{org}_prl_watch.log"),
        "PRL_WATCH_ORG": org,
        "PRL_WATCH_PUBLIC_ORG": org,
        "PRL_WATCH_API_KEY_ENV": key_env,
        "PRL_WATCH_SLOTS": roi_slots(org),
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
for org in ENABLED_ORGS:
    if org not in watchers:
        watchers[org] = load_module(KRAY2_PATH, f"{org}_prl_watch_guard", generic_watch_env(org))
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


def trailing_snapshot_price_usd(hours: float = 1.0) -> float | None:
    path = pathlib.Path(os.environ.get("PRL_SNAPSHOT_CSV_PATH", str(DEFAULT_SNAPSHOT_CSV)))
    if not path.exists():
        return None
    cutoff = datetime.now(UTC).timestamp() - hours * 3600
    values: list[float] = []
    try:
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    at = datetime.fromisoformat(str(row.get("at_utc") or "").replace("Z", "+00:00")).timestamp()
                    price = float(row.get("live_market_prl_price") or 0)
                except (TypeError, ValueError):
                    continue
                if at >= cutoff and price > 0:
                    values.append(price)
    except OSError:
        return None
    return (sum(values) / len(values)) if values else None


def smoothed_market_prl_price_usd() -> float | None:
    current = market_prl_price_usd()
    trailing = trailing_snapshot_price_usd(float(os.environ.get("PRL_PRICE_AVERAGE_HOURS", "1")))
    values = [value for value in (current, trailing) if value and value > 0]
    if not values:
        return None
    selected = min(values)
    log(
        "decision_price_smoothed_market",
        current_market_prl_price_usd=round(current or 0.0, 6),
        trailing_average_prl_price_usd=round(trailing or 0.0, 6),
        selected_price_usd=round(selected, 6),
    )
    return selected


def decision_prl_price() -> float:
    if FIXED_DECISION_PRICE_USD > 0:
        return FIXED_DECISION_PRICE_USD
    price = smoothed_market_prl_price_usd()
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


def pending_instances(module: Any, slot: str) -> list[dict[str, Any]]:
    _group, instances = module.slot_state(slot)
    return [
        instance
        for instance in instances
        if not instance.get("ready")
        and not instance.get("started")
        and str(instance.get("id") or "")
        and str(instance.get("state") or "").lower() not in {"running", "starting"}
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
        if candidate is None:
            candidate = module.best_available_candidate(
                slot,
                exclude=actual,
                reason=f"{reason}_fallback_best",
            )
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
        if hasattr(module, "record_slot_action_state"):
            module.record_slot_action_state(slot, "patched", reason, candidate.label)
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

def stop_slot(org: str, slot: str, reason: str) -> dict[str, Any] | None:
    module = watchers.get(org)
    if module is None:
        return None
    try:
        module.request("POST", f"/organizations/{module.ORG}/projects/{module.PROJECT}/containers/{slot}/stop")
        module.log("slot_stopped", slot=slot, reason=reason)
        record_stopped_slot(org, slot, reason)
        log("slot_stopped", org=org, slot=slot, reason=reason)
        return {"org": org, "slot": slot, "state": "stopped", "retargeted": None}
    except Exception as exc:
        log(
            "slot_stop_failed",
            org=org,
            slot=slot,
            reason=reason,
            error=type(exc).__name__,
            detail=str(exc)[:180],
        )
        return None


def reallocate_slot(org: str, slot: str, reason: str, *, retarget: bool = True) -> list[dict[str, Any]]:
    module = watchers.get(org)
    if module is None:
        return []
    pre_retarget_instances = running_instances(module, slot)
    pre_retarget_ids = {str(instance.get("id") or "") for instance in pre_retarget_instances}
    retargeted = retarget_slot(org, slot, reason) if retarget else None
    if retarget and retargeted is None:
        stopped = stop_slot(org, slot, f"{reason}:no_retarget_candidate")
        if stopped is not None:
            return [stopped]
    actions: list[dict[str, Any]] = []
    instances_by_id: dict[str, dict[str, Any]] = {}
    for instance in pre_retarget_instances + running_instances(module, slot):
        instance_id = str(instance.get("id") or "")
        if not instance_id:
            continue
        instances_by_id.setdefault(instance_id, instance)
    for instance_id, instance in instances_by_id.items():
        module.reallocate(slot, instance_id, reason)
        record_guard_slot_action(org, slot, "reallocated", reason)
        actions.append(
            {
                "org": org,
                "slot": slot,
                "instance_id": instance_id,
                "state": instance.get("state"),
                "ready": instance.get("ready"),
                "started": instance.get("started"),
                "captured_before_retarget": instance_id in pre_retarget_ids,
                "retargeted": retargeted,
            }
        )
    if not actions and retargeted is not None:
        actions.append(
            {
                "org": org,
                "slot": slot,
                "state": "retargeted_no_running_instances",
                "retargeted": retargeted,
            }
        )
    return actions


def recycle_zero_running_slot(org: str, slot: str, reason: str) -> list[dict[str, Any]]:
    module = watchers.get(org)
    if module is None:
        return []
    try:
        pending = pending_instances(module, slot)
    except Exception as exc:
        log("stuck_non_live_pending_lookup_failed", org=org, slot=slot, reason=reason, error=type(exc).__name__, detail=str(exc)[:180])
        return []

    actions: list[dict[str, Any]] = []
    for instance in pending:
        instance_id = str(instance.get("id") or "")
        if not instance_id:
            continue
        module.reallocate(slot, instance_id, reason)
        record_guard_slot_action(org, slot, "reallocated_pending", reason)
        actions.append(
            {
                "org": org,
                "slot": slot,
                "instance_id": instance_id,
                "state": instance.get("state"),
                "ready": instance.get("ready"),
                "started": instance.get("started"),
                "retargeted": None,
                "recycled_pending": True,
            }
        )
    if actions:
        return actions

    try:
        restart_reason = f"{reason}:hidden_pending_restart"
        module.request("POST", f"/organizations/{module.ORG}/projects/{module.PROJECT}/containers/{slot}/stop")
        module.start_slot(slot, restart_reason)
        log("stuck_non_live_hidden_pending_restarted", org=org, slot=slot, reason=restart_reason)
        return [{"org": org, "slot": slot, "state": "hidden_pending_restarted", "retargeted": None}]
    except Exception as exc:
        log(
            "stuck_non_live_hidden_pending_restart_failed",
            org=org,
            slot=slot,
            reason=reason,
            error=type(exc).__name__,
            detail=str(exc)[:180],
        )
        return []


def org_for_slot(slot: str) -> str:
    for org in ENABLED_ORGS:
        if slot.startswith(f"prl-{org}-roi-"):
            return org
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


def seen_since_from_state_age(store: dict[tuple[str, str], float], key: tuple[str, str], item: dict[str, Any]) -> float:
    now_ts = time.time()
    if key in store:
        return store[key]
    try:
        state_age_seconds = float(item.get("state_age_seconds") or 0)
    except (TypeError, ValueError):
        state_age_seconds = 0.0
    org, slot = key
    recent_action = recent_slot_action(org, slot)
    try:
        recent_action_age = float(recent_action.get("age_seconds") or 0) if recent_action else 0.0
    except (TypeError, ValueError):
        recent_action_age = 0.0
    if recent_action_age > 0 and (state_age_seconds <= 0 or recent_action_age < state_age_seconds):
        store[key] = max(0.0, now_ts - recent_action_age)
    elif state_age_seconds > 0:
        store[key] = max(0.0, now_ts - state_age_seconds)
    else:
        store[key] = now_ts
    return store[key]


def tick() -> None:
    price = decision_prl_price()
    snap = snapshot.build_snapshot(price)
    no_hash = list(snap.get("running_no_live_billable_slots") or [])
    stale_current_workers = list(snap.get("stale_current_workers") or [])
    stuck_non_live = list(snap.get("stuck_non_live_slots") or [])
    org_discrepancies = list(snap.get("org_discrepancies") or [])
    totals = snap.get("totals") or {}
    observed_keys = {(str(item.get("org")), str(item.get("slot"))) for item in no_hash}
    for key in list(SEEN_SINCE):
        if key not in observed_keys:
            SEEN_SINCE.pop(key, None)
    no_hash_slots = {slot for _org, slot in observed_keys}
    stale_keys = {(org_for_slot(str(item.get("slot") or "")), str(item.get("slot") or "")) for item in stale_current_workers}
    stale_keys = {(org, slot) for org, slot in stale_keys if org and slot}
    for key in list(STALE_WORKER_SEEN_SINCE):
        if key not in stale_keys:
            STALE_WORKER_SEEN_SINCE.pop(key, None)

    low_fresh_pool_sample = int(snap.get("fresh_workers") or 0) < GLOBAL_POOL_MIN_FRESH_WORKERS
    if low_fresh_pool_sample:
        log(
            "global_pool_profit_guard_skip",
            fresh_workers=snap.get("fresh_workers"),
            min_fresh_workers=GLOBAL_POOL_MIN_FRESH_WORKERS,
            no_hash=no_hash,
            stuck_non_live_slots=stuck_non_live,
            profit_day=totals.get("profit_day"),
        )

    actions: list[dict[str, Any]] = []
    if low_fresh_pool_sample:
        stale_current_workers = []
        no_hash = []

    for item in stale_current_workers:
        slot = str(item.get("slot") or "")
        org = org_for_slot(slot)
        if not org or org not in watchers or slot in no_hash_slots:
            continue
        key = (org, slot)
        first_seen = STALE_WORKER_SEEN_SINCE.setdefault(key, time.time())
        age = time.time() - first_seen
        if age < STALE_WORKER_GRACE_SECONDS:
            log(
                "stale_worker_observed",
                org=org,
                slot=slot,
                age_seconds=round(age, 1),
                grace_seconds=STALE_WORKER_GRACE_SECONDS,
                worker=item.get("worker"),
                last_stats_at=item.get("last_stats_at"),
                th=item.get("th"),
                gpu=item.get("gpu"),
            )
            continue
        reason = f"auto_stale_worker_guard_{STALE_WORKER_GRACE_SECONDS}s"
        slot_actions = reallocate_slot(org, slot, reason, retarget=False)
        if slot_actions:
            log(
                "stale_worker_reallocated",
                org=org,
                slot=slot,
                reason=reason,
                actions=slot_actions,
                worker=item.get("worker"),
                last_stats_at=item.get("last_stats_at"),
                th=item.get("th"),
                gpu=item.get("gpu"),
            )
        actions.extend(slot_actions)
        STALE_WORKER_SEEN_SINCE.pop(key, None)

    total_no_hash_cost_day = sum(float(item.get("cost_day") or 0) for item in no_hash)
    for item in no_hash:
        org = str(item.get("org") or "")
        slot = str(item.get("slot") or "")
        key = (org, slot)
        first_seen = seen_since_from_state_age(SEEN_SINCE, key, item)
        age = time.time() - first_seen
        profit_day = float(totals.get("profit_day") or 0)
        force_now = False if low_fresh_pool_sample else profit_day < 0 or total_no_hash_cost_day > max(0.0, profit_day)
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
                state_age_seconds=item.get("state_age_seconds"),
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
        slot_actions = reallocate_slot(org, slot, reason, retarget=False)
        if slot_actions:
            log(
                "no_hash_slot_reallocated",
                org=org,
                slot=slot,
                reason=reason,
                actions=slot_actions,
                age_seconds=round(age, 1),
                grace_seconds=grace_seconds,
                cost_day=item.get("cost_day"),
                state_age_seconds=item.get("state_age_seconds"),
                market_profit_day=totals.get("market_profit_day"),
                profit_day=totals.get("profit_day"),
                decision_price_usd=price,
            )
        actions.extend(slot_actions)
        SEEN_SINCE.pop(key, None)

    negative_rows = []
    if low_fresh_pool_sample:
        candidate_rows = []
    else:
        candidate_rows = list(snap.get("slots") or [])
    for row in candidate_rows:
        slot = str(row.get("slot") or "")
        if not slot or slot in no_hash_slots or str(row.get("gpu") or "") == "requested":
            continue
        profit_day = float(row.get("profit_day") or 0)
        if profit_day >= NEGATIVE_SLOT_PROFIT_DAY:
            continue
        market_profit_day = row.get("market_profit_day")
        if market_profit_day is not None and float(market_profit_day) >= NEGATIVE_SLOT_PROFIT_DAY:
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
        reason = f"auto_negative_slot_guard_{NEGATIVE_SLOT_GRACE_SECONDS}s"
        slot_actions = reallocate_slot(org, slot, reason, retarget=False)
        if slot_actions:
            log(
                "negative_slot_reallocated",
                org=org,
                slot=slot,
                reason=reason,
                actions=slot_actions,
                age_seconds=round(age, 1),
                grace_seconds=NEGATIVE_SLOT_GRACE_SECONDS,
                profit_day=item.get("profit_day"),
                market_profit_day=item.get("market_profit_day"),
                decision_price_usd=price,
                th=item.get("th"),
                gpu=item.get("gpu"),
                priority=item.get("priority"),
            )
        actions.extend(slot_actions)
        NEGATIVE_SLOT_SEEN_SINCE.pop(key, None)

    underperform_rows = []
    for row in candidate_rows:
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
        reason = f"auto_underperform_slot_guard_{UNDERPERFORM_GRACE_SECONDS}s"
        slot_actions = reallocate_slot(org, slot, reason, retarget=False)
        if slot_actions:
            log(
                "underperform_slot_reallocated",
                org=org,
                slot=slot,
                reason=reason,
                actions=slot_actions,
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
        actions.extend(slot_actions)
        UNDERPERFORM_SLOT_SEEN_SINCE.pop(key, None)

    stuck_keys = {(str(item.get("org") or ""), str(item.get("slot") or "")) for item in stuck_non_live}
    for key in list(STUCK_NON_LIVE_RETARGETED_AT):
        if key not in stuck_keys:
            STUCK_NON_LIVE_RETARGETED_AT.pop(key, None)

    stuck_actions = 0
    urgent_observations = len(stale_current_workers) + len(no_hash) + len(negative_rows)
    active_slots = 0
    for row in org_discrepancies:
        try:
            active_slots += int(row.get("active_salad_slots") or 0)
        except (TypeError, ValueError):
            continue
    if actions or urgent_observations:
        log(
            "stuck_non_live_cleanup_limited",
            reason="urgent_slots_first_zero_running_only",
            urgent_observations=urgent_observations,
            pending=len(stuck_non_live),
            actions=actions,
        )
    if STUCK_NON_LIVE_MIN_ACTIVE_SLOTS > 0 and active_slots < STUCK_NON_LIVE_MIN_ACTIVE_SLOTS:
        log(
            "stuck_non_live_cleanup_limited",
            reason="active_slots_below_floor",
            active_slots=active_slots,
            min_active_slots=STUCK_NON_LIVE_MIN_ACTIVE_SLOTS,
            pending=len(stuck_non_live),
            actions=actions,
        )
        stuck_non_live = []

    stuck_started_at = time.monotonic()
    for item in stuck_non_live:
        elapsed = time.monotonic() - stuck_started_at
        if elapsed >= STUCK_NON_LIVE_TICK_BUDGET_SECONDS:
            log(
                "stuck_non_live_cleanup_budget_exhausted",
                elapsed_seconds=round(elapsed, 1),
                budget_seconds=STUCK_NON_LIVE_TICK_BUDGET_SECONDS,
                pending=len(stuck_non_live),
                actions=actions,
            )
            break
        org = str(item.get("org") or "")
        slot = str(item.get("slot") or "")
        if not org or org not in watchers or slot in no_hash_slots:
            continue
        if stuck_actions >= STUCK_NON_LIVE_MAX_ACTIONS:
            continue
        zero_running = int(item.get("running") or 0) <= 0
        status = str(item.get("status") or "").lower()
        if zero_running:
            cleanup_grace = (
                EMPTY_STUCK_NON_LIVE_GRACE_SECONDS if item.get("empty_pending") else STUCK_NON_LIVE_GRACE_SECONDS
            )
            try:
                state_age_seconds = float(item.get("state_age_seconds") or 0)
            except (TypeError, ValueError):
                state_age_seconds = 0.0
            empty_pending = bool(item.get("empty_pending"))
            defer_until_seconds = cleanup_grace
            if not empty_pending:
                defer_until_seconds += STUCK_RUNNING_ZERO_DEFER_SECONDS
            if state_age_seconds < defer_until_seconds:
                log(
                    "stuck_non_live_zero_running_deferred",
                    org=org,
                    slot=slot,
                    state_age_seconds=item.get("state_age_seconds"),
                    grace_seconds=cleanup_grace,
                    running_zero_defer_seconds=0 if empty_pending else STUCK_RUNNING_ZERO_DEFER_SECONDS,
                    defer_until_seconds=defer_until_seconds,
                    empty_pending=empty_pending,
                    status=item.get("status"),
                    running=item.get("running"),
                    creating=item.get("creating"),
                    allocating=item.get("allocating"),
                    requested_gpus=item.get("requested_gpus"),
                )
                continue
        if (actions or urgent_observations) and not zero_running:
            continue
        key = (org, slot)
        recent_stop_age = recent_stopped_slot_age_seconds(org, slot)
        if recent_stop_age is not None and recent_stop_age < STUCK_NON_LIVE_RETARGET_COOLDOWN_SECONDS:
            log(
                "stuck_non_live_slot_recent_stop_cooldown",
                org=org,
                slot=slot,
                stopped_age_seconds=round(recent_stop_age, 1),
                cooldown_seconds=STUCK_NON_LIVE_RETARGET_COOLDOWN_SECONDS,
                state_age_seconds=item.get("state_age_seconds"),
                status=item.get("status"),
                running=item.get("running"),
                creating=item.get("creating"),
                allocating=item.get("allocating"),
                requested_gpus=item.get("requested_gpus"),
            )
            continue
        recent_action = recent_slot_action(org, slot)
        if recent_action is not None:
            action_age = float(recent_action.get("age_seconds") or 0)
            if action_age < STUCK_NON_LIVE_RETARGET_COOLDOWN_SECONDS:
                log(
                    "stuck_non_live_slot_recent_action_cooldown",
                    org=org,
                    slot=slot,
                    action=recent_action.get("action"),
                    action_age_seconds=round(action_age, 1),
                    cooldown_seconds=STUCK_NON_LIVE_RETARGET_COOLDOWN_SECONDS,
                    state_age_seconds=item.get("state_age_seconds"),
                    status=item.get("status"),
                    running=item.get("running"),
                    creating=item.get("creating"),
                    allocating=item.get("allocating"),
                    requested_gpus=item.get("requested_gpus"),
                )
                continue
        last_retargeted_at = STUCK_NON_LIVE_RETARGETED_AT.get(key)
        if last_retargeted_at is not None:
            cooldown_age = time.time() - last_retargeted_at
            if cooldown_age < STUCK_NON_LIVE_RETARGET_COOLDOWN_SECONDS:
                log(
                    "stuck_non_live_slot_cooldown",
                    org=org,
                    slot=slot,
                    cooldown_age_seconds=round(cooldown_age, 1),
                    cooldown_seconds=STUCK_NON_LIVE_RETARGET_COOLDOWN_SECONDS,
                    state_age_seconds=item.get("state_age_seconds"),
                    status=item.get("status"),
                    running=item.get("running"),
                    creating=item.get("creating"),
                    allocating=item.get("allocating"),
                    requested_gpus=item.get("requested_gpus"),
                )
                continue
        log(
            "stuck_non_live_slot_cleanup",
            action="recycle_zero_running" if zero_running else "retarget",
            org=org,
            slot=slot,
            state_age_seconds=item.get("state_age_seconds"),
            grace_seconds=EMPTY_STUCK_NON_LIVE_GRACE_SECONDS
            if item.get("empty_pending")
            else STUCK_NON_LIVE_GRACE_SECONDS,
            status=item.get("status"),
            running=item.get("running"),
            creating=item.get("creating"),
            allocating=item.get("allocating"),
            instance_count=item.get("instance_count"),
            empty_pending=item.get("empty_pending"),
            requested_gpus=item.get("requested_gpus"),
        )
        cleanup_grace = (
            EMPTY_STUCK_NON_LIVE_GRACE_SECONDS if item.get("empty_pending") else STUCK_NON_LIVE_GRACE_SECONDS
        )
        reason = f"auto_stuck_non_live_{cleanup_grace}s"
        if zero_running:
            slot_actions = recycle_zero_running_slot(org, slot, f"{reason}:zero_running")
        else:
            slot_actions = reallocate_slot(org, slot, reason)
        if slot_actions:
            STUCK_NON_LIVE_RETARGETED_AT[key] = time.time()
            actions.extend(slot_actions)
            stuck_actions += 1

    log(
        "snapshot",
        fresh_workers=snap.get("fresh_workers"),
        profit_day=totals.get("profit_day"),
        market_profit_day=totals.get("market_profit_day"),
        decision_price_usd=price,
        cost_day=totals.get("cost_day"),
        no_hash=no_hash,
        stale_current_workers=stale_current_workers,
        org_discrepancies=org_discrepancies,
        stuck_non_live_slots=stuck_non_live,
        negative_slots=negative_rows,
        underperform_slots=underperform_rows,
        actions=actions,
    )
    save_guard_observation_state()


def main() -> int:
    load_guard_observation_state()
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
        stale_worker_grace_seconds=STALE_WORKER_GRACE_SECONDS,
        empty_stuck_non_live_grace_seconds=EMPTY_STUCK_NON_LIVE_GRACE_SECONDS,
        stuck_non_live_grace_seconds=STUCK_NON_LIVE_GRACE_SECONDS,
        stuck_running_zero_defer_seconds=STUCK_RUNNING_ZERO_DEFER_SECONDS,
        stuck_non_live_retarget_cooldown_seconds=STUCK_NON_LIVE_RETARGET_COOLDOWN_SECONDS,
        stuck_non_live_tick_budget_seconds=STUCK_NON_LIVE_TICK_BUDGET_SECONDS,
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
