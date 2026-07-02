#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

import reporter
import salad_prl_profit_snapshot
import state_db
from config_loader import load_config
from fleet_common import compact_json, env_float, json_dumps, safe_public_payload, utc_now


MONEY_RE = re.compile(r"-?\d+(?:\.\d+)?")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "")
    match = MONEY_RE.search(text)
    if not match:
        return None
    parsed = float(match.group(0))
    stripped = text.strip()
    if parsed > 0 and (stripped.startswith("-") or (stripped.startswith("(") and stripped.endswith(")"))):
        return -parsed
    return parsed


def latest_slot_profit_by_org(conn, enabled_org_labels: set[str] | None = None) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for row in latest_slot_profit_by_key(conn, enabled_org_labels).values():
        org = str(row["org_label"])
        item = summary.setdefault(
            org,
            {
                "org_label": org,
                "billable_slots": 0,
                "th": 0.0,
                "cost_day": 0.0,
                "profit_day": 0.0,
            },
        )
        item["billable_slots"] += 1
        item["th"] += float(row.get("th") or 0)
        item["cost_day"] += float(row.get("cost_day") or 0)
        item["profit_day"] += float(row.get("profit_day") or 0)
    return summary


def latest_slot_profit_by_key(conn, enabled_org_labels: set[str] | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    latest = conn.execute(
        "SELECT at_utc FROM profit_snapshots WHERE scope = 'slot' ORDER BY at_utc DESC, id DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        return {}
    rows = conn.execute(
        """
        SELECT p.id, p.org_label, p.slot_name, p.profile_key, p.th,
               p.cost_day, p.revenue_day, p.profit_day
        FROM profit_snapshots p
        JOIN slots s
          ON s.org_label = p.org_label
         AND s.slot_name = p.slot_name
        WHERE p.scope = 'slot'
          AND p.at_utc = ?
          AND COALESCE(s.observed_status, '') IN ('running', 'deploying', 'creating', 'allocating')
        ORDER BY p.org_label, p.slot_name, p.id
        """,
        (latest["at_utc"],),
    ).fetchall()
    return {
        (str(row["org_label"]), str(row["slot_name"])): dict(row)
        for row in rows
        if enabled_org_labels is None or str(row["org_label"]) in enabled_org_labels
    }


def org_runtime_summary(conn, enabled_org_labels: set[str] | None = None) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    status_rows = conn.execute(
        """
        SELECT org_label, COALESCE(observed_status, 'unknown') AS observed_status, COUNT(*) AS count
        FROM slots
        GROUP BY org_label, COALESCE(observed_status, 'unknown')
        """
    ).fetchall()
    for row in status_rows:
        org = str(row["org_label"])
        if enabled_org_labels is not None and org not in enabled_org_labels:
            continue
        item = summary.setdefault(
            org,
            {
                "active_slots": 0,
                "running_slots": 0,
                "creating_slots": 0,
                "allocating_slots": 0,
                "status_counts": {},
            },
        )
        status = str(row["observed_status"])
        count = int(row["count"] or 0)
        item["status_counts"][status] = count
        if status in {"running", "creating", "allocating", "deploying"}:
            item["active_slots"] += count
        if status == "running":
            item["running_slots"] += count
        elif status == "creating":
            item["creating_slots"] += count
        elif status == "allocating":
            item["allocating_slots"] += count

    worker_rows = conn.execute(
        """
        SELECT org_label,
               COUNT(*) AS live_hashing_gpus,
               SUM(COALESCE(reported_hashrate_th, 0)) AS live_th
        FROM workers
        WHERE stale = 0 AND COALESCE(reported_hashrate_th, 0) > 0
        GROUP BY org_label
        """
    ).fetchall()
    for row in worker_rows:
        org = str(row["org_label"])
        if enabled_org_labels is not None and org not in enabled_org_labels:
            continue
        item = summary.setdefault(
            org,
            {
                "active_slots": 0,
                "running_slots": 0,
                "creating_slots": 0,
                "allocating_slots": 0,
                "status_counts": {},
            },
        )
        item["live_hashing_gpus"] = int(row["live_hashing_gpus"] or 0)
        item["live_th"] = float(row["live_th"] or 0)

    profits = latest_slot_profit_by_org(conn, enabled_org_labels)
    for org, item in summary.items():
        profit = profits.get(org, {})
        item.setdefault("live_hashing_gpus", 0)
        item.setdefault("live_th", 0.0)
        item["cost_day"] = float(profit.get("cost_day") or 0)
        item["profit_day"] = float(profit.get("profit_day") or 0)
        item["billable_slots"] = int(profit.get("billable_slots") or 0)
    return summary


def record_slot_active_snapshots(
    conn,
    snapshot_id: int,
    enabled_org_labels: set[str] | None = None,
) -> int:
    profits = latest_slot_profit_by_key(conn, enabled_org_labels)
    rows = conn.execute(
        """
        SELECT s.org_label,
               s.slot_name,
               s.slot_index,
               s.desired_profile_key,
               s.observed_profile_key,
               s.observed_status,
               s.live_hashrate_th,
               s.protected,
               s.updated_at_utc,
               s.observed_profile_since_utc,
               s.observed_status_since_utc,
               t.profile_key AS target_profile_key,
               t.mode AS target_mode,
               t.decision_price_usd AS target_decision_price_usd,
               t.expected_profit_day AS target_expected_profit_day,
               t.protected AS target_protected,
               t.reason AS target_reason,
               t.assigned_at_utc AS target_assigned_at_utc
        FROM slots s
        LEFT JOIN slot_targets t
          ON t.org_label = s.org_label AND t.slot_name = s.slot_name
        ORDER BY s.org_label, s.slot_index, s.slot_name
        """
    ).fetchall()
    rows = [
        row
        for row in rows
        if enabled_org_labels is None or str(row["org_label"]) in enabled_org_labels
    ]
    for row in rows:
        key = (str(row["org_label"]), str(row["slot_name"]))
        profit = profits.get(key, {})
        conn.execute(
            """
            INSERT INTO fleet_slot_active_snapshots(
              snapshot_id, org_label, slot_name, slot_index, observed_status,
              desired_profile_key, observed_profile_key, target_profile_key,
              target_mode, target_reason, protected, live_hashrate_th, billable,
              cost_day, profit_day, updated_at_utc, observed_profile_since_utc,
              observed_status_since_utc, payload_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                row["org_label"],
                row["slot_name"],
                int(row["slot_index"] or 0),
                row["observed_status"],
                row["desired_profile_key"],
                row["observed_profile_key"],
                row["target_profile_key"],
                row["target_mode"],
                row["target_reason"],
                int(row["protected"] or 0),
                float(row["live_hashrate_th"] or 0),
                1 if profit else 0,
                profit.get("cost_day"),
                profit.get("profit_day"),
                row["updated_at_utc"],
                row["observed_profile_since_utc"],
                row["observed_status_since_utc"],
                compact_json(
                    safe_public_payload(
                        {
                            "profit_profile_key": profit.get("profile_key"),
                            "profit_th": profit.get("th"),
                            "profit_revenue_day": profit.get("revenue_day"),
                            "target_decision_price_usd": row["target_decision_price_usd"],
                            "target_expected_profit_day": row["target_expected_profit_day"],
                            "target_protected": row["target_protected"],
                            "target_assigned_at_utc": row["target_assigned_at_utc"],
                        }
                    )
                ),
            ),
        )
    return len(rows)


def refresh_profit_snapshot(db_path: str | None = None, *, price: float | None = None) -> dict[str, Any]:
    snapshot_price = price
    if snapshot_price is None:
        configured = os.environ.get("PRL_AUDIT_PROFIT_SNAPSHOT_PRICE")
        snapshot_price = float(configured) if configured else salad_prl_profit_snapshot.default_snapshot_price()
    snapshot = salad_prl_profit_snapshot.build_snapshot(float(snapshot_price))
    salad_prl_profit_snapshot.write_snapshot_db(snapshot, db_path=db_path, decision_price=float(snapshot_price))
    return snapshot


def record_active_snapshot(
    db_path: str | None = None,
    *,
    refresh_profit: bool = False,
    profit_snapshot_price: float | None = None,
) -> dict[str, Any]:
    config = load_config()
    enabled_org_labels = {org.label for org in config.enabled_orgs()}
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.sync_config(conn, config)
        conn.commit()
    if refresh_profit:
        refresh_profit_snapshot(db_path, price=profit_snapshot_price)
    report = reporter.build_report(db_path)
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.sync_config(conn, config)
        org_summary = org_runtime_summary(conn, enabled_org_labels)
        profit_064 = report.get("profit_at_0_64") or {}
        profit_live = report.get("profit_at_live") or {}
        cursor = conn.execute(
            """
            INSERT INTO fleet_active_snapshots(
              at_utc, assigned_targets, target_slots, live_hashing_gpus, live_th,
              cost_day, profit_day_064, market_profit_day, status_counts_json,
              org_summary_json, payload_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                int(report.get("assigned_targets") or 0),
                int(report.get("target_slots") or 0),
                int(report.get("live_hashing_gpus") or 0),
                float(report.get("live_th") or 0),
                profit_064.get("cost_day"),
                profit_064.get("profit_day"),
                profit_live.get("market_profit_day"),
                compact_json(report.get("status_counts") or {}),
                compact_json(safe_public_payload(org_summary)),
                compact_json(
                    safe_public_payload(
                        {
                            "refresh_error": report.get("refresh_error"),
                            "negative_slots": report.get("negative_slots") or [],
                            "running_no_live_billable_slots": report.get("running_no_live_billable_slots") or [],
                            "stuck_slots": report.get("stuck_slots") or [],
                        }
                    )
                ),
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        for org in config.enabled_orgs():
            item = org_summary.get(org.label, {})
            conn.execute(
                """
                INSERT INTO fleet_org_active_snapshots(
                  snapshot_id, org_label, active_slots, running_slots, creating_slots,
                  allocating_slots, live_hashing_gpus, live_th, cost_day, profit_day,
                  payload_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    org.label,
                    int(item.get("active_slots") or 0),
                    int(item.get("running_slots") or 0),
                    int(item.get("creating_slots") or 0),
                    int(item.get("allocating_slots") or 0),
                    int(item.get("live_hashing_gpus") or 0),
                    float(item.get("live_th") or 0),
                    float(item.get("cost_day") or 0),
                    float(item.get("profit_day") or 0),
                    compact_json(safe_public_payload(item)),
                ),
            )
        slot_snapshot_count = record_slot_active_snapshots(conn, snapshot_id, enabled_org_labels)
        state_db.write_heartbeat(
            conn,
            "fleet_audit",
            stale_after_seconds=900,
            payload={
                "last_active_snapshot_id": snapshot_id,
                "assigned_targets": report.get("assigned_targets"),
                "target_slots": report.get("target_slots"),
                "live_hashing_gpus": report.get("live_hashing_gpus"),
                "cost_day": profit_064.get("cost_day"),
                "slot_snapshots": slot_snapshot_count,
            },
        )
        state_db.record_event(
            conn,
            "fleet_active_snapshot",
            source="fleet_audit",
            message="fleet active GPU snapshot recorded",
            payload={"snapshot_id": snapshot_id, "orgs": len(org_summary), "slots": slot_snapshot_count},
        )
        conn.commit()
    return {
        "snapshot_id": snapshot_id,
        "assigned_targets": report.get("assigned_targets"),
        "target_slots": report.get("target_slots"),
        "live_hashing_gpus": report.get("live_hashing_gpus"),
        "live_th": report.get("live_th"),
        "cost_day": profit_064.get("cost_day"),
        "profit_day_064": profit_064.get("profit_day"),
        "market_profit_day": profit_live.get("market_profit_day"),
        "org_summary": org_summary,
        "slot_snapshots": slot_snapshot_count,
    }


def load_monitor_db_balances(monitor_db: str, *, max_age_seconds: float | None = None) -> tuple[dict[str, float | None], str] | None:
    path = pathlib.Path(monitor_db)
    if not path.exists():
        return None
    max_age = env_float("PRL_BALANCE_SOURCE_MAX_AGE_SECONDS", 7200.0) if max_age_seconds is None else max_age_seconds
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT b.org, b.balance_usd, b.amount_cents, s.checked_at_utc
                FROM salad_org_balances b
                JOIN monitor_snapshots s ON s.id = b.snapshot_id
                JOIN (
                  SELECT org, MAX(snapshot_id) AS snapshot_id
                  FROM salad_org_balances
                  WHERE ok = 1 AND amount_cents IS NOT NULL
                  GROUP BY org
                ) latest ON latest.org = b.org AND latest.snapshot_id = b.snapshot_id
                WHERE b.ok = 1
                  AND b.amount_cents IS NOT NULL
                """
            ).fetchall()
    except sqlite3.Error:
        return {}, f"monitor_db_error:{path}"
    balances: dict[str, float | None] = {}
    ages: list[float] = []
    for row in rows:
        checked_at = row["checked_at_utc"]
        if checked_at:
            try:
                ages.append(max(0.0, (datetime.now(UTC) - parse_utc(str(checked_at))).total_seconds()))
            except ValueError:
                pass
        value = row["balance_usd"]
        if value is None and row["amount_cents"] is not None:
            value = float(row["amount_cents"]) / 100.0
        balances[str(row["org"])] = parse_money(value)
    age = max(ages) if ages else None
    if age is not None and max_age >= 0 and age > max_age:
        return {}, f"stale_monitor_db:{path}:age_seconds={round(age)}"
    return balances, f"monitor_db:{path}"


def load_balance_values(
    *,
    balance_json: str | None = None,
    balance_file: str | None = None,
    monitor_db: str | None = None,
) -> tuple[dict[str, float | None], str]:
    raw: Any = None
    source = "unavailable"
    if balance_json:
        try:
            raw = json.loads(balance_json)
        except json.JSONDecodeError:
            return {}, "invalid_json"
        source = "json"
    elif balance_file:
        path = pathlib.Path(balance_file)
        if path.exists():
            max_age = env_float("PRL_BALANCE_FILE_MAX_AGE_SECONDS", 7200.0)
            age = max(0.0, time.time() - path.stat().st_mtime)
            if max_age >= 0 and age > max_age:
                source = f"stale_file:{path}:age_seconds={round(age)}"
                raw = None
            else:
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    return {}, f"invalid_json:file:{path}"
                source = f"file:{path}"
        else:
            source = f"missing_file:{path}"
    if not isinstance(raw, dict):
        monitor_source = monitor_db or os.environ.get("PRL_AUDIT_MONITOR_DB")
        if monitor_source:
            try:
                loaded = load_monitor_db_balances(monitor_source)
                if loaded is not None:
                    return loaded
            except Exception:
                return {}, source
        return {}, source
    return {str(key): parse_money(value) for key, value in raw.items()}, source


def latest_active_cost_day(conn, org_label: str) -> float:
    row = conn.execute(
        """
        SELECT cost_day
        FROM fleet_org_active_snapshots
        WHERE org_label = ?
        ORDER BY snapshot_id DESC
        LIMIT 1
        """,
        (org_label,),
    ).fetchone()
    return float(row["cost_day"] or 0) if row else 0.0


def previous_balance_audit(conn, org_label: str) -> Any | None:
    return conn.execute(
        """
        SELECT *
        FROM fleet_org_balance_audits
        WHERE org_label = ? AND balance_ok = 1 AND balance_usd IS NOT NULL
        ORDER BY at_utc DESC, id DESC
        LIMIT 1
        """,
        (org_label,),
    ).fetchone()


def average_cost_day_since(conn, org_label: str, since_utc: str, fallback_cost_day: float) -> float:
    row = conn.execute(
        """
        SELECT AVG(os.cost_day) AS avg_cost_day
        FROM fleet_org_active_snapshots os
        JOIN fleet_active_snapshots fs ON fs.id = os.snapshot_id
        WHERE os.org_label = ?
          AND julianday(fs.at_utc) >= julianday(?)
        """,
        (org_label, since_utc),
    ).fetchone()
    if row and row["avg_cost_day"] is not None:
        return float(row["avg_cost_day"] or 0)
    return fallback_cost_day


def record_balance_audits(
    *,
    db_path: str | None = None,
    balances: dict[str, float | None] | None = None,
    balance_source: str = "unavailable",
) -> list[dict[str, Any]]:
    balances = balances or {}
    config = load_config()
    now = utc_now()
    tolerance_usd = env_float("PRL_BALANCE_AUDIT_TOLERANCE_USD", 1.0)
    tolerance_ratio = env_float("PRL_BALANCE_AUDIT_TOLERANCE_RATIO", 0.35)
    results: list[dict[str, Any]] = []
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.sync_config(conn, config)
        for org in config.enabled_orgs():
            balance = balances.get(org.label)
            balance_ok = balance is not None
            cost_day = latest_active_cost_day(conn, org.label)
            previous = previous_balance_audit(conn, org.label)
            previous_balance = float(previous["balance_usd"]) if previous else None
            previous_at = str(previous["at_utc"]) if previous else None
            elapsed_hours = None
            expected_cost = None
            balance_delta = None
            variance = None
            status = "unavailable"
            if balance_ok and previous is None:
                status = "baseline"
            elif balance_ok and previous is not None:
                elapsed_hours = max(0.0, (parse_utc(now) - parse_utc(previous_at)).total_seconds() / 3600.0)
                avg_cost_day = average_cost_day_since(conn, org.label, previous_at, cost_day)
                expected_cost = avg_cost_day * elapsed_hours / 24.0
                balance_delta = previous_balance - float(balance)
                variance = balance_delta - expected_cost
                tolerance = max(tolerance_usd, abs(expected_cost) * tolerance_ratio)
                status = "ok" if abs(variance) <= tolerance else "mismatch"
            payload = {
                "tolerance_usd": tolerance_usd,
                "tolerance_ratio": tolerance_ratio,
                "source": balance_source,
            }
            conn.execute(
                """
                INSERT INTO fleet_org_balance_audits(
                  at_utc, org_label, balance_usd, balance_source, balance_ok,
                  previous_balance_usd, previous_at_utc, elapsed_hours, cost_day,
                  expected_cost_usd, balance_delta_usd, variance_usd, status,
                  payload_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    org.label,
                    balance,
                    balance_source,
                    1 if balance_ok else 0,
                    previous_balance,
                    previous_at,
                    elapsed_hours,
                    cost_day,
                    expected_cost,
                    balance_delta,
                    variance,
                    status,
                    compact_json(safe_public_payload(payload)),
                ),
            )
            result = {
                "org_label": org.label,
                "balance_usd": balance,
                "balance_source": balance_source,
                "balance_ok": balance_ok,
                "cost_day": cost_day,
                "expected_cost_usd": expected_cost,
                "balance_delta_usd": balance_delta,
                "variance_usd": variance,
                "status": status,
            }
            results.append(result)
        statuses = [row["status"] for row in results]
        has_bad_status = any(status in {"unavailable", "mismatch"} for status in statuses)
        state_db.record_event(
            conn,
            "fleet_balance_audit",
            source="fleet_audit",
            level="warning" if has_bad_status else "info",
            message="fleet balance audit recorded",
            payload={"orgs": len(results), "source": balance_source, "statuses": statuses},
        )
        state_db.write_heartbeat(
            conn,
            "fleet_balance_audit",
            status="degraded" if has_bad_status else "ok",
            stale_after_seconds=7200,
            payload={"orgs": len(results), "source": balance_source, "statuses": statuses},
        )
        conn.commit()
    return results


def record_runtime_failure(db_path: str | None, component: str, exc: Exception) -> None:
    try:
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            state_db.record_failure(
                conn,
                component,
                severity="warning",
                error_type=type(exc).__name__,
                message=str(exc)[:300],
            )
            state_db.write_heartbeat(
                conn,
                component,
                status="degraded",
                stale_after_seconds=900,
                payload={"error_type": type(exc).__name__, "message": str(exc)[:300]},
            )
            conn.commit()
    except Exception:
        return


def balance_audit_due(db_path: str | None, interval_seconds: int) -> bool:
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        row = conn.execute("SELECT MAX(at_utc) AS at_utc FROM fleet_org_balance_audits").fetchone()
    if row is None or not row["at_utc"]:
        return True
    age = (datetime.now(UTC) - parse_utc(str(row["at_utc"]))).total_seconds()
    return age >= interval_seconds


def run_once(
    *,
    db_path: str | None = None,
    force_balance: bool = False,
    balance_interval_seconds: int = 3600,
    balance_json: str | None = None,
    balance_file: str | None = None,
    monitor_db: str | None = None,
    refresh_profit_snapshot: bool = False,
    profit_snapshot_price: float | None = None,
) -> dict[str, Any]:
    active = record_active_snapshot(
        db_path,
        refresh_profit=refresh_profit_snapshot,
        profit_snapshot_price=profit_snapshot_price,
    )
    balance_results = None
    if force_balance or balance_audit_due(db_path, balance_interval_seconds):
        balances, source = load_balance_values(balance_json=balance_json, balance_file=balance_file, monitor_db=monitor_db)
        balance_results = record_balance_audits(db_path=db_path, balances=balances, balance_source=source)
    return {"active": active, "balance_audits": balance_results}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fleet active-GPU and balance-vs-cost audit recorder.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=300, help="Active GPU snapshot interval.")
    parser.add_argument("--balance-interval", type=int, default=3600, help="Hourly balance audit interval.")
    parser.add_argument("--force-balance", action="store_true", help="Record balance audit on this tick.")
    parser.add_argument("--balance-json", default=None, help="JSON object of org_label to balance USD.")
    parser.add_argument("--balance-file", default=None, help="JSON file of org_label to balance USD, refreshed externally.")
    parser.add_argument("--monitor-db", default=None, help="Existing salad-pearl-monitor SQLite DB with salad_org_balances.")
    parser.add_argument(
        "--refresh-profit-snapshot",
        action="store_true",
        help="Fetch Pearl live workers and persist profit snapshots before recording the fleet audit.",
    )
    parser.add_argument("--profit-snapshot-price", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    while True:
        try:
            payload = run_once(
                db_path=args.db,
                force_balance=args.force_balance,
                balance_interval_seconds=args.balance_interval,
                balance_json=args.balance_json,
                balance_file=args.balance_file,
                monitor_db=args.monitor_db,
                refresh_profit_snapshot=args.refresh_profit_snapshot,
                profit_snapshot_price=args.profit_snapshot_price,
            )
        except Exception as exc:
            record_runtime_failure(args.db, "fleet_audit", exc)
            if args.json:
                print(json_dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:300]}), flush=True)
            else:
                print(f"fleet_audit failed error={type(exc).__name__}: {str(exc)[:180]}", flush=True)
            if args.once or not args.loop:
                raise SystemExit(1) from exc
            time.sleep(max(1, args.interval))
            continue
        if args.json:
            print(json_dumps(payload), flush=True)
        else:
            active = payload["active"]
            print(
                f"fleet_audit snapshot={active['snapshot_id']} "
                f"targets={active['assigned_targets']}/{active['target_slots']} "
                f"live_hashing={active['live_hashing_gpus']} "
                f"cost_day={float(active.get('cost_day') or 0):.3f}",
                flush=True,
            )
            if payload.get("balance_audits") is not None:
                statuses = {row["status"]: 0 for row in payload["balance_audits"]}
                for row in payload["balance_audits"]:
                    statuses[row["status"]] = statuses.get(row["status"], 0) + 1
                print(f"balance_audit statuses={statuses}", flush=True)
        if args.once or not args.loop:
            break
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
