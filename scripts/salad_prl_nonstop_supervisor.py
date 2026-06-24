#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import time
from datetime import UTC, datetime
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
STATE_DIR = pathlib.Path(os.environ.get("SALAD_PRL_STATE_DIR", str(REPO_ROOT / "state")))
LOG_DIR = STATE_DIR / "logs"
LOG = pathlib.Path(os.environ.get("PRL_SUPERVISOR_LOG", str(LOG_DIR / "kray_prl_nonstop_supervisor.log")))
START_SCRIPT = pathlib.Path(os.environ.get("PRL_START_WATCHERS_SCRIPT", str(SCRIPT_DIR / "start_watchers.sh")))
REQUIRED_SESSIONS = (
    "kray-prl-watch",
    "kry1-prl-watch",
    "kray2-prl-watch",
    "kray3-prl-watch",
    "kray-prl-guard",
)
FORBIDDEN_SESSIONS: tuple[str, ...] = ()
FULL_LIVE_WORKERS_PER_ORG = 10
HEARTBEAT_LOGS = {
    "kray-prl-watch": LOG_DIR / "kray_prl_watch.log",
    "kry1-prl-watch": LOG_DIR / "kry1_prl_watch.log",
    "kray2-prl-watch": LOG_DIR / "kray2_prl_watch.log",
    "kray3-prl-watch": LOG_DIR / "kray3_prl_watch.log",
    "kray-prl-guard": LOG_DIR / "prl_nohash_guard.log",
}
WATCHER_LOGS = {
    "kray": LOG_DIR / "kray_prl_watch.log",
    "kry1": LOG_DIR / "kry1_prl_watch.log",
    "kray2": LOG_DIR / "kray2_prl_watch.log",
    "kray3": LOG_DIR / "kray3_prl_watch.log",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(event: str, **fields: Any) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": utc_now(), "event": event, **fields}, sort_keys=True) + "\n")


def run(
    args: list[str],
    *,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = None
    if env:
        merged_env = os.environ.copy()
        merged_env.update(env)
    return subprocess.run(args, cwd=str(REPO_ROOT), text=True, capture_output=True, check=check, env=merged_env)


def tmux_has_session(name: str) -> bool:
    result = run(["tmux", "has-session", "-t", name])
    return result.returncode == 0


def kill_session(name: str, reason: str) -> None:
    if not tmux_has_session(name):
        return
    result = run(["tmux", "kill-session", "-t", name])
    log("forbidden_session_killed", session=name, reason=reason, returncode=result.returncode)


def parse_time(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(UTC).timestamp()
    except ValueError:
        return None


def latest_log_timestamp(path: pathlib.Path) -> float | None:
    if not path.exists():
        return None
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-300:]):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = parse_time(row.get("at") or row.get("at_utc"))
        if ts is not None:
            return ts
    return None


def sessions_missing() -> list[str]:
    return [session for session in REQUIRED_SESSIONS if not tmux_has_session(session)]


def stale_sessions(max_age_seconds: int) -> list[dict[str, Any]]:
    now_ts = time.time()
    stale: list[dict[str, Any]] = []
    for session, path in HEARTBEAT_LOGS.items():
        ts = latest_log_timestamp(path)
        if ts is None:
            stale.append({"session": session, "log": str(path), "age_seconds": None})
            continue
        age = now_ts - ts
        if age > max_age_seconds:
            stale.append({"session": session, "log": str(path), "age_seconds": round(age, 1)})
    return stale


def latest_watch_snapshot(org: str) -> dict[str, Any] | None:
    path = WATCHER_LOGS[org]
    if not path.exists():
        return None
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-500:]):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") != "snapshot" or "results" not in row:
            continue
        states: dict[str, int] = {}
        for result in row.get("results") or []:
            state = str(result.get("state") or "unknown")
            states[state] = states.get(state, 0) + 1
        return {
            "at": row.get("at"),
            "age_seconds": round(time.time() - (parse_time(row.get("at")) or time.time()), 1),
            "live_workers": int(row.get("live_workers") or 0),
            "active_or_pending_slots": int(row.get("active_or_pending_slots") or 0),
            "states": states,
        }
    return None


