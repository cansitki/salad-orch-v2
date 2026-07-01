#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import time
from typing import Any

import requests

import org_worker
from profit_model import profile_key
import state_db
from config_loader import load_config
from fleet_common import json_dumps, utc_now


def _targets_for_org(db_path: str | None, org_label: str) -> list[dict[str, Any]]:
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        return org_worker.target_rows(conn, org_label)


def _public_http_error(exc: requests.HTTPError) -> tuple[int | None, str]:
    response = exc.response
    if response is None:
        return None, str(exc)[:180]
    return response.status_code, (response.text or str(exc))[:180]


def _existing_container_error(status: int | None, text: str) -> bool:
    lowered = text.lower()
    return status in {400, 409} and (
        "already" in lowered
        or "exist" in lowered
        or "duplicate" in lowered
        or "name" in lowered
        or "replicas_quota_exceeded" in lowered
        or "created_replicas_quota_exceeded" in lowered
    )


def _container_items(watch: Any) -> list[dict[str, Any]]:
    payload = watch.request("GET", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    for key in ("items", "container_groups", "containers"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _status_info(container: dict[str, Any] | None) -> dict[str, Any]:
    if not container:
        return {"exists": False, "status": "missing", "instance_counts": {}, "active_or_pending": False}
    status_obj = container.get("status") or container.get("current_state") or container.get("state") or {}
    if isinstance(status_obj, dict):
        status = str(status_obj.get("status") or "unknown")
        counts = {
            key: int(value or 0)
            for key, value in (status_obj.get("instance_status_counts") or {}).items()
        }
    else:
        status = str(status_obj or "unknown")
        counts = {}
    active_or_pending = status in {"running", "deploying", "creating", "allocating"} or any(
        int(counts.get(key) or 0) > 0
        for key in ("running_count", "creating_count", "allocating_count", "stopping_count")
    )
    return {
        "exists": True,
        "status": status,
        "instance_counts": counts,
        "active_or_pending": active_or_pending,
    }


def _observed_profile_key(watch: Any, target: dict[str, Any], container: dict[str, Any] | None) -> str | None:
    if not container:
        return None
    gpu_by_id = {value: key for key, value in watch.GPU.items()}
    container_payload = container.get("container") or {}
    resources = container_payload.get("resources") or {}
    gpu_ids = resources.get("gpu_classes") or []
    gpu_keys = [gpu_by_id.get(str(gpu_id)) for gpu_id in gpu_ids]
    gpu_keys = [key for key in gpu_keys if key]
    if len(gpu_keys) != 1:
        return str(target.get("profile_key") or "") or None
    priority = str(container.get("priority") or container_payload.get("priority") or target.get("priority") or "")
    memory_mb = int(resources.get("memory") or target.get("memory_mb") or 2048)
    return profile_key(gpu_keys[0], priority, memory_mb)


def _sync_observations(
    *,
    db_path: str | None,
    org_label: str,
    watch: Any,
    targets: list[dict[str, Any]],
    existing_by_name: dict[str, dict[str, Any]],
) -> None:
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        for target in targets:
            slot_name = str(target["slot_name"])
            container = existing_by_name.get(slot_name)
            info = _status_info(container)
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": org_label,
                    "slot_name": slot_name,
                    "observed_profile_key": _observed_profile_key(watch, target, container),
                    "observed_status": info["status"],
                    "protected": bool(info["active_or_pending"]),
                    "reset_observed_age": False,
                },
            )
        state_db.write_heartbeat(
            conn,
            f"fast_fill_sync:{org_label}",
            payload={
                "targets": len(targets),
                "observed": len(existing_by_name),
            },
        )
        conn.commit()


