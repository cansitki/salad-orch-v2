from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rollout
import state_db
from config_loader import load_config


class RolloutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_shadow_db_only_rollout_builds_safe_targets(self) -> None:
        payload = rollout.run_rollout(
            stage="shadow",
            db_path=self.db_path,
            price=0.64,
            fee=0.01,
            skip_workers=True,
            skip_guard=True,
        )
        self.assertTrue(payload["gates"]["ok"])
        self.assertEqual(payload["gates"]["coverage"], {"assigned_targets": 40, "target_slots": 40})
        self.assertEqual(payload["health"]["health"], "healthy")
        self.assertEqual(payload["report"]["assigned_targets"], 40)

    def test_gate_fails_when_runtime_failure_exists(self) -> None:
        scheduler_payload = {
            "mode": "base_fill",
            "assigned_targets": 40,
            "target_slots": 40,
        }
        report_payload = {
            "running_no_live_billable_slots": [],
            "negative_slots": [],
        }
        health_payload = {
            "health": "degraded",
            "runtime_failures": [{"component": "guard"}],
            "stale_heartbeats": [],
        }
        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload=scheduler_payload,
            worker_payloads=[],
            guard_payload=None,
            report_payload=report_payload,
            health_payload=health_payload,
            allow_degraded=False,
        )
        self.assertFalse(gates["ok"])
        self.assertEqual(gates["failed"][0]["gate"], "runtime_failures")

    def test_portal_balance_failure_warns_without_blocking_gpu_actions(self) -> None:
        scheduler_payload = {
            "mode": "base_fill",
            "assigned_targets": 40,
            "target_slots": 40,
        }
        report_payload = {
            "running_no_live_billable_slots": [],
            "negative_slots": [],
        }
        health_payload = {
            "health": "degraded",
            "runtime_failures": [{"component": "portal_balances"}],
            "stale_heartbeats": [],
        }
        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload=scheduler_payload,
            worker_payloads=[],
            guard_payload=None,
            report_payload=report_payload,
            health_payload=health_payload,
            allow_degraded=False,
        )
        self.assertTrue(gates["ok"])
        self.assertEqual(gates["failed"], [])
        self.assertEqual(gates["warnings"][0]["gate"], "runtime_failures")

    def test_gate_fails_when_target_coverage_is_incomplete(self) -> None:
        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload={"mode": "base_fill", "assigned_targets": 39, "target_slots": 40},
            worker_payloads=[],
            guard_payload=None,
            report_payload={"running_no_live_billable_slots": [], "negative_slots": []},
            health_payload={"health": "healthy", "runtime_failures": [], "stale_heartbeats": []},
            allow_degraded=False,
        )
        self.assertFalse(gates["ok"])
        self.assertEqual(gates["failed"][0]["gate"], "target_coverage")

    def test_target_profit_gate_allows_protected_live_positive_slot(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "5070:low:2048",
                    "observed_status": "running",
                    "live_hashrate_th": 121.7,
                    "protected": True,
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T23:30:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "5070:low:2048",
                    "decision_price_usd": 0.64,
                    "th": 121.7,
                    "cost_day": 3.192,
                    "revenue_day": 3.24,
                    "profit_day": 0.048,
                    "payload": {},
                },
            )
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "5070:low:2048",
                    "mode": "risk_off",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": -0.06,
                    "protected": True,
                    "reason": "risk_off:negative_observed_profile_no_replacement",
                },
            )
            conn.commit()

        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload={"mode": "risk_off", "assigned_targets": 40, "target_slots": 40},
            worker_payloads=[],
            guard_payload=None,
            report_payload={"running_no_live_billable_slots": [], "negative_slots": []},
            health_payload={"health": "healthy", "runtime_failures": [], "stale_heartbeats": []},
            allow_degraded=False,
        )

        self.assertTrue(gates["ok"])
        self.assertEqual(gates["failed"], [])

    def test_stale_heartbeats_warn_by_default_for_one_shot_rollout(self) -> None:
        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload={"mode": "base_fill", "assigned_targets": 40, "target_slots": 40},
            worker_payloads=[],
            guard_payload=None,
            report_payload={"running_no_live_billable_slots": [], "negative_slots": []},
            health_payload={
                "health": "degraded",
                "runtime_failures": [],
                "stale_heartbeats": [{"process_name": "fleet_scheduler"}],
            },
            allow_degraded=False,
        )
        self.assertTrue(gates["ok"])
        self.assertEqual(gates["warnings"][0]["gate"], "stale_heartbeats")

    def test_stale_heartbeats_can_be_required_as_hard_gate(self) -> None:
        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload={"mode": "base_fill", "assigned_targets": 40, "target_slots": 40},
            worker_payloads=[],
            guard_payload=None,
            report_payload={"running_no_live_billable_slots": [], "negative_slots": []},
            health_payload={
                "health": "degraded",
                "runtime_failures": [],
                "stale_heartbeats": [{"process_name": "fleet_scheduler"}],
            },
            allow_degraded=False,
            require_fresh_heartbeats=True,
        )
        self.assertFalse(gates["ok"])
        self.assertEqual(gates["failed"][0]["gate"], "stale_heartbeats")

    def test_transient_worker_start_failure_warns_without_blocking_fill(self) -> None:
        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload={"mode": "base_fill", "assigned_targets": 40, "target_slots": 40},
            worker_payloads=[
                {
                    "org": "kray",
                    "results": [
                        {
                            "slot_name": "prl-kray-roi-01",
                            "action": "start_failed",
                            "ok": False,
                            "error": "http_400:replicas_quota_exceeded",
                        }
                    ],
                }
            ],
            guard_payload=None,
            report_payload={"running_no_live_billable_slots": [], "negative_slots": []},
            health_payload={"health": "healthy", "runtime_failures": [], "stale_heartbeats": []},
            allow_degraded=False,
        )

        self.assertTrue(gates["ok"])
        self.assertEqual(gates["failed"], [])
        self.assertEqual(gates["warnings"][0]["gate"], "worker_actions")

    def test_non_transient_worker_start_failure_still_blocks(self) -> None:
        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload={"mode": "base_fill", "assigned_targets": 40, "target_slots": 40},
            worker_payloads=[
                {
                    "org": "kray",
                    "results": [
                        {
                            "slot_name": "prl-kray-roi-01",
                            "action": "start_failed",
                            "ok": False,
                            "error": "http_401:unauthorized",
                        }
                    ],
                }
            ],
            guard_payload=None,
            report_payload={"running_no_live_billable_slots": [], "negative_slots": []},
            health_payload={"health": "healthy", "runtime_failures": [], "stale_heartbeats": []},
            allow_degraded=False,
        )

        self.assertFalse(gates["ok"])
        self.assertEqual(gates["failed"][0]["gate"], "worker_actions")

    def test_all_org_live_apply_requires_confirmation(self) -> None:
        with self.assertRaises(SystemExit):
            rollout.run_rollout(
                stage="all-orgs",
                db_path=self.db_path,
                apply_workers=True,
                skip_workers=True,
                skip_guard=True,
            )

    def test_live_retarget_requires_confirmation(self) -> None:
        with self.assertRaises(SystemExit):
            rollout.run_rollout(
                stage="one-org",
                org_label="kry1",
                db_path=self.db_path,
                apply_workers=True,
                allow_live_retarget=True,
                skip_workers=True,
                skip_guard=True,
            )

    def test_live_apply_rollout_creates_checkpoint_before_scheduler_tick(self) -> None:
        payload = rollout.run_rollout(
            stage="one-org",
            org_label="kry1",
            db_path=self.db_path,
            price=0.64,
            fee=0.01,
            apply_workers=True,
            skip_workers=True,
            skip_guard=True,
        )
        self.assertIsNotNone(payload["checkpoint"])
        self.assertEqual(payload["checkpoint"]["stage"], "one-org")

    def test_pending_cooldown_triggers_same_cycle_second_worker_pass(self) -> None:
        scheduler_payload = {
            "mode": "base_fill",
            "assigned_targets": 40,
            "target_slots": 40,
        }
        report_payload = {
            "assigned_targets": 40,
            "target_slots": 40,
            "active_pending_slots": 0,
            "live_hashing_gpus": 0,
            "running_no_live_billable_slots": [],
            "negative_slots": [],
            "stuck_slots": [],
            "capacity_actions": {
                "summary": {
                    "top_up_slots": 20,
                    "quota_blocked_funded_slots": 200,
                    "zero_balance_zero_quota_slots": 30,
                }
            },
        }
        health_payload = {
            "health": "healthy",
            "target_count": 40,
            "slot_count": 40,
            "runtime_failures": [],
            "guard_issues": [],
            "stale_heartbeats": [],
        }
        shadow_payload = {
            "ok": True,
            "unsafe_targets": [],
            "missing_targets": [],
            "mismatches": [],
            "warnings": [],
            "diversification": {"unique_target_profiles": 4, "top_profile_share": 0.25},
        }
        worker_passes = [
            [
                {
                    "org": "kry1",
                    "apply": True,
                    "targets": 10,
                    "action_counts": {"cooldown_pending": 1},
                    "results": [{"slot_name": "roi01", "action": "cooldown_pending", "ok": True}],
                }
            ],
            [
                {
                    "org": "kry1",
                    "apply": True,
                    "targets": 10,
                    "action_counts": {"patch": 1},
                    "results": [{"slot_name": "roi01", "action": "patch", "ok": True}],
                }
            ],
        ]

        with (
            mock.patch.object(rollout.fleet_scheduler, "schedule_once", return_value=scheduler_payload) as schedule_once,
            mock.patch.object(rollout.reporter, "build_report", return_value=report_payload),
            mock.patch.object(rollout.health, "build_health", return_value=health_payload),
            mock.patch.object(rollout.shadow_compare, "build_shadow_compare", return_value=shadow_payload),
            mock.patch.object(rollout, "_run_org_workers", side_effect=worker_passes) as run_workers,
        ):
            payload = rollout.run_rollout(
                stage="one-org",
                org_label="kry1",
                db_path=self.db_path,
                price=0.64,
                fee=0.01,
                apply_workers=True,
                allow_pending_retarget=True,
                skip_guard=True,
            )

        self.assertEqual(run_workers.call_count, 2)
        self.assertEqual(schedule_once.call_count, 3)
        self.assertEqual(len(payload["workers"]), 2)
        self.assertEqual(payload["workers"][0]["action_counts"], {"cooldown_pending": 1})
        self.assertEqual(payload["workers"][1]["action_counts"], {"patch": 1})
        self.assertEqual(
            payload["report"]["capacity_action_summary"],
            {
                "top_up_slots": 20,
                "quota_blocked_funded_slots": 200,
                "zero_balance_zero_quota_slots": 30,
            },
        )

    def test_scheduler_replacement_target_triggers_same_cycle_second_worker_pass(self) -> None:
        initial_scheduler_payload = {
            "mode": "base_fill",
            "assigned_targets": 40,
            "target_slots": 40,
            "targets": [],
        }
        replacement_scheduler_payload = {
            "mode": "base_fill",
            "assigned_targets": 40,
            "target_slots": 40,
            "targets": [
                {
                    "org_label": "kry1",
                    "slot_name": "prl-kry1-roi-01",
                    "profile_key": "5090:batch:2048",
                    "reason": "risk_off:replace_nohash_observed_profile:4070tis:low:2048:availability_probe_fallback",
                }
            ],
        }
        final_scheduler_payload = {
            "mode": "base_fill",
            "assigned_targets": 40,
            "target_slots": 40,
            "targets": [],
        }
        report_payload = {
            "assigned_targets": 40,
            "target_slots": 40,
            "active_pending_slots": 0,
            "live_hashing_gpus": 0,
            "running_no_live_billable_slots": [],
            "negative_slots": [],
            "stuck_slots": [],
        }
        health_payload = {
            "health": "healthy",
            "target_count": 40,
            "slot_count": 40,
            "runtime_failures": [],
            "guard_issues": [],
            "stale_heartbeats": [],
        }
        shadow_payload = {
            "ok": True,
            "unsafe_targets": [],
            "missing_targets": [],
            "mismatches": [],
            "warnings": [],
            "diversification": {"unique_target_profiles": 4, "top_profile_share": 0.25},
        }
        worker_passes = [
            [
                {
                    "org": "kry1",
                    "apply": True,
                    "targets": 10,
                    "action_counts": {"observe": 1},
                    "results": [{"slot_name": "roi01", "action": "observe", "ok": True}],
                }
            ],
            [
                {
                    "org": "kry1",
                    "apply": True,
                    "targets": 10,
                    "action_counts": {"patch": 1},
                    "results": [{"slot_name": "roi01", "action": "patch", "ok": True}],
                }
            ],
        ]

        with (
            mock.patch.object(
                rollout.fleet_scheduler,
                "schedule_once",
                side_effect=[initial_scheduler_payload, replacement_scheduler_payload, final_scheduler_payload],
            ) as schedule_once,
            mock.patch.object(rollout.reporter, "build_report", return_value=report_payload),
            mock.patch.object(rollout.health, "build_health", return_value=health_payload),
            mock.patch.object(rollout.shadow_compare, "build_shadow_compare", return_value=shadow_payload),
            mock.patch.object(rollout, "_run_org_workers", side_effect=worker_passes) as run_workers,
        ):
            payload = rollout.run_rollout(
                stage="one-org",
                org_label="kry1",
                db_path=self.db_path,
                price=0.64,
                fee=0.01,
                apply_workers=True,
                allow_pending_retarget=True,
                skip_guard=True,
            )

        self.assertEqual(run_workers.call_count, 2)
        self.assertEqual(schedule_once.call_count, 3)
        self.assertEqual(len(payload["workers"]), 2)
        self.assertEqual(payload["workers"][0]["action_counts"], {"observe": 1})
        self.assertEqual(payload["workers"][1]["action_counts"], {"patch": 1})

    def test_parallel_org_workers_return_large_payloads_without_pool_deadlock(self) -> None:
        def fake_run_once(**kwargs):
            return {
                "org": kwargs["org_label"],
                "apply": kwargs["apply"],
                "targets": 10,
                "action_counts": {"observe": 1},
                "results": [{"slot_name": "roi01", "action": "observe", "blob": "x" * 1_000_000}],
            }

        with mock.patch.object(rollout.org_worker, "run_once", side_effect=fake_run_once):
            payloads = rollout._run_org_workers(
                ["kry1", "kray"],
                db_path=self.db_path,
                apply_workers=False,
                allow_live_retarget=False,
                allow_pending_retarget=False,
                pending_retarget_after_seconds=60,
                pending_status_retarget_after_seconds=None,
                worker_parallelism=2,
            )

        self.assertEqual([payload["org"] for payload in payloads], ["kry1", "kray"])
        self.assertNotIn("blob", payloads[0]["results"][0])
        self.assertEqual(payloads[0]["results"][0]["action"], "observe")

    def test_parallel_org_worker_batches_do_not_share_api_key(self) -> None:
        tasks = [
            {"org_label": "kray", "_api_key_env": "SALAD_API_KEY_2"},
            {"org_label": "kry1", "_api_key_env": "SALAD_API_KEY_KRY1"},
            {"org_label": "kray2", "_api_key_env": "SALAD_API_KEY_2"},
            {"org_label": "kray3", "_api_key_env": "SALAD_API_KEY_2"},
        ]

        batches = rollout._batch_org_worker_tasks(tasks, max_workers=4)

        self.assertEqual(
            [[task["org_label"] for task in batch] for batch in batches],
            [["kray", "kry1"], ["kray2"], ["kray3"]],
        )

    def test_parallel_org_worker_times_out_stuck_child(self) -> None:
        def stuck_run_once(**kwargs):
            time.sleep(5)
            return {
                "org": kwargs["org_label"],
                "apply": kwargs["apply"],
                "targets": 10,
                "action_counts": {"observe": 1},
                "results": [{"slot_name": "roi01", "action": "observe"}],
            }

        with (
            mock.patch.object(rollout.org_worker, "run_once", side_effect=stuck_run_once),
            mock.patch.object(rollout, "_org_worker_timeout_seconds", return_value=0.1),
        ):
            with self.assertRaisesRegex(TimeoutError, "org worker kry1 timed out"):
                rollout._run_org_workers(
                    ["kry1", "kray"],
                    db_path=self.db_path,
                    apply_workers=False,
                    allow_live_retarget=False,
                    allow_pending_retarget=False,
                    pending_retarget_after_seconds=60,
                    pending_status_retarget_after_seconds=None,
                    worker_parallelism=2,
                )


if __name__ == "__main__":
    unittest.main()
