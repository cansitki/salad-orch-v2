#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from typing import Any

import state_db
from fleet_common import json_dumps


def report_once(
    *,
    db_path: str | None = None,
    windows_minutes: tuple[int, ...] = (30, 60),
    limit: int = 20,
    write_heartbeat: bool = False,
) -> dict[str, Any]:
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        summary = state_db.recent_spike_summary(conn, windows_minutes=windows_minutes, limit=limit)
        if write_heartbeat:
            state_db.write_heartbeat(
                conn,
                "spike_report",
                payload={
                    "event_count": summary["event_count"],
                    "unstable_profiles": sum(1 for row in summary["profiles"] if row.get("unstable")),
                    "top_profiles": summary["profiles"][:5],
                },
            )
            state_db.record_event(
                conn,
                "spike_report_refreshed",
                source="spike_report",
                message="recent guard spike summary refreshed",
                payload=summary,
            )
            conn.commit()
        return summary


def print_table(summary: dict[str, Any]) -> None:
    print(
        f"spike_events={summary['event_count']} "
        f"windows={','.join(str(item) for item in summary['windows_minutes'])}m"
    )
    print("profiles:")
    for row in summary["profiles"]:
        mark = "BAD" if row.get("unstable") else "ok "
        print(
            f"{mark} {str(row['profile_key']):<24} "
            f"30m={int(row.get('spikes_30m') or 0):>2} "
            f"60m={int(row.get('spikes_60m') or 0):>2} "
            f"slots60={int(row.get('affected_slots_60m') or 0):>2} "
            f"worst60={float(row.get('worst_profit_day_60m') or 0):>7.3f} "
            f"last={row.get('last_seen_utc')}"
        )
    print("slots:")
    for row in summary["slots"]:
        print(
            f"{str(row['org_label'])}/{str(row['slot_name']):<24} "
            f"{str(row['profile_key']):<24} "
            f"30m={int(row.get('spikes_30m') or 0):>2} "
            f"60m={int(row.get('spikes_60m') or 0):>2} "
            f"worst60={float(row.get('worst_profit_day_60m') or 0):>7.3f} "
            f"last={row.get('last_seen_utc')}"
        )


def parse_windows(raw: str) -> tuple[int, ...]:
    values = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    if not values:
        raise argparse.ArgumentTypeError("at least one window is required")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="Report recent negative/no-hash spike history by profile and slot.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--windows", type=parse_windows, default=(30, 60), help="Comma-separated minute windows.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--heartbeat", action="store_true", help="Persist a spike_report heartbeat and event.")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    def run_and_print() -> None:
        summary = report_once(
            db_path=args.db,
            windows_minutes=args.windows,
            limit=args.limit,
            write_heartbeat=args.heartbeat,
        )
        if args.json:
            print(json_dumps(summary), flush=True)
        else:
            print_table(summary)

    if args.loop:
        while True:
            run_and_print()
            time.sleep(args.interval)
    else:
        run_and_print()


if __name__ == "__main__":
    main()
