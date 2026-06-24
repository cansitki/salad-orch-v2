#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from typing import Any

import state_db
from fleet_common import json_dumps


RETENTION_TABLES = {
    "events": ("at_utc", 7),
    "attempts": ("at_utc", 14),
    "profit_snapshots": ("at_utc", 14),
    "price_history": ("sampled_at_utc", 14),
    "risk_modes": ("at_utc", 14),
    "profile_availability": ("checked_at_utc", 3),
}


def _count_old_rows(conn, table: str, column: str, days: int) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE julianday({column}) < julianday('now', ?)",
        (f"-{days} days",),
    ).fetchone()
    return int(row["count"])


def _delete_old_rows(conn, table: str, column: str, days: int) -> int:
    before = _count_old_rows(conn, table, column, days)
    conn.execute(
        f"DELETE FROM {table} WHERE julianday({column}) < julianday('now', ?)",
        (f"-{days} days",),
    )
    return before


def maintenance_once(
    *,
    db_path: str | None = None,
    dry_run: bool = True,
    retention_days: dict[str, int] | None = None,
    vacuum: bool = False,
) -> dict[str, Any]:
    retention_days = retention_days or {}
    deleted: dict[str, int] = {}
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        for table, (column, default_days) in RETENTION_TABLES.items():
            days = int(retention_days.get(table, default_days))
            if days <= 0:
                continue
            deleted[table] = _count_old_rows(conn, table, column, days) if dry_run else _delete_old_rows(conn, table, column, days)
        if dry_run:
            conn.rollback()
        else:
            state_db.record_event(
                conn,
                "maintenance_retention",
                source="maintenance",
                message="old scheduler rows pruned",
                payload={"deleted": deleted, "vacuum": vacuum},
            )
            state_db.write_heartbeat(conn, "maintenance", payload={"deleted": deleted, "vacuum": vacuum})
            conn.commit()
    if vacuum and not dry_run:
        with state_db.connect(db_path) as conn:
            conn.execute("VACUUM")
    return {"dry_run": dry_run, "deleted": deleted, "vacuum": bool(vacuum and not dry_run)}


def _retention_overrides(values: list[str]) -> dict[str, int]:
    overrides: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"invalid --retention value {value!r}; expected table=days")
        table, raw_days = value.split("=", 1)
        table = table.strip()
        if table not in RETENTION_TABLES:
            raise SystemExit(f"unknown retention table {table!r}")
        overrides[table] = int(raw_days)
    return overrides


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune and compact Salad PRL scheduler state.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--apply", action="store_true", help="Delete rows. Default is dry-run.")
    parser.add_argument("--vacuum", action="store_true", help="Run VACUUM after deleting rows.")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=21600, help="Loop interval in seconds.")
    parser.add_argument(
        "--retention",
        action="append",
        default=[],
        metavar="TABLE=DAYS",
        help="Override retention days for one table.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    retention = _retention_overrides(args.retention)

    def run_and_print() -> None:
        payload = maintenance_once(
            db_path=args.db,
            dry_run=not args.apply,
            retention_days=retention,
            vacuum=args.vacuum,
        )
        if args.json:
            print(json_dumps(payload))
            return
        mode = "dry-run" if payload["dry_run"] else "applied"
        print(f"maintenance {mode}")
        for table, count in payload["deleted"].items():
            print(f"  {table}: {count}")
        if payload["vacuum"]:
            print("  vacuum: done")

    if args.loop:
        while True:
            run_and_print()
            time.sleep(args.interval)
    else:
        run_and_print()


if __name__ == "__main__":
    main()