def desired_fleet_mode() -> tuple[str, dict[str, Any]]:
    snapshots = {org: latest_watch_snapshot(org) for org in WATCHER_LOGS}
    full = all(
        snapshot is not None and int(snapshot.get("live_workers") or 0) >= FULL_LIVE_WORKERS_PER_ORG
        for snapshot in snapshots.values()
    )
    return ("optimize" if full else "fill"), {
        "full_live_workers_per_org": FULL_LIVE_WORKERS_PER_ORG,
        "snapshots": snapshots,
    }


def process_fleet_modes() -> dict[str, str]:
    modes: dict[str, str] = {}
    for proc in pathlib.Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
        except OSError:
            continue
        if "salad_prl_watch.py" not in cmdline and "salad_prl_guard.py" not in cmdline:
            continue
        try:
            env_items = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        env: dict[str, str] = {}
        for item in env_items:
            if b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            decoded_key = key.decode(errors="ignore")
            if decoded_key in {"PRL_WATCH_NAME", "PRL_GUARD_ORGS", "PRL_FLEET_MODE"}:
                env[decoded_key] = value.decode(errors="ignore")
        name = env.get("PRL_WATCH_NAME") or ("guard" if env.get("PRL_GUARD_ORGS") else proc.name)
        modes[name] = env.get("PRL_FLEET_MODE", "")
    return modes


def current_fleet_mode() -> tuple[str | None, dict[str, str]]:
    modes = process_fleet_modes()
    values = {mode for mode in modes.values() if mode}
    if len(values) == 1:
        return next(iter(values)), modes
    return None, modes


def restart_stack(reason: str, *, fleet_mode: str | None = None, **fields: Any) -> bool:
    log("stack_restart_requested", reason=reason, **fields)
    env = {"PRL_FLEET_MODE": fleet_mode} if fleet_mode else None
    result = run(["bash", str(START_SCRIPT)], env=env)
    log(
        "stack_restart_finished",
        reason=reason,
        returncode=result.returncode,
        stdout_tail=result.stdout[-1000:],
        stderr_tail=result.stderr[-1000:],
    )
    return result.returncode == 0


def check_once(max_heartbeat_age_seconds: int) -> bool:
    for session in FORBIDDEN_SESSIONS:
        kill_session(session, "session_not_enabled")

    missing = sessions_missing()
    if missing:
        return restart_stack("missing_sessions", missing_sessions=missing)

    stale = stale_sessions(max_heartbeat_age_seconds)
    if stale:
        return restart_stack("stale_heartbeats", stale_sessions=stale)

    desired_mode, mode_evidence = desired_fleet_mode()
    current_mode, process_modes = current_fleet_mode()
    if current_mode != desired_mode:
        return restart_stack(
            "fleet_mode_transition",
            fleet_mode=desired_mode,
            current_mode=current_mode,
            process_modes=process_modes,
            mode_evidence=mode_evidence,
        )

    log(
        "supervisor_ok",
        sessions=list(REQUIRED_SESSIONS),
        max_heartbeat_age_seconds=max_heartbeat_age_seconds,
        fleet_mode=current_mode,
        mode_evidence=mode_evidence,
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--max-heartbeat-age-seconds", type=int, default=600)
    args = parser.parse_args()

    log(
        "supervisor_started",
        once=args.once,
        interval=args.interval,
        max_heartbeat_age_seconds=args.max_heartbeat_age_seconds,
    )
    while True:
        try:
            check_once(args.max_heartbeat_age_seconds)
        except Exception as exc:
            log("supervisor_check_failed", error=type(exc).__name__, detail=str(exc)[:200])
        if args.once:
            return 0
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