def _fast_apply_one(
    watch: Any,
    target: dict[str, Any],
    *,
    start_after: bool,
    patch_existing: bool,
    touch_active: bool,
) -> dict[str, Any]:
    slot_name = str(target["slot_name"])
    profile_key = str(target["profile_key"])
    candidate = watch.Candidate(
        str(target.get("label") or profile_key),
        str(target["priority"]),
        (str(target["gpu_key"]),),
        int(target["memory_mb"]),
    )
    started = False
    patched = False
    created = False
    action = "create"
    started_at = time.monotonic()
    existing_info = _status_info(target.get("_existing_container"))
    if existing_info["exists"]:
        if existing_info["active_or_pending"] and not touch_active:
            return {
                "slot_name": slot_name,
                "profile_key": profile_key,
                "action": "skip_active_container",
                "ok": True,
                "created": False,
                "patched": False,
                "started": False,
                "status": existing_info["status"],
                "instance_counts": existing_info["instance_counts"],
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            }
        if not patch_existing:
            action = "start_existing"
            try:
                if start_after:
                    watch.request("POST", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers/{slot_name}/start")
                    started = True
                return {
                    "slot_name": slot_name,
                    "profile_key": profile_key,
                    "action": action,
                    "ok": True,
                    "created": created,
                    "patched": patched,
                    "started": started,
                    "status": existing_info["status"],
                    "instance_counts": existing_info["instance_counts"],
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                }
            except requests.HTTPError as exc:
                status, text = _public_http_error(exc)
                return {
                    "slot_name": slot_name,
                    "profile_key": profile_key,
                    "action": f"{action}_failed",
                    "ok": False,
                    "created": created,
                    "patched": patched,
                    "started": started,
                    "status": existing_info["status"],
                    "instance_counts": existing_info["instance_counts"],
                    "error": f"http_{status}: {text}" if status else text,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                }
    try:
        payload = watch.container_payload(slot_name, candidate)
        try:
            watch.request("POST", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers", payload)
            created = True
        except requests.HTTPError as exc:
            status, text = _public_http_error(exc)
            if not _existing_container_error(status, text):
                return {
                    "slot_name": slot_name,
                    "profile_key": profile_key,
                    "action": "create_failed",
                    "ok": False,
                    "error": f"http_{status}: {text}" if status else text,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                }
            if not patch_existing:
                action = "start_existing"
                if start_after:
                    watch.request("POST", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers/{slot_name}/start")
                    started = True
                return {
                    "slot_name": slot_name,
                    "profile_key": profile_key,
                    "action": action,
                    "ok": True,
                    "created": created,
                    "patched": patched,
                    "started": started,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                }
            patch_payload = dict(payload)
            patch_payload.pop("name", None)
            action = "patch"
            watch.request(
                "PATCH",
                f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers/{slot_name}",
                patch_payload,
                patch=True,
            )
            patched = True

        if start_after:
            action = f"{action}+start"
            watch.request("POST", f"/organizations/{watch.ORG}/projects/{watch.PROJECT}/containers/{slot_name}/start")
            started = True

        return {
            "slot_name": slot_name,
            "profile_key": profile_key,
            "action": action,
            "ok": True,
            "created": created,
            "patched": patched,
            "started": started,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        }
    except requests.HTTPError as exc:
        status, text = _public_http_error(exc)
        return {
            "slot_name": slot_name,
            "profile_key": profile_key,
            "action": f"{action}_failed",
            "ok": False,
            "created": created,
            "patched": patched,
            "started": started,
            "error": f"http_{status}: {text}" if status else text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        }
    except Exception as exc:
        return {
            "slot_name": slot_name,
            "profile_key": profile_key,
            "action": f"{action}_failed",
            "ok": False,
            "created": created,
            "patched": patched,
            "started": started,
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        }


def _record_results(db_path: str | None, org_label: str, results: list[dict[str, Any]]) -> None:
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        for result in results:
            state_db.record_attempt(
                conn,
                {
                    "at_utc": utc_now(),
                    "org_label": org_label,
                    "slot_name": result["slot_name"],
                    "profile_key": result.get("profile_key"),
                    "action": f"fast_{result['action']}",
                    "ok": bool(result.get("ok")),
                    "duration_ms": result.get("duration_ms"),
                    "error": result.get("error"),
                    "payload": result,
                },
            )
        state_db.record_event(
            conn,
            "fast_fill_targets",
            source=f"fast_fill:{org_label}",
            message="fast direct target apply finished",
            payload={
                "results": len(results),
                "ok": sum(1 for item in results if item.get("ok")),
                "failed": sum(1 for item in results if not item.get("ok")),
                "actions": _counts(result["action"] for result in results),
            },
        )
        conn.commit()


def _counts(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return counts


def fast_fill_org(
    *,
    db_path: str | None,
    org_label: str,
    workers: int,
    decision_price: float,
    min_profit_day: float,
    start_after: bool,
    patch_existing: bool,
    touch_active: bool,
    skip_live_hashing: bool,
    limit: int,
) -> dict[str, Any]:
    config = load_config()
    orgs = {org.label: org for org in config.enabled_orgs()}
    if org_label not in orgs:
        raise SystemExit(f"org {org_label!r} is not enabled in current config")
    org = orgs[org_label]
    targets = _targets_for_org(db_path, org_label)
    if skip_live_hashing:
        targets = [
            target
            for target in targets
            if not (
                str(target.get("slot_observed_status") or "") == "running"
                and float(target.get("live_worker_th") or 0) > 0
            )
        ]
    if limit > 0:
        targets = targets[:limit]

    watch = org_worker.load_watch_module(org, decision_price=decision_price, min_profit_day=min_profit_day)
    existing_by_name = {
        str(item.get("name") or ""): item
        for item in _container_items(watch)
        if item.get("name")
    }
    _sync_observations(
        db_path=db_path,
        org_label=org_label,
        watch=watch,
        targets=targets,
        existing_by_name=existing_by_name,
    )
    for target in targets:
        target["_existing_container"] = existing_by_name.get(str(target["slot_name"]))
    results: list[dict[str, Any]] = []
    max_workers = max(1, int(workers))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _fast_apply_one,
                watch,
                target,
                start_after=start_after,
                patch_existing=patch_existing,
                touch_active=touch_active,
            ): target
            for target in targets
        }
        for future in concurrent.futures.as_completed(future_map):
            results.append(future.result())

    _record_results(db_path, org_label, results)
    return {
        "org_label": org_label,
        "target_count": len(targets),
        "workers": max_workers,
        "ok": sum(1 for item in results if item.get("ok")),
        "failed": sum(1 for item in results if not item.get("ok")),
        "actions": _counts(result["action"] for result in results),
        "errors": _counts((result.get("error") or "")[:90] for result in results if not result.get("ok")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fast direct apply of already scheduled Salad slot targets.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--org", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--price", type=float, default=0.55)
    parser.add_argument("--min-profit", type=float, default=-999.0)
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--patch-existing", action="store_true")
    parser.add_argument("--touch-active", action="store_true")
    parser.add_argument("--include-live-hashing", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = fast_fill_org(
        db_path=args.db,
        org_label=args.org,
        workers=args.workers,
        decision_price=args.price,
        min_profit_day=args.min_profit,
        start_after=not args.no_start,
        patch_existing=args.patch_existing,
        touch_active=args.touch_active,
        skip_live_hashing=not args.include_live_hashing,
        limit=args.limit,
    )
    if args.json:
        print(json_dumps(payload))
    else:
        print(
            f"fast_fill org={payload['org_label']} ok={payload['ok']}/{payload['target_count']} "
            f"failed={payload['failed']} workers={payload['workers']}"
        )
        for action, count in sorted(payload["actions"].items(), key=lambda item: item[1], reverse=True):
            print(f"{count:>4} {action}")


if __name__ == "__main__":
    main()
