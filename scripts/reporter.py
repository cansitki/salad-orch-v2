#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import signal
from datetime import UTC, datetime
from typing import Any

import guard
import state_db
from config_loader import load_config
from fleet_common import json_dumps


def _age_seconds(at_utc: str | None) -> float | None:
    if not at_utc:
        return None
    try:
        at = datetime.fromisoformat(str(at_utc).replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - at).total_seconds())


def _payload(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        return json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


class RefreshTimeout(RuntimeError):
    pass


def _raise_refresh_timeout(_signum: int, _frame: Any) -> None:
    raise RefreshTimeout("reporter refresh timed out")


def build_report(
    db_path: str | None = None,
    *,
    refresh: bool = False,
    refresh_timeout_seconds: int = 45,
) -> dict[str, Any]:
    config = load_config()
    refresh_error: str | None = None
    if refresh:
        old_handler = signal.signal(signal.SIGALRM, _raise_refresh_timeout)
        signal.setitimer(signal.ITIMER_REAL, max(1, refresh_timeout_seconds))
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            try:
                payload = guard.snapshot.build_snapshot(0.64)
                analysis = guard.analyze_snapshot(payload)
                totals = payload.get("totals") or {}
                prl_day = float(totals.get("prl_day") or 0)
                cost_day = float(totals.get("cost_day") or 0)
                prices = [0.64, 0.70]
                live_price = payload.get("live_market_prl_price")
                if live_price:
                    prices.append(float(live_price))
                for price in prices:
                    revenue_day = prl_day * price
                    state_db.record_profit_snapshot(
                        conn,
                        {
                            "scope": "fleet",
                            "decision_price_usd": price,
                            "live_price_usd": live_price,
                            "th": totals.get("th"),
                            "cost_day": cost_day,
                            "revenue_day": revenue_day,
                            "profit_day": revenue_day - cost_day,
                            "payload": analysis,
                        },
                    )
                state_db.write_heartbeat(conn, "reporter", payload={"refresh": True, "prices": prices})
                state_db.record_event(
                    conn,
                    "reporter_snapshot_refreshed",
                    source="reporter",
                    message="reporter refreshed one live snapshot and derived profit prices",
                    payload={"prices": prices, "issue_count": analysis["issue_count"]},
                )
                conn.commit()
            except Exception as exc:
                refresh_error = f"{type(exc).__name__}: {str(exc)[:180]}"
                state_db.write_heartbeat(conn, "reporter", status="degraded", payload={"refresh_error": refresh_error})
                state_db.record_event(
                    conn,
                    "reporter_snapshot_refresh_failed",
                    source="reporter",
                    level="warning",
                    message="reporter live snapshot refresh failed",
                    payload={"error": refresh_error},
                )
                conn.commit()
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old_handler)

    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        status = state_db.status_payload(conn)
        targets = [
            dict(row)
            for row in conn.execute(
                """
                SELECT org_label, slot_name, profile_key, mode, decision_price_usd,
                       expected_profit_day, reason, assigned_at_utc
                FROM slot_targets
                ORDER BY org_label, slot_name
                """
            ).fetchall()
        ]
        scores = [
            dict(row)
            for row in conn.execute(
                """
                SELECT profile_key, mode, decision_price_usd, expected_profit_day,
                       score, risk_tier, scored_at_utc
                FROM profile_scores
                ORDER BY score DESC
                LIMIT 15
                """
            ).fetchall()
        ]
        latest_profit = conn.execute(
            """
            SELECT *
            FROM profit_snapshots
            WHERE scope = 'fleet'
            ORDER BY at_utc DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        profit_at_prices = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM profit_snapshots
                WHERE scope = 'fleet'
                  AND id IN (
                    SELECT MAX(id)
                    FROM profit_snapshots
                    WHERE scope = 'fleet'
                    GROUP BY ROUND(decision_price_usd, 2)
                  )
                ORDER BY decision_price_usd
                """
            ).fetchall()
        ]
        slot_rows = [dict(row) for row in conn.execute("SELECT * FROM slots ORDER BY org_label, slot_name").fetchall()]
        heartbeats = [dict(row) for row in conn.execute("SELECT * FROM heartbeats ORDER BY process_name").fetchall()]
        risky_profiles = [
            dict(row)
            for row in conn.execute(
                """
                SELECT profile_key, mode, risk_tier, expected_profit_day, score, scored_at_utc
                FROM profile_scores
                WHERE risk_tier NOT IN ('safe_base', 'boost_only')
                ORDER BY score ASC
                LIMIT 20
                """
            ).fetchall()
        ]
    profile_counts: dict[str, int] = {}
    for target in targets:
        profile_counts[str(target["profile_key"])] = profile_counts.get(str(target["profile_key"]), 0) + 1
    status_counts: dict[str, int] = {}
    for slot in slot_rows:
        key = str(slot.get("observed_status") or "unknown")
        status_counts[key] = status_counts.get(key, 0) + 1
    active_pending_statuses = {"running", "creating", "allocating"}
    active_pending_slots = sum(count for key, count in status_counts.items() if key in active_pending_statuses)
    live_hashing_gpus = sum(1 for slot in slot_rows if str(slot.get("observed_status") or "") == "running")
    live_th = sum(float(slot.get("live_hashrate_th") or 0) for slot in slot_rows)
    latest_payload = _payload(latest_profit)
    stuck_slots = [
        {
            "org_label": slot["org_label"],
            "slot_name": slot["slot_name"],
            "observed_status": slot.get("observed_status"),
            "age_seconds": round(age, 1),
        }
        for slot in slot_rows
        if (slot.get("observed_status") in {"creating", "allocating"})
        for age in [_age_seconds(slot.get("updated_at_utc"))]
        if age is not None and age > 600
    ]
    heartbeat_status = []
    for heartbeat in heartbeats:
        age = _age_seconds(heartbeat.get("at_utc"))
        stale_after = int(heartbeat.get("stale_after_seconds") or 0)
        heartbeat_status.append(
            {
                "process_name": heartbeat["process_name"],
                "status": heartbeat["status"],
                "age_seconds": round(age, 1) if age is not None else None,
                "stale_after_seconds": stale_after,
                "stale": bool(age is not None and stale_after and age > stale_after),
            }
        )
    profit_by_price = {
        f"{float(row['decision_price_usd']):.2f}": {
            "profit_day": row["profit_day"],
            "revenue_day": row["revenue_day"],
            "cost_day": row["cost_day"],
            "live_price_usd": row["live_price_usd"],
            "at_utc": row["at_utc"],
        }
        for row in profit_at_prices
    }
    return {
        "target_slots": config.target_slot_count(),
        "refresh_error": refresh_error,
        "assigned_targets": len(targets),
        "active_pending_slots": active_pending_slots,
        "live_hashing_gpus": live_hashing_gpus,
        "live_th": live_th,
        "status_counts": status_counts,
        "running_no_live_billable_slots": latest_payload.get("running_no_live_billable_slots") or [],
        "negative_slots": latest_payload.get("negative_slots") or [],
        "stuck_slots": stuck_slots,
        "profile_counts": profile_counts,
        "latest_profit": dict(latest_profit) if latest_profit else None,
        "profit_by_price": profit_by_price,
        "profit_at_0_64": profit_by_price.get("0.64"),
        "profit_at_0_70": profit_by_price.get("0.70"),
        "profit_at_live": (
            {
                "live_price_usd": latest_profit["live_price_usd"],
                "market_profit_day": latest_payload.get("totals", {}).get("market_profit_day"),
            }
            if latest_profit
            else None
        ),
        "risky_profiles": risky_profiles,
        "heartbeat_status": heartbeat_status,
        "status": status,
        "top_scores": scores,
        "targets": targets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Operator report for the Salad PRL fleet scheduler.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--refresh", action="store_true", help="Fetch fresh live guard snapshots at 0.64 and 0.70.")
    parser.add_argument("--refresh-timeout", type=int, default=45)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(args.db, refresh=args.refresh, refresh_timeout_seconds=args.refresh_timeout)
    if args.json:
        print(json_dumps(report))
        return
    print(f"targets={report['assigned_targets']}/{report['target_slots']}")
    if report.get("refresh_error"):
        print(f"refresh_error={report['refresh_error']}")
    print(
        f"active_pending={report['active_pending_slots']} "
        f"live_hashing={report['live_hashing_gpus']} live_th={float(report['live_th']):.3f}"
    )
    latest = report.get("latest_profit")
    if latest:
        print(
            f"latest_profit=${float(latest.get('profit_day') or 0):.3f}/day "
            f"th={float(latest.get('th') or 0):.3f} cost=${float(latest.get('cost_day') or 0):.3f}/day"
        )
    if report.get("profit_at_0_64"):
        print(f"profit_at_0.64=${float(report['profit_at_0_64'].get('profit_day') or 0):.3f}/day")
    if report.get("profit_at_0_70"):
        print(f"profit_at_0.70=${float(report['profit_at_0_70'].get('profit_day') or 0):.3f}/day")
    print(
        f"no_hash={len(report['running_no_live_billable_slots'])} "
        f"negative={len(report['negative_slots'])} stuck={len(report['stuck_slots'])}"
    )
    print("profile targets:")
    for profile_key, count in sorted(report["profile_counts"].items(), key=lambda item: item[1], reverse=True):
        print(f"  {count:>3} {profile_key}")
    print("top scores:")
    for score in report["top_scores"][:8]:
        print(
            f"  {float(score['score']):>8.2f} {score['profile_key']:<22} "
            f"profit=${float(score['expected_profit_day']):>7.3f}/day tier={score['risk_tier']}"
        )


if __name__ == "__main__":
    main()
