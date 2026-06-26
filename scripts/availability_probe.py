#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import multiprocessing
import os
import pathlib
import queue
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import profit_model
import state_db
from config_loader import OrgConfig, load_config
from fleet_common import env_bool, env_int, json_dumps, utc_now
from org_worker import (
    clear_no_credits_cooldown_row,
    explicit_positive_balance_restore,
    explicit_zero_balance_skip,
    install_rate_limited_request,
    record_replica_quota_status,
    replica_quota_status,
    zero_replica_quota_skip_from_status,
)


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


def _probe_org_profiles(
    org: OrgConfig,
    profiles: list[profit_model.Profile],
    *,
    db_path: str | None,
    profile_parallelism: int = 1,
) -> list[dict[str, Any]]:
    watch = load_watch_module(org)
    install_rate_limited_request(watch, org, db_path=db_path)
    quota_status = replica_quota_status(watch)
    if quota_status is not None:
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            record_replica_quota_status(
                conn,
                org_label=org.label,
                quota_status=quota_status,
                source="availability_probe",
            )
            conn.commit()
    replica_quota_skip = zero_replica_quota_skip_from_status(quota_status)
    if replica_quota_skip is not None:
        return [
            {
                "org_label": org.label,
                "profile_key": "*",
                "available_count": None,
                "ok": True,
                "error": None,
                "checked_at_utc": utc_now(),
                "skip_reason": "zero_replica_quota",
                "zero_replica_quota_skip": replica_quota_skip,
            }
        ]

    def probe_profile(profile: profit_model.Profile) -> dict[str, Any]:
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
        return {
            "org_label": org.label,
            "profile_key": profile.profile_key,
            "available_count": available,
            "ok": ok,
            "error": error,
            "checked_at_utc": checked_at,
        }

    if profile_parallelism <= 1 or len(profiles) <= 1:
        return [probe_profile(profile) for profile in profiles]

    max_workers = min(profile_parallelism, len(profiles))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(probe_profile, profiles))


def _refresh_quota_only_org(org: OrgConfig, *, db_path: str | None) -> dict[str, Any]:
    checked_at = utc_now()
    try:
        watch = load_watch_module(org)
        install_rate_limited_request(watch, org, db_path=db_path)
        quota_status = replica_quota_status(watch)
    except Exception as exc:
        return {
            "org_label": org.label,
            "ok": False,
            "error": type(exc).__name__,
            "checked_at_utc": checked_at,
        }
    if quota_status is None:
        return {
            "org_label": org.label,
            "ok": False,
            "error": "quota_status_unavailable",
            "checked_at_utc": checked_at,
        }
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        record_replica_quota_status(
            conn,
            org_label=org.label,
            quota_status=quota_status,
            source="availability_probe_zero_balance",
        )
        conn.commit()
    return {
        "org_label": org.label,
        "ok": True,
        "checked_at_utc": checked_at,
        **quota_status,
    }


def _refresh_quota_only_orgs(orgs: list[OrgConfig], *, db_path: str | None) -> list[dict[str, Any]]:
    return [_refresh_quota_only_org(org, db_path=db_path) for org in orgs]


def _probe_org_process(task: dict[str, Any], result_queue: Any) -> None:
    try:
        result_queue.put(("ok", _probe_org_profiles(**task)))
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)[:500]))


def _join_probe_process(process: Any) -> None:
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)


