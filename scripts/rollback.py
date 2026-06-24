#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from typing import Any

import state_db
from fleet_common import json_dumps


def create_checkpoint(db_path: str | None, *, name: str, stage: str) -> dict[str, Any]:
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        row = state_db.create_rollout_checkpoint(conn, name=name, stage=stage)
        conn.commit()
    return {
        "action": "create",
        "id": row["id"],
        "name": row["name"],
        "stage": row["stage"],
        "target_count": row["target_count"],
        "created_at_utc": row["created_at_utc"],
    }


def list_checkpoints(db_path: str | None, *, limit: int = 20) -> dict[str, Any]:
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        rows = state_db.list_rollout_checkpoints(conn, limit=limit)
    return {"action": "list", "checkpoints": rows}


def restore_checkpoint(db_path: str | None, *, checkpoint_id: int, apply: bool) -> dict[str, Any]:
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        checkpoint = state_db.get_rollout_checkpoint(conn, checkpoint_id)
        if checkpoint is None:
            raise SystemExit(f"unknown rollout checkpoint {checkpoint_id}")
        targets = list((checkpoint.get("payload") or {}).get("slot_targets") or [])
        if apply:
            restored = state_db.restore_slot_targets_from_checkpoint(conn, checkpoint_id)
            conn.commit()
        else:
            restored = {"id": checkpoint_id, "target_count": len(targets)}
    return {
        "action": "restore",
        "apply": apply,
        "id": checkpoint_id,
        "name": checkpoint["name"],
        "stage": checkpoint["stage"],
        "target_count": restored["target_count"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and restore rollout checkpoints for scheduler targets.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Save current slot_targets as a rollback checkpoint.")
    create.add_argument("--name", default="manual")
    create.add_argument("--stage", default="manual")

    list_cmd = sub.add_parser("list", help="List rollout checkpoints.")
    list_cmd.add_argument("--limit", type=int, default=20)

    restore = sub.add_parser("restore", help="Restore slot_targets from a checkpoint. Dry-run by default.")
    restore.add_argument("id", type=int)
    restore.add_argument("--apply", action="store_true")

    args = parser.parse_args()
    if args.command == "create":
        payload = create_checkpoint(args.db, name=args.name, stage=args.stage)
    elif args.command == "list":
        payload = list_checkpoints(args.db, limit=args.limit)
    elif args.command == "restore":
        payload = restore_checkpoint(args.db, checkpoint_id=args.id, apply=args.apply)
    else:
        raise AssertionError(args.command)

    if args.json:
        print(json_dumps(payload))
        return
    if payload["action"] == "list":
        if not payload["checkpoints"]:
            print("no rollout checkpoints")
        for row in payload["checkpoints"]:
            print(f"{row['id']}: {row['created_at_utc']} {row['stage']} {row['name']} targets={row['target_count']}")
    elif payload["action"] == "create":
        print(f"checkpoint {payload['id']} created targets={payload['target_count']} name={payload['name']} stage={payload['stage']}")
    elif payload["action"] == "restore":
        mode = "applied" if payload["apply"] else "dry-run"
        print(f"restore {mode} checkpoint={payload['id']} targets={payload['target_count']} name={payload['name']}")
        if not payload["apply"]:
            print("pass --apply to write slot_targets")
    sys.exit(0)


if __name__ == "__main__":
    main()
