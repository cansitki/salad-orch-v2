from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import reporter
import state_db
from config_loader import load_config


class ReporterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")
        self.balance_file = pathlib.Path(self.tmpdir.name) / "balances.json"
        self._balance_env = {
            "PRL_ORG_BALANCE_FILE": os.environ.get("PRL_ORG_BALANCE_FILE"),
            "PRL_BALANCE_FILE": os.environ.get("PRL_BALANCE_FILE"),
            "SALAD_BALANCE_FILE": os.environ.get("SALAD_BALANCE_FILE"),
        }
        os.environ.pop("PRL_ORG_BALANCE_FILE", None)
        os.environ.pop("SALAD_BALANCE_FILE", None)
        os.environ["PRL_BALANCE_FILE"] = str(self.balance_file)
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            conn.commit()

    def tearDown(self) -> None:
        for key, value in self._balance_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmpdir.cleanup()

    def test_live_th_falls_back_to_latest_slot_snapshots(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T11:59:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-03",
                    "profile_key": "4090:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 150.0,
                    "cost_day": 3.36,
                    "revenue_day": 4.0,
                    "profit_day": 0.64,
                    "payload": {"worker": "kray-prl-old"},
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T12:00:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "3090:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 100.5,
                    "cost_day": 2.16,
                    "revenue_day": 3.0,
                    "profit_day": 0.84,
                    "payload": {"worker": "kray-prl-test"},
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T12:00:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-02",
                    "profile_key": "3080:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 0,
                    "cost_day": 1.44,
                    "revenue_day": 0,
                    "profit_day": -1.44,
                    "payload": {"worker": "NO_POOL_HASHRATE"},
                },
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["live_th_source"], "profit_snapshots")
        self.assertEqual(report["live_hashing_gpus"], 1)
        self.assertEqual(report["snapshot_live_hashing_gpus"], 1)
        self.assertEqual(report["live_th"], 100.5)
        self.assertEqual(report["snapshot_live_th"], 100.5)
        self.assertEqual(report["snapshot_live_at_utc"], "2026-06-24T12:00:00+00:00")

    def test_newer_zero_fleet_snapshot_does_not_use_stale_slot_snapshots(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T11:59:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "3090:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 100.5,
                    "cost_day": 2.16,
                    "revenue_day": 3.0,
                    "profit_day": 0.84,
                    "payload": {"worker": "kray-prl-old"},
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T12:00:00+00:00",
                    "scope": "fleet",
                    "decision_price_usd": 0.64,
                    "live_price_usd": 0.64,
                    "th": 0.0,
                    "cost_day": 0.0,
                    "revenue_day": 0.0,
                    "profit_day": 0.0,
                    "payload": {"totals": {"th": 0.0, "cost_day": 0.0, "prl_day": 0.0}},
                },
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["snapshot_live_th"], 0)
        self.assertEqual(report["snapshot_live_hashing_gpus"], 0)
        self.assertEqual(report["live_th"], 0)

    def test_slot_hashrate_wins_over_snapshot_fallback(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_status": "running",
                    "observed_profile_key": "3090:batch:2048",
                    "live_hashrate_th": 111.5,
                },
            )
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-02",
                    "observed_status": "running",
                    "observed_profile_key": "3080:batch:2048",
                    "live_hashrate_th": 0,
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T12:00:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "3090:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 100.5,
                    "cost_day": 2.16,
                    "revenue_day": 3.0,
                    "profit_day": 0.84,
                    "payload": {"worker": "kray-prl-test"},
                },
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["live_th_source"], "slots")
        self.assertEqual(report["live_hashing_gpus"], 1)
        self.assertEqual(report["live_th"], 111.5)
        self.assertEqual(report["snapshot_live_th"], 100.5)

    def test_worker_hashrate_wins_over_slot_observations(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_status": "running",
                    "observed_profile_key": "3090:batch:2048",
                    "live_hashrate_th": 0,
                },
            )
            state_db.sync_worker_rows(
                conn,
                [
                    {
                        "worker_name": "kray-prl-test-pearlfortune-inst-1",
                        "org_label": "kray",
                        "slot_name": "prl-kray-roi-01",
                        "instance_id": "inst-1",
                        "gpu_key": "3090",
                        "reported_hashrate_th": 101.5,
                        "last_stats_at": "2026-06-24T12:00:00+00:00",
                    }
                ],
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["live_th_source"], "workers")
        self.assertEqual(report["live_workers"], 1)
        self.assertEqual(report["live_hashing_gpus"], 1)
        self.assertEqual(report["worker_th"], 101.5)
        self.assertEqual(report["live_th"], 101.5)
        self.assertEqual(report["slot_live_hashing_gpus"], 0)
        self.assertEqual(report["running_no_live_billable_slots"], [])

    def test_running_no_live_falls_back_to_stale_slot_observation(self) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        with state_db.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE slots
                SET observed_status='running',
                    observed_profile_key='3090:batch:2048',
                    live_hashrate_th=0,
                    protected=0,
                    observed_profile_since_utc=?,
                    observed_status_since_utc=?,
                    updated_at_utc=?
                WHERE org_label='kray' AND slot_name='prl-kray-roi-01'
                """,
                (
                    (now - timedelta(minutes=3)).isoformat(timespec="seconds"),
                    (now - timedelta(minutes=10)).isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(len(report["running_no_live_billable_slots"]), 1)
        issue = report["running_no_live_billable_slots"][0]
        self.assertEqual(issue["source"], "slot_observation")
        self.assertEqual(issue["org"], "kray")
        self.assertEqual(issue["slot"], "prl-kray-roi-01")
        self.assertGreaterEqual(issue["state_age_seconds"], 60)

    def test_fresh_running_no_live_slot_observation_stays_in_grace(self) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        with state_db.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE slots
                SET observed_status='running',
                    observed_profile_key='3090:batch:2048',
                    live_hashrate_th=0,
                    protected=0,
                    observed_profile_since_utc=?,
                    observed_status_since_utc=?,
                    updated_at_utc=?
                WHERE org_label='kray' AND slot_name='prl-kray-roi-01'
                """,
                (
                    (now - timedelta(seconds=20)).isoformat(timespec="seconds"),
                    (now - timedelta(minutes=10)).isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["running_no_live_billable_slots"], [])

    def test_deploying_slots_count_as_active_pending(self) -> None:
        with state_db.connect(self.db_path) as conn:
            for slot_name, status in (
                ("prl-kray-roi-01", "deploying"),
                ("prl-kray-roi-02", "allocating"),
                ("prl-kray-roi-03", "running"),
            ):
                state_db.update_slot_observation(
                    conn,
                    {
                        "org_label": "kray",
                        "slot_name": slot_name,
                        "observed_status": status,
                        "observed_profile_key": "3090:batch:2048",
                    },
                )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["status_counts"]["deploying"], 1)
        self.assertEqual(report["active_pending_slots"], 3)

    def test_stuck_slots_use_observed_status_age_not_refresh_age(self) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        with state_db.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE slots
                SET observed_status='allocating',
                    observed_profile_key='3090:batch:2048',
                    observed_status_since_utc=?,
                    updated_at_utc=?
                WHERE org_label='kray' AND slot_name='prl-kray-roi-01'
                """,
                (
                    (now - timedelta(minutes=12)).isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(len(report["stuck_slots"]), 1)
        self.assertEqual(report["stuck_slots"][0]["slot_name"], "prl-kray-roi-01")
        self.assertEqual(report["stuck_slots"][0]["age_source"], "observed_status_since_utc")

    def test_mature_pending_uses_operational_pending_threshold(self) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        with state_db.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE slots
                SET observed_status='deploying',
                    observed_profile_key='3090:batch:2048',
                    observed_status_since_utc=?,
                    updated_at_utc=?
                WHERE org_label='kray' AND slot_name='prl-kray-roi-01'
                """,
                (
                    (now - timedelta(minutes=6)).isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["mature_pending_after_seconds"], 300)
        self.assertEqual(len(report["mature_pending_slots"]), 1)
        self.assertEqual(report["mature_pending_slots"][0]["slot_name"], "prl-kray-roi-01")
        self.assertEqual(report["mature_pending_slots"][0]["observed_status"], "deploying")
        self.assertEqual(report["stuck_slots"], [])

    def test_target_slots_uses_db_when_runtime_config_has_more_slots(self) -> None:
        with state_db.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO slots(org_label, slot_name, slot_index, observed_status, updated_at_utc)
                VALUES('extra', 'prl-extra-roi-01', 1, 'zero_quota', '2026-06-24T12:00:00+00:00')
                """
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["target_slots"], 41)

    def test_profit_scenarios_are_derived_from_latest_fleet_snapshot(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T12:00:00+00:00",
                    "scope": "fleet",
                    "decision_price_usd": 0.64,
                    "live_price_usd": 0.68,
                    "th": 1000.0,
                    "cost_day": 20.0,
                    "revenue_day": 30.0,
                    "profit_day": 10.0,
                    "payload": {"totals": {"prl_day": 46.875, "cost_day": 20.0}},
                },
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["profit_at_0_64"]["source"], "latest_snapshot")
        self.assertAlmostEqual(report["profit_at_0_64"]["profit_day"], 10.0)
        self.assertEqual(report["profit_at_0_70"]["source"], "latest_snapshot")
        self.assertAlmostEqual(report["profit_at_0_70"]["revenue_day"], 32.8125)
        self.assertAlmostEqual(report["profit_at_0_70"]["profit_day"], 12.8125)

    def test_report_includes_replica_quota_blockers(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.upsert_org_replica_quota(
                conn,
                {
                    "org_label": "kray",
                    "quota": 0,
                    "used": 0,
                    "available": 0,
                    "status": "zero_quota",
                    "reason": "container_replicas_quota_zero",
                    "source": "test",
                },
            )
            state_db.upsert_org_replica_quota(
                conn,
                {
                    "org_label": "kray2",
                    "quota": 10,
                    "used": 7,
                    "available": 3,
                    "status": "available",
                    "reason": "container_replicas_quota_available",
                    "source": "test",
                },
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(len(report["replica_quotas"]), 2)
        self.assertEqual([row["org_label"] for row in report["quota_blockers"]], ["kray"])
        self.assertEqual(report["replica_quota_summary"][0]["status"], "available")
        self.assertEqual(report["replica_quota_summary"][1]["status"], "zero_quota")
        self.assertEqual(report["capacity_summary"]["quota_known_slots"], 20)
        self.assertEqual(report["capacity_summary"]["quota_capacity_slots"], 10)
        self.assertEqual(report["capacity_summary"]["quota_used_slots"], 7)
        self.assertEqual(report["capacity_summary"]["quota_blocked_slots"], 10)
        self.assertEqual(report["capacity_summary"]["balance_blocked_slots"], 0)
        self.assertEqual(report["capacity_summary"]["quota_unknown_slots"], 20)

    def test_report_includes_capacity_actions_from_quota_and_balance(self) -> None:
        self.balance_file.write_text(
            json.dumps(
                {
                    "kray": 0.0,
                    "kry1": 4.25,
                    "kray2": 0.0,
                }
            ),
            encoding="utf-8",
        )
        with state_db.connect(self.db_path) as conn:
            state_db.upsert_org_replica_quota(
                conn,
                {
                    "org_label": "kray",
                    "quota": 10,
                    "used": 0,
                    "available": 10,
                    "status": "available",
                    "reason": "container_replicas_quota_available",
                    "source": "test",
                },
            )
            state_db.upsert_org_replica_quota(
                conn,
                {
                    "org_label": "kry1",
                    "quota": 0,
                    "used": 0,
                    "available": 0,
                    "status": "zero_quota",
                    "reason": "container_replicas_quota_zero",
                    "source": "test",
                },
            )
            state_db.upsert_org_replica_quota(
                conn,
                {
                    "org_label": "kray2",
                    "quota": 0,
                    "used": 0,
                    "available": 0,
                    "status": "zero_quota",
                    "reason": "container_replicas_quota_zero",
                    "source": "test",
                },
            )
            conn.commit()

        report = reporter.build_report(self.db_path)
        actions = report["capacity_actions"]

        self.assertEqual([row["org_label"] for row in actions["top_up_quota_available_orgs"]], ["kray"])
        self.assertEqual(actions["top_up_quota_available_orgs"][0]["available_slots_if_funded"], 10)
        self.assertEqual([row["org_label"] for row in actions["quota_blocked_funded_orgs"]], ["kry1"])
        self.assertEqual(actions["quota_blocked_funded_orgs"][0]["balance_usd"], 4.25)
        self.assertEqual([row["org_label"] for row in actions["zero_balance_zero_quota_orgs"]], ["kray2"])
        self.assertEqual(
            actions["summary"],
            {
                "fillable_now_slots": 0,
                "fillable_now_balance_usd": 0,
                "fillable_now_target_cost_day_usd": 0,
                "fillable_now_target_profit_day_usd": 0,
                "fillable_now_funding_gap_24h_usd": 0,
                "top_up_slots": 10,
                "top_up_target_cost_day_usd": 0,
                "top_up_target_profit_day_usd": 0,
                "top_up_funding_gap_24h_usd": 0,
                "quota_blocked_funded_slots": 10,
                "quota_blocked_funded_balance_usd": 4.25,
                "quota_blocked_funded_target_cost_day_usd": 0,
                "quota_blocked_funded_target_profit_day_usd": 0,
                "quota_blocked_funded_funding_gap_24h_usd": 0,
                "zero_balance_zero_quota_slots": 10,
                "zero_balance_zero_quota_target_cost_day_usd": 0,
                "zero_balance_zero_quota_target_profit_day_usd": 0,
                "zero_balance_zero_quota_funding_gap_24h_usd": 0,
            },
        )

    def test_capacity_actions_include_fillable_now_orgs(self) -> None:
        self.balance_file.write_text(json.dumps({"kray": 6.5}), encoding="utf-8")
        with state_db.connect(self.db_path) as conn:
            state_db.upsert_org_replica_quota(
                conn,
                {
                    "org_label": "kray",
                    "quota": 10,
                    "used": 3,
                    "available": 7,
                    "status": "available",
                    "reason": "container_replicas_quota_available",
                    "source": "test",
                },
            )
            conn.commit()

        report = reporter.build_report(self.db_path)
        actions = report["capacity_actions"]

        self.assertEqual([row["org_label"] for row in actions["fillable_now_orgs"]], ["kray"])
        self.assertEqual(actions["fillable_now_orgs"][0]["fillable_slots"], 7)
        self.assertEqual(actions["fillable_now_orgs"][0]["balance_usd"], 6.5)
        self.assertEqual(actions["summary"]["fillable_now_slots"], 7)
        self.assertEqual(actions["summary"]["fillable_now_balance_usd"], 6.5)

    def test_capacity_actions_include_target_profit_and_cost_estimates(self) -> None:
        self.balance_file.write_text(json.dumps({"kray": 0.0}), encoding="utf-8")
        with state_db.connect(self.db_path) as conn:
            state_db.upsert_gpu_profiles(
                conn,
                [
                    {
                        "profile_key": "4090:batch:2048",
                        "gpu_key": "4090",
                        "gpu_id": "gpu-4090",
                        "priority": "batch",
                        "label": "RTX 4090 batch",
                        "memory_mb": 2048,
                        "expected_th": 230.0,
                        "static_hourly_usd": 0.16,
                    },
                    {
                        "profile_key": "3060ti:batch:2048",
                        "gpu_key": "3060ti",
                        "gpu_id": "gpu-3060ti",
                        "priority": "batch",
                        "label": "RTX 3060 Ti batch",
                        "memory_mb": 2048,
                        "expected_th": 80.0,
                        "static_hourly_usd": 0.03,
                    },
                ],
            )
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "4090:batch:2048",
                    "mode": "risk_off",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 1.25,
                    "reason": "test",
                },
            )
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-02",
                    "profile_key": "3060ti:batch:2048",
                    "mode": "risk_off",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 0.25,
                    "reason": "test",
                },
            )
            state_db.upsert_org_replica_quota(
                conn,
                {
                    "org_label": "kray",
                    "quota": 10,
                    "used": 0,
                    "available": 10,
                    "status": "available",
                    "reason": "container_replicas_quota_available",
                    "source": "test",
                },
            )
            conn.commit()

        report = reporter.build_report(self.db_path)
        row = report["capacity_actions"]["top_up_quota_available_orgs"][0]

        self.assertEqual(row["org_label"], "kray")
        self.assertEqual(row["target_slots"], 2)
        self.assertAlmostEqual(row["target_profit_day_usd"], 1.5)
        self.assertAlmostEqual(row["target_cost_day_usd"], 4.56)
        self.assertAlmostEqual(row["target_min_balance_24h_usd"], 4.56)
        self.assertAlmostEqual(row["target_runway_hours"], 0.0)
        self.assertAlmostEqual(row["target_funding_gap_24h_usd"], 4.56)
        self.assertAlmostEqual(report["capacity_actions"]["summary"]["top_up_target_profit_day_usd"], 1.5)
        self.assertAlmostEqual(report["capacity_actions"]["summary"]["top_up_target_cost_day_usd"], 4.56)
        self.assertAlmostEqual(report["capacity_actions"]["summary"]["top_up_funding_gap_24h_usd"], 4.56)

    def test_capacity_action_lines_include_actionable_orgs(self) -> None:
        actions = {
            "summary": {
                "fillable_now_slots": 0,
                "top_up_slots": 20,
                "top_up_target_profit_day_usd": 17.4,
                "top_up_funding_gap_24h_usd": 48.48,
                "quota_blocked_funded_slots": 20,
                "zero_balance_zero_quota_slots": 10,
            },
            "top_up_quota_available_orgs": [
                {
                    "org_label": "kray",
                    "balance_usd": 0.0,
                    "quota": 10,
                    "slots": 10,
                    "target_profit_day_usd": 8.7,
                    "target_cost_day_usd": 24.24,
                    "target_runway_hours": 0.0,
                    "target_funding_gap_24h_usd": 24.24,
                },
                {"org_label": "kray2", "balance_usd": 0.0, "quota": 10, "slots": 10},
            ],
            "quota_blocked_funded_orgs": [
                {"org_label": "kr1", "balance_usd": 4.25, "quota": 0, "slots": 10},
                {"org_label": "sal7-3", "balance_usd": 8.93, "quota": 0, "slots": 10},
            ],
            "zero_balance_zero_quota_orgs": [
                {"org_label": "alpha1", "balance_usd": 0.0, "quota": 0, "slots": 10},
            ],
        }

        lines = reporter.capacity_action_lines(actions, limit=1)

        self.assertEqual(
            lines,
            [
                "capacity_actions fillable_now_slots=0 top_up_slots=20 top_up_gap_24h=$48.48 top_up_profit=$17.40/day quota_blocked_funded_slots=20 zero_balance_zero_quota_slots=10",
                "  add_credit: kray(balance=$0.00,quota=10,slots=10,target_profit=$8.70/day,target_cost=$24.24/day,runway=0.00h,gap_24h=$24.24), +1 more",
                "  wait_quota_funded: sal7-3(balance=$8.93,quota=0,slots=10), +1 more",
                "  deprioritized_zero_balance_zero_quota: alpha1(balance=$0.00,quota=0,slots=10)",
            ],
        )

    def test_capacity_action_lines_limit_zero_prints_full_lists(self) -> None:
        actions = {
            "summary": {
                "fillable_now_slots": 0,
                "top_up_slots": 20,
                "top_up_target_profit_day_usd": 17.4,
                "top_up_funding_gap_24h_usd": 48.48,
                "quota_blocked_funded_slots": 20,
                "zero_balance_zero_quota_slots": 20,
            },
            "top_up_quota_available_orgs": [
                {"org_label": "kray", "balance_usd": 0.0, "quota": 10, "slots": 10},
                {"org_label": "kray2", "balance_usd": 0.0, "quota": 10, "slots": 10},
            ],
            "quota_blocked_funded_orgs": [
                {"org_label": "kr1", "balance_usd": 4.25, "quota": 0, "slots": 10},
                {"org_label": "sal7-3", "balance_usd": 8.93, "quota": 0, "slots": 10},
            ],
            "zero_balance_zero_quota_orgs": [
                {"org_label": "alpha1", "balance_usd": 0.0, "quota": 0, "slots": 10},
                {"org_label": "alpha2", "balance_usd": 0.0, "quota": 0, "slots": 10},
            ],
        }

        lines = reporter.capacity_action_lines(actions, limit=0)

        self.assertIn("kray2(balance=$0.00,quota=10,slots=10)", lines[1])
        self.assertIn("kr1(balance=$4.25,quota=0,slots=10)", lines[2])
        self.assertIn("alpha2(balance=$0.00,quota=0,slots=10)", lines[3])
        self.assertNotIn("more", "\n".join(lines))

    def test_zero_balance_slots_are_not_counted_as_unknown_quota(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.upsert_org_replica_quota(
                conn,
                {
                    "org_label": "kray",
                    "quota": 10,
                    "used": 10,
                    "available": 0,
                    "status": "available",
                    "reason": "container_replicas_quota_available",
                    "source": "test",
                },
            )
            conn.execute("UPDATE slots SET observed_status='zero_balance' WHERE org_label='kry1'")
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["capacity_summary"]["quota_known_slots"], 10)
        self.assertEqual(report["capacity_summary"]["quota_capacity_slots"], 10)
        self.assertEqual(report["capacity_summary"]["quota_used_slots"], 10)
        self.assertEqual(report["capacity_summary"]["quota_blocked_slots"], 0)
        self.assertEqual(report["capacity_summary"]["balance_blocked_slots"], 10)
        self.assertEqual(report["capacity_summary"]["quota_unknown_slots"], 20)


if __name__ == "__main__":
    unittest.main()
