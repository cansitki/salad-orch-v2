#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from config_loader import FleetConfig, load_config
from fleet_common import STATE_DIR, compact_json, env_bool, env_int, json_dumps, safe_public_payload, utc_now


DEFAULT_DB = pathlib.Path(__file__).resolve().parent.parent / "state" / "fleet_scheduler.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organizations (
  label TEXT PRIMARY KEY,
  slug TEXT NOT NULL,
  api_key_env TEXT NOT NULL,
  slot_prefix TEXT NOT NULL,
  slot_count INTEGER NOT NULL,
  enabled INTEGER NOT NULL,
  worker_prefix TEXT,
  worker_slot_prefix TEXT,
  pool_worker_prefix TEXT,
  display_prefix TEXT,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slots (
  org_label TEXT NOT NULL,
  slot_name TEXT NOT NULL,
  slot_index INTEGER NOT NULL,
  desired_profile_key TEXT,
  observed_profile_key TEXT,
  observed_status TEXT,
  observed_profile_since_utc TEXT,
  observed_status_since_utc TEXT,
  live_hashrate_th REAL DEFAULT 0,
  protected INTEGER DEFAULT 0,
  updated_at_utc TEXT NOT NULL,
  PRIMARY KEY (org_label, slot_name)
);

CREATE TABLE IF NOT EXISTS gpu_profiles (
  profile_key TEXT PRIMARY KEY,
  gpu_key TEXT NOT NULL,
  gpu_id TEXT NOT NULL,
  priority TEXT NOT NULL,
  label TEXT NOT NULL,
  memory_mb INTEGER NOT NULL,
  expected_th REAL NOT NULL,
  static_hourly_usd REAL NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_prices (
  org_label TEXT NOT NULL,
  profile_key TEXT NOT NULL,
  hourly_usd REAL NOT NULL,
  source TEXT NOT NULL,
  sampled_at_utc TEXT NOT NULL,
  PRIMARY KEY (org_label, profile_key)
);

CREATE TABLE IF NOT EXISTS profile_availability (
  org_label TEXT NOT NULL,
  profile_key TEXT NOT NULL,
  available_count INTEGER,
  ok INTEGER NOT NULL,
  error TEXT,
  checked_at_utc TEXT NOT NULL,
  PRIMARY KEY (org_label, profile_key)
);
CREATE INDEX IF NOT EXISTS idx_profile_availability_checked ON profile_availability(checked_at_utc);

CREATE TABLE IF NOT EXISTS search_cooldowns (
  org_label TEXT NOT NULL,
  slot_name TEXT NOT NULL,
  profile_key TEXT NOT NULL,
  no_gpu_since_utc TEXT,
  sleep_until_utc TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  updated_at_utc TEXT NOT NULL,
  PRIMARY KEY (org_label, slot_name, profile_key)
);
CREATE INDEX IF NOT EXISTS idx_search_cooldowns_sleep ON search_cooldowns(sleep_until_utc);

CREATE TABLE IF NOT EXISTS price_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sampled_at_utc TEXT NOT NULL,
  pearl_price_usd REAL,
  safetrade_last_usd REAL,
  safetrade_buy_usd REAL,
  safetrade_sell_usd REAL,
  selected_price_usd REAL,
  source_spread_usd REAL,
  gross_prl_per_th_day REAL,
  pool_fee_rate REAL,
  configured_pearl_fee_rate REAL,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_history_sampled ON price_history(sampled_at_utc);

CREATE TABLE IF NOT EXISTS risk_modes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at_utc TEXT NOT NULL,
  mode TEXT NOT NULL,
  decision_price_usd REAL NOT NULL,
  trailing_min_15m REAL,
  trailing_min_30m REAL,
  trailing_min_1h REAL,
  trailing_avg_30m REAL,
  trailing_avg_1h REAL,
  pearl_fee_rate REAL NOT NULL,
  reason TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_risk_modes_at ON risk_modes(at_utc);

CREATE TABLE IF NOT EXISTS slot_targets (
  org_label TEXT NOT NULL,
  slot_name TEXT NOT NULL,
  profile_key TEXT NOT NULL,
  mode TEXT NOT NULL,
  decision_price_usd REAL NOT NULL,
  expected_profit_day REAL NOT NULL,
  protected INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL,
  assigned_at_utc TEXT NOT NULL,
  expires_at_utc TEXT,
  PRIMARY KEY (org_label, slot_name)
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at_utc TEXT NOT NULL,
  org_label TEXT NOT NULL,
  slot_name TEXT NOT NULL,
  action TEXT NOT NULL,
  profile_key TEXT,
  ok INTEGER NOT NULL,
  duration_ms INTEGER,
  error TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_attempts_slot ON attempts(org_label, slot_name, at_utc);

CREATE TABLE IF NOT EXISTS workers (
  worker_name TEXT PRIMARY KEY,
  org_label TEXT,
  slot_name TEXT,
  instance_id TEXT,
  gpu_key TEXT,
  reported_hashrate_th REAL,
  stale INTEGER,
  last_stats_at TEXT,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profit_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at_utc TEXT NOT NULL,
  scope TEXT NOT NULL,
  org_label TEXT,
  slot_name TEXT,
  profile_key TEXT,
  decision_price_usd REAL NOT NULL,
  live_price_usd REAL,
  th REAL,
  cost_day REAL,
  revenue_day REAL,
  profit_day REAL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_profit_snapshots_at ON profit_snapshots(at_utc);

CREATE TABLE IF NOT EXISTS profile_scores (
  profile_key TEXT NOT NULL,
  mode TEXT NOT NULL,
  decision_price_usd REAL NOT NULL,
  expected_profit_day REAL NOT NULL,
  score REAL NOT NULL,
  risk_tier TEXT NOT NULL,
  reason_json TEXT NOT NULL DEFAULT '{}',
  scored_at_utc TEXT NOT NULL,
  PRIMARY KEY (profile_key, mode)
);

CREATE TABLE IF NOT EXISTS heartbeats (
  process_name TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  at_utc TEXT NOT NULL,
  stale_after_seconds INTEGER NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at_utc TEXT NOT NULL,
  source TEXT NOT NULL,
  level TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at_utc);

CREATE TABLE IF NOT EXISTS runtime_failures (
  component TEXT PRIMARY KEY,
  at_utc TEXT NOT NULL,
  severity TEXT NOT NULL,
  error_type TEXT NOT NULL,
  message TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS guard_issues (
  org_label TEXT NOT NULL,
  slot_name TEXT NOT NULL,
  issue_type TEXT NOT NULL,
  first_seen_utc TEXT NOT NULL,
  last_seen_utc TEXT NOT NULL,
  action_count INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (org_label, slot_name, issue_type)
);

CREATE TABLE IF NOT EXISTS slot_spike_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at_utc TEXT NOT NULL,
  org_label TEXT NOT NULL,
  slot_name TEXT NOT NULL,
  issue_type TEXT NOT NULL,
  profile_key TEXT,
  gpu_key TEXT,
  priority TEXT,
  profit_day REAL,
  market_profit_day REAL,
  cost_day REAL,
  th REAL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_slot_spike_events_at ON slot_spike_events(at_utc);
CREATE INDEX IF NOT EXISTS idx_slot_spike_events_profile ON slot_spike_events(profile_key, at_utc);
CREATE INDEX IF NOT EXISTS idx_slot_spike_events_slot ON slot_spike_events(org_label, slot_name, at_utc);

CREATE TABLE IF NOT EXISTS api_rate_limits (
  api_key_env TEXT PRIMARY KEY,
  window_started_utc TEXT NOT NULL,
  request_count INTEGER NOT NULL,
  max_requests_per_minute INTEGER NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rollout_checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at_utc TEXT NOT NULL,
  name TEXT NOT NULL,
  stage TEXT NOT NULL,
  target_count INTEGER NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_rollout_checkpoints_created ON rollout_checkpoints(created_at_utc);

CREATE TABLE IF NOT EXISTS fleet_active_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at_utc TEXT NOT NULL,
  assigned_targets INTEGER NOT NULL,
  target_slots INTEGER NOT NULL,
  live_hashing_gpus INTEGER NOT NULL,
  live_th REAL NOT NULL,
  cost_day REAL,
  profit_day_064 REAL,
  market_profit_day REAL,
  status_counts_json TEXT NOT NULL DEFAULT '{}',
  org_summary_json TEXT NOT NULL DEFAULT '{}',
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_fleet_active_snapshots_at ON fleet_active_snapshots(at_utc);

CREATE TABLE IF NOT EXISTS fleet_org_active_snapshots (
  snapshot_id INTEGER NOT NULL,
  org_label TEXT NOT NULL,
  active_slots INTEGER NOT NULL,
  running_slots INTEGER NOT NULL,
  creating_slots INTEGER NOT NULL,
  allocating_slots INTEGER NOT NULL,
  live_hashing_gpus INTEGER NOT NULL,
  live_th REAL NOT NULL,
  cost_day REAL,
  profit_day REAL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (snapshot_id, org_label)
);
CREATE INDEX IF NOT EXISTS idx_fleet_org_active_snapshots_org ON fleet_org_active_snapshots(org_label, snapshot_id);

CREATE TABLE IF NOT EXISTS fleet_slot_active_snapshots (
  snapshot_id INTEGER NOT NULL,
  org_label TEXT NOT NULL,
  slot_name TEXT NOT NULL,
  slot_index INTEGER NOT NULL,
  observed_status TEXT,
  desired_profile_key TEXT,
  observed_profile_key TEXT,
  target_profile_key TEXT,
  target_mode TEXT,
  target_reason TEXT,
  protected INTEGER NOT NULL DEFAULT 0,
  live_hashrate_th REAL NOT NULL DEFAULT 0,
  billable INTEGER NOT NULL DEFAULT 0,
  cost_day REAL,
  profit_day REAL,
  updated_at_utc TEXT,
  observed_profile_since_utc TEXT,
  observed_status_since_utc TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (snapshot_id, org_label, slot_name)
);
CREATE INDEX IF NOT EXISTS idx_fleet_slot_active_snapshots_org_slot
  ON fleet_slot_active_snapshots(org_label, slot_name, snapshot_id);

CREATE TABLE IF NOT EXISTS fleet_org_balance_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at_utc TEXT NOT NULL,
  org_label TEXT NOT NULL,
  balance_usd REAL,
  balance_source TEXT NOT NULL,
  balance_ok INTEGER NOT NULL,
  previous_balance_usd REAL,
  previous_at_utc TEXT,
  elapsed_hours REAL,
  cost_day REAL,
  expected_cost_usd REAL,
  balance_delta_usd REAL,
  variance_usd REAL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_fleet_org_balance_audits_org_at ON fleet_org_balance_audits(org_label, at_utc);
"""


def db_path(path: str | pathlib.Path | None = None) -> pathlib.Path:
    if path:
        return pathlib.Path(path)
    return pathlib.Path(DEFAULT_DB)


def connect(path: str | pathlib.Path | None = None) -> sqlite3.Connection:
    target = db_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_column(conn, "slots", "observed_profile_since_utc", "TEXT")
    _ensure_column(conn, "slots", "observed_status_since_utc", "TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at_utc) VALUES(?, ?)",
        (1, utc_now()),
    )
    conn.commit()


def record_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    source: str,
    message: str,
    level: str = "info",
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events(at_utc, source, level, event_type, message, payload_json)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            source,
            level,
            event_type,
            message,
            compact_json(safe_public_payload(payload or {})),
        ),
    )


def record_failure(
    conn: sqlite3.Connection,
    component: str,
    *,
    severity: str,
    error_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO runtime_failures(component, at_utc, severity, error_type, message, payload_json)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET
          at_utc=excluded.at_utc,
          severity=excluded.severity,
          error_type=excluded.error_type,
          message=excluded.message,
          payload_json=excluded.payload_json
        """,
        (
            component,
            utc_now(),
            severity,
            error_type,
            message,
            compact_json(safe_public_payload(payload or {})),
        ),
    )


def clear_failure(conn: sqlite3.Connection, component: str) -> None:
    conn.execute("DELETE FROM runtime_failures WHERE component = ?", (component,))


def write_heartbeat(
    conn: sqlite3.Connection,
    process_name: str,
    *,
    status: str = "ok",
    stale_after_seconds: int = 120,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO heartbeats(process_name, status, at_utc, stale_after_seconds, payload_json)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(process_name) DO UPDATE SET
          status=excluded.status,
          at_utc=excluded.at_utc,
          stale_after_seconds=excluded.stale_after_seconds,
          payload_json=excluded.payload_json
        """,
        (process_name, status, utc_now(), stale_after_seconds, compact_json(safe_public_payload(payload or {}))),
    )


def sync_config(conn: sqlite3.Connection, config: FleetConfig) -> None:
    now = utc_now()
    for org in config.organizations:
        conn.execute(
            """
            INSERT INTO organizations(
              label, slug, api_key_env, slot_prefix, slot_count, enabled,
              worker_prefix, worker_slot_prefix, pool_worker_prefix, display_prefix, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(label) DO UPDATE SET
              slug=excluded.slug,
              api_key_env=excluded.api_key_env,
              slot_prefix=excluded.slot_prefix,
              slot_count=excluded.slot_count,
              enabled=excluded.enabled,
              worker_prefix=excluded.worker_prefix,
              worker_slot_prefix=excluded.worker_slot_prefix,
              pool_worker_prefix=excluded.pool_worker_prefix,
              display_prefix=excluded.display_prefix,
              updated_at_utc=excluded.updated_at_utc
            """,
            (
                org.label,
                org.slug,
                org.api_key_env,
                org.slot_prefix,
                org.slots,
                1 if org.enabled else 0,
                org.worker_prefix,
                org.worker_slot_prefix,
                org.pool_worker_prefix,
                org.display_prefix,
                now,
            ),
        )
        for slot_index, slot_name in enumerate(org.slot_names(), start=1):
            conn.execute(
                """
                INSERT INTO slots(org_label, slot_name, slot_index, updated_at_utc)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(org_label, slot_name) DO UPDATE SET
                  slot_index=excluded.slot_index,
                  updated_at_utc=excluded.updated_at_utc
                """,
                (org.label, slot_name, slot_index, now),
            )
        desired_slots = org.slot_names()
        placeholders = ",".join("?" for _slot in desired_slots)
        conn.execute(
            f"DELETE FROM slot_targets WHERE org_label = ? AND slot_name NOT IN ({placeholders})",
            (org.label, *desired_slots),
        )
        conn.execute(
            f"DELETE FROM slots WHERE org_label = ? AND slot_name NOT IN ({placeholders})",
            (org.label, *desired_slots),
        )
    record_event(
        conn,
        "config_synced",
        source="state_db",
        message="fleet configuration synced",
        payload={"orgs": len(config.organizations), "target_slots": config.target_slot_count()},
    )


def upsert_gpu_profiles(conn: sqlite3.Connection, profiles: list[Any]) -> None:
    now = utc_now()
    for profile in profiles:
        data = asdict(profile) if hasattr(profile, "__dataclass_fields__") else dict(profile)
        conn.execute(
            """
            INSERT INTO gpu_profiles(
              profile_key, gpu_key, gpu_id, priority, label, memory_mb,
              expected_th, static_hourly_usd, enabled, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_key) DO UPDATE SET
              gpu_key=excluded.gpu_key,
              gpu_id=excluded.gpu_id,
              priority=excluded.priority,
              label=excluded.label,
              memory_mb=excluded.memory_mb,
              expected_th=excluded.expected_th,
              static_hourly_usd=excluded.static_hourly_usd,
              enabled=excluded.enabled,
              updated_at_utc=excluded.updated_at_utc
            """,
            (
                data["profile_key"],
                data["gpu_key"],
                data["gpu_id"],
                data["priority"],
                data["label"],
                int(data["memory_mb"]),
                float(data["expected_th"]),
                float(data["static_hourly_usd"]),
                1 if data.get("enabled", True) else 0,
                now,
            ),
        )


def upsert_profile_availability(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO profile_availability(
          org_label, profile_key, available_count, ok, error, checked_at_utc
        )
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(org_label, profile_key) DO UPDATE SET
          available_count=excluded.available_count,
          ok=excluded.ok,
          error=excluded.error,
          checked_at_utc=excluded.checked_at_utc
        """,
        (
            row["org_label"],
            row["profile_key"],
            row.get("available_count"),
            1 if row.get("ok") else 0,
            row.get("error"),
            row.get("checked_at_utc") or utc_now(),
        ),
    )


def latest_profile_availability(conn: sqlite3.Connection, max_age_seconds: int | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    if max_age_seconds is None:
        max_age_seconds = env_int(
            "PRL_PROFILE_AVAILABILITY_MAX_AGE_SECONDS",
            env_int("PRL_AVAILABILITY_STALE_AFTER_SECONDS", 1800),
        )
    rows = conn.execute(
        """
        SELECT org_label, profile_key, available_count, ok, error, checked_at_utc
        FROM profile_availability
        WHERE julianday(checked_at_utc) >= julianday('now', ?)
        """,
        (f"-{max_age_seconds} seconds",),
    ).fetchall()
    by_org: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        org = str(row["org_label"])
        by_org.setdefault(org, {})[str(row["profile_key"])] = dict(row)
    return by_org


def record_search_state(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    existing = conn.execute(
        """
        SELECT reason, sleep_until_utc
        FROM search_cooldowns
        WHERE org_label = ? AND slot_name = ? AND profile_key = ?
        """,
        (row["org_label"], row["slot_name"], row["profile_key"]),
    ).fetchone()
    if existing and str(existing["reason"] or "") == "unstable_recent_spikes":
        try:
            existing_until = _parse_utc(str(existing["sleep_until_utc"]))
        except (TypeError, ValueError):
            existing_until = None
        if (
            existing_until is not None
            and existing_until > datetime.now(UTC)
            and str(row.get("reason") or "") != "unstable_recent_spikes"
        ):
            return
    conn.execute(
        """
        INSERT INTO search_cooldowns(
          org_label, slot_name, profile_key, no_gpu_since_utc,
          sleep_until_utc, attempts, reason, updated_at_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(org_label, slot_name, profile_key) DO UPDATE SET
          no_gpu_since_utc=excluded.no_gpu_since_utc,
          sleep_until_utc=excluded.sleep_until_utc,
          attempts=excluded.attempts,
          reason=excluded.reason,
          updated_at_utc=excluded.updated_at_utc
        """,
        (
            row["org_label"],
            row["slot_name"],
            row["profile_key"],
            row.get("no_gpu_since_utc"),
            row.get("sleep_until_utc"),
            int(row.get("attempts") or 0),
            row.get("reason"),
            row.get("updated_at_utc") or utc_now(),
        ),
    )


def active_org_cooldown(conn: sqlite3.Connection, org_label: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT org_label, slot_name, profile_key, no_gpu_since_utc,
               sleep_until_utc, attempts, reason, updated_at_utc
        FROM search_cooldowns
        WHERE org_label = ?
          AND slot_name = '*'
          AND profile_key = '*'
          AND sleep_until_utc IS NOT NULL
          AND julianday(sleep_until_utc) > julianday('now')
        ORDER BY sleep_until_utc DESC
        LIMIT 1
        """,
        (org_label,),
    ).fetchone()
    return dict(row) if row else None


def record_guard_issue(conn: sqlite3.Connection, row: dict[str, Any]) -> sqlite3.Row:
    now = row.get("last_seen_utc") or utc_now()
    payload = compact_json(safe_public_payload(row.get("payload", {})))
    conn.execute(
        """
        INSERT INTO guard_issues(
          org_label, slot_name, issue_type, first_seen_utc, last_seen_utc,
          action_count, payload_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(org_label, slot_name, issue_type) DO UPDATE SET
          last_seen_utc=excluded.last_seen_utc,
          payload_json=excluded.payload_json
        """,
        (
            row["org_label"],
            row["slot_name"],
            row["issue_type"],
            row.get("first_seen_utc") or now,
            now,
            int(row.get("action_count") or 0),
            payload,
        ),
    )
    return conn.execute(
        """
        SELECT *
        FROM guard_issues
        WHERE org_label = ? AND slot_name = ? AND issue_type = ?
        """,
        (row["org_label"], row["slot_name"], row["issue_type"]),
    ).fetchone()


def increment_guard_issue_action(conn: sqlite3.Connection, org_label: str, slot_name: str, issue_type: str) -> None:
    conn.execute(
        """
        UPDATE guard_issues
        SET action_count = action_count + 1,
            last_seen_utc = ?
        WHERE org_label = ? AND slot_name = ? AND issue_type = ?
        """,
        (utc_now(), org_label, slot_name, issue_type),
    )


def clear_guard_issues(conn: sqlite3.Connection, active_keys: set[tuple[str, str, str]]) -> None:
    rows = conn.execute("SELECT org_label, slot_name, issue_type FROM guard_issues").fetchall()
    for row in rows:
        key = (str(row["org_label"]), str(row["slot_name"]), str(row["issue_type"]))
        if key not in active_keys:
            conn.execute(
                "DELETE FROM guard_issues WHERE org_label = ? AND slot_name = ? AND issue_type = ?",
                key,
            )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def record_slot_spike_event(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO slot_spike_events(
          at_utc, org_label, slot_name, issue_type, profile_key, gpu_key,
          priority, profit_day, market_profit_day, cost_day, th, payload_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.get("at_utc") or utc_now(),
            row["org_label"],
            row["slot_name"],
            row["issue_type"],
            row.get("profile_key"),
            row.get("gpu_key"),
            row.get("priority"),
            _float_or_none(row.get("profit_day")),
            _float_or_none(row.get("market_profit_day")),
            _float_or_none(row.get("cost_day")),
            _float_or_none(row.get("th")),
            compact_json(safe_public_payload(row.get("payload", {}))),
        ),
    )


def recent_spike_summary(
    conn: sqlite3.Connection,
    *,
    windows_minutes: tuple[int, ...] = (30, 60),
    limit: int = 20,
    now_utc: str | None = None,
) -> dict[str, Any]:
    windows = tuple(sorted({int(window) for window in windows_minutes if int(window) > 0}))
    if not windows:
        windows = (30, 60)
    now = _parse_utc(now_utc or utc_now())
    max_window = max(windows)
    profile_30_threshold = env_int("PRL_SPIKE_PROFILE_30M_THRESHOLD", 3)
    profile_60_threshold = env_int("PRL_SPIKE_PROFILE_60M_THRESHOLD", 5)
    affected_slots_threshold = env_int("PRL_SPIKE_PROFILE_AFFECTED_SLOTS_60M_THRESHOLD", 3)
    bucket_seconds = max(60, env_int("PRL_SPIKE_SUMMARY_BUCKET_SECONDS", 300))
    rows = conn.execute(
        """
        SELECT *
        FROM slot_spike_events
        WHERE julianday(at_utc) >= julianday(?, ?)
        ORDER BY at_utc ASC, id ASC
        """,
        (now.isoformat(timespec="seconds"), f"-{max_window} minutes"),
    ).fetchall()

    def profile_identity(row: sqlite3.Row) -> str:
        if row["profile_key"]:
            return str(row["profile_key"])
        gpu = str(row["gpu_key"] or "unknown").lower()
        priority = str(row["priority"] or "unknown").lower()
        return f"{gpu}:{priority}"

    profiles: dict[str, dict[str, Any]] = {}
    slots: dict[tuple[str, str, str], dict[str, Any]] = {}
    seen_profile_buckets: set[tuple[str, str, str, str, int]] = set()
    seen_slot_buckets: set[tuple[str, str, str, str, str, int]] = set()

    def apply_common(item: dict[str, Any], row: sqlite3.Row, suffix: str) -> None:
        item[f"spikes_{suffix}"] = int(item.get(f"spikes_{suffix}") or 0) + 1
        issue_counts = item.setdefault(f"issue_counts_{suffix}", {})
        issue = str(row["issue_type"])
        issue_counts[issue] = int(issue_counts.get(issue) or 0) + 1
        profit = _float_or_none(row["profit_day"])
        if profit is not None:
            current = item.get(f"worst_profit_day_{suffix}")
            item[f"worst_profit_day_{suffix}"] = profit if current is None else min(float(current), profit)
            total_key = f"profit_total_{suffix}"
            sample_key = f"profit_samples_{suffix}"
            item[total_key] = float(item.get(total_key) or 0.0) + profit
            item[sample_key] = int(item.get(sample_key) or 0) + 1
        item["last_seen_utc"] = str(row["at_utc"])

    for row in rows:
        try:
            at = _parse_utc(str(row["at_utc"]))
        except ValueError:
            continue
        profile_key = profile_identity(row)
        profile = profiles.setdefault(
            profile_key,
            {
                "profile_key": profile_key,
                "gpu_key": row["gpu_key"],
                "priority": row["priority"],
                "affected_slots": {window: set() for window in windows},
                "sample_slots": [],
                "first_seen_utc": str(row["at_utc"]),
                "last_seen_utc": str(row["at_utc"]),
            },
        )
        slot_key_text = f"{row['org_label']}/{row['slot_name']}"
        if slot_key_text not in profile["sample_slots"] and len(profile["sample_slots"]) < 8:
            profile["sample_slots"].append(slot_key_text)
        slot = slots.setdefault(
            (str(row["org_label"]), str(row["slot_name"]), profile_key),
            {
                "org_label": row["org_label"],
                "slot_name": row["slot_name"],
                "profile_key": profile_key,
                "gpu_key": row["gpu_key"],
                "priority": row["priority"],
                "first_seen_utc": str(row["at_utc"]),
                "last_seen_utc": str(row["at_utc"]),
            },
        )
        for window in windows:
            if (now - at).total_seconds() > window * 60:
                continue
            suffix = f"{window}m"
            profile["affected_slots"][window].add(slot_key_text)
            bucket = int(at.timestamp() // bucket_seconds)
            issue = str(row["issue_type"])
            profile_bucket_key = (profile_key, slot_key_text, issue, suffix, bucket)
            slot_bucket_key = (str(row["org_label"]), str(row["slot_name"]), profile_key, issue, suffix, bucket)
            if profile_bucket_key not in seen_profile_buckets:
                seen_profile_buckets.add(profile_bucket_key)
                apply_common(profile, row, suffix)
            if slot_bucket_key not in seen_slot_buckets:
                seen_slot_buckets.add(slot_bucket_key)
                apply_common(slot, row, suffix)

    def finalize(item: dict[str, Any]) -> dict[str, Any]:
        out = dict(item)
        profile_parts = str(out.get("profile_key") or "").split(":")
        if len(profile_parts) >= 2:
            out["gpu_key"] = out.get("gpu_key") or profile_parts[0]
            out["priority"] = profile_parts[1]
        affected = out.pop("affected_slots", None)
        if affected:
            for window, values in affected.items():
                out[f"affected_slots_{window}m"] = len(values)
        for key in list(out):
            if key.startswith("profit_total_"):
                suffix = key.removeprefix("profit_total_")
                samples = int(out.get(f"profit_samples_{suffix}") or 0)
                if samples:
                    out[f"avg_profit_day_{suffix}"] = round(float(out[key]) / samples, 6)
                del out[key]
                out.pop(f"profit_samples_{suffix}", None)
        spikes_30 = int(out.get("spikes_30m") or 0)
        spikes_60 = int(out.get("spikes_60m") or 0)
        affected_60 = int(out.get("affected_slots_60m") or 0)
        out["unstable"] = (
            spikes_30 >= profile_30_threshold
            or spikes_60 >= profile_60_threshold
            or affected_60 >= affected_slots_threshold
        )
        out["instability_score"] = spikes_30 * 2 + spikes_60 + affected_60 * 2
        return out

    profile_rows = [finalize(item) for item in profiles.values()]
    slot_rows = [finalize(item) for item in slots.values()]
    profile_rows.sort(
        key=lambda item: (
            bool(item.get("unstable")),
            int(item.get("instability_score") or 0),
            int(item.get("spikes_60m") or 0),
            int(item.get("spikes_30m") or 0),
        ),
        reverse=True,
    )
    slot_rows.sort(
        key=lambda item: (
            int(item.get("spikes_60m") or 0),
            int(item.get("spikes_30m") or 0),
            float(item.get("worst_profit_day_60m") or 0),
        ),
        reverse=True,
    )
    if limit > 0:
        profile_rows = profile_rows[:limit]
        slot_rows = slot_rows[:limit]
    return {
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "windows_minutes": list(windows),
        "thresholds": {
            "profile_30m": profile_30_threshold,
            "profile_60m": profile_60_threshold,
            "affected_slots_60m": affected_slots_threshold,
            "bucket_seconds": bucket_seconds,
        },
        "event_count": len(rows),
        "profiles": profile_rows,
        "slots": slot_rows,
    }


def reserve_api_request(
    conn: sqlite3.Connection,
    api_key_env: str,
    *,
    max_requests_per_minute: int,
    now_utc: str | None = None,
) -> float:
    if max_requests_per_minute <= 0:
        return 0.0
    now = now_utc or utc_now()
    now_dt = _parse_utc(now)
    row = conn.execute(
        "SELECT * FROM api_rate_limits WHERE api_key_env = ?",
        (api_key_env,),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO api_rate_limits(
              api_key_env, window_started_utc, request_count,
              max_requests_per_minute, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?)
            """,
            (api_key_env, now, 1, max_requests_per_minute, now),
        )
        return 0.0
    window_started = _parse_utc(str(row["window_started_utc"]))
    elapsed = max(0.0, (now_dt - window_started).total_seconds())
    if elapsed >= 60.0:
        conn.execute(
            """
            UPDATE api_rate_limits
            SET window_started_utc = ?,
                request_count = 1,
                max_requests_per_minute = ?,
                updated_at_utc = ?
            WHERE api_key_env = ?
            """,
            (now, max_requests_per_minute, now, api_key_env),
        )
        return 0.0
    request_count = int(row["request_count"] or 0)
    if request_count < max_requests_per_minute:
        conn.execute(
            """
            UPDATE api_rate_limits
            SET request_count = request_count + 1,
                max_requests_per_minute = ?,
                updated_at_utc = ?
            WHERE api_key_env = ?
            """,
            (max_requests_per_minute, now, api_key_env),
        )
        return 0.0
    return max(0.0, 60.0 - elapsed)


def active_search_cooldowns(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    ignore_availability_zero = env_bool("PRL_IGNORE_AVAILABILITY_ZERO_COOLDOWN", False)
    rows = conn.execute(
        """
        SELECT org_label, slot_name, profile_key, reason
        FROM search_cooldowns
        WHERE sleep_until_utc IS NOT NULL
          AND julianday(sleep_until_utc) > julianday('now')
        """
    ).fetchall()
    cooldowns = set()
    for row in rows:
        if ignore_availability_zero and str(row["reason"] or "") == "availability_zero":
            continue
        cooldowns.add((str(row["org_label"]), str(row["slot_name"]), str(row["profile_key"])))
    return cooldowns


def update_slot_observation(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    now = row.get("updated_at_utc") or utc_now()
    existing = conn.execute(
        """
        SELECT observed_profile_key, observed_status, observed_profile_since_utc,
               observed_status_since_utc, live_hashrate_th, updated_at_utc
        FROM slots
        WHERE org_label = ? AND slot_name = ?
        """,
        (row["org_label"], row["slot_name"]),
    ).fetchone()
    observed_profile = row.get("observed_profile_key")
    observed_status = row.get("observed_status")
    reset_observed_age = bool(row.get("reset_observed_age"))
    if existing is None:
        profile_since = now if observed_profile else None
        status_since = now if observed_status else None
    elif reset_observed_age:
        profile_since = now if observed_profile else None
        status_since = now if observed_status else None
    else:
        profile_since = (
            existing["observed_profile_since_utc"] or existing["updated_at_utc"] or now
            if existing["observed_profile_key"] == observed_profile
            else (now if observed_profile else None)
        )
        status_since = (
            existing["observed_status_since_utc"] or existing["updated_at_utc"] or now
            if existing["observed_status"] == observed_status
            else (now if observed_status else None)
        )
    if "live_hashrate_th" in row:
        live_hashrate_th = float(row.get("live_hashrate_th") or 0)
    elif existing is not None:
        live_hashrate_th = float(existing["live_hashrate_th"] or 0)
    else:
        live_hashrate_th = 0.0
    conn.execute(
        """
        UPDATE slots
        SET observed_profile_key = ?,
            observed_status = ?,
            observed_profile_since_utc = ?,
            observed_status_since_utc = ?,
            live_hashrate_th = ?,
            protected = ?,
            updated_at_utc = ?
        WHERE org_label = ? AND slot_name = ?
        """,
        (
            observed_profile,
            observed_status,
            profile_since,
            status_since,
            live_hashrate_th,
            1 if row.get("protected") else 0,
            now,
            row["org_label"],
            row["slot_name"],
        ),
    )


def reset_slot_hashrates(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE slots SET live_hashrate_th = 0")


def sync_worker_rows(conn: sqlite3.Connection, workers: list[dict[str, Any]]) -> None:
    now = utc_now()
    conn.execute("UPDATE workers SET stale = 1, updated_at_utc = ?", (now,))
    for worker in workers:
        conn.execute(
            """
            INSERT INTO workers(
              worker_name, org_label, slot_name, instance_id, gpu_key,
              reported_hashrate_th, stale, last_stats_at, updated_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_name) DO UPDATE SET
              org_label = excluded.org_label,
              slot_name = excluded.slot_name,
              instance_id = excluded.instance_id,
              gpu_key = excluded.gpu_key,
              reported_hashrate_th = excluded.reported_hashrate_th,
              stale = excluded.stale,
              last_stats_at = excluded.last_stats_at,
              updated_at_utc = excluded.updated_at_utc
            """,
            (
                worker["worker_name"],
                worker.get("org_label"),
                worker.get("slot_name"),
                worker.get("instance_id"),
                worker.get("gpu_key"),
                float(worker.get("reported_hashrate_th") or 0),
                1 if worker.get("stale") else 0,
                worker.get("last_stats_at"),
                worker.get("updated_at_utc") or now,
            ),
        )


def insert_price_sample(conn: sqlite3.Connection, sample: dict[str, Any]) -> int:
    cursor = conn.execute(
        """
        INSERT INTO price_history(
          sampled_at_utc, pearl_price_usd, safetrade_last_usd, safetrade_buy_usd,
          safetrade_sell_usd, selected_price_usd, source_spread_usd,
          gross_prl_per_th_day, pool_fee_rate, configured_pearl_fee_rate, error
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sample.get("sampled_at_utc") or utc_now(),
            sample.get("pearl_price_usd"),
            sample.get("safetrade_last_usd"),
            sample.get("safetrade_buy_usd"),
            sample.get("safetrade_sell_usd"),
            sample.get("selected_price_usd"),
            sample.get("source_spread_usd"),
            sample.get("gross_prl_per_th_day"),
            sample.get("pool_fee_rate"),
            sample.get("configured_pearl_fee_rate"),
            sample.get("error"),
        ),
    )
    return int(cursor.lastrowid)


def latest_price_sample(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM price_history ORDER BY sampled_at_utc DESC, id DESC LIMIT 1").fetchone()


def price_window(conn: sqlite3.Connection, minutes: int) -> dict[str, float | None]:
    rows = conn.execute(
        """
        SELECT selected_price_usd, sampled_at_utc
        FROM price_history
        WHERE selected_price_usd IS NOT NULL
          AND julianday(sampled_at_utc) >= julianday('now', ?)
        """,
        (f"-{minutes} minutes",),
    ).fetchall()
    values = [float(row["selected_price_usd"]) for row in rows if row["selected_price_usd"] is not None]
    timestamps: list[float] = []
    for row in rows:
        try:
            timestamps.append(datetime.fromisoformat(str(row["sampled_at_utc"]).replace("Z", "+00:00")).timestamp())
        except ValueError:
            continue
    if not values:
        return {"min": None, "avg": None, "count": 0.0, "span_seconds": 0.0}
    span_seconds = max(timestamps) - min(timestamps) if len(timestamps) >= 2 else 0.0
    return {"min": min(values), "avg": sum(values) / len(values), "count": float(len(values)), "span_seconds": span_seconds}


def set_risk_mode(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO risk_modes(
          at_utc, mode, decision_price_usd, trailing_min_15m, trailing_min_30m,
          trailing_min_1h, trailing_avg_30m, trailing_avg_1h, pearl_fee_rate,
          reason, payload_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.get("at_utc") or utc_now(),
            payload["mode"],
            float(payload["decision_price_usd"]),
            payload.get("trailing_min_15m"),
            payload.get("trailing_min_30m"),
            payload.get("trailing_min_1h"),
            payload.get("trailing_avg_30m"),
            payload.get("trailing_avg_1h"),
            float(payload["pearl_fee_rate"]),
            payload.get("reason", ""),
            compact_json(safe_public_payload(payload)),
        ),
    )


def latest_risk_mode(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM risk_modes ORDER BY at_utc DESC, id DESC LIMIT 1").fetchone()


def upsert_profile_score(conn: sqlite3.Connection, score: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO profile_scores(
          profile_key, mode, decision_price_usd, expected_profit_day,
          score, risk_tier, reason_json, scored_at_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile_key, mode) DO UPDATE SET
          decision_price_usd=excluded.decision_price_usd,
          expected_profit_day=excluded.expected_profit_day,
          score=excluded.score,
          risk_tier=excluded.risk_tier,
          reason_json=excluded.reason_json,
          scored_at_utc=excluded.scored_at_utc
        """,
        (
            score["profile_key"],
            score["mode"],
            float(score["decision_price_usd"]),
            float(score["expected_profit_day"]),
            float(score["score"]),
            score["risk_tier"],
            compact_json(safe_public_payload(score.get("reason", {}))),
            score.get("scored_at_utc") or utc_now(),
        ),
    )


def set_slot_target(conn: sqlite3.Connection, target: dict[str, Any]) -> None:
    now = target.get("assigned_at_utc") or utc_now()
    conn.execute(
        """
        INSERT INTO slot_targets(
          org_label, slot_name, profile_key, mode, decision_price_usd,
          expected_profit_day, protected, reason, assigned_at_utc, expires_at_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(org_label, slot_name) DO UPDATE SET
          profile_key=excluded.profile_key,
          mode=excluded.mode,
          decision_price_usd=excluded.decision_price_usd,
          expected_profit_day=excluded.expected_profit_day,
          protected=excluded.protected,
          reason=excluded.reason,
          assigned_at_utc=excluded.assigned_at_utc,
          expires_at_utc=excluded.expires_at_utc
        """,
        (
            target["org_label"],
            target["slot_name"],
            target["profile_key"],
            target["mode"],
            float(target["decision_price_usd"]),
            float(target["expected_profit_day"]),
            1 if target.get("protected") else 0,
            target.get("reason", ""),
            now,
            target.get("expires_at_utc"),
        ),
    )
    conn.execute(
        """
        UPDATE slots
        SET desired_profile_key = ?, updated_at_utc = ?
        WHERE org_label = ? AND slot_name = ?
        """,
        (target["profile_key"], now, target["org_label"], target["slot_name"]),
    )


def current_slot_targets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT org_label, slot_name, profile_key, mode, decision_price_usd,
               expected_profit_day, protected, reason, assigned_at_utc,
               expires_at_utc
        FROM slot_targets
        ORDER BY org_label, slot_name
        """
    ).fetchall()
    return [dict(row) for row in rows]


def create_rollout_checkpoint(
    conn: sqlite3.Connection,
    *,
    name: str,
    stage: str,
    payload: dict[str, Any] | None = None,
) -> sqlite3.Row:
    targets = current_slot_targets(conn)
    latest_risk = latest_risk_mode(conn)
    checkpoint_payload = {
        "slot_targets": targets,
        "latest_risk_mode": dict(latest_risk) if latest_risk else None,
        **(payload or {}),
    }
    cursor = conn.execute(
        """
        INSERT INTO rollout_checkpoints(
          created_at_utc, name, stage, target_count, payload_json
        )
        VALUES(?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            name,
            stage,
            len(targets),
            compact_json(safe_public_payload(checkpoint_payload)),
        ),
    )
    checkpoint_id = int(cursor.lastrowid)
    record_event(
        conn,
        "rollout_checkpoint_created",
        source="state_db",
        message="rollout checkpoint created",
        payload={"id": checkpoint_id, "name": name, "stage": stage, "target_count": len(targets)},
    )
    return conn.execute("SELECT * FROM rollout_checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()


def list_rollout_checkpoints(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, created_at_utc, name, stage, target_count
        FROM rollout_checkpoints
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_rollout_checkpoint(conn: sqlite3.Connection, checkpoint_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM rollout_checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except json.JSONDecodeError:
        payload = {}
    data["payload"] = payload
    return data


def restore_slot_targets_from_checkpoint(conn: sqlite3.Connection, checkpoint_id: int) -> dict[str, Any]:
    checkpoint = get_rollout_checkpoint(conn, checkpoint_id)
    if checkpoint is None:
        raise ValueError(f"unknown rollout checkpoint {checkpoint_id}")
    targets = list((checkpoint.get("payload") or {}).get("slot_targets") or [])
    conn.execute("DELETE FROM slot_targets")
    for target in targets:
        set_slot_target(conn, target)
    record_event(
        conn,
        "rollout_checkpoint_restored",
        source="state_db",
        level="warning",
        message="rollout checkpoint restored slot targets",
        payload={"id": checkpoint_id, "target_count": len(targets)},
    )
    return {"id": checkpoint_id, "target_count": len(targets)}


def record_attempt(conn: sqlite3.Connection, attempt: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO attempts(
          at_utc, org_label, slot_name, action, profile_key, ok,
          duration_ms, error, payload_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attempt.get("at_utc") or utc_now(),
            attempt["org_label"],
            attempt["slot_name"],
            attempt["action"],
            attempt.get("profile_key"),
            1 if attempt.get("ok") else 0,
            attempt.get("duration_ms"),
            attempt.get("error"),
            compact_json(safe_public_payload(attempt.get("payload", {}))),
        ),
    )


def record_profit_snapshot(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO profit_snapshots(
          at_utc, scope, org_label, slot_name, profile_key, decision_price_usd,
          live_price_usd, th, cost_day, revenue_day, profit_day, payload_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.get("at_utc") or utc_now(),
            snapshot["scope"],
            snapshot.get("org_label"),
            snapshot.get("slot_name"),
            snapshot.get("profile_key"),
            float(snapshot["decision_price_usd"]),
            snapshot.get("live_price_usd"),
            snapshot.get("th"),
            snapshot.get("cost_day"),
            snapshot.get("revenue_day"),
            snapshot.get("profit_day"),
            compact_json(safe_public_payload(snapshot.get("payload", {}))),
        ),
    )


def attempt_stats(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    rows = conn.execute(
        """
        SELECT profile_key, action, ok, COUNT(*) AS count
        FROM attempts
        WHERE profile_key IS NOT NULL
          AND julianday(at_utc) >= julianday('now', '-24 hours')
        GROUP BY profile_key, action, ok
        """
    ).fetchall()
    stats: dict[str, dict[str, float]] = {}
    for row in rows:
        profile = str(row["profile_key"])
        item = stats.setdefault(profile, {"success": 0, "failure": 0, "capacity_failure": 0, "no_hash": 0})
        count = int(row["count"])
        if int(row["ok"]):
            item["success"] += count
        else:
            item["failure"] += count
            if str(row["action"]) in {"availability_zero", "capacity_failure"}:
                item["capacity_failure"] += count
            if str(row["action"]) in {"no_hash", "negative_no_hash"}:
                item["no_hash"] += count
    return stats


def status_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {}
    for table in (
        "organizations",
        "slots",
        "gpu_profiles",
        "profile_availability",
        "search_cooldowns",
        "price_history",
        "slot_targets",
        "attempts",
        "workers",
        "profile_scores",
        "heartbeats",
        "runtime_failures",
        "guard_issues",
        "api_rate_limits",
        "rollout_checkpoints",
        "events",
        "fleet_active_snapshots",
        "fleet_org_active_snapshots",
        "fleet_slot_active_snapshots",
        "fleet_org_balance_audits",
    ):
        tables[table] = int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
    risk = latest_risk_mode(conn)
    price = latest_price_sample(conn)
    heartbeats = [
        dict(row)
        for row in conn.execute("SELECT process_name, status, at_utc, stale_after_seconds FROM heartbeats ORDER BY process_name")
    ]
    slot_status = [
        dict(row)
        for row in conn.execute(
            """
            SELECT observed_status, COUNT(*) AS count
            FROM slots
            GROUP BY observed_status
            ORDER BY observed_status
            """
        )
    ]
    worker_status = [
        dict(row)
        for row in conn.execute(
            """
            SELECT stale, COUNT(*) AS count, SUM(reported_hashrate_th) AS reported_hashrate_th
            FROM workers
            GROUP BY stale
            ORDER BY stale
            """
        )
    ]
    return {
        "db": str(conn.execute("PRAGMA database_list").fetchone()["file"]),
        "tables": tables,
        "latest_risk_mode": dict(risk) if risk else None,
        "latest_price_sample": dict(price) if price else None,
        "heartbeats": heartbeats,
        "runtime_failures": [
            dict(row)
            for row in conn.execute("SELECT * FROM runtime_failures ORDER BY at_utc DESC").fetchall()
        ],
        "slot_status": slot_status,
        "worker_status": worker_status,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite state database for the Salad PRL fleet scheduler.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--init", action="store_true", help="Initialize/migrate the DB.")
    parser.add_argument("--sync-config", action="store_true", help="Load org/slot config into the DB.")
    parser.add_argument("--heartbeat", help="Write a heartbeat for a process name.")
    parser.add_argument("--status", action="store_true", help="Print DB status.")
    args = parser.parse_args()

    config = load_config()
    with connect(args.db) as conn:
        if args.init or args.sync_config or args.heartbeat or args.status:
            init_db(conn)
        if args.sync_config:
            sync_config(conn, config)
        if args.heartbeat:
            write_heartbeat(conn, args.heartbeat)
        conn.commit()
        if args.status:
            print(json_dumps(status_payload(conn)))


if __name__ == "__main__":
    main()
