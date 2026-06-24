#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import profit_model
import state_db
from config_loader import OrgConfig, load_config
from fleet_common import env_int, json_dumps, utc_now
from org_worker import install_rate_limited_request


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
WATCH_PATH = SCRIPT_DIR / "salad_prl_watch.py"


def load_watch_module(org: OrgConfig) -> Any:
    env = org.watch_env()
    old_env: dict[str, str | None] = {}
    for key, value in env.items():
        old_env[key] = os.environ.get(key)
        os.environ[key] = value
    name = f"salad_prl_watch_availability_{org.label}"
    spec = importlib.util.spec_from_file_location(name, WATCH_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {WATCH_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def candidate_from_profile(watch: Any, profile: profit_model.Profile) -> Any:
    return watch.Candidate(profile.label, profile.priority, (profile.gpu_key,), profile.memory_mb)


def run_once(
    *,
    db_path: str | None = None,
    priorities: tuple[str, ...] = ("batch",),
    profile_limit: int | None = None,
) -> dict[str, Any]:
    config = load_config()
    no_gpu_sleep_after_seconds = env_int("PRL_NO_GPU_SLEEP_AFTER_SECONDS", 3600)
    no_gpu_sleep_seconds = env_int("PRL_NO_GPU_SLEEP_SECONDS", 900)
    profiles = [profile for profile in profit_model.load_profiles() if profile.priority in priorities]
    profiles.sort(
        key=lambda item: profit_model.expected_profit(
            item,
            decision_price_usd=config.risk.base_decision_price,
            gross_prl_per_th_day=profit_model.DEFAULT_GROSS_PRL_PER_TH_DAY,
            pearl_fee_rate=config.risk.effective_fee_rate(),
        ).profit_day,
        reverse=True,
    )
    if profile_limit is not None:
        profiles = profiles[:profile_limit]
    results: list[dict[str, Any]] = []

    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.sync_config(conn, config)
        state_db.upsert_gpu_profiles(conn, profiles)
        conn.commit()

    for org in config.enabled_orgs():
        watch = load_watch_module(org)
        install_rate_limited_request(watch, org, db_path=db_path)
        for profile in profiles:
            checked_at = utc_now()
            candidate = candidate_from_profile(watch, profile)
            try:
                available = watch.candidate_availability("__availability_probe__", candidate)
                ok = available is not None
                error = None
            except Exception as exc:
                available = None
                ok = False
                error = type(exc).__name__
            row = {
                "org_label": org.label,
                "profile_key": profile.profile_key,
                "available_count": available,
                "ok": ok,
                "error": error,
                "checked_at_utc": checked_at,
            }
            with state_db.connect(db_path) as conn:
                state_db.init_db(conn)
                state_db.upsert_profile_availability(conn, row)
                if ok and int(available or 0) <= 0:
                    existing = conn.execute(
                        """
                        SELECT *
                        FROM search_cooldowns
                        WHERE org_label = ? AND slot_name = '*' AND profile_key = ?
                        """,
                        (org.label, profile.profile_key),
                    ).fetchone()
                    no_gpu_since = existing["no_gpu_since_utc"] if existing and existing["no_gpu_since_utc"] else checked_at
                    attempts = int(existing["attempts"] or 0) + 1 if existing else 1
                    sleep_until = existing["sleep_until_utc"] if existing else None
                    try:
                        since_dt = datetime.fromisoformat(str(no_gpu_since).replace("Z", "+00:00"))
                    except ValueError:
                        since_dt = datetime.now(UTC)
                        no_gpu_since = since_dt.isoformat(timespec="seconds")
                    if since_dt.tzinfo is None:
                        since_dt = since_dt.replace(tzinfo=UTC)
                    age = (datetime.now(UTC) - since_dt).total_seconds()
                    if age >= no_gpu_sleep_after_seconds:
                        sleep_until = (datetime.now(UTC) + timedelta(seconds=no_gpu_sleep_seconds)).isoformat(timespec="seconds")
                        no_gpu_since = datetime.now(UTC).isoformat(timespec="seconds")
                    state_db.record_search_state(
                        conn,
                        {
                            "org_label": org.label,
                            "slot_name": "*",
                            "profile_key": profile.profile_key,
                            "no_gpu_since_utc": no_gpu_since,
                            "sleep_until_utc": sleep_until,
                            "attempts": attempts,
                            "reason": "availability_zero",
                        },
                    )
                    state_db.record_attempt(
                        conn,
                        {
                            "at_utc": checked_at,
                            "org_label": org.label,
                            "slot_name": "__availability__",
                            "action": "capacity_failure",
                            "profile_key": profile.profile_key,
                            "ok": False,
                            "payload": {"available_count": available},
                        },
                    )
                elif ok and int(available or 0) > 0:
                    state_db.record_search_state(
                        conn,
                        {
                            "org_label": org.label,
                            "slot_name": "*",
                            "profile_key": profile.profile_key,
                            "no_gpu_since_utc": None,
                            "sleep_until_utc": None,
                            "attempts": 0,
                            "reason": "availability_restored",
                        },
                    )
                conn.commit()
            results.append(row)

    by_profile: dict[str, int] = {}
    for row in results:
        if row["ok"] and row.get("available_count") is not None:
            by_profile[row["profile_key"]] = by_profile.get(row["profile_key"], 0) + int(row["available_count"] or 0)

    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.write_heartbeat(
            conn,
            "availability_probe",
            stale_after_seconds=600,
            payload={"probed": len(results), "priorities": priorities, "by_profile": by_profile},
        )
        state_db.record_event(
            conn,
            "availability_probed",
            source="availability_probe",
            message="Salad GPU availability probed",
            payload={"probed": len(results), "profiles": by_profile},
        )
        conn.commit()

    return {"probed": len(results), "priorities": priorities, "by_profile": by_profile, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Salad GPU availability and store it in the fleet DB.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--priorities", default="batch", help="Comma-separated priorities to probe.")
    parser.add_argument("--profile-limit", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    priorities = tuple(item.strip().lower() for item in args.priorities.split(",") if item.strip())

    def emit(payload: dict[str, Any]) -> None:
        if args.json:
            print(json_dumps(payload))
        else:
            print(f"availability probed={payload['probed']} priorities={','.join(payload['priorities'])}")

    if args.loop:
        while True:
            emit(run_once(db_path=args.db, priorities=priorities, profile_limit=args.profile_limit))
            time.sleep(args.interval)
    else:
        emit(run_once(db_path=args.db, priorities=priorities, profile_limit=args.profile_limit))


if __name__ == "__main__":
    main()
