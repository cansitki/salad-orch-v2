from __future__ import annotations

import pathlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_audit
import state_db
from config_loader import load_config


def insert_org_cost_snapshot(db_path: str, *, at_utc: str, costs: dict[str, float]) -> None:
    config = load_config()
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        cursor = conn.execute(
            """
            INSERT INTO fleet_active_snapshots(
              at_utc, assigned_targets, target_slots, live_hashing_gpus, live_th,
              cost_day, profit_day_064, market_profit_day, status_counts_json,
              org_summary_json, payload_json
            )
            VALUES(?, 40, 40, 1, 100.0, ?, 0.0, 0.0, '{}', '{}', '{}')
            """,
            (at_utc, sum(costs.values())),
        )
        snapshot_id = int(cursor.lastrowid)
        for org in config.enabled_orgs():
            conn.execute(
                """
                INSERT INTO fleet_org_active_snapshots(
                  snapshot_id, org_label, active_slots, running_slots, creating_slots,
                  allocating_slots, live_hashing_gpus, live_th, cost_day, profit_day,
                  payload_json
                )
                VALUES(?, ?, 1, 1, 0, 0, 1, 100.0, ?, 0.0, '{}')
                """,
                (snapshot_id, org.label, costs.get(org.label, 0.0)),
            )
        conn.commit()


class FleetAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            conn.commit()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_parse_money_handles_portal_text(self) -> None:
        self.assertEqual(fleet_audit.parse_money("$1,234.56"), 1234.56)
        self.assertEqual(fleet_audit.parse_money("-$12.34"), -12.34)
        self.assertIsNone(fleet_audit.parse_money("not available"))

    def test_missing_or_invalid_balance_source_does_not_raise(self) -> None:
        missing_file = str(pathlib.Path(self.tmpdir.name) / "missing.json")
        balances, source = fleet_audit.load_balance_values(balance_file=missing_file)
        self.assertEqual(balances, {})
        self.assertIn("missing_file:", source)

        balances, source = fleet_audit.load_balance_values(balance_json="{bad")
        self.assertEqual(balances, {})
        self.assertEqual(source, "invalid_json")

    def test_load_balance_values_can_read_monitor_db(self) -> None:
        monitor_db = str(pathlib.Path(self.tmpdir.name) / "monitor.db")
        with sqlite3.connect(monitor_db) as conn:
            conn.executescript(
                """
                CREATE TABLE monitor_snapshots (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  checked_at_utc TEXT NOT NULL
                );
                CREATE TABLE salad_org_balances (
                  snapshot_id INTEGER NOT NULL,
                  org TEXT NOT NULL,
                  ok INTEGER NOT NULL,
                  amount_cents INTEGER,
                  balance_usd REAL
                );
                """
            )
            conn.execute("INSERT INTO monitor_snapshots(id, checked_at_utc) VALUES(1, '2026-06-24T10:00:00+00:00')")
            conn.execute("INSERT INTO monitor_snapshots(id, checked_at_utc) VALUES(2, '2026-06-24T11:00:00+00:00')")
            conn.execute(
                "INSERT INTO salad_org_balances(snapshot_id, org, ok, amount_cents, balance_usd) VALUES(1, 'kray', 1, 500, 5.0)"
            )
            conn.execute(
                "INSERT INTO salad_org_balances(snapshot_id, org, ok, amount_cents, balance_usd) VALUES(2, 'kray', 1, 450, 4.5)"
            )
            conn.execute(
                "INSERT INTO salad_org_balances(snapshot_id, org, ok, amount_cents, balance_usd) VALUES(2, 'kray3', 1, 125, NULL)"
            )
            conn.commit()

        with patch.dict("os.environ", {"PRL_BALANCE_SOURCE_MAX_AGE_SECONDS": "999999999"}):
            balances, source = fleet_audit.load_balance_values(balance_file="missing.json", monitor_db=monitor_db)

        self.assertIn("monitor_db:", source)
        self.assertEqual(balances["kray"], 4.5)
        self.assertEqual(balances["kray3"], 1.25)

    def test_stale_balance_file_falls_back_to_monitor_db(self) -> None:
        balance_file = pathlib.Path(self.tmpdir.name) / "balances.json"
        balance_file.write_text('{"kray": 99.0}', encoding="utf-8")
        old = time.time() - 7200
        os.utime(balance_file, (old, old))
        monitor_db = str(pathlib.Path(self.tmpdir.name) / "monitor-fallback.db")
        with sqlite3.connect(monitor_db) as conn:
            conn.executescript(
                """
                CREATE TABLE monitor_snapshots (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  checked_at_utc TEXT NOT NULL
                );
                CREATE TABLE salad_org_balances (
                  snapshot_id INTEGER NOT NULL,
                  org TEXT NOT NULL,
                  ok INTEGER NOT NULL,
                  amount_cents INTEGER,
                  balance_usd REAL
                );
                """
            )
            conn.execute("INSERT INTO monitor_snapshots(id, checked_at_utc) VALUES(1, '2026-06-24T11:00:00+00:00')")
            conn.execute(
                "INSERT INTO salad_org_balances(snapshot_id, org, ok, amount_cents, balance_usd) VALUES(1, 'kray', 1, 450, 4.5)"
            )
            conn.commit()

        with patch.dict(
            "os.environ",
            {"PRL_BALANCE_FILE_MAX_AGE_SECONDS": "1", "PRL_BALANCE_SOURCE_MAX_AGE_SECONDS": "999999999"},
        ):
            balances, source = fleet_audit.load_balance_values(balance_file=str(balance_file), monitor_db=monitor_db)

        self.assertIn("monitor_db:", source)
        self.assertEqual(balances["kray"], 4.5)

    def test_stale_monitor_db_balances_are_not_treated_as_live(self) -> None:
        monitor_db = str(pathlib.Path(self.tmpdir.name) / "stale-monitor.db")
        with sqlite3.connect(monitor_db) as conn:
            conn.executescript(
                """
                CREATE TABLE monitor_snapshots (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  checked_at_utc TEXT NOT NULL
                );
                CREATE TABLE salad_org_balances (
                  snapshot_id INTEGER NOT NULL,
                  org TEXT NOT NULL,
                  ok INTEGER NOT NULL,
                  amount_cents INTEGER,
                  balance_usd REAL
                );
                """
            )
            conn.execute("INSERT INTO monitor_snapshots(id, checked_at_utc) VALUES(1, '2020-01-01T00:00:00+00:00')")
            conn.execute(
                "INSERT INTO salad_org_balances(snapshot_id, org, ok, amount_cents, balance_usd) VALUES(1, 'kray', 1, 500, 5.0)"
            )
            conn.commit()

        balances, source = fleet_audit.load_monitor_db_balances(monitor_db, max_age_seconds=1)

        self.assertEqual(balances, {})
        self.assertIn("stale_monitor_db:", source)

    def test_record_active_snapshot_persists_fleet_and_org_rows(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "4090:batch:2048",
                    "observed_status": "running",
                    "live_hashrate_th": 100.0,
                    "updated_at_utc": "2026-06-24T10:00:00+00:00",
                },
            )
            state_db.sync_worker_rows(
                conn,
                [
                    {
                        "worker_name": "kray-worker-1",
                        "org_label": "kray",
                        "slot_name": "prl-kray-roi-01",
                        "instance_id": "instance-1",
                        "gpu_key": "4090",
                        "reported_hashrate_th": 100.0,
                        "last_stats_at": "2026-06-24T10:00:00+00:00",
                    }
                ],
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T10:00:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "4090:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 100.0,
                    "cost_day": 1.2,
                    "revenue_day": 1.6,
                    "profit_day": 0.4,
                    "payload": {},
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T10:00:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "4090:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 100.0,
                    "cost_day": 1.2,
                    "revenue_day": 1.6,
                    "profit_day": 0.4,
                    "payload": {},
                },
            )
            conn.commit()

        report = {
            "assigned_targets": 40,
            "target_slots": 40,
            "live_hashing_gpus": 1,
            "live_th": 100.0,
            "status_counts": {"running": 1},
            "profit_at_0_64": {"cost_day": 1.2, "profit_day": 0.4},
            "profit_at_live": {"market_profit_day": 0.5},
        }
        with patch.object(fleet_audit.reporter, "build_report", return_value=report):
            payload = fleet_audit.record_active_snapshot(self.db_path)

        with state_db.connect(self.db_path) as conn:
            fleet_count = conn.execute("SELECT COUNT(*) FROM fleet_active_snapshots").fetchone()[0]
            kray = conn.execute(
                """
                SELECT active_slots, running_slots, live_hashing_gpus, live_th, cost_day, profit_day
                FROM fleet_org_active_snapshots
                WHERE snapshot_id = ? AND org_label = 'kray'
                """,
                (payload["snapshot_id"],),
            ).fetchone()
            kray_slot = conn.execute(
                """
                SELECT observed_status, observed_profile_key, live_hashrate_th, billable, cost_day, profit_day
                FROM fleet_slot_active_snapshots
                WHERE snapshot_id = ? AND org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """,
                (payload["snapshot_id"],),
            ).fetchone()
            heartbeat = conn.execute("SELECT * FROM heartbeats WHERE process_name = 'fleet_audit'").fetchone()

        self.assertEqual(fleet_count, 1)
        self.assertEqual(payload["slot_snapshots"], 40)
        self.assertEqual(kray["active_slots"], 1)
        self.assertEqual(kray["running_slots"], 1)
        self.assertEqual(kray["live_hashing_gpus"], 1)
        self.assertEqual(kray["live_th"], 100.0)
        self.assertEqual(kray["cost_day"], 1.2)
        self.assertEqual(kray["profit_day"], 0.4)
        self.assertEqual(kray_slot["observed_status"], "running")
        self.assertEqual(kray_slot["observed_profile_key"], "4090:batch:2048")
        self.assertEqual(kray_slot["live_hashrate_th"], 100.0)
        self.assertEqual(kray_slot["billable"], 1)
        self.assertEqual(kray_slot["cost_day"], 1.2)
        self.assertEqual(kray_slot["profit_day"], 0.4)
        self.assertIsNotNone(heartbeat)

    def test_record_active_snapshot_ignores_stale_profit_for_stopped_slot(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "4090:batch:2048",
                    "observed_status": "stopped",
                    "live_hashrate_th": 0.0,
                    "updated_at_utc": "2026-06-24T10:05:00+00:00",
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T10:00:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "4090:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 100.0,
                    "cost_day": 1.2,
                    "revenue_day": 1.6,
                    "profit_day": 0.4,
                    "payload": {},
                },
            )
            conn.commit()

        report = {
            "assigned_targets": 0,
            "target_slots": 40,
            "live_hashing_gpus": 0,
            "live_th": 0.0,
            "status_counts": {"stopped": 40},
            "profit_at_0_64": {"cost_day": 0.0, "profit_day": 0.0},
            "profit_at_live": {"market_profit_day": 0.0},
        }
        with patch.object(fleet_audit.reporter, "build_report", return_value=report):
            payload = fleet_audit.record_active_snapshot(self.db_path)

        with state_db.connect(self.db_path) as conn:
            kray = conn.execute(
                """
                SELECT active_slots, cost_day, profit_day
                FROM fleet_org_active_snapshots
                WHERE snapshot_id = ? AND org_label = 'kray'
                """,
                (payload["snapshot_id"],),
            ).fetchone()
            kray_slot = conn.execute(
                """
                SELECT observed_status, billable, cost_day, profit_day
                FROM fleet_slot_active_snapshots
                WHERE snapshot_id = ? AND org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """,
                (payload["snapshot_id"],),
            ).fetchone()

        self.assertEqual(kray["active_slots"], 0)
        self.assertEqual(kray["cost_day"], 0.0)
        self.assertEqual(kray["profit_day"], 0.0)
        self.assertEqual(kray_slot["observed_status"], "stopped")
        self.assertEqual(kray_slot["billable"], 0)
        self.assertIsNone(kray_slot["cost_day"])
        self.assertIsNone(kray_slot["profit_day"])

    def test_record_active_snapshot_marks_unknown_slots_stopped_when_idle(self) -> None:
        report = {
            "assigned_targets": 0,
            "target_slots": 40,
            "live_hashing_gpus": 0,
            "live_th": 0.0,
            "status_counts": {"stopped": 40},
            "profit_at_0_64": {"cost_day": 0.0, "profit_day": 0.0},
            "profit_at_live": {"market_profit_day": 0.0},
        }
        with patch.object(fleet_audit.reporter, "build_report", return_value=report):
            payload = fleet_audit.record_active_snapshot(self.db_path)

        with state_db.connect(self.db_path) as conn:
            slot_statuses = {
                row["observed_status"]: row["count"]
                for row in conn.execute(
                    """
                    SELECT observed_status, COUNT(*) AS count
                    FROM slots
                    WHERE org_label = 'kray'
                    GROUP BY observed_status
                    """
                ).fetchall()
            }
            snapshot_statuses = {
                row["observed_status"]: row["count"]
                for row in conn.execute(
                    """
                    SELECT observed_status, COUNT(*) AS count
                    FROM fleet_slot_active_snapshots
                    WHERE snapshot_id = ? AND org_label = 'kray'
                    GROUP BY observed_status
                    """,
                    (payload["snapshot_id"],),
                ).fetchall()
            }

        self.assertEqual(slot_statuses, {"stopped": 10})
        self.assertEqual(snapshot_statuses, {"stopped": 10})

    def test_record_active_snapshot_respects_enabled_org_filter(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "4090:batch:2048",
                    "observed_status": "running",
                    "live_hashrate_th": 100.0,
                    "updated_at_utc": "2026-06-24T10:00:00+00:00",
                },
            )
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kry1",
                    "slot_name": "prl-kry1-roi-01",
                    "observed_profile_key": "4090:batch:2048",
                    "observed_status": "running",
                    "live_hashrate_th": 100.0,
                    "updated_at_utc": "2026-06-24T10:00:00+00:00",
                },
            )
            state_db.sync_worker_rows(
                conn,
                [
                    {
                        "worker_name": "kray-worker-1",
                        "org_label": "kray",
                        "slot_name": "prl-kray-roi-01",
                        "instance_id": "instance-1",
                        "gpu_key": "4090",
                        "reported_hashrate_th": 100.0,
                        "last_stats_at": "2026-06-24T10:00:00+00:00",
                    },
                    {
                        "worker_name": "kry1-worker-1",
                        "org_label": "kry1",
                        "slot_name": "prl-kry1-roi-01",
                        "instance_id": "instance-2",
                        "gpu_key": "4090",
                        "reported_hashrate_th": 100.0,
                        "last_stats_at": "2026-06-24T10:00:00+00:00",
                    },
                ],
            )
            conn.commit()

        report = {
            "assigned_targets": 10,
            "target_slots": 10,
            "live_hashing_gpus": 1,
            "live_th": 100.0,
            "status_counts": {"running": 1},
            "profit_at_0_64": {"cost_day": 1.2, "profit_day": 0.4},
            "profit_at_live": {"market_profit_day": 0.5},
        }
        env = {
            "SALAD_FLEET_CONFIG_PATH": "",
            "PRL_FLEET_CONFIG_PATH": "",
            "SALAD_FLEET_CONFIG_JSON": "",
            "SALAD_FLEET_ORGS_JSON": "",
            "PRL_FLEET_ORGS_JSON": "",
            "SALAD_FLEET_EXTRA_ORGS_JSON": "",
            "PRL_FLEET_EXTRA_ORGS_JSON": "",
            "PRL_ENABLED_ORGS": "kray",
        }
        with (
            patch.dict("os.environ", env),
            patch.object(fleet_audit.reporter, "build_report", return_value=report),
        ):
            payload = fleet_audit.record_active_snapshot(self.db_path)

        with state_db.connect(self.db_path) as conn:
            orgs = [
                row["org_label"]
                for row in conn.execute(
                    """
                    SELECT org_label
                    FROM fleet_org_active_snapshots
                    WHERE snapshot_id = ?
                    ORDER BY org_label
                    """,
                    (payload["snapshot_id"],),
                ).fetchall()
            ]
            slot_orgs = {
                row["org_label"]
                for row in conn.execute(
                    """
                    SELECT org_label
                    FROM fleet_slot_active_snapshots
                    WHERE snapshot_id = ?
                    """,
                    (payload["snapshot_id"],),
                ).fetchall()
            }

        self.assertEqual(orgs, ["kray"])
        self.assertEqual(slot_orgs, {"kray"})
        self.assertEqual(set(payload["org_summary"]), {"kray"})
        self.assertEqual(payload["slot_snapshots"], 10)

    def test_record_active_snapshot_refreshes_profit_when_enabled(self) -> None:
        report = {
            "assigned_targets": 40,
            "target_slots": 40,
            "live_hashing_gpus": 1,
            "live_th": 100.0,
            "status_counts": {"running": 1},
            "profit_at_0_64": {"cost_day": 1.2, "profit_day": 0.4},
            "profit_at_live": {"market_profit_day": 0.5},
        }
        with (
            patch.object(fleet_audit, "refresh_profit_snapshot", return_value={"fresh_workers": 1}) as refresh,
            patch.object(fleet_audit.reporter, "build_report", return_value=report),
        ):
            payload = fleet_audit.record_active_snapshot(
                self.db_path,
                refresh_profit=True,
                profit_snapshot_price=0.55,
            )

        refresh.assert_called_once_with(self.db_path, price=0.55)
        self.assertEqual(payload["live_hashing_gpus"], 1)

    def test_balance_audit_marks_expected_hourly_cost_ok(self) -> None:
        balances = {"kray": 100.0, "kry1": 100.0, "kray2": 100.0, "kray3": 100.0}
        with patch.object(fleet_audit, "utc_now", return_value="2026-06-24T10:00:00+00:00"):
            baseline = fleet_audit.record_balance_audits(
                db_path=self.db_path,
                balances=balances,
                balance_source="json",
            )
        self.assertEqual({row["status"] for row in baseline}, {"baseline"})

        insert_org_cost_snapshot(
            self.db_path,
            at_utc="2026-06-24T10:30:00+00:00",
            costs={"kray": 24.0, "kry1": 0.0, "kray2": 0.0, "kray3": 0.0},
        )
        balances["kray"] = 99.0
        with patch.object(fleet_audit, "utc_now", return_value="2026-06-24T11:00:00+00:00"):
            rows = fleet_audit.record_balance_audits(
                db_path=self.db_path,
                balances=balances,
                balance_source="json",
            )

        kray = next(row for row in rows if row["org_label"] == "kray")
        self.assertEqual(kray["status"], "ok")
        self.assertAlmostEqual(kray["expected_cost_usd"], 1.0)
        self.assertAlmostEqual(kray["balance_delta_usd"], 1.0)
        self.assertAlmostEqual(kray["variance_usd"], 0.0)

    def test_balance_audit_flags_large_cost_variance(self) -> None:
        balances = {"kray": 100.0, "kry1": 100.0, "kray2": 100.0, "kray3": 100.0}
        with patch.object(fleet_audit, "utc_now", return_value="2026-06-24T10:00:00+00:00"):
            fleet_audit.record_balance_audits(db_path=self.db_path, balances=balances, balance_source="json")

        insert_org_cost_snapshot(
            self.db_path,
            at_utc="2026-06-24T10:30:00+00:00",
            costs={"kray": 24.0, "kry1": 0.0, "kray2": 0.0, "kray3": 0.0},
        )
        balances["kray"] = 95.0
        with patch.object(fleet_audit, "utc_now", return_value="2026-06-24T11:00:00+00:00"):
            rows = fleet_audit.record_balance_audits(db_path=self.db_path, balances=balances, balance_source="json")

        kray = next(row for row in rows if row["org_label"] == "kray")
        self.assertEqual(kray["status"], "mismatch")
        self.assertAlmostEqual(kray["expected_cost_usd"], 1.0)
        self.assertAlmostEqual(kray["balance_delta_usd"], 5.0)
        self.assertAlmostEqual(kray["variance_usd"], 4.0)


if __name__ == "__main__":
    unittest.main()