def _run_probe_batch(tasks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ctx = multiprocessing.get_context("fork")
    workers = []
    for task in tasks:
        result_queue = ctx.Queue(maxsize=1)
        process = ctx.Process(target=_probe_org_process, args=(task, result_queue))
        process.start()
        workers.append((task, process, result_queue))

    results: list[list[dict[str, Any]]] = []
    for task, process, result_queue in workers:
        while True:
            try:
                item = result_queue.get(timeout=0.5)
                break
            except queue.Empty:
                if not process.is_alive():
                    process.join(5)
                    org = task["org"].label
                    raise RuntimeError(f"availability probe {org} exited without result rc={process.exitcode}")
        _join_probe_process(process)
        if item[0] == "ok":
            results.append(item[1])
        else:
            org = task["org"].label
            raise RuntimeError(f"availability probe {org} failed: {item[1]}: {item[2]}")
    return results


def _batch_org_tasks(tasks: list[dict[str, Any]], max_workers: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    remaining = list(tasks)
    while remaining:
        batch: list[dict[str, Any]] = []
        used_api_keys: set[str] = set()
        next_remaining: list[dict[str, Any]] = []
        for task in remaining:
            api_key_env = str(task["org"].api_key_env)
            if len(batch) < max_workers and api_key_env not in used_api_keys:
                batch.append(task)
                used_api_keys.add(api_key_env)
            else:
                next_remaining.append(task)
        batches.append(batch)
        remaining = next_remaining
    return batches


def _probe_orgs(
    orgs: list[OrgConfig],
    profiles: list[profit_model.Profile],
    *,
    db_path: str | None,
    org_parallelism: int,
    profile_parallelism: int,
) -> list[dict[str, Any]]:
    tasks = [
        {
            "org": org,
            "profiles": profiles,
            "db_path": db_path,
            "profile_parallelism": profile_parallelism,
        }
        for org in orgs
    ]
    if org_parallelism <= 1 or len(tasks) <= 1:
        rows: list[dict[str, Any]] = []
        for task in tasks:
            rows.extend(_probe_org_profiles(**task))
        return rows

    rows = []
    max_workers = min(org_parallelism, len(tasks))
    for batch in _batch_org_tasks(tasks, max_workers):
        for org_rows in _run_probe_batch(batch):
            rows.extend(org_rows)
    return rows


def run_once(
    *,
    db_path: str | None = None,
    priorities: tuple[str, ...] = ("batch",),
    profile_limit: int | None = None,
    org_parallelism: int | None = None,
    profile_parallelism: int | None = None,
) -> dict[str, Any]:
    config = load_config()
    no_gpu_sleep_after_seconds = env_int("PRL_NO_GPU_SLEEP_AFTER_SECONDS", 3600)
    no_gpu_sleep_seconds = env_int("PRL_NO_GPU_SLEEP_SECONDS", 900)
    heartbeat_stale_after_seconds = env_int("PRL_AVAILABILITY_STALE_AFTER_SECONDS", 1800)
    selected_org_parallelism = max(1, org_parallelism or env_int("PRL_AVAILABILITY_ORG_PARALLELISM", 2))
    selected_profile_parallelism = max(
        1,
        profile_parallelism or env_int("PRL_AVAILABILITY_PROFILE_PARALLELISM", 4),
    )
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
    enabled_orgs = config.enabled_orgs()
    skipped_zero_balance = [
        skip
        for org in enabled_orgs
        if (skip := explicit_zero_balance_skip(org.label)) is not None
    ]
    skipped_zero_balance_labels = {str(skip["org_label"]) for skip in skipped_zero_balance}
    zero_balance_quota_refreshes = []
    if env_bool("PRL_AVAILABILITY_REFRESH_ZERO_BALANCE_QUOTA", True):
        zero_balance_quota_refreshes = _refresh_quota_only_orgs(
            [org for org in enabled_orgs if org.label in skipped_zero_balance_labels],
            db_path=db_path,
        )

    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.sync_config(conn, config)
        state_db.upsert_gpu_profiles(conn, profiles)
        skipped_no_credits = []
        cleared_no_credits_cooldowns = []
        probe_orgs = []
        for org in enabled_orgs:
            if org.label in skipped_zero_balance_labels:
                continue
            cooldown = state_db.active_org_cooldown(conn, org.label)
            if cooldown is not None:
                positive_balance = explicit_positive_balance_restore(org.label)
                if positive_balance is None:
                    skipped_no_credits.append(cooldown)
                    continue
                clear_row = clear_no_credits_cooldown_row(
                    org.label,
                    cooldown=cooldown,
                    balance_restore=positive_balance,
                )
                state_db.record_search_state(conn, clear_row)
                state_db.record_event(
                    conn,
                    "availability_no_credits_cooldown_cleared",
                    source="availability_probe",
                    message="availability probe cleared no-credits cooldown after fresh positive balance",
                    payload={
                        "cooldown": cooldown,
                        "balance_restore": positive_balance,
                    },
                )
                cleared_no_credits_cooldowns.append(
                    {
                        "org_label": org.label,
                        "cooldown": cooldown,
                        "balance_restore": positive_balance,
                    }
                )
            probe_orgs.append(org)
        state_db.write_heartbeat(
            conn,
            "availability_probe",
            stale_after_seconds=heartbeat_stale_after_seconds,
            payload={
                "running": True,
                "priorities": priorities,
                "profile_count": len(profiles),
                "org_parallelism": selected_org_parallelism,
                "profile_parallelism": selected_profile_parallelism,
                "skipped_zero_balance_orgs": skipped_zero_balance,
                "zero_balance_quota_refreshes": zero_balance_quota_refreshes,
                "skipped_no_credits_orgs": skipped_no_credits,
                "cleared_no_credits_cooldowns": cleared_no_credits_cooldowns,
            },
        )
        conn.commit()

    org_rows = _probe_orgs(
        probe_orgs,
        profiles,
        db_path=db_path,
        org_parallelism=selected_org_parallelism,
        profile_parallelism=selected_profile_parallelism,
    )
    skipped_zero_replica_quota = []
    filtered_org_rows = []
    for row in org_rows:
        if row.get("skip_reason") == "zero_replica_quota":
            skipped_zero_replica_quota.append(
                {
                    "org_label": row["org_label"],
                    **dict(row.get("zero_replica_quota_skip") or {}),
                }
            )
        else:
            filtered_org_rows.append(row)
    org_rows = filtered_org_rows
    for row in org_rows:
        available = row.get("available_count")
        ok = bool(row.get("ok"))
        checked_at = str(row["checked_at_utc"])
        profile_key = str(row["profile_key"])
        org_label = str(row["org_label"])
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
                    (org_label, profile_key),
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
                        "org_label": org_label,
                        "slot_name": "*",
                        "profile_key": profile_key,
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
                        "org_label": org_label,
                        "slot_name": "__availability__",
                        "action": "capacity_failure",
                        "profile_key": profile_key,
                        "ok": False,
                        "payload": {"available_count": available},
                    },
                )
            elif ok and int(available or 0) > 0:
                state_db.record_search_state(
                    conn,
                    {
                        "org_label": org_label,
                        "slot_name": "*",
                        "profile_key": profile_key,
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
            stale_after_seconds=heartbeat_stale_after_seconds,
            payload={
                "probed": len(results),
                "priorities": priorities,
                "by_profile": by_profile,
                "org_parallelism": selected_org_parallelism,
                "profile_parallelism": selected_profile_parallelism,
                "skipped_zero_balance_orgs": skipped_zero_balance,
                "zero_balance_quota_refreshes": zero_balance_quota_refreshes,
                "skipped_no_credits_orgs": skipped_no_credits,
                "cleared_no_credits_cooldowns": cleared_no_credits_cooldowns,
                "skipped_zero_replica_quota_orgs": skipped_zero_replica_quota,
            },
        )
        state_db.record_event(
            conn,
            "availability_probed",
            source="availability_probe",
            message="Salad GPU availability probed",
            payload={
                "probed": len(results),
                "profiles": by_profile,
                "skipped_zero_balance_orgs": skipped_zero_balance,
                "zero_balance_quota_refreshes": zero_balance_quota_refreshes,
                "skipped_no_credits_orgs": skipped_no_credits,
                "cleared_no_credits_cooldowns": cleared_no_credits_cooldowns,
                "skipped_zero_replica_quota_orgs": skipped_zero_replica_quota,
            },
        )
        conn.commit()

    return {
        "probed": len(results),
        "priorities": priorities,
        "org_parallelism": selected_org_parallelism,
        "profile_parallelism": selected_profile_parallelism,
        "skipped_zero_balance_orgs": skipped_zero_balance,
        "zero_balance_quota_refreshes": zero_balance_quota_refreshes,
        "skipped_no_credits_orgs": skipped_no_credits,
        "cleared_no_credits_cooldowns": cleared_no_credits_cooldowns,
        "skipped_zero_replica_quota_orgs": skipped_zero_replica_quota,
        "by_profile": by_profile,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Salad GPU availability and store it in the fleet DB.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--priorities", default="batch", help="Comma-separated priorities to probe.")
    parser.add_argument("--profile-limit", type=int, default=None)
    parser.add_argument("--org-parallelism", type=int, default=None)
    parser.add_argument("--profile-parallelism", type=int, default=None)
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
            emit(
                run_once(
                    db_path=args.db,
                    priorities=priorities,
                    profile_limit=args.profile_limit,
                    org_parallelism=args.org_parallelism,
                    profile_parallelism=args.profile_parallelism,
                )
            )
            time.sleep(args.interval)
    else:
        emit(
            run_once(
                db_path=args.db,
                priorities=priorities,
                profile_limit=args.profile_limit,
                org_parallelism=args.org_parallelism,
                profile_parallelism=args.profile_parallelism,
            )
        )


if __name__ == "__main__":
    main()
