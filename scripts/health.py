#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

import state_db
from fleet_common import json_dumps


def age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - at).total_seconds())


def build_health(db_path: str | None = None) -> dict[str, Any]:
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        status = state_db.status_payload(conn)
        heartbeat_rows = status.get("heartbeats") or []
        failures = status.get("runtime_failures") or []
        guard_issues = [
            dict(row)
            for row in conn.execute(
                """
                SELECT org_label, slot_name, issue_type, first_seen_utc,
                       last_seen_utc, action_count
                FROM guard_issues
                ORDER BY last_seen_utc DESC
                """
            ).fetchall()
        ]
        api_rate_limits = [
            dict(row)
            for row in conn.execute(
                """
                SELECT api_key_env, window_started_utc, request_count,
                       max_requests_per_minute, updated_at_utc
                FROM api_rate_limits
                ORDER BY api_key_env
                """
            ).fetchall()
        ]
        target_count = int(conn.execute("SELECT COUNT(*) AS count FROM slot_targets").fetchone()["count"])
        slot_count = int(conn.execute("SELECT COUNT(*) AS count FROM slots").fetchone()["count"])

    stale_heartbeats = []
    for row in heartbeat_rows:
        age = age_seconds(row.get("at_utc"))
        stale_after = int(row.get("stale_after_seconds") or 0)
        if age is not None and stale_after and age > stale_after:
            stale_heartbeats.append(
                {
                    "process_name": row["process_name"],
                    "age_seconds": round(age, 1),
                    "stale_after_seconds": stale_after,
                }
            )

    health = "healthy"
    if failures or stale_heartbeats:
        health = "degraded"
    if slot_count > 0 and target_count == 0:
        health = "down"

    return {
        "health": health,
        "db": status["db"],
        "slot_count": slot_count,
        "target_count": target_count,
        "stale_heartbeats": stale_heartbeats,
        "runtime_failures": failures,
        "guard_issues": guard_issues,
        "api_rate_limits": api_rate_limits,
        "latest_risk_mode": status.get("latest_risk_mode"),
        "latest_price_sample": status.get("latest_price_sample"),
        "slot_status": status.get("slot_status"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only health check for the Salad PRL fleet scheduler DB.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = build_health(args.db)
    if args.json:
        print(json_dumps(payload))
        return
    print(f"health={payload['health']} targets={payload['target_count']}/{payload['slot_count']}")
    if payload["stale_heartbeats"]:
        print(f"stale_heartbeats={len(payload['stale_heartbeats'])}")
    if payload["runtime_failures"]:
        print(f"runtime_failures={len(payload['runtime_failures'])}")
    if payload["guard_issues"]:
        print(f"guard_issues={len(payload['guard_issues'])}")
    if payload["api_rate_limits"]:
        print(f"api_rate_limits={len(payload['api_rate_limits'])}")


if __name__ == "__main__":
    main()
